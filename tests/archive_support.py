"""Builders for tar archives and Engine path-stat headers."""

import base64
import io
import json
import tarfile
from dataclasses import dataclass

from app.docker_engine.archive import STAT_HEADER

GO_MODE_DIR = 1 << 31
GO_MODE_SYMLINK = 1 << 27
GO_MODE_DEVICE = 1 << 26
FILE_MODE = 0o644
HEADER = STAT_HEADER


@dataclass(frozen=True, slots=True)
class Member:
    """One tar member to write."""

    name: str
    data: bytes = b""
    type: bytes = tarfile.REGTYPE
    linkname: str = ""
    mode: int = FILE_MODE


def build_tar(*members: Member, encoding: str = "utf-8", tail: bytes = b"") -> bytes:
    """Write a tar of the members; `tail` is appended after the end-of-archive blocks.

    A name in another encoding needs the GNU format, since PAX records are always UTF-8.
    """
    buffer = io.BytesIO()
    layout = tarfile.PAX_FORMAT if encoding == "utf-8" else tarfile.GNU_FORMAT
    with tarfile.open(fileobj=buffer, mode="w", format=layout, encoding=encoding) as tar:
        for member in members:
            info = tarfile.TarInfo(member.name)
            info.type = member.type
            info.linkname = member.linkname
            info.mode = member.mode
            info.size = len(member.data)
            tar.addfile(info, io.BytesIO(member.data) if member.data else None)
    return buffer.getvalue() + tail


def single_file_tar(name: str, data: bytes) -> bytes:
    """Return the archive the Engine sends for one regular file."""
    return build_tar(Member(name, data))


def stat_header(
    name: str, size: int, mode: int = FILE_MODE, link_target: str = ""
) -> dict[str, str]:
    """Return the response header carrying a base64 JSON path stat."""
    document = {
        "name": name,
        "size": size,
        "mode": mode,
        "mtime": "2024-05-06T07:08:09Z",
        "linkTarget": link_target,
    }
    return {HEADER: encode_stat(document)}


def encode_stat(document: object) -> str:
    """Return the base64 form of a JSON document."""
    return base64.b64encode(json.dumps(document).encode("utf-8")).decode("ascii")
