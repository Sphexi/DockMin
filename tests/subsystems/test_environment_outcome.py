"""How a run ends when its environment cannot be used."""

import pytest

from app.scheduler.model import RunFailed, RunSkipped
from app.services.object_inspector import EnvironmentUnavailable, SwarmUnavailable, UnavailableKind
from app.subsystems.base import environment_outcome

FAILING = {
    "engine": "engine_error",
    "credentials": "environment_credentials",
    "unsupported": "environment_unsupported",
}


@pytest.mark.parametrize("kind", ["unreachable", "outcome_unknown", "not_swarm_manager"])
def test_a_host_that_cannot_be_reached_skips_the_run_and_names_the_kind(
    kind: UnavailableKind,
) -> None:
    ended = environment_outcome(EnvironmentUnavailable(kind))

    assert isinstance(ended, RunSkipped)
    assert ended.reason == "environment_unreachable"
    assert ended.detail == {"kind": kind}


@pytest.mark.parametrize(("kind", "reason"), list(FAILING.items()), ids=list(FAILING))
def test_a_host_only_a_person_can_fix_fails_the_run(kind: UnavailableKind, reason: str) -> None:
    ended = environment_outcome(EnvironmentUnavailable(kind))

    assert isinstance(ended, RunFailed)
    assert ended.reason == reason


def test_a_swarm_node_that_is_not_a_manager_skips_the_run() -> None:
    ended = environment_outcome(SwarmUnavailable())

    assert isinstance(ended, RunSkipped)
    assert ended.detail == {"kind": "not_swarm_manager"}
