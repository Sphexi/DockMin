"""Tests for the shared application fixture: what a test inherits from the one before it."""

import pytest
from fastapi import FastAPI

from app.config import Settings
from app.main import attach_state
from app.web.templating import build_templates
from tests.auth_support import Browsers
from tests.conftest import AttachApp, SharedApp, concurrency_limiter

CHANGED_LIMIT = 8
CHANGED_INTERVAL = 120


def provided() -> None: ...


def replacement() -> None: ...


def test_every_attach_returns_the_one_application_object(
    attach_app: AttachApp, shared_app: SharedApp, settings: Settings
) -> None:
    assert attach_app(settings) is shared_app.app
    assert attach_app(settings) is shared_app.app


def test_every_attach_gives_the_application_the_one_template_environment(
    attach_app: AttachApp, shared_app: SharedApp, settings: Settings
) -> None:
    first = attach_app(settings).state.templates
    second = attach_app(settings).state.templates

    assert first is shared_app.templates
    assert second is shared_app.templates


def test_state_attached_without_an_environment_builds_its_own_and_with_one_uses_it(
    shared_app: SharedApp, settings: Settings
) -> None:
    given = build_templates(development_checks=True)

    attach_state(shared_app.app, settings)
    built = shared_app.app.state.templates
    attach_state(shared_app.app, settings, templates=given)

    assert built is not given and built is not shared_app.templates
    assert shared_app.app.state.templates is given


def test_attaching_again_drops_what_a_test_left_on_the_application(
    attach_app: AttachApp, settings: Settings
) -> None:
    application = attach_app(settings)
    application.state.flash_clock = object()
    application.state.database = object()
    application.dependency_overrides[provided] = replacement
    earlier = {
        name: getattr(application.state, name)
        for name in ("auth", "flash_key", "results", "rate_limiter", "restore_gate", "restore")
    }

    attach_app(settings)

    assert not hasattr(application.state, "flash_clock")
    assert not hasattr(application.state, "database")
    assert application.dependency_overrides == {}
    assert all(getattr(application.state, name) is not earlier[name] for name in earlier)


@pytest.mark.parametrize(
    ("setting", "value"),
    [("development_checks", False), ("max_concurrent_requests", CHANGED_LIMIT)],
)
def test_settings_read_when_the_application_is_built_must_match_the_shared_one(
    attach_app: AttachApp, settings: Settings, setting: str, value: object
) -> None:
    different = settings.model_copy(update={setting: value})

    with pytest.raises(AssertionError, match=rf"this test: .*{setting}={value}"):
        attach_app(different)


def test_a_function_scoped_application_is_refused_while_a_module_holds_the_shared_one(
    attach_app: AttachApp,
    shared_app: SharedApp,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shared_app.claim, "module", True)
    shared_app.app.dependency_overrides[provided] = replacement

    with pytest.raises(AssertionError, match="held by this module's module-scoped fixtures"):
        attach_app(settings)

    assert shared_app.app.dependency_overrides == {provided: replacement}


def test_settings_read_only_after_the_application_is_built_may_differ_from_the_shared_ones(
    attach_app: AttachApp, shared_app: SharedApp, settings: Settings
) -> None:
    different = settings.model_copy(update={"snapshot_interval_seconds": CHANGED_INTERVAL})

    application = attach_app(different)

    assert application is shared_app.app
    assert application.state.settings is different


async def test_the_concurrency_limit_is_in_the_built_stack_and_idle_after_a_request(
    started_app: FastAPI, browsers: Browsers
) -> None:
    client = browsers.open()

    response = await client.get("/login")

    limiter = concurrency_limiter(started_app)
    assert response.status_code == 200
    assert limiter is not None and limiter.active == 0
