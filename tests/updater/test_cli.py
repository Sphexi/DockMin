"""The helper's command line: argument checks, the lock, signals, and the result it leaves."""

import json
import signal
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.updater.conftest import SECRET_ENV, UPDATE_ID, Rig, plan_document
from tests.updater.fake_engine import OLD_ID, Failure, Hook, Target, Verb
from updater import cli
from updater.files import try_exclusive_lock
from updater.plan import UPDATE_SUBDIRECTORY
from updater.result import read_result
from updater.sequence import Updater

OTHER_UPDATE_ID = "f" * 32
USAGE_EXIT, FAILURE_EXIT = 2, 1
HELPER_DIRECTORY = Path(__file__).resolve().parents[2] / "updater"
SUBPROCESS_TIMEOUT_SECONDS = 60
HEALTHY_AT_ONCE = 0


def run_cli(rig: Rig, update_id: str | None = UPDATE_ID) -> int:
    arguments = ["--data-dir", str(rig.data_dir), "--socket", str(rig.socket)]
    return cli.main([*arguments, "--update-id", update_id] if update_id else arguments)


def result_of(rig: Rig) -> tuple[str, str | None]:
    result = read_result(rig.update_dir / "result.json")
    return result.outcome, result.reason


def test_a_successful_update_leaves_a_private_result_and_no_temporary_files(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.daemon.healthy_after = HEALTHY_AT_ONCE

    code = run_cli(rig)

    printed = capsys.readouterr().out
    lock = rig.data_dir / UPDATE_SUBDIRECTORY / ".lock"
    assert code == 0
    assert result_of(rig) == ("succeeded", None)
    assert stat.S_IMODE((rig.update_dir / "result.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert [p.name for p in rig.update_dir.iterdir() if p.name.startswith(".")] == []
    assert all(line.startswith("step=") for line in printed.splitlines())
    assert "step=write_result status=ok outcome=succeeded reason=None" in printed
    assert SECRET_ENV.split("=")[1] not in printed


def test_a_rolled_back_update_still_exits_zero(rig: Rig) -> None:
    rig.daemon.behaviour = "unhealthy"

    code = run_cli(rig)

    assert code == 0
    assert result_of(rig) == ("rolled_back", "unhealthy")


def test_a_rollback_that_fails_exits_nonzero_and_leaves_the_result(rig: Rig) -> None:
    rig.daemon.behaviour = "unhealthy"
    rig.daemon.failures["remove"] = Failure(500, "cannot remove", target="new")

    code = run_cli(rig)

    assert code == FAILURE_EXIT
    assert result_of(rig) == ("rollback_failed", "unhealthy")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param(json.dumps({"schema_version": 2}), id="wrong-schema"),
        pytest.param(json.dumps(plan_document(update_id=OTHER_UPDATE_ID)), id="other-update"),
    ],
)
def test_an_unreadable_plan_is_reported_without_touching_the_engine(rig: Rig, text: str) -> None:
    (rig.update_dir / "plan.json").write_text(text, encoding="utf-8")

    code = run_cli(rig)

    assert code == 0
    assert result_of(rig) == ("not_started", "plan_invalid")
    assert rig.daemon.calls == []


def test_a_lock_held_by_another_helper_refuses_the_update_without_touching_the_engine(
    rig: Rig,
) -> None:
    with try_exclusive_lock(rig.data_dir / UPDATE_SUBDIRECTORY / ".lock") as held:
        assert held
        code = run_cli(rig)

    assert code == 0
    assert result_of(rig) == ("not_started", "locked")
    assert rig.daemon.calls == []


@pytest.mark.parametrize(
    "update_id",
    [
        pytest.param(None, id="no-id"),
        pytest.param("not-an-id", id="malformed-id"),
        pytest.param(OTHER_UPDATE_ID, id="no-such-directory"),
    ],
)
def test_a_bad_update_id_or_directory_is_a_usage_error_with_no_result(
    rig: Rig, update_id: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOCKMIN_UPDATE_ID", raising=False)

    code = run_cli(rig, update_id)

    assert code == USAGE_EXIT
    assert rig.daemon.calls == []
    assert not (rig.update_dir / "result.json").exists()


def test_an_update_that_already_has_a_result_is_not_run_again(rig: Rig) -> None:
    (rig.update_dir / "result.json").write_text("{}", encoding="utf-8")

    code = run_cli(rig)

    assert code == USAGE_EXIT
    assert rig.daemon.calls == []


def test_a_crash_in_the_sequence_still_leaves_a_failed_result(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(self: Updater) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(Updater, "run", crash)

    code = run_cli(rig)

    assert code == FAILURE_EXIT
    assert result_of(rig) == ("rollback_failed", "engine_error")


def test_a_result_that_cannot_be_written_exits_nonzero(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(path: Path, result: object) -> None:
        raise PermissionError

    monkeypatch.setattr(cli, "write_result", refuse)
    rig.daemon.healthy_after = HEALTHY_AT_ONCE

    code = run_cli(rig)

    assert code == FAILURE_EXIT
    assert "step=write_result status=failed" in capsys.readouterr().out


class TestSignals:
    def arm(
        self,
        rig: Rig,
        signals: Callable[[int], None],
        verb: Verb,
        *,
        target: Target = "any",
        skip: int = 0,
        times: int = 1,
    ) -> Hook:
        def send() -> None:
            for _ in range(times):
                signals(signal.SIGTERM)

        hook = Hook(send, target=target, skip=skip)
        rig.daemon.hooks[verb] = hook
        return hook

    def test_a_signal_before_the_stop_changes_nothing_and_is_reported(
        self, rig: Rig, signals: Callable[[int], None]
    ) -> None:
        hook = self.arm(rig, signals, "image")

        code = run_cli(rig)

        assert code == 0
        assert hook.spent
        assert result_of(rig) == ("rolled_back", "engine_error")
        assert rig.daemon.mutations() == []

    def test_a_signal_during_the_health_wait_rolls_back(
        self, rig: Rig, signals: Callable[[int], None]
    ) -> None:
        rig.daemon.behaviour = "starting"
        hook = self.arm(rig, signals, "inspect", target="new", skip=1)

        code = run_cli(rig)

        assert code == 0
        assert hook.spent
        assert result_of(rig) == ("rolled_back", "engine_error")
        assert rig.daemon.names() == [("/dockmin", True)]
        assert list(rig.daemon.containers) == [OLD_ID]

    def test_signals_during_the_rollback_do_not_stop_it_or_lose_the_result(
        self, rig: Rig, signals: Callable[[int], None]
    ) -> None:
        rig.daemon.behaviour = "unhealthy"
        hook = self.arm(rig, signals, "remove", target="new", times=2)

        code = run_cli(rig)

        assert code == 0
        assert hook.spent
        assert result_of(rig) == ("rolled_back", "unhealthy")
        assert list(rig.daemon.containers) == [OLD_ID]

    def test_a_signal_after_the_swap_still_records_success(
        self, rig: Rig, signals: Callable[[int], None]
    ) -> None:
        rig.daemon.healthy_after = HEALTHY_AT_ONCE
        hook = self.arm(rig, signals, "remove", target="old")

        code = run_cli(rig)

        assert code == 0
        assert hook.spent
        assert result_of(rig) == ("succeeded", None)
        assert OLD_ID not in rig.daemon.containers


def test_the_helper_runs_as_a_program_from_its_directory(rig: Rig) -> None:
    rig.daemon.healthy_after = HEALTHY_AT_ONCE

    finished = subprocess.run(
        [
            sys.executable,
            "-I",
            str(HELPER_DIRECTORY),
            "--data-dir",
            str(rig.data_dir),
            "--socket",
            str(rig.socket),
        ],
        env={"DOCKMIN_UPDATE_ID": UPDATE_ID},
        cwd=rig.data_dir,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr
    assert result_of(rig) == ("succeeded", None)
    assert finished.stdout.splitlines()[0].startswith("step=read_plan status=ok")
