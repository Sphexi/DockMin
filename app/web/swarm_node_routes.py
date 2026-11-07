"""Swarm node and task pages."""

from typing import Annotated

from fastapi import Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser
from app.auth.principal import Principal
from app.services.assembly import (
    Swarm,
    SwarmServices,
)
from app.web.identifiers import PathId
from app.web.object_pages import (
    Field,
    page_response,
    problem_page,
    query_of,
    redirect,
    unavailable_page,
)
from app.web.origins import RequestOrigin
from app.web.swarm_routes import (
    ACTION_ERRORS,
    UNAVAILABLE,
    NodeId,
    TaskId,
    failure_of,
    notice_flash,
    service_detail_href,
    swarm_path,
    swarm_router,
    version_of,
)

router = swarm_router()


@router.get("/tasks/{task_id}", response_class=HTMLResponse, name="environment_swarm_task_detail")
async def task_detail(
    task_id: TaskId, request: Request, swarm: Swarm, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Show one task; it is visible exactly when its service is."""
    try:
        detail = await swarm.queries.task(actor, environment_id, task_id)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Task",
            unavailable,
            swarm_path(request, "overview", environment_id=environment_id),
            "Back to Swarm",
            region=None,
        )
    return page_response(
        request,
        "swarm/tasks/detail.html",
        {
            "detail": detail,
            "service_href": (
                f"{service_detail_href(request, environment_id, detail.service_id)}?tab=tasks"
            ),
        },
    )


@router.get("/nodes", response_class=HTMLResponse, name="environment_swarm_node_list")
async def list_nodes(
    request: Request, swarm: Swarm, actor: CurrentUser, environment_id: PathId
) -> Response:
    """List the nodes of the cluster; administrators only."""
    try:
        nodes = await swarm.queries.nodes(actor, environment_id)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Nodes",
            unavailable,
            swarm_path(request, "overview", environment_id=environment_id),
            "Back to Swarm",
            region=None,
        )
    return page_response(request, "swarm/nodes/list.html", {"nodes": nodes})


@router.get(
    "/nodes/{node_id}",
    response_class=HTMLResponse,
    name="environment_swarm_node_detail",
    dependencies=[RequestOrigin],
)
async def node_detail(
    node_id: NodeId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
) -> Response:
    """Show one node and the tasks placed on it; administrators only."""
    try:
        detail = await swarm.queries.node(actor, environment_id, node_id)
    except UNAVAILABLE as unavailable:
        return unavailable_page(
            request,
            "Node",
            unavailable,
            swarm_path(request, "node_list", environment_id=environment_id),
            "Back to nodes",
            region=None,
        )
    return page_response(
        request,
        "swarm/nodes/detail.html",
        {
            "detail": detail,
        },
    )


def _node_problem(
    request: Request,
    swarm: SwarmServices,
    actor: Principal,
    error: Exception,
    environment_id: int,
    node_id: str,
    review_href: str | None,
) -> Response:
    return problem_page(
        request,
        heading="Change node availability",
        failure=failure_of(error, swarm, actor),
        back_href=swarm_path(
            request, "node_detail", environment_id=environment_id, node_id=node_id
        ),
        back_label="Back to the node",
        review_href=review_href,
        region=None,
    )


@router.get(
    "/nodes/{node_id}/availability",
    response_class=HTMLResponse,
    name="environment_swarm_node_availability",
)
async def availability_confirm(
    node_id: NodeId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
    availability: Annotated[str, Query(max_length=16)] = "",
) -> Response:
    """Name the running tasks a change of availability moves; administrators only."""
    try:
        impact = await swarm.queries.availability_impact(
            actor, environment_id, node_id, availability
        )
    except ACTION_ERRORS as error:
        return _node_problem(request, swarm, actor, error, environment_id, node_id, None)
    return page_response(
        request,
        "swarm/nodes/availability_confirm.html",
        {
            "impact": impact,
            "detail_href": swarm_path(
                request, "node_detail", environment_id=environment_id, node_id=node_id
            ),
            "run_href": swarm_path(
                request, "node_availability_run", environment_id=environment_id, node_id=node_id
            ),
        },
    )


@router.post(
    "/nodes/{node_id}/availability",
    response_class=HTMLResponse,
    name="environment_swarm_node_availability_run",
)
async def run_availability(
    node_id: NodeId,
    request: Request,
    swarm: Swarm,
    actor: CurrentUser,
    environment_id: PathId,
    availability: Field = "",
    expected_version: Field = "",
    expected_availability: Field = "",
) -> Response:
    """Change the availability of the node that was reviewed."""
    try:
        changed = await swarm.nodes.set_availability(
            actor,
            environment_id,
            node_id,
            availability=availability,
            expected_version=version_of(expected_version, "node"),
            expected_availability=expected_availability,
        )
    except ACTION_ERRORS as error:
        return _node_problem(
            request,
            swarm,
            actor,
            error,
            environment_id,
            node_id,
            swarm_path(request, "node_availability", environment_id=environment_id, node_id=node_id)
            + f"?{query_of('availability', availability)}",
        )
    notice = "availability_unchanged" if changed.unchanged else "availability_changed"
    return redirect(
        request,
        swarm_path(request, "node_detail", environment_id=environment_id, node_id=node_id),
        [notice_flash(notice)],
    )
