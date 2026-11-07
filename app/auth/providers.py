"""Authentication providers: the protocol, the registry that names them, and the internal one."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from app.auth.passwords import PasswordHasher
from app.auth.principal import Credentials, Principal
from app.db.repositories.user import UserRepository

FailureReason = Literal[
    "unknown_user", "bad_password", "account_disabled", "password_sign_in_disabled"
]

INTERNAL_PROVIDER = "internal"


class AuthenticationFailed(Exception):
    """Raised by a provider to reject credentials; reason is for the audit trail, not the user."""

    def __init__(self, reason: FailureReason, user_id: int | None = None) -> None:
        super().__init__(reason)
        self.reason: FailureReason = reason
        self.user_id = user_id


class AuthProvider(Protocol):
    """Turns credentials into a Principal or raises AuthenticationFailed."""

    async def authenticate(self, credentials: Credentials) -> Principal:
        """Check the credentials and return the caller they identify."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """What a provider factory may draw on."""

    users: UserRepository
    hasher: PasswordHasher
    administrators_only: bool = False


ProviderFactory = Callable[[ProviderContext], AuthProvider]


class UnknownProviderError(KeyError):
    """Raised when a provider name was never registered."""


class ProviderRegistry:
    """Maps provider names to factories; the names it holds are the enabled methods."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        """Add a provider; a second registration under one name is a programming error."""
        if name in self._factories:
            raise ValueError(f"authentication provider {name!r} is already registered")
        self._factories[name] = factory

    def names(self) -> list[str]:
        """Return the registered provider names in registration order."""
        return list(self._factories)

    def create(self, name: str, context: ProviderContext) -> AuthProvider:
        """Build the named provider for one request."""
        try:
            factory = self._factories[name]
        except KeyError:
            raise UnknownProviderError(name) from None
        return factory(context)


class InternalAuthProvider:
    """Checks a username and password against the user table."""

    def __init__(
        self, users: UserRepository, hasher: PasswordHasher, *, administrators_only: bool = False
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._administrators_only = administrators_only

    async def authenticate(self, credentials: Credentials) -> Principal:
        """Verify the password, upgrading the stored hash when its parameters are out of date."""
        user = await self._users.get_by_username(credentials.username)
        if user is None:
            # An unknown name must cost as much as a known one, or response time reveals which
            # usernames exist.
            await self._hasher.verify_against_nothing(credentials.password)
            raise AuthenticationFailed("unknown_user")
        if not await self._hasher.verify(user.password_hash, credentials.password):
            raise AuthenticationFailed("bad_password", user.id)
        # Checked after the hash so a disabled account is indistinguishable by timing.
        if not user.is_enabled:
            raise AuthenticationFailed("account_disabled", user.id)
        if self._administrators_only and user.role != "administrator":
            raise AuthenticationFailed("password_sign_in_disabled", user.id)
        if self._hasher.needs_rehash(user.password_hash):
            await self._users.set_password_hash(
                user.id, await self._hasher.hash(credentials.password)
            )
        return Principal(user.id, user.username, user.role)


def internal_provider_factory(context: ProviderContext) -> AuthProvider:
    """Build the internal provider from a request's context."""
    return InternalAuthProvider(
        context.users, context.hasher, administrators_only=context.administrators_only
    )


def default_registry() -> ProviderRegistry:
    """Return a registry holding every provider this build ships."""
    registry = ProviderRegistry()
    registry.register(INTERNAL_PROVIDER, internal_provider_factory)
    return registry
