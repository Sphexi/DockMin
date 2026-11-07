"""A reply is refused before it is decoded when its structure would outgrow the heap budget."""

import json

import pytest

from app.docker_engine import EngineClient
from app.docker_engine.engine_http import (
    MAX_DECODE_ESTIMATE_BYTES,
    decode_estimate,
)
from app.docker_engine.errors import ResponseTooLarge
from tests.docker_engine.fake_engine import FakeEngine, Reply, json_reply

NESTED_ENTRIES = 600_000
SMALL_ENTRY_COUNT = 50
SAMPLE = {"Id": "c0ffee", "Names": ["/web"], "Labels": {"a": "b"}}


def empty_nests(count: int) -> bytes:
    """A document of `count` empty arrays: a few bytes each, a great deal of heap once decoded."""
    return b"[" + b",".join([b"[]"] * count) + b"]"


def test_the_estimate_grows_with_each_kind_of_structure() -> None:
    plain = decode_estimate(b"[]")

    assert decode_estimate(b"[[]]") > plain
    assert decode_estimate(b'["a"]') > plain
    assert decode_estimate(b"[1,2]") > plain


def test_the_estimate_is_proportional_to_the_number_of_entries() -> None:
    one = decode_estimate(json.dumps([SAMPLE] * 100).encode())
    two = decode_estimate(json.dumps([SAMPLE] * 200).encode())

    assert 1.9 < two / one < 2.1


def test_a_document_of_many_tiny_containers_is_estimated_far_above_its_byte_length() -> None:
    document = empty_nests(NESTED_ENTRIES)

    assert decode_estimate(document) > 50 * len(document)
    assert decode_estimate(document) > MAX_DECODE_ESTIMATE_BYTES


def test_an_ordinary_listing_is_estimated_far_below_the_budget() -> None:
    document = json.dumps([SAMPLE] * SMALL_ENTRY_COUNT).encode()

    assert decode_estimate(document) < MAX_DECODE_ESTIMATE_BYTES // 1000


async def test_a_list_whose_decoded_size_would_exceed_the_budget_is_refused_though_its_bytes_fit(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    body = empty_nests(NESTED_ENTRIES)
    engine.route("GET", "/containers/json", Reply(200, body, {"Content-Type": "application/json"}))

    with pytest.raises(ResponseTooLarge) as raised:
        await engine_client.containers.list(all=True)

    assert len(body) < 4 * 1024 * 1024
    assert "too large" in raised.value.reason


async def test_a_listing_of_ids_is_held_to_the_same_budget(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route(
        "GET",
        "/containers/json",
        Reply(200, empty_nests(NESTED_ENTRIES), {"Content-Type": "application/json"}),
    )

    with pytest.raises(ResponseTooLarge):
        await engine_client.containers.list_ids()


async def test_a_list_within_the_budget_is_decoded(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/containers/json", json_reply([]))

    assert await engine_client.containers.list(all=True) == []
