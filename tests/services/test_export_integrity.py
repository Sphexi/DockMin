"""The MAC over a sealed manifest: a modified or transplanted manifest is not opened."""

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

from app.services.export_manifest import (
    ExportFormatError,
    ExportManifest,
    ExportRefusal,
    manifest_bytes,
    open_manifest_secrets,
    parse_manifest,
    verify_manifest,
)
from app.services.export_sealing import MAX_ITERATIONS, MAX_MEMORY_KIB, SealError
from tests.export_archive_support import (
    FIXTURE,
    FIXTURE_PASSPHRASE,
    FIXTURE_SECRET_NAME,
    FIXTURE_SECRET_VALUE,
    PASSPHRASE,
    SECRET_ENVIRONMENT_VALUE,
    SECRET_VALUE,
    container_description,
    members_of,
    prepared,
    stack_description,
    volume_manifest,
)

CONTAINER_FIXTURE = FIXTURE.with_name("container_format_version_1.tar.gz")
SEAL_REFUSED = "The sealed values could not be opened."

type Change = Callable[[dict[str, Any]], object]


def stack_manifest() -> ExportManifest:
    return prepared(stack_description(volumes=(volume_manifest(),))).manifest


def container_manifest() -> ExportManifest:
    return prepared(container_description()).manifest


def document_of(manifest: ExportManifest) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(manifest_bytes(manifest))
    return document


def changed(manifest: ExportManifest, change: Change) -> ExportManifest:
    document = copy.deepcopy(document_of(manifest))
    change(document)
    return parse_manifest(json.dumps(document).encode("utf-8"))


def plain_variable(document: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in document["stack"]["variables"] if not item["secret"])


STACK_CHANGES: dict[str, Change] = {
    "stack file digest": lambda d: d["stack"]["compose_file"].update(sha256="0" * 64),
    "stack file size": lambda d: d["stack"]["compose_file"].update(size=1),
    "companion file digest": lambda d: d["stack"]["files"][0].update(sha256="0" * 64),
    "context digest": lambda d: d["stack"]["contexts"][0].update(sha256="0" * 64),
    "plain variable": lambda d: plain_variable(d).update(value="changed"),
    "image reference": lambda d: d["images"][0].update(reference="evil.example/nginx:1.27"),
    "image digest": lambda d: d["images"][0].update(digest="sha256:" + "cd" * 32),
    "nested volume manifest": lambda d: d["volumes"][0]["manifest"]["labels"].update(team="other"),
    "nested volume name": lambda d: d["volumes"][0]["manifest"].update(name="other"),
    "sealed ciphertext": lambda d: d["sealed"]["records"][0].update(
        ciphertext=d["sealed"]["records"][1]["ciphertext"]
    ),
    "kdf salt": lambda d: d["sealed"]["kdf"].update(salt="A" * 22 + "=="),
    "name": lambda d: d.update(name="other"),
    "description": lambda d: d.update(description="Another description"),
    "exported at": lambda d: d.update(exported_at="2030-01-01T00:00:00Z"),
    "not exported list": lambda d: d["not_exported"].append("nothing"),
}

CONTAINER_CHANGES: dict[str, Change] = {
    "plain environment value": lambda d: d["container"]["environment"][0].update(value="evil"),
    "form field": lambda d: d["container"]["form"].update(image="evil.example/nginx"),
}


def test_an_unmodified_stack_manifest_verifies_and_opens_after_a_json_round_trip() -> None:
    manifest = parse_manifest(manifest_bytes(stack_manifest()))

    verify_manifest(manifest, PASSPHRASE)
    opened = open_manifest_secrets(manifest, PASSPHRASE)

    assert opened["variable:DB_PASSWORD"] == SECRET_VALUE.encode()
    assert manifest.integrity_protected


def test_an_unmodified_container_manifest_opens_only_the_names_asked_for() -> None:
    manifest = parse_manifest(manifest_bytes(container_manifest()))

    opened = open_manifest_secrets(manifest, PASSPHRASE, ["environment:1"])

    assert opened == {"environment:1": SECRET_ENVIRONMENT_VALUE.encode()}


@pytest.mark.parametrize("label", list(STACK_CHANGES))
def test_a_stack_manifest_modified_after_sealing_is_refused_by_both_checks(label: str) -> None:
    manifest = changed(stack_manifest(), STACK_CHANGES[label])

    with pytest.raises(SealError) as opened:
        open_manifest_secrets(manifest, PASSPHRASE)
    with pytest.raises(SealError):
        verify_manifest(manifest, PASSPHRASE)

    assert str(opened.value) == SEAL_REFUSED
    assert opened.value.__cause__ is None


@pytest.mark.parametrize("label", list(CONTAINER_CHANGES))
def test_a_container_manifest_modified_after_sealing_is_refused(label: str) -> None:
    manifest = changed(container_manifest(), CONTAINER_CHANGES[label])

    with pytest.raises(SealError):
        open_manifest_secrets(manifest, PASSPHRASE)


def test_the_mac_of_another_archive_sealed_under_the_same_passphrase_is_refused() -> None:
    other = document_of(stack_manifest())["sealed"]["mac"]

    manifest = changed(stack_manifest(), lambda d: d["sealed"].update(mac=other))

    with pytest.raises(SealError):
        open_manifest_secrets(manifest, PASSPHRASE)


def test_the_sealed_section_of_another_archive_is_refused() -> None:
    other = document_of(stack_manifest())

    manifest = changed(stack_manifest(), lambda d: d.update(sealed=other["sealed"]))

    with pytest.raises(SealError):
        open_manifest_secrets(manifest, PASSPHRASE)


def test_a_wrong_passphrase_is_refused_with_the_same_error() -> None:
    manifest = parse_manifest(manifest_bytes(stack_manifest()))

    with pytest.raises(SealError) as raised:
        open_manifest_secrets(manifest, "another passphrase!")

    assert str(raised.value) == SEAL_REFUSED


def test_a_sealed_manifest_without_a_mac_does_not_parse() -> None:
    document = document_of(stack_manifest())
    del document["sealed"]["mac"]

    with pytest.raises(ExportFormatError) as raised:
        parse_manifest(json.dumps(document).encode("utf-8"))

    assert raised.value.reason is ExportRefusal.MANIFEST_INVALID


def test_a_manifest_with_nothing_sealed_is_refused_by_both_checks() -> None:
    manifest = prepared(stack_description(), passphrase=None).manifest

    with pytest.raises(SealError):
        open_manifest_secrets(manifest, PASSPHRASE)
    with pytest.raises(SealError):
        verify_manifest(manifest, PASSPHRASE)


@pytest.mark.parametrize(
    ("manifest", "protected"),
    [
        (stack_manifest(), True),
        (prepared(stack_description(), passphrase=None).manifest, False),
        (prepared(stack_description(secret=False)).manifest, False),
        (prepared(container_description(), passphrase=None).manifest, False),
    ],
    ids=["sealed", "omitted", "none", "container-omitted"],
)
def test_only_a_sealed_manifest_reports_integrity_protection(
    manifest: ExportManifest, protected: bool
) -> None:
    assert manifest.integrity_protected is protected
    assert (manifest.sealed is not None) is protected


@pytest.mark.parametrize(
    "kdf",
    [
        {"memory_kib": MAX_MEMORY_KIB + 1},
        {"memory_kib": 1_048_576},
        {"iterations": MAX_ITERATIONS + 1},
        {"iterations": 16},
    ],
)
def test_a_manifest_asking_for_a_costlier_derivation_than_the_cap_is_invalid(
    kdf: dict[str, int],
) -> None:
    document = document_of(stack_manifest())
    document["sealed"]["kdf"].update(kdf)

    with pytest.raises(ExportFormatError) as raised:
        parse_manifest(json.dumps(document).encode("utf-8"))

    assert raised.value.reason is ExportRefusal.MANIFEST_INVALID


@pytest.mark.parametrize(
    ("archive", "name", "value"),
    [
        (FIXTURE, FIXTURE_SECRET_NAME, FIXTURE_SECRET_VALUE),
        (CONTAINER_FIXTURE, "environment:1", None),
    ],
    ids=["stack", "container"],
)
def test_the_format_1_fixtures_verify_with_the_test_passphrase(
    archive: Any, name: str, value: bytes | None
) -> None:
    manifest = parse_manifest(members_of(archive.read_bytes())["manifest.json"][1])

    opened = open_manifest_secrets(manifest, FIXTURE_PASSPHRASE)

    assert manifest.integrity_protected and name in opened
    assert value is None or opened[name] == value
