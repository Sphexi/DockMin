"""The login flow: lockout check, provider authentication, bookkeeping and session issue."""

from dataclasses import dataclass
from functools import partial

from app.auth.errors import InvalidCredentials, LockedOut
from app.auth.passwords import HashingBusy
from app.auth.principal import Credentials, PasswordCredentials, Principal
from app.auth.providers import (
    AuthenticationFailed,
    ProviderContext,
    ProviderRegistry,
)
from app.auth.sessions import IssuedSession, SessionService
from app.auth.throttle import SUBJECT_MAX, LoginThrottleService
from app.db.engine import Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.user import UserRepository
from app.db.repositories.user_session import SignInMethod
from app.logging import get_logger
from app.services.activity import (
    LOGIN_ACTION,
    ActivityRecorder,
    AnonymousActor,
    UserActor,
    audited,
)

logger = get_logger(__name__)

PASSWORD_METHOD: SignInMethod = "password"


@dataclass(frozen=True, slots=True)
class LoginResult:
    """A successful login: the caller and the cookie value of the session just created."""

    principal: Principal
    session: IssuedSession


class LoginService:
    """Runs one login attempt end to end and records it whatever the outcome."""

    def __init__(
        self,
        *,
        db: Session,
        registry: ProviderRegistry,
        provider_context: ProviderContext,
        users: UserRepository,
        throttle: LoginThrottleService,
        activity: ActivityRecorder,
        sessions: SessionService,
    ) -> None:
        self._db = db
        self._registry = registry
        self._provider_context = provider_context
        self._users = users
        self._throttle = throttle
        self._activity = activity
        self._sessions = sessions

    async def login(
        self,
        provider: str,
        credentials: Credentials,
        *,
        remote_address: str,
        user_agent: str | None,
    ) -> LoginResult:
        """Authenticate, or raise LockedOut or InvalidCredentials; every outcome is recorded."""
        username = credentials.username.strip()[:SUBJECT_MAX]
        # Attempts sharing a username or an address run one at a time, so a burst cannot all read
        # the counter before the first of them has written it.
        async with self._throttle.guard(username, remote_address):
            return await self._login_guarded(
                provider,
                credentials,
                username=username,
                remote_address=remote_address,
                user_agent=user_agent,
            )

    async def _login_guarded(
        self,
        provider: str,
        credentials: Credentials,
        *,
        username: str,
        remote_address: str,
        user_agent: str | None,
    ) -> LoginResult:
        retry_after = await self._throttle.retry_after(username, remote_address)
        if retry_after > 0:
            await self._record_refusal(username, remote_address, "locked_out")
            raise LockedOut(retry_after)

        # Counted before the password is checked, so an attempt that is cancelled or crashes
        # mid-verification has still cost one; success takes the count back.
        await self._throttle.record_failure(username, remote_address)
        authenticator = self._registry.create(provider, self._provider_context)
        try:
            principal = await authenticator.authenticate(
                PasswordCredentials(username, credentials.password)
            )
        except AuthenticationFailed as failure:
            await self._record_failure(username, remote_address, failure)
            raise InvalidCredentials from None
        except HashingBusy:
            # Refused for lack of capacity, not judged: the caller must not pay for it.
            await retry_busy(partial(self._throttle.refund, username, remote_address))
            await self._record_refusal(username, remote_address, "server_busy")
            raise

        async def commit() -> IssuedSession:
            async with self._db.transaction():
                await self._throttle.settle_success(username, remote_address)
                await self._users.record_login(principal.user_id)
                await self._activity.record_event(
                    UserActor(principal.user_id),
                    action=LOGIN_ACTION,
                    target_type="user",
                    target_id=str(principal.user_id),
                    result="success",
                    detail={"address": remote_address, "method": PASSWORD_METHOD},
                    category="authentication",
                )
                issued = await self._sessions.issue(
                    principal.user_id, user_agent=user_agent, remote_address=remote_address
                )
                await self._sessions.reap()
                return issued

        try:
            issued = await retry_busy(commit)
        except Exception:
            await self._activity.record_failure(
                UserActor(principal.user_id),
                action=LOGIN_ACTION,
                target_type="user",
                target_id=str(principal.user_id),
                reason="commit_failed",
                detail={"address": remote_address, "method": PASSWORD_METHOD},
                category="authentication",
            )
            raise
        logger.info(
            "login_succeeded",
            extra={
                "user_id": principal.user_id,
                "username": principal.username,
                "address": remote_address,
                "method": PASSWORD_METHOD,
            },
        )
        return LoginResult(
            Principal(
                principal.user_id,
                principal.username,
                principal.role,
                issued.session_id,
                PASSWORD_METHOD,
            ),
            issued,
        )

    @audited(
        action="logout",
        target_type="user",
        target_id=lambda a: str(a["actor"].user_id),
        category="authentication",
    )
    async def logout(self, actor: Principal, *, remote_address: str) -> None:
        """End the caller's session and record it."""
        async with self._db.transaction():
            if actor.session_id is not None:
                await self._sessions.end(actor.session_id)
        logger.info(
            "logout_succeeded",
            extra={
                "user_id": actor.user_id,
                "username": actor.username,
                "address": remote_address,
                "method": actor.sign_in_method or PASSWORD_METHOD,
            },
        )

    async def _record_failure(
        self, username: str, remote_address: str, failure: AuthenticationFailed
    ) -> None:
        # The counter was written before verification and is not touched here, so a lock conflict
        # on this audit row cannot undo it.
        await self._record_attempt_failure(
            failure.user_id, username, remote_address, failure.reason
        )
        extra: dict[str, object] = {
            "user_id": failure.user_id,
            "reason": failure.reason,
            "address": remote_address,
            "method": PASSWORD_METHOD,
        }
        if failure.user_id is not None:
            account = await self._users.get(failure.user_id)
            if account is not None:
                extra["username"] = account.username
        # The submitted name is never logged: a mistyped password often lands in that field.
        logger.warning("login_failed", extra=extra)

    async def _record_refusal(self, username: str, remote_address: str, reason: str) -> None:
        existing = await self._users.get_by_username(username)
        await self._record_attempt_failure(
            None if existing is None else existing.id, username, remote_address, reason
        )
        extra: dict[str, object] = {
            "reason": reason,
            "address": remote_address,
            "method": PASSWORD_METHOD,
        }
        if existing is not None:
            extra["user_id"] = existing.id
            extra["username"] = existing.username
        logger.warning("login_refused", extra=extra)

    async def _record_attempt_failure(
        self, user_id: int | None, username: str, remote_address: str, reason: str
    ) -> None:
        detail: dict[str, DetailValue] = {
            "address": remote_address,
            "reason": reason,
            "username": username[:SUBJECT_MAX],
            "method": PASSWORD_METHOD,
        }
        await retry_busy(
            partial(
                self._activity.record_event,
                AnonymousActor(),
                action=LOGIN_ACTION,
                target_type="user",
                target_id=None if user_id is None else str(user_id),
                result="failure",
                detail=detail,
                category="authentication",
            )
        )
