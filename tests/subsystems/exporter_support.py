"""A fake bucket that re-verifies signatures, and the rows an exporter test runs against."""

import csv
import gzip
import io
import itertools
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.config import Settings
from app.crypto.envelope import SecretLocation
from app.crypto.vault import Vault
from app.scheduler.model import RunResult
from app.services.activity_retention import ActivityRetention
from app.subsystems.activity_log_exporter import ActivityLogExporter
from tests.object_storage.support import ACCESS, BUCKET, ENDPOINT, SECRET, verify_signature
from tests.scheduler.support import START, Rig, dispatch_of

EXPORTER = "activity-log-exporter"
PREFIX = "exports"
CREATED = "2026-01-01T00:00:00Z"
RETRY_PAUSE = 2.0
PUT = "PUT"
TARGETS = itertools.count()

type Step = Callable[[httpx.Request], httpx.Response | Exception | None]


def error(status: int, code: str) -> Callable[[httpx.Request], httpx.Response]:
    """Build a step answering with an S3 error document."""

    def reply(_request: httpx.Request) -> httpx.Response:
        body = f'<?xml version="1.0"?><Error><Code>{code}</Code><Message>m</Message></Error>'
        return httpx.Response(
            status, content=body.encode(), headers={"content-type": "application/xml"}
        )

    return reply


def put_times(count: int, outcome: Callable[[httpx.Request], httpx.Response | Exception]) -> Step:
    """Build a step that fails the first `count` PUTs and then lets requests through."""
    left = [count]

    def step(request: httpx.Request) -> httpx.Response | Exception | None:
        if request.method == PUT and left[0] > 0:
            left[0] -= 1
            return outcome(request)
        return None

    return step


def nth_put(n: int, outcome: Callable[[httpx.Request], httpx.Response | Exception]) -> Step:
    """Build a step that fails only the n-th PUT."""
    seen = [0]

    def step(request: httpx.Request) -> httpx.Response | Exception | None:
        if request.method != PUT:
            return None
        seen[0] += 1
        return outcome(request) if seen[0] == n else None

    return step


class Bucket:
    """Stores objects, checks every signature, and can be scripted to fail."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.cursor_at_put: list[int | None] = []
        self.script: list[Step] = []
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        verify_signature(request)
        for step in self.script:
            outcome = step(request)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                return outcome
        if request.method == PUT:
            rows = self.rig.sql("SELECT last_event_id FROM activity_export_cursor")
            self.cursor_at_put.append(int(rows[0][0]) if rows else None)
            key = request.url.path.split("/", 2)[2]
            self.puts.append(key)
            self.objects[key] = request.content
        return httpx.Response(200)


def rows_of(body: bytes) -> list[list[str]]:
    """Decompress an exported object and parse its CSV."""
    return list(csv.reader(io.StringIO(gzip.decompress(body).decode("utf-8"), newline="")))


class Exporter:
    """One exporter rule over a fake bucket, with the means to seed, run and prune."""

    def __init__(self, rig: Rig, settings: Settings, vault: Vault) -> None:
        self.rig = rig
        self.vault = vault
        self.settings = settings
        self.bucket = Bucket(rig)
        self.sleeps: list[float] = []
        deps, _ = rig.deps(settings)
        deps.engine.vault = vault

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.subsystem = ActivityLogExporter(
            deps, transport=httpx.MockTransport(self.bucket.handler), sleep=sleep
        )
        self.user_id = rig.user("operator")
        self.target_id = 0
        self.rule_id = 0
        self.job_id = 0

    def make_target(
        self,
        *,
        endpoint: str = ENDPOINT,
        prefix: str = PREFIX,
        ciphertext: str | None = None,
        access: str = ACCESS,
    ) -> int:
        self.rig.sql(
            "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, "
            "access_key_id, secret_access_key_ciphertext, use_path_style, verify_tls, ca_bundle, "
            "backup_prefix, activity_log_prefix, retention_count, created_at, updated_at) "
            "VALUES (?, ?, 'us-east-1', ?, ?, 'x', 1, 1, NULL, 'backups', ?, NULL, ?, ?)",
            (f"target-{next(TARGETS)}", endpoint, BUCKET, access, prefix, CREATED, CREATED),
        )
        target_id = int(self.rig.sql("SELECT max(id) FROM object_storage_target")[0][0])
        sealed = ciphertext or self.vault.seal(
            SECRET,
            SecretLocation("object_storage_target", "secret_access_key_ciphertext", target_id),
        )
        self.rig.sql(
            "UPDATE object_storage_target SET secret_access_key_ciphertext = ? WHERE id = ?",
            (sealed, target_id),
        )
        self.target_id = target_id
        return target_id

    async def make_rule(self, target_id: int | None = None) -> None:
        parameters = json.dumps({"object_storage_target_id": target_id or self.target_id})
        self.rule_id = await self.rig.rule(
            f"export-{next(TARGETS)}",
            owner=self.user_id,
            subsystem=EXPORTER,
            parameters=parameters,
            environment_id=None,
        )
        self.job_id = await self.rig.job(
            subsystem=EXPORTER, rule_id=self.rule_id, expression="3600"
        )

    async def run(self, *, dry: bool = False) -> RunResult:
        return await dispatch_of(
            self.rig,
            self.subsystem,
            self.job_id,
            trigger="dry_run" if dry else "schedule",
            dry_run=dry,
        )

    def seed(self, count: int, *, when: datetime = START, start: int = 0) -> None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        connection = self.rig.database.connect()
        try:
            connection.execute("BEGIN")
            connection.executemany(
                "INSERT INTO activity_event (category, actor_type, actor_user_id, target_type, "
                "target_id, action, result, detail, occurred_at) VALUES ('user_action', 'user', "
                "?, 'container', ?, 'container_restarted', 'success', ?, ?)",
                [
                    (
                        self.user_id,
                        f"c{start + n}",
                        json.dumps({"note": f'row {start + n}, "q"\nline'}),
                        stamp,
                    )
                    for n in range(count)
                ],
            )
            connection.execute("COMMIT")
        finally:
            connection.close()

    def age(self, days: int = 30) -> None:
        self.rig.clock.now += timedelta(days=days)

    def cursor(self) -> int | None:
        found = self.rig.sql(
            "SELECT last_event_id FROM activity_export_cursor WHERE rule_id = ?", (self.rule_id,)
        )
        return int(found[0][0]) if found else None

    def failures(self) -> list[dict[str, Any]]:
        """Return the exporter's failure rows with detail decoded."""
        return [
            {**dict(row), "detail": json.loads(row["detail"])}
            for row in self.rig.sql(
                "SELECT * FROM activity_event WHERE action = 'activity_log_exported' ORDER BY id"
            )
        ]

    def count(self) -> int:
        return int(self.rig.sql("SELECT count(*) FROM activity_event")[0][0])

    async def prune(self) -> int:
        retention = ActivityRetention(self.rig.database, clock=self.rig.clock, days=7)
        return (await retention.apply(self.rig.clock())).removed
