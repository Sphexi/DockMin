"""The container swap against a fake daemon: success, every failure, and the rollback path."""

import stat

import pytest

from tests.updater.conftest import (
    HEALTH_TIMEOUT,
    ORIGINAL_DATABASE,
    ORIGINAL_SHM,
    SECRET_ENV,
    Rig,
    Run,
)
from tests.updater.fake_engine import (
    LOG_LINES,
    LONG_LINE_BYTES,
    NAME,
    NEW_DATABASE_SUFFIX,
    NEW_REFERENCE,
    OLD_ID,
    OTHER_IMAGE_ID,
    Behaviour,
    Failure,
    FakeContainer,
)

MAX_EXCERPT_LINES = 200
MAX_LINE_CHARS = 1024
FIRST_KEPT_LINE = "line 102"
PREVIOUS = f"{NAME}-previous"


def assert_old_container_restored(rig: Rig) -> None:
    assert rig.daemon.names() == [(f"/{NAME}", True)]
    assert list(rig.daemon.containers) == [OLD_ID]
    assert rig.database() == ORIGINAL_DATABASE
    assert (rig.data_dir / "dockmin.db-shm").read_bytes() == ORIGINAL_SHM
    assert not (rig.data_dir / "dockmin.db-wal").exists()
    assert stat.S_IMODE((rig.data_dir / "dockmin.db").stat().st_mode) == 0o640


def steps(run: Run) -> list[str]:
    return [line.split()[0].removeprefix("step=") for line in run.lines]


def test_a_healthy_replacement_ends_with_the_new_container_named_for_the_old_one(rig: Rig) -> None:
    run = rig.run()

    new = rig.daemon.newest()
    [removal] = [c for c in rig.daemon.calls if c.method == "DELETE"]
    assert (run.result.outcome, run.result.reason) == ("succeeded", None)
    assert run.result.new_container_id == new.id
    assert rig.daemon.names() == [(f"/{NAME}", True)]
    assert new.id != OLD_ID
    assert new.body is not None and new.body["Image"] == NEW_REFERENCE
    assert new.body["Env"] == [SECRET_ENV]
    assert rig.daemon.verbs() == ["stop", "rename", "create", "start", "remove"]
    assert removal.path.endswith(OLD_ID) and removal.query == {"force": "0", "v": "0"}
    assert steps(run) == [
        "preflight",
        "stop_old",
        "copy_database",
        "rename_old",
        "create_new",
        "start_new",
        "wait_healthy",
        "remove_previous",
    ]


def test_the_database_is_copied_with_its_mode_before_the_new_container_starts(rig: Rig) -> None:
    rig.run()

    backup = rig.update_dir / "db-before"
    assert rig.database() == ORIGINAL_DATABASE + NEW_DATABASE_SUFFIX
    assert (backup / "dockmin.db").read_bytes() == ORIGINAL_DATABASE
    assert stat.S_IMODE((backup / "dockmin.db").stat().st_mode) == 0o640
    assert (backup / "dockmin.db-shm").read_bytes() == ORIGINAL_SHM
    assert not (backup / "dockmin.db-wal").exists()


@pytest.mark.parametrize(
    ("behaviour", "reason"),
    [
        pytest.param("exit", "exited", id="exits"),
        pytest.param("unhealthy", "unhealthy", id="reports-unhealthy"),
        pytest.param("starting", "health_timeout", id="never-healthy"),
    ],
)
def test_a_replacement_that_does_not_become_healthy_is_rolled_back(
    rig: Rig, behaviour: Behaviour, reason: str
) -> None:
    rig.daemon.behaviour = behaviour

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("rolled_back", reason)
    assert run.result.new_container_id is not None
    assert_old_container_restored(rig)


def test_the_health_wait_lasts_the_planned_time_and_no_longer(rig: Rig) -> None:
    rig.daemon.behaviour = "starting"

    run = rig.run()

    assert run.result.reason == "health_timeout"
    assert HEALTH_TIMEOUT <= run.clock.now < HEALTH_TIMEOUT + 2.0


def test_the_excerpt_keeps_the_last_two_hundred_lines_each_cut_to_a_kibibyte(rig: Rig) -> None:
    rig.daemon.behaviour = "unhealthy"

    excerpt = rig.run().result.log_excerpt

    assert len(excerpt) == MAX_EXCERPT_LINES
    assert excerpt[0] == FIRST_KEPT_LINE
    assert len(excerpt[-2]) == MAX_LINE_CHARS < LONG_LINE_BYTES
    assert excerpt[-1] == "partial"
    assert f"line {LOG_LINES - 1}" in excerpt


def test_the_excerpt_of_a_container_with_a_terminal_is_read_as_raw_text(rig: Rig) -> None:
    rig.daemon.behaviour = "unhealthy"
    rig.daemon.new_tty = True

    excerpt = rig.run().result.log_excerpt

    assert (len(excerpt), excerpt[0], excerpt[-1]) == (MAX_EXCERPT_LINES, "raw 100", "raw 299")


def test_a_create_that_fails_is_rolled_back_with_nothing_to_remove(rig: Rig) -> None:
    rig.daemon.failures["create"] = Failure(500, "no space left")

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("rolled_back", "create_failed")
    assert run.result.new_container_id is None and run.result.log_excerpt == ()
    assert any("no space left" in line for line in run.lines)
    assert "step=find_created status=ok found=no" in run.lines
    assert "remove" not in rig.daemon.verbs()
    assert_old_container_restored(rig)


def test_a_failure_after_the_stop_but_before_the_create_still_restores_the_old_container(
    rig: Rig,
) -> None:
    rig.daemon.failures["rename"] = Failure(500, "rename broke")

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("rolled_back", "engine_error")
    assert_old_container_restored(rig)


def test_a_start_that_fails_removes_the_new_container_and_starts_the_old_one_again(
    rig: Rig,
) -> None:
    rig.daemon.failures["start"] = Failure(500, "boom", target="new")

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("rolled_back", "start_failed")
    assert run.result.new_container_id is not None
    assert_old_container_restored(rig)


def test_a_database_directory_that_cannot_be_made_stops_the_swap_before_the_rename(
    rig: Rig,
) -> None:
    (rig.update_dir / "db-before").mkdir()

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("rolled_back", "copy_failed")
    assert rig.daemon.verbs() == ["stop", "start"]
    assert_old_container_restored(rig)


def test_a_database_file_that_is_not_a_regular_file_is_never_copied(rig: Rig) -> None:
    (rig.data_dir / "dockmin.db-wal").mkdir()

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("rolled_back", "copy_failed")
    assert rig.daemon.verbs() == ["stop", "start"]
    assert rig.database() == ORIGINAL_DATABASE


def test_a_container_named_for_the_previous_version_stops_the_update_before_anything_changes(
    rig: Rig,
) -> None:
    rig.daemon.containers["e" * 64] = FakeContainer("e" * 64, f"/{PREVIOUS}", running=False)

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("not_started", "previous_exists")
    assert rig.daemon.mutations() == []
    assert not (rig.update_dir / "db-before").exists()


def test_an_image_that_changed_since_the_plan_stops_the_update_before_anything_changes(
    rig: Rig,
) -> None:
    rig.daemon.images[NEW_REFERENCE] = OTHER_IMAGE_ID

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("not_started", "image_changed")
    assert rig.daemon.mutations() == []


def test_an_old_container_that_lost_its_planned_name_stops_the_update(rig: Rig) -> None:
    rig.daemon.containers[OLD_ID].name = "/someone-renamed-it"

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("not_started", "engine_error")
    assert rig.daemon.mutations() == []


def test_an_inspect_that_answers_with_another_container_stops_the_update(rig: Rig) -> None:
    rig.daemon.wrong_id_for = OLD_ID

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("not_started", "engine_error")
    assert rig.daemon.mutations() == []


def test_a_container_created_from_another_image_is_never_started_and_is_rolled_back(
    rig: Rig,
) -> None:
    rig.daemon.created_image = OTHER_IMAGE_ID

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("rolled_back", "image_changed")
    assert rig.daemon.verbs() == ["stop", "rename", "create", "stop", "remove", "rename", "start"]
    assert_old_container_restored(rig)


def test_a_new_container_that_cannot_be_removed_leaves_the_old_one_stopped_and_the_database_alone(
    rig: Rig,
) -> None:
    rig.daemon.behaviour = "unhealthy"
    rig.daemon.failures["remove"] = Failure(500, "cannot remove", target="new")

    run = rig.run()

    old = rig.daemon.containers[OLD_ID]
    assert (run.result.outcome, run.result.reason) == ("rollback_failed", "unhealthy")
    assert (old.name, old.running) == (f"/{PREVIOUS}", False)
    assert rig.database() == ORIGINAL_DATABASE + NEW_DATABASE_SUFFIX


def test_an_old_container_that_will_not_start_is_reported_as_a_failed_rollback(rig: Rig) -> None:
    rig.daemon.behaviour = "unhealthy"
    rig.daemon.failures["start"] = Failure(500, "no start", target="old")

    run = rig.run()

    old = rig.daemon.containers[OLD_ID]
    assert (run.result.outcome, run.result.reason) == ("rollback_failed", "unhealthy")
    assert (old.name, old.running) == (f"/{NAME}", False)
    assert rig.database() == ORIGINAL_DATABASE


def test_a_container_with_no_health_check_is_held_to_running_for_the_whole_window(
    rig: Rig,
) -> None:
    rig.daemon.behaviour = "nohealth"

    run = rig.run()

    assert run.result.outcome == "succeeded"
    assert run.clock.now >= HEALTH_TIMEOUT
    assert any("step=wait_healthy" in line and "mode=running_only" in line for line in run.lines)


def test_a_container_with_no_health_check_that_dies_within_the_window_is_rolled_back(
    rig: Rig,
) -> None:
    rig.daemon.behaviour = "nohealth_dies"

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("rolled_back", "exited")
    assert_old_container_restored(rig)


def test_a_create_whose_answer_was_lost_is_found_by_name_and_removed(rig: Rig) -> None:
    rig.daemon.create_mode = "dropped"

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("rolled_back", "create_failed")
    assert run.result.new_container_id is not None
    assert run.result.new_container_id not in rig.daemon.containers
    assert any("step=find_created status=ok found=yes" in line for line in run.lines)
    assert_old_container_restored(rig)


def test_a_container_with_the_new_name_that_is_not_the_planned_one_is_left_alone(rig: Rig) -> None:
    rig.daemon.create_mode = "dropped_foreign"

    run = rig.run()

    assert (run.result.outcome, run.result.reason) == ("rollback_failed", "create_failed")
    assert any("found=unrelated" in line for line in run.lines)
    assert "remove" not in rig.daemon.verbs()
    assert len(rig.daemon.containers) == 2


def test_the_database_backup_directory_is_private(rig: Rig) -> None:
    rig.run()

    assert stat.S_IMODE((rig.update_dir / "db-before").stat().st_mode) == 0o700
