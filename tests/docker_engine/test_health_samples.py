"""Documents captured from a daemon report health in the shape the health checker reads."""

import json
from pathlib import Path

import pytest

from app.docker_engine.models import ContainerInspect, ContainerSummary, EngineEvent
from app.subsystems.selectors import LabelsTarget, matches

FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"
FAILING_STREAK = 7
RESTARTED_AT_ZERO = 0


def inspected(name: str) -> ContainerInspect:
    return ContainerInspect.model_validate(json.loads((FIXTURES / name).read_bytes()))


def listed(name: str) -> list[ContainerSummary]:
    return [
        ContainerSummary.model_validate(row) for row in json.loads((FIXTURES / name).read_bytes())
    ]


@pytest.mark.parametrize(
    ("sample", "status", "streak"),
    [
        ("inspect_health_02_healthy.json", "healthy", 0),
        ("inspect_health_03_unhealthy.json", "unhealthy", FAILING_STREAK),
        ("inspect_health_04_after_restart_starting.json", "starting", RESTARTED_AT_ZERO),
        ("inspect_exited_before_restart.json", "unhealthy", None),
    ],
)
def test_an_inspect_document_reports_the_health_status_and_streak(
    sample: str, status: str, streak: int | None
) -> None:
    health = inspected(sample).state.health

    assert health is not None
    assert health.status == status
    assert streak is None or health.failing_streak == streak


def test_a_container_that_exited_keeps_the_health_it_last_reported() -> None:
    state = inspected("inspect_exited_before_restart.json").state

    assert (state.status, state.running) == ("exited", False)


def test_a_container_without_a_healthcheck_reports_no_health() -> None:
    assert inspected("inspect_no_healthcheck.json").state.health is None


def test_the_unhealthy_listing_names_running_containers_and_their_labels() -> None:
    rows = listed("list_unhealthy_running.json")

    assert {row.state for row in rows} == {"running"}
    assert {tuple(sorted(row.labels.items())) for row in rows} == {
        (("app", "api"), ("tier", "front")),
        (("app", "web"), ("tier", "front")),
    }
    assert not any(row.labels_truncated for row in rows)


def test_a_label_selector_picks_the_one_container_the_engine_filter_picked() -> None:
    [only] = listed("list_unhealthy_running_label.json")
    everything = listed("list_unhealthy_running.json")
    selector = LabelsTarget(labels=(("app", "web"),))

    picked = [r for r in everything if matches(selector, name=r.names[0], labels=r.labels)]

    assert [r.id for r in picked] == [only.id]


def test_a_health_event_names_the_container_and_its_labels_in_the_actor_attributes() -> None:
    lines = (FIXTURES / "events_health_filter.ndjson").read_bytes().splitlines()
    events = [EngineEvent.model_validate(json.loads(line)) for line in lines]

    unhealthy = [e for e in events if e.action == "health_status: unhealthy"]

    assert len(events) == 10
    assert len(unhealthy) == 6
    assert all(e.type == "container" and "name" in e.actor.attributes for e in events)
    assert unhealthy[0].actor.attributes["app"] == "web"
