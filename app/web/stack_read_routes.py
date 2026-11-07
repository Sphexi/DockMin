"""Read from host and Reconstruct from the cluster: the result pages and Adopt as is."""

from typing import Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.services.assembly import Environments, Stacks, StackServices, Swarm
from app.services.host_read_stash import HostReadStash
from app.services.stack_host_read import HostReadReport
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.object_pages import (
    Failure,
    page_response,
    problem_page,
    query_of,
    redirect,
    route,
    unavailable_page,
)
from app.web.stack_pages import Problem
from app.web.stack_read_pages import HostReads, Kind, read_page
from app.web.stack_routes import STACK_ERRORS, UNAVAILABLE, ProjectName
from app.web.stack_routes import problem_of as stack_problem
from app.web.swarm_routes import UNAVAILABLE as SWARM_UNAVAILABLE
from app.web.swarm_routes import Namespace, swarm_path, swarm_router
from app.web.swarm_stack_manage_routes import SWARM_ERRORS

__all__ = ["router", "swarm"]

router = APIRouter(
    prefix="/environments/{environment_id}/stacks",
    tags=["stacks"],
    dependencies=[Depends(require_user)],
)
swarm = swarm_router()

PAGE: Final = "stacks/read_result.html"
EXPIRED: Final = Failure(
    409,
    "The read has expired",
    "The files read from the host are no longer held. Read them again.",
)
ADOPTED: Final = {
    "compose": "Stack adopted as is. Nothing was changed on the host, and it reads as unchanged.",
    "swarm": "Stack adopted as is. Nothing was changed on the cluster, and it reads as unchanged.",
}


def _administrators_only(actor: Principal) -> None:
    if not actor.is_administrator:
        raise ResourceNotFound("not found")


def _failure(problem: Problem) -> Failure:
    if problem.failure is not None:
        return problem.failure
    rejection = problem.rejection
    messages = [message for _, message in rejection.problems] if rejection else []
    messages += [message for message in problem.errors.values() if message not in messages]
    return Failure(
        problem.status,
        rejection.title if rejection else "Check the request",
        " ".join(messages) or "The request failed.",
    )


def _hrefs(request: Request, kind: Kind, environment_id: int, name: str) -> dict[str, str]:
    if kind == "compose":
        arguments = {"environment_id": environment_id, "project": name}
        return {
            "paste_href": route(request, "environment_stack_adopt", **arguments),
            "adopt_href": route(request, "environment_stack_adopt_as_is", **arguments),
            "cancel_href": route(request, "environment_stack_list", environment_id=environment_id),
        }
    arguments = {"environment_id": environment_id, "namespace": name}
    return {
        "paste_href": swarm_path(request, "stack_adopt", **arguments),
        "adopt_href": swarm_path(request, "stack_adopt_as_is", **arguments),
        "cancel_href": swarm_path(request, "stack_list", environment_id=environment_id),
    }


async def _result(
    request: Request,
    environments: Environments,
    actor: Principal,
    environment_id: int,
    kind: Kind,
    report: HostReadReport,
    token: str | None,
) -> Response:
    environment = (await environments.environments.get(actor, environment_id)).summary
    links = _hrefs(request, kind, environment_id, report.name)
    prefill = f"{links['paste_href']}?{query_of('read', token)}" if token else links["paste_href"]
    return page_response(
        request,
        PAGE,
        {
            "environment": environment,
            "page": read_page(kind, report, token),
            "failure": None,
            "prefill_href": prefill,
            **links,
        },
    )


def _kept(
    stash: HostReadStash, actor: Principal, environment_id: int, kind: Kind, report: HostReadReport
) -> str | None:
    result = report.result
    if result is None:
        return None
    return stash.put(actor.user_id, environment_id, kind, report.name, result)


@router.post(
    "/-/adopt/{project}/read", response_class=HTMLResponse, name="environment_stack_read_host"
)
async def read_host(
    project: ProjectName,
    request: Request,
    stacks: Stacks,
    environments: Environments,
    stash: HostReads,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Read an unmanaged project's files from its host and show what would be adopted."""
    _administrators_only(actor)
    list_href = route(request, "environment_stack_list", environment_id=environment_id)
    try:
        await stacks.queries.adopt_context(actor, environment_id, project)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Read from host", unavailable, list_href, "Back to stacks", region=None
        )
    try:
        report = await stacks.reader.read_compose(actor, environment_id, project)
    except STACK_ERRORS as error:
        return _refused(request, stacks, actor, environment_id, "compose", project, error)
    token = _kept(stash, actor, environment_id, "compose", report)
    return await _result(request, environments, actor, environment_id, "compose", report, token)


def _refused(
    request: Request,
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    kind: Kind,
    name: str,
    error: Exception,
) -> Response:
    problem = stack_problem(error, stacks, actor)
    links = _hrefs(request, kind, environment_id, name)
    return problem_page(
        request,
        heading="Read from host" if kind == "compose" else "Reconstruct from the cluster",
        failure=_failure(problem),
        back_href=links["paste_href"],
        back_label="Back to the paste form",
        region=None,
    )


@router.post(
    "/-/adopt/{project}/as-is", response_class=HTMLResponse, name="environment_stack_adopt_as_is"
)
async def adopt_as_is(
    project: ProjectName,
    request: Request,
    stacks: Stacks,
    stash: HostReads,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Adopt the project with the files that were read, and deploy nothing."""
    _administrators_only(actor)
    list_href = route(request, "environment_stack_list", environment_id=environment_id)
    try:
        await stacks.queries.adopt_context(actor, environment_id, project)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Adopt stack", unavailable, list_href, "Back to stacks", region=None
        )
    token = str((await request.form()).get("read_token", ""))
    held = stash.get(token, actor.user_id, environment_id, "compose", project)
    links = _hrefs(request, "compose", environment_id, project)
    if held is None:
        return problem_page(
            request,
            heading="Adopt stack",
            failure=EXPIRED,
            back_href=links["paste_href"],
            back_label="Back to the paste form",
            region=None,
        )
    try:
        await stacks.actions.adopt(actor, environment_id, name=project, prepared=held, as_is=True)
    except STACK_ERRORS as error:
        return _refused(request, stacks, actor, environment_id, "compose", project, error)
    stash.discard(token)
    detail = route(
        request, "environment_stack_detail", environment_id=environment_id, stack_name=project
    )
    return redirect(request, detail, [Flash("ok", ADOPTED["compose"])])


@swarm.post(
    "/stacks/-/adopt/{namespace}/read",
    response_class=HTMLResponse,
    name="environment_swarm_stack_read_cluster",
)
async def read_cluster(
    namespace: Namespace,
    request: Request,
    swarm_services: Swarm,
    stacks: Stacks,
    environments: Environments,
    stash: HostReads,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Reconstruct an unmanaged namespace's file from the cluster and show what would be adopted."""
    _administrators_only(actor)
    list_href = swarm_path(request, "stack_list", environment_id=environment_id)
    try:
        found = await swarm_services.stacks.adopt_context(actor, environment_id, namespace)
    except SWARM_UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Reconstruct from the cluster",
            unavailable,
            list_href,
            "Back to stacks",
            region=None,
        )
    try:
        report = await stacks.reader.read_swarm(actor, environment_id, found.name)
    except SWARM_ERRORS as error:
        return _refused(request, stacks, actor, environment_id, "swarm", found.name, error)
    token = _kept(stash, actor, environment_id, "swarm", report)
    return await _result(request, environments, actor, environment_id, "swarm", report, token)


@swarm.post(
    "/stacks/-/adopt/{namespace}/as-is",
    response_class=HTMLResponse,
    name="environment_swarm_stack_adopt_as_is",
)
async def adopt_cluster_as_is(
    namespace: Namespace,
    request: Request,
    swarm_services: Swarm,
    stacks: Stacks,
    stash: HostReads,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Adopt the namespace with the file that was reconstructed; the cluster is not changed."""
    _administrators_only(actor)
    list_href = swarm_path(request, "stack_list", environment_id=environment_id)
    try:
        found = await swarm_services.stacks.adopt_context(actor, environment_id, namespace)
    except SWARM_UNAVAILABLE as unavailable:
        return unavailable_page(
            request, "Adopt stack", unavailable, list_href, "Back to stacks", region=None
        )
    token = str((await request.form()).get("read_token", ""))
    held = stash.get(token, actor.user_id, environment_id, "swarm", found.name)
    links = _hrefs(request, "swarm", environment_id, found.name)
    if held is None:
        return problem_page(
            request,
            heading="Adopt stack",
            failure=EXPIRED,
            back_href=links["paste_href"],
            back_label="Back to the paste form",
            region=None,
        )
    try:
        await stacks.swarm_adopter.adopt(
            actor, environment_id, found.name, prepared=held, as_is=True
        )
    except SWARM_ERRORS as error:
        return _refused(request, stacks, actor, environment_id, "swarm", found.name, error)
    stash.discard(token)
    detail = swarm_path(
        request, "stack_detail", environment_id=environment_id, namespace=found.name
    )
    return redirect(request, detail, [Flash("ok", ADOPTED["swarm"])])
