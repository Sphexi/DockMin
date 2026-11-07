"""Progress lines captured from `compose stop` and `compose start`, in both JSON shapes."""

from pathlib import Path

import pytest

from app.compose_cli.parsers import ProgressEvent, parse_event

FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"
CHAIN = "dockmin-e2e-pair"


def events(name: str) -> list[ProgressEvent | None]:
    lines = (FIXTURES / name).read_bytes().splitlines()
    return [parse_event(line) for line in lines]


def test_a_stop_of_named_services_reports_each_container_stopping_then_stopped() -> None:
    found = events("compose_stop_named_services.stderr")

    assert [(e.resource, e.status, e.text) for e in found if e] == [
        (f"Container {CHAIN}-a-1", "Working", "Stopping"),
        (f"Container {CHAIN}-a-1", "Done", "Stopped"),
        (f"Container {CHAIN}-z-1", "Working", "Stopping"),
        (f"Container {CHAIN}-z-1", "Done", "Stopped"),
    ]


def test_a_start_in_the_older_shape_carries_text_and_waits_are_events_too() -> None:
    found = [e for e in events("compose_start_older_shape.stderr") if e]

    assert {e.status for e in found} >= {"Working", "Done"}
    assert all(e.text for e in found)
    assert not any(e.is_error for e in found)


def test_a_start_in_the_newer_shape_has_a_status_and_no_text() -> None:
    found = events("compose_start_newer_shape.stderr")

    assert [(e.status, e.text) for e in found if e] == [
        ("Starting", None),
        ("Started", None),
    ] * 3


def test_a_start_that_fails_ends_with_an_error_event_and_an_error_line_that_is_not_an_event() -> (
    None
):
    found = events("compose_start_failure.stderr")

    assert [e.is_error for e in found if e].count(True) == 1
    assert found[-1] is None
    assert all(e is not None for e in found[:-1])


@pytest.mark.parametrize(
    "line",
    [b'{"error":true,"message":"no such service: nosuch"}', b"plain text", b'{"id":1,"status":2}'],
)
def test_output_that_is_not_a_progress_event_is_not_one(line: bytes) -> None:
    assert parse_event(line) is None
