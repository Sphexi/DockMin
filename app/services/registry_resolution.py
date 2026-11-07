"""Turning a credential set into the registry login a pull carries, refreshing derived secrets."""

import asyncio
import sqlite3
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from pydantic import SecretStr

from app.auth.errors import ProductError
from app.auth.principal import Principal
from app.crypto.vault import Vault
from app.db.engine import Database
from app.db.repositories.clocked import Clock, parse_timestamp, utc_now
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.registry_access import RegistryAccessRepository
from app.db.repositories.registry_credential import (
    RegistryCredentialRepository,
    RevealedSecrets,
    SecretUnreadableError,
    StoredRegistryCredential,
)
from app.logging import get_logger
from app.registry_client.addresses import server_address
from app.registry_client.auth_config import encode_registry_auth, split_basic_token
from app.registry_client.ecr import DerivationRequest, DerivedSecret, DerivedSecretSource
from app.registry_client.errors import TokenExchangeFailed
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.services.pull_credentials import PullCredentials

logger = get_logger(__name__)

REFRESH_MARGIN: Final = timedelta(hours=1)
MAX_LIFETIME: Final = timedelta(hours=12)
FAILURE_BACKOFF: Final = timedelta(seconds=60)
REFRESH_ATTEMPTS: Final = 2

type UnavailableReason = Literal[
    "missing",
    "not_granted_environment",
    "not_granted_user",
    "secret_unreadable",
    "token_exchange_failed",
    "credential_mismatch",
]

_NOT_AVAILABLE = "That registry credential set is not available here."
_MESSAGES: Final[Mapping[UnavailableReason, str]] = {
    "missing": _NOT_AVAILABLE,
    "not_granted_environment": _NOT_AVAILABLE,
    "not_granted_user": _NOT_AVAILABLE,
    "secret_unreadable": (
        "The stored secret of that registry credential set cannot be read. "
        "An administrator needs to enter it again."
    ),
    "token_exchange_failed": "A registry token could not be obtained for that credential set.",
    "credential_mismatch": "That credential set is for a different registry.",
}
_UNUSABLE_TOKEN = "The credential exchange returned a token that cannot be used."
_NO_DERIVER = "No credential exchange is available for this provider."


class CredentialSetUnavailable(ProductError):
    """Raised when a credential set cannot be used for a pull; every message is product-written.

    `reason` says why. A set that does not exist, one not granted to the environment and one the
    user holds no grant on read alike, so a caller learns nothing about sets they cannot use.
    """

    def __init__(self, reason: UnavailableReason, message: str | None = None) -> None:
        super().__init__(message or _MESSAGES[reason])
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _Failure:
    message: str
    kind: str


class DerivedSecretCache:
    """Keeps derived secrets fresh at use: one exchange at a time per set, none while backing off.

    Process-wide. It runs no timer, and a refresh is written on a database session of its own,
    because the caller is inside an audit scope that allows a single commit.
    """

    def __init__(
        self,
        *,
        database: Database,
        vault: Vault,
        derivers: Mapping[str, DerivedSecretSource],
        clock: Clock = utc_now,
        margin: timedelta = REFRESH_MARGIN,
        max_lifetime: timedelta = MAX_LIFETIME,
        backoff: timedelta = FAILURE_BACKOFF,
    ) -> None:
        self._database = database
        self._vault = vault
        self._derivers = derivers
        self._clock = clock
        self._margin = margin
        self._max_lifetime = max_lifetime
        self._backoff = backoff
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
        self._failures: dict[int, tuple[datetime, _Failure]] = {}

    async def basic_token(
        self,
        stored: StoredRegistryCredential,
        secrets: RevealedSecrets,
        *,
        min_remaining: timedelta | None = None,
    ) -> SecretStr:
        """Return a Basic token for the set that is valid beyond the refresh margin.

        A `min_remaining` longer than the margin replaces it, so a cached token with less life
        left is derived anew; it never asks for more than the token's lifetime less the margin.
        Raises CredentialSetUnavailable when none can be had. A cached token that has not yet
        expired is used if a refresh fails.
        """
        now = self._clock()
        token = self._usable_beyond_margin(stored, secrets, now, min_remaining)
        if token is not None:
            return token
        failure = self._recent_failure(stored.id, now)
        if failure is not None:
            return self._still_valid_or_raise(stored, secrets, now, failure)
        async with self._lock_for(stored.id):
            return await self._refresh(stored.id, min_remaining)

    def _lock_for(self, credential_id: int) -> asyncio.Lock:
        lock = self._locks.get(credential_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[credential_id] = lock
        return lock

    async def _refresh(self, credential_id: int, min_remaining: timedelta | None) -> SecretStr:
        async with self._database.session() as db:
            credentials = RegistryCredentialRepository(db, self._vault, self._clock)
            for _ in range(REFRESH_ATTEMPTS):
                token = await self._refresh_once(credentials, credential_id, min_remaining)
                if token is not None:
                    return token
        raise CredentialSetUnavailable("token_exchange_failed")

    async def _refresh_once(
        self,
        credentials: RegistryCredentialRepository,
        credential_id: int,
        min_remaining: timedelta | None,
    ) -> SecretStr | None:
        """Exchange and cache once; None when the set changed during the exchange."""
        unreadable = False
        loaded = None
        try:
            loaded = await credentials.fetch_revealed(credential_id)
        except SecretUnreadableError:
            unreadable = True
        if unreadable:
            raise CredentialSetUnavailable("secret_unreadable")
        if loaded is None:
            raise CredentialSetUnavailable("missing")
        stored, secrets = loaded

        now = self._clock()
        token = self._usable_beyond_margin(stored, secrets, now, min_remaining)
        if token is not None:
            return token
        failure = self._recent_failure(stored.id, now)
        if failure is not None:
            return self._still_valid_or_raise(stored, secrets, now, failure)

        outcome = await self._exchange(stored, secrets)
        now = self._clock()
        if isinstance(outcome, _Failure):
            self._record_failure(stored.id, now, outcome)
            return self._still_valid_or_raise(stored, secrets, now, outcome)

        expires_at = min(outcome.expires_at, now + self._max_lifetime)
        self._failures.pop(stored.id, None)
        details = {"credential_set_id": stored.id, "expires_at": expires_at.isoformat()}
        try:
            cached = await credentials.store_derived(
                stored, outcome.basic_token, expires_at=expires_at, refreshed_at=now
            )
        except sqlite3.OperationalError:
            logger.warning("registry_derived_secret_discarded", extra={**details, "reason": "busy"})
            return outcome.basic_token
        if not cached:
            logger.info("registry_derived_secret_discarded", extra={**details, "reason": "changed"})
            return None
        logger.info("registry_derived_secret_refreshed", extra=details)
        return outcome.basic_token

    async def _exchange(
        self, stored: StoredRegistryCredential, secrets: RevealedSecrets
    ) -> DerivedSecret | _Failure:
        deriver = self._derivers.get(stored.provider)
        if deriver is None or stored.username is None or secrets.secret is None:
            return _Failure(_NO_DERIVER, "unavailable")
        request = DerivationRequest(
            provider=stored.provider,
            address=stored.url,
            username=stored.username,
            secret=secrets.secret,
            extras=stored.extras,
        )
        failure: _Failure | None = None
        derived: DerivedSecret | None = None
        try:
            derived = await deriver.derive(request)
        except TokenExchangeFailed as error:
            failure = _Failure(error.message, error.kind.value)
        except Exception as error:
            logger.error(
                "registry_derived_secret_exchange_crashed",
                extra={"credential_set_id": stored.id, "error": type(error).__name__},
            )
            failure = _Failure(_MESSAGES["token_exchange_failed"], "crashed")
        if derived is None:
            return failure or _Failure(_MESSAGES["token_exchange_failed"], "crashed")
        if not _is_usable(derived, self._clock()):
            return _Failure(_UNUSABLE_TOKEN, "malformed")
        return derived

    def _usable_beyond_margin(
        self,
        stored: StoredRegistryCredential,
        secrets: RevealedSecrets,
        now: datetime,
        min_remaining: timedelta | None,
    ) -> SecretStr | None:
        token = self._cached(secrets)
        if token is None or stored.derived_secret_expires_at is None:
            return None
        expires_at = parse_timestamp(stored.derived_secret_expires_at)
        margin = self._margin
        wanted = min_remaining
        if stored.derived_secret_refreshed_at is not None:
            lifetime = expires_at - parse_timestamp(stored.derived_secret_refreshed_at)
            margin = min(margin, lifetime / 2)
            if wanted is not None:
                wanted = min(wanted, lifetime - self._margin)
        if wanted is not None:
            margin = max(margin, wanted)
        return token if expires_at - now > margin else None

    def _still_valid_or_raise(
        self,
        stored: StoredRegistryCredential,
        secrets: RevealedSecrets,
        now: datetime,
        failure: _Failure,
    ) -> SecretStr:
        token = self._cached(secrets)
        if (
            token is not None
            and stored.derived_secret_expires_at is not None
            and parse_timestamp(stored.derived_secret_expires_at) > now
        ):
            return token
        raise CredentialSetUnavailable("token_exchange_failed", failure.message)

    @staticmethod
    def _cached(secrets: RevealedSecrets) -> SecretStr | None:
        token = secrets.derived_secret
        if token is None:
            return None
        try:
            split_basic_token(token)
        except ValueError:
            return None
        return token

    def _recent_failure(self, credential_id: int, now: datetime) -> _Failure | None:
        recorded = self._failures.get(credential_id)
        if recorded is None:
            return None
        when, failure = recorded
        if now - when < self._backoff:
            return failure
        del self._failures[credential_id]
        return None

    def _record_failure(self, credential_id: int, now: datetime, failure: _Failure) -> None:
        self._failures = {
            key: (when, kept)
            for key, (when, kept) in self._failures.items()
            if now - when < self._backoff
        }
        self._failures[credential_id] = (now, failure)
        logger.warning(
            "registry_derived_secret_refresh_failed",
            extra={"credential_set_id": credential_id, "kind": failure.kind},
        )


def _is_usable(derived: DerivedSecret, now: datetime) -> bool:
    if derived.expires_at.tzinfo is None or derived.expires_at <= now:
        return False
    try:
        split_basic_token(derived.basic_token)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class ResolvedLogin:
    """What a registry request needs: the set's connection settings and its login, if any."""

    credential_set_id: int
    target: RegistryTarget
    login: RegistryLogin | None


class RegistryCredentialResolver:
    """The single entry point that turns a credential set into the login for one use."""

    def __init__(
        self,
        *,
        credentials: RegistryCredentialRepository,
        access: RegistryAccessRepository,
        environments: EnvironmentAccessRepository,
        cache: DerivedSecretCache,
    ) -> None:
        self._credentials = credentials
        self._access = access
        self._environments = environments
        self._cache = cache

    async def resolve_pull_credentials(
        self,
        credential_set_id: int,
        *,
        environment_id: int,
        principal: Principal | None,
        registry_address: str | None = None,
        min_remaining: timedelta | None = None,
    ) -> PullCredentials:
        """Return the credentials for pulling with a set on an environment.

        Grants are checked on every call: the set must be granted to the environment, and a
        standard user must also see the environment and hold a user or team grant on the set.
        Administrators need only the environment grant, and None for principal is the automation
        path, which does too. With `registry_address`, the set must be at that address, read from
        the same row as the secret. Raises CredentialSetUnavailable, and RuntimeError when the
        session is inside a transaction, since a refresh writes on a connection of its own and
        would wait on the caller's lock. An anonymous set returns its id and no header.

        A derived token is re-derived when less than `min_remaining` of its life is left; None
        keeps the refresh margin.
        """
        stored, secrets = await self._authorised(
            credential_set_id, environment_id, principal, registry_address
        )
        if not stored.authentication_enabled:
            return PullCredentials(credential_set_id=stored.id)
        username, password = await self._basic(stored, secrets, min_remaining)
        return PullCredentials(
            credential_set_id=stored.id,
            registry_auth=encode_registry_auth(
                username=username,
                password=password,
                server_address=server_address(stored.provider, stored.url),
            ),
        )

    async def resolve_login(
        self,
        credential_set_id: int,
        *,
        environment_id: int,
        principal: Principal | None,
        registry_address: str | None = None,
        min_remaining: timedelta | None = None,
    ) -> ResolvedLogin:
        """Return the connection settings and login of a set, under the checks of a pull.

        Shares the grants, the address check, the derived-token path, `min_remaining` and the
        refusal inside a transaction with `resolve_pull_credentials`. A set with authentication
        off has no login.
        """
        stored, secrets = await self._authorised(
            credential_set_id, environment_id, principal, registry_address
        )
        target = RegistryTarget(
            provider=stored.provider,
            address=stored.url,
            verify_tls=stored.verify_tls,
            ca_bundle=stored.ca_bundle,
            extras=stored.extras,
        )
        if not stored.authentication_enabled:
            return ResolvedLogin(stored.id, target, None)
        username, password = await self._basic(stored, secrets, min_remaining)
        return ResolvedLogin(stored.id, target, RegistryLogin(username, password))

    async def check_pull_set(
        self,
        credential_set_id: int,
        *,
        environment_id: int,
        principal: Principal | None,
        registry_address: str | None = None,
    ) -> None:
        """Raise CredentialSetUnavailable unless a pull with the set would pass the same checks.

        Runs the environment grant, user grant and registry address checks of
        `resolve_pull_credentials` without reading or decrypting the secret.
        """
        stored = await self._granted(credential_set_id, environment_id, principal)
        if registry_address is not None and stored.url != registry_address:
            raise CredentialSetUnavailable("credential_mismatch")

    async def _granted(
        self, credential_set_id: int, environment_id: int, principal: Principal | None
    ) -> StoredRegistryCredential:
        if self._credentials.in_transaction:
            raise RuntimeError("credentials are resolved outside a write transaction")
        if (
            principal is not None
            and not principal.is_administrator
            and await self._environments.gate(principal.user_id, environment_id) is None
        ):
            raise CredentialSetUnavailable("not_granted_environment")
        stored = await self._credentials.get(credential_set_id)
        if stored is None:
            raise CredentialSetUnavailable("missing")
        if not await self._access.is_granted_to_environment(credential_set_id, environment_id):
            raise CredentialSetUnavailable("not_granted_environment")
        if (
            principal is not None
            and not principal.is_administrator
            and not await self._access.user_may_use(credential_set_id, principal.user_id)
        ):
            raise CredentialSetUnavailable("not_granted_user")
        return stored

    async def _authorised(
        self,
        credential_set_id: int,
        environment_id: int,
        principal: Principal | None,
        registry_address: str | None,
    ) -> tuple[StoredRegistryCredential, RevealedSecrets]:
        await self._granted(credential_set_id, environment_id, principal)
        unreadable = False
        loaded = None
        try:
            loaded = await self._credentials.fetch_revealed(credential_set_id)
        except SecretUnreadableError:
            unreadable = True
        if unreadable:
            raise CredentialSetUnavailable("secret_unreadable")
        if loaded is None:
            raise CredentialSetUnavailable("missing")
        stored, secrets = loaded
        if registry_address is not None and stored.url != registry_address:
            raise CredentialSetUnavailable("credential_mismatch")
        return stored, secrets

    async def _basic(
        self,
        stored: StoredRegistryCredential,
        secrets: RevealedSecrets,
        min_remaining: timedelta | None,
    ) -> tuple[str, SecretStr]:
        username = stored.username
        password = secrets.secret
        if stored.secret_kind == "derived":
            token = await self._cache.basic_token(stored, secrets, min_remaining=min_remaining)
            username, password = split_basic_token(token)
        if username is None or password is None:
            raise CredentialSetUnavailable("secret_unreadable")
        return username, password
