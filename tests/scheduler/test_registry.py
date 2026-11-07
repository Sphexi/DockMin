"""The handler registry: which subsystems have code in this process."""

import pytest

from app.scheduler.registry import HandlerRegistry
from tests.scheduler.support import REFRESHER, RESTARTER, ScriptedHandler


def test_a_registered_handler_is_found_by_its_subsystem_name() -> None:
    registry = HandlerRegistry()
    handler = ScriptedHandler(RESTARTER)

    registry.register(handler)

    assert registry.get(RESTARTER) is handler
    assert registry.names() == frozenset({RESTARTER})


def test_a_subsystem_with_no_handler_is_absent() -> None:
    assert HandlerRegistry().get(REFRESHER) is None


def test_a_name_outside_the_subsystem_registry_is_refused() -> None:
    with pytest.raises(ValueError, match="not a registered subsystem"):
        HandlerRegistry().register(ScriptedHandler("log-shipper"))


def test_a_second_handler_for_one_subsystem_is_refused() -> None:
    registry = HandlerRegistry()
    registry.register(ScriptedHandler(RESTARTER))

    with pytest.raises(ValueError, match="already has a handler"):
        registry.register(ScriptedHandler(RESTARTER))


@pytest.mark.parametrize("register", ["register", "replace"])
def test_the_pruner_owns_no_job_so_no_handler_may_be_installed_for_it(register: str) -> None:
    registry = HandlerRegistry()

    with pytest.raises(ValueError, match="owns no jobs"):
        getattr(registry, register)(ScriptedHandler("activity-log-pruner"))

    assert registry.get("activity-log-pruner") is None
