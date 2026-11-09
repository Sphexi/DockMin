"""A stack with every kind of input, and a daemon holding its image and volume, for export tests."""

from typing import Literal

from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.clocked import Clock
from app.db.repositories.stack import StackRepository, StoredStack, VariableWrite
from app.db.repositories.stack_build_context import ContextWrite
from app.services.build_context_archive import inspect_context
from app.services.stack_deploy import file_digest
from tests.access_support import ENV_A
from tests.archive_support import Member, build_tar
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.volume_export_support import VolumeEngine, volume_tar

STACK = "shop"
VOLUME = "shop-data"
IMAGE = "busybox:1"
WEB_IMAGE = "nginx:1.27"
WEB_DIGEST = "sha256:" + "ab" * 32
PLAIN_NAME = "PLAIN"
PLAIN_VALUE = "plain-visible-value"
SECRET_NAME = "DB_PASSWORD"
SECRET_VALUE = "sentinel-secret-value-5521"
PLAIN_FILE = "nginx.conf"
PLAIN_FILE_BODY = "worker_processes 1;\n"
SECRET_FILE = "tls.key"
SECRET_FILE_BODY = "sentinel-secret-file-body-8841\n"
CONTEXT = "web"
BIND_PATH = "./site"
PASSPHRASE = "sentinel passphrase 7731"
COMPOSE_LABEL = "com.docker.compose.project"
SWARM_LABEL = "com.docker.stack.namespace"
COMPOSE_TEXT = f"""\
services:
  web:
    image: {WEB_IMAGE}
    volumes:
      - {VOLUME}:/data
      - {BIND_PATH}:/usr/share/nginx/html
volumes:
  {VOLUME}: {{}}
"""
PROJECT_DIRECTORY = "c" * 32


class SwarmVolumeEngine(SwarmEngine, VolumeEngine):
    """A Swarm manager that also serves the archive of a volume a helper mounts."""


def stocked[E: VolumeEngine](host: E, *, label: str = COMPOSE_LABEL, project: str = STACK) -> E:
    """Give a daemon the helper image, the web image with a digest, and the stack's volume."""
    host.add_image(IMAGE)
    host.add_image(WEB_IMAGE, "sha256:" + "d" * 64, digests=(f"nginx@{WEB_DIGEST}",))
    host.add_volume(VOLUME, labels={label: project})
    host.serve_volume(VOLUME, volume_tar())
    return host


async def seed_export_stack(
    db: Session,
    vault: Vault,
    clock: Clock,
    *,
    kind: Literal["compose", "swarm"] = "compose",
    name: str = STACK,
    environment_id: int = ENV_A,
    text: str = COMPOSE_TEXT,
    secrets: bool = True,
) -> StoredStack:
    """Insert a stack holding plain and secret variables and files and one build context."""
    repository = StackRepository(db, vault, clock)
    stack_id = await repository.insert(
        environment_id=environment_id,
        name=name,
        kind=kind,
        origin="created",
        project_directory=PROJECT_DIRECTORY if kind == "compose" else "d" * 32,
        compose_file=text,
        compose_file_digest=file_digest(text),
        created_by_user_id=None,
    )
    variables = [VariableWrite(PLAIN_NAME, False, PLAIN_VALUE)]
    if secrets:
        variables.append(VariableWrite(SECRET_NAME, True, SECRET_VALUE))
    await repository.replace_variables(stack_id, variables)
    await repository.files.put_file(stack_id, PLAIN_FILE, content=PLAIN_FILE_BODY, secret=False)
    if secrets:
        await repository.files.put_file(
            stack_id, SECRET_FILE, content=SECRET_FILE_BODY, secret=True
        )
    archive = inspect_context(build_tar(Member("Dockerfile", b"FROM busybox\n")))
    await repository.contexts.put(
        stack_id,
        CONTEXT,
        ContextWrite(
            content=archive.content,
            content_digest=archive.content_digest,
            compressed=archive.compressed,
            member_count=archive.member_count,
            extracted_size=archive.extracted_size,
        ),
    )
    stored = await repository.get_by_id(stack_id)
    assert stored is not None
    return stored
