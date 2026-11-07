"""The image calls of the Engine client: bounded lists, history, tag, push, removal and streams."""

import threading
from typing import Any

import pytest
from pydantic import SecretStr

from app.docker_engine import EngineClient, EngineError, resources
from app.docker_engine.errors import RequestOutcomeUnknown, ResponseTooLarge
from app.docker_engine.models import PullProgress
from tests.docker_engine.fake_engine import FakeEngine, Reply, Stream, json_reply
from tests.docker_engine.helpers import drain, drain_until_error, json_line

IMAGE_ID = "sha256:" + "a" * 64
DIGEST = "sha256:" + "b" * 64
REGISTRY_AUTH = "c2VjcmV0LXJlZ2lzdHJ5LWF1dGg="
ANONYMOUS_AUTH = "e30="
ENGINE_TEXT = "registry-text-marker-6612"
MIB = 1024 * 1024
LIST_CAP_MIB = 16
HISTORY_ROWS = 512
LONG_LINE = b'{"status":"' + b"x" * (16 * 1024) + b'"}\n'
LAYER = "a1b2c3d4e5f6"

PULL = "/images/create"
PUSH = "/images/registry.example.com/org/app/push"


def summary(**fields: Any) -> dict[str, Any]:
    return {"Id": IMAGE_ID, "Created": 1, "Size": 5, **fields}


@pytest.mark.parametrize(
    "behaviour",
    [
        pytest.param(
            Reply(200, b"[]", {"Content-Length": str((LIST_CAP_MIB + 1) * MIB)}), id="declared"
        ),
        pytest.param(Stream([b"x" * MIB] * (LIST_CAP_MIB + 2)), id="streamed"),
    ],
)
async def test_an_image_list_over_the_cap_raises_and_returns_nothing(
    engine: FakeEngine, engine_client: EngineClient, behaviour: Reply | Stream
) -> None:
    engine.route("GET", "/images/json", behaviour)

    with pytest.raises(ResponseTooLarge):
        await engine_client.images.list()


async def test_an_image_list_drops_placeholder_names_and_reads_no_labels(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    row = summary(
        RepoTags=["<none>:<none>"],
        RepoDigests=["<none>@<none>", f"app@{DIGEST}"],
        Labels={"secret": "label-value"},
    )
    engine.route("GET", "/images/json", json_reply([row]))

    [image] = await engine_client.images.list()

    assert (image.repo_tags, image.repo_digests) == ([], [f"app@{DIGEST}"])
    assert image.labels == {}


async def test_an_image_list_asks_for_digests_and_decodes_off_the_event_loop(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine.route("GET", "/images/json", json_reply([summary()]))
    threads: list[str] = []
    original = type(engine_client.images._http)._decode_models

    def recording(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread().name)
        return original(*args, **kwargs)

    monkeypatch.setattr(type(engine_client.images._http), "_decode_models", recording)

    await engine_client.images.list()

    assert engine.calls("GET", "/images/json")[0].query == {"all": "false", "digests": "true"}
    assert threads
    assert threading.current_thread().name not in threads


async def test_history_keeps_at_most_512_rows(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    rows = [{"Id": f"{n:012x}", "Created": 1, "CreatedBy": "RUN", "Size": 1} for n in range(600)]
    engine.route("GET", f"/images/{IMAGE_ID}/history", json_reply(rows))

    layers = await engine_client.images.history(IMAGE_ID)

    assert len(layers) == HISTORY_ROWS


async def test_history_over_one_mebibyte_raises(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route(
        "GET", f"/images/{IMAGE_ID}/history", Reply(200, b"[]", {"Content-Length": str(2 * MIB)})
    )

    with pytest.raises(ResponseTooLarge):
        await engine_client.images.history(IMAGE_ID)


async def test_tag_posts_the_repository_and_tag_as_query_parameters(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", f"/images/{IMAGE_ID}/tag", Reply(201))

    await engine_client.images.tag(IMAGE_ID, repository="registry.example.com/org/app", tag="2")

    assert engine.calls("POST", f"/images/{IMAGE_ID}/tag")[0].query == {
        "repo": "registry.example.com/org/app",
        "tag": "2",
    }


@pytest.mark.parametrize(
    ("repository", "tag"),
    [
        pytest.param("org/app@" + DIGEST, "2", id="digest-repository"),
        pytest.param("org/app", "-bad", id="tag-starts-with-hyphen"),
        pytest.param("org/app", "t" * 129, id="tag-too-long"),
        pytest.param("org/../app", "2", id="dot-dot"),
    ],
)
async def test_tag_refuses_an_unsafe_name_without_sending_anything(
    engine: FakeEngine, engine_client: EngineClient, repository: str, tag: str
) -> None:
    with pytest.raises(EngineError):
        await engine_client.images.tag(IMAGE_ID, repository=repository, tag=tag)

    assert engine.calls("POST") == []


async def test_a_push_without_credentials_sends_the_empty_auth_document_and_yields_the_digest(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    lines = [
        json_line({"status": "Preparing", "id": LAYER}),
        json_line({"status": "pushed", "aux": {"Tag": "1", "Digest": DIGEST, "Size": 9}}),
    ]
    engine.route("POST", PUSH, Stream(lines))

    events = await drain(engine_client.images.push("registry.example.com/org/app", tag="1"))

    sent = engine.calls("POST", PUSH)[0]
    assert sent.headers["x-registry-auth"] == ANONYMOUS_AUTH
    assert sent.query == {"tag": "1"}
    assert [e.aux.digest if e.aux else None for e in events] == [None, DIGEST]


async def test_a_push_sends_the_registry_credential_as_a_header(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", PUSH, Stream([json_line({"status": "Preparing"})]))

    await drain(
        engine_client.images.push(
            "registry.example.com/org/app", tag="1", registry_auth=SecretStr(REGISTRY_AUTH)
        )
    )

    assert engine.calls("POST", PUSH)[0].headers["x-registry-auth"] == REGISTRY_AUTH


async def test_a_push_aux_object_with_a_malformed_digest_yields_no_digest(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    line = json_line({"aux": {"Tag": "1", "Digest": "sha256:short", "Size": 1}})
    engine.route("POST", PUSH, Stream([line]))

    [event] = await drain(engine_client.images.push("registry.example.com/org/app", tag="1"))

    assert event.aux is not None
    assert event.aux.digest is None


async def test_a_push_error_line_raises_a_fixed_message_without_the_registry_text(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", PUSH, Stream([json_line({"error": ENGINE_TEXT})]))

    _, error = await drain_until_error(
        engine_client.images.push("registry.example.com/org/app", tag="1")
    )

    assert isinstance(error, EngineError)
    assert "image push failed" in error.reason
    assert ENGINE_TEXT not in repr(error) + str(error) + error.reason


@pytest.mark.parametrize(
    ("name", "tag"),
    [
        pytest.param(f"org/app@{DIGEST}", "1", id="digest-name"),
        pytest.param("org/app", "-bad", id="bad-tag"),
    ],
)
async def test_a_push_refuses_an_unsafe_name_without_sending_anything(
    engine: FakeEngine, engine_client: EngineClient, name: str, tag: str
) -> None:
    _, error = await drain_until_error(engine_client.images.push(name, tag=tag))

    assert isinstance(error, EngineError)
    assert engine.calls("POST") == []


async def test_removal_returns_what_the_engine_untagged_and_deleted(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    reply = [{"Untagged": "app:1"}, {"Deleted": IMAGE_ID}]
    engine.route("DELETE", "/images/app:1", json_reply(reply))

    items = await engine_client.images.remove("app:1", force=False, noprune=True)

    sent = engine.calls("DELETE", "/images/app:1")[0]
    assert [(i.untagged, i.deleted) for i in items] == [("app:1", None), (None, IMAGE_ID)]
    assert sent.query == {"force": "false", "noprune": "true"}


@pytest.mark.parametrize(
    "behaviour",
    [
        pytest.param(Reply(200, b"not json"), id="not-json"),
        pytest.param(Reply(200, b'{"Untagged": "app:1"}'), id="not-a-list"),
    ],
)
async def test_a_removal_reply_that_is_not_a_list_of_results_is_an_engine_error(
    engine: FakeEngine, engine_client: EngineClient, behaviour: Reply
) -> None:
    engine.route("DELETE", "/images/app:1", behaviour)

    with pytest.raises(EngineError):
        await engine_client.images.remove("app:1")


async def test_a_removal_reply_over_one_mebibyte_raises(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("DELETE", "/images/app:1", Reply(200, b"[]", {"Content-Length": str(2 * MIB)}))

    with pytest.raises(ResponseTooLarge):
        await engine_client.images.remove("app:1")


async def test_pull_progress_detail_is_clamped_to_non_negative_integers(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    detail = {"current": -5, "total": 10**30}
    other = {"current": "many", "total": True}
    lines = [
        json_line({"status": "Downloading", "id": LAYER, "progressDetail": detail}),
        json_line({"status": "Downloading", "id": LAYER, "progressDetail": other}),
    ]
    engine.route("POST", PULL, Stream(lines))

    first, second = await drain(engine_client.images.pull("alpine"))

    assert (first.progress_detail.current, first.progress_detail.total) == (0, 2**53)
    assert (second.progress_detail.current, second.progress_detail.total) == (0, 0)


@pytest.mark.parametrize(
    ("image", "tag", "expected"),
    [
        pytest.param(
            "ghcr.io/org/app",
            None,
            {"fromImage": "ghcr.io/org/app", "tag": "latest"},
            id="untagged-gets-latest",
        ),
        pytest.param(
            "registry.example.com:5000/app",
            None,
            {"fromImage": "registry.example.com:5000/app", "tag": "latest"},
            id="registry-port-is-not-a-tag",
        ),
        pytest.param("app:1", None, {"fromImage": "app:1"}, id="tag-in-reference"),
        pytest.param(f"app@{DIGEST}", None, {"fromImage": f"app@{DIGEST}"}, id="digest"),
        pytest.param("app", "2", {"fromImage": "app", "tag": "2"}, id="explicit-tag"),
    ],
)
async def test_a_pull_always_names_one_tag_or_digest(
    engine: FakeEngine,
    engine_client: EngineClient,
    image: str,
    tag: str | None,
    expected: dict[str, str],
) -> None:
    engine.route("POST", PULL, Stream([json_line({"status": "Pulling"})]))

    await drain(engine_client.images.pull(image, tag=tag))

    assert engine.calls("POST", PULL)[0].query == expected


async def test_a_pull_error_line_raises_a_fixed_message_without_the_registry_text(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("POST", PULL, Stream([json_line({"errorDetail": {"message": ENGINE_TEXT}})]))

    _, error = await drain_until_error(engine_client.images.pull("alpine"))

    assert isinstance(error, EngineError)
    assert "image pull failed" in error.reason
    assert ENGINE_TEXT not in repr(error) + str(error) + error.reason


@pytest.mark.parametrize(
    "stream",
    [
        pytest.param(Stream([LONG_LINE]), id="line-over-16-kib"),
        pytest.param(Stream([json_line({"status": "Pulling"})[:-6]]), id="cut-mid-line"),
        pytest.param(Stream([b"not json\n"]), id="not-json"),
        pytest.param(Stream([b"[1, 2]\n"]), id="not-an-object"),
    ],
)
async def test_a_pull_stream_that_cannot_be_read_ends_as_an_unknown_outcome(
    engine: FakeEngine, engine_client: EngineClient, stream: Stream
) -> None:
    engine.route("POST", PULL, stream)

    _, error = await drain_until_error(engine_client.images.pull("alpine"))

    assert isinstance(error, RequestOutcomeUnknown)


async def test_a_large_batch_of_progress_lines_is_decoded_off_the_event_loop(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    padding = "p" * 8000
    batch = b"".join(
        json_line({"status": "Downloading", "id": LAYER, "x": padding}) for _ in range(40)
    )
    engine.route("POST", PULL, Stream([batch]))
    threads: list[str] = []
    original = resources._decode_lines

    def recording(lines: Any) -> Any:
        threads.append(threading.current_thread().name)
        return original(lines)

    monkeypatch.setattr(resources, "_decode_lines", recording)

    events: list[PullProgress] = await drain(engine_client.images.pull("alpine"))

    assert len(events) == 40
    assert threading.current_thread().name not in threads


async def test_a_registry_credential_outside_the_base64_alphabets_is_refused_unsent(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    _, error = await drain_until_error(
        engine_client.images.pull("alpine", registry_auth=SecretStr("not base64!\r\nX: y"))
    )

    assert isinstance(error, EngineError)
    assert engine.calls("POST") == []
