"""hash_file: whole-file digests and the digests of each part of a multi-part file."""

import base64
import hashlib
import inspect
from pathlib import Path

import pytest

from app.backup.archive import hash_file
from app.backup.parts import MULTIPART_PART_BYTES

PART = 4096
PATTERN = bytes(range(256))


def content(size: int) -> bytes:
    return (PATTERN * (size // len(PATTERN) + 1))[:size]


def md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()


@pytest.mark.parametrize(
    ("size", "sizes"),
    [
        (1, []),
        (PART - 1, []),
        (PART, []),
        (PART + 1, [PART, 1]),
        (2 * PART, [PART, PART]),
        (2 * PART + 1, [PART, PART, 1]),
        (3 * PART - 1, [PART, PART, PART - 1]),
    ],
)
def test_a_file_yields_one_part_per_part_size_and_none_when_it_fits_in_one(
    tmp_path: Path, size: int, sizes: list[int]
) -> None:
    path = tmp_path / "file"
    path.write_bytes(content(size))

    digests = hash_file(path, with_parts=True, part_bytes=PART)

    assert [part.size for part in digests.parts] == sizes
    assert [part.offset for part in digests.parts] == [PART * i for i in range(len(sizes))]


def test_each_part_carries_the_sha256_and_base64_md5_of_its_own_bytes(tmp_path: Path) -> None:
    data = content(2 * PART + 100)
    path = tmp_path / "file"
    path.write_bytes(data)

    first, second, third = hash_file(path, with_parts=True, part_bytes=PART).parts

    assert first.sha256_hex == hashlib.sha256(data[:PART]).hexdigest()
    assert second.sha256_hex == hashlib.sha256(data[PART : 2 * PART]).hexdigest()
    assert third.sha256_hex == hashlib.sha256(data[2 * PART :]).hexdigest()
    assert first.md5_b64 == md5_b64(data[:PART])
    assert second.md5_b64 == md5_b64(data[PART : 2 * PART])
    assert third.md5_b64 == md5_b64(data[2 * PART :])


def test_the_whole_file_digests_and_size_are_the_same_with_and_without_parts(
    tmp_path: Path,
) -> None:
    data = content(2 * PART + 100)
    path = tmp_path / "file"
    path.write_bytes(data)

    split = hash_file(path, with_parts=True, part_bytes=PART)
    whole = hash_file(path)

    assert (split.sha256, split.md5_b64, split.size) == (
        hashlib.sha256(data).hexdigest(),
        md5_b64(data),
        len(data),
    )
    assert (whole.sha256, whole.md5_b64, whole.size) == (split.sha256, split.md5_b64, split.size)


def test_parts_are_not_computed_unless_asked_for(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(content(3 * PART))

    assert hash_file(path, part_bytes=PART).parts == ()


def test_an_empty_file_has_the_digests_of_nothing_and_no_parts(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"")

    digests = hash_file(path, with_parts=True, part_bytes=PART)

    assert (digests.size, digests.parts, digests.sha256) == (0, (), hashlib.sha256().hexdigest())


def test_the_default_part_size_is_the_clients_multipart_part_size() -> None:
    assert inspect.signature(hash_file).parameters["part_bytes"].default == MULTIPART_PART_BYTES
