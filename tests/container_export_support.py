"""A container with a secret, a plain variable, a network, a volume and a bind, for export tests."""

import json
import tarfile
from typing import Any

from app.services.export_manifest import ExportManifest, parse_manifest
from tests.access_support import container_id
from tests.docker_engine.container_engine import ContainerEngine
from tests.volume_export_support import VolumeEngine, volume_tar

IMAGE = "busybox:1"
IMAGE_DIGEST = "sha256:" + "ab" * 32
SOURCE = container_id(0xE7)
SOURCE_NAME = "shop-web"
VOLUME = "shop-data"
NETWORK = "shop-net"
BIND_SOURCE = "/srv/shop"
SECRET_NAME = "DB_PASSWORD"
SECRET_VALUE = "env-secret-sentinel-71"
PLAIN_NAME = "MODE"
PLAIN_VALUE = "plain-env-visible"
PASSPHRASE = "export passphrase sentinel 3"
NETWORK_PASSWORD = "network-option-sentinel-9"
ARCHIVE_TARGET = "/data"

type Members = dict[str, tuple[tarfile.TarInfo, bytes]]

SOURCE_BODY: dict[str, Any] = {
    "Image": IMAGE,
    "Env": [f"{SECRET_NAME}={SECRET_VALUE}", f"{PLAIN_NAME}={PLAIN_VALUE}"],
    "Cmd": ["serve"],
    "HostConfig": {
        "Binds": [f"{VOLUME}:{ARCHIVE_TARGET}", f"{BIND_SOURCE}:/host:ro"],
        "NetworkMode": NETWORK,
    },
    "NetworkingConfig": {"EndpointsConfig": {NETWORK: {}}},
}


def stock_source(host: ContainerEngine) -> None:
    """Add the image, the volume, the network and the container to the daemon."""
    host.add_image(IMAGE, digests=(f"busybox@{IMAGE_DIGEST}",))
    host.add_volume(VOLUME)
    if isinstance(host, VolumeEngine):
        host.serve_volume(VOLUME, volume_tar())
    host.add_network(
        NETWORK,
        0x77,
        "bridge",
        Internal=True,
        Labels={"team": "core", "com.docker.compose.project": "shop"},
        Options={"com.docker.network.bridge.name": "br-shop", "password": NETWORK_PASSWORD},
        IPAM={"Driver": "default", "Config": [{"Subnet": "172.30.0.0/24"}]},
    )
    host.add_configured(SOURCE, SOURCE_NAME, SOURCE_BODY)


def manifest_of(members: Members) -> ExportManifest:
    """Parse the manifest member of an unpacked archive."""
    return parse_manifest(members["manifest.json"][1])


def archive_text(members: Members) -> str:
    """Return every member of an unpacked archive as one text."""
    return json.dumps(
        {name: data.decode("utf-8", "replace") for name, (_, data) in members.items()}
    )
