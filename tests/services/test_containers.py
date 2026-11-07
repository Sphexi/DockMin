"""The container list and detail: filters, paging, visibility, redaction and failure."""

from collections.abc import Sequence

import pytest

import app.docker_engine.resources as resources
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.docker_engine.labels import LABEL_BUDGET_CHARS
from app.logging import REDACTED
from app.services.containers import (
    ContainerFilters,
    ContainerListTooLarge,
    ContainerRow,
    ContainerTooLarge,
)
from app.services.object_inspector import EnvironmentUnavailable
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, container_id, control, stack_key
from tests.access_support import container_key as key_of
from tests.container_support import (
    ABSENT,
    API,
    BIND_SOURCE,
    CACHE,
    DB,
    HIDDEN,
    JOB,
    PLAIN_ENV_VALUE,
    SECRET_ARGUMENT,
    SECRET_ENV_VALUE,
    SECRET_LABEL_VALUE,
    STACK,
    STACK_LABEL,
    URL_CREDENTIAL,
    WEB,
    stock,
)
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Refuse, Reply
from tests.services.conftest import ContainerRig

ALIAS = container_id(0xD1)
FRESH = container_id(0xB1)
GONE = container_id(0xB2)
HUGE = "x" * (LABEL_BUDGET_CHARS + 1)
PAGE_OF_TWO = 2
MANY_PORTS = 25
SECRETS = (SECRET_ENV_VALUE, SECRET_ARGUMENT, SECRET_LABEL_VALUE, URL_CREDENTIAL, BIND_SOURCE)
OVER_THE_CAP = {"Content-Length": "1" + "0" * 12}


def names(rows: Sequence[ContainerRow]) -> list[str]:
    return [row.name for row in rows]


async def listed(
    rig: ContainerRig,
    who: Principal = ADMIN,
    filters: ContainerFilters | None = None,
    environment_id: int = ENV_A,
) -> list[str]:
    result = await rig.services.queries.page(who, environment_id, filters)
    return names(result.page.items)


@pytest.fixture
def filterable(host: ContainerEngine, rig: ContainerRig) -> ContainerRig:
    stock(host)
    host.add(FRESH, "fresh", image="alpine", state="created")
    host.add(GONE, "gone", image="alpine", state="dead")
    return rig


@pytest.fixture
async def shared(host: ContainerEngine, rig: ContainerRig) -> ContainerRig:
    """A stack the standard user is granted, and one container of each way of missing it."""
    stock(host)
    host.add(API, "api", labels={STACK_LABEL: STACK.upper()})
    host.add(HIDDEN, "wrong-key", labels={"Com.Docker.Compose.Project": STACK})
    await control(rig.access, stack_key(STACK, ENV_A), public=True, level="write")
    await control(rig.access, key_of(0xA1, ENV_A), administrators_only=True)
    await control(rig.access, key_of(0xA5, ENV_A), public=True, level="read")
    return rig


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (ContainerFilters(state="running"), ["db", "warm", "web"]),
        (ContainerFilters(state="paused"), ["cache"]),
        (ContainerFilters(state="restarting"), ["worker"]),
        (ContainerFilters(state="exited"), ["job"]),
        (ContainerFilters(state="stopped"), ["fresh", "gone", "job"]),
        (ContainerFilters(health="unhealthy"), ["db"]),
        (ContainerFilters(health="starting"), ["warm"]),
        (ContainerFilters(q="WE"), ["web"]),
        (ContainerFilters(q="/job"), ["job"]),
        (ContainerFilters(image="BUSY"), ["job", "warm", "worker"]),
        (ContainerFilters(labels=("tier",)), ["web"]),
        (ContainerFilters(labels=("tier=front",)), ["web"]),
        (ContainerFilters(labels=("Tier",)), []),
        (ContainerFilters(labels=("tier=FRONT",)), []),
        (ContainerFilters(labels=("tier=front", STACK_LABEL)), ["web"]),
        (ContainerFilters(stack="SHOP"), ["db", "web"]),
        (ContainerFilters(stack="  shop "), ["db", "web"]),
        (ContainerFilters(state="running", stack="shop"), ["db", "web"]),
    ],
    ids=lambda value: str(value) if isinstance(value, list) else "",
)
async def test_each_filter_selects_the_containers_it_names(
    filterable: ContainerRig, filters: ContainerFilters, expected: list[str]
) -> None:
    assert await listed(filterable, filters=filters) == expected


@pytest.mark.parametrize(
    ("filters", "field"),
    [
        (ContainerFilters(state="bogus"), "state"),
        (ContainerFilters(health="healthy"), "health"),
        (ContainerFilters(q="x" * 65), "q"),
        (ContainerFilters(image="x" * 129), "image"),
        (ContainerFilters(stack="x" * 129), "stack"),
        (ContainerFilters(labels=("a",) * 6), "label"),
        (ContainerFilters(labels=("=value",)), "label"),
        (ContainerFilters(labels=("k=" + "v" * 256,)), "label"),
    ],
)
async def test_an_invalid_filter_is_refused_by_field_before_the_host_is_read(
    filterable: ContainerRig, host: ContainerEngine, filters: ContainerFilters, field: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await filterable.services.queries.page(ADMIN, ENV_A, filters)

    assert field in refused.value.fields
    assert host.requests == []


@pytest.mark.parametrize(
    ("limit", "cursor", "field"),
    [(0, None, "limit"), (501, None, "limit"), (25, "not-a-cursor", "cursor")],
)
async def test_a_bad_page_size_or_cursor_is_refused(
    filterable: ContainerRig, limit: int, cursor: str | None, field: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await filterable.services.queries.page(ADMIN, ENV_A, limit=limit, cursor=cursor)

    assert field in refused.value.fields


async def test_paging_continues_after_the_last_container_seen_when_the_list_changes(
    filterable: ContainerRig, host: ContainerEngine
) -> None:
    queries = filterable.services.queries
    first = await queries.page(ADMIN, ENV_A, limit=PAGE_OF_TWO)
    del host.boxes[CACHE]
    host.add(container_id(0xB3), "aardvark")

    second = await queries.page(ADMIN, ENV_A, limit=PAGE_OF_TWO, cursor=first.page.next_cursor)

    assert names(first.page.items) == ["cache", "db"]
    assert names(second.page.items) == ["fresh", "gone"]
    assert first.total == 8 and second.total == 8


async def test_the_last_page_has_no_cursor_and_a_full_page_that_ends_the_list_has_none_either(
    filterable: ContainerRig,
) -> None:
    result = await filterable.services.queries.page(ADMIN, ENV_A, limit=8)

    assert result.page.next_cursor is None and len(result.page.items) == 8


async def test_a_standard_user_lists_only_permitted_containers_and_those_inherited_from_a_stack(
    shared: ContainerRig,
) -> None:
    result = await shared.services.queries.page(STANDARD, ENV_A)

    assert names(result.page.items) == ["api", "db", "job"]
    assert result.total == 3


async def test_a_detail_for_a_standard_user_costs_one_inspect_for_the_check_and_the_page(
    shared: ContainerRig, host: ContainerEngine
) -> None:
    await shared.services.queries.detail(STANDARD, ENV_A, DB)

    assert len(host.calls("GET", f"/containers/{DB}/json")) == 1


async def test_an_administrator_sees_every_container_whatever_its_labels(
    shared: ContainerRig, host: ContainerEngine
) -> None:
    host.add(container_id(0xB4), "labelled", labels={"hidden": "true", "dockmin.hide": "1"})

    assert await listed(shared) == [
        "api",
        "cache",
        "db",
        "job",
        "labelled",
        "warm",
        "web",
        "worker",
        "wrong-key",
    ]


async def test_the_standard_users_write_actions_are_offered_only_where_write_is_held(
    shared: ContainerRig,
) -> None:
    result = await shared.services.queries.page(STANDARD, ENV_A)

    assert {row.name: bool(row.actions) for row in result.page.items} == {
        "api": True,
        "db": True,
        "job": False,
    }


@pytest.mark.parametrize(
    "hidden", [WEB, CACHE, HIDDEN, ABSENT], ids=["admin-only", "uncontrolled", "key-case", "absent"]
)
async def test_a_container_the_user_cannot_see_is_not_found_on_detail(
    shared: ContainerRig, hidden: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await shared.services.queries.detail(STANDARD, ENV_A, hidden)


@pytest.mark.parametrize("who", [ADMIN, STANDARD], ids=["administrator", "standard"])
@pytest.mark.parametrize("reference", ["web", WEB[:12], WEB.upper(), "../web", WEB + "0"])
async def test_a_name_short_id_or_malformed_id_is_not_found_with_no_call_to_the_host(
    shared: ContainerRig, host: ContainerEngine, who: Principal, reference: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await shared.services.queries.detail(who, ENV_A, reference)

    assert host.requests == []


async def test_a_name_that_is_another_containers_id_is_refused_as_not_the_container_asked_for(
    shared: ContainerRig, host: ContainerEngine
) -> None:
    host.add(container_id(0xC1), ALIAS)

    with pytest.raises(ResourceNotFound):
        await shared.services.queries.detail(ADMIN, ENV_A, ALIAS)


async def test_a_user_without_the_environment_gets_not_found_and_the_host_is_never_called(
    shared: ContainerRig, host: ContainerEngine, no_grants: None
) -> None:
    with pytest.raises(ResourceNotFound):
        await shared.services.queries.page(STANDARD, ENV_A)

    assert host.requests == []


@pytest.mark.parametrize(
    "route", [("GET", "/_ping"), ("GET", "/containers/json")], ids=["ping", "list"]
)
async def test_an_unreachable_host_is_a_product_error_with_fixed_text(
    filterable: ContainerRig, host: ContainerEngine, route: tuple[str, str]
) -> None:
    host.route(*route, Refuse())

    with pytest.raises(EnvironmentUnavailable) as failed:
        await filterable.services.queries.page(ADMIN, ENV_A)

    assert failed.value.kind == "unreachable"
    assert "refused" not in str(failed.value)


async def test_a_host_marked_version_unsupported_is_refused_without_an_engine_call(
    filterable: ContainerRig, host: ContainerEngine
) -> None:
    await filterable.db.execute(
        "UPDATE environment SET status = 'version_unsupported', failed_stage = 'engine' "
        "WHERE id = ?",
        (ENV_A,),
    )

    with pytest.raises(EnvironmentUnavailable) as failed:
        await filterable.services.queries.page(ADMIN, ENV_A)

    assert failed.value.kind == "unsupported"
    assert host.requests == []


async def test_a_list_over_the_read_limit_is_refused_with_the_limit_named_not_shown_in_part(
    filterable: ContainerRig, host: ContainerEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "CONTAINER_LIST_BYTES", 100)
    host.route("GET", "/containers/json", Reply(200, b"[]", OVER_THE_CAP))

    with pytest.raises(ContainerListTooLarge) as failed:
        await filterable.services.queries.page(ADMIN, ENV_A)

    assert "32 MiB" in str(failed.value)


async def test_an_inspect_over_the_read_limit_is_refused_as_too_large_and_not_shown_in_part(
    filterable: ContainerRig, host: ContainerEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "CONTAINER_INSPECT_BYTES", 100)
    host.route("GET", f"/containers/{WEB}/json", Reply(200, b"{}", OVER_THE_CAP))

    with pytest.raises(ContainerTooLarge):
        await filterable.services.queries.detail(ADMIN, ENV_A, WEB)


async def test_a_container_with_huge_labels_is_listed_marked_and_keeps_its_stack(
    filterable: ContainerRig, host: ContainerEngine
) -> None:
    host.add(
        container_id(0xB5),
        "bulky",
        labels={"bulk": HUGE, STACK_LABEL: STACK, "small": "kept"},
    )

    result = await filterable.services.queries.page(ADMIN, ENV_A, ContainerFilters(stack=STACK))

    bulky = next(row for row in result.page.items if row.name == "bulky")
    assert dict(bulky.labels) == {STACK_LABEL: STACK, "small": "kept"}
    assert (bulky.labels_truncated, bulky.stack) == (True, STACK)
    assert [row.labels_truncated for row in result.page.items if row.name != "bulky"] == [
        False,
        False,
    ]


async def test_a_failed_health_read_still_lists_the_containers_and_says_health_is_unavailable(
    filterable: ContainerRig, host: ContainerEngine
) -> None:
    host.health_listing_status = 500

    result = await filterable.services.queries.page(ADMIN, ENV_A)

    assert result.health_unavailable is True
    assert len(result.page.items) == 8
    assert {row.health for row in result.page.items} == {None}


async def test_the_swapped_row_equals_the_list_row_including_assigned_ports_and_names(
    filterable: ContainerRig,
) -> None:
    queries = filterable.services.queries
    from_list = next(row for row in (await queries.page(ADMIN, ENV_A)).page.items if row.id == WEB)

    from_inspect = await queries.row(ADMIN, ENV_A, WEB)

    assert from_inspect == from_list
    assert [port.text for port in from_list.ports] == ["8080->80/tcp"]


async def test_a_container_publishing_many_ports_shows_twenty_and_counts_the_rest_on_both_rows(
    filterable: ContainerRig, host: ContainerEngine
) -> None:
    host.add(
        container_id(0xB6),
        "busy",
        ports=[
            {"IP": "0.0.0.0", "PrivatePort": 1000 + n, "PublicPort": 2000 + n, "Type": "tcp"}
            for n in range(MANY_PORTS)
        ],
    )
    queries = filterable.services.queries
    from_list = next(r for r in (await queries.page(ADMIN, ENV_A)).page.items if r.name == "busy")

    from_inspect = await queries.row(ADMIN, ENV_A, container_id(0xB6))

    assert (len(from_list.ports), from_list.ports_hidden) == (20, MANY_PORTS - 20)
    assert from_inspect == from_list


async def test_a_standard_user_sees_no_secret_in_the_detail_the_list_or_the_labels(
    shared: ContainerRig, host: ContainerEngine
) -> None:
    host.boxes[DB].labels.update(
        {
            "db_password": SECRET_LABEL_VALUE,
            "dsn": f"postgres://user:{URL_CREDENTIAL}@db/app",
            "api.token.v2": SECRET_LABEL_VALUE,
        }
    )
    host.boxes[DB].env = [f"DB_PASSWORD={SECRET_ENV_VALUE}", f"MODE={PLAIN_ENV_VALUE}"]
    host.boxes[DB].cmd = ["serve", f"--token={SECRET_ARGUMENT}"]
    host.boxes[DB].mounts = [
        {"Type": "bind", "Source": BIND_SOURCE, "Destination": "/srv", "RW": False}
    ]
    queries = shared.services.queries

    detail = await queries.detail(STANDARD, ENV_A, DB)
    row = next(r for r in (await queries.page(STANDARD, ENV_A)).page.items if r.id == DB)

    shown = repr(detail) + repr(row)
    for secret in (
        SECRET_ENV_VALUE,
        SECRET_ARGUMENT,
        SECRET_LABEL_VALUE,
        URL_CREDENTIAL,
        BIND_SOURCE,
    ):
        assert secret not in shown
    assert (detail.variables, detail.admin, detail.command, detail.entrypoint) == (
        None,
        None,
        None,
        None,
    )
    assert detail.mounts[0].source is None
    assert {label.key: label.redacted for label in detail.labels}["dsn"] is True
    assert row.labels["db_password"] == REDACTED


async def test_an_administrator_sees_the_variables_bind_paths_command_and_every_label_value(
    shared: ContainerRig, host: ContainerEngine
) -> None:
    host.boxes[WEB].labels["db_password"] = SECRET_LABEL_VALUE

    detail = await shared.services.queries.detail(ADMIN, ENV_A, WEB)

    variables = {
        variable.key: (variable.value, variable.redacted) for variable in detail.variables or ()
    }
    assert variables == {"DB_PASSWORD": (REDACTED, True), "MODE": (PLAIN_ENV_VALUE, False)}
    assert detail.mounts[0].source == BIND_SOURCE
    assert detail.command is not None and SECRET_ARGUMENT in detail.command
    assert detail.admin is not None and detail.admin.privileged is True
    assert {label.key: label.value for label in detail.labels}["db_password"] == SECRET_LABEL_VALUE


async def test_a_stopped_container_offers_start_and_a_running_one_stop_for_a_write_holder(
    shared: ContainerRig,
) -> None:
    exited = await shared.services.queries.detail(ADMIN, ENV_A, JOB)
    running = await shared.services.queries.detail(ADMIN, ENV_A, WEB)

    assert "start" in exited.actions and "stop" not in exited.actions
    assert "stop" in running.actions and "start" not in running.actions


async def test_the_access_panel_and_effective_access_are_for_administrators_only(
    shared: ContainerRig,
) -> None:
    queries = shared.services.queries

    with pytest.raises(PermissionDenied):
        await queries.access_panel(STANDARD, ENV_A, DB)
    with pytest.raises(PermissionDenied):
        await queries.effective_access(STANDARD, ENV_A, "admin-user")


@pytest.mark.parametrize("username", ["", "   ", "nobody", "x" * 129])
async def test_effective_access_for_a_name_that_is_no_account_is_a_field_error(
    shared: ContainerRig, username: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await shared.services.queries.effective_access(ADMIN, ENV_A, username)

    assert "username" in refused.value.fields


async def test_effective_access_explains_every_container_on_the_host_for_the_user(
    shared: ContainerRig,
) -> None:
    result = await shared.services.queries.effective_access(ADMIN, ENV_A, "standard-user")

    levels = {result.names[entry.resource_id]: entry.level for entry in result.access.entries}
    assert levels == {
        "api": "write",
        "cache": None,
        "db": "write",
        "job": "read",
        "warm": None,
        "web": None,
        "wrong-key": None,
        "worker": None,
    }


async def test_a_standard_users_local_socket_rows_offer_no_actions(
    host: ContainerEngine, rig: ContainerRig
) -> None:
    stock(host)
    await control(rig.access, key_of(0xA1, ENV_B), public=True, level="write")

    result = await rig.services.queries.page(STANDARD, ENV_B)

    assert [(row.name, row.actions) for row in result.page.items] == [("web", ())]
