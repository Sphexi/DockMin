"""Reading an untrusted archive: damage, hostile structure, limits and staging."""

import gzip
import io
import shutil
import stat
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from app.backup import archive, paths
from app.backup.manifest import ArchiveRefused, Manifest, MemberEntry
from tests.backup.support import (
    NEW_STACK,
    NEW_STACK_FILE,
    OLD_STACK,
    Stored,
    digest,
    rebuilt,
    sealed_manifest,
    stage,
    tar_info,
    write_tar,
)

_ntuple_diskusage = shutil._ntuple_diskusage
type Item = tuple[tarfile.TarInfo, bytes]
DATABASE_BODY = b"abc"
MEBIBYTE = 1024 * 1024
OWNER_DIRECTORY = 0o700
OWNER_FILE = 0o600
STACK_NAME = f"stacks/{'a' * 32}/compose.yaml"


def refusal(call: Callable[[], object]) -> str:
    with pytest.raises(ArchiveRefused) as raised:
        call()
    return raised.value.reason


@pytest.fixture(scope="module")
def small(stored: Stored) -> tuple[Manifest, Item, Item]:
    """A manifest listing one three-byte database, with its first two members."""
    manifest, raw = sealed_manifest(
        stored.manifest, (MemberEntry("database.sqlite3", 3, digest(DATABASE_BODY)),)
    )
    return (
        manifest,
        (tar_info("manifest.json", len(raw)), raw),
        (tar_info("database.sqlite3", 3), DATABASE_BODY),
    )


def test_a_genuine_archive_and_a_rebuilt_equivalent_both_verify(
    stored: Stored, tmp_path: Path
) -> None:
    archive.verify_members(stored.path, stored.manifest)
    archive.verify_members(rebuilt(tmp_path / "rebuilt.tar.gz", stored), stored.manifest)


def test_the_manifest_is_read_from_the_first_member_alone(stored: Stored) -> None:
    manifest, raw = archive.read_manifest(stored.path)

    assert (manifest, raw) == (stored.manifest, stored.raw_manifest)


def cut_in_half(data: bytes) -> bytes:
    return data[: len(data) // 2]


def flip_the_middle_byte(data: bytes) -> bytes:
    changed = bytearray(data)
    changed[len(changed) // 2] ^= 0xFF
    return bytes(changed)


@pytest.mark.parametrize("damage", [cut_in_half, flip_the_middle_byte])
def test_an_archive_cut_short_or_altered_in_transit_fails_at_the_digests(
    stored: Stored, tmp_path: Path, damage: Callable[[bytes], bytes]
) -> None:
    damaged = tmp_path / "damaged.tar.gz"
    damaged.write_bytes(damage(stored.path.read_bytes()))

    assert refusal(lambda: archive.verify_members(damaged, stored.manifest)) == "digest_mismatch"


def test_a_member_with_one_changed_byte_fails_its_digest(stored: Stored, tmp_path: Path) -> None:
    altered = bytearray(stored.members["database.sqlite3"])
    altered[-1] ^= 1
    members = {**stored.members, "database.sqlite3": bytes(altered)}
    path = write_tar(
        tmp_path / "altered.tar.gz", [(tar_info(n, len(d)), d) for n, d in members.items()]
    )

    assert refusal(lambda: archive.verify_members(path, stored.manifest)) == "digest_mismatch"


def test_a_member_shorter_than_declared_fails_as_a_digest_mismatch(
    stored: Stored, tmp_path: Path
) -> None:
    items = [
        (tar_info("manifest.json", len(stored.raw_manifest)), stored.raw_manifest),
        (tar_info("database.sqlite3", 10), b"x" * 10),
    ]
    path = write_tar(tmp_path / "short.tar.gz", items)

    assert refusal(lambda: archive.verify_members(path, stored.manifest)) == "digest_mismatch"


def test_an_archive_missing_the_stack_files_the_manifest_lists_fails_as_a_digest_mismatch(
    stored: Stored, tmp_path: Path
) -> None:
    names = list(stored.members)[:2]
    path = write_tar(
        tmp_path / "missing.tar.gz",
        [(tar_info(n, len(stored.members[n])), stored.members[n]) for n in names],
    )

    assert refusal(lambda: archive.verify_members(path, stored.manifest)) == "digest_mismatch"


@pytest.mark.parametrize(
    "content",
    [b"", b"not gzip at all" * 100, gzip.compress(b"short")[:30]],
    ids=["empty-file", "not-gzip", "cut-gzip"],
)
def test_a_file_that_is_not_an_archive_has_no_manifest(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "raw.bin"
    path.write_bytes(content)

    assert refusal(lambda: archive.read_manifest(path)) == "archive_invalid"


def test_a_symbolic_link_is_not_read_as_an_archive(stored: Stored, tmp_path: Path) -> None:
    link = tmp_path / "link.tar.gz"
    link.symlink_to(stored.path)

    assert refusal(lambda: archive.read_manifest(link)) == "archive_invalid"


HOSTILE: dict[str, Callable[[Item, Item], list[Item]]] = {
    "traversal-name": lambda first, db: [first, (tar_info("../evil", 1), b"x")],
    "absolute-name": lambda first, db: [first, (tar_info("/etc/passwd", 1), b"x")],
    "nested-traversal": lambda first, db: [
        first,
        (tar_info("stacks/../../evil/compose.yaml", 1), b"x"),
    ],
    "unlisted-name": lambda first, db: [first, (tar_info("extra.txt", 1), b"x")],
    "symbolic-link": lambda first, db: [
        first,
        (tar_info("database.sqlite3", 0, tarfile.SYMTYPE, "/etc/passwd"), b""),
    ],
    "hard-link": lambda first, db: [
        first,
        (tar_info("database.sqlite3", 0, tarfile.LNKTYPE, "manifest.json"), b""),
    ],
    "character-device": lambda first, db: [
        first,
        (tar_info("database.sqlite3", 0, tarfile.CHRTYPE), b""),
    ],
    "named-pipe": lambda first, db: [
        first,
        (tar_info("database.sqlite3", 0, tarfile.FIFOTYPE), b""),
    ],
    "directory": lambda first, db: [first, (tar_info("stacks/", 0, tarfile.DIRTYPE), b"")],
    "repeated-member": lambda first, db: [first, db, (tar_info("database.sqlite3", 3), b"abc")],
    "out-of-order": lambda first, db: [db, first],
    "repeated-manifest": lambda first, db: [
        first,
        (tar_info("manifest.json", len(first[1])), first[1]),
    ],
    "oversize-stack-file": lambda first, db: [
        first,
        db,
        (tar_info(STACK_NAME, 200_000), b"x" * 200_000),
    ],
    "oversize-manifest": lambda first, db: [
        (tar_info("manifest.json", 2 * MEBIBYTE), b"x" * (2 * MEBIBYTE))
    ],
}


@pytest.mark.parametrize("label", HOSTILE)
def test_a_member_that_is_not_a_plain_allow_listed_file_in_order_is_refused(
    small: tuple[Manifest, Item, Item], tmp_path: Path, label: str
) -> None:
    manifest, first, database = small
    path = write_tar(tmp_path / f"{label}.tar.gz", HOSTILE[label](first, database))

    assert refusal(lambda: archive.verify_members(path, manifest)) == "archive_invalid"


def test_a_header_that_renames_a_member_through_an_extended_record_is_refused(
    small: tuple[Manifest, Item, Item], tmp_path: Path
) -> None:
    manifest, first, _ = small
    renamed = tar_info("database.sqlite3", 3)
    renamed.pax_headers = {"path": "../../evil"}
    path = write_tar(tmp_path / "pax.tar.gz", [first, (renamed, DATABASE_BODY)])

    assert refusal(lambda: archive.verify_members(path, manifest)) == "archive_invalid"


def test_a_long_name_header_in_the_gnu_format_is_refused(
    small: tuple[Manifest, Item, Item], tmp_path: Path
) -> None:
    manifest, first, _ = small
    long_name = tar_info("stacks/" + "a" * 90 + "/x" * 5, 1)
    path = write_tar(tmp_path / "gnu.tar.gz", [first, (long_name, b"x")], fmt=tarfile.GNU_FORMAT)

    assert refusal(lambda: archive.verify_members(path, manifest)) == "archive_invalid"


def test_a_hostile_first_member_is_refused_when_only_the_manifest_is_read(tmp_path: Path) -> None:
    path = write_tar(tmp_path / "first.tar.gz", [(tar_info("../evil", 1), b"x")])

    assert refusal(lambda: archive.read_manifest(path)) == "archive_invalid"


def test_a_member_after_the_end_marker_is_refused(
    small: tuple[Manifest, Item, Item], tmp_path: Path
) -> None:
    manifest, first, database = small
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in (first, database):
            tar.addfile(info, io.BytesIO(data))
    extra = io.BytesIO()
    with tarfile.open(fileobj=extra, mode="w", format=tarfile.PAX_FORMAT) as tar:
        tar.addfile(tar_info("database.sqlite3", 3), io.BytesIO(DATABASE_BODY))
    path = tmp_path / "trailing.tar.gz"
    path.write_bytes(gzip.compress(plain.getvalue() + extra.getvalue()))

    assert refusal(lambda: archive.verify_members(path, manifest)) == "archive_invalid"


@pytest.fixture
def small_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive, "MAX_ARCHIVE_BYTES", MEBIBYTE)
    monkeypatch.setattr(archive, "STREAM_OVERHEAD", MEBIBYTE)


@pytest.mark.usefixtures("small_limits")
def test_a_gzip_stream_that_expands_beyond_the_limit_is_refused_before_it_is_read_whole(
    tmp_path: Path,
) -> None:
    path = tmp_path / "zeros.tar.gz"
    path.write_bytes(gzip.compress(bytes(8 * MEBIBYTE)))

    assert refusal(lambda: archive.read_manifest(path)) == "archive_invalid"


@pytest.mark.usefixtures("small_limits")
def test_zeros_after_a_valid_archive_beyond_the_limit_are_refused(
    small: tuple[Manifest, Item, Item], tmp_path: Path
) -> None:
    manifest, first, database = small
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in (first, database):
            tar.addfile(info, io.BytesIO(data))
    path = tmp_path / "trailer.tar.gz"
    path.write_bytes(gzip.compress(plain.getvalue() + bytes(8 * MEBIBYTE)))

    assert refusal(lambda: archive.verify_members(path, manifest)) == "archive_invalid"


@pytest.mark.usefixtures("small_limits")
def test_a_member_larger_than_the_archive_limit_is_refused(
    small: tuple[Manifest, Item, Item], tmp_path: Path
) -> None:
    manifest, first, _ = small
    big = (tar_info("database.sqlite3", 3 * MEBIBYTE), bytes(3 * MEBIBYTE))
    path = write_tar(tmp_path / "member.tar.gz", [first, big])

    assert refusal(lambda: archive.verify_members(path, manifest)) == "archive_invalid"


def test_staging_builds_every_path_itself_with_owner_only_modes(
    stored: Stored, tmp_path: Path
) -> None:
    staging = stage(stored, tmp_path / "staging")

    tree = sorted(str(path.relative_to(staging)) for path in staging.rglob("*"))
    folders = [staging, *(p for p in staging.rglob("*") if p.is_dir())]
    directories = {stat.S_IMODE(folder.stat().st_mode) for folder in folders}
    files = {stat.S_IMODE(p.stat().st_mode) for p in staging.rglob("*") if p.is_file()}
    assert tree == [
        "dockmin.db",
        "stacks",
        f"stacks/{OLD_STACK}",
        f"stacks/{OLD_STACK}/compose.yaml",
        f"stacks/{NEW_STACK}",
        f"stacks/{NEW_STACK}/compose.yaml",
    ]
    assert (directories, files) == ({OWNER_DIRECTORY}, {OWNER_FILE})
    assert (staging / "dockmin.db").read_bytes() == stored.members["database.sqlite3"]
    assert (staging / "stacks" / NEW_STACK / "compose.yaml").read_bytes() == NEW_STACK_FILE


def test_staging_refuses_a_directory_that_is_not_empty_or_is_a_link(
    stored: Stored, tmp_path: Path
) -> None:
    occupied = stage(stored, tmp_path / "occupied")
    link = tmp_path / "link"
    link.symlink_to(tmp_path)

    with pytest.raises(paths.UnsafePath):
        archive.stage_members(stored.path, stored.manifest, occupied)
    with pytest.raises(paths.UnsafePath):
        archive.stage_members(stored.path, stored.manifest, link)


def test_an_archive_replaced_after_it_was_verified_is_caught_while_staging(
    stored: Stored, tmp_path: Path
) -> None:
    altered = bytearray(stored.members["database.sqlite3"])
    altered[-1] ^= 1
    members = {**stored.members, "database.sqlite3": bytes(altered)}
    replacement = write_tar(
        tmp_path / "replaced.tar.gz", [(tar_info(n, len(d)), d) for n, d in members.items()]
    )

    assert (
        refusal(lambda: archive.stage_members(replacement, stored.manifest, tmp_path / "staging"))
        == "digest_mismatch"
    )


def test_staging_refuses_when_the_volume_has_too_little_free_space(
    stored: Stored, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(100, 99, 1))

    assert (
        refusal(lambda: archive.stage_members(stored.path, stored.manifest, tmp_path / "staging"))
        == "insufficient_space"
    )


def test_a_hostile_member_writes_nothing_outside_the_staging_directory(
    small: tuple[Manifest, Item, Item], tmp_path: Path
) -> None:
    manifest, first, _ = small
    path = write_tar(tmp_path / "evil.tar.gz", [first, (tar_info("../evil", 3), b"abc")])
    staging = tmp_path / "area" / "staging"
    staging.parent.mkdir()

    assert refusal(lambda: archive.stage_members(path, manifest, staging)) == "archive_invalid"

    assert not (tmp_path / "area" / "evil").exists() and not (tmp_path / "evil").exists()


def test_a_stop_request_ends_verifying_and_staging_between_chunks(
    stored: Stored, tmp_path: Path
) -> None:
    with pytest.raises(archive.StopRequested):
        archive.verify_members(stored.path, stored.manifest, stop=lambda: True)
    with pytest.raises(archive.StopRequested):
        archive.stage_members(stored.path, stored.manifest, tmp_path / "staging", stop=lambda: True)
