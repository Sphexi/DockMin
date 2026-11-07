"""Notification pages: channels, subscriptions, the test message and the link base URL."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import SecretStr

from app.auth.dependencies import require_administrator_or_not_found
from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.services.assembly import NotificationAdmin
from app.services.notifications import (
    KINDS,
    SCOPES,
    SECRET_MAX,
    TEXT_MAX,
    ChannelInput,
    ChannelView,
    KindSpec,
    SubscriptionInput,
    form_names,
)
from app.web.flash import Flash
from app.web.identifiers import PathId
from app.web.navigation import no_store
from app.web.object_pages import redirect, route
from app.web.templating import is_htmx_request, render_fragment, render_page

router = APIRouter(
    prefix="/notifications", dependencies=[Depends(require_administrator_or_not_found)]
)

NotificationAdministrator = Annotated[Principal, Depends(require_administrator_or_not_found)]

NAME_FIELD_MAX: Final = 512
LINK_FIELD_MAX: Final = 1024
EXPECTED_MAX: Final = 64
ID_DIGITS_MAX: Final = 18
MAX_LISTED_VALUES: Final = 20_000

LIST_TEMPLATE: Final = "notifications/list.html"
NEW_TEMPLATE: Final = "notifications/new.html"
FORM_TEMPLATE: Final = "notifications/form.html"
DETAIL_TEMPLATE: Final = "notifications/detail.html"
DELETE_TEMPLATE: Final = "notifications/delete_confirm.html"
SUBSCRIPTIONS_TEMPLATE: Final = "notifications/subscriptions.html"
TEST_TEMPLATE: Final = "notifications/_test.html"

NOTICES: Final = {
    "created": "The channel is saved. Send a test message to check it.",
    "updated": "Changes saved.",
    "deleted": "The channel is deleted. Messages waiting for it are discarded.",
    "subscriptions": "Subscriptions saved.",
    "settings": "Settings saved.",
}


@dataclass(frozen=True, slots=True)
class FormState:
    """The values a channel form shows; no secret is ever among them."""

    name: str = ""
    is_enabled: bool = True
    values: Mapping[str, str] = field(default_factory=dict)
    clear: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ChannelTestResult:
    """The outcome of a test message, in the product's own wording."""

    ok: bool
    message: str


def _state_of(channel: ChannelView) -> FormState:
    return FormState(name=channel.name, is_enabled=channel.is_enabled, values=channel.values)


def _page(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    return no_store(render_page(request, template, context, status_code=status_code))


async def _read_form(request: Request, spec: KindSpec) -> tuple[ChannelInput, FormState, str]:
    """Read a channel form of this kind; fields the kind does not define are ignored."""
    posted = await request.form()
    names = form_names(spec)

    def text(name: str, limit: int) -> str:
        value = posted.get(name) if name in names else None
        return value[: limit + 1] if isinstance(value, str) else ""

    values: dict[str, str] = {}
    secrets: dict[str, SecretStr] = {}
    clear: set[str] = set()
    for item in spec.fields:
        if item.secret:
            secrets[item.name] = SecretStr(text(item.name, SECRET_MAX))
        else:
            values[item.name] = text(item.name, TEXT_MAX)
        if item.clearable and text(item.clear_name, 2) == "on":
            clear.add(item.name)
    name = text("name", NAME_FIELD_MAX)
    enabled = text("is_enabled", 2) == "on"
    form = ChannelInput(
        name=name,
        kind=spec.kind,
        is_enabled=enabled,
        values=values,
        secrets=secrets,
        clear=frozenset(clear),
    )
    state = FormState(name=name, is_enabled=enabled, values=values, clear=frozenset(clear))
    return form, state, text("expected_updated_at", EXPECTED_MAX)


def _form_page(
    request: Request,
    spec: KindSpec,
    state: FormState,
    *,
    channel: ChannelView | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return _page(
        request,
        FORM_TEMPLATE,
        {
            "spec": spec,
            "state": state,
            "channel": channel,
            "editing": channel is not None,
            "errors": errors or {},
        },
        status_code,
    )


def _chooser_page(request: Request, error: str | None = None, status_code: int = 200) -> Response:
    return _page(
        request, NEW_TEMPLATE, {"kinds": tuple(KINDS.values()), "error": error}, status_code
    )


async def _list_page(
    request: Request,
    services: NotificationAdmin,
    actor: Principal,
    *,
    link_base_url: str | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    settings = await services.settings(actor)
    return _page(
        request,
        LIST_TEMPLATE,
        {
            "channels": await services.list_channels(actor),
            "kinds": tuple(KINDS.values()),
            "link_base_url": settings.link_base_url if link_base_url is None else link_base_url,
            "errors": errors or {},
        },
        status_code,
    )


async def _detail_page(
    request: Request,
    services: NotificationAdmin,
    actor: Principal,
    channel_id: int,
    *,
    test: ChannelTestResult | None = None,
) -> Response:
    channel = await services.get(actor, channel_id, open_masks=True)
    return _page(
        request, DETAIL_TEMPLATE, {"channel": channel, "spec": KINDS[channel.kind], "test": test}
    )


async def _delete_page(
    request: Request,
    services: NotificationAdmin,
    actor: Principal,
    channel_id: int,
    *,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    channel = await services.get(actor, channel_id)
    return _page(
        request, DELETE_TEMPLATE, {"channel": channel, "errors": errors or {}}, status_code
    )


async def _subscriptions_page(
    request: Request,
    services: NotificationAdmin,
    actor: Principal,
    channel_id: int,
    *,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    view = await services.subscription_view(actor, channel_id)
    return _page(
        request,
        SUBSCRIPTIONS_TEMPLATE,
        {"view": view, "scopes": SCOPES, "errors": errors or {}},
        status_code,
    )


@router.get("", response_class=HTMLResponse, name="notification_channels")
async def list_channels(
    request: Request, services: NotificationAdmin, actor: NotificationAdministrator
) -> Response:
    """List the channels with their last success and last error, and the link base URL."""
    return await _list_page(request, services, actor)


@router.post("/-/settings", name="notification_settings_update")
async def update_settings(
    request: Request,
    services: NotificationAdmin,
    actor: NotificationAdministrator,
    link_base_url: Annotated[str, Form(max_length=LINK_FIELD_MAX)] = "",
) -> Response:
    """Save the address activity links are built on; blank removes the links."""
    try:
        await services.set_link_base_url(actor, link_base_url)
    except ValidationFailed as invalid:
        return await _list_page(
            request,
            services,
            actor,
            link_base_url=link_base_url,
            errors=invalid.fields,
            status_code=422,
        )
    return redirect(
        request, route(request, "notification_channels"), [Flash("ok", NOTICES["settings"])]
    )


@router.get("/new", response_class=HTMLResponse, name="notification_channel_new")
async def new_channel(
    request: Request, actor: NotificationAdministrator, kind: str = ""
) -> Response:
    """Offer the kinds of channel, or the create form for the chosen kind."""
    spec = KINDS.get(kind)
    if spec is None:
        return _chooser_page(request)
    return _form_page(request, spec, FormState())


@router.post("", name="notification_channel_create")
async def create_channel(
    request: Request, services: NotificationAdmin, actor: NotificationAdministrator
) -> Response:
    """Create a channel of the posted kind, then open its page."""
    posted = await request.form()
    kind = posted.get("kind")
    spec = KINDS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        return _chooser_page(request, "Choose a kind of channel.", 422)
    form, state, _ = await _read_form(request, spec)
    try:
        channel_id = await services.create(actor, form)
    except ValidationFailed as invalid:
        return _form_page(request, spec, state, errors=invalid.fields, status_code=422)
    return redirect(
        request,
        route(request, "notification_channel_detail", channel_id=channel_id),
        [Flash("ok", NOTICES["created"])],
    )


@router.get("/{channel_id}", response_class=HTMLResponse, name="notification_channel_detail")
async def channel_detail(
    request: Request,
    services: NotificationAdmin,
    actor: NotificationAdministrator,
    channel_id: PathId,
) -> Response:
    """Show a channel; its secrets are only reported as set."""
    return await _detail_page(request, services, actor, channel_id)


@router.get("/{channel_id}/edit", response_class=HTMLResponse, name="notification_channel_edit")
async def edit_channel_form(
    request: Request,
    services: NotificationAdmin,
    actor: NotificationAdministrator,
    channel_id: PathId,
) -> Response:
    """Render the edit form; secrets are never filled in."""
    channel = await services.get(actor, channel_id)
    return _form_page(request, KINDS[channel.kind], _state_of(channel), channel=channel)


@router.post("/{channel_id}", name="notification_channel_update")
async def update_channel(
    request: Request,
    services: NotificationAdmin,
    actor: NotificationAdministrator,
    channel_id: PathId,
) -> Response:
    """Save the channel if it is unchanged since the form was loaded."""
    channel = await services.get(actor, channel_id)
    spec = KINDS[channel.kind]
    form, state, expected = await _read_form(request, spec)
    try:
        await services.update(actor, channel_id, form, expected_updated_at=expected)
    except ValidationFailed as invalid:
        stale = "form" in invalid.fields
        return _form_page(
            request,
            spec,
            _state_of(await services.get(actor, channel_id)) if stale else state,
            channel=channel,
            errors=invalid.fields,
            status_code=422,
        )
    return redirect(
        request,
        route(request, "notification_channel_detail", channel_id=channel_id),
        [Flash("ok", NOTICES["updated"])],
    )


@router.post("/{channel_id}/test", name="notification_channel_test")
async def test_channel(
    request: Request,
    services: NotificationAdmin,
    actor: NotificationAdministrator,
    channel_id: PathId,
) -> Response:
    """Send one fixed message through the saved channel.

    htmx receives the result as a fragment, a browser the channel page.
    """
    try:
        await services.test(actor, channel_id)
        result = ChannelTestResult(True, "The test message was accepted by the receiver.")
    except ValidationFailed as invalid:
        result = ChannelTestResult(False, next(iter(invalid.fields.values()), "The test failed."))
    if is_htmx_request(request):
        return no_store(
            render_fragment(request, TEST_TEMPLATE, "channel_test_result", result=result)
        )
    return await _detail_page(request, services, actor, channel_id, test=result)


@router.get(
    "/{channel_id}/delete", response_class=HTMLResponse, name="notification_channel_delete_confirm"
)
async def confirm_delete(
    request: Request,
    services: NotificationAdmin,
    actor: NotificationAdministrator,
    channel_id: PathId,
) -> Response:
    """Name the channel to be deleted; nothing is deleted by this page."""
    return await _delete_page(request, services, actor, channel_id)


@router.post("/{channel_id}/delete", name="notification_channel_delete")
async def delete_channel(
    request: Request,
    services: NotificationAdmin,
    actor: NotificationAdministrator,
    channel_id: PathId,
    expected_name: Annotated[str, Form(max_length=NAME_FIELD_MAX)] = "",
) -> Response:
    """Delete the channel while its name is still the reviewed one."""
    try:
        await services.delete(actor, channel_id, expected_name=expected_name)
    except ValidationFailed as invalid:
        return await _delete_page(
            request, services, actor, channel_id, errors=invalid.fields, status_code=422
        )
    return redirect(
        request, route(request, "notification_channels"), [Flash("ok", NOTICES["deleted"])]
    )


@router.get(
    "/{channel_id}/subscriptions", response_class=HTMLResponse, name="notification_subscriptions"
)
async def subscriptions_form(
    request: Request,
    services: NotificationAdmin,
    actor: NotificationAdministrator,
    channel_id: PathId,
) -> Response:
    """Show the event types and environments the channel is limited to."""
    return await _subscriptions_page(request, services, actor, channel_id)


@router.post("/{channel_id}/subscriptions", name="notification_subscriptions_save")
async def save_subscriptions(
    request: Request,
    services: NotificationAdmin,
    actor: NotificationAdministrator,
    channel_id: PathId,
) -> Response:
    """Replace the channel's event types and environment scope."""
    posted = await request.form()
    types = frozenset(
        value[:NAME_FIELD_MAX]
        for value in posted.getlist("event_type")[:MAX_LISTED_VALUES]
        if isinstance(value, str)
    )
    scope = posted.get("environment_scope")
    environments = frozenset(
        int(value)
        for value in posted.getlist("environment_id")[:MAX_LISTED_VALUES]
        if isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and len(value) <= ID_DIGITS_MAX
        and int(value) > 0
    )
    form = SubscriptionInput(types, scope if isinstance(scope, str) else "", environments)
    try:
        await services.set_subscriptions(actor, channel_id, form)
    except ValidationFailed as invalid:
        return await _subscriptions_page(
            request, services, actor, channel_id, errors=invalid.fields, status_code=422
        )
    return redirect(
        request,
        route(request, "notification_channel_detail", channel_id=channel_id),
        [Flash("ok", NOTICES["subscriptions"])],
    )
