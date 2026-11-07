"""Guard: no /api route is both unauthenticated and state-changing.

Routes are found through the OpenAPI schema, so one declared with include_in_schema=False is not
seen.
"""

import re

from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.dependencies import require_user
from app.paths import HOOKS_PATH

MUTATING_METHODS = ("post", "put", "patch", "delete")
UNAUTHENTICATED = 401
NOT_FOUND = 404
TOKEN_AUTHENTICATED = frozenset({"POST /api/v1/hooks", "POST /api/v1/hooks/{token}"})
PATH_PARAMETER = re.compile(r"\{[^}]+\}")


async def unauthenticated_mutations(app: FastAPI) -> list[str]:
    """Return 'METHOD path' for each state-changing /api route that does not refuse a bad token."""
    offenders: list[str] = []
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        for path, operations in app.openapi()["paths"].items():
            if path != "/api" and not path.startswith("/api/"):
                continue
            for method in MUTATING_METHODS & operations.keys():
                if f"{method.upper()} {path}" in TOKEN_AUTHENTICATED:
                    continue
                response = await http.request(
                    method.upper(),
                    PATH_PARAMETER.sub("1", path),
                    headers={"Authorization": "Bearer not-a-token"},
                )
                if response.status_code != UNAUTHENTICATED:
                    offenders.append(f"{method.upper()} {path}")
    return sorted(offenders)


async def test_unauthenticated_mutations_flags_an_open_state_changing_api_route() -> None:
    app = FastAPI()
    router = APIRouter(prefix="/api")

    @router.post("/open")
    async def open_route() -> None: ...

    @router.post("/guarded", dependencies=[Depends(require_user)])
    async def guarded_route() -> None: ...

    @router.get("/read")
    async def read_route() -> None: ...

    app.include_router(router)

    assert await unauthenticated_mutations(app) == ["POST /api/open"]


async def test_no_api_route_is_both_unauthenticated_and_state_changing(
    started_app: FastAPI,
) -> None:
    assert await unauthenticated_mutations(started_app) == []


async def test_every_token_authenticated_route_is_a_registered_state_changing_route(
    started_app: FastAPI,
) -> None:
    routes = {
        f"{method.upper()} {path}"
        for path, operations in started_app.openapi()["paths"].items()
        for method in MUTATING_METHODS & operations.keys()
    }

    assert routes >= TOKEN_AUTHENTICATED


async def test_the_webhook_routes_refuse_an_unusable_token_without_a_cookie_or_csrf(
    started_app: FastAPI,
) -> None:
    transport = ASGITransport(app=started_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        by_path = await http.post("/api/v1/hooks/not-a-token")
        by_header = await http.post(
            "/api/v1/hooks", headers={"Authorization": "Bearer not-a-token"}
        )

    assert by_path.status_code == by_header.status_code == NOT_FOUND
    assert "set-cookie" not in by_path.headers


async def test_exactly_two_routes_exist_under_the_webhook_prefix_and_both_are_post(
    started_app: FastAPI,
) -> None:
    served = {
        f"{method.upper()} {path}"
        for path, operations in started_app.openapi()["paths"].items()
        if path == HOOKS_PATH or path.startswith(f"{HOOKS_PATH}/")
        for method in operations
    }

    assert served == TOKEN_AUTHENTICATED
