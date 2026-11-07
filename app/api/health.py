"""Unauthenticated liveness and readiness endpoints."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import Settings
from app.db import Database
from app.db.repositories.readiness import schema_is_readable
from app.logging import get_logger

ReadinessCheck = Callable[[], Awaitable[bool]]

router = APIRouter(prefix="/api", tags=["health"])
logger = get_logger(__name__)


def data_dir_check(settings: Settings) -> ReadinessCheck:
    """Build a check that the data directory exists and is writable."""

    def probe(path: Path) -> bool:
        return path.is_dir() and os.access(path, os.W_OK)

    async def check() -> bool:
        return await asyncio.to_thread(probe, settings.data_dir)

    return check


def database_check(database: Database) -> ReadinessCheck:
    """Build a check that a fresh connection opens and answers a query."""

    async def check() -> bool:
        async with database.session() as session:
            return await schema_is_readable(session)

    return check


async def _run(name: str, check: ReadinessCheck) -> bool:
    try:
        return await check()
    except Exception:
        # A failing check must report "down", never take the endpoint with it.
        logger.exception("readiness_check_failed", extra={"check": name})
        return False


@router.get("/health")
async def health() -> dict[str, str]:
    """Report that the process is serving requests."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Report per-check up/down status; 503 when any check is down."""
    checks: dict[str, ReadinessCheck] = request.app.state.readiness_checks
    names = list(checks)
    outcomes = await asyncio.gather(*(_run(name, checks[name]) for name in names))
    results = {name: "up" if ok else "down" for name, ok in zip(names, outcomes, strict=True)}
    all_up = all(outcomes)
    return JSONResponse(
        {"status": "ready" if all_up else "not_ready", "checks": results},
        status_code=200 if all_up else 503,
    )
