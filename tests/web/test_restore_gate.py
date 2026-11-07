"""The offline gate: which requests each phase admits, what is refused, and the drain."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute

import app.main as main_module
from app.main import stop_runtime
from app.services.restore_gate import RestoreGate, RestoreGateMiddleware, admits
from tests.auth_support import ADMIN_PASSWORD, ADMIN_USERNAME, Browsers, csrf_form

NOW = datetime(2026, 1, 1, tzinfo=UTC)
RESTORE_ID = "r" * 32
SERVICE_UNAVAILABLE = 503
RETRY_ENGAGED = "30"
RETRY_SWAPPING = "5"
SHORT_DRAIN = 0.3
TOKEN = "C" * 43
PHASES = ("open", "engaged", "swapping", "failed")

ADMISSION = [
    ("open", "GET", "/x", True),
    ("open", "POST", "/x", True),
    ("open", "DELETE", "/api/x", True),
    ("engaged", "GET", "/x", True),
    ("engaged", "HEAD", "/x", True),
    ("engaged", "OPTIONS", "/x", True),
    ("engaged", "POST", "/x", False),
    ("engaged", "PUT", "/api/x", False),
    ("engaged", "DELETE", "/x", False),
    ("engaged", "PATCH", "/x", False),
    ("engaged", "POST", "/login", True),
    ("engaged", "POST", "/login/continue", True),
    ("engaged", "POST", "/logout", True),
    ("engaged", "POST", "/login/x", False),
    ("engaged", "POST", "/loginx", False),
    ("engaged", "POST", "/restore", True),
    ("engaged", "POST", "/restore/begin", True),
    ("engaged", "POST", "/restore/archive", True),
    ("engaged", "POST", "/restorex", False),
    ("swapping", "GET", "/x", False),
    ("swapping", "GET", "/api/health", True),
    ("swapping", "GET", "/api/ready", False),
    ("swapping", "GET", "/static/css/a.css", True),
    ("swapping", "GET", "/restore/progress/abc", True),
    ("swapping", "POST", "/restore/progress/abc", False),
    ("swapping", "GET", "/restore", False),
    ("swapping", "POST", "/login", False),
    ("swapping", "POST", "/login/continue", False),
    ("swapping", "GET", "/staticx", False),
    ("failed", "GET", "/api/health", False),
    ("failed", "GET", "/x", False),
    ("failed", "POST", "/login", False),
    ("failed", "GET", "/static/a", False),
]


@pytest.mark.parametrize(("phase", "method", "path", "admitted"), ADMISSION)
def test_each_phase_admits_exactly_the_requests_it_is_defined_to(
    phase: str, method: str, path: str, admitted: bool
) -> None:
    assert admits(phase, method, path) is admitted  # type: ignore[arg-type]


@pytest.mark.parametrize("phase", PHASES)
def test_a_websocket_is_admitted_only_while_the_gate_is_open(phase: str) -> None:
    assert admits(phase, "GET", "/ws", websocket=True) is (phase == "open")  # type: ignore[arg-type]


class Probe:
    """A bare application behind the gate middleware, with one request that can be held."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.gate = RestoreGate()

        async def slow(request: Request) -> PlainTextResponse:
            self.started.set()
            await self.release.wait()
            return PlainTextResponse("done")

        async def quick(request: Request) -> PlainTextResponse:
            return PlainTextResponse("quick")

        async def socket(websocket: object) -> None:
            await websocket.accept()  # type: ignore[attr-defined]
            await websocket.send_text("hi")  # type: ignore[attr-defined]
            await websocket.close()  # type: ignore[attr-defined]

        self.app = Starlette(
            routes=[
                Route("/slow", slow),
                Route("/quick", quick, methods=["GET", "POST"]),
                Route("/api/health", quick),
                Route("/restore/progress/{x}", quick),
                WebSocketRoute("/ws", socket),
            ]
        )
        self.app.add_middleware(RestoreGateMiddleware, gate=self.gate)

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
        return httpx.AsyncClient(transport=transport, base_url="http://t")


@pytest.fixture
def probe() -> Probe:
    return Probe()


async def test_an_admitted_request_is_counted_while_it_runs_and_a_refused_one_is_not(
    probe: Probe,
) -> None:
    async with probe.client() as client:
        task = asyncio.create_task(client.get("/slow"))
        await probe.started.wait()
        counted = probe.gate.inflight
        probe.gate.engage(RESTORE_ID, NOW, purpose="restore")
        probe.gate.begin_swap()

        refused = await client.get("/quick")
        always = [await client.get("/api/health"), await client.get("/restore/progress/1")]
        probe.release.set()
        finished = await task

    assert counted == 1
    assert (refused.status_code, refused.headers["retry-after"]) == (
        SERVICE_UNAVAILABLE,
        RETRY_SWAPPING,
    )
    assert [response.status_code for response in always] == [200, 200]
    assert (finished.text, probe.gate.inflight) == ("done", 0)


async def test_the_drain_times_out_while_a_request_runs_and_returns_when_none_does(
    probe: Probe,
) -> None:
    probe.gate.enter()

    waited = await probe.gate.drain(SHORT_DRAIN)
    probe.gate.leave()

    assert waited is False
    assert await probe.gate.drain(SHORT_DRAIN) is True


async def test_the_drain_wakes_when_the_last_admitted_request_leaves_and_never_counts_below_zero(
    probe: Probe,
) -> None:
    probe.gate.enter()
    probe.gate.enter()
    waiter = asyncio.create_task(probe.gate.drain(5))

    probe.gate.leave()
    await asyncio.sleep(0)
    still_waiting = not waiter.done()
    probe.gate.leave()
    drained = await asyncio.wait_for(waiter, 2)
    probe.gate.leave()

    assert (still_waiting, drained, probe.gate.inflight) == (True, True, 0)


async def test_a_gate_that_failed_stays_failed_when_reopened_and_refuses_liveness(
    probe: Probe,
) -> None:
    probe.gate.mark_failed(("/a", "/b"))
    probe.gate.reopen()

    async with probe.client() as client:
        response = await client.get("/api/health")

    assert (probe.gate.phase, probe.gate.failed_paths) == ("failed", ("/a", "/b"))
    assert response.status_code == SERVICE_UNAVAILABLE


async def test_a_reopened_gate_forgets_the_restore_and_admits_again(probe: Probe) -> None:
    probe.gate.engage(RESTORE_ID, NOW, purpose="restore")
    probe.gate.reopen()

    async with probe.client() as client:
        response = await client.post("/quick")

    assert (probe.gate.phase, probe.gate.restore_id) == ("open", None)
    assert response.status_code == 200


class Handshake:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def receive(self) -> dict[str, str]:
        return {"type": "websocket.connect"}

    async def send(self, message: dict[str, object]) -> None:
        self.sent.append(message)


def websocket_scope(app: Starlette) -> dict[str, object]:
    return {
        "type": "websocket",
        "path": "/ws",
        "raw_path": b"/ws",
        "headers": [],
        "query_string": b"",
        "scheme": "ws",
        "server": ("t", 80),
        "client": ("c", 1),
        "subprotocols": [],
        "root_path": "",
        "app": app,
    }


async def test_a_websocket_handshake_is_accepted_when_open_and_closed_with_1013_when_engaged(
    probe: Probe,
) -> None:
    scope = websocket_scope(probe.app)
    open_gate, engaged = Handshake(), Handshake()

    await probe.app(scope, open_gate.receive, open_gate.send)  # type: ignore[arg-type]
    probe.gate.engage(RESTORE_ID, NOW, purpose="restore")
    await probe.app(scope, engaged.receive, engaged.send)  # type: ignore[arg-type]

    assert open_gate.sent[0]["type"] == "websocket.accept"
    assert engaged.sent == [{"type": "websocket.close", "code": 1013}]


class Closer:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def aclose(self) -> None:
        self.order.append("database")


async def test_stopping_the_runtime_waits_for_admitted_requests_only_during_a_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "DRAIN_SECONDS", SHORT_DRAIN)
    gate = RestoreGate()
    order: list[str] = []
    state = SimpleNamespace(database=Closer(order), restore_gate=gate)
    gate.enter()
    loop = asyncio.get_running_loop()

    await stop_runtime(SimpleNamespace(state=state))  # type: ignore[arg-type]
    outside = list(order)
    order.clear()
    state.database = Closer(order)
    gate.engage(RESTORE_ID, NOW, purpose="restore")
    gate.begin_swap()
    started = loop.time()
    with pytest.raises(main_module.RequestsNotDrained):
        await stop_runtime(SimpleNamespace(state=state))  # type: ignore[arg-type]
    waited = loop.time() - started

    assert outside == ["database"]
    assert SHORT_DRAIN * 0.8 < waited < SHORT_DRAIN * 5
    assert order == ["database"] and not hasattr(state, "database")


@pytest.fixture
def engaged(started_app: FastAPI) -> FastAPI:
    started_app.state.restore_gate.engage(RESTORE_ID, NOW, purpose="restore")
    return started_app


async def test_an_engaged_gate_refuses_a_change_in_the_shape_the_caller_asked_for(
    engaged: FastAPI, browsers: Browsers
) -> None:
    client = browsers.open()
    api = await client.post("/api/anything")
    htmx = await client.post("/groups", headers={"HX-Request": "true", "X-CSRF-Token": TOKEN})
    page = await client.post("/groups", data={"csrf_token": TOKEN})

    assert api.status_code == SERVICE_UNAVAILABLE
    assert api.json()["error"]["status"] == SERVICE_UNAVAILABLE
    assert "offline for a restore" in api.json()["error"]["message"]
    assert api.headers["retry-after"] == RETRY_ENGAGED
    assert htmx.status_code == SERVICE_UNAVAILABLE and "<html" not in htmx.text
    assert htmx.headers["cache-control"] == "no-store"
    assert page.status_code == SERVICE_UNAVAILABLE and 'href="/restore"' in page.text
    assert "Offline for a restore" in page.text


async def test_an_engaged_gate_still_serves_reads_and_the_sign_in_form(
    engaged: FastAPI, browsers: Browsers
) -> None:
    client = browsers.open()

    health = await client.get("/api/health")
    login = await client.get("/login")
    attempt = await client.post(
        "/login", data=csrf_form(client, username=ADMIN_USERNAME, password=ADMIN_PASSWORD)
    )

    assert (health.status_code, login.status_code) == (200, 200)
    assert attempt.status_code == 303


async def test_a_failed_gate_names_the_paths_on_every_page_and_in_the_json_shape(
    started_app: FastAPI, browsers: Browsers
) -> None:
    started_app.state.restore_gate.mark_failed(
        ("/data/restore/x/previous", "/data/restore/x/staging")
    )
    client = browsers.open()

    page = await client.get("/")
    api = await client.get("/api/ready")

    assert page.status_code == SERVICE_UNAVAILABLE
    assert "/data/restore/x/previous" in page.text and "restart the container" in page.text
    assert api.status_code == SERVICE_UNAVAILABLE
    assert api.json()["error"]["status"] == SERVICE_UNAVAILABLE


async def test_a_request_with_a_session_cookie_is_answered_while_the_database_is_absent(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    started_app.state.auth.setup.complete = False
    database = started_app.state.database
    del started_app.state.database

    response = await admin_client.get("/api/health")
    started_app.state.database = database

    assert response.status_code == 200


async def test_a_request_with_a_session_cookie_is_answered_while_the_database_is_closed(
    started_app: FastAPI, admin_client: AsyncClient
) -> None:
    started_app.state.auth.setup.complete = False
    started_app.state.restore_gate.engage(RESTORE_ID, NOW, purpose="restore")
    started_app.state.restore_gate.begin_swap()
    await started_app.state.database.aclose()

    health = await admin_client.get("/api/health")
    progress = await admin_client.get(f"/restore/progress/{'0' * 32}")

    assert health.status_code == 200
    assert progress.status_code == 404
