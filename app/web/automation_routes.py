"""Automation pages: rules, their runs and the per-object panel."""

import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ProductError, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.config import Settings, get_settings
from app.db.repositories.automation_rule import AutomationRule
from app.db.repositories.pagination import MAX_PAGE_SIZE, InvalidCursorError
from app.db.repositories.resource_control import canonical_stack_key
from app.db.repositories.stack import StackKind
from app.scheduler.loop import SchedulerBusy
from app.scheduler.setup import SchedulerRuntime
from app.services.access import is_canonical_id
from app.services.activity import SUBSYSTEMS
from app.services.assembly import (
    Environments,
    EnvironmentServices,
    Jobs,
    JobServices,
    Stacks,
    StackServices,
    TargetAdmin,
    get_scheduler_runtime,
)
from app.services.automation_rules import TARGETED_SUBSYSTEMS, RuleView
from app.services.jobs import NOT_AVAILABLE, PAUSE_OPTIONS
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_storage_admin import ObjectStorageTargetService
from app.services.stack_document import STACK_NAME, SWARM_STACK_NAME
from app.services.stack_views import ManagedStackName
from app.services.webhooks import WEBHOOK_SUBSYSTEM
from app.subsystems.base import Subsystem
from app.subsystems.container_updater import NAME as UPDATER
from app.subsystems.selectors import MAX_LABEL_TERMS, MAX_TARGET_NAMES
from app.web.automation_webhook_routes import Pages as Webhooks
from app.web.flash import Flash
from app.web.job_routes import (
    FILTER_MAX,
    NOTICE_MAX,
    POLICY_LABELS,
    RUN_COLUMNS,
    Limit,
    RunId,
    RunRow,
    bad_cursor,
    blocked_reason,
    dry_run_offered,
    html_fragment,
    html_page,
    path_identifier,
    run_row,
)
from app.web.object_pages import Failure, problem_page, redirect, route
from app.web.origins import RequestOrigin
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.templating import is_htmx_request

router = APIRouter(prefix="/automation", dependencies=[Depends(require_user)])

AUTOMATION_PATH: Final = "/automation"
TABLE: Final = "automation/_table.html"
RUNS: Final = "automation/_runs.html"
RUN_REGION: Final = "automation/_run_region.html"
FIELD_MAX: Final = 256
LABELS_MAX: Final = 2048
EXPRESSION_MAX: Final = 160
ID_DIGITS: Final = 18
RETURN_MAX: Final = 80
AUTOMATION_TAB: Final = "tab=automation"
PARAMETER_KEY_MAX: Final = 64
MAX_POSTED_PARAMETERS: Final = 40
LISTED_STACKS: Final = 200
MAX_POSTED_NAMES: Final = 100
ROW_ACTION_MAX: Final = 16
ROW_ADD: Final = "add"
ROW_REMOVE: Final = "remove-"
TARGETS_FRAGMENT: Final = "automation/_target_rows.html"
NAME_KINDS: Final = ("container", "stack", "service")
LABEL_KINDS: Final = ("labels", "service_labels")
UNUSED_TARGET_FIELDS: Final = {
    "labels": "containers with labels",
    "service_labels": "Swarm services with labels",
    "environment": "every container in the environment",
}
DEFAULT_INTERVAL_SECONDS: Final = 300
DEFAULT_TIME_ZONE: Final = "UTC"
TARGET_PARAMETER: Final = "object_storage_target_id"
KIND_LABELS: Final = {
    "interval": "Fixed interval",
    "cron": "Cron expression",
    "webhook": "When called",
}
TARGET_LABELS: Final = {
    "container": "Container",
    "stack": "Compose stack",
    "labels": "Containers with labels",
    "service": "Swarm service",
    "service_labels": "Services with labels",
    "environment": "Every container in the environment",
}
BOTH_STACK_KINDS: Final = frozenset({"compose", "swarm"})
STACK_LABEL: Final = "Stack"
FORM_FIELD_NAMES: Final = {
    "name": "name",
    "target_selector": "target",
    "schedule": "schedule_expression",
    "time_zone": "time_zone",
    "overrun_policy": "overrun_policy",
    "parameters": "parameters",
    "environment": "environment_id",
    "subsystem": "subsystem",
}

INSTANCE_TARGET: Final = "This instance"

NOTICES: Final = {
    "created": "The automation was created and is disabled. Run a dry run, then enable it.",
    "updated": "The automation is saved.",
    "enabled": "The automation is enabled.",
    "disabled": "The automation is disabled.",
    "paused": "The automation is paused.",
    "resumed": "The automation is resumed.",
    "started": "The run has started.",
    "dry_run": "The dry run has started. It changes no container, stack or automation.",
    "deleted": "The automation is deleted.",
    "released": "The container is released. Its restart budget starts again.",
    "updated_disabled": "The automation is saved and disabled. Review it, then enable it again.",
}

RULE_COLUMNS: Final = (
    Column("name", "Name"),
    Column("environment", "Environment", optional=True),
    Column("target", "Target"),
    Column("schedule", "Schedule", optional=True),
    Column("state", "State", width="badge"),
    Column("next_run", "Next run", optional=True, width="time"),
    Column("last_run", "Last run", width="time"),
    Column("updates", "Updates", optional=True, width="count", wide_only=True),
    Column("recent", "Recent runs", optional=True, width="count", wide_only=True),
)
OWNER_COLUMN: Final = Column("owner", "Owner", optional=True)
Text = Annotated[str, Form(max_length=FIELD_MAX)]
Scheduling = Annotated[SchedulerRuntime, Depends(get_scheduler_runtime)]


def rule_id_of(rule_id: str) -> int:
    """Read the rule id of the path; anything that is not a plain number is a missing rule."""
    return path_identifier(rule_id)


def held_container_of(container_id: str) -> str:
    """Read the container id of the path; anything that cannot be one is a missing container."""
    if not is_canonical_id("container", container_id):
        raise HTTPException(status_code=404)
    return container_id


RuleId = Annotated[int, Depends(rule_id_of)]
HeldContainerId = Annotated[str, Depends(held_container_of)]


@dataclass(frozen=True, slots=True)
class SubsystemInfo:
    """What a subsystem accepts in a rule, read from its handler."""

    name: str
    schedule_kinds: tuple[str, ...]
    min_interval: int
    overrun_policies: tuple[str, ...]
    default_overrun: str
    target_kinds: tuple[str, ...]
    stack_kinds: tuple[str, ...]
    requires_environment: bool
    administrators_only: bool
    supports_dry_run: bool
    parameter_fields: tuple[str, ...]

    @property
    def acts_on_swarm_stacks(self) -> bool:
        """Report whether a stack target of this automation may name a Swarm stack."""
        return "swarm" in self.stack_kinds

    def target_label(self, kind: str) -> str:
        """Name a target kind; a stack is a Compose stack unless both stack kinds are acted on."""
        if kind == "stack" and set(self.stack_kinds) >= BOTH_STACK_KINDS:
            return STACK_LABEL
        return TARGET_LABELS[kind]


@dataclass(frozen=True, slots=True)
class StorageTargetRef:
    """The object storage target a rule names; name is none once the target is deleted."""

    id: int
    name: str | None


@dataclass(frozen=True, slots=True)
class EnvironmentOption:
    """An environment the viewer may pick."""

    id: int
    name: str
    is_local: bool
    is_swarm: bool = False


@dataclass(slots=True)
class RuleInput:
    """The rule form as posted."""

    subsystem: str
    environment_id: str
    name: str
    target_kind: str
    target_names: list[str]
    target_labels: str
    target_rows: str
    schedule_kind: str
    schedule_expression: str
    time_zone: str
    overrun_policy: str
    parameters: dict[str, str]


@dataclass(slots=True)
class FormState:
    """What the rule form shows: the values and the errors beside them."""

    name: str = ""
    target_kind: str = "container"
    target_names: list[str] = field(default_factory=lambda: [""])
    target_labels: str = ""
    schedule_kind: str = "interval"
    schedule_expression: str = ""
    time_zone: str = DEFAULT_TIME_ZONE
    overrun_policy: str = ""
    parameters: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


async def read_rule_form(
    request: Request,
    subsystem: Text = "",
    environment_id: Annotated[str, Form(max_length=ID_DIGITS)] = "",
    name: Text = "",
    target_kind: Text = "",
    target_labels: Annotated[str, Form(max_length=LABELS_MAX)] = "",
    target_rows: Annotated[str, Form(max_length=ROW_ACTION_MAX)] = "",
    schedule_kind: Text = "",
    schedule_expression: Annotated[str, Form(max_length=EXPRESSION_MAX)] = "",
    time_zone: Text = "",
    overrun_policy: Text = "",
) -> RuleInput:
    """Read the posted rule; subsystem parameters arrive as `param_<name>` fields.

    The target names arrive as repeated `target_name` fields, one per row.
    """
    posted = await request.form()
    names = [
        value[:FIELD_MAX]
        for value in posted.getlist("target_name")[:MAX_POSTED_NAMES]
        if isinstance(value, str)
    ]
    parameters: dict[str, str] = {}
    for key, value in posted.items():
        if len(parameters) >= MAX_POSTED_PARAMETERS:
            break
        if (
            key.startswith("param_")
            and len(key) <= PARAMETER_KEY_MAX
            and isinstance(value, str)
            and len(value) <= FIELD_MAX
        ):
            parameters[key.removeprefix("param_")] = value.strip()
    return RuleInput(
        subsystem=subsystem.strip(),
        environment_id=environment_id.strip(),
        name=name,
        target_kind=target_kind.strip(),
        target_names=names,
        target_labels=target_labels,
        target_rows=target_rows.strip(),
        schedule_kind=schedule_kind.strip(),
        schedule_expression=schedule_expression,
        time_zone=time_zone.strip(),
        overrun_policy=overrun_policy.strip(),
        parameters=parameters,
    )


Submitted = Annotated[RuleInput, Depends(read_rule_form)]


def _rule_href(request: Request, rule_id: int) -> str:
    return route(request, "automation_rule_detail", rule_id=rule_id)


def _rule_redirect(
    request: Request, rule_id: int, notice: str, href: str | None = None
) -> Response:
    return redirect(request, href or _rule_href(request, rule_id), [Flash("ok", NOTICES[notice])])


def _positive_int(text: str) -> int | None:
    stripped = text.strip()
    if stripped.isascii() and stripped.isdigit() and len(stripped) <= ID_DIGITS:
        value = int(stripped)
        return value if value >= 1 else None
    return None


def _info_of(handler: object) -> SubsystemInfo | None:
    if not isinstance(handler, Subsystem) or handler.parameters_model is None:
        return None
    kinds = tuple(kind for kind in TARGET_LABELS if kind in handler.target_kinds)
    return SubsystemInfo(
        name=handler.name,
        schedule_kinds=tuple(sorted(handler.schedule_kinds)),
        min_interval=handler.min_interval_seconds,
        overrun_policies=tuple(sorted(handler.allowed_overrun_policies)),
        default_overrun=handler.overrun_policy,
        target_kinds=kinds,
        stack_kinds=tuple(sorted(handler.stack_kinds)),
        requires_environment=handler.requires_environment,
        administrators_only=handler.administrators_only,
        supports_dry_run=handler.supports_dry_run,
        parameter_fields=tuple(handler.parameters_model.model_fields),
    )


def _offered(scheduling: SchedulerRuntime, actor: Principal) -> dict[str, SubsystemInfo]:
    """Return the subsystems a rule can be created for through these pages, by name.

    A subsystem whose rules take no environment, or that only administrators may use, is offered
    to administrators only.
    """
    found: dict[str, SubsystemInfo] = {}
    for name in sorted(SUBSYSTEMS):
        if not SUBSYSTEMS[name]:
            continue
        info = _info_of(scheduling.handlers.get(name))
        if info is None:
            continue
        restricted = info.administrators_only or not info.requires_environment
        if restricted and not actor.is_administrator:
            continue
        found[name] = info
    return found


def _info_for(scheduling: SchedulerRuntime, name: str) -> SubsystemInfo:
    info = _info_of(scheduling.handlers.get(name)) if SUBSYSTEMS.get(name) else None
    if info is None:
        raise ValidationFailed({"subsystem": NOT_AVAILABLE})
    return info


async def _environment_options(
    environments: EnvironmentServices, actor: Principal, *, creating: bool
) -> list[EnvironmentOption]:
    page = await environments.environments.dashboard(actor, limit=MAX_PAGE_SIZE)
    return [
        EnvironmentOption(row.id, row.name, row.is_local, row.type == "swarm")
        for row in page.items
        if not (creating and row.is_local and not actor.is_administrator)
    ]


async def _managed_stacks(
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    *,
    kind: StackKind | None,
    needle: str | None = None,
) -> list[ManagedStackName]:
    try:
        found = await stacks.queries.managed_names(
            actor, environment_id, kind=kind, q=needle, limit=LISTED_STACKS
        )
    except ResourceNotFound:
        return []
    return list(found)


async def _stack_kind(
    stacks: StackServices, actor: Principal, view: RuleView, info: SubsystemInfo | None
) -> str | None:
    """Return `compose` or `swarm` when every stack a rule names is a managed stack of that kind.

    None when a name is not a managed stack the viewer can see, or the kinds differ.
    """
    selector = _stored_object(view.rule.target_selector)
    names = _names_of(selector)
    environment_id = view.rule.environment_id
    if (
        info is None
        or not info.acts_on_swarm_stacks
        or selector.get("kind") != "stack"
        or not names
        or environment_id is None
    ):
        return None
    kinds: set[str] = set()
    for name in names:
        key = canonical_stack_key(name)
        found = await _managed_stacks(
            stacks, actor, environment_id, kind=None, needle=name.casefold()
        )
        kind = next((stack.kind for stack in found if canonical_stack_key(stack.name) == key), None)
        if kind is None:
            return None
        kinds.add(kind)
    return kinds.pop() if len(kinds) == 1 else None


def _names_of(selector: dict[str, Any]) -> list[str]:
    names = selector.get("names")
    if not isinstance(names, list):
        return []
    return [name for name in names if isinstance(name, str)]


def _labels_text(lines: str) -> list[list[str | None]]:
    terms: list[list[str | None]] = []
    for line in lines.splitlines():
        text = line.strip()
        if not text:
            continue
        if len(terms) >= MAX_LABEL_TERMS:
            raise ValidationFailed({"target_selector": f"Use at most {MAX_LABEL_TERMS} labels."})
        key, separator, value = text.partition("=")
        terms.append([key.strip(), value.strip() if separator else None])
    return terms


def _typed_names(form: RuleInput) -> list[str]:
    return [name.strip() for name in form.target_names if name.strip()]


def _refuse_unused_fields(form: RuleInput) -> None:
    """Refuse a value in a target field the chosen kind does not use, naming the field."""
    kind = form.target_kind
    if form.target_labels.strip() and (kind in NAME_KINDS or kind == "environment"):
        raise ValidationFailed(
            {
                "target_selector": "Labels is only used when the automation acts on containers "
                "with labels. Clear it, or choose that target."
            }
        )
    unused = UNUSED_TARGET_FIELDS.get(kind)
    if unused is not None and _typed_names(form):
        raise ValidationFailed(
            {
                "target_selector": f"Target name is not used when the automation acts on {unused}. "
                "Clear it, or choose a target that takes names."
            }
        )


def _selector_text(form: RuleInput) -> str:
    _refuse_unused_fields(form)
    data: dict[str, object]
    match form.target_kind:
        case "container" | "stack" | "service":
            data = {"kind": form.target_kind, "names": _typed_names(form)}
        case "labels" | "service_labels":
            data = {"kind": form.target_kind, "labels": _labels_text(form.target_labels)}
        case "environment":
            data = {"kind": "environment"}
        case _:
            raise ValidationFailed({"target_selector": "Choose what the automation acts on."})
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _rows_after(names: list[str], action: str) -> list[str]:
    """Apply an add or remove button to the rows as typed; a row is never left out entirely."""
    rows = list(names) or [""]
    if action == ROW_ADD:
        if len(rows) < MAX_TARGET_NAMES:
            rows.append("")
    elif action.startswith(ROW_REMOVE):
        index = action.removeprefix(ROW_REMOVE)
        if index.isascii() and index.isdigit() and int(index) < len(rows):
            del rows[int(index)]
    return rows or [""]


def _arguments(info: SubsystemInfo, form: RuleInput) -> dict[str, Any]:
    return {
        "name": form.name,
        "target_selector": _selector_text(form) if info.requires_environment else "",
        "parameters": {
            key: value
            for key, value in form.parameters.items()
            if key in info.parameter_fields and value
        },
        "schedule_kind": form.schedule_kind,
        "schedule_expression": form.schedule_expression,
        "time_zone": form.time_zone or DEFAULT_TIME_ZONE,
        "overrun_policy": form.overrun_policy or None,
    }


def _state_of(form: RuleInput) -> FormState:
    return FormState(
        name=form.name,
        target_kind=form.target_kind or "container",
        target_names=_rows_after(form.target_names, form.target_rows),
        target_labels=form.target_labels,
        schedule_kind=form.schedule_kind or "interval",
        schedule_expression=form.schedule_expression,
        time_zone=form.time_zone or DEFAULT_TIME_ZONE,
        overrun_policy=form.overrun_policy,
        parameters=dict(form.parameters),
    )


def _stored_object(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def parameter_rows(rule: AutomationRule) -> list[tuple[str, str]]:
    """List a rule's parameters that hold a value, by name."""
    return sorted(
        (key, str(value))
        for key, value in _stored_object(rule.parameters).items()
        if value is not None
    )


def _target_kind(rule: AutomationRule) -> str | None:
    kind = _stored_object(rule.target_selector).get("kind")
    return kind if isinstance(kind, str) else None


def _state_from_rule(view: RuleView, info: SubsystemInfo) -> FormState:
    rule, job = view.rule, view.job
    selector = _stored_object(rule.target_selector)
    kind = selector.get("kind")
    state = FormState(
        name=rule.name,
        target_kind=kind if kind in TARGET_LABELS else "container",
        schedule_kind=job.schedule_kind,
        schedule_expression=job.schedule_expression,
        time_zone=job.time_zone,
        overrun_policy=job.overrun_policy if len(info.overrun_policies) > 1 else "",
        parameters=dict(parameter_rows(rule)),
    )
    names = _names_of(selector)
    if kind in NAME_KINDS:
        state.target_names = names or [""]
    elif kind in LABEL_KINDS:
        terms = selector.get("labels")
        if isinstance(terms, list):
            state.target_labels = "\n".join(
                term[0] if len(term) < 2 or term[1] is None else f"{term[0]}={term[1]}"
                for term in terms
                if isinstance(term, list) and term and isinstance(term[0], str)
            )
    return state


def _default_state(info: SubsystemInfo, prefill: dict[str, str]) -> FormState:
    kind = prefill.get("target_kind", "")
    target = prefill.get("target", "")
    project = prefill.get("stack", "")
    state = FormState(
        schedule_kind="interval"
        if "interval" in info.schedule_kinds or not info.schedule_kinds
        else info.schedule_kinds[0],
        schedule_expression=str(max(info.min_interval, DEFAULT_INTERVAL_SECONDS)),
    )
    if info.name == UPDATER and project and "stack" in info.target_kinds:
        state.target_kind = "stack"
        state.target_names = [project]
    elif kind in info.target_kinds:
        state.target_kind = kind
        if kind in NAME_KINDS:
            state.target_names = [target]
    elif info.target_kinds:
        state.target_kind = info.target_kinds[0]
    return state


def _field_errors(invalid: ValidationFailed) -> dict[str, str]:
    return {FORM_FIELD_NAMES.get(key, key): message for key, message in invalid.fields.items()}


def _failure_page(
    request: Request, heading: str, rule_id: int, refusal: ProductError, *, title: str
) -> Response:
    if isinstance(refusal, ValidationFailed):
        status, message = 422, " ".join(refusal.fields.values())
    elif isinstance(refusal, SchedulerBusy):
        status, message = 409, str(refusal)
    else:
        status, message = 503, str(refusal)
    return problem_page(
        request,
        heading=heading,
        failure=Failure(status, title, message),
        back_href=_rule_href(request, rule_id),
        back_label="Back to the automation",
        region=None,
    )


REFUSALS: Final = (ValidationFailed, SchedulerBusy, EnvironmentUnavailable, ObjectTooLarge)


async def _storage_choices(
    targets: ObjectStorageTargetService, actor: Principal, info: SubsystemInfo
) -> list[tuple[int, str]]:
    """List the object storage targets a rule of this subsystem can send to."""
    return await targets.summaries(actor) if info.name in TARGETED_SUBSYSTEMS else []


async def _storage_ref(
    targets: ObjectStorageTargetService, actor: Principal, rule: AutomationRule
) -> StorageTargetRef | None:
    """Name the target a rule sends to; none for a rule that names no target."""
    value = dict(parameter_rows(rule)).get(TARGET_PARAMETER, "")
    target_id = _positive_int(value)
    if target_id is None:
        return None
    try:
        found = await targets.get(actor, target_id)
    except ResourceNotFound:
        return StorageTargetRef(target_id, None)
    return StorageTargetRef(target_id, found.name)


async def _form_page(
    request: Request,
    targets: ObjectStorageTargetService,
    actor: Principal,
    *,
    info: SubsystemInfo,
    environment: EnvironmentOption | None,
    state: FormState,
    rule_id: int | None = None,
    rule_name: str = "",
    banner: str | None = None,
    status_code: int = 200,
) -> Response:
    editing = rule_id is not None
    swarm = environment is not None and environment.is_swarm
    kinds = [
        kind
        for kind in info.target_kinds
        if kind != "service_labels" or swarm or state.target_kind == kind
    ]
    return html_page(
        request,
        "automation/form.html",
        {
            "info": info,
            "environment": environment,
            "state": state,
            "editing": editing,
            "rule_id": rule_id,
            "rule_name": rule_name,
            "max_target_names": MAX_TARGET_NAMES,
            "storage_targets": await _storage_choices(targets, actor, info),
            "banner": banner,
            "target_options": [(kind, info.target_label(kind)) for kind in kinds],
            "offers_service_labels": "service_labels" in kinds,
            "stack_label": (
                "Stack name" if info.target_label("stack") == STACK_LABEL else "Compose stack name"
            ),
            "schedule_options": [(kind, KIND_LABELS[kind]) for kind in info.schedule_kinds],
            "policy_options": [(policy, POLICY_LABELS[policy]) for policy in info.overrun_policies],
            "submit_href": (
                route(request, "automation_rule_update", rule_id=rule_id)
                if rule_id is not None
                else route(request, "automation_rule_create")
            ),
            "cancel_href": (
                _rule_href(request, rule_id)
                if rule_id is not None
                else route(request, "automation")
            ),
        },
        status_code,
    )


async def _choose_page(
    request: Request,
    environments: EnvironmentServices,
    scheduling: SchedulerRuntime,
    actor: Principal,
    *,
    subsystem: str = "",
    environment_id: str = "",
    prefill: dict[str, str],
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return html_page(
        request,
        "automation/new_choose.html",
        {
            "subsystems": list(_offered(scheduling, actor).values()),
            "environments": await _environment_options(environments, actor, creating=True),
            "subsystem": subsystem,
            "environment_id": environment_id,
            "prefill": prefill,
            "errors": errors or {},
        },
        status_code,
    )


@router.get("", response_class=HTMLResponse, name="automation")
async def overview(
    request: Request,
    services: Jobs,
    environments: Environments,
    actor: CurrentUser,
    subsystem: Annotated[str, Query(max_length=FILTER_MAX)] = "",
    environment_id: Annotated[str, Query(max_length=ID_DIGITS)] = "",
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """List the rules the viewer manages: their own, or every rule for an administrator."""
    options = await _environment_options(environments, actor, creating=False)
    names = sorted(name for name, takes_rules in SUBSYSTEMS.items() if takes_rules)
    chosen = subsystem.strip() or None
    wanted = _positive_int(environment_id)
    errors: dict[str, str] = {}
    if chosen is not None and chosen not in names:
        errors["subsystem"] = "Choose a known subsystem."
        chosen = None
    if environment_id.strip() and (wanted is None or wanted not in {o.id for o in options}):
        errors["environment_id"] = "Choose one of the listed environments."
        wanted = None
    try:
        page = await services.rules.overview(
            actor, limit=limit, cursor=cursor, subsystem=chosen, environment_id=wanted
        )
    except (InvalidCursorError, TypeError):
        raise bad_cursor() from None
    params = [("subsystem", chosen)] if chosen else []
    if wanted is not None:
        params.append(("environment_id", str(wanted)))
    pager = PageView.of(page, base_path=AUTOMATION_PATH, limit=limit, cursor=cursor, params=params)
    columns = (
        (RULE_COLUMNS[0], OWNER_COLUMN, *RULE_COLUMNS[1:])
        if actor.is_administrator
        else RULE_COLUMNS
    )
    filtered = chosen is not None or wanted is not None
    total = await services.rules.overview_count(actor, subsystem=chosen, environment_id=wanted)
    if is_htmx_request(request) and not errors:
        return html_fragment(
            request,
            TABLE,
            "rules_table",
            rows=page.items,
            columns=columns,
            pager=pager,
            filtered=filtered,
            total=total,
            oob=True,
        )
    return html_page(
        request,
        "automation/overview.html",
        {
            "rows": page.items,
            "columns": columns,
            "pager": pager,
            "filtered": filtered,
            "total": total,
            "subsystem": chosen or "",
            "subsystems": names,
            "environment_id": "" if wanted is None else str(wanted),
            "environments": options,
            "errors": errors,
        },
        422 if errors else 200,
    )


@router.get("/rules/new", response_class=HTMLResponse, name="automation_rule_new")
async def new_rule(
    request: Request,
    environments: Environments,
    targets: TargetAdmin,
    scheduling: Scheduling,
    actor: CurrentUser,
    subsystem: Annotated[str, Query(max_length=FILTER_MAX)] = "",
    environment_id: Annotated[str, Query(max_length=ID_DIGITS)] = "",
    target_kind: Annotated[str, Query(max_length=FILTER_MAX)] = "",
    target: Annotated[str, Query(max_length=FIELD_MAX)] = "",
    stack: Annotated[str, Query(max_length=FIELD_MAX)] = "",
) -> Response:
    """Step one picks the subsystem and environment; step two takes the target and settings.

    `stack` names the Compose project of the object the page was opened from.
    """
    prefill = {"target_kind": target_kind, "target": target, "stack": stack}
    if not subsystem:
        return await _choose_page(
            request, environments, scheduling, actor, prefill=prefill, environment_id=environment_id
        )
    offered = _offered(scheduling, actor)
    options = {o.id: o for o in await _environment_options(environments, actor, creating=True)}
    errors: dict[str, str] = {}
    info = offered.get(subsystem)
    if info is None:
        errors["subsystem"] = "Choose one of the listed automations."
    wanted = _positive_int(environment_id)
    chosen = None if wanted is None else options.get(wanted)
    scoped = info is None or info.requires_environment
    if scoped and chosen is None:
        errors["environment_id"] = "Choose one of the listed environments."
    if errors or info is None or (info.requires_environment and chosen is None):
        return await _choose_page(
            request,
            environments,
            scheduling,
            actor,
            subsystem=subsystem,
            environment_id=environment_id,
            prefill=prefill,
            errors=errors,
            status_code=422,
        )
    return await _form_page(
        request,
        targets,
        actor,
        info=info,
        environment=chosen if info.requires_environment else None,
        state=_default_state(info, prefill),
    )


@router.post("/rules", name="automation_rule_create")
async def create_rule(
    request: Request,
    services: Jobs,
    environments: Environments,
    targets: TargetAdmin,
    scheduling: Scheduling,
    webhooks: Webhooks,
    actor: CurrentUser,
    form: Submitted,
) -> Response:
    """Create a rule, disabled, and open it; a webhook's URL is shown once instead."""
    prefill = {"target_kind": "", "target": "", "stack": ""}
    try:
        info = _info_for(scheduling, form.subsystem)
        chosen = None
        if info.requires_environment:
            options = {
                o.id: o for o in await _environment_options(environments, actor, creating=True)
            }
            wanted = _positive_int(form.environment_id)
            chosen = None if wanted is None else options.get(wanted)
            if chosen is None:
                raise ValidationFailed({"environment": "Choose one of the listed environments."})
    except ValidationFailed as invalid:
        return await _choose_page(
            request,
            environments,
            scheduling,
            actor,
            subsystem=form.subsystem,
            environment_id=form.environment_id,
            prefill=prefill,
            errors=_field_errors(invalid),
            status_code=422,
        )
    state = _state_of(form)
    if form.target_rows:
        return await _form_page(request, targets, actor, info=info, environment=chosen, state=state)
    issued = None
    try:
        if info.name == WEBHOOK_SUBSYSTEM:
            created = await services.rules.create_issuing(
                actor,
                subsystem=info.name,
                environment_id=None if chosen is None else chosen.id,
                **_arguments(info, form),
            )
            rule, issued = created.rule, created.webhook
        else:
            rule = await services.rules.create(
                actor,
                subsystem=info.name,
                environment_id=None if chosen is None else chosen.id,
                **_arguments(info, form),
            )
    except ValidationFailed as invalid:
        state.errors = _field_errors(invalid)
        return await _form_page(
            request,
            targets,
            actor,
            info=info,
            environment=chosen,
            state=state,
            status_code=422,
        )
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return await _form_page(
            request,
            targets,
            actor,
            info=info,
            environment=chosen,
            state=state,
            banner=str(unavailable),
            status_code=503,
        )
    if issued is not None:
        return await webhooks.token_page(request, rule, issued, rotated=False)
    return _rule_redirect(request, rule.id, "created")


async def _detail_page(
    request: Request,
    services: JobServices,
    targets: ObjectStorageTargetService,
    webhooks: Webhooks,
    actor: Principal,
    rule_id: int,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    status_code: int = 200,
    errors: dict[str, str] | None = None,
) -> Response:
    view = await services.rules.detail(actor, rule_id)
    try:
        page = await services.rules.runs(actor, rule_id, limit=limit, cursor=cursor)
    except (InvalidCursorError, TypeError):
        raise bad_cursor() from None
    pager = PageView.of(
        page,
        base_path=route(request, "automation_rule_detail", rule_id=rule_id),
        limit=limit,
        cursor=cursor,
    )
    rows: list[RunRow] = [run_row(run) for run in page.items]
    if is_htmx_request(request) and status_code == 200:
        return html_fragment(
            request,
            RUNS,
            "rule_runs_table",
            rule_id=rule_id,
            rows=rows,
            columns=RUN_COLUMNS,
            pager=pager,
        )
    return html_page(
        request,
        "automation/detail.html",
        {
            "view": view,
            "rows": rows,
            "columns": RUN_COLUMNS,
            "pager": pager,
            "parameters": parameter_rows(view.rule),
            "target_names": _names_of(_stored_object(view.rule.target_selector)),
            "storage_target": await _storage_ref(targets, actor, view.rule),
            "blocked": blocked_reason(view.state),
            "dry_run_offered": dry_run_offered(view.state, supported=view.supports_dry_run),
            "durations": PAUSE_OPTIONS,
            "policy_label": POLICY_LABELS.get(view.job.overrun_policy),
            "holds": await services.rules.holds(actor, rule_id),
            "waiting": await services.rules.waiting_images(actor, rule_id),
            "errors": errors or {},
            **await webhooks.section(request, view, actor),
        },
        status_code,
    )


@router.post("/rules/-/targets", response_class=HTMLResponse, name="automation_rule_targets")
async def target_rows(request: Request, form: Submitted) -> Response:
    """Render the target name rows of the rule form after an add or a remove; nothing is saved."""
    return html_fragment(
        request,
        TARGETS_FRAGMENT,
        "target_rows",
        names=_rows_after(form.target_names, form.target_rows),
        target_kind=form.target_kind or "container",
        subsystem=form.subsystem,
        environment_id=form.environment_id,
        max_names=MAX_TARGET_NAMES,
        error=None,
    )


@router.get(
    "/rules/{rule_id}",
    response_class=HTMLResponse,
    name="automation_rule_detail",
    dependencies=[RequestOrigin],
)
async def rule_detail(
    request: Request,
    services: Jobs,
    targets: TargetAdmin,
    webhooks: Webhooks,
    actor: CurrentUser,
    rule_id: RuleId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Response:
    """Show a rule with its actions and its runs, newest first."""
    return await _detail_page(
        request,
        services,
        targets,
        webhooks,
        actor,
        rule_id,
        limit=limit,
        cursor=cursor,
    )


@router.get("/rules/{rule_id}/edit", response_class=HTMLResponse, name="automation_rule_edit")
async def edit_rule(
    request: Request,
    services: Jobs,
    environments: Environments,
    targets: TargetAdmin,
    scheduling: Scheduling,
    actor: CurrentUser,
    rule_id: RuleId,
) -> Response:
    """Show a rule's name, target, settings and schedule for editing."""
    view = await services.rules.detail(actor, rule_id)
    info = _info_of(scheduling.handlers.get(view.rule.subsystem))
    if info is None:
        return problem_page(
            request,
            heading="Edit automation",
            failure=Failure(409, "The automation cannot be edited", NOT_AVAILABLE),
            back_href=_rule_href(request, rule_id),
            back_label="Back to the automation",
            region=None,
        )
    chosen = await _environment_of(view, info, environments, actor)
    return await _form_page(
        request,
        targets,
        actor,
        info=info,
        environment=chosen,
        state=_state_from_rule(view, info),
        rule_id=rule_id,
        rule_name=view.rule.name,
    )


async def _environment_of(
    view: RuleView, info: SubsystemInfo, environments: EnvironmentServices, actor: Principal
) -> EnvironmentOption | None:
    if not info.requires_environment:
        return None
    environment_id = view.rule.environment_id or 0
    known = next(
        (
            option
            for option in await _environment_options(environments, actor, creating=False)
            if option.id == environment_id
        ),
        None,
    )
    return EnvironmentOption(
        environment_id,
        view.environment_name or "All environments",
        False,
        known is not None and known.is_swarm,
    )


@router.post("/rules/{rule_id}", name="automation_rule_update")
async def update_rule(
    request: Request,
    services: Jobs,
    environments: Environments,
    targets: TargetAdmin,
    scheduling: Scheduling,
    actor: CurrentUser,
    rule_id: RuleId,
    form: Submitted,
) -> Response:
    """Save a rule's name, target, settings and schedule."""
    view = await services.rules.detail(actor, rule_id)
    info = _info_of(scheduling.handlers.get(view.rule.subsystem))
    if info is None:
        return _failure_page(
            request,
            "Edit automation",
            rule_id,
            ValidationFailed({"subsystem": NOT_AVAILABLE}),
            title="The automation was not saved",
        )
    chosen = await _environment_of(view, info, environments, actor)
    state = _state_of(form)
    if form.target_rows:
        return await _form_page(
            request,
            targets,
            actor,
            info=info,
            environment=chosen,
            state=state,
            rule_id=rule_id,
            rule_name=view.rule.name,
        )
    try:
        saved = await services.rules.update(actor, rule_id, **_arguments(info, form))
    except ValidationFailed as invalid:
        state.errors = _field_errors(invalid)
        return await _form_page(
            request,
            targets,
            actor,
            info=info,
            environment=chosen,
            state=state,
            rule_id=rule_id,
            rule_name=view.rule.name,
            status_code=422,
        )
    except (EnvironmentUnavailable, ObjectTooLarge) as unavailable:
        return await _form_page(
            request,
            targets,
            actor,
            info=info,
            environment=chosen,
            state=state,
            rule_id=rule_id,
            rule_name=view.rule.name,
            banner=str(unavailable),
            status_code=503,
        )
    notice = "updated_disabled" if view.rule.is_enabled and not saved.is_enabled else "updated"
    return _rule_redirect(request, rule_id, notice)


@router.get(
    "/rules/{rule_id}/enable", response_class=HTMLResponse, name="automation_rule_enable_confirm"
)
async def confirm_enable(
    request: Request,
    services: Jobs,
    stacks: Stacks,
    scheduling: Scheduling,
    actor: CurrentUser,
    rule_id: RuleId,
) -> Response:
    """Name what the rule will act on; nothing changes until it is confirmed."""
    try:
        preview = await services.rules.enable_preview(actor, rule_id)
    except ValidationFailed as refusal:
        return _failure_page(
            request, "Enable automation", rule_id, refusal, title="The automation cannot be enabled"
        )
    view = preview.view
    return html_page(
        request,
        "automation/enable_confirm.html",
        {
            "view": view,
            "parameters": parameter_rows(view.rule),
            "target": preview.target,
            "target_kind": _target_kind(view.rule),
            "stack_kind": await _stack_kind(
                stacks, actor, view, _info_of(scheduling.handlers.get(view.rule.subsystem))
            ),
            "digest": preview.digest,
        },
    )


@router.post("/rules/{rule_id}/enable", name="automation_rule_enable")
async def enable_rule(
    request: Request,
    services: Jobs,
    actor: CurrentUser,
    rule_id: RuleId,
    expected_updated_at: Annotated[str, Form(max_length=FIELD_MAX)] = "",
    expected_digest: Annotated[str, Form(max_length=FIELD_MAX)] = "",
) -> Response:
    """Enable a rule as it was reviewed, so its job runs on schedule."""
    try:
        await services.rules.enable(
            actor,
            rule_id,
            expected_updated_at=expected_updated_at,
            expected_digest=expected_digest,
        )
    except REFUSALS as refusal:
        return _failure_page(
            request, "Enable automation", rule_id, refusal, title="The automation was not enabled"
        )
    return _rule_redirect(request, rule_id, "enabled")


@router.post("/rules/{rule_id}/disable", name="automation_rule_disable")
async def disable_rule(
    request: Request, services: Jobs, actor: CurrentUser, rule_id: RuleId
) -> Response:
    """Disable a rule; its job stops firing and nothing is deleted."""
    await services.rules.disable(actor, rule_id)
    return _rule_redirect(request, rule_id, "disabled")


def _return_href(request: Request, rule: AutomationRule, return_to: str) -> str | None:
    """Map `container:<id>`, `stack:<name>`, `service:<id>` or `swarm_stack:<name>` to the page."""
    kind, _, value = return_to.partition(":")
    if rule.environment_id is None:
        return None
    page = _return_page(request, kind, value, rule.environment_id)
    return None if page is None else f"{page}?{AUTOMATION_TAB}"


def _return_page(request: Request, kind: str, value: str, environment_id: int) -> str | None:
    if kind == "service" and is_canonical_id("service", value):
        return route(
            request,
            "environment_swarm_service_detail",
            environment_id=environment_id,
            service_id=value,
        )
    if kind == "swarm_stack" and SWARM_STACK_NAME.fullmatch(value):
        return route(
            request,
            "environment_swarm_stack_detail",
            environment_id=environment_id,
            namespace=value,
        )
    if kind == "container" and is_canonical_id("container", value):
        return route(
            request,
            "environment_container_detail",
            environment_id=environment_id,
            container_id=value,
        )
    if kind == "stack" and STACK_NAME.fullmatch(value):
        return route(
            request,
            "environment_stack_detail",
            environment_id=environment_id,
            stack_name=value,
        )
    return None


@router.post("/rules/{rule_id}/pause", name="automation_rule_pause")
async def pause_rule(
    request: Request,
    services: Jobs,
    targets: TargetAdmin,
    webhooks: Webhooks,
    actor: CurrentUser,
    rule_id: RuleId,
    duration: Annotated[str, Form(max_length=NOTICE_MAX)] = "",
    return_to: Annotated[str, Form(max_length=RETURN_MAX)] = "",
) -> Response:
    """Pause a rule for a fixed time."""
    rule = await services.rules.get(actor, rule_id)
    try:
        await services.rules.pause(actor, rule_id, duration=duration)
    except ValidationFailed as invalid:
        return await _detail_page(
            request,
            services,
            targets,
            webhooks,
            actor,
            rule_id,
            status_code=422,
            errors=invalid.fields,
        )
    return _rule_redirect(request, rule_id, "paused", _return_href(request, rule, return_to))


@router.post("/rules/{rule_id}/resume", name="automation_rule_resume")
async def resume_rule(
    request: Request,
    services: Jobs,
    actor: CurrentUser,
    rule_id: RuleId,
    return_to: Annotated[str, Form(max_length=RETURN_MAX)] = "",
) -> Response:
    """Clear a rule's pause."""
    rule = await services.rules.get(actor, rule_id)
    try:
        await services.rules.resume(actor, rule_id)
    except REFUSALS as refusal:
        return _failure_page(
            request, "Resume automation", rule_id, refusal, title="The automation was not resumed"
        )
    return _rule_redirect(request, rule_id, "resumed", _return_href(request, rule, return_to))


@router.post("/rules/{rule_id}/holds/{container_id}/release", name="automation_rule_hold_release")
async def release_hold(
    request: Request,
    services: Jobs,
    actor: CurrentUser,
    rule_id: RuleId,
    container_id: HeldContainerId,
) -> Response:
    """Release a container the rule holds, so the rule may restart it again."""
    try:
        await services.rules.release_hold(actor, rule_id, container_id)
    except REFUSALS as refusal:
        return _failure_page(
            request, "Release container", rule_id, refusal, title="The container was not released"
        )
    return _rule_redirect(request, rule_id, "released")


@router.get("/rules/{rule_id}/run", response_class=HTMLResponse, name="automation_rule_run_confirm")
async def confirm_run(
    request: Request,
    services: Jobs,
    stacks: Stacks,
    scheduling: Scheduling,
    actor: CurrentUser,
    rule_id: RuleId,
) -> Response:
    """Name what a run now acts on; nothing starts until it is confirmed."""
    try:
        preview = await services.rules.run_preview(actor, rule_id)
    except ValidationFailed as refusal:
        return _failure_page(
            request, "Run automation", rule_id, refusal, title="The automation cannot be run"
        )
    view = preview.view
    info = _info_of(scheduling.handlers.get(view.rule.subsystem))
    instance_wide = info is not None and not info.requires_environment
    return html_page(
        request,
        "automation/run_confirm.html",
        {
            "view": view,
            "parameters": parameter_rows(view.rule),
            "blocked": blocked_reason(view.state),
            "target": preview.target,
            "target_kind": _target_kind(view.rule),
            "stack_kind": await _stack_kind(stacks, actor, view, info),
            "digest": preview.digest,
            "environment_text": INSTANCE_TARGET if instance_wide else None,
            "automation_paused": request.app.state.restore_gate.automation_paused,
        },
    )


async def _start(
    request: Request,
    services: JobServices,
    actor: Principal,
    rule_id: int,
    *,
    dry_run: bool,
    expected_updated_at: str | None = None,
    expected_digest: str | None = None,
) -> Response:
    try:
        run_id = await services.rules.run_now(
            actor,
            rule_id,
            dry_run=dry_run,
            expected_updated_at=expected_updated_at,
            expected_digest=expected_digest,
        )
    except REFUSALS as refusal:
        return _failure_page(
            request,
            "Dry run" if dry_run else "Run automation",
            rule_id,
            refusal,
            title="The dry run was not started" if dry_run else "The automation was not run",
        )
    notice = "dry_run" if dry_run else "started"
    run_href = route(request, "automation_rule_run", rule_id=rule_id, run_id=run_id)
    return redirect(request, run_href, [Flash("ok", NOTICES[notice])])


@router.post("/rules/{rule_id}/run", name="automation_rule_run_now")
async def run_rule(
    request: Request,
    services: Jobs,
    actor: CurrentUser,
    rule_id: RuleId,
    expected_updated_at: Annotated[str, Form(max_length=FIELD_MAX)] = "",
    expected_digest: Annotated[str, Form(max_length=FIELD_MAX)] = "",
) -> Response:
    """Start a run of the rule as it was reviewed, outside the schedule, and open it."""
    return await _start(
        request,
        services,
        actor,
        rule_id,
        dry_run=False,
        expected_updated_at=expected_updated_at,
        expected_digest=expected_digest,
    )


@router.post("/rules/{rule_id}/dry-run", name="automation_rule_dry_run")
async def dry_run_rule(
    request: Request, services: Jobs, actor: CurrentUser, rule_id: RuleId
) -> Response:
    """Start a run that reports what it would do and changes nothing, and open it."""
    return await _start(request, services, actor, rule_id, dry_run=True)


async def _delete_page(
    request: Request,
    services: JobServices,
    actor: Principal,
    rule_id: int,
    settings: Settings,
    *,
    status_code: int = 200,
    errors: dict[str, str] | None = None,
) -> Response:
    preview = await services.rules.delete_preview(actor, rule_id)
    return html_page(
        request,
        "automation/delete_confirm.html",
        {
            "preview": preview,
            "waiting": await services.rules.waiting_images(actor, rule_id),
            "errors": errors or {},
            "retention_on": settings.activity_retention_days is not None,
        },
        status_code,
    )


@router.get(
    "/rules/{rule_id}/delete", response_class=HTMLResponse, name="automation_rule_delete_confirm"
)
async def confirm_delete(
    request: Request,
    services: Jobs,
    actor: CurrentUser,
    rule_id: RuleId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Name what deleting the rule takes away; nothing is deleted by this page."""
    return await _delete_page(request, services, actor, rule_id, settings)


@router.post("/rules/{rule_id}/delete", name="automation_rule_delete")
async def delete_rule(
    request: Request,
    services: Jobs,
    actor: CurrentUser,
    rule_id: RuleId,
    settings: Annotated[Settings, Depends(get_settings)],
    expected_name: Text = "",
    typed_name: Text = "",
) -> Response:
    """Delete a rule, its job and its runs once its name has been typed."""
    if typed_name != expected_name or not expected_name:
        return await _delete_page(
            request,
            services,
            actor,
            rule_id,
            settings,
            status_code=422,
            errors={"typed_name": "Type the automation's name exactly as shown to confirm."},
        )
    try:
        await services.rules.delete(actor, rule_id, expected_name=expected_name)
    except ValidationFailed as invalid:
        return await _delete_page(
            request, services, actor, rule_id, settings, status_code=422, errors=invalid.fields
        )
    except SchedulerBusy as busy:
        return _failure_page(
            request, "Delete automation", rule_id, busy, title="The automation was not deleted"
        )
    return redirect(request, route(request, "automation"), [Flash("ok", NOTICES["deleted"])])


HOLDER_TYPE: Final = "holder_type"
HOLDER_LABEL: Final = "holder_label"


def _detail_rows(run_detail: dict[str, Any], actor: Principal) -> list[tuple[str, Any]]:
    """List a run's detail; a user who held the object is named only to an administrator."""
    rows = sorted(run_detail.items())
    if actor.is_administrator or run_detail.get(HOLDER_TYPE) != "user":
        return rows
    return [
        (key, _viewer_holder(value, actor) if key == HOLDER_LABEL else value) for key, value in rows
    ]


def _viewer_holder(label: Any, actor: Principal) -> str:
    return "you" if label == actor.username else "another user"


@router.get(
    "/rules/{rule_id}/runs/{run_id}", response_class=HTMLResponse, name="automation_rule_run"
)
async def run_detail(
    request: Request,
    services: Jobs,
    actor: CurrentUser,
    rule_id: RuleId,
    run_id: RunId,
) -> Response:
    """Show one run of the rule: when it ran, how it ended, why, and the detail it recorded."""
    view, run = await services.rules.run(actor, rule_id, run_id)
    context: dict[str, Any] = {
        "view": view,
        "row": run_row(run),
        "detail": _detail_rows(dict(run.detail), actor),
        "finished": run.outcome is not None,
        "region_href": route(request, "automation_rule_run", rule_id=rule_id, run_id=run_id),
    }
    if is_htmx_request(request):
        return html_fragment(request, RUN_REGION, "run_region", **context)
    return html_page(request, "automation/run.html", context)
