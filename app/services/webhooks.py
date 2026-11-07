"""Webhooks: the token, the inbound call path and the owner's rotate and revoke."""

import hmac
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Final, Literal

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.auth.token_format import PREFIX_ATTEMPTS, WEBHOOK_TOKEN_MARK, new_token, token_pattern
from app.auth.tokens import hash_token
from app.db.engine import Session
from app.db.repositories.automation_rule import AutomationRule, AutomationRuleRepository
from app.db.repositories.clocked import Clock, format_timestamp, parse_timestamp
from app.db.repositories.errors import DuplicateError
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.db.repositories.setting import SettingRepository
from app.db.repositories.user import UserRepository
from app.db.repositories.webhook import RevokeReason, Webhook, WebhookRepository
from app.logging import LOG_INTERVAL_SECONDS, get_logger
from app.scheduler.gates import read_subsystem_enabled
from app.scheduler.registry import HandlerRegistry
from app.services.activity import ActivityRecorder, audited, current_audit
from app.services.jobs import job_state

WEBHOOK_SUBSYSTEM: Final = "webhook-receiver"
WEBHOOK_RUN_SPACING: Final = timedelta(seconds=60)
CALL_BURST: Final = 6
CALL_REFILL_SECONDS: Final = 10.0
MAX_TRACKED_WEBHOOKS: Final = 10_000
CHANGED_SINCE_REVIEW: Final = "The webhook changed since you reviewed it; nothing was changed."
_ABSENT_HASH: Final = "0" * 64
_TOKEN_PATTERN = token_pattern(WEBHOOK_TOKEN_MARK)

logger = get_logger(__name__)

type WebhookState = Literal["active", "revoked", "revoked_by_recovery"]
type AuthoriseWrite = Callable[[Principal, AutomationRule], Awaitable[None]]

_REFUSAL_REASONS: Final = {
    "unavailable": "unavailable",
    "subsystem_disabled": "subsystem_disabled",
    "auto_disabled": "auto_disabled",
    "rule_disabled": "disabled",
    "paused": "paused",
}


def token_prefix(presented: str) -> str | None:
    """Return the prefix of a well-formed token, or None for any other value."""
    match = _TOKEN_PATTERN.fullmatch(presented)
    return None if match is None else match.group(1)


@dataclass(frozen=True, slots=True)
class IssuedWebhook:
    """A new token; value is the only copy that will ever exist and is kept out of repr."""

    value: str = field(repr=False)
    prefix: str


@dataclass(frozen=True, slots=True)
class WebhookView:
    """A webhook as the rule page shows it; there is no field that could hold the token."""

    prefix: str
    state: WebhookState
    issued_at: str
    last_called_at: str | None
    last_call_address: str | None
    pending_calls: int

    @classmethod
    def of(cls, webhook: Webhook) -> "WebhookView":
        """Project a row, dropping the hash."""
        state: WebhookState = "active"
        if webhook.is_revoked:
            state = (
                "revoked_by_recovery" if webhook.revoked_reason == "account_recovery" else "revoked"
            )
        return cls(
            webhook.token_prefix,
            state,
            webhook.issued_at,
            webhook.last_called_at,
            webhook.last_call_address,
            webhook.pending_calls,
        )


class WebhookChangedSinceReview(ValidationFailed):
    """Raised when the webhook's token is not the one the caller reviewed."""

    def __init__(self) -> None:
        super().__init__({"webhook": CHANGED_SINCE_REVIEW})


async def issue_token(db: Session, write: Callable[[str, str], Awaitable[None]]) -> IssuedWebhook:
    """Write a fresh token's prefix and hash through write, retrying on a taken prefix.

    Runs inside the caller's transaction.
    """
    for _ in range(PREFIX_ATTEMPTS):
        value, prefix = new_token(WEBHOOK_TOKEN_MARK)
        try:
            async with db.savepoint():
                await write(prefix, hash_token(value))
        except DuplicateError:
            continue
        return IssuedWebhook(value, prefix)
    raise RuntimeError("could not allocate a unique webhook prefix")


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class WebhookCallLimiter:
    """Token buckets per webhook prefix, and the once-a-minute tally of refused calls."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._refused: dict[str, int] = {}
        self._logged_at: float | None = None

    def take(self, prefix: str) -> float | None:
        """Take a token; return None when admitted, else the seconds until one is available."""
        self._flush_if_due()
        now = self._clock()
        bucket = self._buckets.get(prefix)
        if bucket is None:
            bucket = _Bucket(float(CALL_BURST), now)
            self._buckets[prefix] = bucket
            if len(self._buckets) > MAX_TRACKED_WEBHOOKS:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(prefix)
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(float(CALL_BURST), bucket.tokens + elapsed / CALL_REFILL_SECONDS)
            bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return None
        return (1.0 - bucket.tokens) * CALL_REFILL_SECONDS

    def refuse(self, answer: str) -> None:
        """Count a refused call; log the counts since the last line, at most once a minute."""
        self._refused[answer] = self._refused.get(answer, 0) + 1
        self._flush_if_due()

    def _flush_if_due(self) -> None:
        if not self._refused:
            return
        now = self._clock()
        if self._logged_at is not None and now - self._logged_at < LOG_INTERVAL_SECONDS:
            return
        self._logged_at = now
        counts, self._refused = self._refused, {}
        logger.warning("webhook_refused", extra={"counts": counts})


class Outcome(Enum):
    """How a call ended."""

    ACCEPTED = "accepted"
    ALREADY_PENDING = "already_pending"
    NOT_FOUND = "not_found"
    USE_HTTPS = "use_https"
    QUERY_NOT_SUPPORTED = "query_not_supported"
    RATE_LIMITED = "rate_limited"
    NOT_ACCEPTING = "not_accepting"


@dataclass(frozen=True, slots=True)
class CallResult:
    """The answer to a call; reason is set for NOT_ACCEPTING, retry_after for RATE_LIMITED."""

    outcome: Outcome
    reason: str | None = None
    retry_after: int | None = None


class WebhookService:
    """Answers inbound calls, and rotates and revokes a rule's webhook token."""

    def __init__(
        self,
        *,
        db: Session,
        webhooks: WebhookRepository,
        rules: AutomationRuleRepository,
        jobs: ScheduledJobRepository,
        users: UserRepository,
        settings_repo: SettingRepository,
        subsystems: HandlerRegistry | None,
        limiter: WebhookCallLimiter,
        activity: ActivityRecorder,
        clock: Clock,
        poke: Callable[[], None],
        tls_enabled: bool,
        authorise_write: AuthoriseWrite | None = None,
    ) -> None:
        self._db = db
        self._webhooks = webhooks
        self._rules = rules
        self._jobs = jobs
        self._users = users
        self._settings = settings_repo
        self._subsystems = subsystems
        self._limiter = limiter
        self._activity = activity
        self._clock = clock
        self._poke = poke
        self._tls_enabled = tls_enabled
        self._authorise_write = authorise_write

    async def call(
        self, presented: str | None, *, address: str, over_tls: bool, has_query: bool = False
    ) -> CallResult:
        """Answer a call: arm the webhook's job for one run, or say why not.

        Transport and query are refused before anything is read. Every unusable token gives the
        same NOT_FOUND, whatever is wrong with it.
        """
        if self._tls_enabled and not over_tls:
            return self._refused(Outcome.USE_HTTPS)
        if has_query:
            return self._refused(Outcome.QUERY_NOT_SUPPORTED)
        webhook = await self._authenticate(presented)
        if webhook is None:
            return self._refused(Outcome.NOT_FOUND)
        wait = self._limiter.take(webhook.token_prefix)
        if wait is not None:
            return self._refused(Outcome.RATE_LIMITED, retry_after=max(1, math.ceil(wait)))
        result = await self._arm(webhook.rule_id, address)
        if result.outcome is Outcome.NOT_ACCEPTING:
            return self._refused(result.outcome, reason=result.reason)
        return result

    async def describe(self, rule_id: int) -> WebhookView | None:
        """Return a rule's webhook for its page; the caller has already authorised the rule."""
        webhook = await self._webhooks.get(rule_id)
        return None if webhook is None else WebhookView.of(webhook)

    async def rotate(self, actor: Principal, rule_id: int, expected_prefix: str) -> IssuedWebhook:
        """Replace the owner's token; the previous one stops working at once.

        Raises ResourceNotFound for anyone but the owner, whatever the owner can no longer write
        is refused as enabling the rule would be, and WebhookChangedSinceReview when the token is
        no longer the one with expected_prefix. The returned value is shown once.
        """
        rule = await self._owned_rule(actor, rule_id, owner_only=True)
        if self._authorise_write is None:
            raise PermissionDenied("insufficient access")
        await self._authorise_write(actor, rule)
        return await self._rotate(
            actor, rule_id, environment_id=rule.environment_id, expected_prefix=expected_prefix
        )

    async def revoke(self, actor: Principal, rule_id: int, expected_prefix: str) -> None:
        """Revoke a token for the owner or an administrator; a revoked token stays revoked.

        Raises ResourceNotFound for anyone else, WebhookChangedSinceReview when the token is no
        longer the one with expected_prefix.
        """
        rule = await self._owned_rule(actor, rule_id, owner_only=False)
        await self._revoke(
            actor, rule_id, environment_id=rule.environment_id, expected_prefix=expected_prefix
        )

    @audited(
        action="webhook_token_rotated",
        target_type="automation_rule",
        target_id=lambda a: str(a["rule_id"]),
        environment_id=lambda a: a["environment_id"],
        category="authentication",
    )
    async def _rotate(
        self,
        actor: Principal,
        rule_id: int,
        *,
        environment_id: int | None,
        expected_prefix: str,
    ) -> IssuedWebhook:
        audit = current_audit()
        async with self._db.transaction():
            rule = await self._rules.get(rule_id)
            webhook = await self._webhooks.get(rule_id)
            if rule is None or webhook is None or rule.created_by_user_id != actor.user_id:
                raise ResourceNotFound(f"automation {rule_id} does not exist")
            if webhook.token_prefix != expected_prefix:
                audit.reason = "changed_since_review"
                raise WebhookChangedSinceReview

            async def write(prefix: str, token_hash: str) -> None:
                await self._webhooks.rotate(
                    rule_id, expected_prefix=expected_prefix, prefix=prefix, token_hash=token_hash
                )

            issued = await issue_token(self._db, write)
            audit.detail.update(
                name=rule.name, prefix=issued.prefix, previous_prefix=expected_prefix
            )
        return issued

    @audited(
        action="webhook_token_revoked",
        target_type="automation_rule",
        target_id=lambda a: str(a["rule_id"]),
        environment_id=lambda a: a["environment_id"],
        category="authentication",
    )
    async def _revoke(
        self,
        actor: Principal,
        rule_id: int,
        *,
        environment_id: int | None,
        expected_prefix: str,
    ) -> None:
        audit = current_audit()
        reason: RevokeReason = "manual"
        async with self._db.transaction():
            rule = await self._rules.get(rule_id)
            webhook = await self._webhooks.get(rule_id)
            if rule is None or webhook is None:
                raise ResourceNotFound(f"automation {rule_id} does not exist")
            if rule.created_by_user_id != actor.user_id and not actor.is_administrator:
                raise ResourceNotFound(f"automation {rule_id} does not exist")
            if webhook.token_prefix != expected_prefix:
                audit.reason = "changed_since_review"
                raise WebhookChangedSinceReview
            if not await self._webhooks.revoke(
                rule_id, expected_prefix=expected_prefix, reason=reason
            ):
                audit.unchanged()
                return
            audit.detail.update(name=rule.name, prefix=expected_prefix, reason=reason)

    async def _owned_rule(
        self, actor: Principal, rule_id: int, *, owner_only: bool
    ) -> AutomationRule:
        rule = await self._rules.get(rule_id)
        owned = rule is not None and rule.created_by_user_id == actor.user_id
        if rule is None or not (owned or (actor.is_administrator and not owner_only)):
            raise ResourceNotFound(f"automation {rule_id} does not exist")
        return rule

    def _refused(
        self, outcome: Outcome, *, reason: str | None = None, retry_after: int | None = None
    ) -> CallResult:
        self._limiter.refuse(outcome.value)
        return CallResult(outcome, reason, retry_after)

    async def _authenticate(self, presented: str | None) -> Webhook | None:
        """Return the webhook a presented token opens, or None for any reason it does not."""
        if presented is None:
            return None
        prefix = token_prefix(presented)
        if prefix is None:
            return None
        webhook = await self._webhooks.get_by_prefix(prefix)
        stored = (
            _ABSENT_HASH if webhook is None or webhook.token_hash is None else webhook.token_hash
        )
        # Hashed and compared whether or not the prefix exists, so the work does not tell.
        matches = hmac.compare_digest(stored, hash_token(presented))
        if webhook is None or webhook.token_hash is None or not matches:
            return None
        rule = await self._rules.get(webhook.rule_id)
        if rule is None or rule.created_by_user_id is None:
            return None
        owner = await self._users.get(rule.created_by_user_id)
        if owner is None or not owner.is_enabled:
            return None
        return webhook

    async def _arm(self, rule_id: int, address: str) -> CallResult:
        now = self._clock()
        async with self._db.transaction():
            rule = await self._rules.get(rule_id)
            job = await self._jobs.get_for_rule(rule_id)
            if rule is None or job is None:
                return CallResult(Outcome.NOT_ACCEPTING, "unavailable")
            state = job_state(
                job,
                rule,
                running=False,
                subsystem_enabled=await read_subsystem_enabled(self._settings, WEBHOOK_SUBSYSTEM),
                available=self._subsystems is not None
                and self._subsystems.get(WEBHOOK_SUBSYSTEM) is not None,
                now=now,
            )
            reason = _REFUSAL_REASONS.get(state)
            if reason is not None:
                return CallResult(Outcome.NOT_ACCEPTING, reason)
            started = await self._jobs.last_run_started(job.id)
            not_before = now
            if started is not None:
                not_before = max(now, parse_timestamp(started) + WEBHOOK_RUN_SPACING)
            armed = await self._jobs.arm(job.id, format_timestamp(not_before))
            if not armed and job.next_run_at is None:
                return CallResult(Outcome.NOT_ACCEPTING, "unavailable")
            fresh_count = armed and not await self._jobs.newest_run_open(job.id)
            await self._webhooks.record_call(rule_id, address, armed=fresh_count)
        if armed:
            self._poke()
        return CallResult(Outcome.ACCEPTED if armed else Outcome.ALREADY_PENDING)
