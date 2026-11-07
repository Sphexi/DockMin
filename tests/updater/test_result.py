"""The update result format: the reasons each outcome allows, and the private atomic write."""

import copy
import json
import stat
from pathlib import Path
from typing import Any

import pydantic
import pytest

from app.services.self_update import UpdateResult
from tests.updater.conftest import UPDATE_ID
from tests.updater.fake_engine import OLD_ID
from updater.result import ResultError, parse_result, read_result, write_result

FINISHED_AT = "2026-11-03T10:05:00Z"
MAX_EXCERPT_LINES = 200
MAX_LINE_CHARS = 1024


def result_document(**override: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "update_id": UPDATE_ID,
        "outcome": "succeeded",
        "reason": None,
        "finished_at": FINISHED_AT,
        "new_container_id": OLD_ID,
        "log_excerpt": ["one", "two"],
    }
    document.update(override)
    return document


VALID = [
    pytest.param("succeeded", None, id="succeeded"),
    pytest.param("rolled_back", "unhealthy", id="unhealthy"),
    pytest.param("rolled_back", "health_timeout", id="health-timeout"),
    pytest.param("rolled_back", "exited", id="exited"),
    pytest.param("rolled_back", "create_failed", id="create-failed"),
    pytest.param("rolled_back", "start_failed", id="start-failed"),
    pytest.param("rolled_back", "copy_failed", id="copy-failed"),
    pytest.param("rolled_back", "image_changed", id="rolled-back-image-changed"),
    pytest.param("rolled_back", "engine_error", id="rolled-back-engine-error"),
    pytest.param("rollback_failed", "unhealthy", id="rollback-failed"),
    pytest.param("not_started", "previous_exists", id="previous-exists"),
    pytest.param("not_started", "image_changed", id="not-started-image-changed"),
    pytest.param("not_started", "plan_invalid", id="plan-invalid"),
    pytest.param("not_started", "locked", id="locked"),
    pytest.param("not_started", "engine_error", id="not-started-engine-error"),
]
INVALID = [
    pytest.param({"outcome": "succeeded", "reason": "exited"}, id="succeeded-with-a-reason"),
    pytest.param({"outcome": "not_started", "reason": "exited"}, id="not-started-for-a-failure"),
    pytest.param({"outcome": "rolled_back", "reason": "locked"}, id="rolled-back-for-a-refusal"),
    pytest.param({"outcome": "rolled_back", "reason": None}, id="rolled-back-without-a-reason"),
    pytest.param({"outcome": "rollback_failed", "reason": "plan_invalid"}, id="failed-refusal"),
    pytest.param({"outcome": "finished"}, id="unknown-outcome"),
    pytest.param({"schema_version": 2}, id="schema-version"),
    pytest.param({"update_id": "short"}, id="update-id"),
    pytest.param({"finished_at": "later"}, id="timestamp"),
    pytest.param({"new_container_id": "short"}, id="container-id"),
    pytest.param({"log_excerpt": ["x"] * (MAX_EXCERPT_LINES + 1)}, id="too-many-lines"),
    pytest.param({"log_excerpt": ["x" * (MAX_LINE_CHARS + 1)]}, id="long-line"),
    pytest.param({"log_excerpt": "text"}, id="excerpt-not-a-list"),
]


@pytest.mark.parametrize(("outcome", "reason"), VALID)
def test_every_documented_outcome_and_reason_is_read_by_the_helper_and_the_instance(
    outcome: str, reason: str | None
) -> None:
    document = result_document(outcome=outcome, reason=reason)

    helper = parse_result(document)
    instance = UpdateResult.model_validate(document)

    assert helper.to_dict() == document
    assert (instance.outcome, instance.reason) == (outcome, reason)


@pytest.mark.parametrize("override", INVALID)
def test_a_result_outside_its_schema_is_refused_by_the_helper_and_the_instance(
    override: dict[str, Any],
) -> None:
    document = result_document(**override)

    with pytest.raises(ResultError):
        parse_result(copy.deepcopy(document))
    with pytest.raises(pydantic.ValidationError):
        UpdateResult.model_validate(document)


def test_a_result_is_written_privately_and_atomically(tmp_path: Path) -> None:
    result = parse_result(result_document())
    path = tmp_path / "result.json"

    write_result(path, result)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == result_document()
    assert read_result(path) == result
    assert [entry.name for entry in tmp_path.iterdir()] == ["result.json"]


def test_a_result_that_cannot_be_put_in_place_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.mkdir()

    with pytest.raises(OSError):
        write_result(path, parse_result(result_document()))

    assert [entry.name for entry in tmp_path.iterdir()] == ["result.json"]
