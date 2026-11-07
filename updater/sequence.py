"""The container swap: preflight, replace, health wait, and the rollback path."""

import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from updater.engine import Engine, EngineError
from updater.files import copy_file_atomic, fsync_directory
from updater.plan import CONTAINER_ID_PATTERN, DATABASE_BACKUP_DIRECTORY, PREVIOUS_SUFFIX, Plan
from updater.result import FailureReason, Outcome, Reason, Result, utc_timestamp

HEALTH_POLL_SECONDS = 2.0
NEW_STOP_TIMEOUT_SECONDS = 10
MAX_FIELD_CHARS = 200
CLOCK_SKEW_SECONDS = 5.0

Stage = Literal["stop", "copy", "rename", "create", "start", "health"]

_STAGE_REASONS: dict[Stage, FailureReason] = {
    "stop": "engine_error",
    "copy": "copy_failed",
    "rename": "engine_error",
    "create": "create_failed",
    "start": "start_failed",
    "health": "engine_error",
}


class Interrupted(Exception):
    """The helper received a termination signal."""


class StopGate:
    """Raises `Interrupted` on a termination signal only while open; otherwise records it."""

    def __init__(self) -> None:
        self.open = False
        self.requested = False

    def handle(self, signum: int, frame: object) -> None:
        """Signal handler: note the request and, if open, close the gate and raise."""
        self.requested = True
        if self.open:
            self.open = False
            raise Interrupted(f"signal {signum}")


class UpdateFailure(Exception):
    """The replacement cannot be kept; the sequence must roll back."""

    def __init__(self, reason: FailureReason, detail: str) -> None:
        super().__init__(detail)
        self.reason: FailureReason = reason


@dataclass(slots=True)
class _Progress:
    stage: Stage = "stop"
    stop_attempted: bool = False
    database_copied: bool = False
    new_container_id: str | None = None
    create_started_at: float | None = None
    completed: bool = False


class Updater:
    """Runs one update against an Engine and returns the result to record."""

    def __init__(
        self,
        plan: Plan,
        engine: Engine,
        update_dir: Path,
        data_dir: Path,
        *,
        emit: Callable[[str], None],
        poll_interval: float = HEALTH_POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        gate: StopGate | None = None,
    ) -> None:
        self._plan = plan
        self._engine = engine
        self._update_dir = update_dir
        self._data_dir = data_dir
        self._emit = emit
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._monotonic = monotonic
        self._gate = gate if gate is not None else StopGate()
        self._previous_name = plan.old.name + PREVIOUS_SUFFIX

    def run(self) -> Result:
        """Swap the containers; every outcome is returned, none is raised."""
        refusal = self._preflight()
        if refusal is not None:
            return refusal
        progress = _Progress()
        failure: UpdateFailure | None = None
        try:
            try:
                self._gate.open = True
                if self._gate.requested:
                    raise Interrupted("signal received before the stop")
                self._replace(progress)
            except Exception as exc:
                failure = self._classify(exc, progress)
            finally:
                self._gate.open = False
        except Interrupted:
            if failure is None and not progress.completed:
                failure = UpdateFailure("engine_error", "interrupted")
        if failure is not None:
            self._step("replace", "failed", reason=failure.reason, error=str(failure))
            return self._roll_back(progress, failure.reason)
        self._remove_previous()
        return self._result("succeeded", None, progress.new_container_id)

    def _preflight(self) -> Result | None:
        plan = self._plan
        try:
            old = self._engine.inspect_container(plan.old.id)
            if old.get("Name") != f"/{plan.old.name}":
                raise EngineError("the old container no longer carries the planned name")
            if self._container_exists(self._previous_name):
                self._step("preflight", "refused", reason="previous_exists")
                return self._result("not_started", "previous_exists", None)
            image = self._engine.inspect_image(plan.new.image)
            if image.get("Id") != plan.new.image_id:
                self._step("preflight", "refused", reason="image_changed")
                return self._result("not_started", "image_changed", None)
        except Exception as exc:
            self._step("preflight", "failed", error=_field(exc))
            return self._result("not_started", "engine_error", None)
        self._step("preflight", "ok", container=plan.old.id[:12], image=plan.new.image_id[:19])
        return None

    def _container_exists(self, name: str) -> bool:
        try:
            self._engine.inspect_container(name)
        except EngineError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def _replace(self, progress: _Progress) -> None:
        plan = self._plan
        engine = self._engine

        progress.stage = "stop"
        progress.stop_attempted = True
        engine.stop_container(plan.old.id, plan.old.stop_timeout)
        self._step("stop_old", "ok", container=plan.old.id[:12], timeout=plan.old.stop_timeout)

        progress.stage = "copy"
        copied = self._copy_database()
        progress.database_copied = True
        self._step("copy_database", "ok", files=copied)

        progress.stage = "rename"
        engine.rename_container(plan.old.id, self._previous_name)
        self._step("rename_old", "ok", name=self._previous_name)

        progress.stage = "create"
        progress.create_started_at = time.time()
        progress.new_container_id = engine.create_container(plan.new.name, plan.new.create_body)
        self._step("create_new", "ok", container=progress.new_container_id[:12], name=plan.new.name)
        created = engine.inspect_container(progress.new_container_id)
        if created.get("Image") != plan.new.image_id:
            raise UpdateFailure("image_changed", "the created container uses a different image")

        progress.stage = "start"
        engine.start_container(progress.new_container_id)
        self._step("start_new", "ok", container=progress.new_container_id[:12])

        progress.stage = "health"
        self._await_healthy(progress.new_container_id, _has_healthcheck(created))
        progress.completed = True

    def _await_healthy(self, container_id: str, has_healthcheck: bool) -> None:
        timeout = self._plan.health_timeout_seconds
        deadline = self._monotonic() + timeout
        while True:
            state = _mapping(self._engine.inspect_container(container_id).get("State"))
            restarting = bool(state.get("Restarting"))
            if not state.get("Running") and not restarting:
                raise UpdateFailure("exited", f"exit code {state.get('ExitCode')}")
            if restarting and not has_healthcheck:
                raise UpdateFailure("exited", "the container is restarting")
            status = _mapping(state.get("Health")).get("Status")
            if has_healthcheck:
                if status == "healthy":
                    self._step("wait_healthy", "ok", container=container_id[:12], mode="health")
                    return
                if status == "unhealthy":
                    raise UpdateFailure("unhealthy", "the health check reported unhealthy")
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                if has_healthcheck:
                    raise UpdateFailure("health_timeout", f"not healthy within {timeout} seconds")
                self._step("wait_healthy", "ok", container=container_id[:12], mode="running_only")
                return
            self._sleep(min(self._poll_interval, remaining))

    def _remove_previous(self) -> None:
        try:
            self._engine.remove_container(self._plan.old.id, force=False)
        except Exception as exc:
            self._step("remove_previous", "failed", error=_field(exc))
        else:
            self._step("remove_previous", "ok", container=self._plan.old.id[:12])

    def _classify(self, exc: Exception, progress: _Progress) -> UpdateFailure:
        if isinstance(exc, UpdateFailure):
            return exc
        return UpdateFailure(_STAGE_REASONS[progress.stage], _field(exc))

    def _copy_database(self) -> int:
        backup_dir = self._update_dir / DATABASE_BACKUP_DIRECTORY
        backup_dir.mkdir(mode=0o700)
        copied = 0
        for name in self._plan.database_files:
            source = self._data_dir / name
            try:
                mode = source.lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(mode):
                raise OSError(f"{name} is not a regular file")
            copy_file_atomic(source, backup_dir / name)
            copied += 1
        fsync_directory(backup_dir)
        return copied

    def _restore_database(self) -> int:
        backup_dir = self._update_dir / DATABASE_BACKUP_DIRECTORY
        for name in self._plan.database_files:
            (self._data_dir / name).unlink(missing_ok=True)
        restored = 0
        for name in self._plan.database_files:
            backup = backup_dir / name
            if backup.is_file():
                copy_file_atomic(backup, self._data_dir / name)
                restored += 1
        return restored

    def _roll_back(self, progress: _Progress, reason: FailureReason) -> Result:
        plan = self._plan
        engine = self._engine
        excerpt: tuple[str, ...] = ()
        new_gone = True
        restored = True
        renamed = True
        started = True

        new_id = progress.new_container_id
        if new_id is None and progress.stage == "create":
            new_id = self._find_unreported_container(progress.create_started_at)
        if new_id is not None:
            excerpt = self._capture_logs(new_id)
            self._attempt(
                "stop_new", lambda: engine.stop_container(new_id, NEW_STOP_TIMEOUT_SECONDS)
            )
            new_gone = self._attempt(
                "remove_new", lambda: engine.remove_container(new_id, force=True)
            )
        if new_gone and progress.database_copied:
            restored = self._attempt("restore_database", self._restore_database)
        if new_gone and progress.stop_attempted:
            renamed = self._attempt("rename_back", self._rename_back)
            if restored:
                started = self._attempt("start_old", lambda: engine.start_container(plan.old.id))
        complete = new_gone and restored and renamed and started
        return self._result(
            "rolled_back" if complete else "rollback_failed", reason, new_id, excerpt
        )

    def _find_unreported_container(self, since: float | None) -> str | None:
        plan = self._plan
        try:
            info = self._engine.inspect_container(plan.new.name)
        except EngineError as exc:
            if exc.status == 404:
                self._step("find_created", "ok", found="no")
                return None
            self._step("find_created", "failed", error=_field(exc))
            return None
        container_id = info.get("Id")
        ours = (
            isinstance(container_id, str)
            and CONTAINER_ID_PATTERN.fullmatch(container_id) is not None
            and container_id != plan.old.id
            and info.get("Name") == f"/{plan.new.name}"
            and info.get("Image") == plan.new.image_id
            and since is not None
            and _created_after(info.get("Created"), since - CLOCK_SKEW_SECONDS)
        )
        if not ours or not isinstance(container_id, str):
            self._step("find_created", "skipped", found="unrelated")
            return None
        self._step("find_created", "ok", found="yes", container=container_id[:12])
        return container_id

    def _attempt(self, step: str, action: Callable[[], object]) -> bool:
        try:
            action()
        except Exception as exc:
            self._step(step, "failed", error=_field(exc))
            return False
        self._step(step, "ok")
        return True

    def _rename_back(self) -> None:
        info = self._engine.inspect_container(self._plan.old.id)
        if info.get("Name") == f"/{self._previous_name}":
            self._engine.rename_container(self._plan.old.id, self._plan.old.name)

    def _capture_logs(self, container_id: str) -> tuple[str, ...]:
        try:
            info = self._engine.inspect_container(container_id)
            tty = bool(_mapping(info.get("Config")).get("Tty"))
            lines = self._engine.container_logs(container_id, tty=tty)
        except Exception as exc:
            self._step("capture_logs", "failed", error=_field(exc))
            return ()
        self._step("capture_logs", "ok", lines=len(lines))
        return tuple(lines)

    def _result(
        self,
        outcome: Outcome,
        reason: Reason | None,
        new_container_id: str | None,
        excerpt: tuple[str, ...] = (),
    ) -> Result:
        return Result(
            update_id=self._plan.update_id,
            outcome=outcome,
            reason=reason,
            finished_at=utc_timestamp(),
            new_container_id=new_container_id,
            log_excerpt=excerpt,
        )

    def _step(self, step: str, status: str, **fields: object) -> None:
        parts = [f"step={step}", f"status={status}"]
        parts.extend(f"{key}={_field(value)}" for key, value in fields.items())
        self._emit(" ".join(parts))


def _has_healthcheck(info: dict[str, object]) -> bool:
    test = _mapping(_mapping(info.get("Config")).get("Healthcheck")).get("Test")
    return isinstance(test, list) and bool(test) and test[0] != "NONE"


_CREATED = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z")


def _created_after(created: object, threshold: float) -> bool:
    match = _CREATED.fullmatch(created) if isinstance(created, str) else None
    if match is None:
        return False
    fraction = (match.group(2) or "")[:7]
    stamp = datetime.fromisoformat(f"{match.group(1)}{fraction}+00:00")
    return stamp.timestamp() >= threshold


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


_WHITESPACE = re.compile(r"\s+")


def _field(value: object) -> str:
    text = _WHITESPACE.sub(" ", str(value)).strip()
    return text[:MAX_FIELD_CHARS] if text else "-"
