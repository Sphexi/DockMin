"""Reveals one secret stack variable to an administrator inside a bounded, audited window."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, NoReturn

from app.auth.errors import PermissionDenied, ProductError, ResourceNotFound
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.clocked import Clock
from app.db.repositories.registry_credential import SecretUnreadableError
from app.db.repositories.stack import StackKind, StackRepository, StoredStack
from app.db.repositories.user_session import UserSessionRepository
from app.services.activity import ActivityRecorder, audited, current_audit, environment_argument
from app.services.secret_mask import MASK
from app.services.stack_views import VariableView, variable_views

__all__ = [
    "CHANGED_SINCE_REVIEW",
    "REVEAL_WINDOW_CLOSED",
    "SECRETS_UNREADABLE",
    "RevealRefused",
    "RevealedValue",
    "RevealedVariable",
    "StackLookup",
    "StackVariableReveal",
]

REVEAL_WINDOW_CLOSED: Final = "reveal_window_closed"
CHANGED_SINCE_REVIEW: Final = "changed_since_review"
SECRETS_UNREADABLE: Final = "secrets_unreadable"

_CLOSED_MESSAGE: Final = "The reveal window is closed."
_CHANGED_MESSAGE: Final = "The variable changed after the page was loaded."
_UNREADABLE_MESSAGE: Final = "The stored value cannot be read with this server's key."

type StackLookup = Callable[[Principal, int, str], Awaitable[StoredStack]]


class RevealRefused(ProductError):
    """Raised when a reveal is refused for a reason the caller can act on; `reason` names it."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class RevealedValue:
    """A revealed secret that keeps itself out of reprs, logs and formatted text."""

    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text = text

    def reveal(self) -> str:
        """Return the plaintext; the only way to read it."""
        return self._text

    def __repr__(self) -> str:
        return "RevealedValue(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"

    def __reduce__(self) -> NoReturn:
        raise TypeError("a revealed value cannot be serialised")


@dataclass(frozen=True, slots=True)
class RevealedVariable:
    """A revealed variable, the window it was revealed in and the reveals the window has left."""

    variable: VariableView
    value: RevealedValue
    window_expires_at: str
    reveals_left: int


class StackVariableReveal:
    """Opens a secret variable's value for an administrator whose reveal window is open."""

    def __init__(
        self,
        *,
        db: Session,
        stacks: StackRepository,
        sessions: UserSessionRepository,
        compose: StackLookup,
        swarm: StackLookup,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._db = db
        self._stacks = stacks
        self._sessions = sessions
        self._compose = compose
        self._swarm = swarm
        self._activity = activity
        self._clock = clock

    async def variable_view(
        self,
        actor: Principal,
        environment_id: int,
        stack_kind: StackKind,
        stack_name: str,
        variable_id: int,
    ) -> VariableView:
        """Return one secret variable as the Variables tab shows it; administrators only."""
        _require_administrator(actor)
        stack = await self._load(actor, environment_id, stack_kind, stack_name)
        views = await variable_views(
            self._stacks, stack.id, actor, open_masks=True, only=variable_id
        )
        if not views or not views[0].secret:
            raise ResourceNotFound("not found")
        return views[0]

    async def window_open(self, actor: Principal) -> bool:
        """Report whether the caller's session could spend a reveal now; spends nothing."""
        _require_administrator(actor)
        if actor.session_id is None:
            return False
        window = await self._sessions.reveal_window(actor.session_id, self._clock())
        return window is not None

    @audited(
        action="stack_variable_revealed",
        target_type="stack",
        target_id=lambda arguments: arguments["stack_name"],
        environment_id=environment_argument,
        announces=False,
    )
    async def reveal(
        self,
        actor: Principal,
        environment_id: int,
        stack_kind: StackKind,
        stack_name: str,
        variable_id: int,
        expected_name: str,
    ) -> RevealedVariable:
        """Spend one reveal of the caller's window and return the secret variable's value.

        PermissionDenied unless the caller is an administrator with a session. ResourceNotFound
        for a missing stack or variable and for a variable that is not secret. RevealRefused for
        a closed window, for a variable whose name is not `expected_name`, and for a value the
        server's key cannot open; none of these spends a reveal.
        """
        audit = current_audit()
        _require_administrator(actor)
        session_id = actor.session_id
        if session_id is None:
            raise PermissionDenied("a reveal needs a signed-in session")
        stack = await self._load(actor, environment_id, stack_kind, stack_name)
        audit.target_id = stack.name
        named = await self._secret(stack.id, variable_id)
        audit.detail.update(name=stack.name, stack_kind=stack_kind, variable=named)
        if named != expected_name:
            _refuse(CHANGED_SINCE_REVIEW, _CHANGED_MESSAGE)
        now = self._clock()
        async with self._db.transaction():
            window = await self._sessions.take_reveal(session_id, now)
            if window is None:
                audit.refuse()
                raise RevealRefused(REVEAL_WINDOW_CLOSED, _CLOSED_MESSAGE)
            try:
                opened = await self._stacks.reveal_variable(stack.id, variable_id)
            except SecretUnreadableError:
                _refuse(SECRETS_UNREADABLE, _UNREADABLE_MESSAGE)
            if opened is None or not opened.is_secret or opened.value is None:
                raise ResourceNotFound("not found")
            if opened.name != expected_name:
                _refuse(CHANGED_SINCE_REVIEW, _CHANGED_MESSAGE)
            audit.detail.update(
                value_version=opened.value_token or "",
                window_expires_at=window.ends_at,
                reveals_left=window.reveals_left,
            )
            revealed = RevealedValue(opened.value)
        return RevealedVariable(
            variable=VariableView(variable_id, opened.name, True, None, MASK, can_reveal=True),
            value=revealed,
            window_expires_at=window.ends_at,
            reveals_left=window.reveals_left,
        )

    async def _load(
        self, actor: Principal, environment_id: int, stack_kind: StackKind, stack_name: str
    ) -> StoredStack:
        lookup = self._compose if stack_kind == "compose" else self._swarm
        return await lookup(actor, environment_id, stack_name)

    async def _secret(self, stack_id: int, variable_id: int) -> str:
        for variable in await self._stacks.variables(stack_id):
            if variable.id == variable_id and variable.is_secret:
                return variable.name
        raise ResourceNotFound("not found")


def _require_administrator(actor: Principal) -> None:
    if not actor.is_administrator:
        raise PermissionDenied("administrator role required")


def _refuse(reason: str, message: str) -> NoReturn:
    current_audit().reason = reason
    raise RevealRefused(reason, message)
