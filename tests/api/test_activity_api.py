"""GET /api/v1/activity: bearer tokens, administrators only, filters, paging and bounds."""

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.auth_support import ADMIN_ID, ADMIN_SESSION_TOKEN, STANDARD_SESSION_TOKEN, Browsers
from tests.web.environment_support import bearer, issue_token

BASE = "/api/v1/activity"
EVENTS = 40
PAGE = 7
SMALL_PAGE = 5
NOTE_INDEX = 5
NOTE = '=x,"y"\nz'
START = datetime(2026, 2, 1, 12, 0, 0, tzinfo=UTC)
ITEM_KEYS = {
    "id",
    "occurred_at",
    "category",
    "actor_type",
    "actor_user_id",
    "actor_username",
    "actor_subsystem",
    "rule_id",
    "job_run_id",
    "request_id",
    "environment_id",
    "environment_name",
    "target_type",
    "target_id",
    "action",
    "result",
    "detail",
}
INSERT_EVENT = (
    "INSERT INTO activity_event (category, actor_type, actor_user_id, environment_id, "
    "target_type, target_id, action, result, detail, occurred_at) "
    "VALUES ('user_action', 'user', ?, ?, ?, ?, ?, ?, ?, ?)"
)
ENVIRONMENT_ID = 1
ENVIRONMENT_NAME = "local"
UNAUTHENTICATED = 401
FORBIDDEN = 403
BAD_REQUEST = 400
UNPROCESSABLE = 422
OK = 200


def stamp(index: int) -> str:
    return (START - timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_row(index: int) -> tuple[Any, ...]:
    has_environment = index % 2
    detail = {"i": index, "note": NOTE} if index == NOTE_INDEX else {"i": index}
    return (
        ADMIN_ID,
        ENVIRONMENT_ID if has_environment else None,
        "container" if has_environment else "stack",
        f"t{index}",
        "container_restarted" if index % 3 else "stack_redeployed",
        "success" if index % 4 else "failure",
        json.dumps(detail),
        stamp(index),
    )


def cursor_token(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


FILTERS: dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], bool]]] = {
    "result": ({"result": "failure"}, lambda item: item["result"] == "failure"),
    "category": ({"category": "user_action"}, lambda item: item["category"] == "user_action"),
    "actor-type": ({"actor_type": "user"}, lambda item: item["actor_type"] == "user"),
    "action": (
        {"action": "stack_redeployed"},
        lambda item: item["action"] == "stack_redeployed",
    ),
    "user": ({"user_id": ADMIN_ID}, lambda item: item["actor_user_id"] == ADMIN_ID),
    "environment": (
        {"environment_id": ENVIRONMENT_ID},
        lambda item: item["environment_name"] == ENVIRONMENT_NAME,
    ),
    "target-type": (
        {"environment_id": ENVIRONMENT_ID, "target_type": "container"},
        lambda item: item["target_type"] == "container",
    ),
    "target": (
        {"environment_id": ENVIRONMENT_ID, "target_type": "container", "target_id": "t1"},
        lambda item: item["target_id"] == "t1",
    ),
}
BAD_PARAMETERS: dict[str, dict[str, Any]] = {
    "category": {"category": "zzz"},
    "actor-type": {"actor_type": "zzz"},
    "result": {"result": "zzz"},
    "limit-zero": {"limit": 0},
    "limit-too-large": {"limit": 501},
    "user-id-zero": {"user_id": 0},
    "rule-id-beyond-int64": {"rule_id": 2**63},
    "target-type-pattern": {"target_type": "Bad Type"},
    "action-too-long": {"action": "x" * 65},
    "empty-target-id": {"target_id": ""},
    "unknown-subsystem": {"subsystem": "no-such-subsystem"},
    "target-id-without-type": {"target_id": "t1"},
    "target-type-without-id-or-environment": {"target_type": "container"},
    "naive-from": {"from": "2026-01-01T00:00:00"},
    "date-only-to": {"to": "2026-01-01"},
    "garbage-from": {"from": "garbage"},
    "from-before-1970-after-offset": {"from": "0001-01-01T00:00:00+14:00"},
    "to-after-9998": {"to": "9999-12-31T23:59:59Z"},
    "from-just-before-1970": {"from": "1969-12-31T23:59:59Z"},
}
ACCEPTED_PARAMETERS: dict[str, dict[str, Any]] = {
    "from-epoch": {"from": "1970-01-01T00:00:00Z"},
    "to-9998": {"to": "9998-12-31T23:59:59Z"},
    "both": {"from": "1970-01-01T00:00:00Z", "to": "9998-12-31T23:59:59+00:00"},
    "registered-subsystem": {"subsystem": "stack-restarter"},
}
CURSORS = {
    "not-base64": ("!!!", BAD_REQUEST),
    "lone-surrogate": (cursor_token('["\\ud800", 1]'), BAD_REQUEST),
    "wrong-length": (cursor_token("[1]"), BAD_REQUEST),
    "too-long": ("x" * 513, UNPROCESSABLE),
}


async def statuses(
    api: AsyncClient, headers: dict[str, str], cases: dict[str, dict[str, Any]]
) -> dict[str, int]:
    """Request the route once per case and return each case's status."""
    return {
        name: (await api.get(BASE, params=params, headers=headers)).status_code
        for name, params in cases.items()
    }


@pytest.fixture
async def seeded(started_app: FastAPI) -> FastAPI:
    connection = started_app.state.database.connect()
    try:
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO environment (id, name, connection_kind, socket_path, created_at, "
            "updated_at) VALUES (?, ?, 'unix_socket', '/run/docker.sock', ?, ?)",
            (ENVIRONMENT_ID, ENVIRONMENT_NAME, stamp(0), stamp(0)),
        )
        connection.executemany(INSERT_EVENT, [event_row(index) for index in range(EVENTS)])
        connection.execute("COMMIT")
    finally:
        connection.close()
    return started_app


@pytest.fixture
async def admin_headers(browsers: Browsers) -> dict[str, str]:
    return bearer(await issue_token(browsers.open(session_token=ADMIN_SESSION_TOKEN)))


@pytest.fixture
async def standard_headers(browsers: Browsers) -> dict[str, str]:
    return bearer(await issue_token(browsers.open(session_token=STANDARD_SESSION_TOKEN)))


@pytest.fixture
def api(browsers: Browsers) -> AsyncClient:
    """A client with no cookie, so only the bearer header can authenticate it."""
    return browsers.open()


async def walk(
    api: AsyncClient, headers: dict[str, str], *, limit: int = 500, **params: Any
) -> list[dict[str, Any]]:
    """Follow next_cursor to the end, failing on any status but 200."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        query = {k: v for k, v in {**params, "limit": limit, "cursor": cursor}.items() if v}
        response = await api.get(BASE, params=query, headers=headers)
        assert response.status_code == OK, (params, response.text)
        body = response.json()
        items += body["items"]
        cursor = body["next_cursor"]
        if cursor is None:
            return items


async def test_a_request_without_a_valid_bearer_token_is_unauthenticated(
    api: AsyncClient, browsers: Browsers
) -> None:
    cookie_only = browsers.open(session_token=ADMIN_SESSION_TOKEN)

    answers = [
        (await api.get(BASE)).status_code,
        (await cookie_only.get(BASE)).status_code,
        (await api.get(BASE, headers=bearer("bogus"))).status_code,
    ]

    assert answers == [UNAUTHENTICATED] * 3


async def test_a_standard_users_token_is_forbidden_whatever_the_query(
    api: AsyncClient, standard_headers: dict[str, str]
) -> None:
    plain = await api.get(BASE, headers=standard_headers)
    invalid = await api.get(BASE + "?limit=9999&category=zzz", headers=standard_headers)

    assert (plain.status_code, invalid.status_code) == (FORBIDDEN, FORBIDDEN)
    assert plain.json()["error"]["status"] == FORBIDDEN


async def test_items_carry_the_documented_fields_newest_first_and_the_cursor_visits_each_once(
    api: AsyncClient, admin_headers: dict[str, str], seeded: FastAPI
) -> None:
    first = await api.get(BASE, params={"limit": SMALL_PAGE}, headers=admin_headers)
    everything = await walk(api, admin_headers, limit=PAGE)

    body = first.json()
    times = [item["occurred_at"] for item in body["items"]]
    identifiers = [item["id"] for item in everything]
    assert {frozenset(item) for item in body["items"]} == {frozenset(ITEM_KEYS)}
    assert times == sorted(times, reverse=True)
    assert body["next_cursor"]
    assert body["items"][0]["actor_username"]
    assert len(identifiers) == len(set(identifiers)) >= EVENTS


async def test_every_filter_returns_only_matching_events_and_some_of_them(
    api: AsyncClient, admin_headers: dict[str, str], seeded: FastAPI
) -> None:
    results = {
        name: await walk(api, admin_headers, **params) for name, (params, _) in FILTERS.items()
    }

    empty = [name for name, items in results.items() if not items]
    wrong = [name for name, items in results.items() if not all(FILTERS[name][1](i) for i in items)]
    assert (empty, wrong) == ([], [])


async def test_a_time_range_includes_its_start_excludes_its_end_and_detail_is_as_stored(
    api: AsyncClient, admin_headers: dict[str, str], seeded: FastAPI
) -> None:
    first, last = stamp(10), stamp(5)

    bounds: dict[str, Any] = {"from": first, "to": last}
    ranged = await walk(api, admin_headers, **bounds)
    everything = await walk(api, admin_headers)

    times = {item["occurred_at"] for item in ranged}
    [noted] = [item for item in everything if item["detail"].get("i") == NOTE_INDEX]
    assert (min(times), last in times) == (first, False)
    assert noted["detail"]["note"] == NOTE


async def test_a_cursor_the_service_did_not_issue_is_refused_not_a_server_error(
    api: AsyncClient, admin_headers: dict[str, str], seeded: FastAPI
) -> None:
    answers = await statuses(
        api, admin_headers, {name: {"cursor": token} for name, (token, _) in CURSORS.items()}
    )

    assert answers == {name: status for name, (_, status) in CURSORS.items()}


async def test_a_parameter_outside_its_bounds_is_unprocessable(
    api: AsyncClient, admin_headers: dict[str, str], seeded: FastAPI
) -> None:
    answers = await statuses(api, admin_headers, BAD_PARAMETERS)

    assert answers == dict.fromkeys(BAD_PARAMETERS, UNPROCESSABLE)


async def test_a_time_between_1970_and_9998_and_a_registered_subsystem_are_accepted(
    api: AsyncClient, admin_headers: dict[str, str], seeded: FastAPI
) -> None:
    answers = await statuses(api, admin_headers, ACCEPTED_PARAMETERS)

    assert answers == dict.fromkeys(ACCEPTED_PARAMETERS, OK)
