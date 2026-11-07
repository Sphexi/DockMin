"""Tests for ContainersApi.compose_projects: the Compose project names present on a host."""

import json
import threading
from typing import Any

import pytest

import app.docker_engine.resources as resources
from app.docker_engine import EngineClient
from app.docker_engine.engine_http import EngineHttp
from app.docker_engine.errors import ResponseTooLarge
from app.docker_engine.labels import LABEL_BUDGET_CHARS
from tests.docker_engine.fake_engine import FakeEngine, Reply, json_reply

PROJECT_LABEL = "com.docker.compose.project"
SMALL_CAP = 2048
JSON_HEADERS = {"Content-Type": "application/json"}
HUGE_LABEL = "x" * (LABEL_BUDGET_CHARS + 1)


def row(number: int, labels: dict[str, Any] | None) -> dict[str, Any]:
    return {"Id": f"{number:064x}", "Labels": labels}


def padded(rows: list[dict[str, Any]], size: int) -> bytes:
    return json.dumps(rows).encode("utf-8").ljust(size)


async def test_it_returns_the_case_folded_project_names_and_ignores_other_containers(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route(
        "GET",
        "/containers/json",
        json_reply(
            [
                row(1, {PROJECT_LABEL: "Shop"}),
                row(2, {PROJECT_LABEL: "shop", "app": "web"}),
                row(3, {PROJECT_LABEL: "BLOG"}),
                row(4, {"app": "web"}),
                row(5, {}),
                row(6, None),
                row(7, {PROJECT_LABEL: ""}),
                row(8, {PROJECT_LABEL: None}),
                row(9, {"com.docker.compose.project.extra": "decoy"}),
            ]
        ),
    )

    projects = await engine_client.containers.compose_projects()

    assert projects == frozenset({"shop", "blog"})


async def test_it_asks_for_stopped_containers_filtered_on_the_label_key_alone(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/containers/json", json_reply([]))

    projects = await engine_client.containers.compose_projects()

    query = engine.calls("GET", "/containers/json")[0].query
    assert projects == frozenset()
    assert query["all"] == "true"
    assert json.loads(query["filters"]) == {"label": [PROJECT_LABEL]}


async def test_the_project_label_survives_an_oversized_label_on_the_same_container(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    labels = {"bulk": HUGE_LABEL, PROJECT_LABEL: "Shop"}
    engine.route("GET", "/containers/json", json_reply([row(1, labels)]))

    projects = await engine_client.containers.compose_projects()

    assert projects == frozenset({"shop"})


async def test_a_reply_of_exactly_the_cap_is_read_and_one_byte_more_is_refused(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "CONTAINER_IDS_BYTES", SMALL_CAP)
    rows = [row(1, {PROJECT_LABEL: "shop"})]
    engine.route("GET", "/containers/json", Reply(200, padded(rows, SMALL_CAP), JSON_HEADERS))
    at_the_cap = await engine_client.containers.compose_projects()
    engine.route("GET", "/containers/json", Reply(200, padded(rows, SMALL_CAP + 1), JSON_HEADERS))

    with pytest.raises(ResponseTooLarge):
        await engine_client.containers.compose_projects()

    assert at_the_cap == frozenset({"shop"})


def test_the_cap_is_four_mebibytes() -> None:
    assert resources.CONTAINER_IDS_BYTES == 4 * 1024 * 1024


async def test_the_reply_is_decoded_in_a_worker_thread(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.route("GET", "/containers/json", json_reply([row(1, {PROJECT_LABEL: "shop"})]))
    threads: list[int] = []
    decode = EngineHttp._decode_models

    def recording(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        return decode(*args, **kwargs)

    monkeypatch.setattr(EngineHttp, "_decode_models", recording)

    await engine_client.containers.compose_projects()

    assert threads != [] and threading.get_ident() not in threads
