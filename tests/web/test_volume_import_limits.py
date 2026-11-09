"""The volume import upload's body cap, deadline and streamed-upload status."""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message

from app.services.volume_import_archive import MAX_UPLOAD_BYTES
from app.web.csrf import is_context_upload, is_streamed_upload, is_volume_import_upload
from app.web.limits import (
    MAX_BODY_BYTES,
    VOLUME_IMPORT_BODY_BYTES,
    BodySizeLimitMiddleware,
)
from tests.serving.asgi import http_scope
from tests.web.test_context_upload_limits import status_for

PAYLOAD_TOO_LARGE = 413
SMALL_CAP = 100
IMPORT_CAP = 1000
IMPORT_PATH = "/environments/1/volumes/-/import"
CONTEXT_PATH = "/environments/1/stacks/shop/contexts/-/upload"
OTHER_PATH = "/environments/1/volumes/-/new"


async def read_body(request: Request) -> Response:
    return PlainTextResponse(f"read {len(await request.body())}")


def capped_app(*paths: str) -> ASGIApp:
    routes = [Route(path, read_body, methods=["POST"]) for path in paths]
    return BodySizeLimitMiddleware(
        Starlette(routes=routes),
        max_bytes=SMALL_CAP,
        upload_max_bytes=SMALL_CAP * 2,
        volume_import_max_bytes=IMPORT_CAP,
    )


async def test_the_import_upload_has_its_own_cap_and_the_other_routes_keep_theirs() -> None:
    app = capped_app(IMPORT_PATH, CONTEXT_PATH, OTHER_PATH)

    within, _ = await status_for(app, IMPORT_PATH, 400, 400)
    over, _ = await status_for(app, IMPORT_PATH, 600, 600)
    context_over, _ = await status_for(app, CONTEXT_PATH, SMALL_CAP * 2 + 1)
    other_over, _ = await status_for(app, OTHER_PATH, SMALL_CAP + 1)

    assert (within, over) == (200, PAYLOAD_TOO_LARGE)
    assert (context_over, other_over) == (PAYLOAD_TOO_LARGE, PAYLOAD_TOO_LARGE)


async def test_a_declared_length_past_the_import_cap_is_refused_without_reading_the_body() -> None:
    app = capped_app(IMPORT_PATH)

    refused, pulled = await status_for(app, IMPORT_PATH, 10, declare=IMPORT_CAP + 1)

    assert (refused, pulled) == (PAYLOAD_TOO_LARGE, 0)


async def test_the_import_upload_is_answered_408_when_it_outlasts_its_own_deadline() -> None:
    app = BodySizeLimitMiddleware(
        Starlette(routes=[Route(IMPORT_PATH, read_body, methods=["POST"])]),
        volume_import_total_seconds=0.05,
    )
    sent: list[Message] = []

    async def receive() -> Message:
        await asyncio.sleep(5)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(http_scope(method="POST", path=IMPORT_PATH, host="testserver"), receive, send)

    assert next(m["status"] for m in sent if m["type"] == "http.response.start") == 408


def test_the_caps_are_the_documented_sizes_and_the_global_one_is_not_raised() -> None:
    assert MAX_UPLOAD_BYTES == 7 * 1024**3
    assert VOLUME_IMPORT_BODY_BYTES == MAX_UPLOAD_BYTES + 64 * 1024
    assert MAX_BODY_BYTES == 256 * 1024


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        pytest.param("POST", IMPORT_PATH, True, id="the-route"),
        pytest.param("POST", "/environments/12/volumes/-/import", True, id="ids"),
        pytest.param("GET", IMPORT_PATH, False, id="wrong-method"),
        pytest.param("POST", "/environments/0/volumes/-/import", False, id="zero"),
        pytest.param("POST", "/environments/1/volumes/import/import", False, id="a-volume-name"),
        pytest.param("POST", f"{IMPORT_PATH}/abc", False, id="the-review"),
        pytest.param("POST", f"{IMPORT_PATH}/abc/cancel", False, id="the-cancel"),
        pytest.param("POST", f"/x{IMPORT_PATH}", False, id="prefixed"),
        pytest.param("POST", OTHER_PATH, False, id="other-route"),
    ],
)
def test_only_the_upload_route_of_a_volume_import_is_a_streamed_upload(
    method: str, path: str, expected: bool
) -> None:
    assert is_volume_import_upload(method, path) is expected
    assert is_streamed_upload(method, path) is expected
    assert is_context_upload(method, path) is False
