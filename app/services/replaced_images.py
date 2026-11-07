"""Records of images an update replaced, and the removal of those whose grace period ended."""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from app.auth.errors import PermissionDenied, ProductError, ResourceNotFound
from app.db.engine import DatabaseError, Session
from app.db.repositories.automation_rule import AutomationRule, AutomationRuleRepository
from app.db.repositories.clocked import Clock, format_timestamp, parse_timestamp
from app.db.repositories.errors import RepositoryError
from app.db.repositories.replaced_image import (
    ReplacedImage,
    ReplacedImageRepository,
    ReplacedTargetType,
    WaitingImages,
)
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, NotFound
from app.docker_engine.models import ContainerSummary
from app.logging import get_logger
from app.scheduler.model import RunSkipped
from app.services.access import canonical_stack_key
from app.services.acting import AutomationAuthority
from app.services.containers import short_id
from app.services.image_bindings import bare_image_id, daemon_references
from app.services.image_listing import read_containers
from app.services.image_queries import inspect_image
from app.services.image_views import (
    ImageChangedSinceReview,
    ImageGone,
    ImageHasDependents,
    ImageInUse,
    ImageTooLarge,
    references_digest,
)
from app.services.images import ImageActionService
from app.services.object_inspector import EnvironmentUnavailable, HostClients, unavailable_from
from app.services.object_locks import ObjectBusy, ObjectKey
from app.services.stack_deploy import in_project
from app.services.stack_queries import read_project_containers
from app.services.swarm import swarm_read

logger = get_logger(__name__)

MAX_EXAMINED: Final = 20
MAX_PRUNED: Final = 10
MAX_RECORD_AGE: Final = timedelta(days=30)
UPDATE_PRUNE: Final = "update_prune"

type Suppressed = Callable[[ObjectKey], Awaitable[datetime | None]]
type StopReason = Literal["rule", "owner", "unavailable"]
type _Verdict = Literal["pruned", "would_prune", "waiting", "kept", "failed", "gone"]


@dataclass(frozen=True, slots=True)
class PruneReport:
    """What one prune phase did or, in a dry run, would do.

    `stopped` names why the phase ended before every due record was examined: the rule changed
    or was deleted, its owner may no longer remove images, or the host could not be read.
    """

    pruned: int = 0
    waiting: int = 0
    kept: int = 0
    failed: int = 0
    would_prune: int = 0
    would_prune_images: tuple[str, ...] = ()
    stopped: StopReason | None = None


@dataclass(frozen=True, slots=True)
class WaitingSummary:
    """How many images a rule is holding and when the oldest becomes due."""

    count: int
    oldest_due_at: str | None


@dataclass(frozen=True, slots=True)
class _Host:
    client: EngineClient
    containers: Sequence[ContainerSummary]
    service_digests: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Result:
    verdict: _Verdict
    stop: StopReason | None = None


def summarise_waiting(found: WaitingImages, grace: timedelta) -> WaitingSummary:
    """Turn a rule's record count and oldest write time into the time the oldest is due."""
    if found.oldest_replaced_at is None:
        return WaitingSummary(found.count, None)
    due = parse_timestamp(found.oldest_replaced_at) + grace
    return WaitingSummary(found.count, format_timestamp(due))


def _no_poke(_environment_id: int) -> None:
    return None


def _digests_of(references: Sequence[str]) -> frozenset[str]:
    return frozenset(text.rsplit("@", 1)[1] for text in references if "@" in text)


class ReplacedImages:
    """Writes and reads the replaced-image records and prunes the due ones as an automation run.

    Callers pass the run's own session. Record writes are single statements that commit on their
    own, after the audited update they follow; a failed write is logged and costs only the
    removal it would have led to.
    """

    def __init__(
        self,
        *,
        db: Session,
        hosts: HostClients,
        images: ImageActionService,
        clock: Clock,
        poke: Callable[[int], None] = _no_poke,
    ) -> None:
        self._db = db
        self._hosts = hosts
        self._images = images
        self._clock = clock
        self._poke = poke

    def _records(self) -> ReplacedImageRepository:
        return ReplacedImageRepository(self._db, self._clock)

    @asynccontextmanager
    async def _bookkeeping(self) -> AsyncIterator[None]:
        if self._db.in_transaction:
            yield
            return
        async with self._db.transaction():
            yield

    async def record_container(
        self,
        rule: AutomationRule,
        environment_id: int,
        *,
        old_image_id: str,
        new_image_id: str,
        container_id: str,
    ) -> bool:
        """Record the image a container update replaced; report whether a record was written.

        Nothing is written unless both ids are image ids and they differ.
        """
        old, new = bare_image_id(old_image_id), bare_image_id(new_image_id)
        if old is None or new is None or old == new:
            return False
        return await self._write(rule, environment_id, [old], "container", container_id)

    async def stack_images(self, environment_id: int, stack_name: str) -> frozenset[str] | None:
        """Return the image ids the stack's containers run now, or None when they cannot be read.

        Call before a redeploy and pass the result to `record_stack` after it.
        """
        try:
            client = await self._hosts.client(environment_id)
            containers = await read_project_containers(client)
        except ProductError as error:
            logger.warning(
                "stack_images_unreadable",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            return None
        key = canonical_stack_key(stack_name)
        found = (bare_image_id(c.image_id) for c in containers if in_project(c.labels, key))
        return frozenset(image for image in found if image is not None)

    async def record_stack(
        self,
        rule: AutomationRule,
        environment_id: int,
        stack_name: str,
        before: frozenset[str] | None,
    ) -> int:
        """Record each image the stack ran before a redeploy and no container of it runs now.

        `before` is what `stack_images` returned; None records nothing. Returns the number of
        records written.
        """
        if not before:
            return 0
        after = await self.stack_images(environment_id, stack_name)
        if after is None:
            return 0
        replaced = sorted(before - after)
        if not replaced:
            return 0
        written = await self._write(rule, environment_id, replaced, "stack", stack_name)
        return len(replaced) if written else 0

    async def _write(
        self,
        rule: AutomationRule,
        environment_id: int,
        image_ids: Sequence[str],
        target_type: ReplacedTargetType,
        target_id: str,
    ) -> bool:
        try:
            async with self._bookkeeping():
                records = self._records()
                for image_id in image_ids:
                    await records.add_replaced(
                        rule, environment_id, image_id, target_type, target_id
                    )
        except (RepositoryError, DatabaseError) as error:
            logger.warning(
                "replaced_image_record_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            return False
        return True

    async def prune_due(
        self,
        authority: AutomationAuthority,
        rule: AutomationRule,
        *,
        grace: timedelta,
        suppressed: Suppressed,
        dry_run: bool = False,
    ) -> PruneReport:
        """Remove the rule's replaced images whose grace period ended and that nothing uses.

        Examines the oldest 20 due records and removes at most 10, each through an audited
        `ImageActionService.remove` under the authority. The caller runs this only for a rule
        whose prune parameter is on. A dry run reads the same state, changes nothing, and
        reports what a real run would remove. Raises ValueError for a rule without an
        environment.
        """
        environment_id = rule.environment_id
        if environment_id is None:
            raise ValueError("a rule that prunes images has an environment")
        records = self._records()
        if not dry_run:
            async with self._bookkeeping():
                await records.drop_stale(rule)
        now = self._clock()
        due = await records.due(rule, environment_id, now, grace, MAX_EXAMINED)
        if not due:
            return PruneReport()
        try:
            host = await self._read_host(environment_id)
        except ProductError as error:
            logger.warning(
                "image_prune_host_unreadable",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            return PruneReport(stopped="unavailable")
        oldest_kept = now - max(MAX_RECORD_AGE, grace + timedelta(days=1))
        tally: dict[_Verdict, int] = {
            "pruned": 0,
            "would_prune": 0,
            "waiting": 0,
            "kept": 0,
            "failed": 0,
            "gone": 0,
        }
        listed: list[str] = []
        stopped: StopReason | None = None
        for record in due:
            if tally["pruned"] + tally["would_prune"] >= MAX_PRUNED:
                break
            stopped = await self._stop_reason(authority, rule, environment_id, record)
            if stopped is not None:
                break
            result = await self._examine(
                authority, rule, record, host, suppressed, oldest_kept, dry_run
            )
            tally[result.verdict] += 1
            if result.verdict == "would_prune":
                listed.append(short_id(record.image_id))
            if result.stop is not None:
                stopped = result.stop
                break
        return PruneReport(
            pruned=tally["pruned"],
            waiting=tally["waiting"],
            kept=tally["kept"],
            failed=tally["failed"],
            would_prune=tally["would_prune"],
            would_prune_images=tuple(listed),
            stopped=stopped,
        )

    async def _read_host(self, environment_id: int) -> _Host:
        client = await self._hosts.client(environment_id)
        containers = await read_containers(client)
        info = await swarm_read(client.system.info(), environment_id, self._poke)
        digests: frozenset[str] = frozenset()
        if info.swarm.local_node_state == "active" and info.swarm.control_available:
            services = await swarm_read(client.swarm_services.list(), environment_id, self._poke)
            digests = _digests_of(
                [
                    image
                    for service in services
                    for spec in (service.spec, service.previous_spec)
                    if spec is not None
                    and isinstance(
                        image := spec.task_template.get("ContainerSpec", {}).get("Image"), str
                    )
                ]
            )
        return _Host(client, containers, digests)

    async def _stop_reason(
        self,
        authority: AutomationAuthority,
        rule: AutomationRule,
        environment_id: int,
        record: ReplacedImage,
    ) -> StopReason | None:
        current = await AutomationRuleRepository(self._db, self._clock).get(rule.id)
        if current is None or current.created_at != rule.created_at:
            return "rule"
        if current.updated_at != rule.updated_at:
            return "rule"
        try:
            await authority.authorise(ObjectKey(environment_id, "image", record.image_id))
        except (PermissionDenied, ResourceNotFound):
            return "owner"
        return None if authority.owner_is_administrator else "owner"

    async def _examine(
        self,
        authority: AutomationAuthority,
        rule: AutomationRule,
        record: ReplacedImage,
        host: _Host,
        suppressed: Suppressed,
        oldest_kept: datetime,
        dry_run: bool,
    ) -> _Result:
        environment_id = record.environment_id
        if await suppressed(ObjectKey(environment_id, "image", record.image_id)) is not None:
            return _Result("waiting")
        try:
            inspected = await inspect_image(host.client, record.image_id)
        except ResourceNotFound:
            await self._drop(rule, record, dry_run)
            return _Result("gone")
        except ImageTooLarge:
            return _Result("failed")
        except EnvironmentUnavailable:
            return _Result("waiting", stop="unavailable")
        if inspected.repo_tags:
            await self._drop(rule, record, dry_run)
            return _Result("kept")
        try:
            trusted = await self._trusted(host, record)
        except EnvironmentUnavailable:
            return _Result("waiting", stop="unavailable")
        if not trusted:
            return _Result("waiting")
        if parse_timestamp(record.replaced_at) < oldest_kept:
            await self._drop(rule, record, dry_run)
            return _Result("kept")
        digests = _digests_of(inspected.repo_digests)
        if any(bare_image_id(c.image_id) == record.image_id for c in host.containers):
            return _Result("kept")
        if digests & host.service_digests:
            return _Result("kept")
        if dry_run:
            return _Result("would_prune")
        return await self._remove(
            authority, rule, record, inspected.repo_tags, inspected.repo_digests
        )

    async def _trusted(self, host: _Host, record: ReplacedImage) -> bool:
        """Report whether the update that replaced the image looks settled."""
        if record.target_type == "stack":
            key = canonical_stack_key(record.target_id)
            return all(c.state == "running" for c in host.containers if in_project(c.labels, key))
        replacement = next((c for c in host.containers if c.id == record.target_id), None)
        if replacement is None:
            return True
        if replacement.state != "running":
            return False
        try:
            inspected = await host.client.containers.inspect(record.target_id)
        except NotFound:
            return True
        except EngineError as error:
            raise unavailable_from(error) from None
        health = inspected.state.health
        return health is None or health.status != "unhealthy"

    async def _remove(
        self,
        authority: AutomationAuthority,
        rule: AutomationRule,
        record: ReplacedImage,
        repo_tags: Sequence[str],
        repo_digests: Sequence[str],
    ) -> _Result:
        environment_id = record.environment_id
        expected = references_digest(daemon_references(repo_tags, repo_digests))
        note = {
            "trigger": UPDATE_PRUNE,
            "replaced_at": record.replaced_at,
            "replaced_target_type": record.target_type,
            "replaced_target": record.target_id,
        }
        try:
            await self._images.remove(
                authority,
                environment_id,
                record.image_id,
                expected_references=expected,
                expected_in_use=0,
                force=False,
                note=note,
            )
        except ImageHasDependents:
            await self._drop(rule, record, False)
            return _Result("kept")
        except (ImageInUse, ImageChangedSinceReview, ObjectBusy, RunSkipped):
            return _Result("waiting")
        except (ImageGone, ResourceNotFound):
            await self._drop(rule, record, False)
            return _Result("gone")
        except PermissionDenied:
            return _Result("waiting", stop="owner")
        except EnvironmentUnavailable:
            return _Result("failed", stop="unavailable")
        except Exception as error:
            logger.warning(
                "image_prune_failed",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            return _Result("failed")
        await self._drop(rule, record, False)
        return _Result("pruned")

    async def _drop(self, rule: AutomationRule, record: ReplacedImage, dry_run: bool) -> None:
        if dry_run:
            return
        async with self._bookkeeping():
            await self._records().delete(rule, record.environment_id, record.image_id)
