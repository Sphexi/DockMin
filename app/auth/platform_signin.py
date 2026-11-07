"""Sign-in from an access platform token: session exchange, binding and linked accounts."""

import secrets
import ssl
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.auth.jwt_identity import Assertion, JwtConfig, JwtRefused, JwtVerifier
from app.auth.jwt_keys import KeyStore
from app.auth.passwords import PasswordHasher
from app.auth.policy import USERNAME_PATTERN
from app.auth.principal import Principal
from app.auth.sessions import IssuedSession, SessionService
from app.config import Settings
from app.db.engine import Session, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.errors import DuplicateError
from app.db.repositories.user import User, UserRepository
from app.logging import get_logger
from app.services.activity import LOGIN_ACTION, ActivityRecorder, AnonymousActor, UserActor
from app.services.jwks_fetcher import JwksFetcher

logger = get_logger(__name__)

JWT_METHOD: Final = "jwt"
ASSERTION_ENDED: Final = "platform_assertion_ended"
IDENTITY_CHANGED: Final = "platform_identity_changed"
REFUSAL_LOG_INTERVAL: Final = 60.0
REMEMBERED_REFUSALS: Final = 1000
UNUSABLE_PASSWORD_BYTES: Final = 32


class PlatformIdentity:
    """The verifier of one running instance, and which refusals it has already reported."""

    def __init__(
        self,
        *,
        header: str,
        create_users: bool,
        verifier: JwtVerifier,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.header = header
        self.create_users = create_users
        self._verifier = verifier
        self._monotonic = monotonic
        self._logged: dict[str, float] = {}
        self._recorded: OrderedDict[str, None] = OrderedDict()

    @classmethod
    def from_settings(
        cls, settings: Settings, *, trust: ssl.SSLContext | None = None
    ) -> "PlatformIdentity":
        """Build the verifier from the access platform settings, which must be complete."""
        assert settings.jwt_header is not None
        assert settings.jwt_issuer is not None
        assert settings.jwt_audience is not None
        fetcher = (
            None
            if settings.jwt_jwks_url is None
            else JwksFetcher(settings.jwt_jwks_url, settings.jwt_algorithms, trust=trust)
        )
        config = JwtConfig(
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            algorithms=tuple(settings.jwt_algorithms),
            identity_claim=settings.jwt_identity_claim,
            username_claim=settings.jwt_username_claim or settings.jwt_identity_claim,
            max_age=settings.jwt_max_age.total_seconds(),
        )
        keys = KeyStore(fixed=settings.jwt_file_keys, fetcher=fetcher)
        return cls(
            header=settings.jwt_header,
            create_users=settings.jwt_create_users,
            verifier=JwtVerifier(config, keys),
        )

    async def assertion(self, header_value: str | None) -> Assertion | None:
        """Return what a header value asserts, or None for no header or a token not accepted."""
        if header_value is None:
            return None
        try:
            return await self._verifier.verify(header_value)
        except JwtRefused as refused:
            self._log_refusal(refused.reason)
        except Exception as error:
            self._log_refusal(f"verifier_error_{type(error).__name__}", unexpected=True)
        return None

    def first_refusal(self, digest: str) -> bool:
        """Report whether this token has not been recorded as refused before; remember it."""
        if digest in self._recorded:
            self._recorded.move_to_end(digest)
            return False
        self._recorded[digest] = None
        while len(self._recorded) > REMEMBERED_REFUSALS:
            self._recorded.popitem(last=False)
        return True

    def _log_refusal(self, reason: str, *, unexpected: bool = False) -> None:
        now = self._monotonic()
        last = self._logged.get(reason)
        if last is not None and now - last < REFUSAL_LOG_INTERVAL:
            return
        self._logged[reason] = now
        if unexpected:
            logger.error("jwt_verifier_failed", extra={"reason": reason}, exc_info=True)
            return
        logger.warning("jwt_refused", extra={"reason": reason})


@dataclass(frozen=True, slots=True)
class PlatformSession:
    """A session opened from an assertion, and the principal it belongs to."""

    principal: Principal
    session: IssuedSession


class _Refused(Exception):
    def __init__(self, reason: str, user_id: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_id = user_id


class PlatformLogin:
    """Turns assertions into sessions and decides whether a session still stands on one."""

    def __init__(
        self,
        *,
        db: Session,
        platform: PlatformIdentity,
        users: UserRepository,
        sessions: SessionService,
        activity: ActivityRecorder,
        hasher: PasswordHasher,
    ) -> None:
        self._db = db
        self._platform = platform
        self._users = users
        self._sessions = sessions
        self._activity = activity
        self._hasher = hasher

    async def candidate(self, assertion: Assertion | None) -> str | None:
        """Return the username an assertion would sign in as, or None when it would not."""
        if assertion is None:
            return None
        user = await self._users.get_by_external_identity(assertion.identity)
        return user.username if user is not None and user.is_enabled else None

    async def binding_ending(self, principal: Principal, assertion: Assertion | None) -> str | None:
        """Return the reason a platform session must end, or None while its assertion stands."""
        if assertion is None:
            return ASSERTION_ENDED
        owner = await self._users.get_by_external_identity(assertion.identity)
        if owner is None or owner.id != principal.user_id:
            return IDENTITY_CHANGED
        return None

    async def end_session(
        self,
        principal: Principal,
        reason: str,
        *,
        remote_address: str,
        method: str = JWT_METHOD,
    ) -> None:
        """Delete the session and record why, unless another request already ended it."""
        assert principal.session_id is not None
        async with self._db.transaction():
            ended = await self._sessions.end(principal.session_id)
            if ended:
                await self._activity.record_event(
                    UserActor(principal.user_id),
                    action="logout",
                    target_type="user",
                    target_id=str(principal.user_id),
                    result="success",
                    detail={"reason": reason, "method": method, "address": remote_address},
                    category="authentication",
                )
        if ended:
            logger.info(
                "logout_succeeded",
                extra={
                    "user_id": principal.user_id,
                    "username": principal.username,
                    "address": remote_address,
                    "method": method,
                    "reason": reason,
                },
            )

    async def exchange(
        self,
        assertion: Assertion,
        *,
        remote_address: str,
        user_agent: str | None,
        may_create: bool = True,
    ) -> PlatformSession | None:
        """Open a session for the account an assertion names; None when refused and recorded.

        `may_create` false refuses an identity with no linked account even when accounts are
        created on first sign-in.
        """
        try:
            user = await self._account_of(assertion, remote_address, may_create)
        except _Refused as refused:
            await self._record_refusal(assertion, refused, remote_address)
            return None
        issued = await self._open_session(user, remote_address, user_agent)
        logger.info(
            "login_succeeded",
            extra={
                "user_id": user.id,
                "username": user.username,
                "address": remote_address,
                "method": JWT_METHOD,
            },
        )
        return PlatformSession(
            Principal(user.id, user.username, user.role, issued.session_id, JWT_METHOD), issued
        )

    async def _account_of(
        self, assertion: Assertion, remote_address: str, may_create: bool
    ) -> User:
        user = await self._users.get_by_external_identity(assertion.identity)
        if user is None:
            if not (self._platform.create_users and may_create):
                raise _Refused("jwt_unlinked")
            return await self._create_account(assertion, remote_address)
        if not user.is_enabled:
            raise _Refused("jwt_account_disabled", user.id)
        return user

    async def _create_account(self, assertion: Assertion, remote_address: str) -> User:
        username = assertion.username
        if username is None or USERNAME_PATTERN.fullmatch(username) is None:
            raise _Refused("jwt_username_invalid")
        password_hash = await self._hasher.hash(secrets.token_hex(UNUSABLE_PASSWORD_BYTES))
        try:
            async with self._db.transaction():
                user = await self._users.create(
                    username, password_hash, "standard", external_identity=assertion.identity
                )
                await self._activity.record_event(
                    UserActor(user.id),
                    action="user_created",
                    target_type="user",
                    target_id=str(user.id),
                    result="success",
                    detail={"role": "standard", "method": JWT_METHOD, "address": remote_address},
                    category="user_action",
                )
        except DuplicateError:
            linked = await self._users.get_by_external_identity(assertion.identity)
            if linked is None:
                raise _Refused("jwt_username_taken") from None
            if not linked.is_enabled:
                raise _Refused("jwt_account_disabled", linked.id) from None
            return linked
        return user

    async def _open_session(
        self, user: User, remote_address: str, user_agent: str | None
    ) -> IssuedSession:
        async def commit() -> IssuedSession:
            async with self._db.transaction():
                await self._users.record_login(user.id)
                await self._activity.record_event(
                    UserActor(user.id),
                    action=LOGIN_ACTION,
                    target_type="user",
                    target_id=str(user.id),
                    result="success",
                    detail={"address": remote_address, "method": JWT_METHOD},
                    category="authentication",
                )
                issued = await self._sessions.issue(
                    user.id,
                    user_agent=user_agent,
                    remote_address=remote_address,
                    sign_in_method=JWT_METHOD,
                )
                await self._sessions.reap()
                return issued

        try:
            return await retry_busy(commit)
        except Exception:
            await self._activity.record_failure(
                UserActor(user.id),
                action=LOGIN_ACTION,
                target_type="user",
                target_id=str(user.id),
                reason="commit_failed",
                detail={"address": remote_address, "method": JWT_METHOD},
                category="authentication",
            )
            raise

    async def _record_refusal(
        self, assertion: Assertion, refused: _Refused, remote_address: str
    ) -> None:
        if not self._platform.first_refusal(assertion.digest):
            return
        detail: dict[str, DetailValue] = {
            "address": remote_address,
            "method": JWT_METHOD,
            "identity": assertion.identity,
        }
        await self._activity.record_failure(
            AnonymousActor(),
            action=LOGIN_ACTION,
            target_type="user",
            target_id=None if refused.user_id is None else str(refused.user_id),
            reason=refused.reason,
            detail=detail,
            category="authentication",
        )
        extra: dict[str, object] = {
            "reason": refused.reason,
            "address": remote_address,
            "method": JWT_METHOD,
        }
        if refused.user_id is not None:
            extra["user_id"] = refused.user_id
        logger.warning("login_failed", extra=extra)
