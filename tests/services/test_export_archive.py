"""The stack and container export manifest, its writer with nested volumes, and its reader."""

import asyncio
import gzip
import json
import random
import sys
import tarfile
from typing import Any

import pytest

from app.services.export_archive_writer import ExportArchiveStream
from app.services.export_build import ExportDescription
from app.services.export_manifest import (
    FORMAT_VERSION,
    MAX_CONTEXTS_BYTES,
    ExportFormatError,
    ExportManifest,
    ExportRefusal,
    manifest_bytes,
    parse_manifest,
    secret_names,
)
from app.services.volume_export_writer import StreamMalformed
from app.services.volume_manifest import FORMAT_VERSION as VOLUME_FORMAT_VERSION
from app.services.volume_manifest import VolumeManifest
from tests.archive_support import Member
from tests.export_archive_support import (
    COMPOSE,
    FILE_CONTENT,
    FILE_NAME,
    PASSPHRASE,
    PLAIN_VALUE,
    SECRET_ENVIRONMENT_VALUE,
    SECRET_FILE_CONTENT,
    SECRET_VALUE,
    VOLUME_FILE,
    VOLUME_NOTE,
    FakeVolume,
    container_description,
    members_of,
    prepared,
    stack_description,
    volume_manifest,
    write_archive,
)
from tests.volume_export_support import unterminated, volume_tar


def as_document(manifest: ExportManifest) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(manifest_bytes(manifest))
    return document


def parse_document(document: dict[str, Any]) -> ExportManifest:
    return parse_manifest(json.dumps(document).encode("utf-8"))


async def stack_archive(volumes: int = 1) -> bytes:
    sources = [FakeVolume() for _ in range(volumes)]
    description = stack_description(volumes=tuple(volume_manifest(f"v{i}") for i in range(volumes)))
    stream = ExportArchiveStream(prepared(description), [source.source() for source in sources])
    return await write_archive(stream)


def test_a_stack_manifest_round_trips_through_its_json() -> None:
    manifest = prepared(stack_description(volumes=(volume_manifest(),))).manifest

    parsed = parse_manifest(manifest_bytes(manifest))

    assert parsed == manifest
    assert (parsed.format_version, parsed.kind, parsed.secrets) == (1, "compose_stack", "sealed")
    assert [section.prefix for section in parsed.volumes] == ["volumes/0"]
    assert parsed.volumes[0].manifest == volume_manifest()
    assert parsed.images[0].digest is not None and parsed.images[0].tag == "1.27"


def test_secrets_are_sealed_by_name_and_plain_values_stay_inline() -> None:
    manifest = prepared(stack_description()).manifest

    assert manifest.sealed is not None
    assert [record.name for record in manifest.sealed.records] == sorted(secret_names(manifest))
    assert manifest.stack is not None
    assert {item.name: item.value for item in manifest.stack.variables} == {
        "DB_PASSWORD": None,
        "PLAIN": PLAIN_VALUE,
    }
    assert {item.name: item.member for item in manifest.stack.files} == {
        FILE_NAME: f"stack/files/{FILE_NAME}",
        "tls.key": None,
    }


def test_without_a_passphrase_secret_names_are_listed_and_nothing_is_sealed() -> None:
    manifest = prepared(stack_description(), passphrase=None).manifest

    assert (manifest.secrets, manifest.sealed) == ("omitted", None)
    assert secret_names(manifest) == ["variable:DB_PASSWORD", "file:tls.key"]


def test_an_object_with_no_secrets_records_secrets_none_even_with_a_passphrase() -> None:
    manifest = prepared(stack_description(secret=False)).manifest

    assert (manifest.secrets, manifest.sealed) == ("none", None)


def test_a_container_export_seals_marked_environment_values_by_position() -> None:
    manifest = prepared(container_description()).manifest

    assert manifest.sealed is not None
    assert [record.name for record in manifest.sealed.records] == ["environment:1"]
    assert manifest.container is not None
    assert [(e.name, e.secret, e.value) for e in manifest.container.environment] == [
        ("MODE", False, "production"),
        ("TOKEN", True, None),
    ]


def test_environment_rows_in_the_container_form_are_refused() -> None:
    with pytest.raises(ValueError):
        prepared(container_description(form={"image": "nginx", "env-0-key": "A"}))


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["stack"]["variables"][1].update(value="leaked"),
        lambda d: d["stack"]["variables"][0].update(value=None),
        lambda d: d.update(secrets="omitted"),
        lambda d: d.update(secrets="none"),
        lambda d: d.update(sealed=None),
        lambda d: d["sealed"]["records"].pop(),
        lambda d: d["sealed"].pop("mac"),
        lambda d: d["sealed"].update(mac="AAAA"),
        lambda d: d.update(kind="swarm_stack"),
        lambda d: d.update(kind="container"),
        lambda d: d.update(archive_id="xyz"),
        lambda d: d["stack"]["files"][0].update(member="stack/files/other"),
        lambda d: d["stack"]["contexts"][0].update(member="stack/contexts/x"),
        lambda d: d["stack"]["compose_file"].update(member="elsewhere.yaml"),
        lambda d: d["volumes"][0].update(prefix="volumes/x"),
        lambda d: d["volumes"].append(dict(d["volumes"][0])),
        lambda d: d.update(unknown_field=1),
    ],
)
def test_a_manifest_that_breaks_a_layout_rule_is_invalid(change: Any) -> None:
    document = as_document(prepared(stack_description(volumes=(volume_manifest(),))).manifest)
    document["stack"]["variables"].sort(key=lambda item: item["secret"])
    change(document)

    with pytest.raises(ExportFormatError) as raised:
        parse_document(document)

    assert raised.value.reason is ExportRefusal.MANIFEST_INVALID


def test_contexts_over_64_mib_in_total_are_invalid() -> None:
    document = as_document(prepared(stack_description()).manifest)
    template = document["stack"]["contexts"][0]
    document["stack"]["contexts"] = [
        {**template, "name": f"c{i}", "member": f"stack/contexts/{i}", "size": 16 * 1024 * 1024}
        for i in range(MAX_CONTEXTS_BYTES // (16 * 1024 * 1024) + 1)
    ]

    with pytest.raises(ExportFormatError):
        parse_document(document)


@pytest.mark.parametrize("version", [FORMAT_VERSION + 1, 99])
def test_a_newer_version_is_refused_naming_it(version: int) -> None:
    document = as_document(prepared(stack_description()).manifest)
    document.update(format_version=version, unrecognised={"shape": "of version 2"})

    with pytest.raises(ExportFormatError) as raised:
        parse_document(document)

    assert raised.value.reason is ExportRefusal.NEWER_VERSION
    assert raised.value.version == version
    assert str(version) in str(raised.value)


def test_a_newer_nested_volume_version_is_refused_naming_it() -> None:
    document = as_document(prepared(stack_description(volumes=(volume_manifest(),))).manifest)
    document["volumes"][0]["manifest"]["format_version"] = 2

    with pytest.raises(ExportFormatError) as raised:
        parse_document(document)

    assert (raised.value.reason, raised.value.version) == (ExportRefusal.NEWER_VERSION, 2)
    assert raised.value.nested
    assert str(raised.value) == (
        f"A volume's format version in the archive is 2; "
        f"this build reads up to {VOLUME_FORMAT_VERSION}."
    )


@pytest.mark.parametrize("volumes", [5, True, "volumes", {"manifest": {}}])
def test_a_volumes_entry_that_is_not_a_list_is_invalid(volumes: object) -> None:
    document = as_document(prepared(stack_description()).manifest)
    document["volumes"] = volumes

    with pytest.raises(ExportFormatError) as raised:
        parse_document(document)

    assert raised.value.reason is ExportRefusal.MANIFEST_INVALID


def test_a_manifest_nested_beyond_the_recursion_limit_is_invalid() -> None:
    depth = sys.getrecursionlimit() * 2
    data = b'{"format_version": 1, "deep": ' + b"[" * depth + b"]" * depth + b"}"

    with pytest.raises(ExportFormatError) as raised:
        parse_manifest(data)

    assert raised.value.reason is ExportRefusal.MANIFEST_INVALID


@pytest.mark.parametrize(
    "data", [b"not json", b"[]", b"{}", b'{"format_version": 0}', b'{"format_version": true}']
)
def test_data_that_is_not_a_manifest_is_invalid(data: bytes) -> None:
    with pytest.raises(ExportFormatError) as raised:
        parse_manifest(data)

    assert raised.value.reason is ExportRefusal.MANIFEST_INVALID


async def test_the_archive_lists_manifest_first_then_documents_then_each_volume() -> None:
    members = members_of(await stack_archive(volumes=2))

    assert list(members) == [
        "manifest.json",
        "stack/compose.yaml",
        f"stack/files/{FILE_NAME}",
        "stack/contexts/0",
        "volumes/0/manifest.json",
        "volumes/0/data",
        f"volumes/0/data/{VOLUME_FILE}",
        "volumes/0/skipped.json",
        "volumes/1/manifest.json",
        "volumes/1/data",
        f"volumes/1/data/{VOLUME_FILE}",
        "volumes/1/skipped.json",
    ]
    assert members["stack/compose.yaml"][1] == COMPOSE.encode()
    assert members[f"stack/files/{FILE_NAME}"][1] == FILE_CONTENT
    assert members[f"volumes/1/data/{VOLUME_FILE}"][1] == VOLUME_NOTE


async def test_each_nested_volume_carries_its_own_manifest_and_skipped_list() -> None:
    members = members_of(await stack_archive(volumes=2))

    first = VolumeManifest.model_validate_json(members["volumes/0/manifest.json"][1])
    second = VolumeManifest.model_validate_json(members["volumes/1/manifest.json"][1])
    skipped = json.loads(members["volumes/1/skipped.json"][1])

    assert (first.name, second.name) == ("v0", "v1")
    assert (skipped["count"], skipped["format_version"]) == (0, 1)


async def test_a_nested_volume_keeps_the_volume_layout_and_reports_what_it_skipped() -> None:
    volume = FakeVolume(
        volume_tar({VOLUME_FILE: VOLUME_NOTE}, Member("pipe", type=tarfile.FIFOTYPE))
    )
    stream = ExportArchiveStream(
        prepared(stack_description(volumes=(volume_manifest(),))), [volume.source()]
    )

    members = members_of(await write_archive(stream))

    result = stream.volume_results[0]
    assert members[f"volumes/0/data/{VOLUME_FILE}"][1] == VOLUME_NOTE
    assert (result.prefix, result.skipped_count) == ("volumes/0", 1)
    assert [(item.path, item.kind) for item in result.skipped] == [("pipe", "fifo")]
    assert json.loads(members["volumes/0/skipped.json"][1])["count"] == 1
    assert volume.exits == [None] and stream.bytes_out > 0


async def test_a_source_that_fails_mid_volume_fails_the_stream_and_leaves_its_context() -> None:
    volume = FakeVolume(fail_after=1)
    stream = ExportArchiveStream(
        prepared(stack_description(volumes=(volume_manifest(),))), [volume.source()]
    )

    with pytest.raises(ConnectionError):
        await write_archive(stream)

    assert volume.exits == [ConnectionError]


async def test_a_malformed_volume_tar_fails_with_stream_malformed_and_leaves_its_context() -> None:
    volume = FakeVolume(unterminated(volume_tar()))
    stream = ExportArchiveStream(
        prepared(stack_description(volumes=(volume_manifest(),))), [volume.source()]
    )

    with pytest.raises(StreamMalformed):
        await write_archive(stream)

    assert volume.exits == [StreamMalformed]


async def test_closing_the_stream_mid_volume_leaves_the_context_and_ends_the_worker() -> None:
    noise = random.Random(7)
    volume = FakeVolume(volume_tar({f"f{i}": noise.randbytes(50_000) for i in range(40)}))
    stream = ExportArchiveStream(
        prepared(stack_description(volumes=(volume_manifest(),))), [volume.source()]
    )

    async for _ in stream:
        if volume.entered:
            break
    await asyncio.wait_for(stream.aclose(), timeout=10)

    assert volume.entered == 1 and len(volume.exits) == 1 and volume.exits[0] is not None


def test_the_writer_refuses_volume_sources_that_do_not_match_the_manifest() -> None:
    with pytest.raises(ValueError):
        ExportArchiveStream(prepared(stack_description(volumes=(volume_manifest(),))), [])


async def archive_of(description: ExportDescription, passphrase: str | None) -> bytes:
    return await write_archive(ExportArchiveStream(prepared(description, passphrase), []))


@pytest.mark.parametrize(
    ("description", "passphrase"),
    [
        (stack_description(), PASSPHRASE),
        (stack_description(), None),
        (container_description(), PASSPHRASE),
        (container_description(), None),
    ],
    ids=["stack-sealed", "stack-omitted", "container-sealed", "container-omitted"],
)
async def test_no_secret_value_or_passphrase_appears_in_the_archive_bytes(
    description: ExportDescription, passphrase: str | None
) -> None:
    secrets = [
        SECRET_VALUE.encode(),
        SECRET_FILE_CONTENT,
        SECRET_ENVIRONMENT_VALUE.encode(),
        PASSPHRASE.encode(),
    ]

    archive = await archive_of(description, passphrase)

    plain = gzip.decompress(archive)
    assert not any(secret in plain or secret in archive for secret in secrets)


async def test_plain_values_are_in_the_archive_so_the_sentinel_check_can_fail() -> None:
    plain = gzip.decompress(await archive_of(stack_description(), PASSPHRASE))

    assert PLAIN_VALUE.encode() in plain and FILE_CONTENT in plain
