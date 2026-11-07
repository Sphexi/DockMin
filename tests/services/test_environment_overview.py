"""The environment page's figures, stack states and container table, scoped to the caller."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.crypto.vault import Vault
from app.db.repositories.automation_rule import AutomationRule, AutomationRuleRepository
from app.db.repositories.backup_record import BackupRecordRepository
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.services.activity import ActivityRecorder, SubsystemActor
from app.services.assembly import (
    AccessServices,
    build_access_services,
    build_container_services,
    build_host_clients,
    build_image_services,
    build_stack_services,
)
from app.services.environment_overview import (
    MAX_CONTAINER_ROWS,
    MAX_STACK_TILES,
    CardFigure,
    EnvironmentOverview,
    OverviewData,
)
from app.services.image_queries import ImageQueryService
from app.services.image_views import ImageFilters
from app.services.object_inspector import EngineObjectInspector
from app.services.restart_holds import EXHAUSTED_ACTION, HEALTH_CHECKER
from tests import stack_support
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    STANDARD,
    container_id,
    control,
    stack_key,
    swarm_labels,
)
from tests.auth_support import ADMIN_ID, STANDARD_ID, FakeClock
from tests.container_support import CACHE, DB, STACK, STACK_LABEL, WEB, stock
from tests.docker_engine.fake_engine import Refuse
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec
from tests.image_support import digest, full_id
from tests.web.environment_support import seed_stack_rows

NGINX, REDIS = 0x1101, 0x1102
NGINX_REFERENCE, REDIS_REFERENCE = "docker.io/library/nginx:1", "docker.io/library/redis:7"
CHECKED_AT = "2026-01-02T03:04:05Z"
MIXED_RUNNING, MIXED_STOPPED = container_id(0xB1), container_id(0xB2)
MIXED = "mixed"
LUMEN, DIM, DARK = "Lumen", "Dim", "Dark"
IDLE = "idle"
WINDOW = timedelta(hours=24)
RULE_NAME = "Heal"
NEGOTIATION = ("GET /_ping", "GET /version")
CONTAINER_READS = 2


@dataclass(slots=True)
class Rig:
    """The overview of one request over a fake manager, with what a test reaches into."""

    stacks: stack_support.Rig
    host: SwarmEngine
    clock: FakeClock

    def request(self) -> tuple[EnvironmentOverview, ImageQueryService, AccessServices]:
        """Wire the services as the next request would, remembering nothing of the last."""
        rig = self.stacks
        recorder = ActivityRecorder(rig.db, rig.clock)
        inspector = EngineObjectInspector(build_host_clients(rig.db, rig.runtime, rig.engine))
        access = build_access_services(rig.db, rig.runtime, inspector, recorder)
        registries = rig.registry.registries
        containers = build_container_services(
            rig.db, rig.database, rig.runtime, rig.engine, access, inspector, recorder
        )
        images = build_image_services(
            rig.db, rig.database, rig.runtime, rig.engine, registries, access, inspector, recorder
        )
        stacks = build_stack_services(
            rig.db, rig.database, rig.runtime, rig.engine, registries, access, inspector, recorder
        )
        overview = EnvironmentOverview(
            authoriser=access.authoriser,
            hosts=inspector.hosts,
            containers=containers.queries,
            stacks=stacks.queries,
            images=images.queries,
            db=rig.db,
            clock=rig.runtime.clock,
        )
        return overview, images.queries, access

    async def overview(self, who: Principal = ADMIN, environment_id: int = ENV_A) -> OverviewData:
        overview, _, _ = self.request()
        return await overview.overview(who, environment_id)


@pytest.fixture
async def rig(
    access_template: Path,
    tmp_path: Path,
    runtime: AuthRuntime,
    clock: FakeClock,
    vault: Vault,
    swarm_host: SwarmEngine,
    tls_workdir: Path,
) -> AsyncIterator[Rig]:
    """A reachable Swarm manager on ENV_A holding the standard containers."""
    stock(swarm_host)
    async with stack_support.build(
        access_template, tmp_path, runtime, clock, vault, swarm_host
    ) as built:
        await built.db.execute(
            "UPDATE environment SET status = 'reachable', type = 'swarm' WHERE id IN (?, ?)",
            (ENV_A, ENV_B),
        )
        yield Rig(built, swarm_host, clock)


def figures(data: OverviewData) -> dict[str, list[tuple[str, int | None, int | None]]]:
    return {
        card.key: [(f.label, f.value, f.of) for f in card.figures if f.kind == "count"]
        for card in data.cards
    }


def figure(data: OverviewData, key: str, label: str) -> CardFigure:
    [found] = [
        f for card in data.cards if card.key == key for f in card.figures if f.label == label
    ]
    return found


def states(data: OverviewData) -> dict[str, tuple[str, str | None]]:
    return {tile.name: (tile.kind, tile.state) for tile in data.stacks}


async def make_rule(
    rig: Rig, *, owner: int = ADMIN_ID, environment_id: int = ENV_A
) -> AutomationRule:
    return await AutomationRuleRepository(rig.stacks.db, rig.clock).insert(
        name=f"{RULE_NAME} {owner}",
        subsystem=HEALTH_CHECKER,
        environment_id=environment_id,
        target_selector="{}",
        parameters="{}",
        is_enabled=True,
        created_by_user_id=owner,
    )


async def hold(rig: Rig, rule: AutomationRule, container: str) -> None:
    await ActivityRecorder(rig.stacks.db, rig.clock).record_failure(
        SubsystemActor(rule.subsystem, rule.id, None, rule.name),
        action=EXHAUSTED_ACTION,
        target_type="container",
        target_id=container,
        environment_id=ENV_A,
        reason="restart_budget_exhausted",
        detail={"name": "web"},
    )


async def run_of(
    rig: Rig, rule: AutomationRule, outcome: str, *, trigger: str = "schedule"
) -> None:
    jobs, runs = (
        ScheduledJobRepository(rig.stacks.db, rig.clock),
        JobRunRepository(rig.stacks.db, rig.clock),
    )
    job = await jobs.get_for_rule(rule.id)
    if job is None:
        job = await jobs.insert(
            subsystem=rule.subsystem,
            rule_id=rule.id,
            environment_id=rule.environment_id,
            schedule_kind="interval",
            schedule_expression="600",
            time_zone="UTC",
            overrun_policy="skip",
            catch_up=False,
            failure_threshold=5,
            next_run_at="2030-01-01T00:00:00Z",
        )
    run = await runs.start(job.id, trigger=trigger, scheduled_for=None)  # type: ignore[arg-type]
    reason = "host_down" if outcome == "failure" else None
    await runs.finish(run.id, outcome=outcome, reason=reason, message=None, detail={})  # type: ignore[arg-type]


async def populate(rig: Rig) -> None:
    """Three Compose stacks and three Swarm stacks, one image with an update, one without."""
    host = rig.host
    host.add(MIXED_RUNNING, "mixed-up", labels={STACK_LABEL: MIXED})
    host.add(MIXED_STOPPED, "mixed-down", state="exited", labels={STACK_LABEL: MIXED})
    host.add_image("nginx:1", full_id(NGINX))
    host.add_image("redis:7", full_id(REDIS))
    host.add_service(plain_spec("lumen-web", Labels=swarm_labels(LUMEN)))
    host.add_service(plain_spec("lumen-api", Labels=swarm_labels(LUMEN)))
    dim = host.add_service(plain_spec("dim-web", replicas=2, Labels=swarm_labels(DIM)))
    first_task = next(task for task in host.tasks.values() if task["ServiceID"] == dim)
    first_task["Status"]["State"] = "failed"
    await seed_stack_rows(
        rig.stacks.db,
        ENV_A,
        (STACK, "compose"),
        (MIXED, "compose"),
        (IDLE, "compose"),
        (LUMEN, "swarm"),
        (DIM, "swarm"),
        (DARK, "swarm"),
    )
    await store_update(rig, NGINX_REFERENCE, "update_available")
    await store_update(rig, REDIS_REFERENCE, "up_to_date")


async def store_update(rig: Rig, reference: str, state: str) -> None:
    await rig.stacks.db.execute(
        "INSERT INTO image_update_status (environment_id, image_reference, state, remote_digest, "
        "checked_at) VALUES (?, ?, ?, ?, ?)",
        (ENV_A, reference, state, digest(1), CHECKED_AT),
    )


async def test_an_administrator_sees_the_counts_of_every_card_over_the_whole_environment(
    rig: Rig,
) -> None:
    await populate(rig)
    rule = await make_rule(rig)
    await hold(rig, rule, WEB)
    await run_of(rig, rule, "failure")

    data = await rig.overview()

    assert figures(data) == {
        "stacks": [("running", 1, 3), ("swarm running", 1, 3)],
        "containers": [("running", 4, 8)],
        "health": [("unhealthy", 1, None), ("held", 1, None)],
        "updates": [("images", 1, None)],
        "automation": [("failed in 24 hours", 1, None)],
    }
    assert data.host == "live"


async def test_the_failed_runs_are_those_of_the_last_day_without_dry_runs(rig: Rig) -> None:
    rule = await make_rule(rig)
    await run_of(rig, rule, "failure")
    await run_of(rig, rule, "failure", trigger="dry_run")
    await run_of(rig, rule, "success")
    rig.clock.advance(timedelta(hours=23))

    inside = await rig.overview()
    rig.clock.advance(timedelta(hours=2))
    outside = await rig.overview()

    assert figure(inside, "automation", "failed in 24 hours").value == 1
    assert figure(outside, "automation", "failed in 24 hours").value == 0


async def test_each_stack_has_the_state_its_containers_or_services_give_it(rig: Rig) -> None:
    await populate(rig)

    data = await rig.overview()

    assert states(data) == {
        STACK: ("compose", "running"),
        MIXED: ("compose", "partly running"),
        IDLE: ("compose", "stopped"),
        LUMEN: ("swarm", "running"),
        DIM: ("swarm", "partly running"),
        DARK: ("swarm", "stopped"),
    }
    assert [tile.name for tile in data.stacks] == sorted(states(data), key=str.casefold)


async def test_every_card_figure_links_to_the_list_behind_it(rig: Rig) -> None:
    await populate(rig)

    data = await rig.overview()

    assert {
        (card.key, f.label): (f.link.route, dict(f.link.path), f.link.query)
        for card in data.cards
        for f in card.figures
    } == {
        ("stacks", "running"): ("environment_stack_list", {"environment_id": ENV_A}, ()),
        ("stacks", "swarm running"): (
            "environment_swarm_stack_list",
            {"environment_id": ENV_A},
            (),
        ),
        ("containers", "running"): (
            "environment_container_list",
            {"environment_id": ENV_A},
            (("state", "running"),),
        ),
        ("health", "unhealthy"): (
            "environment_container_list",
            {"environment_id": ENV_A},
            (("health", "unhealthy"),),
        ),
        ("health", "held"): (
            "automation",
            {},
            (("environment_id", str(ENV_A)), ("subsystem", HEALTH_CHECKER)),
        ),
        ("updates", "images"): (
            "environment_image_list",
            {"environment_id": ENV_A},
            (("update", "available"),),
        ),
        ("automation", "failed in 24 hours"): (
            "automation",
            {},
            (("environment_id", str(ENV_A)),),
        ),
        ("automation", "last backup"): ("backups", {}, ()),
    }


async def test_the_last_backup_is_a_time_figure_that_is_empty_until_a_backup_succeeds(
    rig: Rig,
) -> None:
    before = await rig.overview()
    await BackupRecordRepository(rig.stacks.db, rig.clock).add_backup_record(
        origin="download",
        target_id=None,
        size_bytes=1,
        key_version=1,
        key_fingerprint="f",
        destination_key=None,
        archive_sha256=None,
        schema_version=None,
        outcome="success",
        error=None,
    )
    after = await rig.overview()
    standard = await rig.overview(STANDARD)

    backup = figure(before, "automation", "last backup")
    assert (backup.kind, backup.at) == ("time", None)
    assert figure(after, "automation", "last backup").at == format_timestamp(rig.clock())
    assert [
        f.label for card in standard.cards if card.key == "automation" for f in card.figures
    ] == ["failed in 24 hours"]


async def test_the_updates_card_equals_the_image_list_filtered_to_available_for_each_caller(
    rig: Rig,
) -> None:
    await populate(rig)
    await store_update(rig, "docker.io/library/spare:1", "update_available")
    rig.host.add_image("spare:1", full_id(0x1103))
    overview, images, _ = rig.request()

    cards = {
        who.username: figure(await overview.overview(who, ENV_A), "updates", "images").value
        for who in (ADMIN, STANDARD)
    }
    listed = {
        who.username: (await images.page(who, ENV_A, ImageFilters(update="available"))).total
        for who in (ADMIN, STANDARD)
    }

    assert cards == listed == {ADMIN.username: 2, STANDARD.username: 2}


async def test_a_standard_user_counts_only_what_the_controls_let_them_see(rig: Rig) -> None:
    await populate(rig)
    shared = rig.request()[2]
    await control(shared, stack_key(STACK, ENV_A), public=True, level="read")
    mine = await make_rule(rig, owner=STANDARD_ID)
    theirs = await make_rule(rig, owner=ADMIN_ID)
    await hold(rig, mine, WEB)
    await hold(rig, theirs, DB)
    await hold(rig, theirs, CACHE)
    await run_of(rig, mine, "failure")
    await run_of(rig, theirs, "failure")
    await run_of(rig, theirs, "failure")

    administrator = await rig.overview(ADMIN)
    standard = await rig.overview(STANDARD)

    assert figures(administrator)["automation"] == [("failed in 24 hours", 3, None)]
    assert figures(administrator)["health"][1] == ("held", 3, None)
    assert figures(standard) == {
        "stacks": [("running", 1, 1)],
        "containers": [("running", 2, 2)],
        "health": [("unhealthy", 1, None), ("held", 1, None)],
        "updates": [("images", 1, None)],
        "automation": [("failed in 24 hours", 1, None)],
    }
    assert [(t.name, t.kind) for t in standard.stacks] == [(STACK, "compose")]
    assert {c.name for c in standard.containers} == {"web", "db"}
    assert standard.containers_total == 2


async def test_an_environment_the_caller_may_not_see_is_not_found(rig: Rig) -> None:
    await EnvironmentAccessRepository(rig.stacks.db).remove_user("environment", ENV_A, STANDARD_ID)

    with pytest.raises(ResourceNotFound):
        await rig.overview(STANDARD)


async def test_the_page_reads_containers_with_their_health_images_and_services_once_each(
    rig: Rig,
) -> None:
    await populate(rig)

    await rig.overview()

    reads = [path for path in rig.host.paths() if path not in NEGOTIATION]
    assert sorted(set(reads)) == ["GET /containers/json", "GET /images/json", "GET /services"]
    assert reads.count("GET /containers/json") == CONTAINER_READS
    assert reads.count("GET /images/json") == 1
    assert reads.count("GET /services") == 1


async def test_a_standalone_host_or_one_with_no_swarm_stack_makes_no_service_read(rig: Rig) -> None:
    await seed_stack_rows(rig.stacks.db, ENV_A, (STACK, "compose"))

    await rig.overview()

    assert "GET /services" not in rig.host.paths()


async def test_an_environment_stored_as_unreachable_is_not_read_and_keeps_the_database_figures(
    rig: Rig,
) -> None:
    await populate(rig)
    rule = await make_rule(rig)
    await run_of(rig, rule, "failure")
    await rig.stacks.db.execute(
        "UPDATE environment SET status = 'unreachable' WHERE id = ?", (ENV_A,)
    )

    data = await rig.overview()

    assert data.host == "unreachable"
    assert rig.host.requests == []
    assert (data.containers, data.containers_total) == ((), None)
    assert {state for _, state in states(data).values()} == {None}
    assert figures(data)["containers"] == [("running", None, None)]
    assert figures(data)["health"] == [("unhealthy", None, None), ("held", None, None)]
    assert figures(data)["updates"] == [("images", None, None)]
    assert figures(data)["automation"] == [("failed in 24 hours", 1, None)]
    assert figure(data, "stacks", "running (3 unreadable)").value is None


async def test_a_host_that_fails_to_answer_is_reported_in_the_data_and_never_raised(
    rig: Rig,
) -> None:
    await populate(rig)
    rig.host.route("GET", "/containers/json", Refuse())
    rig.host.route("GET", "/containers/json", Refuse())

    data = await rig.overview()

    assert data.host == "unavailable"
    assert (data.containers, data.containers_total) == ((), None)
    assert figures(data)["containers"] == [("running", None, None)]
    assert {tile.state for tile in data.stacks} == {None}
    assert figure(data, "automation", "failed in 24 hours").value == 0


async def test_a_failed_image_read_blanks_only_the_updates_figure(rig: Rig) -> None:
    await populate(rig)
    rig.host.route("GET", "/images/json", Refuse())

    data = await rig.overview()

    assert data.host == "live"
    assert figure(data, "updates", "images").value is None
    assert figure(data, "containers", "running").value == 4


async def test_the_stack_grid_holds_one_hundred_tiles_and_counts_the_rest(rig: Rig) -> None:
    names = [(f"stack{number:03d}", "compose") for number in range(MAX_STACK_TILES + 5)]
    await seed_stack_rows(rig.stacks.db, ENV_A, *names)  # type: ignore[arg-type]

    data = await rig.overview()

    assert len(data.stacks) == MAX_STACK_TILES
    assert (data.stacks_total, data.stacks_more) == (MAX_STACK_TILES + 5, 5)
    assert figures(data)["stacks"] == [("running", 0, MAX_STACK_TILES + 5)]


async def test_the_container_table_holds_fifty_rows_by_name_and_states_the_total(rig: Rig) -> None:
    for number in range(MAX_CONTAINER_ROWS + 5):
        rig.host.add(container_id(0x500 + number), f"extra{number:03d}", labels={})

    data = await rig.overview()

    assert len(data.containers) == MAX_CONTAINER_ROWS
    assert data.containers_total == MAX_CONTAINER_ROWS + 5 + 6
    assert [c.name for c in data.containers] == sorted(c.name for c in data.containers)
    assert figure(data, "containers", "running").of == MAX_CONTAINER_ROWS + 5 + 6


async def test_a_stack_list_at_the_read_limit_says_so_in_its_label(rig: Rig) -> None:
    await seed_stack_rows(
        rig.stacks.db,
        ENV_A,
        *[(f"s{n:04d}", "compose") for n in range(MAX_PAGE_SIZE)],
    )

    data = await rig.overview()

    assert figure(data, "stacks", "running (first 500 stacks only)").of == MAX_PAGE_SIZE


async def test_one_stack_whose_state_cannot_be_read_is_counted_as_unreadable_not_as_stopped(
    rig: Rig,
) -> None:
    rig.host.add_service(plain_spec("lumen-web", Labels=swarm_labels(LUMEN)))
    await seed_stack_rows(rig.stacks.db, ENV_A, (STACK, "compose"), (LUMEN, "swarm"))
    rig.host.route("GET", "/services", Refuse())

    data = await rig.overview()

    assert states(data)[LUMEN] == ("swarm", None)
    assert figure(data, "stacks", "swarm running (1 unreadable)").value is None
