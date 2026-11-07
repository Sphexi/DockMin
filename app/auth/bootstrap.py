"""First-run bootstrap: the setup token, and creation of the first administrator."""

import hmac
import secrets
from dataclasses import dataclass
from functools import partial

from app.auth.errors import LockedOut, SetupClosed, SetupTokenRejected, ValidationFailed
from app.auth.passwords import PasswordHasher
from app.auth.policy import PasswordPolicy, check_username
from app.auth.throttle import LoginThrottleService
from app.db.engine import Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.errors import DuplicateError
from app.db.repositories.user import User, UserRepository
from app.logging import get_logger
from app.services.activity import BOOTSTRAP_ACTION, ActivityRecorder, AnonymousActor, UserActor

logger = get_logger(__name__)


@dataclass(slots=True)
class SetupState:
    """Process-wide bootstrap state; once complete is True it never reverts."""

    token: str | None = None
    complete: bool = False


def issue_setup_token(state: SetupState) -> str:
    """Create a fresh setup token for this process and keep it only in memory."""
    state.token = secrets.token_urlsafe(24)
    return state.token


def announce_setup_token(token: str) -> None:
    """Print the token to the log, the only place it is ever shown."""
    # The wording avoids "token", "secret" or "password" next to a colon or equals sign: the log
    # scrubber redacts the value that follows such a name, which would hide the very value the
    # operator needs.
    logger.warning(
        "First-run setup is required. Open /setup and submit this one-time value: %s", token
    )


class BootstrapService:
    """Decides whether bootstrap is open and performs it."""

    def __init__(
        self,
        *,
        db: Session,
        state: SetupState,
        users: UserRepository,
        activity: ActivityRecorder,
        hasher: PasswordHasher,
        policy: PasswordPolicy,
        throttle: LoginThrottleService,
    ) -> None:
        self._db = db
        self._state = state
        self._users = users
        self._activity = activity
        self._hasher = hasher
        self._policy = policy
        self._throttle = throttle

    async def is_open(self) -> bool:
        """Report whether no administrator exists yet, latching the answer once one does."""
        if self._state.complete:
            return False
        if await self._users.count_administrators() > 0:
            self._close()
            return False
        return True

    async def complete(
        self,
        *,
        setup_token: str,
        username: str,
        password: str,
        password_confirm: str,
        remote_address: str,
    ) -> User:
        """Create the first administrator, raising a ProductError subclass when it cannot."""
        if not await self.is_open():
            raise SetupClosed
        retry_after = await self._throttle.retry_after(None, remote_address)
        if retry_after > 0:
            raise LockedOut(retry_after)
        expected = self._state.token
        if expected is None or not hmac.compare_digest(
            setup_token.strip().encode("utf-8"), expected.encode("utf-8")
        ):
            await self._reject_token(remote_address)
            raise SetupTokenRejected

        try:
            user = await self._create_administrator(
                username=username,
                password=password,
                password_confirm=password_confirm,
                remote_address=remote_address,
            )
        except ValidationFailed as invalid:
            # The token was right, so this is someone who held it; the attempt must leave a trace.
            await self._record_rejection(
                remote_address, "validation_failed", {"fields": ",".join(sorted(invalid.fields))}
            )
            raise
        self._close()
        logger.info("bootstrap_completed", extra={"user_id": user.id})
        return user

    async def _create_administrator(
        self, *, username: str, password: str, password_confirm: str, remote_address: str
    ) -> User:
        username = username.strip()
        check_username(username)
        if password != password_confirm:
            raise ValidationFailed({"password_confirm": "The passwords do not match."})
        self._policy.check(password, username)
        password_hash = await self._hasher.hash(password)

        async with self._db.transaction():
            # Re-checked under the write lock so two submissions cannot both succeed.
            if await self._users.count_administrators() > 0:
                raise SetupClosed
            try:
                user = await self._users.create(username, password_hash, "administrator")
            except DuplicateError:
                raise ValidationFailed({"username": "That username is taken."}) from None
            await self._activity.record_event(
                UserActor(user.id),
                action=BOOTSTRAP_ACTION,
                target_type="user",
                target_id=str(user.id),
                result="success",
                detail={"address": remote_address},
                category="authentication",
            )
        return user

    def _close(self) -> None:
        self._state.complete = True
        self._state.token = None

    async def _reject_token(self, remote_address: str) -> None:
        await self._throttle.record_failure(None, remote_address)
        await self._record_rejection(remote_address, "bad_setup_token")
        logger.warning("bootstrap_token_rejected", extra={"address": remote_address})

    async def _record_rejection(
        self, remote_address: str, reason: str, detail: dict[str, DetailValue] | None = None
    ) -> None:
        await retry_busy(
            partial(
                self._activity.record_event,
                AnonymousActor(),
                action=BOOTSTRAP_ACTION,
                target_type="user",
                result="failure",
                detail={**(detail or {}), "address": remote_address, "reason": reason},
            )
        )
