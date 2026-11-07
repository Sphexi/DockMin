"""Tests for app/services/host_objects.py and the pure helpers beside the network services."""

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

import pytest

from app.auth.errors import ValidationFailed
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.docker_engine.errors import (
    Conflict,
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    RequestOutcomeUnknown,
)
from app.docker_engine.models import ContainerSummary, Network
from app.logging import REDACTED
from app.services.activity import AuditState, UserActor
from app.services.host_objects import (
    MAX_USERS_SHOWN,
    check_limit,
    create_failure,
    decode_position,
    describe_users,
    failure_reason,
    hold_all,
    label_views,
    network_users,
    option_views,
    row_id,
    volume_users,
    write_settled_row,
)
from app.services.networks import network_kind
from app.services.object_inspector import EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectKey, ObjectLockRegistry
from app.services.volume_views import containers_digest
from tests.access_support import ADMIN, ENV_A, container_id
from tests.automation_support import authority_context, event_rows
from tests.services.conftest import SwarmRig
from tests.updater_support import UPDATER

NETWORK_ID = "b" * 64
OTHER_NETWORK_ID = "c" * 64
PASSWORD = "Hunter2Pass"
ENVIRONMENT = 1
FIRST_USER = 11


def network(name: str = "front", network_id: str = NETWORK_ID, **fields: Any) -> Network:
    return Network.model_validate(
        {"Id": network_id, "Name": name, "Driver": "bridge", "Scope": "local", **fields}
    )


def summary(
    number: int,
    name: str = "web",
    *,
    state: str = "running",
    mounts: list[dict[str, Any]] | None = None,
    endpoints: Mapping[str, str] | None = None,
    mode: str | None = None,
) -> ContainerSummary:
    document: dict[str, Any] = {
        "Id": container_id(number),
        "Names": [f"/{name}"],
        "Image": "img",
        "Created": 1,
        "State": state,
        "Mounts": mounts or [],
    }
    if endpoints is not None:
        document["NetworkSettings"] = {
            "Networks": {n: {"NetworkID": i} for n, i in endpoints.items()}
        }
    if mode is not None:
        document["HostConfig"] = {"NetworkMode": mode}
    return ContainerSummary.model_validate(document)


def volume_mount(name: str | None, target: str = "/data") -> dict[str, Any]:
    return {"Type": "volume", "Name": name, "Destination": target}


def test_a_volume_is_used_by_each_container_mounting_it_in_any_state() -> None:
    running = summary(1, mounts=[volume_mount("data")])
    stopped = summary(2, state="exited", mounts=[volume_mount("data", "/b"), volume_mount("other")])

    users = volume_users([running, stopped])

    assert users == {"data": [running, stopped], "other": [stopped]}


def test_a_container_mounting_one_volume_twice_counts_once() -> None:
    twice = summary(1, mounts=[volume_mount("data", "/a"), volume_mount("data", "/b")])

    assert volume_users([twice]) == {"data": [twice]}


def test_bind_tmpfs_and_unnamed_mounts_use_no_volume() -> None:
    other = summary(
        1,
        mounts=[
            {"Type": "bind", "Name": "data", "Destination": "/a"},
            {"Type": "tmpfs", "Destination": "/b"},
            volume_mount(None),
            volume_mount(""),
        ],
    )

    assert volume_users([other]) == {}


def test_a_network_is_used_by_a_container_whose_endpoint_carries_its_id() -> None:
    web = summary(1, endpoints={"front": NETWORK_ID})

    assert network_users([web], [network()]) == {NETWORK_ID: [web]}


def test_a_never_started_container_is_matched_by_the_endpoint_name_when_the_id_is_empty() -> None:
    created = summary(1, state="created", endpoints={"front": ""})

    assert network_users([created], [network()]) == {NETWORK_ID: [created]}


def test_a_container_naming_a_network_only_as_its_mode_is_not_a_user() -> None:
    by_id = summary(1, endpoints={}, mode=NETWORK_ID)
    by_name = summary(2, endpoints={}, mode="front")

    assert network_users([by_id, by_name], [network()]) == {}


def test_an_endpoint_with_a_wrong_id_is_not_rescued_by_a_matching_name() -> None:
    other = summary(1, endpoints={"front": OTHER_NETWORK_ID})

    assert network_users([other], [network()]) == {}


def test_endpoints_on_networks_outside_the_list_are_ignored_and_a_repeat_counts_once() -> None:
    both = summary(1, endpoints={"front": NETWORK_ID, "gone": "d" * 64, "front2": NETWORK_ID})

    assert network_users([both], [network()]) == {NETWORK_ID: [both]}


def test_a_swarm_network_with_a_base_36_id_is_matched_like_any_other() -> None:
    swarm_id = "z" * 25
    overlay = network("ov", swarm_id, Driver="overlay", Scope="swarm")
    web = summary(1, endpoints={"ov": swarm_id})

    assert network_users([web], [overlay]) == {swarm_id: [web]}


def test_describe_users_names_the_permitted_in_name_order_and_counts_the_hidden() -> None:
    using = [summary(1, "beta"), summary(2, "Alpha"), summary(3, "secret")]
    permitted = frozenset({container_id(1), container_id(2)})

    named, more, hidden = describe_users(using, permitted)

    assert [user.name for user in named] == ["Alpha", "beta"]
    assert (more, hidden) == (0, 1)


def test_describe_users_counts_the_cut_and_the_hidden_apart() -> None:
    using = [summary(number, f"c{number:02d}") for number in range(1, MAX_USERS_SHOWN + 5)]
    permitted = frozenset(c.id for c in using[:-1])

    named, more, hidden = describe_users(using, permitted)

    assert len(named) == MAX_USERS_SHOWN
    assert (more, hidden) == (3, 1)


def test_describe_users_carries_the_short_id_and_state() -> None:
    using = [summary(0xABCDEF, "web", state="paused")]

    (user,) = describe_users(using, frozenset({using[0].id}))[0]

    assert (user.short_id, user.state) == (using[0].id[:12], "paused")


@pytest.mark.parametrize("limit", [1, 25, MAX_PAGE_SIZE])
def test_a_page_size_inside_the_bounds_is_returned(limit: int) -> None:
    assert check_limit(limit) == limit


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1, True])
def test_a_page_size_outside_the_bounds_is_refused_on_the_limit_field(limit: int) -> None:
    with pytest.raises(ValidationFailed) as refused:
        check_limit(limit)

    assert "limit" in refused.value.fields


def test_no_cursor_decodes_to_no_position() -> None:
    assert decode_position(None) is None


@pytest.mark.parametrize("cursor", ["garbage", "!!!", ""])
def test_a_cursor_that_does_not_decode_is_refused_on_the_cursor_field(cursor: str) -> None:
    with pytest.raises(ValidationFailed) as refused:
        decode_position(cursor)

    assert "cursor" in refused.value.fields


def test_row_ids_are_stable_and_differ_between_swarm_and_hex_ids() -> None:
    assert row_id(NETWORK_ID) == row_id(NETWORK_ID)
    assert len({row_id(NETWORK_ID), row_id("z" * 25), row_id("data")}) == 3


def test_label_values_are_shown_to_an_administrator_and_secrets_withheld_from_a_standard_user() -> (
    None
):
    labels = {"api_key": "k-123", "team": "core"}

    admin = label_views(labels, True)
    standard = label_views(labels, False)

    assert [(v.key, v.value, v.redacted) for v in admin] == [
        ("api_key", "k-123", False),
        ("team", "core", False),
    ]
    assert [(v.key, v.redacted) for v in standard] == [("api_key", True), ("team", False)]
    assert "k-123" not in repr(standard)


@pytest.mark.parametrize("viewer_is_admin", [True, False])
def test_a_label_value_is_clipped_at_4096_characters(viewer_is_admin: bool) -> None:
    (view,) = label_views({"note": "x" * 10_000}, viewer_is_admin)

    assert len(view.value) == 4097 and view.value.endswith("…")


def test_labels_are_listed_in_key_order() -> None:
    assert [v.key for v in label_views({"b": "1", "a": "2"}, True)] == ["a", "b"]


@pytest.mark.parametrize("key", ["password", "secret_token", "api_key", "Credentials"])
def test_an_option_with_a_sensitive_name_has_its_value_withheld(key: str) -> None:
    (view,) = option_views({key: PASSWORD})

    assert (view.value, view.redacted) == (REDACTED, True)


@pytest.mark.parametrize(
    "value",
    [
        f"addr=10.0.0.5,username=svc,password={PASSWORD},vers=3.0",
        f"user=svc%{PASSWORD}",
        f"username=svc%{PASSWORD},rw",
        f"svc:{PASSWORD}@host:/srv",
        f"https://svc:{PASSWORD}@host/path",
        f"pass={PASSWORD}",
        f"credentials={PASSWORD}",
    ],
)
def test_a_secret_embedded_in_an_option_value_never_reaches_the_view(value: str) -> None:
    (view,) = option_views({"o": value})

    assert PASSWORD not in view.value and view.redacted


def test_an_option_value_without_a_secret_is_shown_as_it_is() -> None:
    views = option_views({"device": "//10.0.0.5/share", "type": "cifs"})
    assert [(v.key, v.value, v.redacted) for v in views] == [
        ("device", "//10.0.0.5/share", False),
        ("type", "cifs", False),
    ]


def test_an_option_value_is_clipped_before_it_is_scrubbed() -> None:
    (view,) = option_views({"o": "x" * 10_000})

    assert len(view.value) <= 4096


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (NotFound(1, "gone"), "not_found"),
        (Conflict(1, "busy"), "conflict"),
        (RequestOutcomeUnknown(1, "timed out"), "outcome_unknown"),
        (EnvironmentUnreachable(1, "down"), "unreachable"),
        (EngineError(1, "other"), "engine_error"),
        (ValueError("x"), "engine_error"),
    ],
)
def test_the_way_an_engine_call_ended_is_classified_for_the_failure_row(
    error: BaseException, reason: str
) -> None:
    assert failure_reason(error) == reason


@pytest.mark.parametrize(
    ("name", "driver", "ingress", "kind"),
    [
        ("bridge", "bridge", False, "predefined"),
        ("host", "host", False, "predefined"),
        ("none", "null", False, "predefined"),
        ("ingress", "overlay", True, "system"),
        ("docker_gwbridge", "bridge", False, "system"),
        ("bridge", "macvlan", False, "user"),
        ("host", "bridge", False, "user"),
        ("front", "bridge", False, "user"),
    ],
)
def test_a_network_is_predefined_by_name_and_driver_together(
    name: str, driver: str, ingress: bool, kind: str
) -> None:
    assert network_kind(network(name, Driver=driver, Ingress=ingress)) == kind


def test_the_force_digest_ignores_order_and_repeats_and_changes_with_the_set() -> None:
    first, second = container_id(1), container_id(2)

    assert containers_digest([first, second]) == containers_digest([second, first, first])
    assert containers_digest([first]) != containers_digest([first, second])
    assert len(containers_digest([])) == 64


class RecordingLocks(ObjectLockRegistry):
    """A registry that notes the order in which objects are asked for."""

    def __init__(self) -> None:
        super().__init__(lambda: datetime.now(UTC))
        self.order: list[str] = []

    def hold(
        self, key: ObjectKey, holder: LockHolder, *, wait: float = 0.0
    ) -> AbstractAsyncContextManager[None]:
        self.order.append(f"{key.object_type}:{key.object_id}")
        return super().hold(key, holder, wait=wait)


async def test_hold_all_takes_the_objects_in_a_fixed_order_and_frees_them_afterwards() -> None:
    locks = RecordingLocks()
    network_key = ObjectKey(ENVIRONMENT, "network", NETWORK_ID)
    container_key = ObjectKey(ENVIRONMENT, "container", container_id(1))
    holder = LockHolder(UserActor(FIRST_USER), "user", "connect")

    async with hold_all(locks, [network_key, container_key, network_key], holder):
        held = [locks.holder_of(network_key), locks.holder_of(container_key)]

    assert locks.order == [f"container:{container_id(1)}", f"network:{NETWORK_ID}"]
    assert all(h is not None for h in held)
    assert locks.holder_of(network_key) is None and locks.holder_of(container_key) is None


async def test_a_settled_row_is_recorded_under_the_actor_it_was_written_for(
    swarm: SwarmRig,
) -> None:
    context = await authority_context(
        swarm.db, swarm.clock, swarm.engine.locks, environment_id=ENV_A, subsystem=UPDATER
    )
    authority = context.authority()
    row: dict[str, Any] = {
        "action": "service_updated",
        "target_type": "service",
        "target_id": "a" * 25,
    }

    await write_settled_row(
        swarm.database, swarm.clock, authority, environment_id=ENV_A, detail={}, **row
    )
    await write_settled_row(
        swarm.database,
        swarm.clock,
        authority,
        environment_id=ENV_A,
        detail={},
        failure="outcome_unknown",
        **row,
    )
    await write_settled_row(
        swarm.database, swarm.clock, ADMIN, environment_id=ENV_A, detail={}, **row
    )

    found = await event_rows(swarm.db, "service_updated")
    assert [(r["actor_type"], r["actor_subsystem"], r["result"]) for r in found] == [
        ("subsystem", UPDATER, "success"),
        ("subsystem", UPDATER, "failure"),
        ("user", None, "success"),
    ]
    assert found[1]["detail"]["reason"] == "outcome_unknown"
    assert found[0]["rule_id"] == found[1]["rule_id"] == context.actor.rule_id


ENVIRONMENT = 1
CREATE_EVENT = "object_create_refused"
CLIENT_ERROR = 400
SERVER_ERROR = 500
REDIRECT = 302


class NameInUse(Exception):
    pass


class Refused(Exception):
    def __init__(self, detail: str | None) -> None:
        super().__init__(detail)
        self.detail = detail


def name_in_use() -> Any:
    return NameInUse()


def refused(detail: str | None) -> Any:
    return Refused(detail)


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> AuditState:
    state = AuditState(target_id=None)
    monkeypatch.setattr("app.services.host_objects.current_audit", lambda: state)
    return state


@pytest.mark.parametrize(
    ("error", "reason", "kind"),
    [
        pytest.param(
            RequestOutcomeUnknown(ENVIRONMENT, "timed out"),
            "outcome_unknown",
            "outcome_unknown",
            id="request-outcome-unknown",
        ),
        pytest.param(
            EngineError(ENVIRONMENT, "no status"),
            "outcome_unknown",
            "outcome_unknown",
            id="no-status",
        ),
        pytest.param(
            EngineError(ENVIRONMENT, "redirected", status_code=REDIRECT),
            "outcome_unknown",
            "outcome_unknown",
            id="status-below-400",
        ),
        pytest.param(
            EnvironmentUnreachable(ENVIRONMENT, "down"),
            "unreachable",
            "unreachable",
            id="unreachable",
        ),
    ],
)
def test_a_create_whose_outcome_or_host_is_unknown_maps_to_an_unavailable_environment(
    audit: AuditState, error: EngineError, reason: str, kind: str
) -> None:
    mapped = create_failure(error, event=CREATE_EVENT, name_in_use=name_in_use, refused=refused)

    assert isinstance(mapped, EnvironmentUnavailable)
    assert mapped.kind == kind
    assert audit.reason == reason


def test_a_create_that_conflicts_maps_to_the_name_in_use_error(audit: AuditState) -> None:
    error = Conflict(ENVIRONMENT, "exists", status_code=409)

    mapped = create_failure(error, event=CREATE_EVENT, name_in_use=name_in_use, refused=refused)

    assert isinstance(mapped, NameInUse)
    assert audit.reason == "name_in_use"


@pytest.mark.parametrize("status", [CLIENT_ERROR, SERVER_ERROR])
def test_a_create_the_engine_rejected_maps_to_the_refusal_carrying_the_engine_text(
    audit: AuditState, status: int
) -> None:
    error = EngineError(ENVIRONMENT, "rejected", status_code=status, engine_message="bad option")

    mapped = create_failure(error, event=CREATE_EVENT, name_in_use=name_in_use, refused=refused)

    assert isinstance(mapped, Refused)
    assert mapped.detail == "bad option"
    assert audit.reason == "engine_rejected"


def test_a_refused_create_logs_the_error_class_and_status_under_the_given_event(
    audit: AuditState, logged: Callable[[str], list[dict[str, Any]]]
) -> None:
    error = EngineError(ENVIRONMENT, "rejected", status_code=SERVER_ERROR)

    create_failure(error, event=CREATE_EVENT, name_in_use=name_in_use, refused=refused)

    [record] = logged(CREATE_EVENT)
    assert (record["error"], record["status"]) == ("EngineError", SERVER_ERROR)
