"""A bucket stub that lists and serves archives, and a live instance wired to it."""

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from urllib.parse import parse_qs, unquote

import httpx

from app.crypto.envelope import SecretLocation
from app.services.restore import RestoreCoordinator
from tests.object_storage.support import ACCESS, BUCKET, ENDPOINT, SECRET
from tests.restore_support import OPERATOR, Instance, make_instance

PREFIX = "backups/"
FOLDER = "backups/rule-1-20260101T000000/"
TARGET_NAME = "restore-bucket"
STAMP = "2026-01-01T00:00:00Z"
LISTED_AT = "2026-10-24T17:21:14Z"
FETCH_POLLS = 600
FETCH_PAUSE = 0.01

type Served = httpx.Response | Callable[[], httpx.Response]


def archive_key(second: int, folder: str = FOLDER) -> str:
    return f"{folder}dockmin-backup-20260101T0000{second:02d}Z.tar.gz"


class Breaking(httpx.AsyncByteStream):
    """A body that sends a first chunk and then fails, or waits forever when told to."""

    def __init__(self, first: bytes, *, hold: asyncio.Event | None = None) -> None:
        self.first = first
        self.hold = hold

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first
        if self.hold is None:
            raise httpx.ReadError("connection reset")
        await self.hold.wait()


class RemoteBucket:
    """Lists the keys it holds and serves their bytes; listing and bodies can be scripted."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.listed_sizes: dict[str, int] = {}
        self.served: dict[str, Served] = {}
        self.listing_reply: httpx.Response | Exception | None = None
        self.page_size = 1000
        self.list_requests = 0
        self.object_requests: list[str] = []
        self.prefixes: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        key = unquote(request.url.path).removeprefix(f"/{BUCKET}").lstrip("/")
        if request.method == "GET" and not key:
            return self._list(request)
        self.object_requests.append(key)
        return self._object(key)

    def _list(self, request: httpx.Request) -> httpx.Response:
        self.list_requests += 1
        if isinstance(self.listing_reply, Exception):
            raise self.listing_reply
        if self.listing_reply is not None:
            return self.listing_reply
        query = parse_qs(request.url.query.decode(), keep_blank_values=True)
        prefix = query.get("prefix", [""])[0]
        self.prefixes.append(prefix)
        after = query.get("continuation-token", [""])[0]
        keys = sorted(k for k in self.objects if k.startswith(prefix) and k > after)
        page, more = keys[: self.page_size], len(keys) > self.page_size
        blocks = "".join(
            f"<Contents><Key>{k}</Key><Size>{self.listed_sizes.get(k, len(self.objects[k]))}"
            f"</Size><LastModified>{LISTED_AT}</LastModified></Contents>"
            for k in page
        )
        token = f"<NextContinuationToken>{page[-1]}</NextContinuationToken>" if more else ""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="x">'
            f"<Name>{BUCKET}</Name><IsTruncated>{'true' if more else 'false'}</IsTruncated>"
            f"{blocks}{token}</ListBucketResult>"
        )
        return httpx.Response(200, content=xml.encode(), headers={"content-type": "text/xml"})

    def _object(self, key: str) -> httpx.Response:
        served = self.served.get(key)
        if served is not None:
            return served() if callable(served) else served
        data = self.objects.get(key)
        if data is None:
            body = "<Error><Code>NoSuchKey</Code><Message>m</Message></Error>"
            return httpx.Response(404, content=body.encode())
        return httpx.Response(200, content=data)


def add_target(
    instance: Instance, *, name: str = TARGET_NAME, ciphertext: str | None = None
) -> int:
    """Insert a path-style target aimed at the stub and return its id."""
    instance.world.run(
        "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, access_key_id, "
        "secret_access_key_ciphertext, use_path_style, verify_tls, ca_bundle, backup_prefix, "
        "activity_log_prefix, retention_count, created_at, updated_at) "
        "VALUES (?, ?, 'us-east-1', ?, ?, 'x', 1, 1, NULL, ?, 'activity-log', NULL, ?, ?)",
        (name, ENDPOINT, BUCKET, ACCESS, PREFIX, STAMP, STAMP),
    )
    target_id = int(instance.live_rows("SELECT max(id) FROM object_storage_target")[0][0])
    sealed = ciphertext or instance.world.vault.seal(
        SECRET, SecretLocation("object_storage_target", "secret_access_key_ciphertext", target_id)
    )
    instance.world.run(
        "UPDATE object_storage_target SET secret_access_key_ciphertext = ? WHERE id = ?",
        (sealed, target_id),
    )
    return target_id


def add_record(instance: Instance, target_id: int, key: str, sha256: str) -> None:
    instance.world.run(
        "INSERT INTO backup_record (origin, object_storage_target_id, created_at, size_bytes, "
        "key_version, key_fingerprint, destination_key, archive_sha256, schema_version, outcome, "
        "error) VALUES ('sync', ?, ?, 1, 1, 'fp', ?, ?, 1, 'success', NULL)",
        (target_id, STAMP, key, sha256),
    )


async def make_remote_instance(base: Path, bucket: RemoteBucket) -> Instance:
    """A live instance whose restore coordinator reads the stub, not the network."""
    instance = await make_instance(base)
    previous = instance.coordinator
    assert previous is not None
    instance.coordinator = RestoreCoordinator(
        gate=instance.gate,
        production=instance.production,
        data_dir=instance.data_dir,
        runtime=instance.runtime,
        stop_runtime=instance.stop,
        start_runtime=instance.start,
        clock=instance.clock,
        sleep=instance.sleep,
        transport=httpx.MockTransport(bucket),
    )
    return instance


async def until_fetched(instance: Instance) -> None:
    """Wait until the restore stops fetching."""
    assert instance.coordinator is not None
    for _ in range(FETCH_POLLS):
        status = instance.coordinator.status(OPERATOR, 1)
        if status is None or status.stage != "fetching":
            return
        await asyncio.sleep(FETCH_PAUSE)
    raise TimeoutError("the fetch did not finish")
