"""Stack archives to import, a daemon that accepts their volumes, and the import service."""

import dataclasses
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

from app.self_identity import SelfIdentitySource
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    StackServices,
    build_access_services,
    build_host_clients,
    build_stack_services,
)
from app.services.export_archive_writer import ExportArchiveStream
from app.services.export_build import ContextInput, FileInput, VariableInput
from app.services.export_manifest import ImageReference
from app.services.export_unlock import UnlockGate
from app.services.object_inspector import EngineObjectInspector
from app.services.stack_import import StackImportService
from tests.access_support import ADMIN
from tests.docker_engine.fake_engine import Reply
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.export_archive_support import (
    FILE_CONTENT,
    PASSPHRASE,
    SECRET_FILE_CONTENT,
    FakeVolume,
    prepared,
    stack_description,
    volume_manifest,
    write_archive,
)
from tests.stack_support import Rig
from tests.volume_export_support import volume_tar
from tests.volume_import_support import ImportEngine

SESSION = 77
OTHER_SESSION = 78
ADMIN_SESSION = dataclasses.replace(ADMIN, session_id=SESSION)
OTHER_ADMIN_SESSION = dataclasses.replace(ADMIN, session_id=OTHER_SESSION)
SOURCE = "shop"
DATA_VOLUME = "shop_data"
NOTE = b"stored in the volume\n"
NOTE_FILE = "notes/a.txt"
PLAIN_NAME = "PLAIN"
SECRET_NAME = "DB_PASSWORD"
SECRET_FILE = "tls.key"
SECRET_FILE_BODY = SECRET_FILE_CONTENT.decode()
PLAIN_FILE = "nginx.conf"
PLAIN_FILE_BODY = FILE_CONTENT
WRONG_PASSPHRASE = "not the passphrase 99"
STACK_FILE = """\
services:
  web:
    image: nginx:1
    volumes:
      - data:/var/lib/data
volumes:
  data: {}
"""
BIND_STACK_FILE = """\
services:
  web:
    image: nginx:1
    volumes:
      - /srv/site:/usr/share/nginx/html:ro
      - ./nginx.conf:/etc/nginx/nginx.conf
      - type: bind
        source: /srv/logs
        target: /var/log/nginx
"""
SWARM_STACK_FILE = """\
services:
  web:
    image: nginx:1
    volumes:
      - data:/var/lib/data
volumes:
  data: {}
"""


class StackImportEngine(SwarmEngine, ImportEngine):
    """A Swarm manager that also accepts the tar a helper container receives into a volume.

    A volume made through the API is stamped with the time it was made, as Docker does.
    """

    def _volume_create(self, document: dict[str, object]) -> Reply:
        name = str(document["Name"])
        if name not in self.volumes:
            self.volume_created[name] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return super()._volume_create(document)


async def build_archive(
    *,
    kind: str = "compose_stack",
    text: str = STACK_FILE,
    name: str = SOURCE,
    secrets: bool = True,
    passphrase: str | None = PASSPHRASE,
    volumes: Mapping[str, Mapping[str, bytes]] | None = None,
    contexts: tuple[ContextInput, ...] = (),
    images: tuple[ImageReference, ...] = (),
    files: tuple[FileInput, ...] | None = None,
    variables: tuple[VariableInput, ...] | None = None,
) -> bytes:
    """Write a stack export archive with the given volumes, each a mapping of path to bytes."""
    held = {DATA_VOLUME: {NOTE_FILE: NOTE}} if volumes is None else dict(volumes)
    fields: dict[str, object] = {
        "kind": kind,
        "name": name,
        "description": f"stack {name}",
        "compose_file": text,
        "contexts": contexts,
        "images": images,
        "volumes": tuple(volume_manifest(volume) for volume in held),
    }
    if files is not None:
        fields["files"] = files
    if variables is not None:
        fields["variables"] = variables
    description = stack_description(secret=secrets, **fields)  # type: ignore[arg-type]
    sources = [FakeVolume(volume_tar(dict(content))).source() for content in held.values()]
    return await write_archive(ExportArchiveStream(prepared(description, passphrase), sources))


async def parts(content: bytes) -> AsyncIterator[bytes]:
    """Yield an upload in chunks."""
    for start in range(0, len(content), 8192):
        yield content[start : start + 8192]


def build_importer(
    rig: Rig,
    directory: Path | None = None,
    *,
    gate: UnlockGate | None = None,
    self_identity: SelfIdentitySource | None = None,
) -> tuple[StackImportService, StackServices]:
    """Wire the import service the way a request would, over the rig's stack services."""
    recorder = ActivityRecorder(rig.db, rig.clock)
    inspector = EngineObjectInspector(build_host_clients(rig.db, rig.runtime, rig.engine))
    access = build_access_services(rig.db, rig.runtime, inspector, recorder)
    services = build_stack_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine,
        rig.registry.registries,
        access,
        inspector,
        recorder,
    )
    service = StackImportService(
        database=rig.database,
        authoriser=access.authoriser,
        inspector=inspector,
        queries=services.queries,
        planner=services.swarm_planner,
        compose=services.actions,
        swarm=services.swarm_actions,
        locks=rig.engine.locks,
        activity=recorder,
        clock=rig.clock,
        store=rig.engine.stack_imports,
        imported=rig.engine.imported_volumes,
        data_dir=directory or rig.data_dir,
        gate=gate,
        self_identity=self_identity,
    )
    return service, services
