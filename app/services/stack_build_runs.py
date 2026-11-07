"""Runs the image builds of a Compose deployment: base checks, then one buildx run per service."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.compose_cli.buildx import BuildEvent, BuildFailed, BuildxCli, BuildxUnavailable
from app.compose_cli.buildx import BuildSpec as BuildxSpec
from app.compose_cli.cli import ComposeCli
from app.compose_cli.process import uninterrupted
from app.compose_cli.target import ComposeTarget
from app.compose_cli.workdir import create_invocation_dir, remove_invocation_dir
from app.db.repositories.stack import StackRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, ResponseTooLarge
from app.docker_engine.errors import NotFound as EngineNotFound
from app.logging import get_logger
from app.services.access import COMPOSE_PROJECT_LABEL
from app.services.object_inspector import unavailable_from
from app.services.stack_build_plan import BUILD_DIGEST_LABEL, SERVICE_LABEL, BuildPlan
from app.services.stack_contexts import extract_stored_context
from app.services.stack_deploy import StackOperationFailed

__all__ = [
    "BuiltImage",
    "build_seconds",
    "built_detail",
    "check_bases",
    "run_builds",
]

logger = get_logger(__name__)

CONTEXT_DIRECTORY: Final = "context"
MAX_DETAIL_CHARS: Final = 480
_PRIVATE_DIRECTORY_MODE: Final = 0o700
_DIGEST_SHOWN: Final = 12

type BuildSinkFactory = Callable[[str, str], Callable[[BuildEvent], None]]
type RepositoryScope = Callable[[], AbstractAsyncContextManager[StackRepository]]


@dataclass(frozen=True, slots=True)
class BuiltImage:
    """The image a service runs from after a deployment: built now, or reused from before."""

    service: str
    tag: str
    image_id: str
    digest: str
    reused: bool


def build_seconds(plans: Sequence[BuildPlan], limit: float) -> float:
    """Return the time a deployment may add for the builds it will run."""
    return sum(limit for plan in plans if not plan.reuse)


def built_detail(images: Sequence[BuiltImage]) -> str:
    """Return the audit text of the images: service, image id, build digest, built or reused."""
    entries = [
        ":".join(
            (
                image.service,
                image.image_id.removeprefix("sha256:")[:_DIGEST_SHOWN],
                image.digest[:_DIGEST_SHOWN],
                "reused" if image.reused else "built",
            )
        )
        for image in images
    ]
    text = ""
    for position, entry in enumerate(entries):
        candidate = f"{text},{entry}" if text else entry
        if len(candidate) > MAX_DETAIL_CHARS:
            return f"{text},+{len(entries) - position}"
        text = candidate
    return text


async def check_bases(client: EngineClient, plans: Sequence[BuildPlan]) -> dict[str, str]:
    """Refuse unless every base of every build is on the host and runs no `ONBUILD` trigger.

    Returns the image id of each base by reference, for `run_builds` to hold the builds to.
    StackOperationFailed (`base_missing` or `base_onbuild`) naming the first base that fails.
    """
    recorded: dict[str, str] = {}
    for plan in plans:
        if plan.reuse:
            continue
        for reference in plan.bases:
            if reference in recorded:
                continue
            try:
                image = await client.images.inspect(reference)
            except EngineNotFound:
                raise StackOperationFailed(
                    "base_missing",
                    f"The base image {reference} is not on the host.",
                    progress=f"Base {reference}",
                ) from None
            except ResponseTooLarge:
                raise StackOperationFailed(
                    "base_missing",
                    f"The base image {reference} could not be inspected.",
                    progress=f"Base {reference}",
                ) from None
            except EngineError as error:
                raise unavailable_from(error) from None
            if image.config.get("OnBuild"):
                raise StackOperationFailed(
                    "base_onbuild",
                    f"The base image {reference} has ONBUILD triggers, which would run steps "
                    "that were not checked.",
                    progress=f"Base {reference}",
                )
            recorded[reference] = image.id
    return recorded


async def run_builds(
    plans: Sequence[BuildPlan],
    built: list[BuiltImage],
    *,
    project: str,
    bases: Mapping[str, str],
    client: EngineClient,
    cli: ComposeCli,
    buildx: BuildxCli,
    target: ComposeTarget,
    repository: RepositoryScope,
    stack_id: int,
    scrub: Callable[[str], str],
    deadline: float,
    on_start: Callable[[], None],
    progress: BuildSinkFactory | None = None,
) -> None:
    """Build each service that is not reused, one at a time, and list every service's image.

    `bases` are the image ids `check_bases` recorded; a base whose id differs once its build has
    finished fails the run. `repository` opens a database session for the moment a context is
    read. `on_start` runs before the first build that changes the host. StackOperationFailed
    (`build_failed`, `buildx_unavailable`, `base_changed`) when a build fails, with the scrubbed
    build output; the services finished before it stay in `built`.
    """
    started = False
    for plan in plans:
        if not plan.reuse:
            if not started:
                on_start()
                started = True
            await _build(
                plan,
                project=project,
                cli=cli,
                buildx=buildx,
                target=target,
                repository=repository,
                stack_id=stack_id,
                scrub=scrub,
                deadline=deadline,
                on_event=None if progress is None else progress(plan.service, plan.tag),
            )
            await _require_unchanged_bases(client, plan, bases)
        built.append(await _image_of(client, plan))


async def _build(
    plan: BuildPlan,
    *,
    project: str,
    cli: ComposeCli,
    buildx: BuildxCli,
    target: ComposeTarget,
    repository: RepositoryScope,
    stack_id: int,
    scrub: Callable[[str], str],
    deadline: float,
    on_event: Callable[[BuildEvent], None] | None,
) -> None:
    workdir = await asyncio.to_thread(create_invocation_dir, cli.work_base)
    try:
        context = workdir / CONTEXT_DIRECTORY
        await asyncio.to_thread(_make_context_directory, context)
        async with repository() as stacks:
            await extract_stored_context(stacks, stack_id, plan.context, context)
        spec = BuildxSpec(
            tag=plan.tag,
            context_dir=context,
            dockerfile=plan.dockerfile,
            target=plan.target,
            no_cache=plan.no_cache,
            args=plan.args,
            labels={
                **plan.labels,
                COMPOSE_PROJECT_LABEL: project,
                SERVICE_LABEL: plan.service,
                BUILD_DIGEST_LABEL: plan.digest,
            },
        )
        try:
            await buildx.build(
                target,
                workdir,
                spec,
                scrub=lambda text: scrub(cli.strip_data_dir(text)),
                on_event=on_event,
                deadline=deadline,
            )
        except BuildxUnavailable:
            raise StackOperationFailed(
                "buildx_unavailable", "Docker buildx is not available on this server."
            ) from None
        except BuildFailed as failed:
            raise StackOperationFailed(
                "build_failed",
                str(failed),
                exit_code=failed.exit_code,
                stderr="\n".join(part for part in (failed.log, failed.stderr) if part),
                progress=f"Build {plan.service}",
            ) from None
    finally:
        await uninterrupted(
            asyncio.to_thread(remove_invocation_dir, workdir), event="buildx_cleanup_failed"
        )


def _make_context_directory(path: Path) -> None:
    path.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    path.chmod(_PRIVATE_DIRECTORY_MODE)


async def _image_of(client: EngineClient, plan: BuildPlan) -> BuiltImage:
    try:
        image = await client.images.inspect(plan.tag)
    except EngineNotFound:
        raise StackOperationFailed(
            "build_failed",
            f"The image {plan.tag} is not on the host after its build.",
            progress=f"Build {plan.service}",
        ) from None
    except ResponseTooLarge:
        logger.warning("stack_build_image_too_large_to_inspect", extra={"tag": plan.tag})
        return BuiltImage(plan.service, plan.tag, "", plan.digest, plan.reuse)
    except EngineError as error:
        raise unavailable_from(error) from None
    return BuiltImage(plan.service, plan.tag, image.id, plan.digest, plan.reuse)


async def _require_unchanged_bases(
    client: EngineClient, plan: BuildPlan, recorded: Mapping[str, str]
) -> None:
    for reference in plan.bases:
        expected = recorded.get(reference)
        if expected is None:
            continue
        try:
            image = await client.images.inspect(reference)
        except (EngineNotFound, ResponseTooLarge):
            current: str | None = None
        except EngineError as error:
            raise unavailable_from(error) from None
        else:
            current = image.id
        if current != expected:
            raise StackOperationFailed(
                "base_changed",
                f"The base image {reference} changed while {plan.service} was building.",
                progress=f"Base {reference}",
            )
