"""The archive manifest: canonical encoding, shape checks, the format version and its signature."""

import json
from typing import Any

import pytest

from app.backup.manifest import (
    ARCHIVE_MEMBER,
    FORMAT_VERSION,
    MAX_COMPOSE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_STACK_MEMBERS,
    ArchiveRefused,
    Manifest,
    MemberEntry,
    canonical_json,
    decode_manifest,
    encode_manifest,
    manifest_mac_matches,
)
from app.crypto.keys import manifest_mac
from tests.crypto.helpers import FIRST_KEY, SECOND_KEY

DIGEST = "ab" * 32
INSTANCE = "a" * 32
STACK_ONE = f"stacks/{'1' * 32}/compose.yaml"
STACK_TWO = f"stacks/{'2' * 32}/compose.yaml"
FIELDS = Manifest(
    format_version=FORMAT_VERSION,
    schema_version=15,
    created_at="2026-03-04T05:06:07Z",
    product_version="1.2.3",
    instance_id=INSTANCE,
    key_version=2,
    key_fingerprint="0123456789abcdef",
    administrators=("alice", "bob"),
    members=(
        MemberEntry("database.sqlite3", 4096, DIGEST),
        MemberEntry(STACK_ONE, 12, DIGEST),
        MemberEntry(STACK_TWO, 20, DIGEST),
    ),
    mac="",
)
ABSENT = object()


def sign(fields: Manifest = FIELDS, key: bytes = SECOND_KEY) -> bytes:
    return encode_manifest(fields, mac=lambda payload: manifest_mac(key, payload))


def document(**changes: Any) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(sign())
    for name, value in changes.items():
        if value is ABSENT:
            del loaded[name]
        else:
            loaded[name] = value
    return loaded


def refusal(data: bytes) -> tuple[str, str]:
    with pytest.raises(ArchiveRefused) as raised:
        decode_manifest(data)
    return raised.value.reason, raised.value.message


def member(name: str, size: int, sha256: str = DIGEST) -> dict[str, Any]:
    return {"name": name, "size": size, "sha256": sha256}


def test_the_canonical_encoding_has_sorted_keys_no_whitespace_and_only_ascii() -> None:
    encoded = sign()

    assert encoded == canonical_json(json.loads(encoded))
    assert b" " not in encoded and b"\n" not in encoded
    assert encoded.isascii()
    assert list(json.loads(encoded)) == sorted(json.loads(encoded))


def test_a_manifest_decodes_to_the_fields_it_was_encoded_from_without_any_key() -> None:
    decoded = decode_manifest(sign())

    assert decoded.members == FIELDS.members
    assert (decoded.administrators, decoded.key_version, decoded.schema_version) == (
        ("alice", "bob"),
        2,
        15,
    )
    assert decoded.mac == json.loads(sign())["mac"]


def test_a_manifest_larger_than_its_limit_is_not_encoded() -> None:
    names = tuple(f"stacks/{index:032x}/compose.yaml" for index in range(MAX_STACK_MEMBERS))
    fields = Manifest(
        1,
        15,
        FIELDS.created_at,
        FIELDS.product_version,
        INSTANCE,
        2,
        FIELDS.key_fingerprint,
        (),
        (MemberEntry("database.sqlite3", 1, DIGEST), *(MemberEntry(n, 1, DIGEST) for n in names)),
        "",
    )

    with pytest.raises(ValueError, match="size limit"):
        sign(fields)


def test_the_signature_holds_under_the_signing_key_only() -> None:
    encoded = sign()

    assert manifest_mac_matches(encoded, SECOND_KEY)
    assert not manifest_mac_matches(encoded, FIRST_KEY)


@pytest.mark.parametrize(
    "tampered",
    [
        sign().replace(b'"alice"', b'"mallo"'),
        sign().replace(b'"key_version":2', b'"key_version":1'),
        sign().replace(b'"size":12', b'"size":13'),
        json.dumps({k: v for k, v in document().items() if k != "mac"}).encode(),
        json.dumps({**document(), "mac": 5}).encode(),
        b"not json",
        b"[]",
    ],
    ids=[
        "administrator",
        "key-version",
        "member-size",
        "no-signature",
        "numeric-signature",
        "text",
        "list",
    ],
)
def test_a_manifest_changed_after_signing_does_not_verify(tampered: bytes) -> None:
    assert not manifest_mac_matches(tampered, SECOND_KEY)


@pytest.mark.parametrize(
    "mutation",
    [
        {"format": "other"},
        {"schema_version": 0},
        {"schema_version": True},
        {"schema_version": 100_001},
        {"created_at": "2026-13-01T00:00:00Z"},
        {"created_at": "2026-03-04 05:06:07"},
        {"product_version": ""},
        {"product_version": "x" * 65},
        {"instance_id": "A" * 32},
        {"instance_id": "a" * 31},
        {"key_version": 0},
        {"key_version": "2"},
        {"key_fingerprint": "xyz"},
        {"administrators": ["bob", "alice"]},
        {"administrators": ["has space"]},
        {"administrators": [f"u{index:03d}" for index in range(101)]},
        {"administrators": "alice"},
        {"mac": "0" * 63},
        {"mac": "G" * 64},
        {"unexpected": 1},
        {"mac": ABSENT},
    ],
    ids=[
        "format",
        "schema-zero",
        "schema-bool",
        "schema-too-high",
        "impossible-date",
        "date-shape",
        "no-product-version",
        "long-product-version",
        "upper-case-instance",
        "short-instance",
        "key-version-zero",
        "key-version-text",
        "fingerprint",
        "administrators-unsorted",
        "administrator-name",
        "too-many-administrators",
        "administrators-not-a-list",
        "short-mac",
        "non-hex-mac",
        "extra-field",
        "missing-mac",
    ],
)
def test_a_field_outside_its_shape_makes_the_manifest_invalid(mutation: dict[str, Any]) -> None:
    reason, _ = refusal(canonical_json(document(**mutation)))

    assert reason == "archive_invalid"


@pytest.mark.parametrize(
    "members",
    [
        [],
        [member(STACK_ONE, 5)],
        [member("manifest.json", 5)],
        [member("database.sqlite3", 5), member(STACK_TWO, 5), member(STACK_ONE, 5)],
        [member("database.sqlite3", 5), member(STACK_ONE, 5), member(STACK_ONE, 5)],
        [member("database.sqlite3", 5), member("stacks/x/compose.yaml", 5)],
        [member("database.sqlite3", 5), member("../evil", 5)],
        [member("database.sqlite3", 5), member(STACK_ONE, 0)],
        [member("database.sqlite3", 5), member(STACK_ONE, MAX_COMPOSE_BYTES + 1)],
        [member("database.sqlite3", 5, "ABC")],
        [member("database.sqlite3", -1)],
        [{"name": "database.sqlite3", "size": 5}],
        ["database.sqlite3"],
    ],
    ids=[
        "none",
        "no-database",
        "lists-the-manifest",
        "unsorted-stacks",
        "repeated-stack",
        "stack-name",
        "traversal",
        "empty-stack",
        "oversize-stack",
        "digest",
        "negative-size",
        "missing-digest",
        "not-an-object",
    ],
)
def test_a_member_inventory_outside_the_allow_list_or_order_is_invalid(
    members: list[Any],
) -> None:
    reason, _ = refusal(canonical_json(document(members=members)))

    assert reason == "archive_invalid"


@pytest.mark.parametrize(
    "data",
    [
        b"{}",
        b"[1]",
        b"\xff\xfe",
        b'{"format":"dockmin-backup","format":"dockmin-backup"}',
        b'{"format":NaN}',
        b" " + sign(),
        sign().replace(b'"alice","bob"', b'"alice", "bob"'),
        b"x" * (MAX_MANIFEST_BYTES + 1),
    ],
    ids=[
        "empty-object",
        "list",
        "not-utf8",
        "duplicate-key",
        "non-finite",
        "leading-space",
        "spacing",
        "oversize",
    ],
)
def test_text_that_is_not_the_one_canonical_json_object_is_invalid(data: bytes) -> None:
    reason, _ = refusal(data)

    assert reason == "archive_invalid"


@pytest.mark.parametrize(
    "mutation",
    [
        {"format_version": 2},
        {"format_version": 2, "unexpected": 1},
        {"format_version": 2, "mac": ABSENT},
    ],
    ids=["newer", "newer-with-other-fields", "newer-with-fewer-fields"],
)
def test_a_newer_format_version_is_reported_as_unsupported_before_the_field_set_is_checked(
    mutation: dict[str, Any],
) -> None:
    reason, message = refusal(canonical_json(document(**mutation)))

    assert reason == "format_unsupported"
    assert f"version {FORMAT_VERSION}" in message


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("manifest.json", True),
        ("database.sqlite3", True),
        (STACK_ONE, True),
        ("stacks/" + "A" * 32 + "/compose.yaml", False),
        ("stacks/" + "a" * 32 + "/compose.yaml/", False),
        ("stacks/../compose.yaml", False),
        ("/etc/passwd", False),
        ("extra.txt", False),
        ("manifest.json\n", False),
    ],
)
def test_only_the_allow_listed_member_names_match(name: str, allowed: bool) -> None:
    assert (ARCHIVE_MEMBER.fullmatch(name) is not None) is allowed
