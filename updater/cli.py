"""Command line wiring: arguments, plan loading, signal handling, and writing the result."""

import argparse
import contextlib
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from updater.engine import Engine
from updater.files import try_exclusive_lock
from updater.plan import (
    LOCK_FILE,
    PLAN_FILE,
    RESULT_FILE,
    UPDATE_ID_PATTERN,
    UPDATE_SUBDIRECTORY,
    Plan,
    read_plan,
)
from updater.result import RefusalReason, Result, utc_timestamp, write_result
from updater.sequence import StopGate, Updater

DEFAULT_DATA_DIR = "/data"
DEFAULT_SOCKET = "/var/run/docker.sock"
UPDATE_ID_ENVIRONMENT = "DOCKMIN_UPDATE_ID"
USAGE_EXIT = 2
FAILURE_EXIT = 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the update named by the environment and return the process exit code."""
    parser = argparse.ArgumentParser(
        prog="updater", description="Swap a container for a newer one."
    )
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--update-id", default=os.environ.get(UPDATE_ID_ENVIRONMENT))
    args = parser.parse_args(argv)

    update_id = args.update_id
    if not isinstance(update_id, str) or UPDATE_ID_PATTERN.fullmatch(update_id) is None:
        _emit("step=start status=failed error=update id missing or malformed")
        return USAGE_EXIT
    update_dir = Path(args.data_dir) / UPDATE_SUBDIRECTORY / update_id
    if not update_dir.is_dir():
        _emit("step=start status=failed error=update directory not found")
        return USAGE_EXIT
    result_path = update_dir / RESULT_FILE
    if result_path.exists():
        _emit("step=start status=failed error=a result already exists")
        return USAGE_EXIT

    data_dir = Path(args.data_dir)
    lock_path = data_dir / UPDATE_SUBDIRECTORY / LOCK_FILE
    try:
        with try_exclusive_lock(lock_path) as acquired:
            if acquired:
                result = _execute(update_id, update_dir, data_dir, args.socket)
            else:
                _emit("step=lock status=refused reason=locked")
                result = _refusal(update_id, "locked")
            return _write(result_path, result)
    except OSError as exc:
        _emit(f"step=lock status=failed error={exc.__class__.__name__}")
        return FAILURE_EXIT


def _write(result_path: Path, result: Result) -> int:
    try:
        write_result(result_path, result)
    except OSError as exc:
        _emit(f"step=write_result status=failed error={exc.__class__.__name__}")
        return FAILURE_EXIT
    _emit(f"step=write_result status=ok outcome={result.outcome} reason={result.reason}")
    return FAILURE_EXIT if result.outcome == "rollback_failed" else 0


def _refusal(update_id: str, reason: RefusalReason) -> Result:
    return Result(
        update_id=update_id,
        outcome="not_started",
        reason=reason,
        finished_at=utc_timestamp(),
        new_container_id=None,
        log_excerpt=(),
    )


def _execute(update_id: str, update_dir: Path, data_dir: Path, socket_path: str) -> Result:
    gate = StopGate()
    signal.signal(signal.SIGTERM, gate.handle)
    signal.signal(signal.SIGINT, gate.handle)
    plan = _load_plan(update_id, update_dir)
    if plan is None:
        return _refusal(update_id, "plan_invalid")
    updater = Updater(
        plan,
        Engine(socket_path, plan.engine_api_version),
        update_dir,
        data_dir,
        emit=_emit,
        gate=gate,
    )
    try:
        return updater.run()
    except Exception as exc:
        _emit(f"step=run status=failed error={exc.__class__.__name__}")
        return Result(
            update_id=update_id,
            outcome="rollback_failed",
            reason="engine_error",
            finished_at=utc_timestamp(),
            new_container_id=None,
            log_excerpt=(),
        )


def _load_plan(update_id: str, update_dir: Path) -> Plan | None:
    try:
        plan = read_plan(update_dir / PLAN_FILE)
        if plan.update_id != update_id:
            raise ValueError("plan update_id does not match the update directory")
    except (OSError, ValueError) as exc:
        _emit(f"step=read_plan status=failed error={' '.join(str(exc).split())[:200]}")
        return None
    _emit(f"step=read_plan status=ok container={plan.old.id[:12]} image={plan.new.image}")
    return plan


def _emit(line: str) -> None:
    # A closed stdout must not abort a swap that is already under way.
    with contextlib.suppress(OSError):
        print(line, flush=True)


if __name__ == "__main__":
    sys.exit(main())
