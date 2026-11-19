"""Brings the containers that share a recreated container's network back onto it."""

from collections import deque
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from app.auth.errors import ProductError
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.job_run import is_reason
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError
from app.docker_engine.models import ContainerSummary
from app.logging import get_logger
from app.scheduler.model import RunSkipped, reason_of
from app.services.access import COMPOSE_PROJECT_LABEL
from app.services.acting import Acting, AutomationAuthority, actor_of
from app.services.activity import ActivityRecorder
from app.services.container_actions import (
    NOT_CONFIRMED,
    WAIT_EXPIRED,
    ContainerActionService,
    WaitBudget,
)
from app.services.container_names import display_name, short_id
from app.services.container_provisioning import ProvisionFeed, VerifyPolicy
from app.services.container_recreate import ContainerRecreateService
from app.services.container_review import RecreateOutcome
from app.services.network_joiners import (
    CONTAINER_MODE,
    MAX_JOINER_CHAIN,
    MAX_JOINER_DEPTH,
    JoinerLink,
    joins_by_id,
    list_joiners,
    walk_joiners,
)
from app.services.object_inspector import HostClients
from app.services.object_locks import ObjectBusy
from app.services.pull_credentials import CredentialChoice

logger = get_logger(__name__)

RECREATED_ACTION: Final = "container_recreated"
RESTARTED_ACTION: Final = "container_restarted"
COMPOSE_MANAGED: Final = "compose_managed"
OWNER_NOT_RUNNING: Final = "owner_not_running"
JOINERS_UNREAD: Final = "joiners_unread"
JOINERS_CAPPED: Final = "joiners_capped"
_REFUSALS: Final = {
    COMPOSE_MANAGED: "Compose manages this container; update its stack to recreate it.",
    OWNER_NOT_RUNNING: "The container it shares a network with is not running.",
    WAIT_EXPIRED: "The time allowed for waiting on the container it follows ran out.",
    NOT_CONFIRMED: "It joined this network after the confirmation was shown.",
    "permission_denied": "Changing it is not permitted.",
    "unreachable": "The host could not be reached to wait for the container it follows.",
    JOINERS_CAPPED: "The limit on containers followed was reached.",
}
_FOLLOWED_OK: Final = frozenset({"done", "unchanged"})
MAX_PLANNED_NAMES: Final = 25

type PlannedAction = Literal["recreate", "restart", "blocked"]


@dataclass(slots=True)
class JoinerReport:
    """What happened to the containers that share a recreated container's network.

    omitted counts the containers the chain limit left out.
    """

    recreated: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    omitted: int = 0


@dataclass(frozen=True, slots=True)
class PlannedJoiner:
    """One container a recreate brings onto the new one, and how."""

    container_id: str
    short_id: str
    name: str
    action: PlannedAction


@dataclass(frozen=True, slots=True)
class JoinerPreview:
    """The chain a recreate would follow: what is done, what the viewer may not change or see."""

    joiners: tuple[PlannedJoiner, ...] = ()
    refused: tuple[PlannedJoiner, ...] = ()
    hidden: int = 0
    omitted: int = 0

    @property
    def confirmed_ids(self) -> tuple[str, ...]:
        """Return the ids a confirmation binds the recreate to."""
        return tuple(item.container_id for item in self.joiners if item.action != "blocked")


@dataclass(frozen=True, slots=True)
class _Owner:
    old_id: str
    name: str
    new_id: str
    started: bool
    depth: int


def joiner_flags(report: JoinerReport) -> dict[str, str]:
    """Return the values a recreate reports to the page that shows its result."""
    counts = {
        "joiners_recreated": len(report.recreated),
        "joiners_restarted": len(report.restarted),
        "joiners_failed": len(report.failed),
        "joiners_omitted": report.omitted,
    }
    return {key: str(count) for key, count in counts.items() if count}


def _reason(error: ProductError) -> str:
    stated = getattr(error, "reason", None)
    return stated if isinstance(stated, str) and is_reason(stated) else reason_of(error)


def _listing(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_PLANNED_NAMES])
    more = len(names) - MAX_PLANNED_NAMES
    return shown if more <= 0 else f"{shown} and {more} more"


class JoinerFollower:
    """Recreates or restarts each container that shared a recreated container's network.

    A joiner that holds the old container's id is recreated with its network mode pointing at
    the new one, and left stopped when it was stopped; one that holds the name is restarted,
    when running. Joiners of a recreated joiner are followed in turn, up to the chain limits.
    The actor must be allowed to change each joiner; one it cannot see is skipped unnamed.
    With `confirmed`, only joiners that confirmation named are followed.
    """

    def __init__(
        self,
        *,
        actor: Acting,
        environment_id: int,
        client: EngineClient,
        recreation: ContainerRecreateService,
        actions: ContainerActionService,
        activity: ActivityRecorder,
        verify: VerifyPolicy | None,
        note: Mapping[str, DetailValue],
        confirmed: Collection[str] | None = None,
        wait: WaitBudget | None = None,
    ) -> None:
        self._actor = actor
        self._environment_id = environment_id
        self._client = client
        self._recreation = recreation
        self._actions = actions
        self._activity = activity
        self._verify = verify
        self._note = note
        self._confirmed = confirmed
        self._wait = wait

    async def planned(self, old_id: str, name: str) -> dict[str, DetailValue]:
        """Name the containers that join the recreated one, for the row of its recreate.

        Only containers the actor may change are named; the rest are counted.
        """
        try:
            rows = await list_joiners(self._client, old_id, name, all=True)
        except EngineError:
            return {}
        recreate: list[str] = []
        restart: list[str] = []
        refused: list[str] = []
        hidden = 0
        for row in rows:
            label = display_name(row.names, row.id)
            match await self._actions.joiner_standing(self._actor, self._environment_id, row.id):
                case "hidden":
                    hidden += 1
                case "readonly":
                    refused.append(label)
                case _ if joins_by_id(row.network_mode, old_id):
                    recreate.append(label)
                case _ if row.state == "running":
                    restart.append(label)
        found: dict[str, DetailValue] = {}
        for key, names in (
            ("joiners_recreate", recreate),
            ("joiners_restart", restart),
            ("joiners_refused", refused),
        ):
            if names:
                found[key] = _listing(sorted(names))
        if hidden:
            found["joiners_hidden"] = hidden
        return found

    async def follow(self, outcome: RecreateOutcome) -> JoinerReport:
        """Bring the owner's joiners onto the new container; never raises for a joiner's failure."""
        report = JoinerReport()
        queue = deque([_Owner(outcome.old_id, outcome.name, outcome.new_id, outcome.started, 0)])
        seen = {outcome.old_id, outcome.new_id}
        handled = 0
        while queue:
            owner = queue.popleft()
            try:
                rows = await list_joiners(self._client, owner.old_id, owner.name, all=True)
            except EngineError as error:
                logger.warning(
                    "container_joiners_unread",
                    extra={"environment_id": self._environment_id, "error": type(error).__name__},
                )
                report.failed.append((owner.name, JOINERS_UNREAD))
                continue
            fresh = [row for row in rows if row.id not in seen]
            if not fresh:
                continue
            waiting = await self._waiting(owner)
            for row in sorted(fresh, key=lambda each: display_name(each.names, each.id)):
                seen.add(row.id)
                if handled >= MAX_JOINER_CHAIN or owner.depth >= MAX_JOINER_DEPTH:
                    report.omitted += 1
                    continue
                standing = await self._actions.joiner_standing(
                    self._actor, self._environment_id, row.id
                )
                if standing == "hidden":
                    continue
                handled += 1
                name = display_name(row.names, row.id)
                recreated = joins_by_id(row.network_mode, owner.old_id)
                action = RECREATED_ACTION if recreated else RESTARTED_ACTION
                if standing == "readonly":
                    await self._refuse(owner, action, row.id, name, "permission_denied", report)
                elif recreated:
                    renewed = await self._recreate(owner, row, name, waiting, report)
                    if renewed is not None:
                        queue.append(renewed)
                elif row.state == "running":
                    handled += await self._restart(
                        owner, row.id, name, waiting, MAX_JOINER_CHAIN - handled, report
                    )
        return report

    async def _waiting(self, owner: _Owner) -> str | None:
        """Return why the containers following the owner cannot be restarted, or None."""
        if not owner.started:
            return OWNER_NOT_RUNNING
        refusal = await self._actions.owner_ready(self._client, owner.new_id, self._wait)
        return None if refusal is None else refusal.reason

    async def _recreate(
        self,
        owner: _Owner,
        row: ContainerSummary,
        name: str,
        waiting: str | None,
        report: JoinerReport,
    ) -> _Owner | None:
        running = row.state == "running"
        if COMPOSE_PROJECT_LABEL in row.labels:
            reason: str | None = COMPOSE_MANAGED
        elif not self._is_confirmed(row.id):
            reason = NOT_CONFIRMED
        else:
            reason = waiting if running else None
        if reason is not None:
            await self._refuse(owner, RECREATED_ACTION, row.id, name, reason, report)
            return None
        try:
            renewed = await self._recreation.recreate(
                self._actor,
                self._environment_id,
                row.id,
                expected_name=name,
                verify=self._verify if running else None,
                note={**self._note, "joined": owner.name},
                network_mode=f"{CONTAINER_MODE}{owner.new_id}",
            )
        except (RunSkipped, ObjectBusy) as skipped:
            await self._skipped(owner, RECREATED_ACTION, row.id, name, skipped, report)
            return None
        except ProductError as error:
            report.failed.append((name, _reason(error)))
            return None
        except Exception as error:
            logger.exception("container_joiner_failed")
            report.failed.append((name, reason_of(error)))
            return None
        report.recreated.append(name)
        return _Owner(row.id, renewed.name, renewed.new_id, renewed.started, owner.depth + 1)

    async def _restart(
        self,
        owner: _Owner,
        container_id: str,
        name: str,
        waiting: str | None,
        limit: int,
        report: JoinerReport,
    ) -> int:
        reason = None if self._is_confirmed(container_id) else NOT_CONFIRMED
        reason = reason or waiting
        if reason is not None:
            await self._refuse(owner, RESTARTED_ACTION, container_id, name, reason, report)
            return 0
        try:
            restarted = await self._actions.restart(
                self._actor,
                self._environment_id,
                container_id,
                note={**self._note, "joined": owner.name},
                chain=frozenset({owner.new_id}),
                joiners=self._confirmed,
                wait=self._wait,
                limit=limit,
            )
        except (RunSkipped, ObjectBusy) as skipped:
            await self._skipped(owner, RESTARTED_ACTION, container_id, name, skipped, report)
            return 0
        except ProductError as error:
            report.failed.append((name, _reason(error)))
            return 0
        except Exception as error:
            logger.exception("container_joiner_failed")
            report.failed.append((name, reason_of(error)))
            return 0
        report.restarted.append(name)
        report.omitted += restarted.joiners_omitted
        for item in restarted.joiners:
            if item.status not in _FOLLOWED_OK:
                report.failed.append((item.name or item.short_id, item.reason or item.status))
        return len(restarted.joiners)

    def _is_confirmed(self, container_id: str) -> bool:
        return self._confirmed is None or container_id in self._confirmed

    async def _skipped(
        self,
        owner: _Owner,
        action: str,
        container_id: str,
        name: str,
        skipped: RunSkipped | ObjectBusy,
        report: JoinerReport,
    ) -> None:
        """Report a joiner whose lock was refused; an automation's refusal leaves no event."""
        reason = skipped.reason if isinstance(skipped, RunSkipped) else "object_busy"
        report.failed.append((name, reason))
        if isinstance(self._actor, AutomationAuthority):
            await self._record(owner, action, container_id, name, reason, str(skipped))

    async def _refuse(
        self,
        owner: _Owner,
        action: str,
        container_id: str,
        name: str,
        reason: str,
        report: JoinerReport,
    ) -> None:
        report.failed.append((name, reason))
        await self._record(owner, action, container_id, name, reason, _REFUSALS[reason])

    async def _record(
        self,
        owner: _Owner,
        action: str,
        container_id: str,
        name: str,
        reason: str,
        message: str,
    ) -> None:
        await self._activity.record_failure(
            actor_of(self._actor),
            action=action,
            target_type="container",
            target_id=container_id,
            environment_id=self._environment_id,
            reason=reason,
            detail={**self._note, "name": name, "joined": owner.name},
            message=message,
        )


async def preview_joiners(
    actions: ContainerActionService,
    client: EngineClient,
    actor: Acting,
    environment_id: int,
    container_id: str,
    name: str,
) -> JoinerPreview:
    """Name the chain a recreate of the container would follow, as the actor may act on it.

    A container the actor may not change is named when it can be seen and counted when it
    cannot; neither ends up in the ids a confirmation binds the recreate to.
    """
    try:
        rows = await client.containers.list(all=True)
    except EngineError:
        return JoinerPreview()
    acting: dict[str, PlannedAction | None] = {}
    standing: dict[str, str] = {}

    async def descend(link: JoinerLink) -> bool:
        row = link.row
        found = await actions.joiner_standing(actor, environment_id, row.id)
        standing[row.id] = found
        parent = acting.get(link.owner_id, "recreate")
        if found != "writable" or parent == "blocked":
            return False
        if parent == "recreate" and joins_by_id(row.network_mode, link.owner_id):
            acting[row.id] = "blocked" if COMPOSE_PROJECT_LABEL in row.labels else "recreate"
        elif row.state == "running":
            acting[row.id] = "restart"
        else:
            return False
        return acting[row.id] != "blocked"

    walk = await walk_joiners(rows, [(container_id, name)], {container_id}, descend)
    planned: list[PlannedJoiner] = []
    refused: list[PlannedJoiner] = []
    hidden = 0
    for link in walk.links:
        row = link.row
        label = display_name(row.names, row.id)
        match standing[row.id]:
            case "hidden":
                hidden += 1
            case "readonly":
                refused.append(PlannedJoiner(row.id, short_id(row.id), label, "blocked"))
            case _ if (found := acting.get(row.id)) is not None:
                planned.append(PlannedJoiner(row.id, short_id(row.id), label, found))
    return JoinerPreview(tuple(planned), tuple(refused), hidden, walk.omitted)


class ContainerJoinerService:
    """Builds the follower and the preview for a recreate a user starts."""

    def __init__(
        self,
        *,
        hosts: HostClients,
        recreation: ContainerRecreateService,
        actions: ContainerActionService,
        activity: ActivityRecorder,
    ) -> None:
        self._hosts = hosts
        self._recreation = recreation
        self._actions = actions
        self._activity = activity

    async def follower(
        self,
        actor: Acting,
        environment_id: int,
        *,
        confirmed: Collection[str] | None = None,
        note: Mapping[str, DetailValue] | None = None,
    ) -> JoinerFollower:
        """Return a follower acting as the actor on the environment's host."""
        return JoinerFollower(
            actor=actor,
            environment_id=environment_id,
            client=await self._hosts.client(environment_id),
            recreation=self._recreation,
            actions=self._actions,
            activity=self._activity,
            verify=None,
            note=note or {},
            confirmed=confirmed,
            wait=self._actions.wait_budget(),
        )

    async def recreate(
        self,
        actor: Acting,
        environment_id: int,
        container_id: str,
        *,
        expected_name: str,
        confirmed: Collection[str] | None,
        pull: bool = False,
        credential_choice: CredentialChoice | None = None,
        progress: ProvisionFeed | None = None,
    ) -> tuple[RecreateOutcome, JoinerReport]:
        """Recreate the container, then bring the containers sharing its network onto it.

        A joiner's failure is in the report; only the recreate itself raises.
        """
        follower = await self.follower(actor, environment_id, confirmed=confirmed)
        outcome = await self._recreation.recreate(
            actor,
            environment_id,
            container_id,
            expected_name=expected_name,
            pull=pull,
            credential_choice=credential_choice,
            progress=progress,
            on_recreated=follower.planned,
        )
        return outcome, await follower.follow(outcome)

    async def preview(
        self, actor: Acting, environment_id: int, container_id: str, name: str
    ) -> JoinerPreview:
        """Name the chain a recreate of the container would follow."""
        client = await self._hosts.client(environment_id)
        return await preview_joiners(
            self._actions, client, actor, environment_id, container_id, name
        )
