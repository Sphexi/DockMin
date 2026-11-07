"""Backup pages: the on-demand download and the record of archives produced."""

import re
from typing import Annotated, Any, Final

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.types import Receive, Scope, Send

from app.auth.dependencies import CurrentAdministrator, require_administrator
from app.config import Settings
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.services.assembly import Backups, TargetAdmin
from app.services.backups import BackupFailed, DownloadStream, ProductionBusy
from app.web.navigation import no_store
from app.web.object_pages import Failure, problem_page, route
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(prefix="/backups", dependencies=[Depends(require_administrator)])

TABLES: Final = "backups/_table.html"
FIELD_MAX: Final = 512
RETRY_SECONDS: Final = "30"
SAFE_FILE_NAME: Final = re.compile(r"[A-Za-z0-9._-]{1,128}")
FALLBACK_FILE_NAME: Final = "dockmin-backup.tar.gz"
COLUMNS: Final = (
    Column("created", "Created", width="time"),
    Column("source", "Source", width="short"),
    Column("outcome", "Outcome", width="badge"),
    Column("size", "Size", numeric=True, width="count"),
    Column("archive", "Archive SHA-256"),
    Column("key", "Key"),
    Column("schema", "Schema", numeric=True, width="count"),
)


class _ArchiveResponse(StreamingResponse):
    """An archive download that removes its work directory on every way out.

    A client that leaves before the first chunk is requested never starts the body, so the
    stream is closed here as well.
    """

    def __init__(self, stream: DownloadStream) -> None:
        self._stream = stream
        name = (
            stream.file_name if SAFE_FILE_NAME.fullmatch(stream.file_name) else FALLBACK_FILE_NAME
        )
        super().__init__(
            stream,
            media_type=stream.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Content-Length": str(stream.size),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(stream.aclose),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._stream.aclose()


def _page(request: Request, context: dict[str, Any]) -> Response:
    return no_store(render_page(request, "backups/list.html", context))


@router.get("", response_class=HTMLResponse, name="backups")
async def list_backups(
    request: Request,
    services: Backups,
    targets: TargetAdmin,
    actor: CurrentAdministrator,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=FIELD_MAX)] = None,
) -> Response:
    """Show the download button and the records of archives produced, newest first."""
    try:
        page = await services.records(actor, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    names = dict(await targets.summaries(actor))
    pager = PageView.of(page, base_path=route(request, "backups"), limit=limit, cursor=cursor)
    if is_htmx_request(request):
        return no_store(
            render_fragment(
                request,
                TABLES,
                "backups_table",
                rows=page.items,
                columns=COLUMNS,
                pager=pager,
                targets=names,
            )
        )
    settings: Settings = request.app.state.settings
    key_file = settings.managed_key_file or settings.secret_key_file
    return _page(
        request,
        {
            "rows": page.items,
            "columns": COLUMNS,
            "pager": pager,
            "targets": names,
            "key_fingerprint": request.app.state.vault.key_fingerprint(),
            "key_source": settings.key_source,
            "key_file": str(key_file) if key_file is not None else None,
        },
    )


@router.post("/download", name="backup_download")
async def download_backup(
    request: Request, services: Backups, actor: CurrentAdministrator
) -> Response:
    """Produce an archive and stream it; the response is the file."""
    back = {"back_href": route(request, "backups"), "back_label": "Back to backups"}
    try:
        stream = await services.download(actor)
    except ProductionBusy as busy:
        refused = problem_page(
            request,
            heading="Download a backup",
            failure=Failure(429, "The backup was not started", str(busy)),
            **back,
            region=None,
        )
        refused.headers["Retry-After"] = RETRY_SECONDS
        return refused
    except BackupFailed as failed:
        return problem_page(
            request,
            heading="Download a backup",
            failure=Failure(503, "The backup could not be made", str(failed)),
            **back,
            region=None,
        )
    return _ArchiveResponse(stream)
