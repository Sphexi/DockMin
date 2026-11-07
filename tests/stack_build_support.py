"""Stacks that build their images: stored contexts, Dockerfiles and the stub's recorded runs."""

from collections.abc import Mapping
from pathlib import Path

from app.db.repositories.stack import StackRepository
from app.db.repositories.stack_build_context import ContextWrite
from app.services.build_context_archive import inspect_context
from tests.access_support import ENV_A
from tests.archive_support import Member, build_tar
from tests.image_support import REFERENCE
from tests.stack_support import Rig, SeededStack, seed_stack_row

STACK = "shop"
CONTEXT = "web"
TAG = f"{STACK}-web"
BUILD_FILE = f"services:\n  web:\n    build: ./{CONTEXT}\n"
DOCKERFILE = f"FROM {REFERENCE}\nRUN echo built\n"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
DIGEST_LABEL = "dockmin.build.digest"


async def store_context(
    repository: StackRepository, stack_id: int, name: str, files: Mapping[str, str]
) -> str:
    """Store an archive of the files as the stack's context and return its content digest."""
    archive = inspect_context(
        build_tar(*(Member(path, content.encode("utf-8")) for path, content in files.items()))
    )
    stored = await repository.contexts.put(
        stack_id,
        name,
        ContextWrite(
            content=archive.content,
            content_digest=archive.content_digest,
            compressed=archive.compressed,
            member_count=archive.member_count,
            extracted_size=archive.extracted_size,
        ),
    )
    return stored.content_digest


async def add_context(rig: Rig, stack_id: int, name: str, files: Mapping[str, str]) -> str:
    """Store a context on a stack of the stack rig."""
    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    return await store_context(repository, stack_id, name, files)


async def seed_build_stack(
    rig: Rig,
    *,
    text: str = BUILD_FILE,
    dockerfile: str = DOCKERFILE,
    owner_user_id: int | None = None,
    name: str = STACK,
    environment_id: int = ENV_A,
) -> SeededStack:
    """Insert a stack whose `web` service builds from a stored context holding the Dockerfile."""
    seeded = await seed_stack_row(rig, environment_id, name, text, owner_user_id=owner_user_id)
    await add_context(rig, seeded.stack_id, CONTEXT, {"Dockerfile": dockerfile})
    return seeded


def stub_argv(rig: Rig) -> list[str]:
    """Return the argument list of the stub's last run."""
    assert rig.buildx is not None
    return rig.buildx.recorded("argv").splitlines()


def runs_left_in(directory: Path) -> list[str]:
    """Return what is left in a work directory base."""
    return sorted(entry.name for entry in directory.iterdir())
