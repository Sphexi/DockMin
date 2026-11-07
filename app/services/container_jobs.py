"""Container create and recreate started as background jobs."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Final

from fastapi import FastAPI

from app.auth.errors import PermissionDenied
from app.auth.principal import Principal
from app.db.repositories.user import UserRepository
from app.docker_engine.models import PullProgress
from app.logging import request_id_var
from app.services.assembly import app_container_services
from app.services.container_review import created_flags, recreated_flags
from app.services.engine_setup import EngineRuntime
from app.services.image_jobs import NO_LONGER_PERMITTED, JobCancelled, JobProgress, JobResult
from app.services.pull_credentials import CredentialChoice

__all__ = ["start_container_create_job", "start_container_recreate_job"]

REFERENCE_MAX: Final = 256
CREATE_CANCELLED: Final = "The pull was cancelled. No container was created."
RECREATE_CANCELLED: Final = "The pull was cancelled. The container was not changed."
STARTER_RECHECK_SECONDS: Final = 5.0
PULLED_STAGE: Final = "pulled"


class _StarterGuard:
    """Passes progress on and refuses to go past the pull once the starter lost the right to."""

    def __init__(self, inner: JobProgress) -> None:
        self._inner = inner
        self.revoked = False

    @property
    def cancel_requested(self) -> bool:
        return self._inner.cancel_requested

    def on_pull(self, event: PullProgress) -> None:
        self._inner.on_pull(event)

    def on_stage(self, stage: str) -> None:
        if stage == PULLED_STAGE and self.revoked:
            raise PermissionDenied(NO_LONGER_PERMITTED)
        self._inner.on_stage(stage)


async def _current_principal(app: FastAPI, starter: Principal) -> Principal:
    """Return the starter as the account stands now; PermissionDenied when it lost the right."""
    async with app.state.database.session() as db:
        user = await UserRepository(db).get(starter.user_id)
    if (
        user is None
        or not user.is_enabled
        or (starter.is_administrator and user.role != "administrator")
    ):
        raise PermissionDenied(NO_LONGER_PERMITTED)
    return replace(starter, username=user.username, role=user.role)


async def _watch_starter(app: FastAPI, starter: Principal, guard: _StarterGuard) -> None:
    while not guard.revoked:
        await asyncio.sleep(STARTER_RECHECK_SECONDS)
        try:
            await _current_principal(app, starter)
        except PermissionDenied:
            guard.revoked = True


@contextlib.asynccontextmanager
async def _guarded(
    app: FastAPI, starter: Principal, progress: JobProgress
) -> AsyncIterator[tuple[Principal, _StarterGuard]]:
    """Yield the starter as it stands now and a feed that stops the job if that changes."""
    principal = await _current_principal(app, starter)
    guard = _StarterGuard(progress)
    watcher = asyncio.create_task(_watch_starter(app, starter, guard))
    try:
        yield principal, guard
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def _engine_of(app: FastAPI) -> EngineRuntime:
    engine: EngineRuntime | None = getattr(app.state, "engine", None)
    if engine is None:
        raise RuntimeError("the Engine runtime has not been started")
    return engine


def start_container_create_job(
    app: FastAPI,
    principal: Principal,
    environment_id: int,
    form: Mapping[str, str],
    *,
    duplicate_of: str | None,
    credential_choice: CredentialChoice | None,
    template: str | None = None,
) -> str:
    """Start a container create in the background and return its id; TooManyJobs at the limits.

    The create validates and authorises again on a session of its own.
    """
    engine = _engine_of(app)
    submitted = dict(form)
    request_id = request_id_var.get()

    async def run(progress: JobProgress) -> JobResult:
        request_id_var.set(request_id)
        async with (
            _guarded(app, principal, progress) as (current, feed),
            app_container_services(app) as services,
        ):
            try:
                outcome = await services.creation.create(
                    current,
                    environment_id,
                    submitted,
                    duplicate_of=duplicate_of,
                    credential_choice=credential_choice,
                    progress=feed,
                    template=template,
                )
            except asyncio.CancelledError:
                if progress.cancel_requested:
                    raise JobCancelled(CREATE_CANCELLED) from None
                raise
        return JobResult(
            f"Created {outcome.name or outcome.short_id}.",
            container_id=outcome.container_id,
            flags=created_flags(outcome),
        )

    reference = submitted.get("image", "").strip()[:REFERENCE_MAX]
    return engine.image_jobs.start("container_create", principal, environment_id, reference, run)


def start_container_recreate_job(
    app: FastAPI,
    principal: Principal,
    environment_id: int,
    container_id: str,
    *,
    expected_name: str,
    credential_choice: CredentialChoice | None,
) -> str:
    """Start a recreate that pulls in the background and return its id; TooManyJobs at the limits.

    The recreate validates and authorises again on a session of its own, with the name the
    caller confirmed.
    """
    engine = _engine_of(app)
    request_id = request_id_var.get()

    async def run(progress: JobProgress) -> JobResult:
        request_id_var.set(request_id)
        async with (
            _guarded(app, principal, progress) as (current, feed),
            app_container_services(app) as services,
        ):
            try:
                outcome = await services.recreation.recreate(
                    current,
                    environment_id,
                    container_id,
                    expected_name=expected_name,
                    pull=True,
                    credential_choice=credential_choice,
                    progress=feed,
                )
            except asyncio.CancelledError:
                if progress.cancel_requested:
                    raise JobCancelled(RECREATE_CANCELLED) from None
                raise
        return JobResult(
            f"Recreated {outcome.name}.",
            container_id=outcome.new_id,
            flags=recreated_flags(outcome),
        )

    return engine.image_jobs.start(
        "container_recreate", principal, environment_id, expected_name[:REFERENCE_MAX], run
    )
