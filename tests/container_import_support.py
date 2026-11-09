"""Container export archives to import, and a daemon that takes their networks and volumes."""

import gzip
import io
import json
import tarfile
from collections.abc import Mapping
from pathlib import Path

from app.services.export_archive_writer import ExportArchiveStream
from app.services.export_build import EnvironmentInput
from app.services.export_manifest import ImageReference, IpamPoolEntry, NetworkEntry
from tests.export_archive_support import (
    FakeVolume,
    container_description,
    prepared,
    volume_manifest,
    write_archive,
)
from tests.volume_export_support import volume_tar
from tests.volume_import_support import ImportEngine

IMAGE = "busybox:1"
CONTAINER_NAME = "shop-web"
VOLUME = "shop-data"
NETWORK = "shop-net"
SUBNET = "172.30.0.0/24"
NOTE_FILE = "notes/a.txt"
NOTE = b"stored in the volume\n"
PLAIN_NAME = "MODE"
PLAIN_VALUE = "plain-env-visible"
SECRET_NAME = "DB_PASSWORD"
SECRET_VALUE = "env-secret-sentinel-83"
PASSPHRASE = "import passphrase sentinel 5"
WRONG_PASSPHRASE = "not the passphrase 99"
HOST_PATH = "/srv/shop"
DIGEST = "sha256:" + "cd" * 32

BASE_FORM = {
    "image": IMAGE,
    "name": CONTAINER_NAME,
    "pull": "never",
    "start": "on",
    "network_mode": "networks",
    "mount-0-type": "volume",
    "mount-0-source": VOLUME,
    "mount-0-target": "/data",
    "network-0-name": NETWORK,
}


class ImportHost(ImportEngine):
    """A daemon that holds the helper's image and accepts the tar written into a volume."""

    def __init__(self) -> None:
        super().__init__()
        self.add_image(IMAGE, digests=(f"busybox@{DIGEST}",))


def network_entry(name: str = NETWORK, **changes: object) -> NetworkEntry:
    """Return an archived network of the bridge driver with one address pool."""
    fields: dict[str, object] = {
        "name": name,
        "driver": "bridge",
        "pools": [IpamPoolEntry(subnet=SUBNET)],
    }
    fields.update(changes)
    return NetworkEntry(**fields)  # type: ignore[arg-type]


async def container_archive(
    *,
    form: Mapping[str, str] | None = None,
    environment: tuple[EnvironmentInput, ...] | None = None,
    networks: tuple[NetworkEntry, ...] | None = None,
    volumes: Mapping[str, Mapping[str, bytes]] | None = None,
    passphrase: str | None = PASSPHRASE,
    images: tuple[ImageReference, ...] | None = None,
) -> bytes:
    """Write a container export archive; each volume is a mapping of path to bytes."""
    held = {VOLUME: {NOTE_FILE: NOTE}} if volumes is None else dict(volumes)
    description = container_description(
        name=CONTAINER_NAME,
        form={**BASE_FORM, **(form or {})},
        environment=(
            (
                EnvironmentInput(PLAIN_NAME, False, PLAIN_VALUE),
                EnvironmentInput(SECRET_NAME, True, SECRET_VALUE),
            )
            if environment is None
            else environment
        ),
        networks=(network_entry(),) if networks is None else networks,
        images=(
            (ImageReference(reference=IMAGE, tag="1", digest=DIGEST, used_by=[CONTAINER_NAME]),)
            if images is None
            else images
        ),
        volumes=tuple(volume_manifest(name) for name in held),
    )
    sources = [FakeVolume(volume_tar(dict(content))).source() for content in held.values()]
    return await write_archive(ExportArchiveStream(prepared(description, passphrase), sources))


def with_manifest(archive: bytes, **changes: object) -> bytes:
    """Return the archive with fields of its manifest replaced and every other member kept."""
    members: list[tuple[tarfile.TarInfo, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as tar:
        for member in tar:
            source = tar.extractfile(member) if member.isreg() else None
            members.append((member, source.read() if source is not None else b""))
    rebuilt = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=rebuilt, mode="wb", mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as out,
    ):
        for member, data in members:
            if member.name == "manifest.json":
                data = json.dumps({**json.loads(data), **changes}).encode("utf-8")
                member.size = len(data)
            out.addfile(member, io.BytesIO(data) if member.isreg() else None)
    return rebuilt.getvalue()


FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "export_archive"
    / "container_format_version_1.tar.gz"
)
