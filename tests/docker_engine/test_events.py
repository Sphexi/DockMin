"""Tests for the events watcher and its backoff policy in app/docker_engine/streams.py."""

import json
import random

import pytest

from app.docker_engine import EngineClient, EngineError, PermissionDenied
from app.docker_engine.engine_http import EngineHttp
from app.docker_engine.streams import BackoffPolicy, watch_events
from tests.docker_engine.fake_engine import Ending, FakeEngine, Refuse, Stream, error_reply
from tests.docker_engine.helpers import (
    EVENT_BASE_SECONDS,
    NEAR_ZERO_BACKOFF,
    drain,
    event_payload,
    json_line,
    take,
)

EXACT_BACKOFF = BackoffPolicy(initial=1.0, factor=2.0, maximum=30.0, jitter=0.0)
MAXIMUM_DELAY = 30.0
MAXIMUM_DELAY_WITH_JITTER = MAXIMUM_DELAY * 1.25
FLOAT_OVERFLOW_ATTEMPT = 1024
ABSURD_ATTEMPT = 10**9
INT64_MAX = 2**63 - 1
FOUR_HUNDRED_DIGITS = 10**399


class RecordingSleep:
    """A sleep replacement that records requested delays and returns at once."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _line(number: int) -> bytes:
    return json_line(event_payload(number))


async def test_events_reconnect_with_backoff_resume_from_the_last_event_and_drop_duplicates(
    engine: FakeEngine, engine_http: EngineHttp
) -> None:
    engine.route(
        "GET",
        "/events",
        Stream([_line(1), _line(2)], Ending.CUT),
        Stream([_line(2), _line(3)], Ending.CUT),
        Refuse(),
        Stream([_line(4)], Ending.HOLD),
    )
    sleep = RecordingSleep()
    events = watch_events(engine_http, backoff=EXACT_BACKOFF, sleep=sleep)

    received = await take(events, 4)
    await events.aclose()

    assert [e.actor.id for e in received] == ["e1", "e2", "e3", "e4"]
    assert sleep.delays == [1.0, 1.0, 2.0]
    assert [r.query.get("since") for r in engine.calls("GET", "/events")] == [
        None,
        str(EVENT_BASE_SECONDS + 2),
        str(EVENT_BASE_SECONDS + 3),
        str(EVENT_BASE_SECONDS + 3),
    ]
    assert len(engine.calls(path="/_ping")) == 4


async def test_events_start_from_the_requested_time_with_the_requested_filters(
    engine: FakeEngine, engine_http: EngineHttp
) -> None:
    engine.route("GET", "/events", Stream([_line(1)], Ending.HOLD))
    filters = {"type": ["container"], "event": ["die"]}
    events = watch_events(engine_http, since=EVENT_BASE_SECONDS, filters=filters)

    await take(events, 1)
    await events.aclose()

    sent = engine.calls("GET", "/events")[0].query
    assert sent == {"since": str(EVENT_BASE_SECONDS), "filters": json.dumps(filters)}


async def test_events_surface_an_error_that_is_not_a_lost_connection_without_reconnecting(
    engine: FakeEngine, engine_http: EngineHttp
) -> None:
    engine.route("GET", "/events", error_reply(403))
    sleep = RecordingSleep()
    events = watch_events(engine_http, sleep=sleep)

    with pytest.raises(PermissionDenied):
        await anext(events)

    assert sleep.delays == []


async def test_events_surface_a_payload_that_is_not_an_event_without_reconnecting(
    engine: FakeEngine, engine_http: EngineHttp
) -> None:
    engine.route("GET", "/events", Stream([json_line({"unexpected": True})]))
    sleep = RecordingSleep()
    events = watch_events(engine_http, sleep=sleep)

    with pytest.raises(EngineError):
        await anext(events)

    assert sleep.delays == []


async def test_events_end_without_connecting_once_the_client_is_closed(
    engine: FakeEngine, engine_http: EngineHttp
) -> None:
    await engine_http.aclose()

    received = await drain(watch_events(engine_http))

    assert received == []
    assert engine.calls("GET", "/events") == []


async def test_client_events_use_the_backoff_policy_given_to_them(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", "/events", Stream([_line(1)], Ending.HOLD))
    events = engine_client.system.events(backoff=NEAR_ZERO_BACKOFF)

    first = await anext(events)
    await events.aclose()

    assert first.actor.id == "e1"


@pytest.mark.parametrize(
    ("attempt", "delay"),
    [(0, 1.0), (1, 2.0), (2, 4.0), (4, 16.0), (5, MAXIMUM_DELAY), (40, MAXIMUM_DELAY)],
)
def test_backoff_doubles_each_attempt_up_to_the_maximum(attempt: int, delay: float) -> None:
    assert EXACT_BACKOFF.delay(attempt) == delay


@pytest.mark.parametrize(("pick", "scale"), [("low", 0.75), ("high", 1.25)])
def test_backoff_jitter_stays_within_its_proportion_of_the_delay(
    monkeypatch: pytest.MonkeyPatch, pick: str, scale: float
) -> None:
    monkeypatch.setattr(
        random, "uniform", {"low": lambda lo, hi: lo, "high": lambda lo, hi: hi}[pick]
    )
    policy = BackoffPolicy(initial=2.0, jitter=0.25)

    assert policy.delay(0) == 2.0 * scale


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        pytest.param(EXACT_BACKOFF, MAXIMUM_DELAY, id="doubling"),
        pytest.param(
            BackoffPolicy(initial=1.0, factor=1.5, maximum=MAXIMUM_DELAY, jitter=0.0),
            MAXIMUM_DELAY,
            id="fractional-factor",
        ),
        pytest.param(
            BackoffPolicy(initial=2.0, factor=1.0, maximum=MAXIMUM_DELAY, jitter=0.0),
            2.0,
            id="constant",
        ),
    ],
)
@pytest.mark.parametrize("attempt", [FLOAT_OVERFLOW_ATTEMPT, ABSURD_ATTEMPT])
def test_backoff_is_capped_at_every_attempt_however_large(
    policy: BackoffPolicy, expected: float, attempt: int
) -> None:
    assert policy.delay(attempt) == expected


def test_backoff_with_jitter_stays_within_the_maximum_plus_its_jitter_at_an_absurd_attempt() -> (
    None
):
    assert BackoffPolicy().delay(ABSURD_ATTEMPT) <= MAXIMUM_DELAY_WITH_JITTER


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        pytest.param({"initial": 0.0}, "initial", id="zero-initial"),
        pytest.param({"initial": -1.0}, "initial", id="negative-initial"),
        pytest.param({"factor": 0.5}, "factor", id="shrinking-factor"),
        pytest.param({"initial": 2.0, "maximum": 1.0}, "maximum", id="maximum-below-initial"),
        pytest.param({"jitter": -0.1}, "jitter", id="negative-jitter"),
        pytest.param({"jitter": 1.0}, "jitter", id="jitter-of-one"),
    ],
)
def test_an_invalid_backoff_policy_is_rejected(kwargs: dict[str, float], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        BackoffPolicy(**kwargs)


@pytest.mark.parametrize(
    "poison",
    [
        pytest.param(INT64_MAX, id="int64-maximum"),
        pytest.param(FOUR_HUNDRED_DIGITS, id="four-hundred-digits"),
    ],
)
async def test_an_event_with_an_implausible_nanosecond_stamp_does_not_suppress_later_events(
    engine: FakeEngine, engine_http: EngineHttp, poison: int
) -> None:
    poisoned = {**event_payload(1), "timeNano": poison}
    engine.route("GET", "/events", Stream([json_line(poisoned), _line(2), _line(3)], Ending.HOLD))
    events = watch_events(engine_http)

    received = await take(events, 3)
    await events.aclose()

    assert [e.actor.id for e in received] == ["e1", "e2", "e3"]


async def test_reconnecting_after_an_implausible_stamp_resumes_from_the_last_plausible_event(
    engine: FakeEngine, engine_http: EngineHttp
) -> None:
    poisoned = {**event_payload(2), "time": INT64_MAX, "timeNano": INT64_MAX}
    engine.route(
        "GET",
        "/events",
        Stream([_line(1), json_line(poisoned)], Ending.CUT),
        Stream([_line(3)], Ending.HOLD),
    )
    events = watch_events(engine_http, backoff=NEAR_ZERO_BACKOFF, sleep=RecordingSleep())

    received = await take(events, 3)
    await events.aclose()

    assert [e.actor.id for e in received] == ["e1", "e2", "e3"]
    assert [r.query.get("since") for r in engine.calls("GET", "/events")] == [
        None,
        str(EVENT_BASE_SECONDS + 1),
    ]
