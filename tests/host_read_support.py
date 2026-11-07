"""A daemon that serves the archive endpoint, and builders for projects read from a host."""

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs

from app.auth.principal import Principal
from app.self_identity import SelfIdentity
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    StackServices,
    build_access_services,
    build_host_clients,
    build_stack_services,
)
from app.services.host_files import HELPER_ROLE, HELPER_ROLE_LABEL, MOUNT_POINT
from app.services.object_inspector import EngineObjectInspector
from tests.archive_support import single_file_tar, stat_header
from tests.docker_engine.container_engine import IMAGE_ID, ContainerEngine
from tests.docker_engine.fake_engine import Reply, Scope, error_reply, split_version
from tests.stack_support import PROJECT_LABEL, SERVICE_LABEL, Rig

WORKING_DIR = "/srv/stacks/shop"
PROJECT = "shop"
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
ENVIRONMENT_FILE_LABEL = "com.docker.compose.project.environment_file"
HELPER_LABELS = {HELPER_ROLE_LABEL: HELPER_ROLE}


class ReaderEngine(ContainerEngine):
    """A daemon that serves the archive endpoint of the files a test places under the mount."""

    def __init__(self) -> None:
        super().__init__()
        self.served: dict[str, Reply] = {}

    def serve(self, relative_path: str, content: bytes | str) -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        name = relative_path.rsplit("/", 1)[-1]
        self.served[f"{MOUNT_POINT}/{relative_path}"] = Reply(
            200, single_file_tar(name, data), stat_header(name, len(data))
        )

    def helper_boxes(self) -> list[Any]:
        """Return the containers that carry the read helper's label."""
        return [b for b in self.boxes.values() if b.labels.get(HELPER_ROLE_LABEL) == HELPER_ROLE]

    async def _reply(self, scope: Scope, body: bytes) -> Reply | None:
        _, path = split_version(scope["path"])
        parts = path.strip("/").split("/")
        if scope["method"] != "GET" or parts[0] != "containers" or parts[2:] != ["archive"]:
            return await super()._reply(scope, body)
        query = {k: v[-1] for k, v in parse_qs(scope["query_string"].decode()).items()}
        if self.resolve(parts[1]) is None:
            return error_reply(404)
        return self.served.get(query["path"], error_reply(404))


def compose_labels(
    *,
    project: str = PROJECT,
    service: str = "web",
    working_dir: str = WORKING_DIR,
    config_files: str | None = None,
    environment_file: str | None = None,
) -> dict[str, str]:
    """Return the labels Compose puts on a container of a project started from files."""
    labels = {
        PROJECT_LABEL: project,
        SERVICE_LABEL: service,
        WORKING_DIR_LABEL: working_dir,
        CONFIG_FILES_LABEL: config_files or f"{working_dir}/compose.yaml",
    }
    if environment_file is not None:
        labels[ENVIRONMENT_FILE_LABEL] = environment_file
    return labels


def add_project_container(
    host: ContainerEngine,
    number: int,
    labels: Mapping[str, str],
    **fields: Any,
) -> Any:
    """Add a container of a Compose project, on an image the host holds."""
    service = labels.get(SERVICE_LABEL, "web")
    return host.add(
        f"{0xB0000 + number:064x}",
        f"{labels.get(PROJECT_LABEL, PROJECT)}-{service}-{number}",
        labels=dict(labels),
        image="nginx:1",
        image_id=IMAGE_ID,
        **fields,
    )


def services_with_identity(rig: Rig, identity: SelfIdentity) -> StackServices:
    """Build the stack services for a new request, as a process that runs in a container."""
    recorder = ActivityRecorder(rig.db, rig.clock)
    inspector = EngineObjectInspector(build_host_clients(rig.db, rig.runtime, rig.engine))
    access = build_access_services(rig.db, rig.runtime, inspector, recorder)
    return build_stack_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine,
        rig.registry.registries,
        access,
        inspector,
        recorder,
        lambda: identity,
    )


async def read(rig: Rig, actor: Principal, environment_id: int, project: str = PROJECT) -> Any:
    """Read a project's files with a fresh set of services."""
    return await rig.fresh().reader.read_compose(actor, environment_id, project)
