"""Export archives to import, and a daemon that accepts the tar written into a helper's volume."""

import gzip
import io
import json
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from tests.archive_support import FILE_MODE, Member, build_tar
from tests.docker_engine.fake_engine import Reply, Scope, error_reply, json_reply, split_version
from tests.volume_export_support import MOUNT_POINT, ROOT, VolumeEngine

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "volume_export" / "format_version_1.tar.gz"
EXPORTED_AT = "2024-05-06T07:08:09Z"
NAME = "imported-data"
NOTE = b"hello\n"
READ_ONLY_TEXT = "extraction point is read-only"


@dataclass(frozen=True, slots=True)
class Entry:
    """One archive member, named exactly as written."""

    name: str
    data: bytes = b""
    type: bytes = tarfile.REGTYPE
    linkname: str = ""
    mode: int = FILE_MODE
    uid: int = 0
    gid: int = 0
    xattrs: Mapping[str, str] = field(default_factory=dict)


def file(path: str, data: bytes = NOTE, **fields: Any) -> Entry:
    """Return a regular file below `data/`."""
    return Entry(f"data/{path}", data, **fields)


def directory(path: str, **fields: Any) -> Entry:
    """Return a directory below `data/`."""
    return Entry(f"data/{path}", type=tarfile.DIRTYPE, mode=0o755, **fields)


def symlink(path: str, target: str) -> Entry:
    """Return a symbolic link below `data/`."""
    return Entry(f"data/{path}", type=tarfile.SYMTYPE, linkname=target, mode=0o777)


def hardlink(path: str, target: str) -> Entry:
    """Return a hard link below `data/` to `target`, a name as the archive writes it."""
    return Entry(f"data/{path}", type=tarfile.LNKTYPE, linkname=target)


def manifest_document(**overrides: Any) -> dict[str, Any]:
    """Return a valid format-1 manifest, with fields replaced."""
    return {
        "format_version": 1,
        "kind": "volume",
        "exported_at": EXPORTED_AT,
        "instance_version": "0.2.2",
        "name": NAME,
        "driver": "local",
        "labels": {},
        "options": {},
        "removed_option_keys": [],
        "data_path": "data",
        **overrides,
    }


def _add(out: tarfile.TarFile, entry: Entry) -> None:
    info = tarfile.TarInfo(entry.name)
    info.type, info.linkname, info.mode = entry.type, entry.linkname, entry.mode
    info.uid, info.gid = entry.uid, entry.gid
    info.size = len(entry.data) if entry.type == tarfile.REGTYPE else 0
    info.pax_headers = dict(entry.xattrs)
    out.addfile(info, io.BytesIO(entry.data) if info.size else None)


def _document(out: tarfile.TarFile, name: str, content: object) -> None:
    raw = json.dumps(content).encode("utf-8")
    _add(out, Entry(name, raw))


def export_archive(
    *entries: Entry,
    manifest: Mapping[str, Any] | None = None,
    skipped: int | None = 0,
    root: bool = True,
) -> bytes:
    """Return a gzip archive laid out as a volume export writes it."""
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as out,
    ):
        _document(out, "manifest.json", manifest if manifest is not None else manifest_document())
        if root:
            _add(out, Entry("data", type=tarfile.DIRTYPE, mode=0o755))
        for entry in entries:
            _add(out, entry)
        if skipped is not None:
            _document(out, "skipped.json", {"format_version": 1, "count": skipped, "skipped": []})
    return buffer.getvalue()


def plain_tar(*entries: Entry) -> bytes:
    """Return a gzip tar with no manifest, such as one made with tar in a container."""
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as out,
    ):
        for entry in entries:
            _add(out, entry)
    return buffer.getvalue()


def stored(directory_path: Path, content: bytes, name: str = "upload.tar.gz") -> Path:
    """Write bytes to a file under `directory_path` and return its path."""
    path = directory_path / name
    path.write_bytes(content)
    return path


def read_tar(chunks: Sequence[bytes] | bytes) -> dict[str, tuple[tarfile.TarInfo, bytes]]:
    """Return every member of an uncompressed tar by name, with its data."""
    data = chunks if isinstance(chunks, bytes) else b"".join(chunks)
    found: dict[str, tuple[tarfile.TarInfo, bytes]] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for member in tar:
            source = tar.extractfile(member) if member.isreg() else None
            found[member.name] = (member, source.read() if source is not None else b"")
    return found


@dataclass(frozen=True, slots=True)
class Written:
    """A member the daemon extracted into a volume."""

    name: str
    type: bytes
    mode: int
    uid: int
    gid: int
    linkname: str
    data: bytes


def empty_volume_tar() -> bytes:
    """Return the archive the Engine sends for a volume that holds nothing."""
    return build_tar(Member(f"{ROOT}/", type=tarfile.DIRTYPE, mode=0o755))


class ImportEngine(VolumeEngine):
    """A daemon that extracts the tar `PUT` into a volume the helper mounts.

    A volume with no archive served for it reads as empty.
    """

    def __init__(self) -> None:
        super().__init__()
        self.written: dict[str, list[Written]] = {}
        self.put_queries: list[dict[str, str]] = []
        self.fail_after_members: int | None = None

    def files_of(self, volume: str) -> dict[str, Written]:
        """Return what was written into a volume, by name."""
        return {member.name: member for member in self.written.get(volume, [])}

    def put_requests(self) -> list[str]:
        """Return the id of the container of every archive write."""
        return [r.path.split("/")[2] for r in self.requests if r.method == "PUT"]

    async def _reply(self, scope: Scope, body: bytes) -> Reply | None:
        _, path = split_version(scope["path"])
        parts = path.strip("/").split("/")
        if scope["method"] == "PUT" and parts[0] == "containers" and parts[2:] == ["archive"]:
            query = {k: v[-1] for k, v in parse_qs(scope["query_string"].decode()).items()}
            return await self._put_archive(parts[1], query, body)
        if scope["method"] == "GET" and parts[0] == "containers" and parts[2:] == ["archive"]:
            self._serve_empty_by_default(parts[1])
        return await super()._reply(scope, body)

    def _serve_empty_by_default(self, reference: str) -> None:
        box = self.resolve(reference)
        mounted = (
            None
            if box is None
            else next((m["Name"] for m in box.mounts if m["Destination"] == MOUNT_POINT), None)
        )
        if mounted is not None and mounted not in self.volume_archives:
            self.volume_archives[mounted] = empty_volume_tar()

    async def _put_archive(self, reference: str, query: dict[str, str], body: bytes) -> Reply:
        box = self.resolve(reference)
        if box is None or query.get("path") != MOUNT_POINT:
            return error_reply(404)
        self.put_queries.append(query)
        mount = next((m for m in box.mounts if m["Destination"] == MOUNT_POINT), None)
        if mount is None:
            return error_reply(404)
        if not mount["RW"]:
            return json_reply({"message": READ_ONLY_TEXT}, 403)
        await self._pass_gate("put_archive")
        failed = self._failure("put_archive", box.id)
        if failed is not None:
            return failed
        members = self.written.setdefault(mount["Name"], [])
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tar:
            for index, member in enumerate(tar):
                if self.fail_after_members is not None and index >= self.fail_after_members:
                    return json_reply({"message": "unpack failed"}, 500)
                source = tar.extractfile(member) if member.isreg() else None
                members.append(
                    Written(
                        member.name,
                        member.type,
                        member.mode,
                        member.uid,
                        member.gid,
                        member.linkname,
                        source.read() if source is not None else b"",
                    )
                )
        return Reply(200)
