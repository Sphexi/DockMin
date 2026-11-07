"""Run structure shared by the subsystems that copy data to an object storage target."""

import asyncio
import gzip
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from typing import Final

import httpx

from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.services.object_storage import (
    CredentialUnreadable,
    ObjectStorageClient,
    ObjectStorageTargets,
    StorageFailure,
)
from app.subsystems.base import RunContext

TARGET_TYPE: Final = "object_storage_target"
CONTENT_TYPE: Final = "application/gzip"
START_BUDGET: Final = timedelta(minutes=20)
RETRY_DELAY: Final = 2.0
SERVER_ERROR: Final = 500

TARGET_MISSING: Final = "The object storage target no longer exists."
CREDENTIAL_UNREADABLE: Final = "The target's stored secret cannot be read."
PREFIX_INVALID: Final = "The target's activity log prefix does not form a valid object key."
RULE_MISSING: Final = "The automation was deleted during the run."


class ExportStopped(Exception):
    """A run that cannot go on; `detail` joins the failure row's detail."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        failure: StorageFailure | None = None,
        detail: Mapping[str, DetailValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.failure = failure
        self.detail = dict(detail or {})


async def resolve_target(targets: ObjectStorageTargets, target_id: int) -> tuple[str, str]:
    """Return the target's name and activity log prefix; ExportStopped when it is gone."""
    known = await targets.prefix_of(target_id)
    if known is None:
        raise ExportStopped("target_missing", TARGET_MISSING)
    return known


async def connect_target(
    targets: ObjectStorageTargets,
    target_id: int,
    *,
    transport: httpx.AsyncBaseTransport | None,
    clock: Clock,
) -> ObjectStorageClient:
    """Open the target's secret and build a client; ExportStopped when that cannot be done.

    The caller enters the client as a context manager.
    """
    try:
        opened = await targets.open(target_id)
    except CredentialUnreadable:
        raise ExportStopped("credential_unreadable", CREDENTIAL_UNREADABLE) from None
    if opened is None:
        raise ExportStopped("target_missing", TARGET_MISSING)
    try:
        return ObjectStorageClient(
            opened.endpoint, opened.credentials, transport=transport, clock=clock
        )
    except StorageFailure as failure:
        raise ExportStopped(failure.kind, failure.message, failure=failure) from None


def gzip_body(pieces: Sequence[bytes]) -> bytes:
    """Compress the pieces into one gzip member with a zero modification time."""
    return gzip.compress(b"".join(pieces), mtime=0)


async def put_with_retry(
    client: ObjectStorageClient,
    key: str,
    body: bytes,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> StorageFailure | None:
    """Store the object, once more after a pause when the first failure may pass.

    Returns the failure that ended the attempt, or None once the object is stored.
    """
    try:
        await client.put_object(key, body, content_type=CONTENT_TYPE)
        return None
    except StorageFailure as failure:
        if not _transient(failure):
            return failure
    await sleep(RETRY_DELAY)
    try:
        await client.put_object(key, body, content_type=CONTENT_TYPE)
    except StorageFailure as failure:
        return failure
    return None


async def record_stop(
    context: RunContext,
    *,
    action: str,
    target_type: str,
    target_id: str,
    stopped: ExportStopped,
    detail: dict[str, DetailValue],
    environment_id: int | None = None,
) -> None:
    """Write the failure row for a stopped run; the storage status and code join `detail`."""
    failure = stopped.failure
    if failure is not None:
        if failure.status is not None:
            detail["storage_status"] = failure.status
        if failure.code is not None:
            detail["storage_code"] = failure.code
    await context.activity.record_failure(
        context.actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        environment_id=environment_id,
        reason=stopped.reason,
        detail=detail,
        message=stopped.message,
    )


def _transient(failure: StorageFailure) -> bool:
    return failure.kind == "unreachable" or (
        failure.kind == "storage_error"
        and failure.status is not None
        and failure.status >= SERVER_ERROR
    )
