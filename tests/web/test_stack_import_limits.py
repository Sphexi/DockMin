"""The stack import upload's body cap, deadline and streamed-upload status."""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message

from app.services.stack_import_archive import MAX_UPLOAD_BYTES
from app.web.csrf import (
    is_context_upload,
    is_stack_import_upload,
    is_streamed_upload,
    is_volume_import_upload,
)
from app.web.limits import (
    MAX_BODY_BYTES,
    STACK_IMPORT_BODY_BYTES,
    BodySizeLimitMiddleware,
)
from tests.serving.asgi import http_scope
from tests.web.test_context_upload_limits import status_for

PAYLOAD_TOO_LARGE = 413
SMALL_CAP = 100
IMPORT_CAP = 1000
COMPOSE_PATH = "/environments/1/stacks/-/import"
SWARM_PATH = "/environments/1/swarm/stacks/-/import"
OTHER_PATH = "/environments/1/stacks/-/new"


async def read_body(request: Request) -> Response:
    return PlainTextResponse(f"read {len(await request.body())}")


def capped_app(*paths: str) -> ASGIApp:
    routes = [Route(path, read_body, methods=["POST"]) for path in paths]
    return BodySizeLimitMiddleware(
        Starlette(routes=routes),
        max_bytes=SMALL_CAP,
        upload_max_bytes=SMALL_CAP * 2,
        stack_import_max_bytes=IMPORT_CAP,
    )


@pytest.mark.parametrize("path", [COMPOSE_PATH, SWARM_PATH])
async def test_the_import_upload_has_its_own_cap_and_the_other_routes_keep_theirs(
    path: str,
) -> None:
    app = capped_app(path, OTHER_PATH)

    within, _ = await status_for(app, path, 400, 400)
    over, _ = await status_for(app, path, 600, 600)
    other_over, _ = await status_for(app, OTHER_PATH, SMALL_CAP + 1)

    assert (within, over, other_over) == (200, PAYLOAD_TOO_LARGE, PAYLOAD_TOO_LARGE)


async def test_a_declared_length_past_the_import_cap_is_refused_without_reading_the_body() -> None:
    app = capped_app(COMPOSE_PATH)

    refused, pulled = await status_for(app, COMPOSE_PATH, 10, declare=IMPORT_CAP + 1)

    assert (refused, pulled) == (PAYLOAD_TOO_LARGE, 0)


async def test_the_import_upload_is_answered_408_when_it_outlasts_its_own_deadline() -> None:
    app = BodySizeLimitMiddleware(
        Starlette(routes=[Route(COMPOSE_PATH, read_body, methods=["POST"])]),
        stack_import_total_seconds=0.05,
    )
    sent: list[Message] = []

    async def receive() -> Message:
        await asyncio.sleep(5)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(http_scope(method="POST", path=COMPOSE_PATH, host="testserver"), receive, send)

    assert next(m["status"] for m in sent if m["type"] == "http.response.start") == 408


def test_the_cap_is_the_documented_size_and_the_global_one_is_not_raised() -> None:
    assert MAX_UPLOAD_BYTES == 7 * 1024**3
    assert STACK_IMPORT_BODY_BYTES == MAX_UPLOAD_BYTES + 64 * 1024
    assert MAX_BODY_BYTES == 256 * 1024


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        pytest.param("POST", COMPOSE_PATH, True, id="compose"),
        pytest.param("POST", SWARM_PATH, True, id="swarm"),
        pytest.param("POST", "/environments/12/stacks/-/import", True, id="ids"),
        pytest.param("GET", COMPOSE_PATH, False, id="wrong-method"),
        pytest.param("POST", "/environments/0/stacks/-/import", False, id="zero"),
        pytest.param("POST", "/environments/1/stacks/import/import", False, id="a-stack-name"),
        pytest.param("POST", f"{COMPOSE_PATH}/abc", False, id="the-review"),
        pytest.param("POST", f"{COMPOSE_PATH}/abc/unlock", False, id="the-unlock"),
        pytest.param("POST", f"{SWARM_PATH}/abc/cancel", False, id="the-cancel"),
        pytest.param("POST", f"/x{COMPOSE_PATH}", False, id="prefixed"),
        pytest.param("POST", OTHER_PATH, False, id="other-route"),
    ],
)
def test_only_the_upload_route_of_a_stack_import_is_a_streamed_upload(
    method: str, path: str, expected: bool
) -> None:
    assert is_stack_import_upload(method, path) is expected
    assert is_streamed_upload(method, path) is expected
    assert (
        is_context_upload(method, path) is False and is_volume_import_upload(method, path) is False
    )
