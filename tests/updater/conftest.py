"""Fixtures for the update helper: a data directory, a fake daemon on a socket, a virtual clock."""

import json
import signal
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.updater.fake_engine import (
    API_VERSION,
    NAME,
    NEW_IMAGE_ID,
    NEW_REFERENCE,
    OLD_ID,
    FakeDaemon,
    serving,
)
from updater.engine import Engine
from updater.plan import Plan, parse_plan, read_plan
from updater.result import Result
from updater.sequence import Updater

UPDATE_ID = "0123456789abcdef0123456789abcdef"
ORIGINAL_DATABASE = b"db-original"
ORIGINAL_SHM = b"shm-original"
DATABASE_MODE = 0o640
SECRET_ENV = "SECRET=hunter2"
OLD_STOP_TIMEOUT = 5
HEALTH_TIMEOUT = 30


class VirtualClock:
    """Time that moves only when the sequence sleeps, so waits cost nothing."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def plan_document(**override: Any) -> dict[str, Any]:
    """Return a valid plan in its documented JSON form, with top-level keys replaced."""
    document: dict[str, Any] = {
        "schema_version": 1,
        "update_id": UPDATE_ID,
        "created_at": "2026-11-03T10:00:00Z",
        "requested_by_user_id": 7,
        "request_event_id": 42,
        "engine_api_version": API_VERSION,
        "old": {
            "id": OLD_ID,
            "name": NAME,
            "stop_timeout": OLD_STOP_TIMEOUT,
            "image": "ghcr.io/example/dockmin:0.0.1",
        },
        "new": {
            "name": NAME,
            "image": NEW_REFERENCE,
            "image_id": NEW_IMAGE_ID,
            "create_body": {"Image": NEW_REFERENCE, "Env": [SECRET_ENV]},
        },
        "health_timeout_seconds": HEALTH_TIMEOUT,
        "database_files": ["dockmin.db", "dockmin.db-wal", "dockmin.db-shm"],
    }
    document.update(override)
    return document


@dataclass(slots=True)
class Run:
    """What one in-process run of the sequence produced."""

    result: Result
    lines: list[str]
    clock: VirtualClock


@dataclass(slots=True)
class Rig:
    """A data directory holding an update, and a fake daemon serving the helper's socket."""

    data_dir: Path
    update_dir: Path
    socket: Path
    daemon: FakeDaemon
    clock: VirtualClock = field(default_factory=VirtualClock)

    def write_plan(self, **override: Any) -> None:
        (self.update_dir / "plan.json").write_text(
            json.dumps(plan_document(**override)), encoding="utf-8"
        )

    def plan(self) -> Plan:
        return read_plan(self.update_dir / "plan.json")

    def engine(self) -> Engine:
        return Engine(str(self.socket), API_VERSION)

    def run(self, plan: Plan | None = None) -> Run:
        """Run the sequence against the daemon, with time that does not pass."""
        lines: list[str] = []
        result = Updater(
            plan or self.plan(),
            self.engine(),
            self.update_dir,
            self.data_dir,
            emit=lines.append,
            sleep=self.clock.sleep,
            monotonic=self.clock.monotonic,
        ).run()
        return Run(result, lines, self.clock)

    def database(self) -> bytes:
        return (self.data_dir / "dockmin.db").read_bytes()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    data_dir = tmp_path / "data"
    update_dir = data_dir / "update" / UPDATE_ID
    update_dir.mkdir(parents=True, mode=0o700)
    (data_dir / "dockmin.db").write_bytes(ORIGINAL_DATABASE)
    (data_dir / "dockmin.db").chmod(DATABASE_MODE)
    (data_dir / "dockmin.db-shm").write_bytes(ORIGINAL_SHM)
    daemon = FakeDaemon(data_dir)
    socket = tmp_path / "engine.sock"
    built = Rig(data_dir, update_dir, socket, daemon)
    built.write_plan()
    with serving(daemon, socket):
        yield built


@pytest.fixture
def signals() -> Iterator[Callable[[int], None]]:
    """Send a signal to the main thread from any thread; the handlers in place are put back."""
    saved = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    main = threading.main_thread().ident
    assert main is not None

    def send(number: int) -> None:
        signal.pthread_kill(main, number)

    yield send
    for number, handler in saved.items():
        signal.signal(number, handler)


def valid_plan() -> Plan:
    return parse_plan(plan_document())
