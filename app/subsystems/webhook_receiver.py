"""webhook-receiver: redeploys or updates a rule's named targets each time its webhook is called."""

from datetime import timedelta
from typing import ClassVar, Final, Literal

from pydantic import Field

from app.db.repositories.activity_event import DetailValue
from app.db.repositories.scheduled_job import OverrunPolicy, ScheduleKind
from app.db.repositories.stack import StackKind
from app.db.repositories.webhook import WebhookRepository
from app.scheduler.model import RunFailed, RunResult
from app.subsystems.base import RuleParameters, RunContext
from app.subsystems.container_updater import (
    ContainerUpdater,
    PassSettings,
    UpdaterParameters,
    run_pass,
)
from app.subsystems.selectors import SelectorKind

NAME: Final = "webhook-receiver"
TRIGGER: Final = "webhook"
TOKEN_REVOKED: Final = "token_revoked"


class WebhookParameters(RuleParameters):
    """What a call does to the rule's targets and how it watches the result."""

    action: Literal["update", "redeploy"] = "update"
    verify_seconds: int = Field(30, ge=0, le=300)
    require_healthy: bool = True


class WebhookReceiver(ContainerUpdater):
    """Applies the rule's action to its named targets when the webhook is called."""

    name = NAME
    parameters_model = WebhookParameters
    schedule_kinds: ClassVar[frozenset[ScheduleKind]] = frozenset({"webhook"})
    allowed_overrun_policies: ClassVar[frozenset[OverrunPolicy]] = frozenset({"skip"})
    suppression_window = timedelta(0)
    max_concurrent_runs: ClassVar[int | None] = 3
    target_kinds: ClassVar[frozenset[SelectorKind]] = frozenset({"container", "stack", "service"})
    stack_kinds: ClassVar[frozenset[StackKind]] = frozenset({"compose", "swarm"})

    async def run(self, context: RunContext) -> RunResult:
        """Update or redeploy the rule's targets; a call's count and address join the detail."""
        params = context.parameters
        if not isinstance(params, WebhookParameters):
            raise RunFailed("parameters_invalid", "The automation's parameters are not valid.")
        detail: dict[str, DetailValue] = {}
        if context.dispatch.run.trigger == TRIGGER:
            rule, _ = context.rule_scope()
            webhooks = WebhookRepository(context.db, context.clock)
            webhook = await webhooks.get(rule.id)
            if webhook is not None and webhook.is_revoked:
                return RunResult(
                    "skipped", reason=TOKEN_REVOKED, message="The webhook URL is revoked."
                )
            pending = await webhooks.take_pending(rule.id)
            detail = {"calls": pending.calls, "caller_address": pending.caller_address}
        settings = PassSettings(
            subsystem=NAME,
            trigger=TRIGGER,
            notes={"webhook_action": params.action},
            refresh=False,
            redeploy=params.action == "redeploy",
            mode=params.action,
            detail=detail,
        )
        return await run_pass(
            context,
            self._deps,
            UpdaterParameters(
                action="update",
                verify_seconds=params.verify_seconds,
                require_healthy=params.require_healthy,
            ),
            target_kinds=self.target_kinds,
            cursors=self._cursors,
            self_identity=self._self_identity,
            settings=settings,
        )
