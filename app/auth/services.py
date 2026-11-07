"""Per-request assembly of the authentication services from a database session."""

from dataclasses import dataclass, field

from app.auth.bootstrap import BootstrapService, SetupState
from app.auth.login import LoginService
from app.auth.passwords import PasswordHasher
from app.auth.platform_signin import PlatformIdentity, PlatformLogin
from app.auth.policy import PasswordPolicy
from app.auth.providers import ProviderContext, ProviderRegistry, default_registry
from app.auth.sessions import SessionService
from app.auth.throttle import KeyedLocks, LoginThrottleService
from app.auth.tokens import ApiTokenService
from app.auth.users import UserService
from app.config import Settings
from app.db.engine import Session
from app.db.repositories.api_token import ApiTokenRepository
from app.db.repositories.clocked import Clock, utc_now
from app.db.repositories.login_throttle import LoginThrottleRepository
from app.db.repositories.user import UserRepository
from app.db.repositories.user_session import UserSessionRepository
from app.db.repositories.webhook import WebhookRepository
from app.services.activity import ActivityRecorder


@dataclass(slots=True)
class AuthRuntime:
    """Process-wide authentication state; one instance lives on the application."""

    hasher: PasswordHasher
    registry: ProviderRegistry = field(default_factory=default_registry)
    setup: SetupState = field(default_factory=SetupState)
    clock: Clock = utc_now
    locks: KeyedLocks = field(default_factory=KeyedLocks)
    platform: PlatformIdentity | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "AuthRuntime":
        """Build the runtime from configuration."""
        return cls(
            hasher=PasswordHasher.from_settings(settings),
            platform=PlatformIdentity.from_settings(settings) if settings.jwt_enabled else None,
        )


@dataclass(frozen=True, slots=True)
class AuthServices:
    """Everything the authentication routes call, bound to one request's database session."""

    users: UserRepository
    sessions: SessionService
    login: LoginService
    bootstrap: BootstrapService
    user_admin: UserService
    tokens: ApiTokenService
    policy: PasswordPolicy
    throttle: LoginThrottleService
    platform: PlatformLogin | None = None


def build_session_service(db: Session, settings: Settings, runtime: AuthRuntime) -> SessionService:
    """Build only the session service, for the per-request principal lookup."""
    return SessionService(
        UserSessionRepository(db, runtime.clock),
        UserRepository(db, runtime.clock),
        lifetime=settings.session_lifetime,
        idle_timeout=settings.session_idle_timeout,
    )


def build_token_service(
    db: Session, runtime: AuthRuntime, activity: ActivityRecorder
) -> ApiTokenService:
    """Build only the token service, for the per-request bearer lookup."""
    return ApiTokenService(
        db=db,
        tokens=ApiTokenRepository(db, runtime.clock),
        users=UserRepository(db, runtime.clock),
        activity=activity,
        clock=runtime.clock,
    )


def build_platform_login(
    db: Session,
    settings: Settings,
    runtime: AuthRuntime,
    activity: ActivityRecorder | None = None,
) -> PlatformLogin | None:
    """Build the access platform sign-in over a database session; None while the feature is off."""
    if runtime.platform is None:
        return None
    return PlatformLogin(
        db=db,
        platform=runtime.platform,
        users=UserRepository(db, runtime.clock),
        sessions=build_session_service(db, settings, runtime),
        activity=activity or ActivityRecorder(db, runtime.clock),
        hasher=runtime.hasher,
    )


def build_services(
    db: Session,
    settings: Settings,
    runtime: AuthRuntime,
    activity: ActivityRecorder,
    *,
    enforce_address: bool | None = None,
) -> AuthServices:
    """Wire the services for one request; pass the request's recorder so every service shares it.

    enforce_address says whether an address lock may refuse this request; unset, it holds only
    behind trusted proxies.
    """
    users = UserRepository(db, runtime.clock)
    sessions = build_session_service(db, settings, runtime)
    policy = PasswordPolicy.from_settings(settings)
    tokens = build_token_service(db, runtime, activity)
    throttle = LoginThrottleService(
        LoginThrottleRepository(db, runtime.clock),
        runtime.clock,
        max_failures=settings.login_max_failures,
        window=settings.login_lockout_window,
        enforce_address=bool(settings.trusted_proxies)
        if enforce_address is None
        else enforce_address,
        locks=runtime.locks,
    )
    return AuthServices(
        users=users,
        sessions=sessions,
        login=LoginService(
            db=db,
            registry=runtime.registry,
            provider_context=ProviderContext(
                users,
                runtime.hasher,
                administrators_only=settings.jwt_enabled
                and settings.jwt_password_sign_in == "administrators",
            ),
            users=users,
            throttle=throttle,
            activity=activity,
            sessions=sessions,
        ),
        bootstrap=BootstrapService(
            db=db,
            state=runtime.setup,
            users=users,
            activity=activity,
            hasher=runtime.hasher,
            policy=policy,
            throttle=throttle,
        ),
        user_admin=UserService(
            db=db,
            users=users,
            sessions=sessions,
            activity=activity,
            hasher=runtime.hasher,
            policy=policy,
            throttle=throttle,
            tokens=tokens,
            webhooks=WebhookRepository(db, runtime.clock),
        ),
        tokens=tokens,
        policy=policy,
        throttle=throttle,
        platform=build_platform_login(db, settings, runtime, activity),
    )
