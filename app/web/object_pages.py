"""Shared object-page pieces: responses, problem pages, forms and the access-panel write path."""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Annotated, Any, Final, Literal, NoReturn
from urllib.parse import urlencode

from fastapi import Depends, Form, Query, Request
from fastapi.responses import Response
from pydantic import StringConstraints

from app.auth.errors import ResourceControlNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.access import ResourceKey, ResourceRef
from app.services.container_actions import BusyView
from app.services.containers import AccessPanel
from app.services.grantees import HasGrantees, resolve_grantees, split_names
from app.services.resource_controls import (
    ControlChanged,
    ControlSpec,
    ControlView,
    ResourceControlService,
    control_digest,
)
from app.web.flash import Flash
from app.web.identifiers import BoundedId
from app.web.navigation import no_store
from app.web.navigation import redirect as redirect_to
from app.web.problems import error_copy, unreachable_copy
from app.web.templating import is_htmx_request, render_fragment, render_page, render_partial

FIELD_MAX: Final = 512
CURSOR_MAX: Final = 512
FILTER_MAX: Final = 512
GRANT_PARAMS_MAX: Final = 600
NAMES_TEXT_MAX: Final = 8192
_NO_CONTROL_HAS: Final = "-"
DIGEST_MAX: Final = 64
OWNER_ID_MAX: Final = 20
_TAMPERED_OWNER: Final = -1
MAX_FORM_FIELDS: Final = 400
MIN_ROWS: Final = 2
ROW_STEP: Final = 2
ROW_CAP: Final = 32
RETRYABLE: Final = (409, 502, 503)
VISIBILITIES: Final = ("restricted", "public", "administrators_only")
ROW_KINDS: Final = ("option", "label")
FORM_ROW: Final = re.compile(r"(option|label)-([0-9]{1,4})-(key|value)")
PROBLEM_TEMPLATE: Final = "shared/problem.html"
ACCESS_SAVED: Final = "Access control saved."
ACCESS_REMOVED: Final = "Access control removed."
REGION_ID: Final = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
HX_TARGET: Final = "HX-Target"

Field = Annotated[str, Form(max_length=FIELD_MAX)]
Short = Annotated[str, StringConstraints(max_length=FIELD_MAX)]
Filter = Annotated[str | None, Query(max_length=FILTER_MAX)]


@dataclass(frozen=True, slots=True)
class Failure:
    """The notice a refused or failed action is answered with."""

    status: int
    title: str
    message: str
    detail: str | None = None


def page_response(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    """Render a full page that no cache keeps."""
    return no_store(render_page(request, template, context, status_code=status_code))


def fragment_response(
    request: Request,
    template: str,
    macro: str,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    **arguments: Any,
) -> Response:
    """Render one macro as a fragment that no cache keeps."""
    return no_store(
        render_fragment(
            request, template, macro, status_code=status_code, headers=headers, **arguments
        )
    )


def redirect(request: Request, location: str, flashes: Sequence[Flash] = ()) -> Response:
    """Answer a completed post with a redirect that no cache keeps, carrying the notices."""
    return no_store(redirect_to(request, location, flashes))


def administrator_detail(error: BaseException, actor: Principal) -> str | None:
    """Return the Engine's text carried by a refusal, only when the caller is an administrator."""
    detail = getattr(error, "engine_detail", None)
    return detail if actor.is_administrator and isinstance(detail, str) else None


def route(request: Request, name: str, /, **params: Any) -> str:
    """Return the path of a named route."""
    return str(request.app.url_path_for(name, **params))


def query_of(name: str, value: str) -> str:
    """Encode one query pair."""
    return urlencode([(name, value)])


def blank_to_none(value: str | None) -> str | None:
    """Return the stripped text, or None when nothing is left."""
    text = (value or "").strip()
    return text or None


def self_href(request: Request) -> str:
    """Return the request's own path and query."""
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def busy_message(busy: BusyView, subject: str) -> str:
    """Say who holds the object and for how long, in the words the viewer may be told."""
    if busy.held_by_you:
        who = "You started"
    elif busy.holder == "another user":
        who = "Another user started"
    else:
        who = f"{busy.holder} started"
    unit = "second" if busy.age_seconds == 1 else "seconds"
    return (
        f"{who} a {busy.action} on {subject} {busy.age_seconds} {unit} ago and it has not "
        "finished. Wait for it to complete, then try again."
    )


@dataclass(frozen=True, slots=True)
class Region:
    """The page element a failed fragment replaces: its id and how a swap into it is made."""

    id: str
    swap: Literal["outerHTML", "innerHTML"]

    def __post_init__(self) -> None:
        if not REGION_ID.fullmatch(self.id):
            raise ValueError(f"not a region id: {self.id!r}")


def active_region(request: Request, region: Region | None) -> Region | None:
    """Return the region when the request is an htmx fragment request aimed at that element."""
    if region is None or not is_htmx_request(request):
        return None
    return region if request.headers.get(HX_TARGET) == region.id else None


def problem_page(
    request: Request,
    *,
    heading: str,
    failure: Failure,
    back_href: str,
    back_label: str,
    region: Region | None,
    review_href: str | None = None,
    environment_name: str = "",
) -> Response:
    """Answer a failed action with the shared problem page, at the failure's status.

    A fragment request gets the error notice alone, which swaps into the region that asked. An
    empty title or message falls back to the copy for the status. `region` is none for a failure
    that is never answered as a fragment.
    """
    copy = error_copy(failure.status)
    title = failure.title or copy.title
    message = failure.message or copy.message
    request_id = str(getattr(request.state, "request_id", ""))
    retry_href = review_href if failure.status in RETRYABLE else None
    if is_htmx_request(request):
        if retry_href is None and request.method == "GET" and failure.status in RETRYABLE:
            retry_href = self_href(request)
        target = active_region(request, region)
        return no_store(
            render_partial(
                request,
                "error_state",
                title=title,
                message=message,
                request_id=request_id or None,
                retry_href=retry_href,
                region_id=target.id if target else None,
                swap=target.swap if target else "outerHTML",
                engine_detail=failure.detail,
                status_code=failure.status,
            )
        )
    return page_response(
        request,
        PROBLEM_TEMPLATE,
        {
            "heading": heading,
            "status_code": failure.status,
            "title": title,
            "message": message,
            "request_id": request_id,
            "engine_detail": failure.detail,
            "back_href": back_href,
            "back_label": back_label,
            "review_href": retry_href,
            "environment_name": environment_name,
        },
        failure.status,
    )


def unavailable_page(
    request: Request,
    heading: str,
    error: Exception,
    back_href: str,
    back_label: str,
    *,
    region: Region | None,
) -> Response:
    """Answer an unreachable or unreadable host with the shared problem page."""
    copy = unreachable_copy(error)
    failure = Failure(503, copy.title, copy.message)
    return problem_page(
        request,
        heading=heading,
        failure=failure,
        back_href=back_href,
        back_label=back_label,
        region=region,
    )


async def read_form(request: Request, names: frozenset[str]) -> dict[str, str]:
    """Read the posted fields the form defines, plus its numbered option and label rows."""
    posted = await request.form()
    values: dict[str, str] = {}
    for key, value in posted.items():
        if not isinstance(value, str):
            continue
        if key in names or FORM_ROW.fullmatch(key):
            values[key] = value
        if len(values) >= MAX_FORM_FIELDS:
            break
    return values


def row_count(values: Mapping[str, str], kind: str, *, add: bool = False) -> int:
    """Return how many rows of a kind the form shows: the filled ones, a blank pair, and more."""
    indices = [
        int(found[2]) for key in values if (found := FORM_ROW.fullmatch(key)) and found[1] == kind
    ]
    used = max(indices, default=-1) + 1
    shown = max(MIN_ROWS, used) + (ROW_STEP if add else 0)
    return max(min(shown, ROW_CAP), used)


@dataclass(frozen=True, slots=True)
class AccessInput:
    """The posted access form."""

    visibility: str
    access_level: str
    owner_name: str
    keep_user_ids: list[int]
    keep_team_ids: list[int]
    add_users: str
    add_teams: str
    expected_digest: str
    owner_user_id: str = ""
    owner_name_shown: str = ""

    def values(self) -> dict[str, Any]:
        """Return the form as the access panel shows it again."""
        return {
            "visibility": self.visibility,
            "access_level": self.access_level,
            "owner_name": self.owner_name,
            "keep_user_ids": self.keep_user_ids,
            "keep_team_ids": self.keep_team_ids,
            "add_users": self.add_users,
            "add_teams": self.add_teams,
            "expected_digest": self.expected_digest,
            "owner_user_id": self.owner_user_id,
            "owner_name_shown": self.owner_name_shown,
        }


def access_form(
    visibility: Field = "",
    access_level: Field = "read",
    owner_name: Field = "",
    keep_user_ids: Annotated[list[BoundedId] | None, Form(max_length=GRANT_PARAMS_MAX)] = None,
    keep_team_ids: Annotated[list[BoundedId] | None, Form(max_length=GRANT_PARAMS_MAX)] = None,
    add_users: Annotated[str, Form(max_length=NAMES_TEXT_MAX)] = "",
    add_teams: Annotated[str, Form(max_length=NAMES_TEXT_MAX)] = "",
    expected_digest: Annotated[str, Form(max_length=DIGEST_MAX)] = "",
    owner_user_id: Annotated[str, Form(max_length=OWNER_ID_MAX)] = "",
    owner_name_shown: Field = "",
) -> AccessInput:
    """Read the posted access form."""
    return AccessInput(
        visibility,
        access_level,
        owner_name,
        list(keep_user_ids or []),
        list(keep_team_ids or []),
        add_users,
        add_teams,
        expected_digest,
        owner_user_id,
        owner_name_shown,
    )


AccessData = Annotated[AccessInput, Depends(access_form)]
DigestField = Annotated[str, Form(max_length=DIGEST_MAX)]


@dataclass(frozen=True, slots=True)
class AccessRefusal:
    """An access form that was not applied, and how its page is shown again."""

    status_code: int
    values: dict[str, Any] | None
    fields: dict[str, str]


def refuse_access(error: ValidationFailed, data: AccessInput) -> AccessRefusal:
    """Describe a refused access form: 409 when the control changed, 422 for a bad field.

    A 422 shows the submitted values under the submitted digest. A 409 shows the current control.
    """
    if isinstance(error, ControlChanged):
        return AccessRefusal(409, None, error.fields)
    return AccessRefusal(422, data.values(), error.fields)


def refuse_removal(error: ValidationFailed) -> AccessRefusal:
    """Describe a refused control removal; the page shows the current control."""
    status = 409 if isinstance(error, ControlChanged) else 422
    return AccessRefusal(status, None, error.fields)


def bind_digest(panel: AccessPanel | None, values: Mapping[str, Any] | None) -> AccessPanel | None:
    """Return the panel carrying the digest a refused form submitted, so a re-render keeps it."""
    if panel is None or values is None:
        return panel
    submitted = values.get("expected_digest")
    if not isinstance(submitted, str):
        return panel
    return replace(panel, submitted_digest=submitted)


def bound_owner(data: AccessInput) -> int | None:
    """Return the owner id the form kept, or None when the owner is the typed name.

    The id is trusted only while the name still reads as it was shown. A value that is not an id
    is returned as an id no account has.
    """
    if not data.owner_user_id or data.owner_name != data.owner_name_shown:
        return None
    text = data.owner_user_id
    return int(text) if text.isascii() and text.isdecimal() else _TAMPERED_OWNER


async def grant_spec(
    services: HasGrantees, data: AccessInput, owner_id: int | None = None
) -> ControlSpec:
    """Read the access form into a control, resolving typed names on the server.

    Kept grants and added names count only for a restricted object. A given `owner_id` replaces
    the typed owner name.
    """
    if data.visibility not in VISIBILITIES:
        raise ValidationFailed({"is_public": "Choose who can reach this object."})
    restricted = data.visibility == "restricted"
    users = split_names(data.add_users, field="add_users") if restricted else ()
    teams = split_names(data.add_teams, field="add_teams", commas=False) if restricted else ()
    added_users, added_teams, owner = await resolve_grantees(
        services, users=users, teams=teams, owner=data.owner_name, owner_id=owner_id
    )
    return ControlSpec(
        access_level=data.access_level,
        is_public=data.visibility == "public",
        is_administrators_only=data.visibility == "administrators_only",
        owner_user_id=owner,
        user_ids=tuple(dict.fromkeys((*data.keep_user_ids, *added_users))) if restricted else (),
        team_ids=tuple(dict.fromkeys((*data.keep_team_ids, *added_teams))) if restricted else (),
    )


async def _refuse_as_changed(
    actor: Principal,
    controls: ResourceControlService,
    key: ResourceKey,
    current: ControlView | None,
) -> NoReturn:
    """Record the refusal of a form built from a control that is no longer current, then raise."""
    # A digest no control can have makes the audited service refuse and record the refusal,
    # even if the control changes back before its transaction starts.
    refused = ControlSpec(access_level="read")
    try:
        if current is not None:
            await controls.update(actor, key, refused, expected_digest=_NO_CONTROL_HAS)
        else:
            await controls.create(actor, key, refused, expected_digest=_NO_CONTROL_HAS)
    except ResourceControlNotFound:
        pass
    raise ControlChanged


async def write_access(
    actor: Principal,
    controls: ResourceControlService,
    services: HasGrantees,
    key: ResourceKey,
    current: ControlView | None,
    data: AccessInput,
) -> None:
    """Create or replace the control over an object from the posted form.

    ControlChanged when the control is no longer the one the form was built from, or the owner id
    the form kept is not the reviewed owner, judged before any typed name is resolved;
    ValidationFailed for a bad field.
    """
    if control_digest(current) != data.expected_digest:
        await _refuse_as_changed(actor, controls, key, current)
    owner_id = bound_owner(data)
    if owner_id is not None and (current is None or current.owner_user_id != owner_id):
        await _refuse_as_changed(actor, controls, key, current)
    spec = await grant_spec(services, data, owner_id)
    if current is None:
        await controls.create(actor, key, spec, expected_digest=data.expected_digest)
    else:
        await controls.update(actor, key, spec, expected_digest=data.expected_digest)


async def apply_access(
    actor: Principal,
    controls: ResourceControlService,
    grantees: HasGrantees,
    panel: AccessPanel,
    data: AccessInput,
    members: Iterable[ResourceRef] | None = None,
) -> AccessRefusal | None:
    """Write the posted control; None when it was saved, else how the access tab is shown again.

    `members`, read before this call, refresh a stack control's display cache once it is saved.
    """
    try:
        await write_access(actor, controls, grantees, panel.key, panel.control, data)
        if members is not None:
            await controls.refresh_sub_resources(panel.key, members)
    except ValidationFailed as invalid:
        return refuse_access(invalid, data)
    return None


async def remove_access(
    actor: Principal, controls: ResourceControlService, panel: AccessPanel, expected_digest: str
) -> AccessRefusal | None:
    """Remove the control; None when it was removed, else how the access tab is shown again."""
    try:
        await controls.delete(actor, panel.key, expected_digest=expected_digest)
    except ValidationFailed as invalid:
        return refuse_removal(invalid)
    return None
