"""The image disk-usage call of the Engine client: the type filter, the cap and bad bodies."""

import threading
from typing import Any

import pytest

from app.docker_engine import EngineClient, EngineError, resources
from app.docker_engine.errors import ResponseTooLarge
from tests.docker_engine.fake_engine import FakeEngine, Reply, Stream, json_reply

MIB = 1024 * 1024
CAP_MIB = 16
LAYERS_SIZE = 2_684_354_560
DF = "/system/df"


async def test_image_disk_usage_asks_for_images_only_and_returns_the_layer_bytes(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", DF, json_reply({"LayersSize": LAYERS_SIZE, "Images": [], "Volumes": []}))

    usage = await engine_client.system.image_disk_usage()

    assert usage.layers_size == LAYERS_SIZE
    assert engine.calls("GET", DF)[0].query == {"type": "image"}


async def test_image_disk_usage_decodes_off_the_event_loop(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.route("GET", DF, json_reply({"LayersSize": 1}))
    threads: list[str] = []
    http_type = type(engine_client.system._http)
    original = http_type._decode_model

    def recording(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread().name)
        return original(*args, **kwargs)

    monkeypatch.setattr(http_type, "_decode_model", recording)

    await engine_client.system.image_disk_usage()

    assert threads and threading.current_thread().name not in threads


@pytest.mark.parametrize(
    "behaviour",
    [
        pytest.param(
            Reply(200, b"{}", {"Content-Length": str((CAP_MIB + 1) * MIB)}), id="declared"
        ),
        pytest.param(Stream([b"x" * MIB] * (CAP_MIB + 2)), id="streamed"),
    ],
)
async def test_a_disk_usage_reply_over_the_cap_raises_and_returns_nothing(
    engine: FakeEngine, engine_client: EngineClient, behaviour: Reply | Stream
) -> None:
    engine.route("GET", DF, behaviour)

    with pytest.raises(ResponseTooLarge):
        await engine_client.system.image_disk_usage()


@pytest.mark.parametrize(
    "body",
    [{}, {"LayersSize": None}, {"LayersSize": -1}, {"LayersSize": "large"}, []],
)
async def test_a_disk_usage_reply_without_a_valid_layer_size_raises_an_engine_error(
    engine: FakeEngine, engine_client: EngineClient, body: Any
) -> None:
    engine.route("GET", DF, json_reply(body))

    with pytest.raises(EngineError):
        await engine_client.system.image_disk_usage()


def test_the_disk_usage_cap_is_sixteen_mebibytes() -> None:
    assert resources.DISK_USAGE_BYTES == CAP_MIB * MIB
