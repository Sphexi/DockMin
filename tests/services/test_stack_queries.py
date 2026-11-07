"""The stack read side: lists, details, unmanaged projects, gates and validation reports."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.containers import short_id
from app.services.object_inspector import EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectKey
from app.services.resource_controls import ControlSpec
from app.services.stack_collisions import (
    CONTAINERS_TOO_MANY,
    MAX_STOPPED_INSPECTED,
    NOT_CHECKED,
    REASON_NAME,
    REASON_PORT,
    STOPPED_UNCHECKED,
)
from app.services.stack_deploy import StackNameInUse, StackRejected
from app.services.stack_views import RestrictedOption
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    LEADER,
    MEMBER,
    PEER,
    STANDARD,
    container_id,
    container_key,
    control,
    digest_of,
    stack_key,
)
from tests.docker_engine.container_engine import ContainerEngine, Publish
from tests.docker_engine.fake_engine import RecordedRequest
from tests.stack_support import (
    PROJECT_LABEL,
    SECRET_SENTINEL,
    SERVICE_LABEL,
    Rig,
    compose_doc,
    form_variables,
    project_box,
    seed_stack_row,
)
from tests.support import present

SHOP = "shop"
GREETING = "hi"
TWO_SERVICES = """
services:
  web:
    image: nginx:1
  worker:
    image: nginx:2
"""
RESTRICTED_BY_ADMIN = """
services:
  web:
    image: nginx:1
    networks: [front]
    volumes: [vault:/data]
networks:
  front:
    driver: macvlan
    driver_opts: {parent: eth0}
    ipam:
      config: [{subnet: 10.1.0.0/24}]
volumes:
  vault:
    driver_opts: {type: nfs, o: "addr=10.0.0.9", device: ":/export"}
"""
NETWORK_AND_VOLUME_OPTIONS = {
    "network_addressing",
    "network_driver",
    "network_option",
    "volume_option",
}
MISSING_ENVIRONMENT = 9999
BAD_CURSOR = "not-a-cursor"
PAGE_OF_TWO = 2
ENGINE_ERROR_STATUS = 500


async def seed_listed_stacks(rig: Rig) -> None:
    await seed_stack_row(rig, ENV_A, "alpha", TWO_SERVICES, owner_user_id=STANDARD.user_id)
    await seed_stack_row(rig, ENV_A, "beta", TWO_SERVICES)
    await seed_stack_row(rig, ENV_A, "gamma", TWO_SERVICES)
    await control(rig.access, stack_key("gamma"), level="read", users=(MEMBER.user_id,))


async def refusals(queries: Any, name: str) -> list[ResourceNotFound]:
    """Ask the standard user PEER for every stack read of `name` and collect each refusal."""
    caught: list[ResourceNotFound] = []
    for call in (
        queries.detail(PEER, ENV_A, name),
        queries.edit_context(PEER, ENV_A, name),
        queries.redeploy_confirmation(PEER, ENV_A, name),
        queries.remove_confirmation(PEER, ENV_A, name),
        queries.busy(PEER, ENV_A, name),
    ):
        with pytest.raises(ResourceNotFound) as raised:
            await call
        caught.append(raised.value)
    return caught


async def test_the_list_shows_each_role_only_the_stacks_it_may_see_and_pages_by_name(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_listed_stacks(rig)
    project_box(rig.host, 1, "Ext")

    admin = await rig.fresh().queries.page(ADMIN, ENV_A)
    standard = await rig.fresh().queries.page(STANDARD, ENV_A)
    member = await rig.fresh().queries.page(MEMBER, ENV_A)
    peer = await rig.fresh().queries.page(PEER, ENV_A)
    first = await rig.fresh().queries.page(ADMIN, ENV_A, limit=PAGE_OF_TWO)
    rest = await rig.fresh().queries.page(ADMIN, ENV_A, limit=PAGE_OF_TWO, cursor=first.next_cursor)
    found = await rig.fresh().queries.page(ADMIN, ENV_A, q="ET")

    assert [r.name for r in admin.items] == ["alpha", "beta", "gamma"]
    assert [r.name for r in standard.items] == ["alpha"]
    assert [r.name for r in member.items] == ["gamma"]
    assert peer.items == () and peer.visible_count == 0
    assert [p.name for p in admin.unmanaged] == ["ext"]
    assert standard.unmanaged == () and member.unmanaged == ()
    assert [r.name for r in first.items] == ["alpha", "beta"]
    assert first.next_cursor is not None and first.visible_count is None
    assert [r.name for r in rest.items] == ["gamma"] and rest.next_cursor is None
    assert rest.unmanaged == ()
    assert [r.name for r in found.items] == ["beta"]
    assert admin.compose.ready and admin.can_create and standard.can_create
    assert admin.environment_name == "environment-a"


async def test_a_bad_page_request_is_refused_and_the_unmanaged_read_can_be_skipped(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    project_box(rig.host, 1, "Ext")
    before = len(rig.host.requests)

    skipped = await rig.fresh().queries.page(ADMIN, ENV_A, include_unmanaged=False)

    assert skipped.unmanaged == () and not skipped.unmanaged_unavailable
    assert len(rig.host.requests) == before
    with pytest.raises(ValidationFailed):
        await rig.fresh().queries.page(ADMIN, ENV_A, cursor=BAD_CURSOR)


async def test_a_missing_stack_and_a_stack_the_caller_may_not_see_are_indistinguishable(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_listed_stacks(rig)
    queries = rig.fresh().queries

    hidden = await refusals(queries, "alpha")
    absent = await refusals(queries, "missing")
    malformed = await refusals(queries, "Alpha")

    assert [str(e) for e in hidden] == [str(e) for e in absent] == [str(e) for e in malformed]


async def test_a_detail_lists_services_and_visible_containers_and_counts_the_hidden_ones(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(
        rig,
        ENV_A,
        SHOP,
        TWO_SERVICES,
        owner_user_id=STANDARD.user_id,
        variables=(("GREETING", False, GREETING), ("TOKEN", True, SECRET_SENTINEL)),
    )
    await rig.access.controls.update(
        ADMIN,
        stack_key(SHOP),
        ControlSpec(
            access_level="write", owner_user_id=STANDARD.user_id, user_ids=[MEMBER.user_id]
        ),
        expected_digest=await digest_of(rig.access, stack_key(SHOP)),
    )
    project_box(rig.host, 1, SHOP, "web")
    project_box(rig.host, 2, SHOP, "legacy")
    secret_box = project_box(rig.host, 3, SHOP, "web")
    await control(
        rig.access,
        ResourceKey(ENV_A, "container", secret_box.id),
        level="read",
        administrators_only=True,
    )

    owner = await rig.fresh().queries.detail(STANDARD, ENV_A, SHOP)
    reader = await rig.fresh().queries.detail(MEMBER, ENV_A, SHOP)
    admin = await rig.fresh().queries.detail(ADMIN, ENV_A, SHOP)

    def services(detail: Any) -> dict[str, tuple[bool, int, int]]:
        return {s.name: (s.declared, len(s.containers), s.hidden) for s in detail.services}

    assert services(admin) == {
        "legacy": (False, 1, 0),
        "web": (True, 2, 0),
        "worker": (True, 0, 0),
    }
    assert services(owner) == services(reader)
    assert services(reader)["web"] == (True, 1, 1)
    assert (reader.hidden_containers, admin.hidden_containers) == (1, 0)
    assert (owner.can_write, reader.can_write, admin.can_write) == (True, True, True)
    assert (owner.can_manage, admin.can_manage) == (False, True)
    assert {(v.name, v.secret, v.value) for v in admin.variables} == {
        ("GREETING", False, GREETING),
        ("TOKEN", True, None),
    }
    assert SECRET_SENTINEL not in repr(admin) + repr(owner) + repr(reader)
    assert admin.containers_available and admin.busy is None


async def test_a_read_holder_cannot_open_the_editor_or_the_access_panel_but_an_administrator_can(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_A, SHOP, TWO_SERVICES, owner_user_id=STANDARD.user_id)
    await rig.access.controls.update(
        ADMIN,
        stack_key(SHOP),
        ControlSpec(access_level="read", owner_user_id=STANDARD.user_id, user_ids=[MEMBER.user_id]),
        expected_digest=await digest_of(rig.access, stack_key(SHOP)),
    )

    panel = await rig.fresh().queries.access_panel(ADMIN, ENV_A, SHOP)
    editable = await rig.fresh().queries.edit_context(ADMIN, ENV_A, SHOP)
    reader = await rig.fresh().queries.detail(MEMBER, ENV_A, SHOP)
    with pytest.raises(PermissionDenied):
        await rig.fresh().queries.access_panel(MEMBER, ENV_A, SHOP)
    with pytest.raises(PermissionDenied):
        await rig.fresh().queries.edit_context(MEMBER, ENV_A, SHOP)

    assert panel.key == stack_key(SHOP) and panel.control is not None
    assert panel.control.user_ids == (MEMBER.user_id,)
    assert editable.compose_file == TWO_SERVICES
    assert reader.can_write is False and reader.can_manage is False


async def test_an_externally_created_project_is_listed_as_unmanaged_and_can_be_adopted_by_name(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_A, SHOP, TWO_SERVICES)
    project_box(rig.host, 1, "SHOP")
    project_box(rig.host, 2, "Ext", "web")
    project_box(rig.host, 3, "Ext", "db", state="exited")
    project_box(rig.host, 4, "has space")
    rig.host.add_network("netonly_default", 0x77, Labels={PROJECT_LABEL: "NetOnly"})

    listed = await rig.fresh().queries.unmanaged(ADMIN, ENV_A)
    context = await rig.fresh().queries.adopt_context(ADMIN, ENV_A, "EXT")
    page = await rig.fresh().queries.page(ADMIN, ENV_A)

    by_name = {p.name: p for p in listed}
    assert sorted(by_name) == ["ext", "has space", "netonly"]
    ext = by_name["ext"]
    assert (ext.containers, ext.running, ext.services) == (2, 1, ("db", "web"))
    assert ext.container_names == ("ext-db-3", "ext-web-2") and ext.adoptable
    assert (by_name["netonly"].containers, by_name["netonly"].networks) == (0, 1)
    assert not by_name["has space"].adoptable
    assert context == ext
    assert page.unmanaged == listed
    with pytest.raises(PermissionDenied):
        await rig.fresh().queries.unmanaged(STANDARD, ENV_A)
    with pytest.raises(PermissionDenied):
        await rig.fresh().queries.adopt_context(STANDARD, ENV_A, "ext")
    with pytest.raises(ResourceNotFound):
        await rig.fresh().queries.adopt_context(ADMIN, ENV_A, "nothere")


async def test_the_unmanaged_list_and_the_name_check_agree_on_a_project_that_differs_in_case(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    project_box(rig.host, 1, "Ext")

    listed = await rig.fresh().queries.unmanaged(ADMIN, ENV_A)
    with pytest.raises(StackNameInUse):
        await rig.fresh().queries.validate(
            ADMIN, ENV_A, name="ext", compose_text=TWO_SERVICES, variables=None
        )
    await seed_stack_row(rig, ENV_A, "ext", TWO_SERVICES)
    after = await rig.fresh().queries.unmanaged(ADMIN, ENV_A)

    assert [p.name for p in listed] == ["ext"]
    assert after == ()


async def test_the_unmanaged_list_fails_as_unavailable_when_the_host_cannot_list_containers(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    project_box(rig.host, 1, "Ext")
    rig.host.listing_status = ENGINE_ERROR_STATUS

    with pytest.raises(EnvironmentUnavailable) as unavailable:
        await rig.fresh().queries.unmanaged(ADMIN, ENV_A)

    assert type(unavailable.value) is EnvironmentUnavailable
    assert unavailable.value.kind == "engine"


async def test_the_environment_gate_and_the_busy_view_follow_the_callers_standing(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_A, SHOP, TWO_SERVICES, owner_user_id=STANDARD.user_id)
    await EnvironmentAccessRepository(rig.db).remove_user("environment", ENV_A, LEADER.user_id)
    holder = LockHolder(UserActor.of(ADMIN), ADMIN.username, "redeploy")
    queries = rig.fresh().queries

    await queries.require_environment(STANDARD, ENV_A)
    idle = await queries.busy(STANDARD, ENV_A, SHOP)
    async with rig.engine.locks.hold(ObjectKey(ENV_A, "stack", SHOP), holder):
        rig.clock.advance(timedelta(seconds=5))
        as_owner = await queries.busy(STANDARD, ENV_A, SHOP)
        as_admin = await queries.busy(ADMIN, ENV_A, SHOP)

    assert idle is None
    assert (present(as_owner).holder, present(as_owner).held_by_you, present(as_owner).action) == (
        "another user",
        False,
        "redeploy",
    )
    assert (present(as_admin).holder, present(as_admin).held_by_you) == (ADMIN.username, True)
    with pytest.raises(ResourceNotFound):
        await queries.require_environment(ADMIN, MISSING_ENVIRONMENT)
    with pytest.raises(ResourceNotFound):
        await queries.require_environment(LEADER, ENV_A)
    with pytest.raises(ResourceNotFound):
        await queries.page(LEADER, ENV_A)


async def test_a_report_for_an_administrator_names_each_restricted_option_by_a_phrase(
    stack_rig: Rig,
) -> None:
    rig = stack_rig

    report = await rig.fresh().queries.validate(
        ADMIN, ENV_B, name=SHOP, compose_text=RESTRICTED_BY_ADMIN, variables=None
    )

    assert set(report.restricted) == NETWORK_AND_VOLUME_OPTIONS
    assert all(isinstance(o, RestrictedOption) for o in report.restricted_options)
    assert {o.option: o.label for o in report.restricted_options}.keys() == set(report.restricted)
    assert {o.label for o in report.restricted_options}.isdisjoint(report.restricted)


async def test_validating_a_change_to_a_stack_runs_in_a_throwaway_directory_and_leaves_no_file(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    record = rig.data_dir / "record.jsonl"
    text = compose_doc() + f"x-fake:\n  record: {record}\n"
    await rig.fresh().actions.create(ADMIN, ENV_B, name=SHOP, compose_text=text)
    token = (present(await rig.db.fetch_one("SELECT project_directory FROM stack")))[
        "project_directory"
    ]
    directory = rig.compose.files.base / token
    runs_before = len(record.read_text().splitlines())

    await rig.fresh().queries.validate(
        ADMIN, ENV_B, name=SHOP, compose_text=text + "# edit\n", variables=None, existing=True
    )

    ran_in = json.loads(record.read_text().splitlines()[runs_before])
    assert Path(ran_in["cwd"]).parent == rig.compose.files.base
    assert ran_in["cwd"] != str(directory)
    assert sorted(p.name for p in rig.compose.files.base.iterdir()) == [token]
    assert sorted(p.name for p in directory.iterdir()) == ["compose.yaml"]


async def test_validating_a_new_stack_stores_nothing_and_a_refused_file_is_rejected_for_a_name(
    stack_rig: Rig,
) -> None:
    rig = stack_rig

    await rig.fresh().queries.validate(
        ADMIN, ENV_B, name=SHOP, compose_text=compose_doc(), variables=None
    )
    with pytest.raises(StackRejected) as refused:
        await rig.fresh().queries.validate(
            ADMIN, ENV_B, name="Not A Name", compose_text=compose_doc(), variables=None
        )
    with pytest.raises(StackRejected) as variable:
        await rig.fresh().queries.validate(
            ADMIN,
            ENV_B,
            name=SHOP,
            compose_text=compose_doc(),
            variables=form_variables(("DOCKER_HOST", "x", False)),
        )

    assert await rig.db.fetch_one("SELECT 1 FROM stack") is None
    assert list(rig.compose.files.base.iterdir()) == []
    assert "name" in refused.value.fields
    assert "variable-0-name" in variable.value.fields


LEGACY = "legacy"
FIXED = "fixed-web"
STOPPED_OWNER = "old"
PUBLISHED = 8080
STOPPED_PORT = 9090
BOUND_ADDRESS = "10.0.0.5"
MANY_STOPPED = MAX_STOPPED_INSPECTED + 5
NAME_FIELD = "services.web.container_name"
PORTS_FIELD = "services.web.ports"
BROKEN_STORED = "broken"


def outside(
    host: ContainerEngine,
    number: int,
    name: str,
    *,
    state: str = "running",
    publishes: tuple[Publish, ...] = (),
) -> None:
    host.add(container_id(number), name, state=state, publishes=list(publishes))


def inspect_requests(host: ContainerEngine) -> list[RecordedRequest]:
    return [
        r
        for r in host.calls("GET")
        if r.path.startswith("/containers/") and r.path.endswith("/json")
        if r.path != "/containers/json"
    ]


def file_of(**services: dict[str, Any]) -> str:
    body = {key: {"image": "nginx:1", **options} for key, options in services.items()}
    return yaml.safe_dump({"services": body})


def mapping(port: int, host_ip: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"target": 80, "published": port}
    return entry if host_ip is None else {**entry, "host_ip": host_ip}


async def collision_check(
    rig: Rig, text: str, *, who: Any = ADMIN, name: str = SHOP, existing: bool = True
) -> Any:
    return await rig.fresh().queries.validate(
        who,
        ENV_A,
        name=name,
        compose_text=text,
        variables=None,
        existing=existing,
        check_collisions=True,
    )


async def test_a_container_name_held_outside_the_project_refuses_and_the_projects_own_does_not(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    outside(rig.host, 1, LEGACY)
    rig.host.add(container_id(2), FIXED, labels={PROJECT_LABEL: SHOP.upper(), SERVICE_LABEL: "web"})
    await seed_stack_row(rig, ENV_A, SHOP, file_of(web={"container_name": FIXED}))

    with pytest.raises(StackRejected) as foreign:
        await collision_check(rig, file_of(web={"container_name": LEGACY}))
    own = await collision_check(rig, file_of(web={"container_name": FIXED}))
    with pytest.raises(StackRejected) as other_project:
        await collision_check(
            rig, file_of(web={"container_name": FIXED}), name="other", existing=False
        )
    unchecked = await rig.fresh().queries.validate(
        ADMIN,
        ENV_A,
        name=SHOP,
        compose_text=file_of(web={"container_name": LEGACY}),
        variables=None,
        existing=True,
    )

    assert foreign.value.reason == REASON_NAME
    assert foreign.value.fields == {
        NAME_FIELD: f"The container name {LEGACY} is already used by "
        f"the container {LEGACY} ({short_id(container_id(1))})."
    }
    assert str(foreign.value) == "Service web sets a container name that is already in use."
    assert own.warnings == () and unchecked.warnings == ()
    assert other_project.value.reason == REASON_NAME


async def test_a_running_port_holder_refuses_with_its_address_and_a_stopped_one_only_warns(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    outside(rig.host, 1, "api", publishes=(Publish(PUBLISHED, host_ip=BOUND_ADDRESS),))
    outside(rig.host, 2, STOPPED_OWNER, state="exited", publishes=(Publish(STOPPED_PORT),))
    outside(rig.host, 3, "dns", publishes=(Publish(PUBLISHED, protocol="udp"),))
    await seed_stack_row(rig, ENV_A, SHOP, file_of(web={}))
    both = file_of(
        web={"ports": [mapping(PUBLISHED, BOUND_ADDRESS)]},
        worker={"ports": [mapping(STOPPED_PORT)]},
    )
    only_stopped = file_of(worker={"ports": [mapping(STOPPED_PORT)]})
    stopped_warning = (
        f"Service worker: port {STOPPED_PORT}/tcp on every address is published by "
        f"the container {STOPPED_OWNER}, which is not running."
    )

    with pytest.raises(StackRejected) as refused:
        await collision_check(rig, both)
    warned = await collision_check(rig, only_stopped)

    assert refused.value.reason == REASON_PORT
    assert refused.value.fields == {
        PORTS_FIELD: f"Port {PUBLISHED}/tcp on {BOUND_ADDRESS} is already published by "
        "the container api."
    }
    assert str(refused.value) == (
        f"Service web publishes tcp port {PUBLISHED} that is already published."
    )
    assert refused.value.warnings == (stopped_warning,)
    assert warned.warnings == (stopped_warning,)


async def test_reads_that_cannot_finish_leave_warnings_and_never_an_exception(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    host = rig.host
    await seed_stack_row(rig, ENV_A, SHOP, file_of(web={}))
    text = file_of(web={"ports": [mapping(PUBLISHED)]})

    host.oversize_listing = True
    overflow = await collision_check(rig, text)
    host.oversize_listing = False
    for number in range(1, MANY_STOPPED + 1):
        outside(host, number, f"stopped-{number}", state="exited")
    many = await collision_check(rig, text)
    inspected = len(inspect_requests(host))
    host.boxes.clear()
    outside(host, 1, "unreadable", state="exited", publishes=(Publish(PUBLISHED),))
    host.fail("inspect", 500, target=container_id(1))
    broken = await collision_check(rig, text)

    assert overflow.warnings == (CONTAINERS_TOO_MANY,)
    assert inspected == MAX_STOPPED_INSPECTED
    assert many.warnings == (STOPPED_UNCHECKED,)
    assert broken.warnings == (STOPPED_UNCHECKED,)


async def test_a_standard_user_never_learns_the_name_address_or_count_of_a_hidden_holder(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    outside(rig.host, 1, "hidden-db", publishes=(Publish(PUBLISHED, host_ip=BOUND_ADDRESS),))
    outside(rig.host, 2, "shown-api", publishes=(Publish(PUBLISHED + 1, host_ip="10.0.0.6"),))
    outside(rig.host, 3, "hidden-old", state="exited", publishes=(Publish(STOPPED_PORT),))
    await control(rig.access, container_key(1), administrators_only=True)
    await control(rig.access, container_key(2), public=True)
    await control(rig.access, container_key(3), administrators_only=True)
    text = file_of(
        web={
            "container_name": "hidden-db",
            "ports": [mapping(PUBLISHED), mapping(PUBLISHED + 1), mapping(STOPPED_PORT)],
        }
    )
    only_stopped = file_of(web={"ports": [mapping(STOPPED_PORT)]})

    with pytest.raises(StackRejected) as refused:
        await collision_check(rig, text, who=STANDARD, name="mine", existing=False)
    warned = await collision_check(rig, only_stopped, who=STANDARD, name="mine", existing=False)

    assert refused.value.fields[NAME_FIELD] == (
        "The container name hidden-db is already used by another container."
    )
    ports = refused.value.fields[PORTS_FIELD]
    assert (
        f"Port {PUBLISHED}/tcp on every address is already published by another container." in ports
    )
    assert (
        f"Port {PUBLISHED + 1}/tcp on 10.0.0.6 is already published by the container shown-api."
        in ports
    )
    stopped = (
        "Service web: a container you cannot see has a stopped port binding that overlaps "
        "the ports this service publishes."
    )
    assert refused.value.warnings == (stopped,) and warned.warnings == (stopped,)
    leaked = " ".join([*refused.value.fields.values(), *refused.value.warnings, *warned.warnings])
    assert not [
        word for word in (BOUND_ADDRESS, "hidden-old", short_id(container_id(1))) if word in leaked
    ]


async def test_the_redeploy_confirmation_lists_what_running_holders_hold_and_warns_about_the_rest(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    outside(rig.host, 1, LEGACY, publishes=(Publish(PUBLISHED),))
    outside(rig.host, 2, STOPPED_OWNER, state="exited", publishes=(Publish(STOPPED_PORT),))
    await seed_stack_row(
        rig,
        ENV_A,
        SHOP,
        file_of(
            web={"container_name": LEGACY, "ports": [mapping(PUBLISHED)]},
            worker={"ports": [mapping(STOPPED_PORT)]},
        ),
    )
    await seed_stack_row(rig, ENV_A, BROKEN_STORED, file_of(web={}) + "x-fake:\n  config: fail\n")

    checked = await rig.fresh().queries.redeploy_confirmation(
        ADMIN, ENV_A, SHOP, check_collisions=True
    )
    unchecked = await rig.fresh().queries.redeploy_confirmation(ADMIN, ENV_A, SHOP)
    unreadable = await rig.fresh().queries.redeploy_confirmation(
        ADMIN, ENV_A, BROKEN_STORED, check_collisions=True
    )

    name_holder = f"the container {LEGACY} ({short_id(container_id(1))})"
    assert checked.conflicts == (
        (NAME_FIELD, f"The container name {LEGACY} is already used by {name_holder}."),
        (
            PORTS_FIELD,
            f"Port {PUBLISHED}/tcp on every address is already published by "
            f"the container {LEGACY}.",
        ),
    )
    assert len(checked.warnings) == 1 and STOPPED_OWNER in checked.warnings[0]
    assert (unchecked.conflicts, unchecked.warnings) == ((), ())
    assert (unreadable.conflicts, unreadable.warnings) == ((), (NOT_CHECKED,))
