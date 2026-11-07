"""The connection test: which of three independent probes of a saved target succeed."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal

from app.object_storage.client import ObjectStorageClient
from app.object_storage.errors import MESSAGES, StorageFailure, StorageFailureKind
from app.object_storage.keys import validate_prefix

type StorageStage = Literal["bucket", "backup_prefix", "activity_log_prefix"]

CHECK_TIMEOUT: Final = 30.0

PERMISSION_NOTE: Final = (
    "The test does not check write or delete permission. A credential that can list and read "
    "passes every stage and fails at the first upload. Remote retention needs list access to the "
    "backup prefix and delete access to the archives in it."
)

_PASSED: Final[dict[StorageStage, str]] = {
    "bucket": (
        "The access key, the secret and the bucket name are accepted. This does not show that "
        "the credential may write or delete."
    ),
    "backup_prefix": "The credential can list the backup prefix, which remote retention needs.",
    "activity_log_prefix": "The credential can list the activity log prefix.",
}

_REFUSED: Final[dict[StorageStage, str]] = {
    "bucket": (
        "A refusal to a HEAD request cannot tell a missing bucket from a credential without access."
    ),
    "backup_prefix": (
        "The credential cannot list this prefix, so remote retention cannot run. Uploads do not "
        "need listing."
    ),
    "activity_log_prefix": ("The credential cannot list this prefix. Uploads do not need listing."),
}


@dataclass(frozen=True, slots=True)
class StorageCheck:
    """The outcome of one probe; kind, status and code describe the failure when it did not pass."""

    stage: StorageStage
    passed: bool
    kind: StorageFailureKind | None
    status: int | None
    code: str | None


async def check_target(
    client: ObjectStorageClient, *, backup_prefix: str, activity_log_prefix: str
) -> tuple[StorageCheck, ...]:
    """Probe the bucket and both prefixes; every probe runs whatever an earlier one reported."""
    deadline = asyncio.get_running_loop().time() + CHECK_TIMEOUT

    async def list_backup_folder() -> None:
        await _list_folder(client, backup_prefix)

    async def list_log_folder() -> None:
        await _list_folder(client, activity_log_prefix)

    probes: tuple[tuple[StorageStage, Callable[[], Awaitable[None]]], ...] = (
        ("bucket", client.head_bucket),
        ("backup_prefix", list_backup_folder),
        ("activity_log_prefix", list_log_folder),
    )
    return tuple([await _run(stage, probe, deadline) for stage, probe in probes])


def stage_message(check: StorageCheck) -> str:
    """Return the product-written explanation of a probe's outcome."""
    if check.passed:
        return _PASSED[check.stage]
    kind: StorageFailureKind = check.kind or "storage_error"
    message = MESSAGES[kind]
    if kind == "refused":
        return f"{message} {_REFUSED[check.stage]}"
    return message


async def _list_folder(client: ObjectStorageClient, prefix: str) -> None:
    folder = validate_prefix(prefix)
    await client.list_objects(f"{folder}/" if folder else "", max_keys=1)


async def _run(
    stage: StorageStage, probe: Callable[[], Awaitable[None]], deadline: float
) -> StorageCheck:
    failure: StorageFailure | None = None
    timed_out = False
    try:
        async with asyncio.timeout_at(deadline):
            await probe()
    except StorageFailure as exc:
        failure = exc
    except TimeoutError:
        timed_out = True
    if timed_out:
        return StorageCheck(stage, False, "unreachable", None, None)
    if failure is None:
        return StorageCheck(stage, True, None, None, None)
    return StorageCheck(stage, False, failure.kind, failure.status, failure.code)
