"""Tests for build context archive validation and extraction."""

import asyncio
import gzip
import hashlib
import stat
import tarfile
import threading
from pathlib import Path

import pytest

import app.services.build_context_archive as archive_module
from app.db.repositories import stack_build_context as limits
from app.services.build_context_archive import (
    ContextRefused,
    extract_context_archive,
    inspect_context,
    read_context_file,
    run_limited,
)
from tests.archive_support import Member, build_tar

DIRECTORY = tarfile.DIRTYPE
SYMLINK = tarfile.SYMTYPE
HARDLINK = tarfile.LNKTYPE
DEVICE = tarfile.CHRTYPE
DOCKERFILE = b"FROM scratch\n"
SCRIPT = b"#!/bin/sh\n"
EXECUTABLE = 0o755
SETUID_FILE = 0o4755
PRIVATE = 0o600


def refusal(data: bytes) -> ContextRefused:
    with pytest.raises(ContextRefused) as caught:
        inspect_context(data)
    return caught.value


def packed(*members: Member) -> bytes:
    return gzip.compress(build_tar(*members), mtime=0)


@pytest.mark.parametrize(
    ("members", "reason"),
    [
        pytest.param(
            [Member("l", type=SYMLINK, linkname="/etc/passwd")], "member_type", id="symlink"
        ),
        pytest.param(
            [Member("l", type=HARDLINK, linkname="Dockerfile")], "member_type", id="hardlink"
        ),
        pytest.param([Member("dev", type=DEVICE)], "member_type", id="device"),
        pytest.param([Member("/etc/passwd", b"x")], "absolute_name", id="absolute"),
        pytest.param([Member("a/../../x", b"x")], "parent_component", id="parent"),
        pytest.param(
            [Member("a", b"x"), Member("a/b", b"x")], "name_conflict", id="file-as-directory"
        ),
        pytest.param(
            [Member("a/b", b"x"), Member("a", b"x")], "name_conflict", id="directory-as-file"
        ),
        pytest.param(
            [Member("a", b"x"), Member("a/b/c", b"x")], "name_conflict", id="file-above-a-deep-path"
        ),
        pytest.param([Member("a", b"x"), Member("a", b"y")], "duplicate_name", id="duplicate"),
        pytest.param([Member("d", type=DIRECTORY)], "empty", id="only-a-directory"),
        pytest.param([Member("a", b"")], "empty", id="only-an-empty-file"),
    ],
)
def test_an_archive_that_breaks_a_rule_is_refused_whether_plain_or_compressed(
    members: list[Member], reason: str
) -> None:
    plain = refusal(build_tar(*members))
    compressed = refusal(packed(*members))

    assert plain.reason == compressed.reason == reason


def test_an_archive_with_more_entries_than_the_limit_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_CONTEXT_MEMBERS", 2)

    refused = refusal(build_tar(Member("a", b"x"), Member("b", b"x"), Member("c", b"x")))

    assert (refused.reason, refused.member) == ("too_many_members", "c")


def test_an_archive_that_extracts_past_the_limit_is_refused_even_when_its_upload_is_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_CONTEXT_EXTRACTED_BYTES", 8)
    deflated = packed(Member("a", b"z" * 6), Member("b", b"z" * 6))

    assert refusal(deflated).reason == "total_too_large"


def test_a_compressed_stream_that_inflates_past_the_extracted_limit_is_refused_while_inflating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_CONTEXT_EXTRACTED_BYTES", 0)
    monkeypatch.setattr(limits, "MAX_CONTEXT_MEMBERS", 0)
    monkeypatch.setattr("app.services.build_context_archive._TAR_FIXED_BYTES", 0)

    assert refusal(gzip.compress(b"\0" * 4096)).reason == "total_too_large"


def test_an_upload_past_the_upload_limit_is_refused_before_it_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_CONTEXT_UPLOAD_BYTES", 16)

    assert refusal(build_tar(Member("a", b"x"))).reason == "upload_too_large"


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not an archive at all" * 40, id="garbage"),
        pytest.param(gzip.compress(build_tar(Member("a", b"x")))[:-12], id="truncated-gzip"),
        pytest.param(gzip.compress(build_tar(Member("a", b"x"))) + b"junk", id="trailing-bytes"),
        pytest.param(gzip.compress(b"plain text, not a tar" * 40), id="gzip-of-text"),
    ],
)
def test_bytes_that_are_not_an_archive_are_refused_as_malformed(data: bytes) -> None:
    assert refusal(data).reason == "malformed"


def test_a_valid_archive_is_returned_with_its_digest_counts_and_form() -> None:
    members = (
        Member("src", type=DIRECTORY),
        Member("Dockerfile", DOCKERFILE),
        Member("src/a", b"ab"),
    )

    plain = inspect_context(build_tar(*members))
    deflated = inspect_context(packed(*members))

    assert plain.compressed is False and deflated.compressed is True
    assert (plain.member_count, plain.extracted_size) == (3, len(DOCKERFILE) + 2)
    assert plain.content_digest == hashlib.sha256(plain.content).hexdigest()
    assert deflated.content_digest != plain.content_digest


def test_the_limits_are_the_documented_sizes() -> None:
    assert limits.MAX_CONTEXT_UPLOAD_BYTES == 16 * 1024 * 1024
    assert limits.MAX_CONTEXT_EXTRACTED_BYTES == 256 * 1024 * 1024
    assert limits.MAX_CONTEXT_MEMBERS == 10_000
    assert limits.MAX_STACK_CONTEXT_BYTES == 64 * 1024 * 1024


def modes_under(root: Path) -> dict[str, int]:
    return {
        path.relative_to(root).as_posix(): stat.S_IMODE(path.stat().st_mode)
        for path in sorted(root.rglob("*"))
    }


def test_extraction_writes_files_and_directories_with_masked_modes(tmp_path: Path) -> None:
    data = packed(
        Member("src", type=DIRECTORY, mode=0o700),
        Member("Dockerfile", DOCKERFILE, mode=PRIVATE),
        Member("run.sh", SCRIPT, mode=SETUID_FILE),
        Member("src/deep/a.txt", b"a", mode=0o666),
    )
    destination = tmp_path / "context"
    destination.mkdir()

    extract_context_archive(data, destination, digest=hashlib.sha256(data).hexdigest())

    assert modes_under(destination) == {
        "Dockerfile": 0o644,
        "run.sh": 0o755,
        "src": 0o755,
        "src/deep": 0o755,
        "src/deep/a.txt": 0o644,
    }
    assert (destination / "run.sh").read_bytes() == SCRIPT
    assert (destination / "src" / "deep" / "a.txt").read_bytes() == b"a"


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        pytest.param(Member("l", type=SYMLINK, linkname="/etc"), "member_type", id="symlink"),
        pytest.param(Member("../escape", b"x"), "parent_component", id="parent"),
        pytest.param(Member("/abs", b"x"), "absolute_name", id="absolute"),
    ],
)
def test_extraction_revalidates_the_archive_and_never_writes_the_refused_entry(
    tmp_path: Path, member: Member, reason: str
) -> None:
    destination = tmp_path / "context"
    destination.mkdir()

    with pytest.raises(ContextRefused) as caught:
        extract_context_archive(build_tar(Member("ok", b"x"), member), destination)

    assert caught.value.reason == reason
    assert {path.name for path in destination.iterdir()} <= {"ok"}
    assert not (tmp_path / "escape").exists()


def test_extraction_refuses_an_archive_that_no_longer_matches_its_digest(tmp_path: Path) -> None:
    destination = tmp_path / "context"
    destination.mkdir()

    with pytest.raises(ContextRefused) as caught:
        extract_context_archive(build_tar(Member("a", b"x")), destination, digest="0" * 64)

    assert caught.value.reason == "digest_mismatch"
    assert list(destination.iterdir()) == []


def test_extraction_goes_only_into_an_empty_directory_that_is_not_a_link(tmp_path: Path) -> None:
    data = build_tar(Member("a", b"x"))
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "present").write_text("x", encoding="utf-8")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    for destination in (occupied, link, tmp_path / "missing"):
        with pytest.raises(ValueError, match="empty directory"):
            extract_context_archive(data, destination)

    assert list(real.iterdir()) == []


def test_a_compressed_archive_that_inflates_past_the_ceiling_stops_before_extracting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "MAX_CONTEXT_EXTRACTED_BYTES", 1024)
    monkeypatch.setattr(limits, "MAX_CONTEXT_MEMBERS", 1)
    monkeypatch.setattr(archive_module, "_TAR_FIXED_BYTES", 0)
    destination = tmp_path / "context"
    destination.mkdir()

    with pytest.raises(ContextRefused) as caught:
        extract_context_archive(gzip.compress(b"\0" * 64 * 1024), destination)

    assert caught.value.reason == "total_too_large"
    assert list(destination.iterdir()) == []


def test_a_member_larger_than_its_header_allows_is_refused_without_being_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_CONTEXT_EXTRACTED_BYTES", 4)

    refused = refusal(packed(Member("big", b"z" * 5)))

    assert (refused.reason, refused.member) == ("member_too_large", "big")


def test_reading_a_file_stops_at_its_member_so_a_damaged_tail_is_not_reached() -> None:
    data = build_tar(Member("Dockerfile", DOCKERFILE), Member("later", b"x" * 2048))

    content = read_context_file(data[: 512 + 512], "Dockerfile")

    assert content == DOCKERFILE


def test_reading_a_file_returns_none_for_a_directory_or_a_missing_name() -> None:
    data = packed(Member("src", type=DIRECTORY), Member("Dockerfile", DOCKERFILE))

    assert read_context_file(data, "src") is None
    assert read_context_file(data, "absent") is None


def test_reading_a_file_longer_than_the_cap_returns_one_byte_past_it() -> None:
    data = packed(Member("Dockerfile", b"x" * 100))

    assert read_context_file(data, "Dockerfile", max_bytes=10) == b"x" * 11


def test_reading_a_file_refuses_an_archive_that_no_longer_matches_its_digest() -> None:
    with pytest.raises(ContextRefused) as caught:
        read_context_file(build_tar(Member("a", b"x")), "a", digest="0" * 64)

    assert caught.value.reason == "digest_mismatch"


async def test_no_more_than_the_slot_count_of_archives_run_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_module, "_slots", asyncio.Semaphore(2))
    entered = threading.Semaphore(0)
    gate = threading.Event()
    lock = threading.Lock()
    running = 0
    peak = 0

    def work() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        entered.release()
        gate.wait()
        with lock:
            running -= 1

    tasks = [asyncio.ensure_future(run_limited(work)) for _ in range(3)]
    await asyncio.to_thread(entered.acquire)
    await asyncio.to_thread(entered.acquire)
    assert archive_module._slots.locked()
    gate.set()
    await asyncio.gather(*tasks)

    assert peak == 2
