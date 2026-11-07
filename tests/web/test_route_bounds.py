"""Every integer id parameter is bounded: an id beyond the database range is a 404 or 422."""

import re
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.web.identifiers import MAX_ID
from tests.auth_support import csrf_headers
from tests.web.route_support import fill

HUGE = 2**70
UNPROCESSABLE = 422
NOT_FOUND = 404
REFUSED = {UNPROCESSABLE, NOT_FOUND}
MIN_BOUNDED_PARAMETERS = 50
MIN_INTEGER_ROUTES = 30


def integer_schemas(schema: dict[str, Any]) -> list[dict[str, Any]]:
    options = schema.get("anyOf") or [schema]
    return [option for option in options if option.get("type") == "integer"]


def integer_parameters(app: FastAPI) -> list[tuple[str, str, dict[str, Any]]]:
    found: list[tuple[str, str, dict[str, Any]]] = []
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                if parameter["in"] not in ("path", "query"):
                    continue
                for schema in integer_schemas(parameter.get("schema", {})):
                    found.append(
                        (f"{method.upper()} {path} {parameter['name']}", parameter["name"], schema)
                    )
    return found


def test_every_integer_path_and_query_parameter_has_an_upper_bound_within_the_database_range(
    app: FastAPI,
) -> None:
    parameters = integer_parameters(app)

    unbounded = [
        label
        for label, _, schema in parameters
        if schema.get("maximum", schema.get("exclusiveMaximum", 2**64)) > MAX_ID
    ]
    assert len(parameters) >= MIN_BOUNDED_PARAMETERS
    assert unbounded == []


def test_every_integer_path_and_query_parameter_has_a_lower_bound_of_one(app: FastAPI) -> None:
    unbounded_below = [
        label
        for label, name, schema in integer_parameters(app)
        if schema.get("minimum", 0) < 1 and name != "limit" and not name.startswith("page")
    ]

    assert unbounded_below == []


def get_paths_with_integer_parameter(app: FastAPI) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path, operations in app.openapi()["paths"].items():
        parameters = operations.get("get", {}).get("parameters", [])
        integers = {
            p["name"]
            for p in parameters
            if p["in"] == "path" and integer_schemas(p.get("schema", {}))
        }
        if integers:
            found[path] = {"integers": integers}
    return found


async def test_an_id_beyond_the_database_range_is_refused_not_a_server_error_on_every_read_route(
    admin_client: AsyncClient, app: FastAPI
) -> None:
    routes = get_paths_with_integer_parameter(app)
    statuses: dict[str, int] = {}

    for path, details in routes.items():
        values: dict[str, object] = dict.fromkeys(details["integers"], HUGE)
        others = {n: "x" for n in re.findall(r"\{(\w+)\}", path) if n not in values}
        response = await admin_client.get(fill(path, **values, **others))
        statuses[path] = response.status_code

    assert len(routes) >= MIN_INTEGER_ROUTES
    assert {p: c for p, c in statuses.items() if c not in REFUSED and "/api/" not in p} == {}


POST_ROUTES = [
    "/users/{id}/disable",
    "/users/{id}/delete",
    "/users/{id}/password",
    "/teams/{id}/delete",
    "/users/{id}/tokens/{id}/revoke",
    "/jobs/{id}/run",
    "/automation/rules/{id}/run",
]


@pytest.mark.parametrize("template", POST_ROUTES)
async def test_an_id_beyond_the_database_range_is_refused_not_a_server_error_on_a_change(
    admin_client: AsyncClient, template: str
) -> None:
    path = template.replace("{id}", str(HUGE))

    response = await admin_client.post(path, headers=csrf_headers(admin_client))

    assert response.status_code in REFUSED
