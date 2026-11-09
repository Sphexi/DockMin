"""The format_version 1 archive on disk: what a later release must keep reading."""

import json
import tarfile
from pathlib import Path

import pytest

from app.services.volume_manifest import VolumeManifest
from tests.volume_export_support import unpack

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "volume_export"
ARCHIVE = FIXTURE / "format_version_1.tar.gz"


@pytest.fixture(scope="module")
def members() -> dict[str, tuple[tarfile.TarInfo, bytes]]:
    return unpack(ARCHIVE.read_bytes())


def test_the_archive_opens_with_the_manifest_and_ends_with_the_skipped_list(
    members: dict[str, tuple[tarfile.TarInfo, bytes]],
) -> None:
    names = list(members)

    assert names[:2] == ["manifest.json", "data"] and names[-1] == "skipped.json"


def test_the_manifest_reads_as_version_1_with_credentials_listed_by_key_only(
    members: dict[str, tuple[tarfile.TarInfo, bytes]],
) -> None:
    text = members["manifest.json"][1]
    manifest = VolumeManifest.model_validate_json(text)

    assert (manifest.format_version, manifest.kind, manifest.data_path) == (1, "volume", "data")
    assert (manifest.name, manifest.driver, manifest.instance_version) == (
        "shop-data",
        "local",
        "0.2.2",
    )
    assert manifest.labels == {"team": "core"}
    assert manifest.options["o"] == "addr=10.0.0.5,username=svc,vers=3.0"
    assert manifest.removed_option_keys == ["o.password"]
    assert b"redacted-in-fixture" not in text


def test_the_files_keep_their_content_modes_owners_and_links(
    members: dict[str, tuple[tarfile.TarInfo, bytes]],
) -> None:
    secret, directory = members["data/secret.txt"][0], members["data/dir"][0]

    assert members["data/a.txt"][1] == b"alpha\n" and members["data/dir/sub/deep/n.txt"][1]
    assert (secret.mode, secret.uid, secret.gid) == (0o600, 1000, 1000)
    assert (directory.isdir(), directory.uid, directory.gid) == (True, 1000, 2000)
    assert members["data/rel"][0].linkname == "a.txt"
    assert members["data/abs"][0].linkname == "/etc/passwd"
    assert members["data/hard"][0].linkname == "data/a.txt"


def test_devices_and_pipes_are_absent_from_data_and_named_in_the_skipped_list(
    members: dict[str, tuple[tarfile.TarInfo, bytes]],
) -> None:
    skipped = json.loads(members["skipped.json"][1])

    assert not {"data/pipe", "data/null"} & members.keys()
    assert skipped["format_version"] == 1 and skipped["count"] == 2
    assert skipped["skipped"] == [
        {"path": "pipe", "type": "fifo"},
        {"path": "null", "type": "character_device"},
    ]
