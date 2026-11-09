"""Streams, archives and a daemon for the volume export tests."""

import gzip
import io
import tarfile
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import parse_qs

from tests.archive_support import GO_MODE_DIR, Member, build_tar, stat_header
from tests.docker_engine.container_engine import TRUTHY, Box, ContainerEngine
from tests.docker_engine.fake_engine import Reply, Scope, error_reply, split_version

ROOT = "dockmin-read"
MOUNT_POINT = f"/{ROOT}"
SECRET_FILE = "secret.txt"
SECRET_MODE = 0o600
SECRET_OWNER = 1000
NOTE = b"hello\n"
NOTE_FILE = "notes/a.txt"
LINK_TARGET = "/etc/passwd"
MIB = 1024 * 1024


def volume_tar(
    files: Mapping[str, bytes] | None = None, *extra: Member, tail: bytes = b""
) -> bytes:
    """Return the archive the Engine sends for a volume mounted at the helper's mount point."""
    members = [Member(f"{ROOT}/", type=tarfile.DIRTYPE, mode=0o755)]
    for path, data in (files or {NOTE_FILE: NOTE}).items():
        members.append(Member(f"{ROOT}/{path}", data))
    members.extend(Member(f"{ROOT}/{m.name}", m.data, m.type, m.linkname, m.mode) for m in extra)
    return build_tar(*members, tail=tail)


def unterminated(archive: bytes) -> bytes:
    """Cut the end-of-archive blocks off a tar, as a connection that ends early would."""
    return archive.rstrip(b"\0")


async def chunked(data: bytes, size: int = 8192) -> AsyncIterator[bytes]:
    """Yield `data` in chunks."""
    for start in range(0, len(data), size):
        yield data[start : start + size]


def unpack(archive: bytes) -> dict[str, tuple[tarfile.TarInfo, bytes]]:
    """Return every member of a gzip archive by name, with its bytes."""
    found: dict[str, tuple[tarfile.TarInfo, bytes]] = {}
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as tar:
        for member in tar:
            source = tar.extractfile(member) if member.isreg() else None
            found[member.name] = (member, source.read() if source is not None else b"")
    return found


class VolumeEngine(ContainerEngine):
    """A daemon that serves the archive of the volume a helper mounts, and removes on `v`."""

    def __init__(self) -> None:
        super().__init__()
        self.volume_archives: dict[str, Reply | bytes] = {}
        self.archive_reads: list[str] = []

    def serve_volume(self, volume: str, archive: Reply | bytes) -> None:
        """Make the archive endpoint answer for a helper that mounts `volume`."""
        self.volume_archives[volume] = archive

    def helpers(self) -> list[Box]:
        """Return the containers carrying the read helper's label."""
        return [b for b in self.boxes.values() if b.labels.get("dockmin.role") == "file-reader"]

    async def _reply(self, scope: Scope, body: bytes) -> Reply | None:
        _, path = split_version(scope["path"])
        parts = path.strip("/").split("/")
        if scope["method"] != "GET" or parts[0] != "containers" or parts[2:] != ["archive"]:
            return await super()._reply(scope, body)
        query = {k: v[-1] for k, v in parse_qs(scope["query_string"].decode()).items()}
        box = self.resolve(parts[1])
        if box is None or query["path"] != MOUNT_POINT:
            return error_reply(404)
        mounted = next((m["Name"] for m in box.mounts if m["Destination"] == MOUNT_POINT), None)
        self.archive_reads.append(box.state)
        served = self.volume_archives.get(mounted or "")
        if served is None:
            return error_reply(404)
        if isinstance(served, Reply):
            return served
        return Reply(200, served, stat_header(ROOT, 4096, GO_MODE_DIR | 0o755))

    def _remove(self, box: Box, query: dict[str, str]) -> Reply:
        reply = super()._remove(box, query)
        if reply.status == 204 and query.get("v") in TRUTHY:
            for mount in box.mounts:
                if mount["Type"] == "volume" and self.volumes.get(mount["Name"]) is True:
                    del self.volumes[mount["Name"]]
        return reply


def helper_mount(box: Box) -> dict[str, Any]:
    """Return the one mount of a helper."""
    (mount,) = box.mounts
    return mount
