"""Swarm pages shared by every area: gate, error mapping, notices and overview."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.assembly import (
    Access,
    Swarm,
    SwarmServices,
    get_object_inspector,
)
from app.services.authorisation import ObjectInspector
from app.services.container_actions import EngineRefused
from app.services.host_objects import (
    NetworkListTooLarge,
    ServiceListTooLarge,
    VolumeListTooLarge,
)
from app.services.object_inspector import (
    SWARM_OBJECT_ID,
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
)
from app.services.object_locks import ObjectBusy
from app.services.pull_credentials import (
    BindingCredentialMissing,
    CredentialChoiceRequired,
)
from app.services.registry_resolution import CredentialSetUnavailable
from app.services.swarm import (
    NodeChangedSinceReview,
    NodeGone,
    NodeRefused,
    SwarmListTooLarge,
)
from app.services.swarm_configs import (
    SwarmObjectChangedSinceReview,
    SwarmObjectGone,
    SwarmObjectInUse,
    SwarmObjectListTooLarge,
    SwarmObjectNameInUse,
    SwarmObjectRefused,
)
from app.services.swarm_gate import require_swarm
from app.services.swarm_service_views import (
    NoPreviousSpec,
    NotReplicated,
    ServiceChangedSinceReview,
    ServiceGone,
    ServiceNameInUse,
    ServiceRefused,
)
from app.services.swarm_stacks import NAMESPACE_ROUTE
from app.web.flash import Flash, FlashLevel
from app.web.identifiers import PathId
from app.web.object_pages import (
    Failure,
    administrator_detail,
    busy_message,
    page_response,
    route,
    unavailable_page,
)
from app.web.problems import unreachable_copy

SEGMENT_MAX: Final = 128
MAX_DIGITS: Final = 19

NOTICES: Final[Mapping[str, tuple[FlashLevel, str]]] = {
    "created": ("ok", "Created."),
    "created_uncontrolled": (
        "warn",
        "Created, but its ownership could not be recorded. An administrator can assign access "
        "to it.",
    ),
    "created_unpinned": (
        "warn",
        "Created. The image could not be pinned to a digest, so nodes may run different images "
        "of this tag.",
    ),
    "updated": ("ok", "Service updated. Its tasks are replaced as the update rolls out."),
    "updated_unpinned": (
        "warn",
        "Service updated. The image could not be pinned to a digest, so nodes may run different "
        "images of this tag.",
    ),
    "unchanged": ("ok", "Nothing changed: the service already has this configuration."),
    "scaled": ("ok", "Replica count changed. Tasks start or stop as the manager schedules them."),
    "force_updated": ("ok", "Forced update started. Running tasks are replaced."),
    "rolled_back": ("ok", "Rollback started. The previous configuration is being restored."),
    "availability_changed": ("ok", "Availability changed."),
    "availability_unchanged": ("ok", "The node already had this availability."),
}
LIST_NOTICES: Final[Mapping[str, str]] = {
    "service_removed": "Service removed.",
    "config_removed": "Config removed.",
    "secret_removed": "Secret removed.",
}


async def require_swarm_manager(
    actor: CurrentUser,
    environment_id: PathId,
    access: Access,
    inspector: Annotated[ObjectInspector, Depends(get_object_inspector)],
) -> None:
    """Refuse with a plain 404 unless the environment is a Swarm manager the caller can see.

    Runs before any Engine call and for every role, so a standalone host, a worker and an
    environment never contacted answer exactly as a missing one does.
    """
    if not isinstance(inspector, EngineObjectInspector):
        raise RuntimeError("the Engine runtime has not been started")
    await require_swarm(access.authoriser, inspector.hosts, actor, environment_id)


def swarm_router() -> APIRouter:
    """Return a router with the Swarm prefix and the dependencies every Swarm page shares."""
    return APIRouter(
        prefix="/environments/{environment_id}/swarm",
        tags=["swarm"],
        dependencies=[Depends(require_user), Depends(require_swarm_manager)],
    )


router = swarm_router()


def _canonical(value: str) -> str:
    if SWARM_OBJECT_ID.fullmatch(value) is None:
        raise ResourceNotFound("not found")
    return value


def _service_id(service_id: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    return _canonical(service_id)


def _task_id(task_id: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    return _canonical(task_id)


def _node_id(node_id: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    return _canonical(node_id)


def _object_id(object_id: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    return _canonical(object_id)


def _namespace(namespace: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    if NAMESPACE_ROUTE.fullmatch(namespace) is None:
        raise ResourceNotFound("not found")
    return namespace


ServiceId = Annotated[str, Depends(_service_id)]
TaskId = Annotated[str, Depends(_task_id)]
NodeId = Annotated[str, Depends(_node_id)]
ObjectId = Annotated[str, Depends(_object_id)]
Namespace = Annotated[str, Depends(_namespace)]

ACTION_ERRORS: Final = (
    ObjectBusy,
    ValidationFailed,
    NotReplicated,
    NoPreviousSpec,
    SwarmObjectInUse,
    ServiceGone,
    NodeGone,
    SwarmObjectGone,
    ServiceRefused,
    NodeRefused,
    SwarmObjectRefused,
    EngineRefused,
    BindingCredentialMissing,
    CredentialSetUnavailable,
    EnvironmentUnavailable,
    SwarmListTooLarge,
    SwarmObjectListTooLarge,
    ServiceListTooLarge,
    NetworkListTooLarge,
    VolumeListTooLarge,
    ObjectTooLarge,
)
UNAVAILABLE: Final = (
    EnvironmentUnavailable,
    SwarmListTooLarge,
    SwarmObjectListTooLarge,
    ServiceListTooLarge,
    NetworkListTooLarge,
    VolumeListTooLarge,
    ObjectTooLarge,
)


def failure_of(error: Exception, swarm: SwarmServices, actor: Principal) -> Failure:
    """Map an error a Swarm action can raise to the notice the caller is shown."""
    if isinstance(error, ObjectBusy):
        busy = swarm.service_actions.describe_busy(error, actor)
        return Failure(409, "This object is busy", busy_message(busy, "it"))
    if isinstance(
        error, ServiceChangedSinceReview | SwarmObjectChangedSinceReview | NodeChangedSinceReview
    ):
        return Failure(409, "It changed since you reviewed it", "; ".join(error.fields.values()))
    if isinstance(error, ServiceNameInUse | SwarmObjectNameInUse):
        return Failure(409, "The name is already used", "; ".join(error.fields.values()))
    if isinstance(error, CredentialChoiceRequired):
        return Failure(400, "Choose a credential set", "; ".join(error.fields.values()))
    if isinstance(error, ValidationFailed):
        return Failure(400, "Check the request", "; ".join(error.fields.values()))
    if isinstance(error, NotReplicated):
        return Failure(409, "This service cannot be scaled", str(error))
    if isinstance(error, NoPreviousSpec):
        return Failure(409, "There is nothing to roll back to", str(error))
    if isinstance(error, SwarmObjectInUse):
        return Failure(409, "It is in use", str(error))
    if isinstance(error, BindingCredentialMissing | CredentialSetUnavailable):
        return Failure(409, "The credential set cannot be used", str(error))
    if isinstance(error, ServiceGone | NodeGone | SwarmObjectGone):
        return Failure(404, "It no longer exists", str(error))
    if isinstance(error, ServiceRefused | NodeRefused | SwarmObjectRefused | EngineRefused):
        return Failure(
            502, "Docker refused the action", str(error), administrator_detail(error, actor)
        )
    return Failure(503, *unreachable_copy(error))


def swarm_path(request: Request, name: str, /, **params: Any) -> str:
    return route(request, f"environment_swarm_{name}", **params)


def notice_flash(notice: str) -> Flash:
    """Return the notice a completed swarm action redirects with."""
    level, text = NOTICES[notice]
    return Flash(level, text)


def removal_flash(notice: str) -> Flash:
    """Return the notice a completed removal redirects with."""
    return Flash("ok", LIST_NOTICES[notice])


def whole_number(text: str, digits: int = MAX_DIGITS) -> int | None:
    stripped = text.strip()
    if stripped.isascii() and stripped.isdigit() and len(stripped) <= digits:
        return int(stripped)
    return None


def version_of(text: str, field: str = "service") -> int:
    number = whole_number(text)
    if number is None:
        raise ValidationFailed({field: "The confirmation was incomplete. Review it again."})
    return number


@dataclass(frozen=True, slots=True)
class Banner:
    title: str
    message: str
    detail: str | None = None


def _environment_href(request: Request, environment_id: int) -> str:
    return route(request, "environment_detail", environment_id=environment_id)


def service_detail_href(request: Request, environment_id: int, service_id: str) -> str:
    return swarm_path(
        request, "service_detail", environment_id=environment_id, service_id=service_id
    )


def service_list_href(request: Request, environment_id: int) -> str:
    return swarm_path(request, "service_list", environment_id=environment_id)


@router.get("", response_class=HTMLResponse, name="environment_swarm_overview")
async def overview(
    request: Request, swarm: Swarm, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Describe the cluster: counts for everyone, cluster and node facts for administrators."""
    try:
        summary = await swarm.queries.overview(actor, environment_id)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Swarm",
            unavailable,
            _environment_href(request, environment_id),
            "Back to the environment",
            region=None,
        )
    return page_response(request, "swarm/overview.html", {"overview": summary})
