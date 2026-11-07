"""Tests for the capped tar member reader."""

import tarfile

import pytest

from app.services.tar_members import (
    MAX_NAME_BYTES,
    TarLimits,
    TarMember,
    TarRefusal,
    TarRefused,
    read_tar_members,
)
from tests.archive_support import Member, build_tar

LIMITS = TarLimits(max_members=4, max_member_bytes=16, max_total_bytes=24)
CONTENT = b"content"
CHUNK = b"x" * 16
OVER_NAME = "n" * (MAX_NAME_BYTES + 1)
DIRECTORY = tarfile.DIRTYPE


def refusal(data: bytes, limits: TarLimits = LIMITS) -> TarRefused:
    with pytest.raises(TarRefused) as caught:
        read_tar_members(data, limits)
    return caught.value


def test_files_and_directories_are_returned_in_order_with_normalised_names() -> None:
    data = build_tar(
        Member("./", type=DIRECTORY),
        Member("./docs/", type=DIRECTORY),
        Member("./docs/a.txt", CONTENT),
        Member("b.txt", b""),
    )

    members = read_tar_members(data, LIMITS)

    assert members == (
        TarMember("docs", True, b""),
        TarMember("docs/a.txt", False, CONTENT),
        TarMember("b.txt", False, b""),
    )


def test_an_archive_with_no_members_yields_nothing() -> None:
    assert read_tar_members(build_tar(), LIMITS) == ()


def test_limits_are_inclusive_at_the_boundary() -> None:
    data = build_tar(
        Member("a", CHUNK),
        Member("b", b"y" * 8),
        Member("c", b""),
        Member("d", b"", type=DIRECTORY),
    )

    members = read_tar_members(data, LIMITS)

    assert [m.name for m in members] == ["a", "b", "c", "d"]


@pytest.mark.parametrize(
    ("members", "reason"),
    [
        pytest.param([Member("/etc/passwd", CONTENT)], TarRefusal.ABSOLUTE_NAME, id="absolute"),
        pytest.param([Member("../x", CONTENT)], TarRefusal.PARENT_COMPONENT, id="leading-parent"),
        pytest.param(
            [Member("a/../../x", CONTENT)], TarRefusal.PARENT_COMPONENT, id="inner-parent"
        ),
        pytest.param(
            [Member("a/..", type=DIRECTORY)], TarRefusal.PARENT_COMPONENT, id="dir-parent"
        ),
        pytest.param(
            [Member("a", b"", type=tarfile.SYMTYPE, linkname="/etc")],
            TarRefusal.MEMBER_TYPE,
            id="symlink",
        ),
        pytest.param(
            [Member("a", b"", type=tarfile.LNKTYPE, linkname="b")],
            TarRefusal.MEMBER_TYPE,
            id="hardlink",
        ),
        pytest.param(
            [Member("a", b"", type=tarfile.CHRTYPE)], TarRefusal.MEMBER_TYPE, id="char-device"
        ),
        pytest.param(
            [Member("a", b"", type=tarfile.BLKTYPE)], TarRefusal.MEMBER_TYPE, id="block-device"
        ),
        pytest.param([Member("a", b"", type=tarfile.FIFOTYPE)], TarRefusal.MEMBER_TYPE, id="fifo"),
        pytest.param(
            [Member("a", CONTENT), Member("a", CONTENT)], TarRefusal.DUPLICATE_NAME, id="duplicate"
        ),
        pytest.param(
            [Member("a", CONTENT), Member("./a", CONTENT)],
            TarRefusal.DUPLICATE_NAME,
            id="duplicate-normalised",
        ),
        pytest.param(
            [Member("a/", type=DIRECTORY), Member("a", CONTENT)],
            TarRefusal.DUPLICATE_NAME,
            id="file-over-directory",
        ),
        pytest.param(
            [Member(OVER_NAME, CONTENT)], TarRefusal.NAME_TOO_LONG, id="name-over-255-bytes"
        ),
        pytest.param(
            [Member("é" * 128, CONTENT)], TarRefusal.NAME_TOO_LONG, id="name-over-255-multibyte"
        ),
        pytest.param([Member("a\nb", CONTENT)], TarRefusal.BAD_NAME, id="control-character"),
        pytest.param([Member(".", CONTENT)], TarRefusal.BAD_NAME, id="file-named-dot"),
        pytest.param(
            [Member("a", CHUNK + b"x")], TarRefusal.MEMBER_TOO_LARGE, id="member-over-limit"
        ),
        pytest.param(
            [Member("a", CHUNK), Member("b", b"y" * 9)],
            TarRefusal.TOTAL_TOO_LARGE,
            id="total-over-limit",
        ),
        pytest.param(
            [Member(name, b"") for name in "abcde"],
            TarRefusal.TOO_MANY_MEMBERS,
            id="too-many-members",
        ),
        pytest.param(
            [Member("d/", b"data", type=DIRECTORY)], TarRefusal.MALFORMED, id="directory-with-data"
        ),
    ],
)
def test_each_hostile_member_is_refused_with_its_own_reason(
    members: list[Member], reason: TarRefusal
) -> None:
    assert refusal(build_tar(*members)).reason is reason


def test_a_name_that_is_not_utf8_is_refused() -> None:
    data = build_tar(Member("caf\xe9", CONTENT), encoding="latin-1")

    assert refusal(data).reason is TarRefusal.BAD_NAME


def test_a_name_of_exactly_255_bytes_is_accepted() -> None:
    members = read_tar_members(build_tar(Member("n" * MAX_NAME_BYTES, CONTENT)), LIMITS)

    assert len(members[0].name) == MAX_NAME_BYTES


def test_nothing_is_returned_when_a_later_member_is_refused() -> None:
    data = build_tar(Member("good", CONTENT), Member("../bad", CONTENT))

    assert refusal(data).reason is TarRefusal.PARENT_COMPONENT


def test_a_refusal_names_the_member_printably_and_truncated() -> None:
    error = refusal(build_tar(Member("a\x1b[31m" + "z" * 200, CONTENT)))

    assert error.member is not None
    assert error.member == "a?[31m" + "z" * 74


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not a tar archive" * 64, id="garbage"),
        pytest.param(build_tar(Member("a", CHUNK))[:600], id="truncated-data"),
        pytest.param(build_tar(Member("a", CONTENT), tail=b"trailing"), id="data-after-the-end"),
    ],
)
def test_bytes_that_are_not_a_whole_archive_are_malformed(data: bytes) -> None:
    assert refusal(data).reason is TarRefusal.MALFORMED
