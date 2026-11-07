"""The Instance page and the restart of the container dockmin runs in."""

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.self_identity import SelfIdentity
from app.services.container_derivation import BLOCKER_MESSAGES
from app.services.instance import (
    DEFAULT_STOP_TIMEOUT,
    RESTART_SEND_DELAY_SECONDS,
    RestartDispatcher,
)
from app.services.self_update import UPDATE_REFUSALS, finish_updates
from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.updater.hosting import (
    ANONYMOUS,
    CHECK,
    CONFIRM,
    FOREIGN_DIGEST,
    HELPER_DIGEST,
    HELPER_IMAGE,
    REQUEST,
    RUNNING_DIGEST,
    RUNNING_IMAGE,
    TARGET_DIGEST,
    TARGET_IMAGE,
    TARGET_TAG,
    UPDATE_ID,
    Hosting,
    Registry,
    confirmation,
    form_actions,
    install_registry,
    make_selfhost,
    reaches,
    request_fields,
    request_update,
    seed_update,
    update_id_of,
)
from tests.web.environment_support import DOWN_NAME, LOCAL_NAME, World
from tests.web.image_support import assert_no_store, hidden_values, links_in, text_of
from tests.web.self_support import (
    SELF_ID,
    SELF_NAME,
    Pauses,
    adopt,
    events,
    identity_of,
    instant_dispatch,
    seed_self,
    settle,
)

OK, FORBIDDEN, CONFLICT, UNAVAILABLE, BAD_REQUEST = 200, 403, 409, 503, 400
INSTANCE = "/server/instance"
RESTART = "/server/instance/restart"
BOOT = "/server/instance/boot"
RESTART_PATH = f"/containers/{SELF_ID}/restart"
CHANGED_SINCE_REVIEW = "The container dockmin runs in changed since you reviewed it."
RESTARTED_SINCE_REVIEW = "dockmin has restarted since this page was shown."
NO_LOCAL_HOME = "could not confirm its container through an environment on the local Docker socket"
NO_CONTAINER = "dockmin could not identify a container of its own"
REQUESTED = "action = 'instance_restart_requested'"


@pytest.fixture
async def running(
    world: World, container_host: ContainerEngine
) -> tuple[RestartDispatcher, Pauses]:
    """A dockmin whose container the local environment reports, restarting without waiting."""
    seed_self(container_host)
    adopt(world.app, identity_of(world))
    return instant_dispatch(world.app)


@pytest.fixture
async def restoring(world: World) -> AsyncIterator[None]:
    gate = world.app.state.restore_gate
    gate.engage("restore-1", datetime.now(UTC), purpose="restore")
    yield
    gate.reopen()


def restart_form(client: AsyncClient, page_boot_id: str, **fields: str) -> dict[str, str]:
    return csrf_form(client, confirm="yes", container_id=SELF_ID, boot_id=page_boot_id, **fields)


def boot_id_of(app: FastAPI) -> str:
    return str(app.state.boot.boot_id)


async def test_a_standard_user_is_refused_every_instance_route(
    standard_client: AsyncClient, world: World
) -> None:
    pages = [
        await standard_client.get(INSTANCE),
        await standard_client.get(RESTART),
        await standard_client.get(BOOT),
        await standard_client.post(RESTART, data=csrf_form(standard_client, confirm="yes")),
    ]

    assert [page.status_code for page in pages] == [FORBIDDEN] * 4


async def test_the_page_names_the_container_how_it_was_found_and_where_it_is_reported(
    admin_client: AsyncClient, world: World, running: tuple[RestartDispatcher, Pauses]
) -> None:
    page = await admin_client.get(INSTANCE)

    assert page.status_code == OK
    assert SELF_NAME in page.text and SELF_ID in page.text
    assert "Mounts of /etc/hostname, /etc/hosts and /etc/resolv.conf" in text_of(page)
    assert LOCAL_NAME in text_of(page) and "Used for restart" in text_of(page)
    assert f"/environments/{world.local_id}" in links_in(page)
    assert RESTART in links_in(page)
    assert NO_CONTAINER not in text_of(page)
    assert_no_store(page)


async def test_the_page_says_dockmin_found_no_container_of_its_own_and_offers_no_restart(
    admin_client: AsyncClient, world: World
) -> None:
    adopt(world.app, SelfIdentity())

    page = await admin_client.get(INSTANCE)

    assert page.status_code == OK
    assert NO_CONTAINER in text_of(page)
    assert RESTART not in links_in(page)
    assert SELF_NAME not in page.text


async def test_the_page_lists_the_environments_that_could_not_be_read_when_incomplete(
    admin_client: AsyncClient, world: World
) -> None:
    adopt(world.app, identity_of(world, unreadable=[world.down_id]))

    page = await admin_client.get(INSTANCE)

    assert "Some environments could not be read" in text_of(page)
    assert DOWN_NAME in text_of(page)


async def test_the_page_explains_a_restart_that_has_no_local_socket_home(
    admin_client: AsyncClient, world: World
) -> None:
    adopt(world.app, identity_of(world, restartable=False))

    page = await admin_client.get(INSTANCE)

    assert NO_LOCAL_HOME in text_of(page)
    assert RESTART not in links_in(page)


async def test_the_confirmation_posts_the_container_and_the_run_it_was_reviewed_against(
    admin_client: AsyncClient, world: World, running: tuple[RestartDispatcher, Pauses]
) -> None:
    page = await admin_client.get(RESTART)

    assert page.status_code == OK
    assert hidden_values(page, "container_id") == [SELF_ID]
    assert hidden_values(page, "boot_id") == [boot_id_of(world.app)]
    assert hidden_values(page, "confirm") == ["yes"]
    assert SELF_NAME in page.text
    assert_no_store(page)


async def test_a_restart_that_does_not_match_the_review_is_refused_and_nothing_is_sent(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    running: tuple[RestartDispatcher, Pauses],
) -> None:
    dispatcher, _ = running
    boot_id = boot_id_of(world.app)

    no_boot = await admin_client.post(
        RESTART, data=csrf_form(admin_client, confirm="yes", container_id=SELF_ID)
    )
    stale_boot = await admin_client.post(
        RESTART, data=restart_form(admin_client, "0" * len(boot_id))
    )
    other_container = await admin_client.post(
        RESTART,
        data=csrf_form(admin_client, confirm="yes", container_id="f" * 64, boot_id=boot_id),
    )
    unconfirmed = await admin_client.post(
        RESTART, data=csrf_form(admin_client, container_id=SELF_ID, boot_id=boot_id)
    )
    await settle(dispatcher)

    assert (no_boot.status_code, stale_boot.status_code) == (CONFLICT, CONFLICT)
    assert RESTARTED_SINCE_REVIEW in text_of(no_boot)
    assert RESTARTED_SINCE_REVIEW in text_of(stale_boot)
    assert other_container.status_code == CONFLICT
    assert CHANGED_SINCE_REVIEW in text_of(other_container)
    assert unconfirmed.status_code == BAD_REQUEST
    assert container_host.calls("POST", RESTART_PATH) == []
    assert await events(world.app, REQUESTED) == []


async def test_a_restart_without_a_local_socket_home_is_refused_and_nothing_is_sent(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    seed_self(container_host)
    adopt(world.app, identity_of(world, restartable=False))
    dispatcher, _ = instant_dispatch(world.app)

    review = await admin_client.get(RESTART)
    refused = await admin_client.post(
        RESTART, data=restart_form(admin_client, boot_id_of(world.app))
    )
    await settle(dispatcher)

    assert (review.status_code, refused.status_code) == (CONFLICT, CONFLICT)
    assert NO_LOCAL_HOME in text_of(refused)
    assert container_host.calls("POST", RESTART_PATH) == []
    assert await events(world.app, REQUESTED) == []


async def test_a_restart_is_refused_while_a_restore_holds_the_instance(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    running: tuple[RestartDispatcher, Pauses],
    restoring: None,
) -> None:
    dispatcher, _ = running

    review = await admin_client.get(RESTART)
    post = await admin_client.post(RESTART, data=restart_form(admin_client, boot_id_of(world.app)))
    await settle(dispatcher)

    assert review.status_code == CONFLICT
    assert "A restore is in progress" in text_of(review)
    assert post.status_code == UNAVAILABLE
    assert container_host.calls("POST", RESTART_PATH) == []
    assert await events(world.app, REQUESTED) == []


async def test_a_restart_whose_record_cannot_be_written_answers_503_and_sends_nothing(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    running: tuple[RestartDispatcher, Pauses],
) -> None:
    dispatcher, _ = running
    async with world.app.state.database.session() as db:
        await db.execute(
            "CREATE TRIGGER refuse_restart_row BEFORE INSERT ON activity_event "
            "WHEN NEW.action = 'instance_restart_requested' "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )

    refused = await admin_client.post(
        RESTART, data=restart_form(admin_client, boot_id_of(world.app))
    )
    await settle(dispatcher)

    assert refused.status_code == UNAVAILABLE
    assert "could not be recorded" in text_of(refused)
    assert container_host.calls("POST", RESTART_PATH) == []
    assert await events(world.app, REQUESTED) == []


async def test_a_confirmed_restart_is_recorded_answers_the_restarting_page_and_is_sent_once(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    running: tuple[RestartDispatcher, Pauses],
) -> None:
    dispatcher, pauses = running
    boot_id = boot_id_of(world.app)

    response = await admin_client.post(RESTART, data=restart_form(admin_client, boot_id))
    await settle(dispatcher)

    [sent] = container_host.calls("POST", RESTART_PATH)
    assert response.status_code == OK
    assert "Restarting dockmin" in text_of(response) and SELF_NAME in text_of(response)
    assert f'data-boot-id="{boot_id}"' in response.text
    assert f'hx-get="{BOOT}"' in response.text
    assert_no_store(response)
    assert pauses.asked == [RESTART_SEND_DELAY_SECONDS]
    assert sent.query["t"] == str(DEFAULT_STOP_TIMEOUT)
    assert [(row["action"], row["result"]) for row in await events(world.app, REQUESTED)] == [
        ("instance_restart_requested", "success")
    ]


async def test_the_boot_fragment_carries_the_run_identifier_and_is_never_cached(
    admin_client: AsyncClient, world: World
) -> None:
    response = await admin_client.get(BOOT)

    assert response.status_code == OK
    assert response.text == f'<span id="boot-id">{boot_id_of(world.app)}</span>'
    assert_no_store(response)


UPDATE_REQUESTED = "action = 'instance_update_requested'"
UPDATE_REQUESTED_OK = f"{UPDATE_REQUESTED} AND result = 'success'"
OUTCOME_ROWS = "action IN ('instance_updated', 'instance_update_rolled_back')"
OFFERED_WITH_NEW_DIGEST = ["1.0.0", "1.2.0", "1.1.0"]
OFFERED_NEWER = ["1.2.0", "1.1.0"]
NOT_OFFERED_TAG = ("0.9.0", "latest", "1.1.0-rc1", "not a tag")
STOP_SECONDS = 30
HEALTH_SECONDS = 180
IN_PROGRESS = "An update is in progress"
SECRET_IN_LOG = "password=hunter2"
STALE = UPDATE_REFUSALS["stale_review"]
DIGEST_MOVED = UPDATE_REFUSALS["digest_changed"]
NOT_OFFERED = UPDATE_REFUSALS["target_invalid"]
UNCONFIRMED = "Confirm the update to continue."


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Registry:
    return install_registry(monkeypatch)


@pytest.fixture
async def selfhost(world: World, container_host: ContainerEngine, registry: Registry) -> Hosting:
    return await make_selfhost(world, container_host, registry)


type Setup = Callable[[Hosting], None]


def no_container_identity(hosting: Hosting) -> None:
    adopt(hosting.app, SelfIdentity())


def incomplete_identity(hosting: Hosting) -> None:
    adopt(hosting.app, identity_of(hosting.world, unreadable=[hosting.world.down_id]))


def no_local_home(hosting: Hosting) -> None:
    adopt(hosting.app, identity_of(hosting.world, restartable=False))


def compose_project(hosting: Hosting) -> None:
    home = identity_of(hosting.world)
    adopt(hosting.app, dataclasses.replace(home, compose_project={hosting.world.local_id: "stack"}))


def swarm_task(hosting: Hosting) -> None:
    home = identity_of(hosting.world)
    adopt(hosting.app, dataclasses.replace(home, swarm_service={hosting.world.local_id: "service"}))


def without_mount(kind: str) -> Setup:
    def setup(hosting: Hosting) -> None:
        hosting.own.mounts = [m for m in hosting.own.mounts if m["Type"] != kind]

    return setup


def with_update_directory(hosting: Hosting) -> None:
    (hosting.update_root / UPDATE_ID).mkdir(parents=True)


def with_previous_container(hosting: Hosting) -> None:
    hosting.host.add("e" * 64, f"{SELF_NAME}-previous", state="exited")


def with_a_bare_image_id_reference(hosting: Hosting) -> None:
    hosting.own.config = {"Image": "a" * 64}


def built_on_this_host(hosting: Hosting) -> None:
    hosting.own.image = "dockmin:local"


def with_auto_remove(hosting: Hosting) -> None:
    assert hosting.own.host_config is not None
    hosting.own.host_config = {**hosting.own.host_config, "AutoRemove": True}


def gone_from_the_host(hosting: Hosting) -> None:
    del hosting.host.boxes[SELF_ID]


def unreadable_on_the_host(hosting: Hosting) -> None:
    hosting.host.fail("inspect", 500, target=SELF_ID)


REFUSALS: dict[str, tuple[Setup, str]] = {
    "not_containerised": (no_container_identity, UPDATE_REFUSALS["not_containerised"]),
    "identity_incomplete": (incomplete_identity, UPDATE_REFUSALS["identity_incomplete"]),
    "not_restartable": (no_local_home, UPDATE_REFUSALS["not_restartable"]),
    "compose_project": (compose_project, UPDATE_REFUSALS["compose_project"]),
    "swarm_task": (swarm_task, UPDATE_REFUSALS["swarm_task"]),
    "update_in_progress": (with_update_directory, UPDATE_REFUSALS["update_in_progress"]),
    "previous_exists": (
        with_previous_container,
        f"A container named {SELF_NAME}-previous already exists",
    ),
    "no_socket_mount": (without_mount("bind"), UPDATE_REFUSALS["no_socket_mount"]),
    "no_data_mount": (without_mount("volume"), UPDATE_REFUSALS["no_data_mount"]),
    "no_repository": (with_a_bare_image_id_reference, UPDATE_REFUSALS["no_repository"]),
    "no_digest": (built_on_this_host, UPDATE_REFUSALS["no_digest"]),
    "not_recreatable": (with_auto_remove, BLOCKER_MESSAGES["auto_remove"]),
    "container_missing": (gone_from_the_host, UPDATE_REFUSALS["container_missing"]),
    "container_unreadable": (unreadable_on_the_host, UPDATE_REFUSALS["container_unreadable"]),
}


class TestRegistryContact:
    async def test_no_page_load_contacts_a_registry_and_the_instance_page_offers_the_check(
        self, admin_client: AsyncClient, selfhost: Hosting, registry: Registry
    ) -> None:
        page = await admin_client.get(INSTANCE)
        loads = [
            await admin_client.get(path)
            for path in (CHECK, CONFIRM, REQUEST, f"{REQUEST}/{UPDATE_ID}", RESTART)
        ]

        assert page.status_code == OK
        assert CHECK in form_actions(page)
        assert registry.calls == []
        assert selfhost.host.verbs() == []
        assert [r.status_code for r in loads[:3]] == [404, 404, 405]

    async def test_the_check_asks_only_for_the_running_tags_digest_and_the_tag_list(
        self, admin_client: AsyncClient, selfhost: Hosting, registry: Registry
    ) -> None:
        checked = await admin_client.post(
            CHECK, data=csrf_form(admin_client, credential_choice=ANONYMOUS)
        )

        assert checked.status_code == OK
        assert sorted(registry.calls) == ["head example/dockmin:1.0.0", "tags example/dockmin"]
        assert selfhost.host.verbs() == []

    async def test_the_confirmation_reads_the_digest_of_the_target_and_of_the_helper_only(
        self, admin_client: AsyncClient, selfhost: Hosting, registry: Registry
    ) -> None:
        page = await confirmation(admin_client)

        assert page.status_code == OK
        assert sorted(registry.calls) == [
            "head example/dockmin-updater:1.2.0",
            "head example/dockmin:1.2.0",
        ]


class TestRefusals:
    @pytest.mark.parametrize("reason", sorted(REFUSALS))
    async def test_a_refusal_shows_its_message_with_no_check_button_and_the_check_is_refused_too(
        self,
        admin_client: AsyncClient,
        selfhost: Hosting,
        registry: Registry,
        reason: str,
    ) -> None:
        setup, message = REFUSALS[reason]
        setup(selfhost)

        page = await admin_client.get(INSTANCE)
        posted = await admin_client.post(
            CHECK, data=csrf_form(admin_client, credential_choice=ANONYMOUS)
        )

        assert page.status_code == OK
        assert message in text_of(page)
        assert CHECK not in form_actions(page)
        assert (posted.status_code, message in text_of(posted)) == (CONFLICT, True)
        assert registry.calls == []
        assert selfhost.host.verbs() == []

    async def test_a_compose_project_is_told_the_compose_commands_to_run_instead(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        compose_project(selfhost)

        page = await admin_client.get(INSTANCE)

        assert "docker compose pull" in page.text and "docker compose up -d" in page.text

    async def test_a_restore_in_progress_withdraws_the_check_and_the_gate_refuses_it(
        self,
        admin_client: AsyncClient,
        selfhost: Hosting,
        registry: Registry,
        restoring: None,
    ) -> None:
        page = await admin_client.get(INSTANCE)
        posted = await admin_client.post(
            CHECK, data=csrf_form(admin_client, credential_choice=ANONYMOUS)
        )

        message = UPDATE_REFUSALS["restore_in_progress"]
        assert message in text_of(page) and CHECK not in form_actions(page)
        assert posted.status_code == UNAVAILABLE
        assert registry.calls == []

    async def test_a_standard_user_reaches_none_of_the_update_routes(
        self, standard_client: AsyncClient, selfhost: Hosting, registry: Registry
    ) -> None:
        fields = csrf_form(standard_client, target_tag=TARGET_TAG, credential_choice=ANONYMOUS)

        responses = [
            await standard_client.post(CHECK, data=fields),
            await standard_client.post(CONFIRM, data=fields),
            await standard_client.post(REQUEST, data=fields),
            await standard_client.get(f"{REQUEST}/{UPDATE_ID}"),
        ]

        assert [r.status_code for r in responses] == [FORBIDDEN] * 4
        assert registry.calls == []
        assert selfhost.host.verbs() == []


class TestCheck:
    @pytest.mark.parametrize(
        ("running_digest", "offered"),
        [
            pytest.param(FOREIGN_DIGEST, OFFERED_WITH_NEW_DIGEST, id="same-tag-moved"),
            pytest.param(RUNNING_DIGEST, OFFERED_NEWER, id="same-tag-unchanged"),
        ],
    )
    async def test_the_check_offers_the_same_tag_at_a_new_digest_and_newer_release_tags_only(
        self,
        admin_client: AsyncClient,
        selfhost: Hosting,
        registry: Registry,
        running_digest: str,
        offered: list[str],
    ) -> None:
        registry.same_tag_digest = running_digest

        page = await admin_client.post(
            CHECK, data=csrf_form(admin_client, credential_choice=ANONYMOUS)
        )

        assert page.status_code == OK
        assert hidden_values(page, "target_tag") == offered
        assert hidden_values(page, "credential_choice") == [ANONYMOUS]
        assert CONFIRM in form_actions(page)
        assert_no_store(page)

    async def test_the_check_without_a_credential_choice_is_refused_before_the_registry(
        self, admin_client: AsyncClient, selfhost: Hosting, registry: Registry
    ) -> None:
        page = await admin_client.post(CHECK, data=csrf_form(admin_client))

        assert page.status_code == BAD_REQUEST
        assert registry.calls == []


class TestConfirmation:
    async def test_it_names_what_is_replaced_what_runs_instead_and_the_outage_and_changes_nothing(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        page = await confirmation(admin_client)

        text = text_of(page)
        named = (
            SELF_NAME,
            RUNNING_IMAGE,
            RUNNING_DIGEST,
            TARGET_IMAGE,
            TARGET_DIGEST,
            HELPER_IMAGE,
            HELPER_DIGEST,
            f"{STOP_SECONDS} seconds",
            f"{HEALTH_SECONDS} seconds",
        )
        assert page.status_code == OK
        assert [fact for fact in named if fact not in text] == []
        assert REQUEST in form_actions(page)
        assert hidden_values(page, "container_id") == [SELF_ID]
        assert hidden_values(page, "current_digest") == [RUNNING_DIGEST]
        assert hidden_values(page, "target_tag") == [TARGET_TAG]
        assert hidden_values(page, "target_digest") == [TARGET_DIGEST]
        assert hidden_values(page, "helper_digest") == [HELPER_DIGEST]
        assert hidden_values(page, "boot_id") == [str(selfhost.app.state.boot.boot_id)]
        assert hidden_values(page, "confirm") == ["yes"]
        assert selfhost.host.verbs() == []
        assert await selfhost.rows(UPDATE_REQUESTED) == []
        assert_no_store(page)

    @pytest.mark.parametrize("tag", NOT_OFFERED_TAG)
    async def test_a_tag_that_is_not_a_newer_release_is_refused_before_the_registry(
        self, admin_client: AsyncClient, selfhost: Hosting, registry: Registry, tag: str
    ) -> None:
        page = await confirmation(admin_client, tag)

        assert page.status_code == CONFLICT
        assert UPDATE_REFUSALS["target_invalid"] in text_of(page)
        assert registry.calls == []


class TestRequest:
    @pytest.mark.parametrize(
        ("field", "value", "status", "message"),
        [
            pytest.param("boot_id", "0" * 16, CONFLICT, STALE, id="restarted"),
            pytest.param("container_id", "f" * 64, CONFLICT, STALE, id="other-container"),
            pytest.param("current_digest", FOREIGN_DIGEST, CONFLICT, STALE, id="running-moved"),
            pytest.param("target_digest", FOREIGN_DIGEST, CONFLICT, DIGEST_MOVED, id="target"),
            pytest.param("helper_digest", FOREIGN_DIGEST, CONFLICT, DIGEST_MOVED, id="helper"),
            pytest.param("target_tag", "0.9.0", CONFLICT, NOT_OFFERED, id="older-tag"),
            pytest.param("confirm", "", BAD_REQUEST, UNCONFIRMED, id="unconfirmed"),
        ],
    )
    async def test_a_request_that_does_not_match_the_review_is_refused_and_starts_nothing(
        self,
        admin_client: AsyncClient,
        selfhost: Hosting,
        registry: Registry,
        field: str,
        value: str,
        status: int,
        message: str,
    ) -> None:
        page = await confirmation(admin_client)

        refused = await request_update(admin_client, page, **{field: value})

        assert refused.status_code == status
        assert message in text_of(refused)
        assert await selfhost.rows(UPDATE_REQUESTED_OK) == []
        assert selfhost.host.verbs() == []
        assert selfhost.update_ids() == []

    async def test_an_accepted_request_answers_the_restarting_page_with_the_status_address(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        page = await confirmation(admin_client)

        response = await request_update(admin_client, page)

        update_id = update_id_of(response)
        await reaches(selfhost.app, update_id, "handed_over")
        assert response.status_code == OK
        assert f"{REQUEST}/{update_id}" in response.text
        assert SELF_NAME in text_of(response)
        assert_no_store(response)
        [row] = await selfhost.rows(UPDATE_REQUESTED_OK)
        [detail] = await selfhost.details("instance_update_requested")
        assert row["target_id"] == SELF_ID
        assert detail["update_id"] == update_id
        assert (detail["target_reference"], detail["target_digest"]) == (
            TARGET_IMAGE,
            TARGET_DIGEST,
        )
        assert (detail["current_digest"], detail["helper_digest"]) == (
            RUNNING_DIGEST,
            HELPER_DIGEST,
        )

    async def test_two_simultaneous_requests_start_one_update_and_refuse_the_other(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        page = await confirmation(admin_client)
        fields = request_fields(page)

        first, second = await asyncio.gather(
            admin_client.post(REQUEST, data=csrf_form(admin_client, **fields)),
            admin_client.post(REQUEST, data=csrf_form(admin_client, **fields)),
        )

        accepted = next(r for r in (first, second) if r.status_code == OK)
        refused = next(r for r in (first, second) if r.status_code == CONFLICT)
        await reaches(selfhost.app, update_id_of(accepted), "handed_over")
        assert IN_PROGRESS in text_of(refused)
        assert len(await selfhost.rows(UPDATE_REQUESTED_OK)) == 1
        assert selfhost.update_ids() == [update_id_of(accepted)]
        assert len(selfhost.helpers()) == 1

    async def test_a_request_whose_record_cannot_be_written_answers_503_and_starts_nothing(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        page = await confirmation(admin_client)
        async with selfhost.app.state.database.session() as db:
            await db.execute(
                "CREATE TRIGGER refuse_update_row BEFORE INSERT ON activity_event "
                "WHEN NEW.action = 'instance_update_requested' "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )

        refused = await request_update(admin_client, page)

        assert refused.status_code == UNAVAILABLE
        assert UPDATE_REFUSALS["record_failed"] in text_of(refused)
        assert selfhost.host.verbs() == []
        assert selfhost.update_ids() == []
        assert not selfhost.app.state.update_dispatcher.busy()

    async def test_restart_is_refused_while_an_update_is_in_progress(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher().claim(UPDATE_ID)

        review = await admin_client.get(RESTART)
        posted = await admin_client.post(
            RESTART,
            data=csrf_form(
                admin_client,
                confirm="yes",
                container_id=SELF_ID,
                boot_id=str(selfhost.app.state.boot.boot_id),
            ),
        )

        assert review.status_code == CONFLICT and IN_PROGRESS in text_of(review)
        assert posted.status_code == CONFLICT
        assert selfhost.host.calls("POST", RESTART_PATH) == []
        assert await events(selfhost.app, REQUESTED) == []


class TestStatusAndOutcome:
    async def test_the_status_of_an_abandoned_update_carries_the_message_the_service_gave(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        selfhost.host.pull_status = 500
        page = await confirmation(admin_client)
        update_id = update_id_of(await request_update(admin_client, page))
        progress = await reaches(selfhost.app, update_id, "abandoned")

        status = await admin_client.get(f"{REQUEST}/{update_id}")

        assert status.status_code == OK and progress.message
        assert progress.message in text_of(status)
        assert_no_store(status)

    async def test_the_status_of_an_update_nothing_is_known_of_is_still_an_answer(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        status = await admin_client.get(f"{REQUEST}/{UPDATE_ID}")
        malformed = await admin_client.get(f"{REQUEST}/not-an-update-id")

        assert status.status_code == OK
        assert malformed.status_code == 404

    async def test_the_instance_page_shows_the_latest_outcome_scrubbed_and_links_its_rows(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        seed_update(
            selfhost,
            outcome="rolled_back",
            reason="health_timeout",
            excerpt=("starting up", f"the key is {SECRET_IN_LOG}", "still starting"),
        )
        await finish_updates(selfhost.app)

        page = await admin_client.get(INSTANCE)

        event_id = await selfhost.event_id("instance_update_rolled_back")
        assert "The new container did not become healthy in time." in text_of(page)
        assert "still starting" in text_of(page)
        assert SECRET_IN_LOG not in page.text
        assert f"/activity/{event_id}" in links_in(page)

    async def test_a_failed_rollback_tells_the_operator_where_the_previous_container_is(
        self, admin_client: AsyncClient, selfhost: Hosting
    ) -> None:
        selfhost.dispatcher()
        seed_update(selfhost, outcome="rollback_failed", reason="exited")
        await finish_updates(selfhost.app)

        page = await admin_client.get(INSTANCE)

        assert f"{SELF_NAME}-previous" in text_of(page)
        assert "docker start" in text_of(page)
