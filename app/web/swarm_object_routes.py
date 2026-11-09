"""Swarm config and secret pages."""

from collections.abc import Mapping
from typing import Annotated, Any, Final

from fastapi import Form, Query, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import UploadFile

from app.auth.dependencies import CurrentUser
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.services.assembly import (
    Environments,
    Swarm,
    SwarmServices,
)
from app.services.host_objects import (
    DEFAULT_PAGE_SIZE,
)
from app.services.object_locks import ObjectBusy
from app.services.swarm_configs import (
    CONTENT_FIELD,
    MAX_CONFIG_BYTES,
    MAX_SECRET_BYTES,
    VALUE_FIELD,
    ObjectKind,
    SwarmConfigActions,
    SwarmConfigQueries,
    SwarmObjectChangedSinceReview,
    SwarmObjectFilters,
    SwarmObjectInUse,
    SwarmObjectInvalid,
    SwarmObjectNameInUse,
    SwarmObjectRefused,
    SwarmSecretActions,
    SwarmSecretQueries,
    redisplay,
)
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.object_pages import (
    ACCESS_REMOVED,
    ACCESS_SAVED,
    CURSOR_MAX,
    ROW_CAP,
    AccessData,
    DigestField,
    Failure,
    Field,
    Filter,
    Region,
    apply_access,
    bind_digest,
    blank_to_none,
    fragment_response,
    page_response,
    problem_page,
    query_of,
    read_form,
    redirect,
    remove_access,
    row_count,
    self_href,
    unavailable_page,
)
from app.web.origins import RequestOrigin
from app.web.pagination import Column, PageView
from app.web.problems import unreachable_copy
from app.web.sorting import DirectionParam, SortParam, view_sort
from app.web.stack_links import stack_links
from app.web.swarm_routes import (
    ACTION_ERRORS,
    UNAVAILABLE,
    Banner,
    ObjectId,
    failure_of,
    notice_flash,
    removal_flash,
    swarm_path,
    swarm_router,
    whole_number,
)
from app.web.templating import is_htmx_request

OBJECT_TABS: Final = ("overview", "access")
OBJECT_PLURALS: Final[Mapping[str, str]] = {"config": "configs", "secret": "secrets"}

router = swarm_router()


OBJECT_TABLES: Final = "swarm/objects/_tables.html"
OBJECT_DETAIL: Final = "swarm/objects/_detail.html"
UPLOAD_FIELD: Final = "upload"
OBJECT_COLUMNS: Final = (
    Column("name", "Name", sortable=True),
    Column("stack", "Stack", sortable=True, optional=True),
    Column("created", "Created", sortable=True, optional=True, width="time"),
    Column("actions", "Actions", width="actions"),
)


def _queries(swarm: SwarmServices, kind: ObjectKind) -> SwarmConfigQueries | SwarmSecretQueries:
    return swarm.configs if kind == "config" else swarm.secrets


def _object_actions(
    swarm: SwarmServices, kind: ObjectKind
) -> SwarmConfigActions | SwarmSecretActions:
    return swarm.config_actions if kind == "config" else swarm.secret_actions


def _object_name(request: Request, kind: ObjectKind, name: str, /, **params: Any) -> str:
    return swarm_path(request, f"{kind}_{name}", **params)


async def _read_object_form(
    request: Request, kind: ObjectKind
) -> tuple[dict[str, str], bytes | None]:
    """Read the create form and the one uploaded file, taking at most the limit and one byte."""
    field = CONTENT_FIELD if kind == "config" else VALUE_FIELD
    values = await read_form(request, frozenset({"name", field, "rows"}))
    upload = (await request.form()).get(UPLOAD_FIELD)
    if isinstance(upload, UploadFile) and upload.filename:
        limit = MAX_CONFIG_BYTES if kind == "config" else MAX_SECRET_BYTES
        return values, await upload.read(limit + 1)
    return values, None


def _object_failure(
    error: Exception, swarm: SwarmServices, actor: Principal, kind: ObjectKind
) -> Failure:
    if isinstance(error, SwarmObjectInUse):
        return Failure(
            409,
            f"This {kind} is in use",
            f"{error.services} services use this {kind}. Remove it from them first.",
        )
    return failure_of(error, swarm, actor)


def _register_objects(kind: ObjectKind) -> None:
    """Register the list, create, detail, remove and access routes of one object kind."""
    plural = OBJECT_PLURALS[kind]
    title = kind.capitalize()

    def named(name: str) -> str:
        return f"environment_swarm_{kind}_{name}"

    @router.get(f"/{plural}", response_class=HTMLResponse, name=named("list"))
    async def list_objects(
        request: Request,
        swarm: Swarm,
        environments: Environments,
        actor: CurrentUser,
        environment_id: PathId,
        q: Filter = None,
        stack: Filter = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(max_length=CURSOR_MAX)] = None,
        sort: SortParam = None,
        direction: DirectionParam = None,
    ) -> Response:
        """List the objects the caller may see, read now."""
        environment = (await environments.environments.get(actor, environment_id)).summary
        filters = SwarmObjectFilters(blank_to_none(q), blank_to_none(stack))
        rows: list[Any] = []
        total = visible = 0
        can_create = False
        errors: tuple[str, ...] = ()
        refused: dict[str, str] = {}
        failure: str | None = None
        status_code = 200
        next_cursor = None
        try:
            result = await _queries(swarm, kind).page(
                actor,
                environment_id,
                filters,
                limit=limit,
                cursor=cursor,
                sort=sort,
                direction=direction,
            )
        except ValidationFailed as invalid:
            errors, status_code, refused = tuple(invalid.fields.values()), 400, invalid.fields
        except UNAVAILABLE as unavailable:
            failure, status_code = str(unavailable), 503
        else:
            rows = list(result.page.items)
            total, visible, can_create = result.total, result.visible, result.can_create
            next_cursor = result.page.next_cursor
        params = [
            (key, value) for key, value in (("q", filters.q), ("stack", filters.stack)) if value
        ]
        view_key, view_direction = view_sort(sort, direction, refused)
        pager = PageView(
            base_path=_object_name(request, kind, "list", environment_id=environment_id),
            limit=limit,
            item_count=len(rows),
            cursor=None if errors or failure else cursor,
            next_cursor=next_cursor,
            sort=view_key,
            direction=view_direction,
            params=tuple(params),
        )
        results: dict[str, Any] = {
            "kind": kind,
            "rows": rows,
            "columns": OBJECT_COLUMNS,
            "pager": pager,
            "total": total,
            "visible": visible,
            "filtered": bool(params),
            "errors": errors,
            "failure": failure,
            "retry_href": self_href(request),
            "stack_links": await stack_links(
                request, actor, environment_id, (("swarm", row.stack) for row in rows)
            ),
        }
        if is_htmx_request(request):
            return fragment_response(
                request, OBJECT_TABLES, "objects_results", status_code, oob=True, **results
            )
        return page_response(
            request,
            "swarm/objects/list.html",
            {
                **results,
                "environment": environment,
                "q": filters.q or "",
                "stack": filters.stack or "",
                "can_create": can_create,
            },
            status_code,
        )

    async def form_page(
        request: Request,
        swarm: SwarmServices,
        environments: Environments,
        actor: Principal,
        environment_id: int,
        values: Mapping[str, str],
        errors: Mapping[str, str],
        *,
        status_code: int = 200,
        banner: Banner | None = None,
        adding: str = "",
        reenter: bool = False,
    ) -> Response:
        environment = (await environments.environments.get(actor, environment_id)).summary
        try:
            context = await _queries(swarm, kind).create_context(actor, environment_id)
        except UNAVAILABLE as unavailable:
            return unavailable_page(
                request,
                title,
                unavailable,
                _object_name(request, kind, "list", environment_id=environment_id),
                f"Back to {plural}",
                region=None,
            )
        shown = redisplay(kind, values)
        return page_response(
            request,
            "swarm/objects/new.html",
            {
                "kind": kind,
                "environment": environment,
                "context": context,
                "values": shown,
                "errors": errors,
                "banner": banner,
                "reenter": reenter,
                "label_count": row_count(shown, "label", add=adding == "label"),
                "row_cap": ROW_CAP,
                "max_kib": context.max_bytes // 1024,
                "submit_href": _object_name(request, kind, "create", environment_id=environment_id),
                "list_href": _object_name(request, kind, "list", environment_id=environment_id),
            },
            status_code,
        )

    @router.get(f"/{plural}/new", response_class=HTMLResponse, name=named("new"))
    async def new_object(
        request: Request,
        swarm: Swarm,
        environments: Environments,
        actor: CurrentUser,
        environment_id: PathId,
    ) -> Response:
        """Show the create form; create access is needed."""
        return await form_page(request, swarm, environments, actor, environment_id, {}, {})

    @router.post(f"/{plural}/new", response_class=HTMLResponse, name=named("create"))
    async def create_object(
        request: Request,
        swarm: Swarm,
        environments: Environments,
        actor: CurrentUser,
        environment_id: PathId,
    ) -> Response:
        """Create the object, or show the form again; a secret's value is never put back."""
        values, uploaded = await _read_object_form(request, kind)

        async def again(
            errors: Mapping[str, str], status_code: int, banner: Banner | None = None
        ) -> Response:
            return await form_page(
                request,
                swarm,
                environments,
                actor,
                environment_id,
                values,
                errors,
                status_code=status_code,
                banner=banner,
                adding=values.get("rows", ""),
                reenter=kind == "secret" and bool(values.get(VALUE_FIELD) or uploaded),
            )

        if values.get("rows") == "label":
            return await again({}, 200)
        try:
            created = await _object_actions(swarm, kind).create(
                actor, environment_id, values, upload=uploaded
            )
        except SwarmObjectNameInUse as taken:
            return await again(taken.fields, 409)
        except SwarmObjectInvalid as invalid:
            return await again(invalid.fields, 413 if invalid.too_large else 400)
        except ValidationFailed as invalid:
            return await again(invalid.fields, 400)
        except SwarmObjectRefused as refused:
            return await again({}, 502, Banner(f"Docker refused this {kind}", str(refused)))
        except UNAVAILABLE as unavailable:
            return await again({}, 503, Banner(*unreachable_copy(unavailable)))
        notice = "created" if created.control_created else "created_uncontrolled"
        return redirect(
            request,
            _object_name(
                request, kind, "detail", environment_id=environment_id, object_id=created.id
            ),
            [notice_flash(notice)],
        )

    async def detail_response(
        request: Request,
        swarm: SwarmServices,
        actor: Principal,
        environment_id: int,
        object_id: str,
        tab: str,
        *,
        status_code: int = 200,
        values: dict[str, Any] | None = None,
        fields: dict[str, str] | None = None,
    ) -> Response:
        if tab not in OBJECT_TABS:
            raise ResourceNotFound("not found")
        queries = _queries(swarm, kind)
        try:
            detail = await queries.detail(actor, environment_id, object_id)
            panel = None
            if tab == "access":
                if not detail.can_manage_access:
                    raise PermissionDenied("administrator role required")
                panel = await queries.access_panel(actor, environment_id, object_id)
        except UNAVAILABLE as unavailable:
            return unavailable_page(
                request,
                title,
                unavailable,
                _object_name(request, kind, "list", environment_id=environment_id),
                f"Back to {plural}",
                region=Region(f"{kind}-tabs", "outerHTML"),
            )
        tabs = [name for name in OBJECT_TABS if detail.can_manage_access or name != "access"]
        arguments: dict[str, Any] = {
            "kind": kind,
            "detail": detail,
            "tab": tab,
            "tabs": tabs,
            "panel": bind_digest(panel, values),
            "values": values,
            "fields": fields or {},
            "stack_links": (
                await stack_links(request, actor, environment_id, [("swarm", detail.stack)])
                if tab == "overview"
                else {}
            ),
        }
        if is_htmx_request(request):
            return fragment_response(
                request, OBJECT_DETAIL, "object_tabs", status_code, **arguments
            )
        return page_response(
            request,
            "swarm/objects/detail.html",
            {
                **arguments,
                "list_href": _object_name(request, kind, "list", environment_id=environment_id),
            },
            status_code,
        )

    @router.get(
        f"/{plural}/{{object_id}}",
        response_class=HTMLResponse,
        name=named("detail"),
        dependencies=[RequestOrigin],
    )
    async def object_detail(
        object_id: ObjectId,
        request: Request,
        swarm: Swarm,
        actor: CurrentUser,
        environment_id: PathId,
        tab: Annotated[str, Query(max_length=32)] = "overview",
    ) -> Response:
        """Show one object, one tab at a time; a secret's value is never part of it."""
        return await detail_response(request, swarm, actor, environment_id, object_id, tab)

    async def remove_page(
        request: Request,
        swarm: SwarmServices,
        actor: Principal,
        environment_id: int,
        object_id: str,
        *,
        status_code: int = 200,
        banner: Failure | None = None,
    ) -> Response:
        detail_href = _object_name(
            request, kind, "detail", environment_id=environment_id, object_id=object_id
        )
        try:
            impact = await _queries(swarm, kind).remove_impact(actor, environment_id, object_id)
        except UNAVAILABLE as unavailable:
            return unavailable_page(
                request, f"Remove {kind}", unavailable, detail_href, "Back", region=None
            )
        return page_response(
            request,
            "swarm/objects/remove_confirm.html",
            {
                "kind": kind,
                "impact": impact,
                "banner": banner,
                "detail_href": detail_href,
                "service_base": swarm_path(request, "service_list", environment_id=environment_id),
                "run_href": _object_name(
                    request, kind, "remove_run", environment_id=environment_id, object_id=object_id
                ),
            },
            status_code,
        )

    @router.get(
        f"/{plural}/{{object_id}}/remove", response_class=HTMLResponse, name=named("remove")
    )
    async def remove_confirm(
        object_id: ObjectId,
        request: Request,
        swarm: Swarm,
        actor: CurrentUser,
        environment_id: PathId,
    ) -> Response:
        """Name the object and the services that use it; write holders only."""
        return await remove_page(request, swarm, actor, environment_id, object_id)

    @router.post(
        f"/{plural}/{{object_id}}/remove", response_class=HTMLResponse, name=named("remove_run")
    )
    async def run_remove(
        object_id: ObjectId,
        request: Request,
        swarm: Swarm,
        actor: CurrentUser,
        environment_id: PathId,
        expected_name: Field = "",
        expected_in_use: Annotated[str, Form(max_length=8)] = "",
    ) -> Response:
        """Remove the reviewed object; it is refused while any service references it."""
        detail_href = _object_name(
            request, kind, "detail", environment_id=environment_id, object_id=object_id
        )
        in_use = whole_number(expected_in_use, 6)
        try:
            if in_use is None or not expected_name:
                raise ValidationFailed({kind: "The confirmation was incomplete. Review it again."})
            await _object_actions(swarm, kind).remove(
                actor,
                environment_id,
                object_id,
                expected_name=expected_name,
                expected_in_use=in_use,
            )
        except (SwarmObjectInUse, SwarmObjectChangedSinceReview, ObjectBusy) as error:
            failure = _object_failure(error, swarm, actor, kind)
            return await remove_page(
                request,
                swarm,
                actor,
                environment_id,
                object_id,
                status_code=failure.status,
                banner=failure,
            )
        except ACTION_ERRORS as error:
            return problem_page(
                request,
                heading=f"Remove {kind}",
                failure=_object_failure(error, swarm, actor, kind),
                back_href=detail_href,
                back_label=f"Back to the {kind}",
                review_href=_object_name(
                    request, kind, "remove", environment_id=environment_id, object_id=object_id
                ),
                region=None,
            )
        return redirect(
            request,
            _object_name(request, kind, "list", environment_id=environment_id),
            [removal_flash(f"{kind}_removed")],
        )

    def access_href(request: Request, environment_id: int, object_id: str) -> str:
        base = _object_name(
            request, kind, "detail", environment_id=environment_id, object_id=object_id
        )
        return f"{base}?{query_of('tab', 'access')}"

    @router.post(
        f"/{plural}/{{object_id}}/access", response_class=HTMLResponse, name=named("access_save")
    )
    async def save_object_access(
        object_id: ObjectId,
        request: Request,
        swarm: Swarm,
        actor: CurrentUser,
        environment_id: PathId,
        data: AccessData,
    ) -> Response:
        """Create or replace the resource control over one object; administrators only."""
        try:
            panel = await _queries(swarm, kind).access_panel(actor, environment_id, object_id)
        except UNAVAILABLE as unavailable:
            return unavailable_page(
                request,
                title,
                unavailable,
                _object_name(request, kind, "list", environment_id=environment_id),
                f"Back to {plural}",
                region=None,
            )
        refusal = await apply_access(actor, swarm.access.controls, swarm.services, panel, data)
        if refusal is None:
            return redirect(
                request,
                access_href(request, environment_id, object_id),
                [Flash("ok", ACCESS_SAVED)],
            )
        return await detail_response(
            request,
            swarm,
            actor,
            environment_id,
            object_id,
            "access",
            status_code=refusal.status_code,
            values=refusal.values,
            fields=refusal.fields,
        )

    @router.post(
        f"/{plural}/{{object_id}}/access/delete",
        response_class=HTMLResponse,
        name=named("access_delete"),
    )
    async def delete_object_access(
        object_id: ObjectId,
        request: Request,
        swarm: Swarm,
        actor: CurrentUser,
        environment_id: PathId,
        expected_digest: DigestField = "",
    ) -> Response:
        """Remove the resource control over one object; administrators only."""
        try:
            panel = await _queries(swarm, kind).access_panel(actor, environment_id, object_id)
        except UNAVAILABLE as unavailable:
            return unavailable_page(
                request,
                title,
                unavailable,
                _object_name(request, kind, "list", environment_id=environment_id),
                f"Back to {plural}",
                region=None,
            )
        refusal = await remove_access(actor, swarm.access.controls, panel, expected_digest)
        if refusal is None:
            return redirect(
                request,
                access_href(request, environment_id, object_id),
                [Flash("ok", ACCESS_REMOVED)],
            )
        return await detail_response(
            request,
            swarm,
            actor,
            environment_id,
            object_id,
            "access",
            status_code=refusal.status_code,
            fields=refusal.fields,
        )


_register_objects("config")
_register_objects("secret")
