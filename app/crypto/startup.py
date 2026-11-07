"""Startup verification of the master keys against the stored canary and stored values."""

from collections.abc import Mapping

from app.config import (
    ALLOW_KEY_ROTATION_VAR,
    PREVIOUS_SECRET_KEY_FILE_VAR,
    PREVIOUS_SECRET_KEY_VAR,
    SECRET_KEY_VAR,
)
from app.crypto import envelope
from app.crypto.envelope import DecryptionError, SecretLocation
from app.crypto.keys import Keyring, MasterKeys, key_fingerprint, key_forms
from app.crypto.vault import Vault
from app.db.engine import Database
from app.db.repositories.crypto_state import (
    CryptoState,
    CryptoStateRepository,
    UnparseableSecret,
    canary_location,
)
from app.logging import DEFAULT_SCRUBBER, Scrubber, get_logger

PREVIOUS_KEY_SUPPLY = f"{PREVIOUS_SECRET_KEY_VAR} or {PREVIOUS_SECRET_KEY_FILE_VAR}"
CANARY_PLAINTEXT = "dockmin-canary"
FIRST_KEY_VERSION = 1

logger = get_logger(__name__)


class KeyMismatchError(Exception):
    """The configured keys do not match the database; the message names no value."""


def _opens(
    token: str, key: bytes, version: int, location: SecretLocation, instance_id: str
) -> bool:
    try:
        return envelope.decrypt(token, {version: key}, location, instance_id) == CANARY_PLAINTEXT
    except DecryptionError:
        return False


def _canary_opens(state: CryptoState, key: bytes) -> bool:
    return _opens(
        state.canary,
        key,
        state.key_version,
        canary_location(state.canary_row_id),
        state.instance_id,
    )


def _log_loaded_keys(held: Mapping[int, bytes]) -> None:
    for version in sorted(held):
        logger.info(
            "encryption_key_loaded",
            extra={"key_version": version, "key_fingerprint": key_fingerprint(held[version])},
        )


def _describe_unparseable(items: list[UnparseableSecret]) -> str:
    return ", ".join(f"{item.field.table} row {item.row_id}" for item in items)


async def establish_vault(
    database: Database,
    keys: MasterKeys,
    scrubber: Scrubber = DEFAULT_SCRUBBER,
    *,
    allow_rotation: bool = False,
    key_description: str = f"the configured {SECRET_KEY_VAR}",
    previous_key_description: str = f"the configured {PREVIOUS_SECRET_KEY_VAR}",
) -> Vault:
    """Verify keys against the canary and stored values, creating the canary on first start.

    Raises KeyMismatchError before any credential is read when the configured keys do not open
    the canary, cannot read every key version present in the data, or would rotate the key
    without allow_rotation.
    """
    for key in (keys.current, keys.previous):
        if key is not None:
            for form in key_forms(key):
                scrubber.register_secret(form)
    async with database.session() as session:
        repo = CryptoStateRepository(session)
        state = await repo.get()
        if state is None:
            return await _first_start(repo, keys)
        if not envelope.is_valid_instance_id(state.instance_id):
            raise KeyMismatchError("the stored database instance id is malformed")
        if _canary_opens(state, keys.current):
            return await _ordinary_start(repo, state, keys)
        if keys.previous is not None and _canary_opens(state, keys.previous):
            return await _rotate(
                repo, state, keys, allow_rotation, key_description, previous_key_description
            )
        raise KeyMismatchError(
            f"{key_description} does not match this database, which expects "
            f"key version {state.key_version}; if the key was rotated, supply the old one as "
            f"{PREVIOUS_KEY_SUPPLY}"
        )


async def _first_start(repo: CryptoStateRepository, keys: MasterKeys) -> Vault:
    # Missing state rows beside stored ciphertext is damage, not a first start: accepting the
    # configured key would seal a fresh canary under a key that may not read those values.
    if await repo.has_secret_values():
        raise KeyMismatchError(
            "the crypto state rows are missing from the setting table while encrypted values "
            "exist; restore the database rather than starting with a new key"
        )
    instance_id = envelope.new_instance_id()
    vault = Vault(Keyring(FIRST_KEY_VERSION, {FIRST_KEY_VERSION: keys.current}), instance_id)
    await repo.write(
        FIRST_KEY_VERSION, instance_id, lambda where: vault.seal(CANARY_PLAINTEXT, where)
    )
    _log_loaded_keys({FIRST_KEY_VERSION: keys.current})
    return vault


async def _ordinary_start(
    repo: CryptoStateRepository, state: CryptoState, keys: MasterKeys
) -> Vault:
    held = {state.key_version: keys.current}
    unreadable: dict[int, int] = {}
    for version, count in sorted((await repo.count_by_version()).items()):
        if version == state.key_version:
            continue
        key = await _key_reading(repo, version, keys.previous, state.instance_id)
        if key is None:
            unreadable[version] = count
        else:
            held[version] = key
    if unreadable:
        described = ", ".join(f"{v} ({n} values)" for v, n in unreadable.items())
        raise KeyMismatchError(
            f"stored values exist at key version {described} that no configured key can read; "
            f"supply the key that reads them as {PREVIOUS_KEY_SUPPLY}"
        )
    unparseable = await repo.find_unparseable()
    if unparseable:
        logger.error(
            "unreadable_secret_values",
            extra={"locations": _describe_unparseable(unparseable)},
        )
    _log_loaded_keys(held)
    return Vault(Keyring(state.key_version, held), state.instance_id)


async def _key_reading(
    repo: CryptoStateRepository, version: int, candidate: bytes | None, instance_id: str
) -> bytes | None:
    """Return candidate if it decrypts a stored value written at version, else None."""
    if candidate is None:
        return None
    for sample in await repo.sample_at_version(version):
        location = SecretLocation(sample.field.table, sample.field.column, sample.row_id)
        try:
            envelope.decrypt(sample.token, {version: candidate}, location, instance_id)
        except DecryptionError:
            continue
        return candidate
    return None


async def _rotate(
    repo: CryptoStateRepository,
    state: CryptoState,
    keys: MasterKeys,
    allow_rotation: bool,
    key_description: str,
    previous_key_description: str,
) -> Vault:
    assert keys.previous is not None
    stored = state.key_version
    stranded = sorted(v for v in await repo.count_by_version() if v != stored)
    unparseable = await repo.find_unparseable()
    if unparseable:
        raise KeyMismatchError(
            "cannot rotate to a new key while secret columns hold values that are not valid "
            f"ciphertext, which no key can read: {_describe_unparseable(unparseable)}; "
            "clear or re-enter them first"
        )
    if stranded:
        raise KeyMismatchError(
            "cannot rotate to a new key while values remain at key version "
            f"{', '.join(str(v) for v in stranded)}; restart with the key that reads them as "
            f"{PREVIOUS_KEY_SUPPLY} and finish the re-key"
        )
    if not allow_rotation:
        raise KeyMismatchError(
            f"The previous key, from {previous_key_description}, opens this database at key "
            f"version {stored} but {key_description} does not. Starting would rotate the "
            f"database to key version {stored + 1} and rewrite its canary under "
            f"{key_description}; stored values would stay at version {stored} until a re-key "
            "runs, and restarting with the old key as "
            f"the current key would no longer work. Set {ALLOW_KEY_ROTATION_VAR}=1 to proceed"
        )
    new_version = stored + 1
    held = {new_version: keys.current, stored: keys.previous}
    vault = Vault(Keyring(new_version, held), state.instance_id)
    await repo.write(
        new_version, state.instance_id, lambda where: vault.seal(CANARY_PLAINTEXT, where)
    )
    _log_loaded_keys(held)
    return vault
