"""Tests for app/auth/passwords.py."""

import asyncio
import threading
import unicodedata
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from typing import Any

import pytest

from app.auth import passwords
from app.auth.passwords import MAX_PASSWORD_LENGTH, HashingBusy, HashParameters, PasswordHasher
from tests.support import CHEAP_ARGON2

PASSWORD = "correct horse battery"
OTHER_PASSWORD = "incorrect horse battery"
ARGON2ID_PREFIX = "$argon2id$v=19$"
SALT = "c2FsdHNhbHRzYWx0c2FsdA"
DIGEST = "ZGlnZXN0ZGlnZXN0ZGlnZXN0ZGlnZXN0ZGlnZXN0MDA"

ABOVE_THE_STORED_CEILING_KIB = 4 * 1024 * 1024
LOWERED_FROM_KIB = CHEAP_ARGON2.memory_kib * 8
MALFORMED_HASHES = [
    pytest.param("", id="empty"),
    pytest.param("plain-text", id="not-phc"),
    pytest.param(f"$argon2i$v=19$m=8192,t=1,p=1${SALT}${DIGEST}", id="wrong-variant"),
    pytest.param(f"$argon2id$v=19$m=0,t=1,p=1${SALT}${DIGEST}", id="zero-memory"),
    pytest.param(f"$argon2id$v=19$m=999999999,t=1,p=1${SALT}${DIGEST}", id="unbounded-memory"),
    pytest.param(
        f"$argon2id$v=19$m={ABOVE_THE_STORED_CEILING_KIB},t=1,p=1${SALT}${DIGEST}",
        id="four-gib-memory",
    ),
    pytest.param(f"$argon2id$v=19$m=8192,t=9999,p=1${SALT}${DIGEST}", id="unbounded-iterations"),
    pytest.param(f"$argon2id$v=19$m=8192,t=1,p=999${SALT}${DIGEST}", id="unbounded-lanes"),
]
STRONGER_THAN_CONFIGURED = [
    pytest.param(replace(CHEAP_ARGON2, iterations=CHEAP_ARGON2.iterations + 1), id="iterations"),
    pytest.param(replace(CHEAP_ARGON2, lanes=CHEAP_ARGON2.lanes + 1), id="lanes"),
    pytest.param(replace(CHEAP_ARGON2, memory_kib=CHEAP_ARGON2.memory_kib * 2), id="memory"),
]
STORED_WARNING = "stored_hash_stronger_than_configured"
CONCURRENCY = 1
PENDING_PER_SLOT = 4
BLOCK_TIMEOUT_SECONDS = 10
STORED = "any-stored-hash"


async def test_hash_produces_distinct_argon2id_strings_for_one_password(
    hasher: PasswordHasher,
) -> None:
    first = await hasher.hash(PASSWORD)
    second = await hasher.hash(PASSWORD)

    assert first.startswith(ARGON2ID_PREFIX)
    assert first != second
    assert PASSWORD not in first


async def test_verify_accepts_the_password_that_was_hashed(hasher: PasswordHasher) -> None:
    stored = await hasher.hash(PASSWORD)

    assert await hasher.verify(stored, PASSWORD) is True


async def test_verify_rejects_a_different_password(hasher: PasswordHasher) -> None:
    stored = await hasher.hash(PASSWORD)

    assert await hasher.verify(stored, OTHER_PASSWORD) is False


async def test_verify_treats_composed_and_decomposed_forms_of_one_password_as_equal(
    hasher: PasswordHasher,
) -> None:
    composed = unicodedata.normalize("NFC", "pässword-é-12345")
    decomposed = unicodedata.normalize("NFD", composed)
    stored = await hasher.hash(composed)

    assert composed != decomposed
    assert await hasher.verify(stored, decomposed) is True


async def test_hash_refuses_a_password_longer_than_the_limit(hasher: PasswordHasher) -> None:
    with pytest.raises(ValueError, match="too long"):
        await hasher.hash("x" * (MAX_PASSWORD_LENGTH + 1))


async def test_verify_rejects_a_password_longer_than_the_limit_without_hashing_it(
    hasher: PasswordHasher,
) -> None:
    stored = await hasher.hash(PASSWORD)

    assert await hasher.verify(stored, "x" * (MAX_PASSWORD_LENGTH + 1)) is False


@pytest.mark.parametrize("stored", MALFORMED_HASHES)
async def test_verify_rejects_a_damaged_stored_hash_and_asks_for_a_rehash(
    hasher: PasswordHasher, stored: str
) -> None:
    assert await hasher.verify(stored, PASSWORD) is False
    assert hasher.needs_rehash(stored) is True


async def test_needs_rehash_is_false_for_a_hash_made_under_the_configured_parameters(
    hasher: PasswordHasher,
) -> None:
    stored = await hasher.hash(PASSWORD)

    assert hasher.needs_rehash(stored) is False


@pytest.mark.parametrize("configured", STRONGER_THAN_CONFIGURED)
async def test_needs_rehash_is_true_for_a_hash_weaker_than_the_configured_parameters(
    hasher: PasswordHasher, configured: HashParameters
) -> None:
    stored = await hasher.hash(PASSWORD)

    stronger = PasswordHasher(configured)

    assert stronger.needs_rehash(stored) is True
    assert await stronger.verify(stored, PASSWORD) is True


@pytest.mark.parametrize("stored_parameters", STRONGER_THAN_CONFIGURED)
async def test_needs_rehash_is_false_and_warns_for_a_hash_stronger_than_the_configured_parameters(
    hasher: PasswordHasher,
    stored_parameters: HashParameters,
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    stored = await PasswordHasher(stored_parameters).hash(PASSWORD)

    assert hasher.needs_rehash(stored) is False
    assert len(logged(STORED_WARNING)) == 1


async def test_needs_rehash_does_not_warn_for_a_hash_under_the_configured_parameters(
    hasher: PasswordHasher, logged: Callable[[str], list[dict[str, Any]]]
) -> None:
    stored = await hasher.hash(PASSWORD)

    hasher.needs_rehash(stored)

    assert logged(STORED_WARNING) == []


async def test_verify_still_accepts_a_hash_stronger_than_the_lowered_configuration(
    hasher: PasswordHasher,
) -> None:
    stored = await PasswordHasher(replace(CHEAP_ARGON2, memory_kib=LOWERED_FROM_KIB)).hash(PASSWORD)

    assert await hasher.verify(stored, PASSWORD) is True


@dataclass
class Saturated:
    """A hasher with a full queue, the event that lets its work finish, and that work."""

    hasher: PasswordHasher
    release: threading.Event
    queued: list["asyncio.Task[bool]"]


@pytest.fixture
async def saturated(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Saturated]:
    release = threading.Event()

    def blocked(encoded: str, password: str) -> bool:
        release.wait(timeout=BLOCK_TIMEOUT_SECONDS)
        return True

    monkeypatch.setattr(passwords, "_verify_sync", blocked)
    hasher = PasswordHasher(CHEAP_ARGON2, max_concurrent=CONCURRENCY)
    queued = [
        asyncio.create_task(hasher.verify(STORED, PASSWORD))
        for _ in range(CONCURRENCY * PENDING_PER_SLOT)
    ]
    await asyncio.sleep(0)
    yield Saturated(hasher, release, queued)
    release.set()
    await asyncio.gather(*queued)


async def test_a_request_past_the_pending_bound_is_refused_rather_than_queued(
    saturated: Saturated,
) -> None:
    with pytest.raises(HashingBusy):
        await saturated.hasher.verify(STORED, PASSWORD)


async def test_a_refused_request_leaves_no_residue_once_the_queued_work_drains(
    saturated: Saturated,
) -> None:
    with pytest.raises(HashingBusy):
        await saturated.hasher.verify(STORED, PASSWORD)
    saturated.release.set()
    await asyncio.gather(*saturated.queued)

    assert await saturated.hasher.verify(STORED, PASSWORD) is True


async def test_verifying_against_nothing_reuses_the_hash_built_by_prepare(
    hasher: PasswordHasher, monkeypatch: pytest.MonkeyPatch
) -> None:
    await hasher.prepare()

    async def refuse(password: str) -> str:
        raise AssertionError("a hash was built after prepare")

    monkeypatch.setattr(hasher, "hash", refuse)

    await hasher.verify_against_nothing(PASSWORD)
