"""The container import upload's body cap, deadline and streamed-upload status."""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message

from app.services.stack_import_archive import MAX_UPLOAD_BYTES
from app.web.csrf import (
    is_container_import_upload,
    is_context_upload,
    is_stack_import_upload,
    is_streamed_upload,
    is_volume_import_upload,
)
from app.web.limits import CONTAINER_IMPORT_BODY_BYTES, MAX_BODY_BYTES, BodySizeLimitMiddleware
from tests.serving.asgi import http_scope
from tests.web.test_context_upload_limits import status_for

PAYLOAD_TOO_LARGE = 413
SMALL_CAP = 100
IMPORT_CAP = 1000
UPLOAD_PATH = "/environments/1/containers/-/import"
OTHER_PATH = "/environments/1/containers/new"


async def read_body(request: Request) -> Response:
    return PlainTextResponse(f"read {len(await request.body())}")


def capped_app(*paths: str) -> ASGIApp:
    routes = [Route(path, read_body, methods=["POST"]) for path in paths]
    return BodySizeLimitMiddleware(
        Starlette(routes=routes),
        max_bytes=SMALL_CAP,
        upload_max_bytes=SMALL_CAP * 2,
        container_import_max_bytes=IMPORT_CAP,
    )


async def test_the_import_upload_has_its_own_cap_and_the_other_routes_keep_theirs() -> None:
    app = capped_app(UPLOAD_PATH, OTHER_PATH)

    within, _ = await status_for(app, UPLOAD_PATH, 400, 400)
    over, _ = await status_for(app, UPLOAD_PATH, 600, 600)
    other_over, _ = await status_for(app, OTHER_PATH, SMALL_CAP + 1)

    assert (within, over, other_over) == (200, PAYLOAD_TOO_LARGE, PAYLOAD_TOO_LARGE)


async def test_a_declared_length_past_the_import_cap_is_refused_without_reading_the_body() -> None:
    app = capped_app(UPLOAD_PATH)

    refused, pulled = await status_for(app, UPLOAD_PATH, 10, declare=IMPORT_CAP + 1)

    assert (refused, pulled) == (PAYLOAD_TOO_LARGE, 0)


async def test_the_import_upload_is_answered_408_when_it_outlasts_its_own_deadline() -> None:
    app = BodySizeLimitMiddleware(
        Starlette(routes=[Route(UPLOAD_PATH, read_body, methods=["POST"])]),
        container_import_total_seconds=0.05,
    )
    sent: list[Message] = []

    async def receive() -> Message:
        await asyncio.sleep(5)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(http_scope(method="POST", path=UPLOAD_PATH, host="testserver"), receive, send)

    assert next(m["status"] for m in sent if m["type"] == "http.response.start") == 408


def test_the_cap_is_the_documented_size_and_the_global_one_is_not_raised() -> None:
    assert CONTAINER_IMPORT_BODY_BYTES == MAX_UPLOAD_BYTES + 64 * 1024
    assert MAX_BODY_BYTES == 256 * 1024


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        pytest.param("POST", UPLOAD_PATH, True, id="upload"),
        pytest.param("POST", "/environments/12/containers/-/import", True, id="ids"),
        pytest.param("GET", UPLOAD_PATH, False, id="wrong-method"),
        pytest.param("POST", "/environments/0/containers/-/import", False, id="zero"),
        pytest.param("POST", f"{UPLOAD_PATH}/abc", False, id="the-review"),
        pytest.param("POST", f"{UPLOAD_PATH}/abc/unlock", False, id="the-unlock"),
        pytest.param("POST", f"/x{UPLOAD_PATH}", False, id="prefixed"),
        pytest.param("POST", OTHER_PATH, False, id="other-route"),
    ],
)
def test_only_the_upload_route_of_a_container_import_is_a_streamed_upload(
    method: str, path: str, expected: bool
) -> None:
    assert is_container_import_upload(method, path) is expected
    assert is_streamed_upload(method, path) is expected
    assert not (
        is_context_upload(method, path)
        or is_volume_import_upload(method, path)
        or is_stack_import_upload(method, path)
    )
