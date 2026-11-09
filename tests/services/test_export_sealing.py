"""Sealing: values opened with the passphrase and refused with anything else."""

import base64

import pytest

from app.auth.errors import ValidationFailed
from app.services.export_sealing import (
    DEFAULT_COST,
    MAX_ITERATIONS,
    MAX_MEMORY_KIB,
    MINIMUM_COST,
    PASSPHRASE_MAX_CHARS,
    PASSPHRASE_MIN_CHARS,
    KdfParameters,
    SealCost,
    SealedRecord,
    SealedValues,
    SealError,
    new_archive_id,
    open_sealed,
    seal,
    verify_sealed,
)

PASSPHRASE = "correct horse battery"
OTHER_ARCHIVE = "f" * 32
VALUES = {
    "variable:DB_PASSWORD": b"s3cret-\x00-value",
    "file:key.pem": b"-----BEGIN KEY-----\n" + bytes(range(256)),
    "environment:0": b"",
}


def sealed_set(archive_id: str) -> SealedValues:
    return seal(VALUES, PASSPHRASE, archive_id, cost=MINIMUM_COST)


def seal_with(passphrase: str) -> SealedValues:
    return seal({"a": b"1"}, passphrase, new_archive_id(), cost=MINIMUM_COST)


def flipped(text: str) -> str:
    raw = bytearray(base64.b64decode(text))
    raw[0] ^= 1
    return base64.b64encode(bytes(raw)).decode("ascii")


def replaced(sealed: SealedValues, index: int, **changes: str) -> SealedValues:
    records = list(sealed.records)
    records[index] = records[index].model_copy(update=changes)
    return sealed.model_copy(update={"records": records})


def test_every_value_opens_with_the_passphrase_and_the_archive_id() -> None:
    archive_id = new_archive_id()

    opened = open_sealed(sealed_set(archive_id), PASSPHRASE, archive_id)

    assert opened == VALUES


def test_a_subset_of_names_opens_only_those() -> None:
    archive_id = new_archive_id()

    opened = open_sealed(sealed_set(archive_id), PASSPHRASE, archive_id, ["file:key.pem"])

    assert opened == {"file:key.pem": VALUES["file:key.pem"]}


@pytest.mark.parametrize(
    "passphrase",
    ["correct horse batterY", "correct horse battery ", "", "x" * (PASSPHRASE_MAX_CHARS + 1)],
)
def test_any_other_passphrase_is_refused(passphrase: str) -> None:
    archive_id = new_archive_id()

    with pytest.raises(SealError):
        open_sealed(sealed_set(archive_id), passphrase, archive_id)


def test_the_same_passphrase_typed_in_another_unicode_form_opens() -> None:
    archive_id = new_archive_id()
    composed = "passéphrase-long"
    sealed = seal({"a": b"1"}, composed, archive_id, cost=MINIMUM_COST)

    assert open_sealed(sealed, "passéphrase-long", archive_id) == {"a": b"1"}


@pytest.mark.parametrize("field", ["ciphertext", "nonce"])
def test_an_altered_record_is_refused(field: str) -> None:
    archive_id = new_archive_id()
    sealed = sealed_set(archive_id)
    altered = replaced(sealed, 0, **{field: flipped(getattr(sealed.records[0], field))})

    with pytest.raises(SealError):
        open_sealed(altered, PASSPHRASE, archive_id)


def test_an_altered_salt_is_refused() -> None:
    archive_id = new_archive_id()
    sealed = sealed_set(archive_id)
    salt = flipped(sealed.kdf.salt)
    altered = sealed.model_copy(update={"kdf": sealed.kdf.model_copy(update={"salt": salt})})

    with pytest.raises(SealError):
        open_sealed(altered, PASSPHRASE, archive_id)


def test_a_value_moved_to_another_name_is_refused() -> None:
    archive_id = new_archive_id()
    sealed = sealed_set(archive_id)
    moved = replaced(sealed, 0, name="variable:OTHER")

    with pytest.raises(SealError):
        open_sealed(moved, PASSPHRASE, archive_id)


def test_two_values_with_their_names_swapped_are_refused() -> None:
    archive_id = new_archive_id()
    sealed = sealed_set(archive_id)
    first, second = sealed.records[0], sealed.records[1]
    swapped = sealed.model_copy(
        update={
            "records": [
                first.model_copy(update={"name": second.name}),
                second.model_copy(update={"name": first.name}),
                *sealed.records[2:],
            ]
        }
    )

    with pytest.raises(SealError):
        open_sealed(swapped, PASSPHRASE, archive_id)


def test_a_set_sealed_for_one_archive_does_not_open_under_another_archive_id() -> None:
    sealed = sealed_set(new_archive_id())

    with pytest.raises(SealError):
        open_sealed(sealed, PASSPHRASE, OTHER_ARCHIVE)


def test_asking_for_a_name_that_is_not_sealed_is_refused() -> None:
    archive_id = new_archive_id()

    with pytest.raises(SealError):
        open_sealed(sealed_set(archive_id), PASSPHRASE, archive_id, ["variable:MISSING"])


@pytest.mark.parametrize("failure", ["passphrase", "tamper", "moved", "archive"])
def test_the_error_is_one_message_that_names_no_value_passphrase_or_name(failure: str) -> None:
    archive_id = new_archive_id()
    sealed = sealed_set(archive_id)
    passphrase = "another passphrase!"
    if failure == "tamper":
        sealed = replaced(sealed, 0, ciphertext=flipped(sealed.records[0].ciphertext))
        passphrase = PASSPHRASE
    if failure == "moved":
        sealed = replaced(sealed, 0, name="variable:OTHER")
        passphrase = PASSPHRASE
    if failure == "archive":
        archive_id, passphrase = OTHER_ARCHIVE, PASSPHRASE

    with pytest.raises(SealError) as raised:
        open_sealed(sealed, passphrase, archive_id)

    shown = f"{raised.value} {raised.value!r}"
    assert str(raised.value) == "The sealed values could not be opened."
    assert raised.value.__cause__ is None
    assert not any(token in shown for token in ("DB_PASSWORD", "key.pem", passphrase, PASSPHRASE))


@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        (PASSPHRASE_MIN_CHARS - 1, False),
        (PASSPHRASE_MIN_CHARS, True),
        (PASSPHRASE_MAX_CHARS, True),
        (PASSPHRASE_MAX_CHARS + 1, False),
    ],
)
def test_the_passphrase_length_bounds_are_enforced_when_sealing(
    length: int, accepted: bool
) -> None:
    if accepted:
        assert seal_with("p" * length).records[0].name == "a"
    else:
        with pytest.raises(ValidationFailed) as raised:
            seal_with("p" * length)
        assert set(raised.value.fields) == {"passphrase"}


def test_each_seal_uses_a_fresh_salt_and_each_value_a_fresh_nonce() -> None:
    archive_id = new_archive_id()

    first, second = sealed_set(archive_id), sealed_set(archive_id)

    nonces = {record.nonce for record in (*first.records, *second.records)}
    assert first.kdf.salt != second.kdf.salt
    assert len(nonces) == 2 * len(VALUES)


def test_the_default_cost_is_recorded_and_the_set_round_trips_through_json() -> None:
    archive_id = new_archive_id()
    sealed = seal({"a": b"value"}, PASSPHRASE, archive_id)

    parsed = SealedValues.model_validate_json(sealed.model_dump_json())

    assert (parsed.kdf.algorithm, parsed.kdf.version, parsed.cipher) == (
        "argon2id",
        19,
        "aes-256-gcm",
    )
    assert (parsed.kdf.memory_kib, parsed.kdf.iterations, parsed.kdf.lanes) == (
        DEFAULT_COST.memory_kib,
        DEFAULT_COST.iterations,
        DEFAULT_COST.lanes,
    )
    assert open_sealed(parsed, PASSPHRASE, archive_id) == {"a": b"value"}


@pytest.mark.parametrize(
    "changes",
    [
        {"memory_kib": MAX_MEMORY_KIB + 1},
        {"memory_kib": 1_048_576},
        {"memory_kib": 1024},
        {"iterations": MAX_ITERATIONS + 1},
        {"iterations": 16},
        {"lanes": 17},
        {"algorithm": "argon2i"},
        {"salt": "AAAA"},
    ],
)
def test_recorded_parameters_outside_what_the_reader_accepts_are_invalid(
    changes: dict[str, object],
) -> None:
    good = seal({"a": b"1"}, PASSPHRASE, new_archive_id(), cost=MINIMUM_COST).kdf.model_dump()

    with pytest.raises(ValueError):
        KdfParameters.model_validate({**good, **changes})


def test_duplicate_record_names_are_invalid() -> None:
    sealed = sealed_set(new_archive_id())
    record: SealedRecord = sealed.records[0]

    with pytest.raises(ValueError):
        SealedValues.model_validate({**sealed.model_dump(), "records": [record, record]})


def test_recorded_parameters_at_the_caps_are_accepted() -> None:
    good = seal({"a": b"1"}, PASSPHRASE, new_archive_id(), cost=MINIMUM_COST).kdf.model_dump()

    parsed = KdfParameters.model_validate(
        {**good, "memory_kib": MAX_MEMORY_KIB, "iterations": MAX_ITERATIONS}
    )

    assert (parsed.memory_kib, parsed.iterations) == (262_144, 8)


@pytest.mark.parametrize(
    "cost",
    [
        SealCost(MAX_MEMORY_KIB + 1, 1, 1),
        SealCost(MINIMUM_COST.memory_kib, MAX_ITERATIONS + 1, 1),
    ],
)
def test_sealing_at_a_cost_the_reader_would_refuse_is_refused(cost: SealCost) -> None:
    with pytest.raises(ValueError):
        seal({"a": b"1"}, PASSPHRASE, new_archive_id(), cost=cost)


def test_the_default_export_cost_is_64_mib_and_three_iterations() -> None:
    assert (DEFAULT_COST.memory_kib, DEFAULT_COST.iterations) == (65_536, 3)


AUTHENTICATED = b"the whole manifest except the mac"


def authenticated_set(archive_id: str) -> SealedValues:
    return seal(
        VALUES,
        PASSPHRASE,
        archive_id,
        cost=MINIMUM_COST,
        authenticate=lambda _: AUTHENTICATED,
    )


def test_authenticated_values_open_when_the_bytes_match_the_mac() -> None:
    archive_id = new_archive_id()
    sealed = authenticated_set(archive_id)

    opened = open_sealed(sealed, PASSPHRASE, archive_id, authenticated=AUTHENTICATED)

    assert sealed.mac is not None
    assert opened == VALUES
    verify_sealed(sealed, PASSPHRASE, archive_id, AUTHENTICATED)


def test_the_callback_receives_the_section_without_a_mac() -> None:
    seen: list[SealedValues] = []

    def authenticate(sealed: SealedValues) -> bytes:
        seen.append(sealed)
        return AUTHENTICATED

    seal(VALUES, PASSPHRASE, new_archive_id(), cost=MINIMUM_COST, authenticate=authenticate)

    assert len(seen) == 1 and seen[0].mac is None and len(seen[0].records) == len(VALUES)


@pytest.mark.parametrize("failure", ["bytes", "passphrase", "no_mac", "moved_mac", "no_names"])
def test_authenticated_values_are_refused_with_the_one_error(failure: str) -> None:
    archive_id = new_archive_id()
    sealed = authenticated_set(archive_id)
    authenticated, passphrase = AUTHENTICATED, PASSPHRASE
    names: list[str] | None = None
    if failure == "bytes":
        authenticated = AUTHENTICATED + b" "
    if failure == "passphrase":
        passphrase = "another passphrase!"
    if failure == "no_mac":
        sealed = sealed.model_copy(update={"mac": None})
    if failure == "moved_mac":
        sealed = sealed.model_copy(update={"mac": authenticated_set(archive_id).mac})
    if failure == "no_names":
        authenticated, names = AUTHENTICATED + b" ", []

    with pytest.raises(SealError) as raised:
        open_sealed(sealed, passphrase, archive_id, names, authenticated=authenticated)

    assert str(raised.value) == "The sealed values could not be opened."
    assert raised.value.__cause__ is None


def test_a_mac_over_nothing_is_still_checked_when_no_value_is_asked_for() -> None:
    archive_id = new_archive_id()
    sealed = authenticated_set(archive_id)

    with pytest.raises(SealError):
        verify_sealed(sealed, PASSPHRASE, archive_id, b"different bytes")
