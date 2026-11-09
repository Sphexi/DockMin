"""Validation and rewriting of volume export archives."""

import tarfile
from pathlib import Path

import pytest

from app.services.volume_import_archive import (
    ArchiveLimits,
    ArchiveRefusal,
    VolumeArchiveRefused,
    inspect_volume_archive,
    rewrite_volume_archive,
)
from tests.volume_import_support import (
    FIXTURE,
    Entry,
    directory,
    export_archive,
    file,
    hardlink,
    manifest_document,
    plain_tar,
    read_tar,
    stored,
    symlink,
)

SETUID_FILE = 0o4755
SETGID_DIRECTORY = 0o2755
STICKY_DIRECTORY = 0o1777
OWNER = 1234
GROUP = 4321
CHUNK_PAYLOAD = b"x" * (600 * 1024)


def refused(path: Path, limits: ArchiveLimits | None = None) -> VolumeArchiveRefused:
    with pytest.raises(VolumeArchiveRefused) as raised:
        inspect_volume_archive(path, limits)
    return raised.value


@pytest.mark.parametrize(
    ("entries", "reason", "member"),
    [
        pytest.param(
            [Entry("data/null", type=tarfile.CHRTYPE)],
            ArchiveRefusal.MEMBER_TYPE,
            "data/null",
            id="character-device",
        ),
        pytest.param(
            [Entry("data/disk", type=tarfile.BLKTYPE)],
            ArchiveRefusal.MEMBER_TYPE,
            "data/disk",
            id="block-device",
        ),
        pytest.param(
            [Entry("data/pipe", type=tarfile.FIFOTYPE)],
            ArchiveRefusal.MEMBER_TYPE,
            "data/pipe",
            id="fifo",
        ),
        pytest.param(
            [Entry("data/sock", type=b"s")], ArchiveRefusal.MEMBER_TYPE, "data/sock", id="socket"
        ),
        pytest.param([Entry("/abs")], ArchiveRefusal.ABSOLUTE_NAME, "/abs", id="absolute-name"),
        pytest.param(
            [Entry("data/../x")], ArchiveRefusal.PARENT_COMPONENT, "data/../x", id="parent-inside"
        ),
        pytest.param([Entry("../x")], ArchiveRefusal.PARENT_COMPONENT, "../x", id="parent-first"),
        pytest.param([Entry("other/x")], ArchiveRefusal.OUTSIDE_DATA, "other/x", id="outside"),
        pytest.param(
            [file("a"), file("a")], ArchiveRefusal.DUPLICATE_NAME, "data/a", id="duplicate"
        ),
        pytest.param(
            [file("a"), file("a/b")], ArchiveRefusal.NAME_CONFLICT, "data/a/b", id="file-as-dir"
        ),
        pytest.param(
            [file("a/b"), file("a")], ArchiveRefusal.NAME_CONFLICT, "data/a", id="dir-as-file"
        ),
        pytest.param(
            [symlink("etc", "/etc"), file("etc/passwd")],
            ArchiveRefusal.SYMLINK_PARENT,
            "data/etc/passwd",
            id="write-through-symlink",
        ),
        pytest.param(
            [symlink("lnk", "/"), directory("lnk/sub")],
            ArchiveRefusal.SYMLINK_PARENT,
            "data/lnk/sub",
            id="directory-through-symlink",
        ),
        pytest.param(
            [symlink("up", "../../x")],
            ArchiveRefusal.SYMLINK_ESCAPE,
            "data/up",
            id="symlink-leaves-root",
        ),
        pytest.param(
            [directory("d"), symlink("d/up", "../../x")],
            ArchiveRefusal.SYMLINK_ESCAPE,
            "data/d/up",
            id="symlink-from-subdirectory-leaves-root",
        ),
        pytest.param(
            [hardlink("h", "data/a"), file("a")],
            ArchiveRefusal.HARDLINK_TARGET,
            "data/h",
            id="hardlink-to-later-member",
        ),
        pytest.param(
            [hardlink("h", "data/missing")],
            ArchiveRefusal.HARDLINK_TARGET,
            "data/h",
            id="hardlink-to-absent-member",
        ),
        pytest.param(
            [hardlink("h", "/etc/passwd")],
            ArchiveRefusal.HARDLINK_TARGET,
            "data/h",
            id="hardlink-to-absolute-path",
        ),
        pytest.param(
            [hardlink("h", "data/../../etc/passwd")],
            ArchiveRefusal.HARDLINK_TARGET,
            "data/h",
            id="hardlink-outside-archive",
        ),
        pytest.param(
            [file("a"), symlink("s", "a"), hardlink("h", "data/s")],
            ArchiveRefusal.HARDLINK_TARGET,
            "data/h",
            id="hardlink-to-symlink",
        ),
        pytest.param(
            [file("a", uid=2**33)], ArchiveRefusal.BAD_OWNER, "data/a", id="owner-out-of-range"
        ),
        pytest.param([file("a\x07b")], ArchiveRefusal.BAD_NAME, "data/a?b", id="control-character"),
        pytest.param([file("a//b")], ArchiveRefusal.BAD_NAME, "data/a//b", id="empty-component"),
        pytest.param(
            [file("x" * 300)], ArchiveRefusal.NAME_TOO_LONG, f"data/{'x' * 75}", id="long-name"
        ),
    ],
)
def test_an_archive_with_an_unsafe_member_is_refused_naming_the_member(
    tmp_path: Path, entries: list[Entry], reason: ArchiveRefusal, member: str
) -> None:
    path = stored(tmp_path, export_archive(*entries))

    error = refused(path)

    assert (error.reason, error.member) == (reason, member)


def test_an_archive_with_more_members_than_the_limit_is_refused_naming_the_first_over(
    tmp_path: Path,
) -> None:
    path = stored(tmp_path, export_archive(file("a"), file("b"), file("c")))

    error = refused(path, ArchiveLimits(max_members=2))

    assert (error.reason, error.member) == (ArchiveRefusal.TOO_MANY_MEMBERS, "data/c")


def test_an_archive_with_more_bytes_than_the_limit_is_refused_naming_the_member_over(
    tmp_path: Path,
) -> None:
    path = stored(tmp_path, export_archive(file("a", b"123456"), file("b", b"123456")))

    error = refused(path, ArchiveLimits(max_bytes=10))

    assert (error.reason, error.member) == (ArchiveRefusal.TOTAL_TOO_LARGE, "data/b")


def test_a_plain_tar_without_a_manifest_is_refused_as_not_an_export(tmp_path: Path) -> None:
    path = stored(tmp_path, plain_tar(file("a")))

    assert refused(path).reason == ArchiveRefusal.NOT_AN_EXPORT


def test_a_newer_format_version_is_refused_with_the_version_named(tmp_path: Path) -> None:
    path = stored(tmp_path, export_archive(file("a"), manifest=manifest_document(format_version=2)))

    error = refused(path)

    assert error.reason == ArchiveRefusal.NEWER_VERSION and error.version == 2
    assert "is 2" in error.message and "up to 1" in error.message


@pytest.mark.parametrize(
    "manifest",
    [
        pytest.param(manifest_document(kind="stack"), id="other-kind"),
        pytest.param(manifest_document(format_version=0), id="version-zero"),
        pytest.param(manifest_document(format_version="1"), id="version-text"),
        pytest.param(manifest_document(data_path="files"), id="other-data-path"),
        pytest.param({**manifest_document(), "extra": 1}, id="unknown-field"),
        pytest.param({"format_version": 1}, id="missing-fields"),
    ],
)
def test_a_manifest_that_is_not_a_volume_manifest_is_refused(
    tmp_path: Path, manifest: dict[str, object]
) -> None:
    path = stored(tmp_path, export_archive(file("a"), manifest=manifest))

    assert refused(path).reason == ArchiveRefusal.MANIFEST_INVALID


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        pytest.param(b"", ArchiveRefusal.NOT_GZIP, id="empty-file"),
        pytest.param(b"a" * 2048, ArchiveRefusal.NOT_GZIP, id="not-gzip"),
        pytest.param(export_archive(file("a"))[:-30], ArchiveRefusal.MALFORMED, id="truncated"),
        pytest.param(export_archive(), ArchiveRefusal.EMPTY, id="no-data"),
    ],
)
def test_a_file_that_is_not_a_complete_export_is_refused(
    tmp_path: Path, content: bytes, reason: ArchiveRefusal
) -> None:
    assert refused(stored(tmp_path, content)).reason == reason


def test_an_entry_after_the_skipped_list_is_refused(tmp_path: Path) -> None:
    path = stored(
        tmp_path,
        export_archive(Entry("skipped.json", b'{"count": 0}'), file("late"), skipped=None),
    )

    error = refused(path)

    assert (error.reason, error.member) == (ArchiveRefusal.OUTSIDE_DATA, "data/late")


def test_the_format_version_1_fixture_is_read_in_full() -> None:
    summary = inspect_volume_archive(FIXTURE)

    assert summary.manifest.format_version == 1 and summary.manifest.name == "shop-data"
    assert (summary.files, summary.directories, summary.symlinks, summary.hardlinks) == (3, 1, 2, 1)
    assert (summary.members, summary.bytes, summary.skipped) == (7, 19, 2)


def test_the_fixture_is_rewritten_relative_to_the_volume_with_owners_and_links_kept() -> None:
    members = read_tar(b"".join(rewrite_volume_archive(FIXTURE)))

    assert list(members) == [
        "a.txt",
        "secret.txt",
        "dir",
        "dir/sub/deep/n.txt",
        "rel",
        "abs",
        "hard",
    ]
    secret, directory_info = members["secret.txt"][0], members["dir"][0]
    assert (secret.mode, secret.uid, secret.gid) == (0o600, 1000, 1000)
    assert (directory_info.isdir(), directory_info.uid, directory_info.gid) == (True, 1000, 2000)
    assert members["a.txt"][1] == b"alpha\n"
    assert members["rel"][0].linkname == "a.txt" and members["abs"][0].linkname == "/etc/passwd"
    assert members["hard"][0].islnk() and members["hard"][0].linkname == "a.txt"


def test_setuid_and_setgid_bits_are_cleared_while_sticky_bits_and_owners_are_kept(
    tmp_path: Path,
) -> None:
    path = stored(
        tmp_path,
        export_archive(
            file("tool", mode=SETUID_FILE, uid=OWNER, gid=GROUP),
            directory("shared", uid=OWNER, gid=GROUP),
            Entry("data/tmp", type=tarfile.DIRTYPE, mode=STICKY_DIRECTORY),
            Entry("data/queue", type=tarfile.DIRTYPE, mode=SETGID_DIRECTORY),
        ),
    )

    members = read_tar(b"".join(rewrite_volume_archive(path)))

    assert members["tool"][0].mode == 0o755
    assert (members["tool"][0].uid, members["tool"][0].gid) == (OWNER, GROUP)
    assert members["tmp"][0].mode == STICKY_DIRECTORY
    assert members["queue"][0].mode == 0o755


def test_only_user_extended_attributes_are_kept_and_the_dropped_ones_are_counted(
    tmp_path: Path,
) -> None:
    path = stored(
        tmp_path,
        export_archive(
            file(
                "bin",
                xattrs={
                    "SCHILY.xattr.user.note": "kept",
                    "SCHILY.xattr.security.capability": "cap",
                },
            ),
            file("plain"),
        ),
    )

    summary = inspect_volume_archive(path)
    members = read_tar(b"".join(rewrite_volume_archive(path)))

    assert members["bin"][0].pax_headers == {"SCHILY.xattr.user.note": "kept"}
    assert summary.xattr_members_dropped == 1


def test_a_large_file_is_rewritten_in_several_chunks_with_its_content_intact(
    tmp_path: Path,
) -> None:
    path = stored(tmp_path, export_archive(file("big", CHUNK_PAYLOAD), file("small")))

    chunks = list(rewrite_volume_archive(path))

    assert len(chunks) > 1
    assert read_tar(b"".join(chunks))["big"][1] == CHUNK_PAYLOAD


def test_a_file_that_stops_passing_validation_after_the_review_is_refused_when_rewritten(
    tmp_path: Path,
) -> None:
    path = stored(tmp_path, export_archive(file("a")))
    inspect_volume_archive(path)
    path.write_bytes(export_archive(Entry("data/null", type=tarfile.CHRTYPE)))

    with pytest.raises(VolumeArchiveRefused) as raised:
        list(rewrite_volume_archive(path))

    assert raised.value.reason == ArchiveRefusal.MEMBER_TYPE
