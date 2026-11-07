"""Dismiss and restore unmanaged Compose projects and Swarm namespaces in the stack lists."""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.services.assembly import Environments, Stacks, Swarm
from app.services.stack_deploy import ChangedSinceReview
from app.services.stack_dismissal_assembly import StackDismissals
from app.services.stack_dismissal_views import DISMISSIBLE_NAME
from app.services.stack_dismissals import MAX_DISMISSED_OBJECTS, NotLeftoversOnly, TooManyObjects
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.object_pages import DigestField, Failure, redirect, route, unavailable_page
from app.web.stack_routes import UNAVAILABLE as COMPOSE_UNAVAILABLE
from app.web.stack_routes import list_response as compose_list_response
from app.web.swarm_routes import UNAVAILABLE as SWARM_UNAVAILABLE
from app.web.swarm_routes import swarm_path, swarm_router
from app.web.swarm_stack_routes import list_response as swarm_list_response

__all__ = ["router", "swarm"]

router = APIRouter(
    prefix="/environments/{environment_id}/stacks",
    tags=["stacks"],
    dependencies=[Depends(require_user)],
)
swarm = swarm_router()

SEGMENT_MAX: Final = 64
NOTICES: Final = {
    "dismissed": "Entry dismissed. It stays out of the list until dismissed entries are shown.",
    "restored": "Entry restored. It is in the list again.",
}
REFUSALS: Final = {
    "not_leftovers_only": Failure(
        409,
        "The entry still has containers or services",
        "Only an entry that holds nothing but leftover objects can be dismissed.",
    ),
    "changed_since_review": Failure(
        409,
        "The entry changed",
        "Its objects are not the ones you reviewed. Review the entry again.",
    ),
    "too_many_objects": Failure(
        400,
        "The entry holds too many objects",
        f"An entry that holds more than {MAX_DISMISSED_OBJECTS} objects cannot be dismissed.",
    ),
}


def _dismissible(name: str) -> str:
    if DISMISSIBLE_NAME.fullmatch(name) is None:
        raise ResourceNotFound("not found")
    return name


def _dismissible_project(project: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    return _dismissible(project)


def _dismissible_namespace(namespace: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    return _dismissible(namespace)


DismissibleProject = Annotated[str, Depends(_dismissible_project)]
DismissibleNamespace = Annotated[str, Depends(_dismissible_namespace)]


def _administrators_only(actor: Principal) -> None:
    if not actor.is_administrator:
        raise ResourceNotFound("not found")


def _list_path(request: Request, environment_id: int) -> str:
    return route(request, "environment_stack_list", environment_id=environment_id)


@router.post(
    "/-/unmanaged/{project}/dismiss",
    response_class=HTMLResponse,
    name="environment_stack_unmanaged_dismiss",
)
async def dismiss_unmanaged(
    project: DismissibleProject,
    request: Request,
    stacks: Stacks,
    dismissals: StackDismissals,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    expected_objects: DigestField = "",
) -> Response:
    """Hide an unmanaged project that holds only leftovers from the list; administrators only."""
    _administrators_only(actor)
    try:
        await dismissals.dismiss(actor, environment_id, "compose", project, expected_objects)
    except COMPOSE_UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Dismiss entry",
            unavailable,
            _list_path(request, environment_id),
            "Back to stacks",
            region=None,
        )
    except (NotLeftoversOnly, ChangedSinceReview, TooManyObjects) as refused:
        return await compose_list_response(
            request,
            stacks,
            environments,
            actor,
            environment_id,
            refusal=REFUSALS[refused.reason],
        )
    return redirect(
        request, _list_path(request, environment_id), [Flash("ok", NOTICES["dismissed"])]
    )


@router.post(
    "/-/unmanaged/{project}/restore",
    response_class=HTMLResponse,
    name="environment_stack_unmanaged_restore",
)
async def restore_unmanaged(
    project: DismissibleProject,
    request: Request,
    dismissals: StackDismissals,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show a dismissed unmanaged project in the list again; administrators only."""
    _administrators_only(actor)
    await dismissals.clear(actor, environment_id, "compose", project)
    return redirect(
        request, _list_path(request, environment_id), [Flash("ok", NOTICES["restored"])]
    )


@swarm.post(
    "/stacks/-/unmanaged/{namespace}/dismiss",
    response_class=HTMLResponse,
    name="environment_swarm_stack_unmanaged_dismiss",
)
async def dismiss_unmanaged_namespace(
    namespace: DismissibleNamespace,
    request: Request,
    cluster: Swarm,
    stacks: Stacks,
    dismissals: StackDismissals,
    environments: Environments,
    actor: CurrentUser,
    environment_id: PathId,
    expected_objects: DigestField = "",
) -> Response:
    """Hide an unmanaged namespace that holds only leftovers from the list; administrators only."""
    _administrators_only(actor)
    list_href = swarm_path(request, "stack_list", environment_id=environment_id)
    try:
        await dismissals.dismiss(actor, environment_id, "swarm", namespace, expected_objects)
    except SWARM_UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Dismiss entry",
            unavailable,
            list_href,
            "Back to Swarm stacks",
            region=None,
        )
    except (NotLeftoversOnly, ChangedSinceReview, TooManyObjects) as refused:
        return await swarm_list_response(
            request,
            cluster,
            stacks,
            environments,
            actor,
            environment_id,
            refusal=REFUSALS[refused.reason],
        )
    return redirect(request, list_href, [Flash("ok", NOTICES["dismissed"])])


@swarm.post(
    "/stacks/-/unmanaged/{namespace}/restore",
    response_class=HTMLResponse,
    name="environment_swarm_stack_unmanaged_restore",
)
async def restore_unmanaged_namespace(
    namespace: DismissibleNamespace,
    request: Request,
    dismissals: StackDismissals,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show a dismissed unmanaged namespace in the list again; administrators only."""
    _administrators_only(actor)
    await dismissals.clear(actor, environment_id, "swarm", namespace)
    return redirect(
        request,
        swarm_path(request, "stack_list", environment_id=environment_id),
        [Flash("ok", NOTICES["restored"])],
    )
