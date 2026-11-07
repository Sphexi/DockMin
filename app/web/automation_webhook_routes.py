"""Webhook pages: the URL shown once, and rotating and revoking it."""

from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, get_runtime, require_user
from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.auth.services import AuthRuntime
from app.auth.token_format import WEBHOOK_TOKEN_MARK
from app.db.engine import Session, get_session
from app.db.repositories.automation_rule import AutomationRule
from app.db.repositories.setting import SettingRepository
from app.paths import HOOKS_PATH
from app.scheduler.setup import SchedulerRuntime
from app.services.assembly import Jobs, build_webhook_service, get_scheduler_runtime
from app.services.automation_rules import RuleView
from app.services.notifications import LINK_BASE_URL_KEY
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.webhooks import (
    WEBHOOK_SUBSYSTEM,
    IssuedWebhook,
    WebhookChangedSinceReview,
    WebhookService,
    WebhookView,
)
from app.web.flash import Flash
from app.web.job_routes import html_page, path_identifier
from app.web.object_pages import Failure, problem_page, redirect, route

router = APIRouter(prefix="/automation", dependencies=[Depends(require_user)])

PREFIX_MAX: Final = 16
REFUSALS: Final = (ValidationFailed, EnvironmentUnavailable, ObjectTooLarge)
REVOKED: Final = "The webhook URL is revoked. It no longer starts the automation."
ALREADY_REVOKED: Final = "The webhook URL is already revoked."
CHANGED: Final = "The webhook changed since you reviewed it; nothing was changed."
STATE_LABELS: Final = {
    "active": ("Active", "ok"),
    "revoked": ("Revoked", "warn"),
    "revoked_by_recovery": ("Revoked by account recovery", "warn"),
}


def webhook_rule_id(rule_id: str) -> int:
    """Read the rule id of the path; anything that is not a plain number is a missing rule."""
    return path_identifier(rule_id)


RuleId = Annotated[int, Depends(webhook_rule_id)]
Prefix = Annotated[str, Form(max_length=PREFIX_MAX)]


@dataclass(frozen=True, slots=True)
class WebhookPages:
    """What the webhook pages call: the webhook service and the address links are built on."""

    service: WebhookService
    settings: SettingRepository

    async def base_url(self, request: Request) -> str:
        """Return the configured link base, else the base of this request, without a slash."""
        stored = await self.settings.get(LINK_BASE_URL_KEY)
        if stored is not None and stored.value:
            return stored.value.rstrip("/")
        return str(request.base_url).rstrip("/")

    def mask(self, base: str, webhook: WebhookView) -> str:
        """Return the webhook's address with the secret part hidden."""
        return f"{base}{HOOKS_PATH}/{WEBHOOK_TOKEN_MARK}_{webhook.prefix}_…"

    async def masked_url(self, request: Request, webhook: WebhookView) -> str:
        """Return the webhook's address as this request sees it, with the secret part hidden."""
        return self.mask(await self.base_url(request), webhook)

    async def section(self, request: Request, view: RuleView, actor: Principal) -> dict[str, Any]:
        """Return the template context of a rule page's Webhook section; empty for other rules."""
        if view.rule.subsystem != WEBHOOK_SUBSYSTEM:
            return {"webhook": None}
        webhook = await self.service.describe(view.rule.id)
        if webhook is None:
            return {"webhook": None}
        base = await self.base_url(request)
        label, tone = STATE_LABELS[webhook.state]
        return {
            "webhook": webhook,
            "webhook_masked_url": self.mask(base, webhook),
            "webhook_state": (label, tone),
            "webhook_can_rotate": view.rule.created_by_user_id == actor.user_id,
            "webhook_armed": view.next_run_at is not None,
        }

    async def token_page(
        self,
        request: Request,
        rule: AutomationRule,
        issued: IssuedWebhook,
        *,
        rotated: bool,
    ) -> Response:
        """Render the one response that holds the URL; no cache keeps it."""
        base = await self.base_url(request)
        return html_page(
            request,
            "automation/webhook_token.html",
            {
                "rule": rule,
                "token": issued.value,
                "base": base,
                "endpoint": f"{base}{HOOKS_PATH}",
                "rotated": rotated,
            },
        )


async def get_webhook_pages(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    runtime: Annotated[AuthRuntime, Depends(get_runtime)],
    scheduling: Annotated[SchedulerRuntime, Depends(get_scheduler_runtime)],
    services: Jobs,
) -> WebhookPages:
    """Provide the webhook pages bound to this request's session."""
    service = build_webhook_service(
        db,
        runtime,
        scheduling,
        limiter=request.app.state.webhook_limiter,
        tls_enabled=request.app.state.settings.tls_enabled,
        authorise_write=services.rules.authorise_write,
    )
    return WebhookPages(service, SettingRepository(db, runtime.clock))


Pages = Annotated[WebhookPages, Depends(get_webhook_pages)]


def _detail_href(request: Request, rule_id: int) -> str:
    return route(request, "automation_rule_detail", rule_id=rule_id)


def _message(refusal: Exception) -> str:
    if isinstance(refusal, ValidationFailed):
        return " ".join(refusal.fields.values())
    return str(refusal)


def _problem(
    request: Request, rule_id: int, heading: str, title: str, message: str, status: int
) -> Response:
    return problem_page(
        request,
        heading=heading,
        failure=Failure(status, title, message),
        back_href=_detail_href(request, rule_id),
        back_label="Back to the automation",
        region=None,
    )


async def _webhook_rule(
    services: Jobs, pages: WebhookPages, actor: Principal, rule_id: int, *, owner_only: bool
) -> tuple[AutomationRule, WebhookView]:
    """Return a webhook rule the caller may act on, else raise a missing page."""
    rule = await services.rules.get(actor, rule_id)
    if rule.subsystem != WEBHOOK_SUBSYSTEM or (
        owner_only and rule.created_by_user_id != actor.user_id
    ):
        raise ResourceNotFound("not found")
    webhook = await pages.service.describe(rule.id)
    if webhook is None:
        raise ResourceNotFound("not found")
    return rule, webhook


@router.get(
    "/rules/{rule_id}/webhook/rotate",
    response_class=HTMLResponse,
    name="automation_webhook_rotate_confirm",
)
async def confirm_rotate(
    request: Request,
    services: Jobs,
    pages: Pages,
    actor: CurrentUser,
    rule_id: RuleId,
) -> Response:
    """Name what rotating the URL changes; nothing changes until it is confirmed."""
    rule, webhook = await _webhook_rule(services, pages, actor, rule_id, owner_only=True)
    try:
        await services.rules.authorise_write(actor, rule)
    except REFUSALS as refusal:
        message = _message(refusal)
        return _problem(
            request,
            rule.id,
            "Rotate webhook URL",
            "The webhook URL cannot be rotated",
            message,
            422,
        )
    return html_page(
        request,
        "automation/webhook_rotate_confirm.html",
        {
            "rule": rule,
            "webhook": webhook,
            "masked_url": await pages.masked_url(request, webhook),
            "state": STATE_LABELS[webhook.state],
        },
    )


@router.post("/rules/{rule_id}/webhook/rotate", name="automation_webhook_rotate")
async def rotate(
    request: Request,
    services: Jobs,
    pages: Pages,
    actor: CurrentUser,
    rule_id: RuleId,
    expected_prefix: Prefix = "",
) -> Response:
    """Replace the URL's secret and answer with the page that shows the new one, once."""
    rule, _ = await _webhook_rule(services, pages, actor, rule_id, owner_only=True)
    try:
        issued = await pages.service.rotate(actor, rule.id, expected_prefix)
    except WebhookChangedSinceReview:
        return _problem(
            request, rule.id, "Rotate webhook URL", "The webhook URL was not rotated", CHANGED, 409
        )
    except REFUSALS as refusal:
        message = _message(refusal)
        return _problem(
            request, rule.id, "Rotate webhook URL", "The webhook URL was not rotated", message, 422
        )
    return await pages.token_page(request, rule, issued, rotated=True)


@router.get(
    "/rules/{rule_id}/webhook/revoke",
    response_class=HTMLResponse,
    name="automation_webhook_revoke_confirm",
)
async def confirm_revoke(
    request: Request, services: Jobs, pages: Pages, actor: CurrentUser, rule_id: RuleId
) -> Response:
    """Name what revoking the URL changes; nothing changes until it is confirmed."""
    rule, webhook = await _webhook_rule(services, pages, actor, rule_id, owner_only=False)
    if webhook.state != "active":
        return _problem(
            request,
            rule.id,
            "Revoke webhook URL",
            "The webhook URL cannot be revoked",
            ALREADY_REVOKED,
            409,
        )
    return html_page(
        request,
        "automation/webhook_revoke_confirm.html",
        {
            "rule": rule,
            "webhook": webhook,
            "masked_url": await pages.masked_url(request, webhook),
            "owner_revokes": rule.created_by_user_id == actor.user_id,
        },
    )


@router.post("/rules/{rule_id}/webhook/revoke", name="automation_webhook_revoke")
async def revoke(
    request: Request,
    services: Jobs,
    pages: Pages,
    actor: CurrentUser,
    rule_id: RuleId,
    expected_prefix: Prefix = "",
) -> Response:
    """Revoke the URL; the automation and its history stay."""
    rule, _ = await _webhook_rule(services, pages, actor, rule_id, owner_only=False)
    try:
        await pages.service.revoke(actor, rule.id, expected_prefix)
    except WebhookChangedSinceReview:
        return _problem(
            request, rule.id, "Revoke webhook URL", "The webhook URL was not revoked", CHANGED, 409
        )
    return redirect(request, _detail_href(request, rule.id), [Flash("ok", REVOKED)])
