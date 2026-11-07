"""The build context upload's body cap, its CSRF exemption and the global cap it leaves alone."""

from collections.abc import AsyncIterator

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message

from app.db.repositories.stack_build_context import MAX_CONTEXT_UPLOAD_BYTES
from app.web.csrf import is_context_upload, is_streamed_upload
from app.web.limits import (
    CONTEXT_UPLOAD_BODY_BYTES,
    MAX_BODY_BYTES,
    BodySizeLimitMiddleware,
)
from tests.serving.asgi import http_scope

PAYLOAD_TOO_LARGE = 413
SMALL_CAP = 100
UPLOAD_CAP = 1000
UPLOAD_PATH = "/environments/1/stacks/shop/contexts/-/upload"
OTHER_PATH = "/environments/1/stacks/shop/files/-/new"


async def read_body(request: Request) -> Response:
    return PlainTextResponse(f"read {len(await request.body())}")


def capped_app(*paths: str) -> ASGIApp:
    routes = [Route(path, read_body, methods=["POST"]) for path in paths]
    return BodySizeLimitMiddleware(
        Starlette(routes=routes), max_bytes=SMALL_CAP, upload_max_bytes=UPLOAD_CAP
    )


async def chunks(*sizes: int) -> AsyncIterator[Message]:
    for index, size in enumerate(sizes):
        yield {"type": "http.request", "body": b"a" * size, "more_body": index < len(sizes) - 1}


async def status_for(
    app: ASGIApp, path: str, *sizes: int, declare: int | None = None
) -> tuple[int, int]:
    sent: list[Message] = []
    messages = chunks(*sizes)
    pulled = 0

    async def receive() -> Message:
        nonlocal pulled
        pulled += 1
        return await anext(messages, {"type": "http.disconnect"})

    async def send(message: Message) -> None:
        sent.append(message)

    headers = () if declare is None else ((b"content-length", str(declare).encode()),)
    scope = http_scope(method="POST", path=path, host="testserver", extra_headers=headers)
    await app(scope, receive, send)
    status: int = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, pulled


async def test_the_context_upload_has_its_own_cap_and_every_other_route_keeps_the_global_one() -> (
    None
):
    app = capped_app(UPLOAD_PATH, OTHER_PATH)

    upload_within, _ = await status_for(app, UPLOAD_PATH, 400, 400)
    upload_over, _ = await status_for(app, UPLOAD_PATH, 600, 600)
    other_over, _ = await status_for(app, OTHER_PATH, SMALL_CAP + 1)
    other_within, _ = await status_for(app, OTHER_PATH, SMALL_CAP)

    assert (upload_within, upload_over) == (200, PAYLOAD_TOO_LARGE)
    assert (other_within, other_over) == (200, PAYLOAD_TOO_LARGE)


async def test_a_declared_length_past_the_upload_cap_is_refused_without_reading_the_body() -> None:
    app = capped_app(UPLOAD_PATH)

    refused, pulled = await status_for(app, UPLOAD_PATH, 10, declare=UPLOAD_CAP + 1)

    assert (refused, pulled) == (PAYLOAD_TOO_LARGE, 0)


def test_the_caps_are_the_documented_sizes_and_the_global_one_is_not_raised() -> None:
    assert MAX_BODY_BYTES == 256 * 1024
    assert MAX_CONTEXT_UPLOAD_BYTES == 16 * 1024 * 1024
    assert CONTEXT_UPLOAD_BODY_BYTES == MAX_CONTEXT_UPLOAD_BYTES + 64 * 1024


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        pytest.param("POST", UPLOAD_PATH, True, id="the-route"),
        pytest.param("POST", "/environments/12/stacks/a-b_c1/contexts/-/upload", True, id="ids"),
        pytest.param("GET", UPLOAD_PATH, False, id="wrong-method"),
        pytest.param("POST", "/environments/0/stacks/shop/contexts/-/upload", False, id="zero"),
        pytest.param("POST", "/environments/1/stacks/Shop/contexts/-/upload", False, id="upper"),
        pytest.param("POST", "/environments/1/stacks/shop/contexts/app/upload", False, id="named"),
        pytest.param("POST", f"{UPLOAD_PATH}/more", False, id="longer"),
        pytest.param("POST", f"/x{UPLOAD_PATH}", False, id="prefixed"),
        pytest.param("POST", OTHER_PATH, False, id="other-route"),
    ],
)
def test_only_the_upload_route_of_a_stack_is_a_streamed_context_upload(
    method: str, path: str, expected: bool
) -> None:
    assert is_context_upload(method, path) is expected
    assert is_streamed_upload(method, path) is expected


def test_the_restore_upload_is_still_a_streamed_upload() -> None:
    assert is_streamed_upload("POST", "/restore/archive") is True
    assert is_context_upload("POST", "/restore/archive") is False
