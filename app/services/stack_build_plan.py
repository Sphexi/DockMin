"""Planning of the image builds a Compose deployment runs: tags, digests, bases and reuse."""

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, cast

from yaml.nodes import MappingNode, Node, ScalarNode

from app.auth.errors import ValidationFailed
from app.db.repositories.stack_build_context import StackContextRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, ResponseTooLarge
from app.docker_engine.errors import NotFound as EngineNotFound
from app.docker_engine.models import ImageInspect
from app.services.access import COMPOSE_PROJECT_LABEL, canonical_stack_key
from app.services.build_context_archive import ContextRefused, read_context_file, run_limited
from app.services.dockerfile_check import MAX_DOCKERFILE_BYTES, DockerfileRefusal, check_dockerfile
from app.services.image_bindings import parse_pull_reference
from app.services.object_inspector import unavailable_from
from app.services.stack_builds import BuildAdmission
from app.services.stack_document import ComposeDocumentRejected, compose_nodes, path_part

__all__ = [
    "BUILD_DIGEST_LABEL",
    "MAX_BUILD_SERVICES",
    "SERVICE_LABEL",
    "BuildInputs",
    "BuildPlan",
    "BuildPlans",
    "ContextSource",
    "StoredContexts",
    "build_digest",
    "builds_digest",
    "plan_builds",
    "services_with_build",
]

BUILD_DIGEST_LABEL: Final = "dockmin.build.digest"
SERVICE_LABEL: Final = "com.docker.compose.service"
MAX_BUILD_SERVICES: Final = 16

TAG_IN_USE: Final = "An image with this name exists on the host and does not belong to this stack."
BUILT_IMAGE_MISSING: Final = (
    "The image built for this service is not on the host, or was built from other inputs. "
    "Deploy the stack from its page to build it again."
)

_TAG: Final = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")
_PROJECT_NAME: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MAX_TAG_CHARS: Final = 255

_REASON_ORDER: Final = ("refused_construct", "invalid", "tag_in_use", "built_image_missing")


class ContextSource(Protocol):
    """Where a stack's build contexts are read from: their digests and the files inside them."""

    async def digests(self) -> dict[str, str]:
        """Return each context's content digest by name."""
        ...

    async def read_file(self, name: str, path: str) -> bytes | None:
        """Return a file of a context, or None when the context or the file is absent."""
        ...


@dataclass(frozen=True, slots=True)
class StoredContexts:
    """A stack's stored build contexts, read when a plan needs their names or a Dockerfile."""

    repository: StackContextRepository
    stack_id: int

    async def digests(self) -> dict[str, str]:
        """Return each context's content digest by its stored name."""
        return {
            item.name: item.content_digest for item in await self.repository.contexts(self.stack_id)
        }

    async def read_file(self, name: str, path: str) -> bytes | None:
        """Return a file of a stored context, or None when the context or the file is absent.

        ContextRefused when the stored archive no longer passes the checks.
        """
        archive = await self.repository.archive(self.stack_id, name)
        if archive is None:
            return None
        return await run_limited(
            read_context_file,
            archive.content,
            path,
            digest=archive.content_digest,
            max_bytes=MAX_DOCKERFILE_BYTES,
        )


@dataclass(frozen=True, slots=True)
class BuildInputs:
    """What a validation needs to admit and plan builds: the contexts and what may run."""

    contexts: ContextSource
    buildx_available: bool = True
    rebuild: bool = False


@dataclass(frozen=True, slots=True)
class BuildPlan:
    """One service's build: the tag it moves, its digest, the bases it needs and whether it runs."""

    service: str
    tag: str
    context: str
    dockerfile: str
    target: str | None
    no_cache: bool
    args: Mapping[str, str] = field(repr=False)
    labels: Mapping[str, str]
    digest: str
    bases: tuple[str, ...]
    reuse: bool
    image_id: str | None = None


@dataclass(frozen=True, slots=True)
class BuildPlans:
    """The plans of a model's build services, or the refusals by Compose path and their reason."""

    plans: tuple[BuildPlan, ...] = ()
    refusals: Mapping[str, str] = field(default_factory=dict)
    reason: str = "refused_construct"

    @property
    def builds_digest(self) -> str:
        """Return a digest of what a review names: each service, tag, build digest and bases."""
        return builds_digest(self.plans)


def builds_digest(plans: Sequence[BuildPlan]) -> str:
    """Return the SHA-256 of the services, tags, build digests and bases of the plans."""
    if not plans:
        return ""
    entries = sorted([p.service, p.tag, p.digest, sorted(p.bases)] for p in plans)
    canonical = json.dumps(entries, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def build_digest(
    context_digest: str,
    dockerfile: str,
    args: Mapping[str, str],
    target: str | None,
    labels: Mapping[str, str],
) -> str:
    """Return the SHA-256 of everything that decides what a build produces."""
    canonical = json.dumps(
        {
            "context": context_digest,
            "dockerfile": dockerfile,
            "args": sorted(args.items()),
            "target": target,
            "labels": sorted(labels.items()),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


async def services_with_build(text: str) -> tuple[str, ...]:
    """Return the services of the file that have a `build` key; none when it cannot be read."""
    try:
        root = await asyncio.to_thread(compose_nodes, text)
    except ComposeDocumentRejected:
        return ()
    services = _entry(root, "services")
    if not isinstance(services, MappingNode):
        return ()
    return tuple(
        key.value
        for key, service in services.value
        if isinstance(key, ScalarNode) and _entry(service, "build") is not None
    )


def _entry(node: Node, key: str) -> Node | None:
    if not isinstance(node, MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return cast(Node, value_node)
    return None


async def plan_builds(
    services: Mapping[str, Mapping[str, Any]],
    *,
    project: str,
    admission: BuildAdmission,
    contexts: ContextSource,
    client: EngineClient,
    rebuild: bool,
    allow_build: bool,
) -> BuildPlans:
    """Plan the builds of every service of a resolved model that has a `build` section.

    Reads each Dockerfile from its stored context and checks it, and inspects the image at each
    tag: one without this project's label is `tag_in_use`, one whose build digest label equals the
    plan's is reused unless `rebuild`. With `allow_build` false a service that is not reused is
    refused (`built_image_missing`). EnvironmentUnavailable when the host cannot be asked.
    """
    digests = await contexts.digests()
    project_key = canonical_stack_key(project)
    refusals: dict[str, str] = {}
    reasons: set[str] = set()
    plans: list[BuildPlan] = []
    dockerfiles: dict[tuple[str, str], bytes | DockerfileRefusal | None] = {}
    named = [name for name, service in services.items() if "build" in service]
    if len(named) > MAX_BUILD_SERVICES:
        return BuildPlans(
            refusals={"services": f"A stack can build at most {MAX_BUILD_SERVICES} services."},
            reason="invalid",
        )
    for name in named:
        base = f"services.{path_part(name)}"
        review = admission.review(base, services[name])
        if review is None or review.spec is None:
            refusals.update({} if review is None else review.refusals)
            reasons.add("refused_construct")
            continue
        spec = review.spec
        where = f"{base}.build"
        tag = f"{project}-{name}".lower()
        if len(tag) > _MAX_TAG_CHARS or _TAG.fullmatch(tag) is None:
            refusals[where] = "The service name cannot make an image name."
            reasons.add("invalid")
            continue
        content = await _dockerfile(contexts, dockerfiles, spec.context, spec.dockerfile)
        if not isinstance(content, bytes):
            refusals[f"{where}.dockerfile"] = _dockerfile_problem(content, spec.dockerfile)
            reasons.add("refused_construct")
            continue
        references = check_dockerfile(content, spec.args)
        if isinstance(references, DockerfileRefusal):
            refusals[f"{where}.dockerfile"] = _line_message(references)
            reasons.add("refused_construct")
            continue
        invalid = _invalid_reference(references.images)
        if invalid is not None:
            refusals[f"{where}.dockerfile"] = f"`{invalid}` is not a valid image reference."
            reasons.add("invalid")
            continue
        digest = build_digest(
            digests[spec.context], spec.dockerfile, spec.args, spec.target, spec.labels
        )
        existing = await _inspect(client, tag)
        reuse = False
        image_id: str | None = None
        if existing is not None:
            labels = _labels(existing)
            owner = labels.get(COMPOSE_PROJECT_LABEL)
            if not owner or canonical_stack_key(owner) != project_key:
                refusals[where] = _tag_in_use(owner)
                reasons.add("tag_in_use")
                continue
            image_id = existing.id
            reuse = not rebuild and labels.get(BUILD_DIGEST_LABEL) == digest
        if not reuse and not allow_build:
            refusals[where] = BUILT_IMAGE_MISSING
            reasons.add("built_image_missing")
            continue
        plans.append(
            BuildPlan(
                service=name,
                tag=tag,
                context=spec.context,
                dockerfile=spec.dockerfile,
                target=spec.target,
                no_cache=spec.no_cache,
                args=spec.args,
                labels=spec.labels,
                digest=digest,
                bases=references.images,
                reuse=reuse,
                image_id=image_id,
            )
        )
    if refusals:
        reason = min(reasons or {"refused_construct"}, key=_REASON_ORDER.index)
        return BuildPlans(refusals=refusals, reason=reason)
    return BuildPlans(plans=tuple(plans))


async def _dockerfile(
    contexts: ContextSource,
    cache: dict[tuple[str, str], bytes | DockerfileRefusal | None],
    context: str,
    path: str,
) -> bytes | DockerfileRefusal | None:
    key = (context, path)
    if key not in cache:
        try:
            cache[key] = await contexts.read_file(context, path)
        except ContextRefused:
            cache[key] = DockerfileRefusal(0, "the stored build context can no longer be read.")
    return cache[key]


def _tag_in_use(owner: str | None) -> str:
    if owner and _PROJECT_NAME.fullmatch(owner) is not None:
        return f"{TAG_IN_USE} It belongs to the Compose project {owner}."
    return TAG_IN_USE


def _dockerfile_problem(found: DockerfileRefusal | None, path: str) -> str:
    if isinstance(found, DockerfileRefusal):
        return _line_message(found)
    return f"`{path}` is not a file in the build context."


def _line_message(refusal: DockerfileRefusal) -> str:
    if refusal.line:
        return f"Dockerfile line {refusal.line}: {refusal.message}"
    return f"Dockerfile: {refusal.message}"


def _invalid_reference(references: Sequence[str]) -> str | None:
    for reference in references:
        try:
            parse_pull_reference(reference)
        except ValidationFailed:
            return reference[:128]
    return None


async def _inspect(client: EngineClient, tag: str) -> ImageInspect | None:
    try:
        return await client.images.inspect(tag)
    except EngineNotFound:
        return None
    except ResponseTooLarge:
        return ImageInspect.model_validate({"Id": "", "Config": {"Labels": {}}})
    except EngineError as error:
        raise unavailable_from(error) from None


def _labels(image: ImageInspect) -> Mapping[str, str]:
    labels = image.config.get("Labels")
    if not isinstance(labels, dict):
        return {}
    return {k: v for k, v in labels.items() if isinstance(k, str) and isinstance(v, str)}
