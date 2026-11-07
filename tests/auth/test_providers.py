"""Tests for app/auth/providers.py."""

from dataclasses import replace

import pytest

from app.auth import passwords
from app.auth.passwords import PasswordHasher
from app.auth.principal import PasswordCredentials, Principal
from app.auth.providers import (
    INTERNAL_PROVIDER,
    AuthenticationFailed,
    InternalAuthProvider,
    ProviderContext,
    ProviderRegistry,
    UnknownProviderError,
    default_registry,
    internal_provider_factory,
)
from app.db.engine import Session
from app.db.repositories.user import UserRepository
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    STANDARD_ID,
    STANDARD_PASSWORD,
    STANDARD_USERNAME,
    FakeClock,
)
from tests.support import CHEAP_ARGON2

UNKNOWN_USERNAME = "nobody-here"
WRONG_PASSWORD = "not-the-password"


@pytest.fixture
def users(db: Session, clock: FakeClock) -> UserRepository:
    return UserRepository(db, clock)


@pytest.fixture
def provider(users: UserRepository, hasher: PasswordHasher) -> InternalAuthProvider:
    return InternalAuthProvider(users, hasher)


@pytest.fixture
def verifications(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the stored hash handed to every verification that reaches the key derivation."""
    seen: list[str] = []
    real = passwords._verify_sync

    def spy(encoded: str, password: str) -> bool:
        seen.append(encoded)
        return real(encoded, password)

    monkeypatch.setattr(passwords, "_verify_sync", spy)
    return seen


async def test_authenticate_returns_the_principal_for_correct_credentials(
    provider: InternalAuthProvider,
) -> None:
    principal = await provider.authenticate(PasswordCredentials(ADMIN_USERNAME, ADMIN_PASSWORD))

    assert principal == Principal(ADMIN_ID, ADMIN_USERNAME, "administrator")


async def test_authenticate_matches_the_username_without_regard_to_case(
    provider: InternalAuthProvider,
) -> None:
    principal = await provider.authenticate(
        PasswordCredentials(ADMIN_USERNAME.upper(), ADMIN_PASSWORD)
    )

    assert principal.user_id == ADMIN_ID


async def test_authenticate_rejects_a_wrong_password_naming_the_user_for_the_audit_trail(
    provider: InternalAuthProvider,
) -> None:
    with pytest.raises(AuthenticationFailed) as raised:
        await provider.authenticate(PasswordCredentials(ADMIN_USERNAME, WRONG_PASSWORD))

    assert (raised.value.reason, raised.value.user_id) == ("bad_password", ADMIN_ID)


async def test_authenticate_rejects_an_unknown_username_without_naming_a_user(
    provider: InternalAuthProvider,
) -> None:
    with pytest.raises(AuthenticationFailed) as raised:
        await provider.authenticate(PasswordCredentials(UNKNOWN_USERNAME, WRONG_PASSWORD))

    assert (raised.value.reason, raised.value.user_id) == ("unknown_user", None)


async def test_authenticate_rejects_a_disabled_account_even_with_the_correct_password(
    provider: InternalAuthProvider, users: UserRepository
) -> None:
    await users.disable(STANDARD_ID)

    with pytest.raises(AuthenticationFailed) as raised:
        await provider.authenticate(PasswordCredentials(STANDARD_USERNAME, STANDARD_PASSWORD))

    assert (raised.value.reason, raised.value.user_id) == ("account_disabled", STANDARD_ID)


async def test_a_wrong_password_for_a_disabled_account_is_reported_as_a_wrong_password(
    provider: InternalAuthProvider, users: UserRepository
) -> None:
    await users.disable(STANDARD_ID)

    with pytest.raises(AuthenticationFailed) as raised:
        await provider.authenticate(PasswordCredentials(STANDARD_USERNAME, WRONG_PASSWORD))

    assert raised.value.reason == "bad_password"


async def test_authenticate_rehashes_a_correct_password_stored_under_weaker_parameters(
    users: UserRepository,
) -> None:
    stronger = PasswordHasher(replace(CHEAP_ARGON2, iterations=CHEAP_ARGON2.iterations + 1))
    before = await users.get(ADMIN_ID)
    assert before is not None
    assert stronger.needs_rehash(before.password_hash)

    await InternalAuthProvider(users, stronger).authenticate(
        PasswordCredentials(ADMIN_USERNAME, ADMIN_PASSWORD)
    )

    after = await users.get(ADMIN_ID)
    assert after is not None
    assert after.password_hash != before.password_hash
    assert stronger.needs_rehash(after.password_hash) is False
    assert await stronger.verify(after.password_hash, ADMIN_PASSWORD) is True


async def test_authenticate_leaves_the_stored_hash_alone_after_a_wrong_password(
    users: UserRepository,
) -> None:
    stronger = PasswordHasher(replace(CHEAP_ARGON2, iterations=CHEAP_ARGON2.iterations + 1))
    before = await users.get(ADMIN_ID)
    assert before is not None

    with pytest.raises(AuthenticationFailed):
        await InternalAuthProvider(users, stronger).authenticate(
            PasswordCredentials(ADMIN_USERNAME, WRONG_PASSWORD)
        )

    after = await users.get(ADMIN_ID)
    assert after is not None
    assert after.password_hash == before.password_hash


async def test_authenticate_accepts_and_does_not_downgrade_a_hash_stronger_than_the_lowered_cost(
    users: UserRepository,
) -> None:
    lowered = PasswordHasher(replace(CHEAP_ARGON2, memory_kib=CHEAP_ARGON2.memory_kib // 2))
    before = await users.get(ADMIN_ID)
    assert before is not None

    principal = await InternalAuthProvider(users, lowered).authenticate(
        PasswordCredentials(ADMIN_USERNAME, ADMIN_PASSWORD)
    )

    after = await users.get(ADMIN_ID)
    assert after is not None
    assert principal.user_id == ADMIN_ID
    assert after.password_hash == before.password_hash


async def test_authenticate_leaves_a_current_hash_alone_after_a_correct_password(
    provider: InternalAuthProvider, users: UserRepository
) -> None:
    before = await users.get(ADMIN_ID)
    assert before is not None

    await provider.authenticate(PasswordCredentials(ADMIN_USERNAME, ADMIN_PASSWORD))

    after = await users.get(ADMIN_ID)
    assert after is not None
    assert after.password_hash == before.password_hash


async def test_an_unknown_username_costs_one_full_verification_like_a_known_one(
    provider: InternalAuthProvider, hasher: PasswordHasher, verifications: list[str]
) -> None:
    with pytest.raises(AuthenticationFailed):
        await provider.authenticate(PasswordCredentials(ADMIN_USERNAME, WRONG_PASSWORD))
    known = list(verifications)
    verifications.clear()

    with pytest.raises(AuthenticationFailed):
        await provider.authenticate(PasswordCredentials(UNKNOWN_USERNAME, WRONG_PASSWORD))

    assert len(known) == len(verifications) == 1
    assert hasher.needs_rehash(verifications[0]) is False


async def test_a_disabled_account_costs_one_full_verification_like_an_enabled_one(
    provider: InternalAuthProvider, users: UserRepository, verifications: list[str]
) -> None:
    await users.disable(STANDARD_ID)

    with pytest.raises(AuthenticationFailed):
        await provider.authenticate(PasswordCredentials(STANDARD_USERNAME, STANDARD_PASSWORD))

    assert len(verifications) == 1


def test_default_registry_offers_only_the_internal_method() -> None:
    assert default_registry().names() == [INTERNAL_PROVIDER]


def test_registering_a_name_twice_is_refused() -> None:
    registry = default_registry()

    with pytest.raises(ValueError, match="already registered"):
        registry.register(INTERNAL_PROVIDER, internal_provider_factory)


def test_creating_a_provider_that_was_never_registered_raises_unknown_provider(
    users: UserRepository, hasher: PasswordHasher
) -> None:
    with pytest.raises(UnknownProviderError):
        ProviderRegistry().create("ldap", ProviderContext(users, hasher))
