"""A fake bucket that stores, lists and deletes, and one backup-syncer rule over it."""

import asyncio
import dataclasses
import itertools
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx

from app.config import Settings
from app.crypto.envelope import SecretLocation
from app.crypto.vault import Vault
from app.scheduler.model import RunResult
from app.services.backups import BackupProduction
from app.subsystems.backup_syncer import BackupSyncer
from tests.crypto.helpers import FIRST_KEY, start
from tests.object_storage.support import ACCESS, BUCKET, ENDPOINT, SECRET, verify_signature
from tests.scheduler.support import Rig, dispatch_of

SYNCER = "backup-syncer"
CREATED = "2026-01-01T00:00:00Z"
RETRY_PAUSE = 2.0
ARCHIVE_NAME = re.compile(r"dockmin-backup-\d{8}T\d{6}Z\.tar\.gz")
LISTING_TIME = "2026-10-24T17:21:14Z"
TARGETS = itertools.count()

type Step = Callable[[httpx.Request], httpx.Response | Exception | None]


def error(status: int, code: str) -> Callable[[httpx.Request], httpx.Response]:
    def reply(_request: httpx.Request) -> httpx.Response:
        body = f'<?xml version="1.0"?><Error><Code>{code}</Code><Message>m</Message></Error>'
        return httpx.Response(
            status, content=body.encode(), headers={"content-type": "application/xml"}
        )

    return reply


def on_method(
    method: str, count: int, outcome: Callable[[httpx.Request], httpx.Response | Exception]
) -> Step:
    """Build a step that answers the first `count` requests of one method with `outcome`."""
    left = [count]

    def step(request: httpx.Request) -> httpx.Response | Exception | None:
        if request.method == method and left[0] > 0:
            left[0] -= 1
            return outcome(request)
        return None

    return step


def always_on(method: str, outcome: Callable[[httpx.Request], httpx.Response | Exception]) -> Step:
    def step(request: httpx.Request) -> httpx.Response | Exception | None:
        return outcome(request) if request.method == method else None

    return step


class Bucket:
    """Keeps objects by key, re-verifies every signature and can be scripted to fail."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.script: list[Step] = []
        self.log: list[str] = []
        self.problems: list[str] = []
        self.page_size = 1000
        self.hold: asyncio.Event | None = None
        self.entered = asyncio.Event()

    @property
    def requests(self) -> int:
        return len(self.log)

    def count(self, method: str) -> int:
        return sum(1 for line in self.log if line.startswith(method))

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        try:
            verify_signature(request)
        except AssertionError as failure:
            self.problems.append(str(failure))
        key = unquote(request.url.path).removeprefix(f"/{BUCKET}/")
        self.log.append(f"{request.method} {key}")
        for step in self.script:
            outcome = step(request)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                return outcome
        if self.hold is not None and request.method == "PUT":
            self.entered.set()
            await self.hold.wait()
        match request.method:
            case "HEAD":
                return httpx.Response(200)
            case "PUT":
                self.objects[key] = body
                return httpx.Response(200)
            case "DELETE":
                self.objects.pop(key, None)
                return httpx.Response(204)
            case "GET":
                return self.listing(request)
        return httpx.Response(405)

    def listing(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode(), keep_blank_values=True)
        prefix = query.get("prefix", [""])[0]
        limit = min(int(query.get("max-keys", ["1000"])[0]), self.page_size)
        after = query.get("continuation-token", [""])[0]
        keys = sorted(k for k in self.objects if k.startswith(prefix) and k > after)
        page, more = keys[:limit], len(keys) > limit
        blocks = "".join(
            f"<Contents><Key>{k}</Key><Size>{len(self.objects[k])}</Size>"
            f"<LastModified>{LISTING_TIME}</LastModified></Contents>"
            for k in page
        )
        token = f"<NextContinuationToken>{page[-1]}</NextContinuationToken>" if more else ""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="x">'
            f"<Name>{BUCKET}</Name><IsTruncated>{'true' if more else 'false'}</IsTruncated>"
            f"{blocks}{token}</ListBucketResult>"
        )
        return httpx.Response(
            200, content=xml.encode(), headers={"content-type": "application/xml"}
        )


def empty_listing(request: httpx.Request) -> httpx.Response | None:
    if request.method != "GET":
        return None
    xml = (
        '<ListBucketResult xmlns="x"><Name>b</Name>'
        "<IsTruncated>false</IsTruncated></ListBucketResult>"
    )
    return httpx.Response(200, content=xml.encode())


class Syncer:
    """One backup-syncer over a fake bucket, with the means to make targets, rules and runs."""

    def __init__(self, rig: Rig, settings: Settings, vault: Vault) -> None:
        self.rig = rig
        self.vault = vault
        self.settings = settings
        self.bucket = Bucket()
        self.sleeps: list[float] = []
        self.production = BackupProduction(settings.data_dir)
        deps, _ = rig.deps(settings)
        deps.engine.vault = vault
        deps = dataclasses.replace(deps, backup_production=self.production)

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.subsystem = BackupSyncer(
            deps, transport=httpx.MockTransport(self.bucket.handler), sleep=sleep
        )
        self.user_id = rig.user("operator")
        self.target_id = 0
        self.rule_id = 0
        self.job_id = 0
        self.folder = ""

    @classmethod
    async def build(cls, rig: Rig, settings: Settings) -> "Syncer":
        vault = await start(rig.database, FIRST_KEY)
        return cls(rig, settings, vault)

    def make_target(
        self,
        *,
        retention: int | None = None,
        endpoint: str = ENDPOINT,
        prefix: str = "backups",
        ciphertext: str | None = None,
    ) -> int:
        self.rig.sql(
            "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, "
            "access_key_id, secret_access_key_ciphertext, use_path_style, verify_tls, ca_bundle, "
            "backup_prefix, activity_log_prefix, retention_count, created_at, updated_at) "
            "VALUES (?, ?, 'us-east-1', ?, ?, 'x', 1, 1, NULL, ?, 'activity-log', ?, ?, ?)",
            (
                f"target-{next(TARGETS)}",
                endpoint,
                BUCKET,
                ACCESS,
                prefix,
                retention,
                CREATED,
                CREATED,
            ),
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

    async def make_rule(self, target_id: int | None = None) -> tuple[int, int]:
        parameters = json.dumps({"object_storage_target_id": target_id or self.target_id})
        rule_id = await self.rig.rule(
            f"sync-{next(TARGETS)}",
            owner=self.user_id,
            subsystem=SYNCER,
            parameters=parameters,
            environment_id=None,
        )
        job_id = await self.rig.job(subsystem=SYNCER, rule_id=rule_id, expression="3600")
        created = self.rig.sql("SELECT created_at FROM automation_rule WHERE id = ?", (rule_id,))
        stamp = str(created[0][0]).replace("-", "").replace(":", "").removesuffix("Z")
        self.rule_id, self.job_id = rule_id, job_id
        self.folder = f"backups/rule-{rule_id}-{stamp}/"
        return rule_id, job_id

    async def run(self, job_id: int | None = None, *, dry: bool = False) -> RunResult:
        return await dispatch_of(
            self.rig,
            self.subsystem,
            job_id or self.job_id,
            trigger="dry_run" if dry else "schedule",
            dry_run=dry,
        )

    def archives(self, folder: str | None = None) -> list[str]:
        base = folder or self.folder
        return sorted(
            key[len(base) :]
            for key in self.bucket.objects
            if key.startswith(base) and ARCHIVE_NAME.fullmatch(key[len(base) :])
        )

    def work_directories(self) -> list[str]:
        base = self.settings.data_dir / "backup-work"
        return sorted(path.name for path in base.iterdir()) if base.exists() else []

    def events(self, action: str) -> list[dict[str, Any]]:
        return [
            {**dict(row), "detail": json.loads(row["detail"]) if row["detail"] else {}}
            for row in self.rig.sql(
                "SELECT * FROM activity_event WHERE action = ? ORDER BY id", (action,)
            )
        ]

    def count(self, table: str) -> int:
        return int(self.rig.sql(f"SELECT count(*) FROM {table}")[0][0])
