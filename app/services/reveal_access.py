"""Opening a session's reveal window after the administrator re-enters their password."""

from typing import Annotated

from fastapi import Depends

from app.auth.current_password import verify_current_password
from app.auth.dependencies import Services, get_activity, get_runtime
from app.auth.errors import PermissionDenied, UserNotFound
from app.auth.passwords import PasswordHasher
from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.auth.throttle import LoginThrottleService
from app.db.engine import Session, get_session
from app.db.repositories.clocked import Clock, format_timestamp
from app.db.repositories.user import UserRepository
from app.db.repositories.user_session import REVEAL_WINDOW, UserSessionRepository
from app.services.activity import ActivityRecorder, audited, current_audit

PASSWORD_FIELD = "password"


class RevealAccess:
    """Grants a session a bounded window in which it may reveal masked secrets."""

    def __init__(
        self,
        *,
        db: Session,
        users: UserRepository,
        sessions: UserSessionRepository,
        activity: ActivityRecorder,
        hasher: PasswordHasher,
        throttle: LoginThrottleService,
        clock: Clock,
    ) -> None:
        self._db = db
        self._users = users
        self._sessions = sessions
        self._activity = activity
        self._hasher = hasher
        self._throttle = throttle
        self._clock = clock

    @audited(
        action="reveal_window_opened",
        target_type="user",
        target_id=lambda a: str(a["actor"].user_id),
        category="authentication",
    )
    async def open(self, actor: Principal, *, password: str, remote_address: str) -> None:
        """Open the caller's reveal window once the password re-entered is theirs."""
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        if actor.session_id is None:
            raise PermissionDenied("a reveal window needs a signed-in session")
        user = await self._users.get(actor.user_id)
        if user is None:
            raise UserNotFound(f"user {actor.user_id} does not exist")
        await verify_current_password(
            user,
            password,
            remote_address,
            throttle=self._throttle,
            hasher=self._hasher,
            field_name=PASSWORD_FIELD,
        )
        expires_at = self._clock() + REVEAL_WINDOW
        async with self._db.transaction():
            if not await self._sessions.open_reveal_window(actor.session_id, expires_at):
                raise PermissionDenied("the session has ended")
            detail = current_audit().detail
            detail["method"] = actor.sign_in_method
            detail["expires_at"] = format_timestamp(expires_at)


async def get_reveal_access(
    services: Services,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> RevealAccess:
    """Provide the reveal service bound to this request's database session and recorder."""
    return RevealAccess(
        db=db,
        users=services.users,
        sessions=UserSessionRepository(db, runtime.clock),
        activity=activity,
        hasher=runtime.hasher,
        throttle=services.throttle,
        clock=runtime.clock,
    )
