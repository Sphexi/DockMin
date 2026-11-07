"""GET /api/v1/environments: bearer-only, paginated, redacted for standard users, no TLS fields."""

from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

from app.db.repositories.pagination import CursorPosition, encode_cursor
from tests.auth_support import Browsers
from tests.web.environment_support import (
    DOWN_ENDPOINT,
    DOWN_NAME,
    LOCAL_NAME,
    LOCAL_PAUSED,
    LOCAL_SOCKET,
    LOCAL_UNHEALTHY,
    REMOTE_NAME,
    World,
    bearer,
    issue_token,
)

LIST_PATH = "/api/v1/environments"
OK = 200
BAD_REQUEST = 400
UNAUTHENTICATED = 401
NOT_FOUND = 404
UNPROCESSABLE = 422
ABSENT_ID = 9999
BEYOND_INT64 = 2**63
TLS_FIELD_NAMES = frozenset(
    {"tls", "ca_certificate", "client_certificate", "client_key", "certificate", "pem", "key"}
)
ALL_NAMES = [DOWN_NAME, LOCAL_NAME, REMOTE_NAME]


def names(response_body: dict[str, Any]) -> list[str]:
    return [item["name"] for item in response_body["items"]]


def property_names(document: Any) -> set[str]:
    """Collect every object key in a JSON document or schema, at any depth."""
    if isinstance(document, dict):
        return set(document) | {
            name for value in document.values() for name in property_names(value)
        }
    if isinstance(document, list):
        return {name for value in document for name in property_names(value)}
    return set()


async def test_a_request_without_a_bearer_token_is_refused_even_with_a_signed_in_session(
    browsers: Browsers, admin_client: AsyncClient, world: World
) -> None:
    anonymous = browsers.open()
    paths = [LIST_PATH, f"{LIST_PATH}/{world.local_id}"]

    statuses = {
        (who, path): (await client.get(path)).status_code
        for who, client in (("anonymous", anonymous), ("session cookie", admin_client))
        for path in paths
    }

    assert set(statuses.values()) == {UNAUTHENTICATED}


async def test_an_administrators_token_pages_through_every_environment_with_connection_and_counts(
    browsers: Browsers, admin_client: AsyncClient, world: World
) -> None:
    caller = browsers.open()
    headers = bearer(await issue_token(admin_client))

    first = (await caller.get(f"{LIST_PATH}?limit=2", headers=headers)).json()
    second = (
        await caller.get(f"{LIST_PATH}?limit=2&cursor={first['next_cursor']}", headers=headers)
    ).json()

    assert names(first) == ALL_NAMES[:2]
    assert names(second) == ALL_NAMES[2:]
    assert second["next_cursor"] is None
    by_name = {item["name"]: item for item in first["items"] + second["items"]}
    local = by_name[LOCAL_NAME]
    assert local["connection"] == {
        "kind": "unix_socket",
        "endpoint": f"unix://{LOCAL_SOCKET}",
        "is_local": True,
    }
    assert local["snapshot"]["counts"]["container_count"] == 7
    assert local["snapshot"]["counts"]["container_paused_count"] == LOCAL_PAUSED
    assert local["snapshot"]["counts"]["container_unhealthy_count"] == LOCAL_UNHEALTHY
    assert by_name[DOWN_NAME]["connection"]["endpoint"] == DOWN_ENDPOINT
    assert by_name[DOWN_NAME]["snapshot"]["counts"] is None
    assert by_name[DOWN_NAME]["failed_stage"] == "tcp"
    assert world.engine_clients == []


async def test_the_list_filters_by_name_status_group_and_tag(
    browsers: Browsers, admin_client: AsyncClient, world: World
) -> None:
    caller = browsers.open()
    headers = bearer(await issue_token(admin_client))
    queries = [
        "q=remote",
        "status=unreachable",
        f"group={world.group_id}",
        "group=none",
        f"tag={world.tag_id}&tag={world.other_tag_id}",
    ]

    listed = {
        query: names((await caller.get(f"{LIST_PATH}?{query}", headers=headers)).json())
        for query in queries
    }

    assert listed == {
        "q=remote": [REMOTE_NAME],
        "status=unreachable": [DOWN_NAME],
        f"group={world.group_id}": [LOCAL_NAME],
        "group=none": [DOWN_NAME, REMOTE_NAME],
        f"tag={world.tag_id}&tag={world.other_tag_id}": [DOWN_NAME],
    }


async def test_out_of_range_ids_filters_and_cursors_are_client_errors_and_never_server_errors(
    browsers: Browsers, admin_client: AsyncClient, world: World
) -> None:
    caller = browsers.open()
    headers = bearer(await issue_token(admin_client))
    hostile_cursor = encode_cursor(CursorPosition("a", BEYOND_INT64))
    requests = {
        f"{LIST_PATH}/{ABSENT_ID}": NOT_FOUND,
        f"{LIST_PATH}/0": UNPROCESSABLE,
        f"{LIST_PATH}/{BEYOND_INT64}": UNPROCESSABLE,
        f"{LIST_PATH}/abc": UNPROCESSABLE,
        f"{LIST_PATH}?limit=0": UNPROCESSABLE,
        f"{LIST_PATH}?limit=1000000": UNPROCESSABLE,
        f"{LIST_PATH}?status=bogus": UNPROCESSABLE,
        f"{LIST_PATH}?group=abc": UNPROCESSABLE,
        f"{LIST_PATH}?tag={BEYOND_INT64}": UNPROCESSABLE,
        f"{LIST_PATH}?{'&'.join(['tag=1'] * 11)}": UNPROCESSABLE,
        f"{LIST_PATH}?cursor=not-a-cursor": BAD_REQUEST,
        f"{LIST_PATH}?cursor={hostile_cursor}": BAD_REQUEST,
    }

    statuses = {path: (await caller.get(path, headers=headers)).status_code for path in requests}

    assert statuses == requests


async def test_a_standard_users_token_lists_only_their_environment_without_connection_or_counts(
    browsers: Browsers, standard_client: AsyncClient, world: World
) -> None:
    caller = browsers.open()
    headers = bearer(await issue_token(standard_client))

    listing = (await caller.get(LIST_PATH, headers=headers)).json()
    granted = await caller.get(f"{LIST_PATH}/{world.local_id}", headers=headers)
    denied = await caller.get(f"{LIST_PATH}/{world.remote_id}", headers=headers)
    missing = await caller.get(f"{LIST_PATH}/{ABSENT_ID}", headers=headers)

    [only] = listing["items"]
    assert only["name"] == LOCAL_NAME
    assert (only["connection"], only["snapshot"], only["failed_stage"]) == (None, None, None)
    assert granted.status_code == OK
    assert granted.json()["created_at"] is None
    assert LOCAL_SOCKET not in granted.text
    assert denied.status_code == missing.status_code == NOT_FOUND
    assert denied.json() == missing.json()


async def test_no_environment_response_or_schema_has_a_field_that_could_hold_tls_material(
    started_app: FastAPI, browsers: Browsers, admin_client: AsyncClient, world: World
) -> None:
    caller = browsers.open()
    headers = bearer(await issue_token(admin_client))
    detail = (await caller.get(f"{LIST_PATH}/{world.remote_id}", headers=headers)).json()
    listing = (await caller.get(LIST_PATH, headers=headers)).json()
    schemas = started_app.openapi()["components"]["schemas"]
    environment_schemas = {
        name: schema for name, schema in schemas.items() if "Environment" in name
    }

    assert environment_schemas
    assert property_names(detail) & TLS_FIELD_NAMES == set()
    assert property_names(listing) & TLS_FIELD_NAMES == set()
    assert property_names(environment_schemas) & TLS_FIELD_NAMES == set()
