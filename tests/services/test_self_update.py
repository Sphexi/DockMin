"""Replacing dockmin's own container: the request, the plan and helper, and the recorded outcome."""

import asyncio
import json
import stat

import pytest
from httpx import AsyncClient

from app.config import ENV_PREFIX, ConfigError, load_settings
from app.db.repositories.activity_event import MAX_DETAIL_TEXT
from app.services.self_update import finish_updates, reserve_update
from tests.auth_support import ADMIN_ID
from tests.docker_engine.container_engine import ContainerEngine
from tests.updater.hosting import (
    DATA_VOLUME,
    FOREIGN_DIGEST,
    GROUP,
    HELPER_ID,
    HELPER_REPOSITORY,
    IMAGE_ENVIRONMENT,
    OTHER_IMAGE_ID,
    OTHER_UPDATE_ID,
    PORT_BINDINGS,
    REPOSITORY,
    RESTART_POLICY,
    SELF_LABELS,
    SOCKET_PATH,
    STOP_TIMEOUT,
    SWEEP_ID,
    TARGET_ID,
    TARGET_IMAGE,
    TARGET_TAG,
    UPDATE_ID,
    Hosting,
    Registry,
    add_helper,
    confirmation,
    install_registry,
    make_selfhost,
    reaches,
    request_update,
    seed_result,
    seed_update,
    until,
    update_id_of,
)
from tests.web.environment_support import World
from tests.web.self_support import SELF_ID, SELF_NAME
from updater.plan import read_plan
from updater.result import Outcome, Reason

REQUESTED = "action = 'instance_update_requested'"
UPDATE_ROWS = "action LIKE 'instance_update%' OR action = 'image_pulled'"
OUTCOME_ROWS = (
    "action IN ('instance_updated', 'instance_update_rolled_back', 'instance_update_abandoned')"
)
SETTING = "DOCKMIN_UPDATE_HEALTH_TIMEOUT_SECONDS"
CREATED_BY_HELPER = "c" * 64
EXPECTED_ENVIRONMENT = ["DOCKMIN_SECRET_KEY_FILE=/run/key", "DOCKMIN_PORT=8080"]
MAX_STORED_EXCERPT_LINES = 200
EXCERPT_LINE = "a line of the new container's log that is thirty-odd characters long"
OLD_PREVIOUS = f"{SELF_NAME}-previous"


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Registry:
    return install_registry(monkeypatch)


@pytest.fixture
async def selfhost(world: World, container_host: ContainerEngine, registry: Registry) -> Hosting:
    return await make_selfhost(world, container_host, registry)


async def start_update(client: AsyncClient, hosting: Hosting) -> str:
    """Review and request an update to the target tag; return its id once the helper runs."""
    hosting.dispatcher()
    page = await confirmation(client)
    response = await request_update(client, page)
    assert response.status_code == 200, response.text
    return update_id_of(response)


class TestRequest:
    async def test_the_request_row_exists_before_anything_is_pulled_or_created(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        requests_seen: list[int] = []

        async def note() -> None:
            requests_seen.append(len(await selfhost.rows(REQUESTED)))

        selfhost.on_pull.append(note)

        update_id = await start_update(admin_client, selfhost)
        await reaches(selfhost.app, update_id, "handed_over")

        rows = await selfhost.rows(UPDATE_ROWS)
        assert requests_seen == [1, 1]
        assert [(r["action"], r["result"]) for r in rows] == [
            ("instance_update_requested", "success"),
            ("image_pulled", "success"),
            ("image_pulled", "success"),
        ]
        assert selfhost.host.verbs() == ["pull", "pull", "create", "start"]

    async def test_the_status_moves_from_pulling_the_image_to_pulling_the_helper_to_handed_over(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        phases: list[str] = []
        dispatcher = selfhost.dispatcher()

        async def note() -> None:
            [update_id] = selfhost.update_ids()
            progress = dispatcher.progress(update_id)
            phases.append(progress.phase if progress else "unknown")

        selfhost.on_pull.append(note)
        page = await confirmation(admin_client)

        response = await request_update(admin_client, page)
        final = await reaches(selfhost.app, update_id_of(response), "handed_over")

        assert phases == ["pulling_target", "pulling_helper"]
        assert final.phase == "handed_over"

    async def test_both_images_are_pulled_by_tag_and_the_helper_runs_from_the_pulled_image_id(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        update_id = await start_update(admin_client, selfhost)
        await reaches(selfhost.app, update_id, "handed_over")

        [created] = selfhost.host.bodies()
        assert selfhost.pulled == [
            f"{REPOSITORY}:{TARGET_TAG}",
            f"{HELPER_REPOSITORY}:{TARGET_TAG}",
        ]
        assert created["Image"] == HELPER_ID
        assert [(b.image, b.image_id) for b in selfhost.helpers()] == [(HELPER_ID, HELPER_ID)]

    async def test_the_helper_gets_the_socket_the_data_volume_the_group_and_no_extra_privilege(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        update_id = await start_update(admin_client, selfhost)
        await reaches(selfhost.app, update_id, "handed_over")

        [body] = selfhost.host.bodies()
        [helper] = selfhost.helpers()
        assert helper.name == f"{SELF_NAME}-updater-{update_id[:8]}"
        assert helper.state == "running"
        assert body == {
            "Image": HELPER_ID,
            "User": "10001:10001",
            "Env": [f"DOCKMIN_UPDATE_ID={update_id}"],
            "Labels": {"dockmin.role": "updater", "dockmin.update.id": update_id},
            "HostConfig": {
                "Mounts": [
                    {"Type": "bind", "Source": SOCKET_PATH, "Target": "/var/run/docker.sock"},
                    {"Type": "volume", "Source": DATA_VOLUME, "Target": "/data"},
                ],
                "GroupAdd": [GROUP],
                "NetworkMode": "none",
                "SecurityOpt": ["no-new-privileges:true"],
                "CapDrop": ["ALL"],
            },
        }

    async def test_the_plan_is_private_and_matches_the_documented_schema(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        update_id = await start_update(admin_client, selfhost)
        await reaches(selfhost.app, update_id, "handed_over")

        directory = selfhost.update_root / update_id
        plan = read_plan(directory / "plan.json")
        document = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
        assert stat.S_IMODE((directory / "plan.json").stat().st_mode) == 0o600
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(selfhost.update_root.stat().st_mode) == 0o700
        assert set(document) == {
            "schema_version",
            "update_id",
            "created_at",
            "requested_by_user_id",
            "request_event_id",
            "engine_api_version",
            "old",
            "new",
            "health_timeout_seconds",
            "database_files",
        }
        assert (plan.update_id, plan.requested_by_user_id) == (update_id, ADMIN_ID)
        assert (plan.old.id, plan.old.name, plan.old.stop_timeout) == (
            SELF_ID,
            SELF_NAME,
            STOP_TIMEOUT,
        )
        assert (plan.new.name, plan.new.image, plan.new.image_id) == (
            SELF_NAME,
            TARGET_IMAGE,
            TARGET_ID,
        )
        assert plan.health_timeout_seconds == 180
        assert plan.database_files == ("dockmin.db", "dockmin.db-wal", "dockmin.db-shm")
        assert plan.request_event_id == await selfhost.event_id("instance_update_requested")

    async def test_the_replacement_is_derived_from_the_running_container_except_for_its_image(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        update_id = await start_update(admin_client, selfhost)
        await reaches(selfhost.app, update_id, "handed_over")

        body = read_plan(selfhost.update_root / update_id / "plan.json").new.create_body
        old = selfhost.own.inspect()
        assert body["Image"] == TARGET_IMAGE != old["Config"]["Image"]
        assert body["HostConfig"] == old["HostConfig"]
        assert body["HostConfig"]["Binds"] == old["HostConfig"]["Binds"]
        assert body["HostConfig"]["RestartPolicy"] == RESTART_POLICY
        assert body["HostConfig"]["PortBindings"] == PORT_BINDINGS
        assert body["HostConfig"]["GroupAdd"] == [GROUP]
        assert body["Labels"] == SELF_LABELS
        assert body["Env"] == EXPECTED_ENVIRONMENT
        assert body["StopTimeout"] == STOP_TIMEOUT
        assert set(body["NetworkingConfig"]["EndpointsConfig"]) == {"bridge"}
        assert IMAGE_ENVIRONMENT[0] not in body["Env"]

    async def test_a_pull_that_fails_abandons_the_update_and_leaves_no_trace_on_the_host(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        selfhost.host.pull_status = 500

        update_id = await start_update(admin_client, selfhost)
        progress = await reaches(selfhost.app, update_id, "abandoned")

        await until(lambda: not any(selfhost.update_root.iterdir()))
        rows = await selfhost.rows("action = 'instance_update_abandoned'")
        assert progress.reason == "pull_failed"
        assert [(r["result"], r["reason"]) for r in rows] == [("failure", "pull_failed")]
        assert selfhost.helpers() == []
        assert selfhost.host.verbs() == ["pull"]

    @pytest.mark.parametrize("which", ["target", "helper"])
    async def test_an_image_pulled_with_another_digest_than_the_reviewed_one_abandons_the_update(
        self, admin_client: AsyncClient, selfhost: Hosting, which: str
    ) -> None:
        selfhost.served[which] = FOREIGN_DIGEST

        update_id = await start_update(admin_client, selfhost)
        progress = await reaches(selfhost.app, update_id, "abandoned")

        await until(lambda: not any(selfhost.update_root.iterdir()))
        assert progress.reason == "digest_changed"
        assert selfhost.helpers() == []
        assert "create" not in selfhost.host.verbs()
        assert [
            r["reason"] for r in await selfhost.rows("action = 'instance_update_abandoned'")
        ] == ["digest_changed"]

    async def test_a_reference_that_no_longer_holds_the_reviewed_digest_abandons_before_the_plan(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        def retag() -> None:
            selfhost.host.add_image(
                TARGET_IMAGE, "sha256:" + "9" * 64, digests=(f"{REPOSITORY}@{FOREIGN_DIGEST}",)
            )

        selfhost.host.on("info", retag)

        update_id = await start_update(admin_client, selfhost)
        progress = await reaches(selfhost.app, update_id, "abandoned")

        await until(lambda: not any(selfhost.update_root.iterdir()))
        assert progress.reason == "digest_changed"
        assert selfhost.helpers() == []

    async def test_a_helper_that_cannot_start_is_removed_and_the_update_abandoned(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        selfhost.host.fail("start", 500)

        update_id = await start_update(admin_client, selfhost)
        progress = await reaches(selfhost.app, update_id, "abandoned")

        await until(lambda: not any(selfhost.update_root.iterdir()))
        assert progress.reason == "helper_failed"
        assert selfhost.helpers() == []
        assert selfhost.host.verbs() == ["pull", "pull", "create", "start", "remove"]
        assert SELF_ID in selfhost.host.boxes and selfhost.own.state == "running"

    async def test_an_update_claimed_but_never_dispatched_gives_back_its_directory(
        self, selfhost: Hosting
    ) -> None:
        dispatcher = selfhost.dispatcher()
        reserve_update(selfhost.data_dir, UPDATE_ID)
        dispatcher.claim(UPDATE_ID)

        dispatcher.discard(UPDATE_ID)

        assert not dispatcher.busy()
        assert list(selfhost.update_root.iterdir()) == []

    async def test_a_directory_that_was_never_claimed_is_left_alone_by_discard(
        self, selfhost: Hosting
    ) -> None:
        dispatcher = selfhost.dispatcher()
        reserve_update(selfhost.data_dir, UPDATE_ID)

        dispatcher.discard(UPDATE_ID)

        assert [p.name for p in selfhost.update_root.iterdir()] == [UPDATE_ID]
        assert stat.S_IMODE((selfhost.update_root / UPDATE_ID).stat().st_mode) == 0o700


OUTCOMES = [
    pytest.param("succeeded", None, "instance_updated", "success", None, False, id="succeeded"),
    pytest.param(
        "rolled_back",
        "health_timeout",
        "instance_update_rolled_back",
        "failure",
        "health_timeout",
        False,
        id="rolled-back",
    ),
    pytest.param(
        "rolled_back",
        "image_changed",
        "instance_update_rolled_back",
        "failure",
        "image_changed",
        False,
        id="rolled-back-image-changed",
    ),
    pytest.param(
        "rollback_failed",
        "unhealthy",
        "instance_update_rolled_back",
        "failure",
        "rollback_failed",
        True,
        id="rollback-failed",
    ),
    pytest.param(
        "not_started",
        "previous_exists",
        "instance_update_abandoned",
        "failure",
        "previous_exists",
        False,
        id="not-started",
    ),
    pytest.param(
        "not_started",
        "locked",
        "instance_update_abandoned",
        "failure",
        "locked",
        False,
        id="locked",
    ),
]


class TestCompletion:
    @pytest.mark.parametrize(
        ("outcome", "reason", "action", "result", "recorded", "kept"), OUTCOMES
    )
    async def test_the_helpers_result_is_recorded_as_the_matching_row_for_the_requesting_user(
        self,
        selfhost: Hosting,
        outcome: Outcome,
        reason: Reason | None,
        action: str,
        result: str,
        recorded: str | None,
        kept: bool,
    ) -> None:
        selfhost.dispatcher()
        seed_update(selfhost, outcome=outcome, reason=reason)

        await finish_updates(selfhost.app)

        [row] = await selfhost.rows(OUTCOME_ROWS)
        [detail] = await selfhost.details(action)
        assert (row["action"], row["result"], row["reason"]) == (action, result, recorded)
        assert detail["update_id"] == UPDATE_ID and detail["request_event_id"] == 1
        assert detail["container_name"] == SELF_NAME
        assert row["target_id"] == (CREATED_BY_HELPER if outcome == "succeeded" else SELF_ID)
        async with selfhost.app.state.database.session() as db:
            actor = await db.fetch_one(
                "SELECT actor_user_id FROM activity_event WHERE action = ?", (action,)
            )
        assert actor["actor_user_id"] == ADMIN_ID

    @pytest.mark.parametrize(
        ("outcome", "reason", "action", "result", "recorded", "kept"), OUTCOMES
    )
    async def test_the_plan_goes_on_every_terminal_path_and_the_directory_unless_rollback_failed(
        self,
        selfhost: Hosting,
        outcome: Outcome,
        reason: Reason | None,
        action: str,
        result: str,
        recorded: str | None,
        kept: bool,
    ) -> None:
        selfhost.dispatcher()
        directory = seed_update(selfhost, outcome=outcome, reason=reason)
        add_helper(selfhost, UPDATE_ID, 0x501, "exited")

        await finish_updates(selfhost.app)

        assert not (directory / "plan.json").exists()
        assert directory.exists() == kept
        assert [b.name for b in selfhost.helpers()] == (
            [f"{SELF_NAME}-updater-abababab"] if kept else []
        )

    async def test_a_rollback_that_failed_names_its_cause_and_keeps_the_database_copy(
        self, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        directory = seed_update(selfhost, outcome="rollback_failed", reason="exited")
        (directory / "db-before").mkdir()
        (directory / "db-before" / "dockmin.db").write_bytes(b"copy")

        await finish_updates(selfhost.app)

        [detail] = await selfhost.details("instance_update_rolled_back")
        assert (detail["reason"], detail["cause"]) == ("rollback_failed", "exited")
        assert (directory / "db-before" / "dockmin.db").read_bytes() == b"copy"

    async def test_the_outcome_is_recorded_once_however_many_times_it_is_found(
        self, selfhost: Hosting
    ) -> None:
        dispatcher = selfhost.dispatcher()
        selfhost.host.fail("remove", 500, times=1)
        directory = seed_update(selfhost, outcome="succeeded")
        add_helper(selfhost, UPDATE_ID, 0x501, "exited")

        await asyncio.gather(dispatcher.complete(UPDATE_ID), dispatcher.complete(UPDATE_ID))
        left = directory.exists() and (directory / "recorded").exists()
        await finish_updates(selfhost.app)
        await finish_updates(selfhost.app)

        assert left
        assert [r["action"] for r in await selfhost.rows(OUTCOME_ROWS)] == ["instance_updated"]
        assert not directory.exists()

    async def test_an_outcome_is_not_recorded_by_a_process_that_is_shutting_down(
        self, selfhost: Hosting
    ) -> None:
        dispatcher = selfhost.dispatcher()
        directory = seed_update(selfhost, outcome="succeeded")

        dispatcher.mark_stopping()
        await dispatcher.complete(UPDATE_ID)

        assert await selfhost.rows(OUTCOME_ROWS) == []
        assert directory.exists()

    async def test_the_scrubbed_log_excerpt_is_stored_for_the_page_and_cut_to_fit_the_row(
        self, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        lines = tuple(f"{EXCERPT_LINE} {number}" for number in range(MAX_STORED_EXCERPT_LINES))
        secret = ("the key is password=hunter2 here",)
        seed_update(
            selfhost, outcome="rolled_back", reason="unhealthy", excerpt=(*secret, *lines[1:])
        )

        await finish_updates(selfhost.app)

        [detail] = await selfhost.details("instance_update_rolled_back")
        async with selfhost.app.state.database.session() as db:
            stored = await db.fetch_one(
                "SELECT value FROM setting WHERE key = 'instance_update_outcome'"
            )
        document = json.loads(stored["value"])
        assert len(detail["log_excerpt"]) <= MAX_DETAIL_TEXT
        assert detail["log_excerpt"].endswith(lines[-1])
        assert document["excerpt"][-1] == lines[-1]
        assert len(document["excerpt"]) == MAX_STORED_EXCERPT_LINES
        assert "hunter2" not in stored["value"] and "hunter2" not in json.dumps(detail)
        assert (document["outcome"], document["reason"]) == ("rolled_back", "unhealthy")
        assert document["event_id"] == await selfhost.event_id("instance_update_rolled_back")

    async def test_a_succeeded_update_that_left_the_previous_container_says_so(
        self, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        selfhost.host.add("f" * 64, OLD_PREVIOUS, state="exited")
        seed_update(selfhost, outcome="succeeded")

        await finish_updates(selfhost.app)

        [detail] = await selfhost.details("instance_updated")
        assert detail["previous_container_left"] is True

    async def test_an_unreadable_result_is_recorded_and_its_directory_kept_for_recovery(
        self, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        directory = seed_update(selfhost)
        (directory / "result.json").write_text("{not json", encoding="utf-8")

        await finish_updates(selfhost.app)

        rows = await selfhost.rows(OUTCOME_ROWS)
        assert [(r["action"], r["reason"]) for r in rows] == [
            ("instance_update_abandoned", "result_unreadable")
        ]
        assert directory.is_dir()


class TestHelperRemoval:
    async def test_only_the_container_with_the_labels_the_name_and_the_helper_image_is_removed(
        self, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        seed_update(selfhost, outcome="succeeded")
        add_helper(selfhost, UPDATE_ID, 0x501, "exited")
        add_helper(selfhost, UPDATE_ID, 0x502, "exited", name="someone-elses-updater")
        add_helper(
            selfhost,
            UPDATE_ID,
            0x503,
            "exited",
            image=f"{REPOSITORY}-other:1",
            image_id=OTHER_IMAGE_ID,
        )
        add_helper(
            selfhost, UPDATE_ID, 0x504, "exited", name="dockmin-self-previous", labelled=False
        )
        add_helper(selfhost, OTHER_UPDATE_ID, 0x505, "running")

        await finish_updates(selfhost.app)

        assert selfhost.others() == [
            f"{0x502:064x}",
            f"{0x503:064x}",
            f"{0x504:064x}",
            f"{0x505:064x}",
        ]

    async def test_a_helper_that_is_still_running_is_waited_for_and_then_removed_by_force(
        self, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        seed_update(selfhost, outcome="succeeded")
        add_helper(selfhost, UPDATE_ID, 0x501, "running")

        await finish_updates(selfhost.app)

        [removal] = selfhost.host.calls("DELETE")
        assert removal.query["force"] == "true"
        assert selfhost.helpers() == []

    async def test_a_helper_that_cannot_be_removed_keeps_the_directory_for_the_next_start(
        self, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        selfhost.host.fail("remove", 500)
        directory = seed_update(selfhost, outcome="succeeded")
        add_helper(selfhost, UPDATE_ID, 0x501, "exited")

        await finish_updates(selfhost.app)

        assert directory.exists() and not (directory / "plan.json").exists()
        assert len(selfhost.helpers()) == 1
        assert len(await selfhost.rows(OUTCOME_ROWS)) == 1

    async def test_stale_helpers_are_swept_when_no_update_directory_names_them(
        self, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        add_helper(selfhost, SWEEP_ID, 0x501, "exited")
        add_helper(
            selfhost, SWEEP_ID, 0x502, "running", name=f"{SELF_NAME}-updater-{SWEEP_ID[:8]}x"
        )
        add_helper(selfhost, OTHER_UPDATE_ID, 0x503, "running")
        add_helper(
            selfhost,
            OTHER_UPDATE_ID,
            0x504,
            "exited",
            image=f"{REPOSITORY}-other:1",
            image_id=OTHER_IMAGE_ID,
        )

        await finish_updates(selfhost.app)

        assert selfhost.others() == [
            f"{0x502:064x}",
            f"{0x503:064x}",
            f"{0x504:064x}",
        ]


class TestRecovery:
    @pytest.mark.parametrize("state", [None, "exited"])
    async def test_an_update_with_no_result_and_no_live_helper_is_resolved_as_abandoned_at_start(
        self, selfhost: Hosting, state: str | None
    ) -> None:
        selfhost.dispatcher()
        directory = seed_update(selfhost)
        if state:
            add_helper(selfhost, UPDATE_ID, 0x501, state)

        await finish_updates(selfhost.app)

        rows = await selfhost.rows(OUTCOME_ROWS)
        assert [(r["action"], r["result"], r["reason"]) for r in rows] == [
            ("instance_update_abandoned", "failure", "helper_lost")
        ]
        assert not directory.exists()

    async def test_a_running_helper_is_watched_and_its_result_is_recorded_when_it_appears(
        self, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        directory = seed_update(selfhost)
        helper = add_helper(selfhost, UPDATE_ID, 0x501, "running")
        await finish_updates(selfhost.app)
        waiting = await selfhost.rows(OUTCOME_ROWS)

        helper.state = "exited"
        seed_result(directory, "succeeded")
        await until(lambda: not directory.exists())

        assert waiting == []
        assert [r["action"] for r in await selfhost.rows(OUTCOME_ROWS)] == ["instance_updated"]

    async def test_a_helper_that_disappears_without_a_result_is_noticed_by_the_watcher(
        self, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        directory = seed_update(selfhost)
        helper = add_helper(selfhost, UPDATE_ID, 0x501, "running")
        await finish_updates(selfhost.app)

        del selfhost.host.boxes[helper.id]
        await until(lambda: not directory.exists())

        rows = await selfhost.rows(OUTCOME_ROWS)
        assert [(r["action"], r["reason"]) for r in rows] == [
            ("instance_update_abandoned", "helper_lost")
        ]

    async def test_recovery_that_cannot_reach_the_host_records_nothing_and_does_not_raise(
        self, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        directory = seed_update(selfhost)
        selfhost.host.listing_status = 500

        await finish_updates(selfhost.app)

        assert await selfhost.rows(OUTCOME_ROWS) == []
        assert directory.exists()


@pytest.mark.parametrize("value", ["30", "180", "1800"])
def test_the_health_timeout_setting_accepts_thirty_to_eighteen_hundred_seconds(
    env: dict[str, str], value: str
) -> None:
    assert load_settings(env | {SETTING: value}).update_health_timeout_seconds == int(value)


@pytest.mark.parametrize("value", ["29", "1801", "0", "-1", "soon", ""])
def test_the_health_timeout_setting_refuses_values_outside_its_range(
    env: dict[str, str], value: str
) -> None:
    with pytest.raises(ConfigError) as refused:
        load_settings(env | {SETTING: value})

    assert refused.value.variables == (f"{ENV_PREFIX}UPDATE_HEALTH_TIMEOUT_SECONDS",)


def test_the_health_timeout_defaults_to_a_hundred_and_eighty_seconds(env: dict[str, str]) -> None:
    assert load_settings(env).update_health_timeout_seconds == 180
