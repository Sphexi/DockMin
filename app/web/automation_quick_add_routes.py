"""Quick add: the page that confirms adding an object to an automation, and the post that does."""

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, get_activity, get_runtime, require_user
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.db.engine import Session, get_session
from app.services.access import is_canonical_id
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    AccessServices,
    Jobs,
    get_access_services,
    get_container_services,
    get_engine_runtime,
    get_object_inspector,
    get_stack_services,
    get_swarm_services,
)
from app.services.authorisation import ObjectInspector
from app.services.automation_quick_add import (
    OBJECT_WORDS,
    STACK_KINDS,
    ObjectKind,
    QuickAddObject,
)
from app.services.engine_setup import EngineRuntime
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
)
from app.services.stack_document import STACK_NAME, SWARM_STACK_NAME
from app.web.automation_routes import (
    FIELD_MAX,
    RETURN_MAX,
    _return_href,
    _rule_href,
    _target_kind,
    parameter_rows,
    rule_id_of,
)
from app.web.flash import Flash
from app.web.job_routes import html_page
from app.web.object_pages import Failure, problem_page, query_of, redirect, route

router = APIRouter(prefix="/automation", dependencies=[Depends(require_user)])

CONFIRM_TEMPLATE: Final = "automation/quick_add_confirm.html"
HEADING: Final = "Add to automation"
UNAVAILABLE: Final = (EnvironmentUnavailable, ObjectTooLarge)
NOT_ADDED: Final = "The object was not added"
Text = Annotated[str, Form(max_length=FIELD_MAX)]


@dataclass(frozen=True, slots=True)
class ObjectReader:
    """Resolves an object under the viewer's read access through the pages' own read paths."""

    request: Request
    db: Session
    runtime: AuthRuntime
    engine: EngineRuntime
    access: AccessServices
    inspector: EngineObjectInspector
    activity: ActivityRecorder

    async def read(
        self, actor: Principal, environment_id: int, kind: ObjectKind, key: str
    ) -> QuickAddObject:
        """Return the object as the host reports it; ResourceNotFound unless readable."""
        arguments = (
            self.request,
            self.db,
            self.runtime,
            self.engine,
            self.access,
            self.inspector,
            self.activity,
        )
        match kind:
            case "container":
                containers = await get_container_services(*arguments)
                detail = await containers.queries.detail(actor, environment_id, key)
                labels = {label.key: label.value for label in detail.labels}
                return QuickAddObject(environment_id, kind, detail.id, detail.name, labels)
            case "stack":
                stacks = await get_stack_services(*arguments)
                name = await stacks.queries.origin_name(actor, environment_id, key)
                return QuickAddObject(environment_id, kind, name, name)
            case "swarm_stack":
                swarm = await get_swarm_services(*arguments)
                managed = await swarm.stacks.managed_detail(actor, environment_id, key)
                if managed is None:
                    raise ResourceNotFound("not found")
                return QuickAddObject(environment_id, kind, managed.stack.name, managed.stack.name)
            case "service":
                swarm = await get_swarm_services(*arguments)
                name = await swarm.services.origin_name(actor, environment_id, key)
                return QuickAddObject(environment_id, kind, key, name)


async def get_object_reader(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    engine: Annotated[EngineRuntime, Depends(get_engine_runtime)],
    access: Annotated[AccessServices, Depends(get_access_services)],
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
    activity: Annotated[ActivityRecorder, Depends(get_activity)],
) -> ObjectReader:
    """Provide the reader that resolves the object a quick add names."""
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    return ObjectReader(request, db, runtime, engine, access, inspector, activity)


Reader = Annotated[ObjectReader, Depends(get_object_reader)]


def parse_object(text: str) -> tuple[ObjectKind, str]:
    """Read `container:<id>`, `stack:<name>`, `swarm_stack:<name>` or `service:<id>`; else 404."""
    kind, _, value = text.partition(":")
    if len(text) <= RETURN_MAX:
        if kind == "container" and is_canonical_id("container", value):
            return "container", value
        if kind == "service" and is_canonical_id("service", value):
            return "service", value
        if kind == "stack" and STACK_NAME.fullmatch(value):
            return "stack", value
        if kind == "swarm_stack" and SWARM_STACK_NAME.fullmatch(value):
            return "swarm_stack", value
    raise HTTPException(status_code=404)


def _confirm_href(request: Request, rule_id: int, object_ref: str) -> str:
    return (
        f"{route(request, 'automation_rule_quick_add_confirm')}"
        f"?{query_of('rule_id', str(rule_id))}&{query_of('object', object_ref)}"
    )


def _refused(
    request: Request,
    rule_id: int,
    back_href: str | None,
    object_ref: str,
    refusal: ValidationFailed | PermissionDenied | EnvironmentUnavailable | ObjectTooLarge,
) -> Response:
    if isinstance(refusal, ValidationFailed):
        status, message = 422, " ".join(refusal.fields.values())
    elif isinstance(refusal, PermissionDenied):
        status, message = 403, str(refusal)
    else:
        status, message = 503, str(refusal)
    return problem_page(
        request,
        heading=HEADING,
        failure=Failure(status, NOT_ADDED, message),
        back_href=back_href or _rule_href(request, rule_id),
        back_label="Back to the automation" if back_href is None else "Back to the Automation tab",
        region=None,
        review_href=_confirm_href(request, rule_id, object_ref),
    )


@router.get(
    "/rules/-/quick-add",
    response_class=HTMLResponse,
    name="automation_rule_quick_add_confirm",
)
async def confirm_quick_add(
    request: Request,
    services: Jobs,
    reader: Reader,
    actor: CurrentUser,
    rule_id: str = "",
    object_ref: Annotated[str, Query(alias="object")] = "",
) -> Response:
    """Name the automation, the object and the targets before and after; nothing changes."""
    number = rule_id_of(rule_id)
    kind, key = parse_object(object_ref)
    rule = await services.rules.get(actor, number)
    if rule.environment_id is None:
        raise ResourceNotFound("not found")
    back_href = _return_href(request, rule, object_ref)
    try:
        obj = await reader.read(actor, rule.environment_id, kind, key)
        preview = await services.rules.quick_add_preview(actor, number, obj)
    except UNAVAILABLE as unavailable:
        return _refused(request, number, back_href, object_ref, unavailable)
    return html_page(
        request,
        CONFIRM_TEMPLATE,
        {
            "view": preview.view,
            "parameters": parameter_rows(preview.view.rule),
            "object": obj,
            "object_ref": object_ref,
            "object_word": OBJECT_WORDS[kind],
            "names_before": preview.names_before,
            "names_after": preview.names_after,
            "target": preview.target,
            "target_kind": _target_kind(preview.view.rule),
            "stack_kind": STACK_KINDS.get(kind),
            "refusal": preview.refusal,
            "digest": preview.digest,
            "owner_is_other": preview.owner_is_other,
            "owner_standing": preview.owner_standing,
            "released_holds": preview.released_holds,
            "back_href": back_href or _rule_href(request, number),
        },
    )


@router.post("/rules/-/quick-add", name="automation_rule_quick_add")
async def quick_add(
    request: Request,
    services: Jobs,
    reader: Reader,
    actor: CurrentUser,
    rule_id: Text = "",
    object_ref: Annotated[str, Form(alias="object", max_length=FIELD_MAX)] = "",
    expected_updated_at: Text = "",
    expected_digest: Text = "",
    expected_name: Text = "",
) -> Response:
    """Add the object's name to the automation as it was reviewed; the automation stays enabled."""
    number = rule_id_of(rule_id)
    kind, key = parse_object(object_ref)
    rule = await services.rules.get(actor, number)
    if rule.environment_id is None:
        raise ResourceNotFound("not found")
    back_href = _return_href(request, rule, object_ref)
    try:
        obj = await reader.read(actor, rule.environment_id, kind, key)
        await services.rules.quick_add(
            actor,
            number,
            obj,
            expected_updated_at=expected_updated_at,
            expected_digest=expected_digest,
            expected_name=expected_name,
        )
    except (ValidationFailed, PermissionDenied, *UNAVAILABLE) as refusal:
        return _refused(request, number, back_href, object_ref, refusal)
    return redirect(
        request,
        back_href or _rule_href(request, number),
        [Flash("ok", f"{obj.name} is added to {rule.name}. The automation stays enabled.")],
    )
