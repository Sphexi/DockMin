"""The update plan format: the documented schema, validation, and bounded reading."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.services.self_update import DATABASE_FILES, UpdatePlan
from tests.updater.conftest import UPDATE_ID, plan_document
from tests.updater.fake_engine import API_VERSION, NAME, NEW_IMAGE_ID, NEW_REFERENCE, OLD_ID
from updater import plan as plan_module
from updater.plan import PlanError, parse_plan, read_plan

MISSING = object()
SMALL_LIMIT = 64


def with_value(path: tuple[str, ...], value: object) -> dict[str, Any]:
    """Return a valid plan document with the value at the path replaced or, if MISSING, removed."""
    document = copy.deepcopy(plan_document())
    holder = document
    for key in path[:-1]:
        holder = holder[key]
    if value is MISSING:
        del holder[path[-1]]
    else:
        holder[path[-1]] = value
    return document


def test_a_valid_plan_is_read_into_its_fields_and_writes_back_unchanged() -> None:
    document = plan_document()

    plan = parse_plan(document)

    assert plan.update_id == UPDATE_ID
    assert (plan.old.id, plan.old.name, plan.new.name) == (OLD_ID, NAME, NAME)
    assert (plan.new.image, plan.new.image_id) == (NEW_REFERENCE, NEW_IMAGE_ID)
    assert plan.database_files == ("dockmin.db", "dockmin.db-wal", "dockmin.db-shm")
    assert plan.to_dict() == document


def test_keys_the_helper_does_not_know_are_ignored() -> None:
    document = plan_document(future_field={"anything": 1})

    assert parse_plan(document).update_id == UPDATE_ID


@pytest.mark.parametrize(
    "path, value",
    [
        pytest.param(("schema_version",), 2, id="schema-version"),
        pytest.param(("schema_version",), MISSING, id="no-schema-version"),
        pytest.param(("update_id",), UPDATE_ID.upper(), id="upper-case-update-id"),
        pytest.param(("update_id",), UPDATE_ID[:-1], id="short-update-id"),
        pytest.param(("created_at",), "yesterday", id="timestamp"),
        pytest.param(("requested_by_user_id",), True, id="boolean-user"),
        pytest.param(("request_event_id",), 0, id="zero-event"),
        pytest.param(("engine_api_version",), f"v{API_VERSION}", id="api-version"),
        pytest.param(("old",), MISSING, id="no-old-container"),
        pytest.param(("old", "id"), "abc", id="short-container-id"),
        pytest.param(("old", "stop_timeout"), -1, id="negative-stop-timeout"),
        pytest.param(("old", "stop_timeout"), 3601, id="long-stop-timeout"),
        pytest.param(("new", "name"), "other", id="renamed-replacement"),
        pytest.param(("new", "image_id"), "sha256:zz", id="image-id"),
        pytest.param(("new", "create_body", "Image"), "elsewhere:1", id="body-image"),
        pytest.param(("new", "create_body"), "text", id="body-not-an-object"),
        pytest.param(("health_timeout_seconds",), 0, id="no-health-time"),
        pytest.param(("health_timeout_seconds",), 3601, id="long-health-time"),
        pytest.param(("database_files",), [], id="no-database-files"),
        pytest.param(("database_files",), ["../outside"], id="traversal"),
        pytest.param(("database_files",), ["a.db", "a.db"], id="duplicate-file"),
        pytest.param(("database_files",), ["f"] * 9, id="too-many-files"),
    ],
)
def test_a_plan_outside_its_schema_is_refused(path: tuple[str, ...], value: object) -> None:
    with pytest.raises(PlanError):
        parse_plan(with_value(path, value))


def test_the_plan_the_instance_writes_is_accepted_by_the_helper() -> None:
    written = UpdatePlan(
        update_id=UPDATE_ID,
        created_at="2026-11-03T10:00:00Z",
        requested_by_user_id=7,
        request_event_id=42,
        engine_api_version=API_VERSION,
        old_id=OLD_ID,
        old_name=NAME,
        stop_timeout=5,
        old_image="ghcr.io/example/dockmin:0.0.1",
        new_image=NEW_REFERENCE,
        new_image_id=NEW_IMAGE_ID,
        create_body={"Image": NEW_REFERENCE},
        health_timeout_seconds=180,
    )

    read = parse_plan(json.loads(json.dumps(written.to_dict())))

    assert read.to_dict() == written.to_dict()
    assert read.database_files == DATABASE_FILES


def test_a_plan_file_that_is_a_link_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text(json.dumps(plan_document()), encoding="utf-8")
    link = tmp_path / "plan.json"
    link.symlink_to(target)

    with pytest.raises(OSError):
        read_plan(link)


def test_a_plan_file_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "plan.json"
    directory.mkdir()

    with pytest.raises(OSError):
        read_plan(directory)


def test_a_plan_file_over_the_size_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_module, "MAX_PLAN_BYTES", SMALL_LIMIT)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan_document()), encoding="utf-8")

    with pytest.raises(ValueError, match="exceeds"):
        read_plan(path)


@pytest.mark.parametrize("text", ["[]", "7", "not json"])
def test_a_plan_file_that_does_not_hold_a_json_object_is_refused(tmp_path: Path, text: str) -> None:
    path = tmp_path / "plan.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match=r"JSON|Expecting"):
        read_plan(path)
