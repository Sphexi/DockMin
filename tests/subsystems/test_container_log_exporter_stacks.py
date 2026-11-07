"""container-log-exporter: Swarm stacks and services selected by labels as targets."""

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs

import pytest

import app.subsystems.container_log_exporter as exporter
from app.auth.errors import ValidationFailed
from app.scheduler.model import RunResult
from app.services.access import COMPOSE_PROJECT_LABEL, SWARM_NAMESPACE_LABEL
from app.subsystems.activity_log_exporter import ActivityLogExporter
from app.subsystems.backup_syncer import BackupSyncer
from app.subsystems.container_health_checker import ContainerHealthChecker
from app.subsystems.container_updater import ContainerUpdater
from app.subsystems.selectors import NOT_OFFERED, NOT_SWARM, SELECTOR_FIELD, parse_selector_text
from app.subsystems.stack_restarter import StackRestarter
from app.subsystems.webhook_receiver import WebhookReceiver
from tests.access_support import ADMIN, ENV_A
from tests.automation_support import cid, counts
from tests.docker_engine.fake_engine import Reply, Scope, split_version
from tests.docker_engine.helpers import log_frame
from tests.docker_engine.swarm_engine import plain_spec
from tests.subsystems.exporter_support import PREFIX
from tests.subsystems.test_container_log_exporter import (
    MULTIPLEXED,
    RULE_FOLDER,
    STDERR,
    STDOUT,
    ServiceWorld,
    at,
    since_ns,
    stamped,
)

COMPOSE_STACK_ROW, SWARM_STACK_ROW = 1, 2
SHOP = {"kind": "stack", "names": ["shop"]}
SHOP_NAMESPACES = ("Shop", "SHOP", "shop")
MANAGED_FOLDER = f"{PREFIX}/service-logs/{RULE_FOLDER}/{ENV_A}"
CONTAINER_FOLDER = f"{PREFIX}/container-logs/{RULE_FOLDER}/{ENV_A}"
SERVICE_LIST = "/services"
NOT_MANAGER = "standalone"
LABELLED_SERVICES = 21
SERVICE_CAP = 20


def stack_row(world: ServiceWorld, name: str, kind: str, number: int) -> None:
    """Insert a managed stack row; `number` makes its project directory unique."""
    world.subject.scheduling.sql(
        "INSERT INTO stack (environment_id, name, kind, origin, project_directory, compose_file, "
        "compose_file_digest, created_at, updated_at) "
        "VALUES (?, ?, ?, 'created', ?, 'x', ?, 'x', 'x')",
        (ENV_A, name, kind, f"{number:032x}", "b" * 64),
    )


def add_labelled(
    world: ServiceWorld, name: str, labels: Mapping[str, str], replicas: int = 1
) -> tuple[str, list[str]]:
    """Add a service carrying the labels whose tasks print one line each."""
    service = world.swarm.add_service(plain_spec(name, replicas=replicas, Labels=dict(labels)))
    tasks = world.swarm.service_tasks(service)
    for task in tasks:
        world.swarm.tasks[task]["CreatedAt"] = stamped(at(0))
        world.print(task, (at(1), STDOUT, f"{name} says hi"))
    return service, tasks


def task_keys(world: ServiceWorld, task: str) -> list[str]:
    return [key for key in world.bucket.stored if f"/{task}/" in key]


def list_filters(world: ServiceWorld) -> list[dict[str, Any]]:
    """Return the decoded filters of every service list request."""
    return [
        json.loads(each.query.get("filters", "{}"))
        for each in world.swarm.requests
        if each.method == "GET" and each.path.endswith(SERVICE_LIST)
    ]


class MixedWorld(ServiceWorld):
    """A Swarm manager whose plain containers also print scripted logs."""

    def __init__(self, rig: Any, settings: Any) -> None:
        super().__init__(rig, settings)
        self.container_logs: dict[str, list[tuple[int, int, str]]] = {}
        original = self.swarm._reply

        async def reply(scope: Scope, body: bytes) -> Reply | None:
            _, path = split_version(scope["path"])
            parts = path.strip("/").split("/")
            if scope["method"] != "GET" or parts[0] != "containers" or parts[-1] != "logs":
                served: Reply | None = await original(scope, body)
                return served
            query = {k: v[-1] for k, v in parse_qs(scope["query_string"].decode()).items()}
            box = self.swarm.resolve(parts[1])
            assert box is not None
            floor = since_ns(query["since"]) if "since" in query else 0
            payload = b"".join(
                log_frame(stream, f"{stamped(ns)} {text}\n".encode())
                for ns, stream, text in self.container_logs.get(box.id, [])
                if ns >= floor
            )
            return Reply(200, payload, MULTIPLEXED)

        self.swarm._reply = reply  # type: ignore[method-assign]

    def add_container(self, number: int, name: str, project: str, *lines: Any) -> str:
        box = self.swarm.add(cid(number), name, labels={COMPOSE_PROJECT_LABEL: project})
        self.container_logs[box.id] = list(lines)
        return box.id


@pytest.fixture
def stack_world(swarm: Any, settings: Any) -> ServiceWorld:
    return ServiceWorld(swarm, settings)


@pytest.fixture
def mixed_world(swarm: Any, settings: Any) -> MixedWorld:
    return MixedWorld(swarm, settings)


async def test_a_managed_swarm_stack_exports_one_object_per_task_whatever_the_label_case(
    stack_world: ServiceWorld,
) -> None:
    stack_row(stack_world, "shop", "swarm", SWARM_STACK_ROW)
    _, web_tasks = add_labelled(stack_world, "shop_web", {SWARM_NAMESPACE_LABEL: "Shop"}, 2)
    _, db_tasks = add_labelled(stack_world, "shop_db", {SWARM_NAMESPACE_LABEL: "SHOP"})
    _, [other_task] = add_labelled(stack_world, "shop2_web", {SWARM_NAMESPACE_LABEL: "shop2"})
    _, [bare_task] = add_labelled(stack_world, "bare", {})
    job = await stack_world.rule(SHOP)

    result = await stack_world.run(job)

    detail = counts(result)
    assert (result.outcome, detail["exported"], detail["services"], detail["tasks"]) == (
        "success",
        3,
        2,
        3,
    )
    assert detail["targets"] == "shop"
    assert {task for task in (*web_tasks, *db_tasks) if task_keys(stack_world, task)} == {
        *web_tasks,
        *db_tasks,
    }
    assert [task_keys(stack_world, task) for task in (other_task, bare_task)] == [[], []]
    assert all(key.startswith(MANAGED_FOLDER) for key in stack_world.bucket.stored)


async def test_a_swarm_stack_that_matches_no_service_exports_nothing_and_succeeds(
    stack_world: ServiceWorld,
) -> None:
    stack_row(stack_world, "shop", "swarm", SWARM_STACK_ROW)
    add_labelled(stack_world, "other_web", {SWARM_NAMESPACE_LABEL: "other"})
    job = await stack_world.rule(SHOP)

    result = await stack_world.run(job)

    assert (result.outcome, counts(result)["exported"]) == ("success", 0)
    assert stack_world.bucket.attempts == []


async def test_a_compose_stack_and_a_swarm_stack_in_one_rule_export_both(
    mixed_world: MixedWorld,
) -> None:
    stack_row(mixed_world, "shop", "compose", COMPOSE_STACK_ROW)
    stack_row(mixed_world, "Mesh", "swarm", SWARM_STACK_ROW)
    mixed_world.add_container(1, "shop-web-1", "Shop", (at(1), STDERR, "from compose"))
    mixed_world.add_container(2, "other-web-1", "other", (at(1), STDOUT, "unrelated"))
    _, [task] = add_labelled(mixed_world, "mesh_web", {SWARM_NAMESPACE_LABEL: "MESH"})
    job = await mixed_world.rule({"kind": "stack", "names": ["Mesh", "shop"]})

    result = await mixed_world.run(job)

    detail = counts(result)
    assert (result.outcome, detail["exported"], detail["tasks"], detail["services"]) == (
        "success",
        2,
        1,
        1,
    )
    stored = sorted(mixed_world.bucket.stored)
    assert [key.startswith(CONTAINER_FOLDER) for key in stored] == [True, False]
    assert f"/{task}/" in stored[1]
    assert sorted(mixed_world.bucket.messages()) == ["from compose", "mesh_web says hi"]
    assert detail["targets"] == "Mesh, shop"


async def test_a_dry_run_over_a_compose_and_a_swarm_stack_plans_both_and_reads_nothing(
    mixed_world: MixedWorld,
) -> None:
    stack_row(mixed_world, "shop", "compose", COMPOSE_STACK_ROW)
    stack_row(mixed_world, "mesh", "swarm", SWARM_STACK_ROW)
    mixed_world.add_container(1, "shop-web-1", "shop", (at(1), STDOUT, "x"))
    _, [task] = add_labelled(mixed_world, "mesh_web", {SWARM_NAMESPACE_LABEL: "mesh"})
    job = await mixed_world.rule({"kind": "stack", "names": ["mesh", "shop"]})

    result = await mixed_world.run(job, dry=True)

    detail = counts(result)
    assert result.outcome == "success"
    assert detail["would_export_count"] == 2
    assert (detail["services"], detail["tasks"], detail["without_cursor"]) == (1, 1, 2)
    assert f"mesh_web/{task[:12]}" in str(detail["would_export"])
    assert mixed_world.bucket.attempts == []


async def test_a_swarm_stack_on_an_environment_that_stopped_being_a_manager_is_refused_at_save(
    stack_world: ServiceWorld,
) -> None:
    stack_row(stack_world, "shop", "swarm", SWARM_STACK_ROW)
    stack_world.subject.scheduling.sql(
        "UPDATE environment SET type = ? WHERE id = ?", (NOT_MANAGER, ENV_A)
    )

    with pytest.raises(ValidationFailed) as refused:
        await stack_world.subject.subsystem.authorise_target(
            ADMIN,
            ENV_A,
            json.dumps(SHOP),
            None,  # type: ignore[arg-type]
        )

    assert refused.value.fields == {SELECTOR_FIELD: NOT_SWARM}


async def test_a_swarm_stack_and_a_compose_stack_save_on_a_manager_and_a_compose_stack_off_it(
    stack_world: ServiceWorld,
) -> None:
    stack_row(stack_world, "mesh", "swarm", SWARM_STACK_ROW)
    stack_row(stack_world, "shop", "compose", COMPOSE_STACK_ROW)
    saved = stack_world.subject.subsystem.authorise_target
    both = json.dumps({"kind": "stack", "names": ["mesh", "shop"]})

    await saved(ADMIN, ENV_A, both, None)  # type: ignore[arg-type]
    stack_world.subject.scheduling.sql(
        "UPDATE environment SET type = ? WHERE id = ?", (NOT_MANAGER, ENV_A)
    )
    await saved(ADMIN, ENV_A, json.dumps(SHOP), None)  # type: ignore[arg-type]


async def test_a_swarm_stack_rule_on_a_non_swarm_environment_fails_and_records_the_run(
    stack_world: ServiceWorld,
) -> None:
    stack_row(stack_world, "shop", "swarm", SWARM_STACK_ROW)
    job = await stack_world.rule(SHOP)
    stack_world.subject.scheduling.sql(
        "UPDATE environment SET type = ? WHERE id = ?", (NOT_MANAGER, ENV_A)
    )

    result = await stack_world.run(job)

    [failure] = stack_world.failures()
    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert failure["result"] == "failure"
    assert stack_world.bucket.attempts == []


async def test_a_stack_rule_whose_stack_is_gone_records_a_target_missing_failure(
    stack_world: ServiceWorld,
) -> None:
    job = await stack_world.rule(SHOP)

    result = await stack_world.run(job)

    [failure] = stack_world.failures()
    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert failure["result"] == "failure"
    assert stack_world.bucket.attempts == []


def service_labels(*terms: tuple[str, str | None]) -> dict[str, Any]:
    return {"kind": "service_labels", "labels": [list(term) for term in terms]}


async def named_rule(world: ServiceWorld, selector: dict[str, Any]) -> int:
    """Create a second rule over the selector and return its job id."""
    _, job = await world.subject.rule(
        selector,
        {"object_storage_target_id": world.target_id},
        name="second",
        environment_id=ENV_A,
    )
    return job


async def test_services_selected_by_labels_match_by_key_and_by_key_and_value_without_a_filter(
    stack_world: ServiceWorld,
) -> None:
    _, [prod_web] = add_labelled(stack_world, "prod_web", {"tier": "web", "env": "prod"})
    _, [dev_web] = add_labelled(stack_world, "dev_web", {"tier": "web"})
    _, [db] = add_labelled(stack_world, "db", {"tier": "db", "env": "prod"})
    _, [bare] = add_labelled(stack_world, "bare", {})
    by_key = await stack_world.rule(service_labels(("env", None)))
    by_pair = await named_rule(stack_world, service_labels(("tier", "web"), ("env", "prod")))

    first = await stack_world.run(by_key)
    second = await stack_world.run(by_pair)

    assert (counts(first)["matched"], counts(second)["matched"]) == (2, 1)
    exported = {task: task_keys(stack_world, task) for task in (prod_web, dev_web, db, bare)}
    assert [len(exported[task]) for task in (prod_web, dev_web, db, bare)] == [2, 0, 1, 0]
    assert all(filters == {} for filters in list_filters(stack_world))
    assert not any("tier" in str(each.query) for each in stack_world.swarm.requests)


async def test_services_selected_by_labels_stop_at_the_service_cap_and_the_next_run_continues(
    stack_world: ServiceWorld,
) -> None:
    tasks = [
        add_labelled(stack_world, f"svc{number:02}", {"export": "yes"})[1][0]
        for number in range(LABELLED_SERVICES)
    ]
    job = await stack_world.rule(service_labels(("export", "yes")))

    first = await stack_world.run(job)
    second = await stack_world.run(job)

    assert (counts(first)["matched"], counts(first)["services"]) == (
        LABELLED_SERVICES,
        SERVICE_CAP,
    )
    assert (counts(first)["more_pending"], counts(second)["exported"]) == (True, 1)
    assert sorted(len(task_keys(stack_world, task)) for task in tasks) == [1] * LABELLED_SERVICES
    assert exporter.MAX_SERVICES_PER_RUN == SERVICE_CAP


async def test_services_selected_by_labels_that_match_none_report_matched_zero(
    stack_world: ServiceWorld,
) -> None:
    add_labelled(stack_world, "web", {"tier": "web"})
    job = await stack_world.rule(service_labels(("tier", "db")))

    result = await stack_world.run(job)

    detail = counts(result)
    assert (result.outcome, detail["matched"], detail["exported"]) == ("success", 0, 0)
    assert stack_world.bucket.attempts == []


async def test_a_services_with_labels_dry_run_lists_the_matched_services_and_their_tasks(
    stack_world: ServiceWorld,
) -> None:
    _, [first, second] = add_labelled(stack_world, "web", {"tier": "web"}, 2)
    add_labelled(stack_world, "db", {"tier": "db"})
    job = await stack_world.rule(service_labels(("tier", "web")))

    result = await stack_world.run(job, dry=True)

    detail = counts(result)
    assert detail["would_export"] == f"web/{first[:12]}, web/{second[:12]}"
    assert (detail["matched"], detail["services"], detail["tasks"]) == (1, 1, 2)
    assert stack_world.bucket.attempts == []
    assert stack_world.reads() == []


async def test_services_selected_by_labels_save_on_a_manager_and_are_refused_off_it(
    stack_world: ServiceWorld,
) -> None:
    saved = stack_world.subject.subsystem.authorise_target
    text = json.dumps(service_labels(("tier", None)))

    await saved(ADMIN, ENV_A, text, None)  # type: ignore[arg-type]
    stack_world.subject.scheduling.sql(
        "UPDATE environment SET type = ? WHERE id = ?", (NOT_MANAGER, ENV_A)
    )
    with pytest.raises(ValidationFailed) as refused:
        await saved(ADMIN, ENV_A, text, None)  # type: ignore[arg-type]

    assert refused.value.fields == {SELECTOR_FIELD: NOT_SWARM}


async def test_services_selected_by_labels_on_a_non_swarm_environment_fail_and_record_the_run(
    stack_world: ServiceWorld,
) -> None:
    job = await stack_world.rule(service_labels(("tier", None)))
    stack_world.subject.scheduling.sql(
        "UPDATE environment SET type = ? WHERE id = ?", (NOT_MANAGER, ENV_A)
    )

    result: RunResult = await stack_world.run(job)

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert len(stack_world.failures()) == 1
    assert stack_world.reads() == []


@pytest.mark.parametrize(
    "subsystem",
    [
        ContainerUpdater,
        StackRestarter,
        ContainerHealthChecker,
        WebhookReceiver,
        BackupSyncer,
        ActivityLogExporter,
    ],
)
def test_every_other_subsystem_refuses_services_with_labels_as_not_offered(
    subsystem: Any,
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        parse_selector_text(json.dumps(service_labels(("tier", None))), subsystem.target_kinds)

    assert refused.value.fields == {SELECTOR_FIELD: NOT_OFFERED}
