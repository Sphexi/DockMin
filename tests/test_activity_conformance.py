"""Source scan: every mutating entry point under app/ records an activity event or is listed."""

import ast
import dataclasses
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import pytest

from app.subsystems.base import RunContext

pytestmark = pytest.mark.xdist_group("activity-conformance")

# Calls into another module are not followed; the one-commit guard on the audit scope covers them.
ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
REPOSITORIES = APP / "db" / "repositories"
DOCKER_ENGINE = APP / "docker_engine"
ENGINE_CLIENT = DOCKER_ENGINE / "client.py"
ENGINE_RESOURCES = DOCKER_ENGINE / "resources.py"
COMPOSE_CLI = APP / "compose_cli" / "cli.py"
COMPOSE_RUN = "ComposeRun"
COMPOSE_MUTATOR = "_mutate"
RESOURCE_CONTROL_MODULE = "app.services.resource_controls"
RESOURCE_CONTROL_FILE = "app/services/resource_controls.py"

TRANSACTION_OPENERS = frozenset({"transaction", "savepoint"})
RAW_WRITERS = frozenset({"execute", "executemany", "executescript", "fetch_written", "_run"})
AUDITED_PATH = "app.services.activity.audited"
# Position of the HTTP method among the positional arguments of each Engine request helper.
HTTP_METHOD_POSITION: Mapping[str, int] = {"request": 0, "stream_bytes": 0, "decoded_stream": 2}
SELF_NAMES = frozenset({"self", "cls"})
ENGINE_READ_HELPERS = frozenset(
    {
        "get_json",
        "get_parsed",
        "get_parsed_list",
        "get_ids",
        "count_items",
        "parse",
        "parse_list",
        "segment",
    }
)
HTTP_RECEIVERS = frozenset({"_http", "http"})
CONTROL_SERVICE = "ResourceControlService"
CONTROL_HOOKS = frozenset({"release", "claim", "transfer", "copy"})
CONTROL_NAMES = frozenset({"controls", "_controls"})
BINDING_HOOK = "record_pull"

SELF_RECORDING = frozenset(
    {
        "app/__main__.py:clear_lockout",
        "app/auth/bootstrap.py:BootstrapService.complete",
        "app/auth/login.py:LoginService.login",
        "app/auth/platform_signin.py:PlatformLogin.end_session",
        "app/auth/platform_signin.py:PlatformLogin.exchange",
        "app/scheduler/recovery.py:record_interruption",
        "app/services/activity_retention.py:ActivityRetention.apply",
        "app/services/encryption.py:RekeyLog.finished",
        "app/services/self_update.py:UpdateDispatcher.complete",
        "app/subsystems/backup_syncer.py:BackupSyncer.run",
    }
)

NOT_AUDITED: Mapping[str, str] = {
    "app/services/navigation.py:remember_navigation": ("display preference; grants nothing"),
    "app/services/onboarding.py:OnboardingService.apply": (
        "runs each target of the confirmed plan as its own audited operation: stack_adopted, "
        "image_bound and resource_control_created"
    ),
    "app/backup/archive.py:_PartHash.absorb": (
        "feeds a chunk of the archive into the per-part SHA-256 and MD5 digests; the hash objects' "
        "update method matches a repository method name but no database is involved"
    ),
    "app/backup/archive.py:_ScanningSink.write": (
        "passes the bytes of the archive being written to the compressor after scanning them for "
        "key material; the name matches a repository method but no database is involved"
    ),
    "app/backup/restore_steps.py:decrypt_and_reseal": (
        "re-seals the staged copy of an archive's database to the running key before the swap; "
        "no live row changes, and the key change is recorded in the restored log by "
        "finalise_staged"
    ),
    "app/backup/restore_steps.py:finalise_staged": (
        "writes the restore rows into the staged copy of the database before the swap; they "
        "belong to the restored log, and the live log records the restore separately"
    ),
    "app/auth/providers.py:InternalAuthProvider.authenticate": (
        "rewrites an outdated password hash after a verified login; the login is recorded by "
        "LoginService.login"
    ),
    "app/auth/sessions.py:SessionService.issue": (
        "session bookkeeping; every caller records the operation that issues a session"
    ),
    "app/auth/sessions.py:SessionService.resolve": "touches last-seen time on each request",
    "app/auth/sessions.py:SessionService.standing": (
        "touches last-seen time for an open terminal connection; no actor action"
    ),
    "app/auth/sessions.py:SessionService.end": (
        "session bookkeeping; every caller records the operation that ends a session"
    ),
    "app/auth/sessions.py:SessionService.end_all_for_user": (
        "session bookkeeping; every caller records the operation that ends sessions"
    ),
    "app/auth/sessions.py:SessionService.reap": "deletes expired sessions; no actor action",
    "app/auth/throttle.py:LoginThrottleService.record_failure": (
        "throttle counter; the attempt is recorded by LoginService.login"
    ),
    "app/auth/throttle.py:LoginThrottleService.settle_success": (
        "throttle counter; the attempt is recorded by LoginService.login"
    ),
    "app/auth/throttle.py:LoginThrottleService.refund": (
        "throttle counter; the attempt is recorded by LoginService.login"
    ),
    "app/auth/throttle.py:LoginThrottleService.clear_username": (
        "throttle counter; UserService.clear_lockout records the operation"
    ),
    "app/auth/throttle.py:LoginThrottleService.clear": (
        "throttle counter; the operator command records the operation"
    ),
    "app/auth/tokens.py:ApiTokenService.resolve": "touches last-used time on each request",
    "app/auth/tokens.py:ApiTokenService.revoke_all_for_user": (
        "token bookkeeping; every caller records the operation that revokes tokens"
    ),
    "app/crypto/startup.py:establish_vault": (
        "startup key state; no actor exists before the application serves requests"
    ),
    "app/docker_engine/frames.py:JsonLinesDecoder.finish": (
        "flushes the last partial line of a decoder; "
        "the name matches a repository method but no database is involved"
    ),
    "app/docker_engine/transport.py:HijackedConnection.write": (
        "writes bytes to an open socket; the name matches a repository method but no database "
        "is involved"
    ),
    "app/scheduler/loop.py:Scheduler.beat": (
        "stamps the heartbeat of runs in flight; no actor action"
    ),
    "app/scheduler/housekeeping.py:Housekeeping.run": (
        "prunes run history no event references and applies the retention policy; no actor action"
    ),
    "app/scheduler/loop.py:Scheduler.ensure_infrastructure_job": (
        "keeps the one rule-less job's schedule current at startup; no actor action"
    ),
    "app/scheduler/loop.py:Scheduler.expedite": (
        "moves one job's next fire to now; scheduling bookkeeping with no actor action, like "
        "claiming a fire"
    ),
    "app/scheduler/loop.py:Scheduler.start_claimed": (
        "starts a run row a caller already inserted; the caller's audited operation records the "
        "request and the run's outcome is its own record"
    ),
    "app/scheduler/loop.py:Scheduler.tick": (
        "claims due fires and records run rows; the run history is the record, and an "
        "interruption or an automatic disable is recorded as it happens"
    ),
    "app/scheduler/recovery.py:recover": (
        "startup sweep; each interrupted run is recorded by record_interruption"
    ),
    "app/services/activity.py:ActivityRecorder.record_event": "is the activity log writer",
    "app/services/activity.py:ActivityRecorder.record_failure": "is the activity log writer",
    "app/services/activity.py:ActivityRecorder.record_related": "is the activity log writer",
    "app/services/activity.py:ActivityRecorder.audit": "is the activity log writer",
    "app/services/automation_rules.py:AutomationRuleService.delete": (
        "reads the rule for its environment, then calls the audited "
        "operation that records the change"
    ),
    "app/services/automation_rules.py:AutomationRuleService.update": (
        "reads the rule for its environment, then calls the audited "
        "operation that records the change"
    ),
    "app/services/automation_rules.py:AutomationRuleService.enable": (
        "reads the rule for its environment, then calls the audited "
        "operation that records the change"
    ),
    "app/services/automation_rules.py:AutomationRuleService.disable": (
        "reads the rule for its environment, then calls the audited "
        "operation that records the change"
    ),
    "app/services/automation_rules.py:AutomationRuleService.pause": (
        "reads the rule for its environment, then calls the audited "
        "operation that records the change"
    ),
    "app/services/automation_rules.py:AutomationRuleService.resume": (
        "reads the rule for its environment, then calls the audited "
        "operation that records the change"
    ),
    "app/services/automation_rules.py:AutomationRuleService.release_hold": (
        "checks the rule and the caller's write access to the container, then calls the audited "
        "operation that records the release, so a refused caller leaves no row"
    ),
    "app/services/webhooks.py:issue_token": (
        "writes a fresh webhook token through the caller; the creating or rotating operation "
        "records the change, and the token itself is never recorded"
    ),
    "app/services/webhooks.py:WebhookService.call": (
        "stamps the calls a webhook has received and arms its job; a row per call would let a "
        "leaked token grow the log without bound, so the run records the call and its actions "
        "write their own rows"
    ),
    "app/services/webhooks.py:WebhookService.rotate": (
        "reads the rule for its environment and owner, then calls the audited operation that "
        "records the change"
    ),
    "app/services/webhooks.py:WebhookService.revoke": (
        "reads the rule for its environment and owner, then calls the audited operation that "
        "records the change"
    ),
    "app/services/environment_snapshots.py:EnvironmentSnapshotService.refresh_one": (
        "cache and status observation; a change between reachable and unreachable records "
        "environment_unreachable or environment_reachable in the same transaction, and a "
        "refresh that changes nothing writes no row"
    ),
    "app/services/environment_snapshots.py:EnvironmentSnapshotService.refresh_all": (
        "cache and status observation; a change between reachable and unreachable records "
        "environment_unreachable or environment_reachable in the same transaction, and a "
        "refresh that changes nothing writes no row"
    ),
    "app/services/host_objects.py:in_transaction": (
        "opens the transaction a caller's work and its row share; every caller is the audited "
        "operation, or its interrupted-request writer, that the work settles"
    ),
    "app/services/image_bindings.py:ImageBindingService.record_pull": (
        "internal hook called inside the operation that pulled or pushed the image"
    ),
    "app/services/image_updates.py:ImageUpdateService.check": (
        "stores the result of a read; not an action on a host"
    ),
    "app/services/jobs.py:JobService.enable": (
        "reads the job for its environment, then calls the audited "
        "operation that records the change"
    ),
    "app/services/jobs.py:JobService.run_now": (
        "reads the job for its environment, then calls the audited operation that records the run"
    ),
    "app/services/jobs.py:JobService.run_rule_job": (
        "reads the rule's job, then calls the audited operation that records the run"
    ),
    "app/services/jobs.py:JobService.set_subsystem_enabled": (
        "calls the audited switch, then rewrites each job's next fire in batches; the switch "
        "is the recorded change"
    ),
    "app/services/registry_resolution.py:DerivedSecretCache.basic_token": (
        "derived-secret cache refresh; the operation that needed the secret records itself"
    ),
    "app/services/resource_controls.py:ResourceControlService.release": (
        "internal hook called inside the operation that removed the object"
    ),
    "app/services/resource_controls.py:ResourceControlService.claim": (
        "internal hook called inside the operation that created the object"
    ),
    "app/services/resource_controls.py:ResourceControlService.transfer": (
        "internal hook called inside the operation that replaced the object"
    ),
    "app/services/resource_controls.py:ResourceControlService.copy": (
        "internal hook called inside the operation that replaced the object"
    ),
    "app/services/resource_controls.py:ResourceControlService.refresh_sub_resources": (
        "display cache refresh; no decision reads it"
    ),
    "app/services/self_update.py:UpdateDispatcher.claim": (
        "adds the update to the dispatcher's in-memory set of updates in flight; no stored row "
        "changes, and the request is recorded by the audited operation that accepted it"
    ),
    "app/services/self_update.py:UpdateDispatcher.dispatch": (
        "starts the background work for an update whose request is already recorded; the "
        "outcome is recorded by complete, and work that cannot start by instance_update_abandoned"
    ),
    "app/services/self_update.py:UpdateDispatcher.watch": (
        "waits for the helper's result and then calls complete, which records the outcome"
    ),
    "app/services/self_update.py:UpdateDispatcher.recover": (
        "finds updates that finished or were lost while the process was down and calls complete "
        "for each, which records the outcome once"
    ),
    "app/services/host_files.py:HostFileReader.session": (
        "creates and removes a helper container within the audited operation that reads the "
        "files, and refuses anyone but an administrator"
    ),
    "app/services/host_files.py:HostFileReader.remove_leftovers": (
        "removes helper containers that an earlier read left behind, within the audited "
        "operation that reads the files"
    ),
    "app/services/replaced_images.py:ReplacedImages.prune_due": (
        "deletes the record of an image once it is gone, tagged or aged out; each removal is its "
        "own audited image_removed row"
    ),
    "app/services/replaced_images.py:ReplacedImages.record_container": (
        "remembers which image an audited container update replaced; it records no action on a host"
    ),
    "app/services/replaced_images.py:ReplacedImages.record_stack": (
        "remembers which images an audited stack redeploy replaced; it records no action on a host"
    ),
    "app/services/stack_common.py:copy_stack_control": (
        "internal hook called inside the operation that removed the stack, in its transaction"
    ),
    "app/services/stack_store.py:store_update": (
        "internal writer called inside the operation that edits the stack, in its transaction"
    ),
    "app/services/terminals.py:TerminalBridge.run": (
        "forwards input and terminal size within a session whose start and end are recorded"
    ),
    "app/subsystems/activity_log_exporter.py:ActivityLogExporter.run": (
        "advances a read position after an object is stored; it records no action on a host, and "
        "a failed export writes its own failure row"
    ),
    "app/subsystems/container_health_checker.py:_Pass.examine": (
        "stamps when a held container was last seen unhealthy, a position the release decision "
        "reads; the release and the restart it leads to are recorded by the acting work"
    ),
    "app/subsystems/container_log_exporter.py:ContainerLogExporter.run": (
        "advances a read position after an object is stored; it records no action on a host, and "
        "a failed export writes its own failure row"
    ),
    "app/subsystems/container_updater.py:_Pass.charge_references": (
        "adds image references to the run's in-memory budget; no stored row changes"
    ),
    "app/subsystems/webhook_receiver.py:WebhookReceiver.run": (
        "takes the call count a run serves and resets it; the count is bookkeeping, and the "
        "deployments the run makes are recorded by the acting work"
    ),
    "app/subsystems/notification_sender.py:NotificationSender.run": (
        "outbox bookkeeping: deletes delivered rows, reschedules failed ones and stamps the "
        "channel; a delivery that fails for good is recorded as notification_delivery_failed"
    ),
}

WRAPPERS = frozenset(
    {
        "app/services/automation_rules.py:AutomationRuleService.update",
        "app/services/automation_rules.py:AutomationRuleService.enable",
        "app/services/automation_rules.py:AutomationRuleService.disable",
        "app/services/automation_rules.py:AutomationRuleService.pause",
        "app/services/automation_rules.py:AutomationRuleService.resume",
        "app/services/automation_rules.py:AutomationRuleService.delete",
        "app/services/automation_rules.py:AutomationRuleService.release_hold",
        "app/services/webhooks.py:WebhookService.rotate",
        "app/services/webhooks.py:WebhookService.revoke",
        "app/services/jobs.py:JobService.enable",
        "app/services/jobs.py:JobService.run_now",
        "app/services/jobs.py:JobService.run_rule_job",
    }
)
RUNNER_FILE = "app/services/stack_runs.py"
RUNNER_CLASS = "StackRunner"
RUNNER_OWNER_FILE = "app/services/stacks.py"
RUNNER_ATTRIBUTE = "_runner"
PROVISIONING_FILE = "app/services/container_provisioning.py"
PROVISIONING_CLASS = "ProvisioningBase"
PROVISIONING_SUBCLASS_FILES = (
    "app/services/container_create.py",
    "app/services/container_recreate.py",
)
COLLABORATORS = frozenset(
    {f"{PROVISIONING_FILE}:{PROVISIONING_CLASS}", f"{RUNNER_FILE}:{RUNNER_CLASS}"}
)
TRANSACTION_HELPER = "in_transaction"
TRANSACTION_HELPER_MODULE = "host_objects"
SUBSYSTEMS_DIR = APP / "subsystems"
SUBSYSTEM_ACTOR_RULE_ID_POSITION = 1
SUBSYSTEM_ACTOR_RULE_NAME_POSITION = 4
START_RUN_NAMES = frozenset({"_start_run", "start_run"})
OWNER_ATTRIBUTES = frozenset(
    {"owner", "owner_gate", "_OwnerGate__principal", "_OwnerGate__authoriser", "_owner_gate"}
)

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
LOOPS = (ast.For, ast.AsyncFor, ast.While)

IMPORT = "from app.services.activity import audited\n"
SERVICE_SOURCE = """
class TeamService:
    async def create(self, actor, name):
        await self._teams.create(name)
"""
AUDITED_SERVICE_SOURCE = (
    IMPORT
    + """
class TeamService:
    @audited(action="team_created", target_type="team")
    async def create(self, actor, name):
        await self._teams.create(name)
"""
)
WRITES = frozenset({"create"})


@dataclass(frozen=True, slots=True)
class Entry:
    """One public function or method and what the scan found in its reachable body."""

    qualname: str
    audited: bool
    mutates: bool
    transactions: int
    records_event: bool
    constructs_anonymous: bool


@dataclass(slots=True)
class _Facts:
    writes: bool = False
    transactions: int = 0
    transaction_helper: bool = False
    records: bool = False
    anonymous: bool = False
    hooks: bool = False
    bare_hooks: bool = False
    pull_hook: bool = False
    compose_call: bool = False
    callees: set[str] = field(default_factory=set)

    def merge(self, other: "_Facts") -> None:
        self.writes |= other.writes
        self.transactions += other.transactions
        self.transaction_helper |= other.transaction_helper
        self.records |= other.records
        self.anonymous |= other.anonymous
        self.hooks |= other.hooks
        self.bare_hooks |= other.bare_hooks
        self.pull_hook |= other.pull_hook
        self.compose_call |= other.compose_call
        self.callees |= other.callees


@dataclass(slots=True)
class _Module:
    name: str
    defined: dict[str, list[FunctionNode]]
    owners: dict[str, str | None]
    facts: dict[str, _Facts]
    imports: dict[str, str]

    def reachable(self, start: str) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            if current in seen or current not in self.facts:
                continue
            seen.add(current)
            pending.extend(self.facts[current].callees)
        return seen

    def public(self) -> list[str]:
        return [name for name in self.defined if not name.rsplit(".", 1)[-1].startswith("_")]


def _type_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else ""


def _mentions(annotation: ast.expr | None, suffix: str) -> bool:
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name | ast.Attribute) and _type_name(node).endswith(suffix):
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                parsed = ast.parse(node.value, mode="eval")
            except SyntaxError:
                continue
            if _mentions(parsed.body, suffix):
                return True
    return False


def _chain_root(node: ast.expr) -> tuple[ast.expr, int]:
    depth = 0
    while isinstance(node, ast.Attribute):
        node = node.value
        depth += 1
    return node, depth


def _is_held(node: ast.expr, held: set[str]) -> bool:
    """Report whether the expression is a repository or an attribute chain off self or a local."""
    if isinstance(node, ast.Call):
        return _type_name(node.func).endswith("Repository")
    root, depth = _chain_root(node)
    if not isinstance(root, ast.Name):
        return False
    return root.id in held or (root.id in SELF_NAMES and depth > 0)


def _assignments(node: ast.AST) -> list[tuple[ast.expr, ast.expr]]:
    if isinstance(node, ast.Assign):
        return [(target, node.value) for target in node.targets]
    if isinstance(node, ast.AnnAssign):
        return [] if node.value is None else [(node.target, node.value)]
    if isinstance(node, ast.NamedExpr):
        return [(node.target, node.value)]
    return []


def _typed_names(function: ast.AST, suffix: str) -> set[str]:
    """Return the parameters and annotated locals whose declared type ends with the suffix."""
    typed: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, FunctionNode):
            arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            typed |= {a.arg for a in arguments if _mentions(a.annotation, suffix)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _mentions(node.annotation, suffix):
                typed.add(node.target.id)
    return typed


def _grow_aliases(
    function: ast.AST, names: set[str], is_source: Callable[[ast.expr], bool]
) -> None:
    """Add each local assigned from a source expression, repeatedly, so chained aliases count."""
    grew = True
    while grew:
        grew = False
        for node in ast.walk(function):
            for target, value in _assignments(node):
                if isinstance(target, ast.Name) and target.id not in names and is_source(value):
                    names.add(target.id)
                    grew = True


@cache
def _held_names(function: ast.AST) -> set[str]:
    held = _typed_names(function, "Repository")
    _grow_aliases(function, held, lambda value: _is_held(value, held))
    return held


def _api_attributes(docker: frozenset[str]) -> frozenset[str]:
    return frozenset(entry.split(".", 1)[0] for entry in docker)


@cache
def _api_names(function: ast.AST, docker: frozenset[str]) -> dict[str, str]:
    """Map each local bound to an Engine API attribute, as in `api = client.containers`."""
    attributes = _api_attributes(docker)
    bound: dict[str, str] = {}

    def attribute_of(value: ast.expr) -> str | None:
        if isinstance(value, ast.Attribute) and value.attr in attributes:
            return value.attr
        if isinstance(value, ast.Name):
            return bound.get(value.id)
        return None

    grew = True
    while grew:
        grew = False
        for node in ast.walk(function):
            for target, value in _assignments(node):
                attribute = attribute_of(value)
                if isinstance(target, ast.Name) and target.id not in bound and attribute:
                    bound[target.id] = attribute
                    grew = True
    return bound


def _api_attribute(
    receiver: ast.expr, apis: Mapping[str, str], docker: frozenset[str]
) -> str | None:
    if isinstance(receiver, ast.Attribute) and receiver.attr in _api_attributes(docker):
        return receiver.attr
    return apis.get(receiver.id) if isinstance(receiver, ast.Name) else None


@cache
def _control_attributes(tree: ast.Module) -> frozenset[str]:
    """Return the attributes a method sets on self from a ResourceControlService parameter."""
    attributes: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, FunctionNode):
            continue
        typed = _typed_names(function, CONTROL_SERVICE)
        for node in ast.walk(function):
            for target, value in _assignments(node):
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(value, ast.Name)
                    and value.id in typed
                ):
                    attributes.add(target.attr)
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Attribute)
                and _mentions(node.annotation, CONTROL_SERVICE)
            ):
                attributes.add(node.target.attr)
    return frozenset(attributes)


def _is_control_receiver(node: ast.expr, names: set[str], attributes: frozenset[str]) -> bool:
    if isinstance(node, ast.Call):
        return _type_name(node.func) == CONTROL_SERVICE
    if isinstance(node, ast.Name):
        return node.id in names
    return isinstance(node, ast.Attribute) and (
        node.attr in CONTROL_NAMES or node.attr in attributes
    )


@cache
def _control_names(function: ast.AST, attributes: frozenset[str]) -> set[str]:
    names = _typed_names(function, CONTROL_SERVICE)
    _grow_aliases(function, names, lambda value: _is_control_receiver(value, names, attributes))
    return names


def _references_transaction_helper(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == TRANSACTION_HELPER
    return (
        isinstance(node, ast.Attribute)
        and node.attr == TRANSACTION_HELPER
        and isinstance(node.value, ast.Name)
        and node.value.id == TRANSACTION_HELPER_MODULE
    )


def _count_transactions(node: ast.AST, in_loop: bool = False) -> int:
    total = 0
    for child in ast.iter_child_nodes(node):
        inside = in_loop or isinstance(child, LOOPS)
        if isinstance(child, ast.Attribute) and child.attr == "transaction":
            total += 2 if inside else 1
        total += _count_transactions(child, inside)
    return total


@cache
def _transactions_of(function: ast.AST) -> int:
    return _count_transactions(function)


def _facts(
    function: ast.AST,
    writes: frozenset[str],
    owner: str | None,
    docker: frozenset[str],
    control_attributes: frozenset[str] = frozenset(),
    compose: frozenset[str] = frozenset(),
) -> _Facts:
    facts = _Facts(
        transactions=_transactions_of(function),
        transaction_helper=any(_references_transaction_helper(n) for n in ast.walk(function)),
    )
    held = _held_names(function)
    apis = _api_names(function, docker)
    controls = _control_names(function, control_attributes)
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Name)
            and node.id.startswith("_")
            and isinstance(node.ctx, ast.Load)
        ):
            facts.callees.add(node.id)
        elif isinstance(node, ast.Attribute):
            root, depth = _chain_root(node)
            if isinstance(root, ast.Name):
                if root.id in SELF_NAMES and depth == 1 and node.attr.startswith("_"):
                    facts.callees.add(f"{owner}.{node.attr}")
                elif node.attr.startswith("_") and depth == 1:
                    facts.callees.add(f"{root.id}.{node.attr}")
            if node.attr in TRANSACTION_OPENERS | RAW_WRITERS or (
                node.attr in writes and _is_held(node.value, held)
            ):
                facts.writes = True
            api = _api_attribute(node.value, apis, docker)
            if api is not None and f"{api}.{node.attr}" in docker:
                facts.writes = True
            if node.attr in CONTROL_HOOKS:
                facts.bare_hooks = True
                facts.hooks |= _is_control_receiver(node.value, controls, control_attributes)
            facts.pull_hook |= node.attr == BINDING_HOOK
            if node.attr in compose:
                facts.writes = True
                facts.compose_call = True
        elif isinstance(node, ast.Call):
            name = _type_name(node.func)
            facts.records |= name == "record_event"
            facts.anonymous |= name == "AnonymousActor"
            if name == "getattr" and isinstance(node.func, ast.Name) and len(node.args) >= 2:
                receiver, attribute = node.args[0], node.args[1]
                facts.writes |= _getattr_writes(receiver, attribute, writes, held)
                facts.writes |= _api_attribute(receiver, apis, docker) is not None
                facts.hooks |= _getattr_hooks(receiver, attribute, controls, control_attributes)
                facts.pull_hook |= (
                    isinstance(attribute, ast.Constant) and attribute.value == BINDING_HOOK
                )
                if isinstance(attribute, ast.Constant) and attribute.value in compose:
                    facts.writes = True
                    facts.compose_call = True
    return facts


def _getattr_writes(
    receiver: ast.expr, name: ast.expr, writes: frozenset[str], held: set[str]
) -> bool:
    if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
        return _is_held(receiver, held)
    if name.value in TRANSACTION_OPENERS | RAW_WRITERS:
        return True
    return name.value in writes and _is_held(receiver, held)


def _getattr_hooks(
    receiver: ast.expr, name: ast.expr, controls: set[str], attributes: frozenset[str]
) -> bool:
    if (
        isinstance(name, ast.Constant)
        and isinstance(name.value, str)
        and name.value not in CONTROL_HOOKS
    ):
        return False
    return _is_control_receiver(receiver, controls, attributes)


@cache
def _collect(tree: ast.Module) -> tuple[dict[str, list[FunctionNode]], dict[str, str | None]]:
    defined: dict[str, list[FunctionNode]] = {}
    owners: dict[str, str | None] = {}

    def visit(node: ast.AST, owner: str | None, inside_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name, False)
            elif isinstance(child, FunctionNode):
                if not inside_function:
                    name = child.name if owner is None else f"{owner}.{child.name}"
                    defined.setdefault(name, []).append(child)
                    owners[name] = owner
                visit(child, owner, True)
            else:
                visit(child, owner, inside_function)

    visit(tree, None, False)
    return defined, owners


def _package_of(module: str) -> list[str]:
    return module.removesuffix(".py").split("/")[:-1]


@cache
def _import_table(tree: ast.Module, module: str) -> dict[str, str]:
    """Map each name the module binds by import to the dotted path it refers to."""
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                table[alias.asname or root] = alias.name if alias.asname else root
        elif isinstance(node, ast.ImportFrom):
            package = _package_of(module)
            base = package[: len(package) - (node.level - 1)] if node.level else []
            parts = [*base, *(node.module.split(".") if node.module else [])]
            for alias in node.names:
                table[alias.asname or alias.name] = ".".join([*parts, alias.name])
    for node in ast.walk(tree):
        if isinstance(node, FunctionNode | ast.ClassDef):
            table.pop(node.name, None)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    table.pop(target.id, None)
    return table


def _dotted(node: ast.expr) -> str | None:
    root, depth = _chain_root(node)
    if not isinstance(root, ast.Name):
        return None
    names = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    return ".".join([root.id, *reversed(names)]) if depth else root.id


def _is_audited(function: FunctionNode, imports: Mapping[str, str]) -> bool:
    for decorator in function.decorator_list:
        dotted = _dotted(decorator.func if isinstance(decorator, ast.Call) else decorator)
        if dotted is None:
            continue
        head, _, rest = dotted.partition(".")
        resolved = f"{imports[head]}.{rest}" if rest and head in imports else None
        if head in imports and not rest:
            resolved = imports[head]
        if resolved == AUDITED_PATH:
            return True
    return False


@cache
def _parsed(source: str) -> ast.Module:
    return ast.parse(source)


@cache
def _analyse(
    source: str,
    module: str,
    writes: frozenset[str],
    docker: frozenset[str] = frozenset(),
    compose: frozenset[str] = frozenset(),
) -> _Module:
    tree = _parsed(source)
    defined, owners = _collect(tree)
    control_attributes = _control_attributes(tree)
    facts: dict[str, _Facts] = {}
    for name, nodes in defined.items():
        facts[name] = _Facts()
        for node in nodes:
            facts[name].merge(
                _facts(node, writes, owners[name], docker, control_attributes, compose)
            )
    return _Module(module, defined, owners, facts, _import_table(tree, module))


def scan_source(
    source: str,
    module: str,
    writes: frozenset[str],
    docker: frozenset[str] = frozenset(),
    compose: frozenset[str] = frozenset(),
) -> list[Entry]:
    """Return an Entry for every public function and method in the source.

    `docker` names the Engine calls (as `attribute.method`) that count as writes, and
    `compose` the Compose run methods that do.
    """
    analysed = _analyse(source, module, writes, docker, compose)
    entries = []
    for name in analysed.public():
        scope = [analysed.facts[member] for member in analysed.reachable(name)]
        entries.append(
            Entry(
                qualname=f"{module}:{name}",
                audited=all(_is_audited(node, analysed.imports) for node in analysed.defined[name]),
                mutates=any(item.writes for item in scope),
                transactions=sum(item.transactions for item in scope)
                + any(item.transaction_helper for item in scope),
                records_event=any(item.records for item in scope),
                constructs_anonymous=any(item.anonymous for item in scope),
            )
        )
    return entries


def anonymous_sites_outside(source: str, module: str, allowed: Iterable[str]) -> list[str]:
    """Return where AnonymousActor is constructed other than under an allowed entry point."""
    analysed = _analyse(source, module, frozenset())
    reaching: dict[str, set[str]] = {}
    for entry in analysed.public():
        for member in analysed.reachable(entry):
            reaching.setdefault(member, set()).add(f"{module}:{entry}")
    permitted = set(allowed)
    found = []
    for name, facts in analysed.facts.items():
        if not facts.anonymous:
            continue
        entries = reaching.get(name, set())
        found.extend(sorted(entries - permitted) or ([] if entries else [f"{module}:{name}"]))
    return found


def _app_sources(app: Path, *, skip_db: bool) -> Iterable[tuple[str, str]]:
    for path in sorted(app.rglob("*.py")):
        relative = path.relative_to(app.parent)
        if skip_db and relative.parts[:2] == ("app", "db"):
            continue
        yield relative.as_posix(), path.read_text(encoding="utf-8")


def scan_tree(
    app: Path,
    writes: frozenset[str],
    docker: frozenset[str] = frozenset(),
    compose: frozenset[str] = frozenset(),
) -> list[Entry]:
    """Scan every module under app/ except app/db/.

    Engine writes count outside the client, and Compose writes outside the Compose wrapper.
    """
    entries: list[Entry] = []
    for relative, source in _app_sources(app, skip_db=True):
        inside_client = relative.startswith("app/docker_engine/")
        inside_wrapper = relative.startswith("app/compose_cli/")
        entries.extend(
            scan_source(
                source,
                relative,
                writes,
                frozenset() if inside_client else docker,
                frozenset() if inside_wrapper else compose,
            )
        )
    return entries


def _in_collaborator(qualname: str) -> bool:
    """Report whether only audited code can reach the function."""
    owner, _, method = qualname.rpartition(".")
    return (owner in COLLABORATORS and method.startswith("_")) or qualname in _driven_entries()


def unaccounted(entries: Iterable[Entry]) -> list[str]:
    """Return mutating entry points that are neither audited nor listed."""
    return [
        entry.qualname
        for entry in entries
        if entry.mutates
        and not entry.audited
        and not _in_collaborator(entry.qualname)
        and entry.qualname not in SELF_RECORDING
        and entry.qualname not in NOT_AUDITED
    ]


def stale_entries(entries: Iterable[Entry]) -> list[str]:
    """Return list entries that name no unaudited mutating function."""
    unaudited_mutating = {e.qualname for e in entries if e.mutates and not e.audited}
    return sorted((SELF_RECORDING | NOT_AUDITED.keys()) - unaudited_mutating)


def self_recording_without_event(entries: Iterable[Entry]) -> list[str]:
    """Return SELF_RECORDING entries whose reachable body never calls record_event."""
    return [e.qualname for e in entries if e.qualname in SELF_RECORDING and not e.records_event]


def audited_with_several_transactions(entries: Iterable[Entry]) -> list[str]:
    """Return audited entries that open more than one write transaction."""
    return [e.qualname for e in entries if e.audited and e.transactions > 1]


def _method(tree: ast.Module, class_name: str, method: str) -> FunctionNode | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for member in node.body:
                if isinstance(member, FunctionNode) and member.name == method:
                    return member
    return None


def wrapper_violations(writes: frozenset[str], wrappers: Iterable[str] = WRAPPERS) -> list[str]:
    """Return wrappers that write, or that do not call exactly one audited method of their class."""
    offenders = []
    for qualname in sorted(wrappers):
        relative, _, dotted = qualname.partition(":")
        class_name, _, method = dotted.rpartition(".")
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imports = _import_table.__wrapped__(tree, relative)
        function = _method(tree, class_name, method)
        if function is None:
            offenders.append(f"{qualname}: not found")
            continue
        audited_calls = set()
        for name in _self_calls(function):
            callee = _method(tree, class_name, name)
            if callee is not None and _is_audited(callee, imports):
                audited_calls.add(name)
        written = {
            node.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Attribute) and node.attr in writes
        }
        if written or len(audited_calls) != 1:
            offenders.append(
                f"{qualname}: writes={sorted(written)} audited={sorted(audited_calls)}"
            )
    return offenders


def owner_reaches(source: str) -> list[int]:
    """Return the lines of a subsystem module that touch the rule owner's authority."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and node.attr in OWNER_ATTRIBUTES
    ]


def blank_reasons() -> list[str]:
    """Return NOT_AUDITED entries with no reason."""
    return [name for name, reason in NOT_AUDITED.items() if not reason.strip()]


def mutating(source: str, writes: frozenset[str] = WRITES) -> bool:
    """Report whether the single public function in the source is found to mutate."""
    [entry] = scan_source(source, "memory.py", writes)
    return entry.mutates


def _calls_session_writer(function: ast.AST) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute) and node.attr in RAW_WRITERS:
            root, depth = _chain_root(node.value)
            on_session = depth == 1 and isinstance(root, ast.Name) and root.id in SELF_NAMES
            if on_session and _type_name(node.value) == "_session":
                return True
    return False


def _self_calls(function: ast.AST) -> set[str]:
    called = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute):
            root, depth = _chain_root(node)
            if depth == 1 and isinstance(root, ast.Name) and root.id in SELF_NAMES:
                called.add(node.attr)
    return called


def derive_write_methods(sources: Iterable[str]) -> frozenset[str]:
    """Return the public repository method names that reach a write primitive.

    Names are resolved across all classes, so a write helper inherited from a base class counts.
    """
    methods: dict[str, list[ast.AST]] = {}
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, FunctionNode):
                        methods.setdefault(member.name, []).append(member)
    writers = {
        name for name, bodies in methods.items() if any(_calls_session_writer(b) for b in bodies)
    }
    grew = True
    while grew:
        grew = False
        for name, bodies in methods.items():
            if name not in writers and any(_self_calls(body) & writers for body in bodies):
                writers.add(name)
                grew = True
    return frozenset(name for name in writers if not name.startswith("_"))


def _http_method(call: ast.Call, position: int) -> str | None:
    keyword = next((k.value for k in call.keywords if k.arg == "method"), None)
    if keyword is None and len(call.args) > position:
        keyword = call.args[position]
    if isinstance(keyword, ast.Constant) and isinstance(keyword.value, str):
        return keyword.value.upper()
    return None


def _plain_calls(function: ast.AST) -> set[str]:
    """Return the names of the module-level functions the body calls directly."""
    return {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _issues_write_request(function: ast.AST) -> bool:
    """Report whether the body sends a request that is not a literal GET.

    A call on the HTTP layer counts as a write unless it is a request with a literal GET or one
    of the known read helpers, so a helper added later is a write until it is listed.
    """
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        name = _type_name(node.func)
        position = HTTP_METHOD_POSITION.get(name)
        if position is not None:
            if _http_method(node, position) != "GET":
                return True
        elif (
            isinstance(node.func, ast.Attribute)
            and _type_name(node.func.value) in HTTP_RECEIVERS
            and name not in ENGINE_READ_HELPERS
        ):
            return True
    return False


def _client_attributes(client_source: str) -> dict[str, str]:
    """Map each `*Api` class to the attribute EngineClient.__init__ exposes it under."""
    attributes: dict[str, str] = {}
    for node in ast.walk(ast.parse(client_source)):
        if isinstance(node, ast.ClassDef) and node.name == "EngineClient":
            for member in ast.walk(node):
                if (
                    isinstance(member, ast.Assign)
                    and isinstance(member.value, ast.Call)
                    and isinstance(member.targets[0], ast.Attribute)
                ):
                    attributes[_type_name(member.value.func)] = member.targets[0].attr
    return attributes


def derive_docker_write_sources(resources_source: str, client_source: str) -> frozenset[str]:
    """Return `attribute.method` for every public Engine call that sends a non-GET request.

    A request whose method is not a literal counts as a write.
    """
    attributes = _client_attributes(client_source)
    tree = ast.parse(resources_source)
    module_writers = {
        node.name
        for node in tree.body
        if isinstance(node, FunctionNode) and _issues_write_request(node)
    }
    sources: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name in attributes):
            continue
        methods = {m.name: m for m in node.body if isinstance(m, FunctionNode)}
        writers = {
            name
            for name, body in methods.items()
            if _issues_write_request(body) or _plain_calls(body) & module_writers
        }
        grew = True
        while grew:
            grew = False
            for name, body in methods.items():
                if name not in writers and _self_calls(body) & writers:
                    writers.add(name)
                    grew = True
        sources |= {f"{attributes[node.name]}.{n}" for n in writers if not n.startswith("_")}
    return frozenset(sources)


def derive_compose_write_sources(cli_source: str) -> frozenset[str]:
    """Return the public `ComposeRun` methods that reach `_mutate`, the only mutating runner."""
    for node in ast.walk(ast.parse(cli_source)):
        if isinstance(node, ast.ClassDef) and node.name == COMPOSE_RUN:
            methods = {m.name: m for m in node.body if isinstance(m, FunctionNode)}
            writers = {
                name for name, body in methods.items() if COMPOSE_MUTATOR in _self_calls(body)
            }
            grew = True
            while grew:
                grew = False
                for name, body in methods.items():
                    if name not in writers and _self_calls(body) & writers:
                        writers.add(name)
                        grew = True
            return frozenset(name for name in writers if not name.startswith("_"))
    return frozenset()


def _unaudited_sites(analysed: _Module, flagged: Iterable[str]) -> set[str]:
    """Return the entry points that reach a flagged function without being audited."""
    reaching: dict[str, set[str]] = {}
    for entry in analysed.public():
        for member in analysed.reachable(entry):
            reaching.setdefault(member, set()).add(entry)
    offenders: set[str] = set()
    for name in flagged:
        callers = reaching.get(name) or {name}
        offenders |= {
            f"{analysed.name}:{caller}"
            for caller in callers
            if caller not in analysed.defined
            or not all(_is_audited(n, analysed.imports) for n in analysed.defined[caller])
        }
    return offenders


def _reaches_effect(module: _Module, name: str) -> bool:
    return any(
        (facts := module.facts[member]).writes
        or facts.hooks
        or facts.pull_hook
        or facts.compose_call
        for member in module.reachable(name)
    )


def mutating_members(module: _Module, class_name: str) -> frozenset[str]:
    """Return the methods of a class, private ones included, whose reachable body has an effect."""
    return frozenset(
        name.rpartition(".")[2]
        for name, owner in module.owners.items()
        if owner == class_name and _reaches_effect(module, name)
    )


@cache
def _derived_module(relative: str) -> _Module:
    return _analyse(
        (ROOT / relative).read_text(encoding="utf-8"),
        relative,
        _derived_writes(),
        _derived_docker(),
        _derived_compose(),
    )


def runner_members() -> frozenset[str]:
    return mutating_members(_derived_module(RUNNER_FILE), RUNNER_CLASS)


def provisioning_members() -> frozenset[str]:
    return mutating_members(_derived_module(PROVISIONING_FILE), PROVISIONING_CLASS)


@cache
def _driven_entries() -> frozenset[str]:
    """Return the public mutating methods of the collaborator classes."""
    entries = set()
    for relative, class_name in (
        (RUNNER_FILE, RUNNER_CLASS),
        (PROVISIONING_FILE, PROVISIONING_CLASS),
    ):
        module = _derived_module(relative)
        entries |= {
            f"{relative}:{class_name}.{method}"
            for method in mutating_members(module, class_name)
            if not method.startswith("_")
        }
    return frozenset(entries)


def _is_runner(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == RUNNER_ATTRIBUTE


def _is_provisioning_receiver(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        return _type_name(node.func) == "super"
    return isinstance(node, ast.Name) and node.id in SELF_NAMES | {PROVISIONING_CLASS}


def _references(
    function: ast.AST,
    members: frozenset[str],
    is_receiver: Callable[[ast.expr], bool],
    typed: str,
) -> bool:
    """Report whether the function names a member of the receiver, by attribute or getattr."""
    aliases = _typed_names(function, typed)

    def receiver(node: ast.expr) -> bool:
        return is_receiver(node) or (isinstance(node, ast.Name) and node.id in aliases)

    _grow_aliases(function, aliases, receiver)
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute) and node.attr in members and receiver(node.value):
            return True
        if (
            isinstance(node, ast.Call)
            and _type_name(node.func) == "getattr"
            and len(node.args) >= 2
            and receiver(node.args[0])
        ):
            name = node.args[1]
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                return True
            if name.value in members:
                return True
    return False


def references_outside_audit(
    source: str,
    module: str,
    members: frozenset[str],
    is_receiver: Callable[[ast.expr], bool],
    typed: str,
) -> list[str]:
    """Return where a member is reached from a function that is not audited.

    A use is permitted in an audited function, or in a helper that only audited functions reach.
    """
    analysed = _analyse(source, module, frozenset())
    flagged = [
        name
        for name, nodes in analysed.defined.items()
        if any(_references(node, members, is_receiver, typed) for node in nodes)
    ]
    return sorted(_unaudited_sites(analysed, flagged))


def runner_calls_outside_audit(source: str, module: str, members: frozenset[str]) -> list[str]:
    return references_outside_audit(source, module, members, _is_runner, RUNNER_CLASS)


def provisioning_calls_outside_audit(
    source: str, module: str, members: frozenset[str]
) -> list[str]:
    return references_outside_audit(
        source, module, members, _is_provisioning_receiver, PROVISIONING_CLASS
    )


def constructions_of(source: str, class_name: str) -> list[int]:
    """Return the lines where the class is constructed."""
    return [
        node.lineno
        for node in ast.walk(_parsed(source))
        if isinstance(node, ast.Call) and _type_name(node.func) == class_name
    ]


def subclasses_of(source: str, class_name: str) -> list[str]:
    """Return the classes that list the class among their bases."""
    return [
        node.name
        for node in ast.walk(_parsed(source))
        if isinstance(node, ast.ClassDef)
        and any(_type_name(base) == class_name for base in node.bases)
    ]


def compose_calls_outside_audit(source: str, module: str, compose: frozenset[str]) -> list[str]:
    """Return where a Compose mutating call is reached from a function that is not audited.

    A use is permitted in an audited function, or in a helper that only audited functions reach.
    """
    analysed = _analyse(source, module, frozenset(), frozenset(), compose)
    flagged = [name for name, facts in analysed.facts.items() if facts.compose_call]
    return sorted(
        site for site in _unaudited_sites(analysed, flagged) if not _in_collaborator(site)
    )


def control_hook_calls_outside_audit(source: str, module: str) -> list[str]:
    """Return where a control hook is reached on a resource control service, outside audit.

    The hooks are `release`, `claim` and `transfer`. Any use of one counts in a module that
    imports the resource control module; elsewhere only a receiver known to be the service does.
    A use is permitted in an audited function, or in a helper that only audited functions reach.
    """
    analysed = _analyse(source, module, frozenset())
    imports_controls = any(
        target.startswith(RESOURCE_CONTROL_MODULE) for target in analysed.imports.values()
    )
    flagged = [
        name
        for name, facts in analysed.facts.items()
        if facts.hooks or (imports_controls and facts.bare_hooks)
    ]
    return sorted(
        site for site in _unaudited_sites(analysed, flagged) if not _in_collaborator(site)
    )


def record_pull_calls_outside_audit(source: str, module: str) -> list[str]:
    """Return where `record_pull` is reached from a function that is not audited.

    The binding writer is a hook for the operation that pulled or pushed the image. A use is
    permitted in an audited function, or in a helper that only audited functions reach.
    """
    analysed = _analyse(source, module, frozenset())
    flagged = [name for name, facts in analysed.facts.items() if facts.pull_hook]
    return sorted(
        site for site in _unaudited_sites(analysed, flagged) if not _in_collaborator(site)
    )


@pytest.fixture(scope="module", autouse=True)
def _analysis_caches_released() -> Iterator[None]:
    yield
    for clear in (
        _held_names.cache_clear,
        _api_names.cache_clear,
        _control_names.cache_clear,
        _control_attributes.cache_clear,
        _collect.cache_clear,
        _import_table.cache_clear,
        _transactions_of.cache_clear,
        _parsed.cache_clear,
        _analyse.cache_clear,
        _derived_writes.cache_clear,
        _derived_docker.cache_clear,
        _derived_compose.cache_clear,
        _derived_module.cache_clear,
        _driven_entries.cache_clear,
    ):
        clear()


@cache
def _derived_writes() -> frozenset[str]:
    sources = [path.read_text(encoding="utf-8") for path in REPOSITORIES.glob("*.py")]
    return derive_write_methods(sources)


@cache
def _derived_docker() -> frozenset[str]:
    return derive_docker_write_sources(
        ENGINE_RESOURCES.read_text(encoding="utf-8"), ENGINE_CLIENT.read_text(encoding="utf-8")
    )


@cache
def _derived_compose() -> frozenset[str]:
    return derive_compose_write_sources(COMPOSE_CLI.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def write_methods() -> frozenset[str]:
    return _derived_writes()


@pytest.fixture(scope="module")
def docker_writes() -> frozenset[str]:
    return _derived_docker()


@pytest.fixture(scope="module")
def compose_writes() -> frozenset[str]:
    return _derived_compose()


@pytest.fixture(scope="module")
def entries(
    write_methods: frozenset[str], docker_writes: frozenset[str], compose_writes: frozenset[str]
) -> list[Entry]:
    return scan_tree(APP, write_methods, docker_writes, compose_writes)


def test_the_derived_write_methods_hold_repository_writes_and_no_reads(
    write_methods: frozenset[str],
) -> None:
    assert {"create", "set_password_hash", "delete"} <= write_methods
    assert {"get", "list_page"}.isdisjoint(write_methods)


def test_the_derived_docker_write_sources_hold_the_mutating_engine_calls_and_no_reads(
    docker_writes: frozenset[str],
) -> None:
    assert {
        "containers.create",
        "containers.rename",
        "containers.start",
        "containers.stop",
        "containers.pause",
        "containers.unpause",
        "containers.remove",
        "containers.create_exec",
        "containers.attach",
        "containers.resize",
        "execs.start",
        "execs.resize",
        "images.pull",
        "images.push",
        "images.tag",
        "images.remove",
        "networks.create",
        "networks.connect",
        "networks.disconnect",
        "networks.remove",
        "volumes.create",
        "volumes.remove",
        "swarm_services.create",
        "swarm_services.update",
        "swarm_services.remove",
        "swarm_nodes.update",
        "swarm_configs.create",
        "swarm_configs.remove",
        "swarm_secrets.create",
        "swarm_secrets.remove",
    } <= docker_writes
    assert {
        "containers.list",
        "containers.inspect",
        "containers.stats",
        "containers.stats_lines",
        "containers.parse_stats",
        "containers.stats_once",
        "containers.logs",
        "containers.list_ids",
        "containers.count",
        "images.list",
        "images.history",
        "images.inspect",
        "networks.list",
        "networks.inspect",
        "networks.count",
        "volumes.list",
        "volumes.inspect",
        "volumes.count",
        "execs.inspect",
        "system.info",
        "system.events",
        "swarm.inspect",
        "swarm.stack_namespaces",
        "swarm_services.list",
        "swarm_services.inspect",
        "swarm_services.document",
        "swarm_services.logs",
        "swarm_tasks.list",
        "swarm_tasks.inspect",
        "swarm_nodes.list",
        "swarm_nodes.inspect",
        "swarm_nodes.document",
        "swarm_configs.list",
        "swarm_configs.inspect",
        "swarm_secrets.list",
        "swarm_secrets.inspect",
        "distribution.inspect",
    }.isdisjoint(docker_writes)


def test_the_derived_compose_write_sources_hold_the_mutating_runs_and_no_reads(
    compose_writes: frozenset[str],
) -> None:
    assert compose_writes == {"compose_up", "compose_down", "compose_start", "compose_stop"}
    assert {"config", "ps", "version"}.isdisjoint(compose_writes)


def test_no_module_calls_a_compose_write_outside_an_audited_function(
    compose_writes: frozenset[str],
) -> None:
    offenders = [
        site
        for relative, source in _app_sources(APP, skip_db=True)
        if not relative.startswith("app/compose_cli/")
        for site in compose_calls_outside_audit(source, relative, compose_writes)
    ]

    assert offenders == []


def test_no_module_calls_a_control_hook_outside_an_audited_function() -> None:
    offenders = [
        site
        for relative, source in _app_sources(APP, skip_db=True)
        if relative != RESOURCE_CONTROL_FILE
        for site in control_hook_calls_outside_audit(source, relative)
    ]

    assert offenders == []


def test_the_binding_writer_is_reached_only_from_audited_functions() -> None:
    offenders = [
        site
        for relative, source in _app_sources(APP, skip_db=True)
        for site in record_pull_calls_outside_audit(source, relative)
    ]

    assert offenders == []


def test_the_stack_runner_is_reached_from_the_stack_service_only_inside_audited_functions() -> None:
    source = (ROOT / RUNNER_OWNER_FILE).read_text(encoding="utf-8")

    offenders = runner_calls_outside_audit(source, RUNNER_OWNER_FILE, runner_members())

    assert offenders == []


def test_the_derived_stack_runner_members_hold_the_entries_that_reach_compose() -> None:
    assert {"run_up", "run_lifecycle", "execute"} <= runner_members()
    assert {"version_text", "reread", "require_idle"}.isdisjoint(runner_members())


@pytest.mark.parametrize("relative", PROVISIONING_SUBCLASS_FILES)
def test_the_provisioning_helpers_are_reached_only_inside_audited_methods(relative: str) -> None:
    source = (ROOT / relative).read_text(encoding="utf-8")

    offenders = provisioning_calls_outside_audit(source, relative, provisioning_members())

    assert offenders == []


def test_the_derived_provisioning_members_hold_the_helpers_that_pull_bind_or_write_controls() -> (
    None
):
    assert {
        "_pull",
        "_bind",
        "_claim_all",
        "_apply_control",
        "_carry_control",
        "_restrict_to_administrators",
        "_restrict_on",
        "_write_interrupted",
        "_spawn",
    } <= provisioning_members()


def test_the_stack_runner_is_constructed_only_by_the_stack_service() -> None:
    constructing = [
        relative
        for relative, source in _app_sources(APP, skip_db=True)
        if constructions_of(source, RUNNER_CLASS)
    ]

    assert constructing == [RUNNER_OWNER_FILE]


def test_the_provisioning_base_is_subclassed_only_by_the_create_and_recreate_services() -> None:
    subclassing = [
        relative
        for relative, source in _app_sources(APP, skip_db=True)
        if subclasses_of(source, PROVISIONING_CLASS)
    ]

    assert subclassing == list(PROVISIONING_SUBCLASS_FILES)


def test_every_mutating_entry_point_is_audited_self_recording_or_listed(
    entries: list[Entry],
) -> None:
    assert unaccounted(entries) == []


def test_no_allow_list_entry_is_stale_or_missing_a_reason(entries: list[Entry]) -> None:
    assert stale_entries(entries) == []
    assert blank_reasons() == []


def test_each_self_recording_entry_point_calls_record_event(entries: list[Entry]) -> None:
    assert self_recording_without_event(entries) == []


def test_an_audited_entry_point_opens_at_most_one_transaction(entries: list[Entry]) -> None:
    assert audited_with_several_transactions(entries) == []


def test_each_wrapper_writes_nothing_and_calls_exactly_one_audited_method(
    write_methods: frozenset[str],
) -> None:
    assert wrapper_violations(write_methods) == []
    assert NOT_AUDITED.keys() >= WRAPPERS


def test_the_wrapper_check_flags_a_method_that_calls_no_audited_method(
    write_methods: frozenset[str],
) -> None:
    unwrapped = "app/services/jobs.py:JobService.page"

    assert wrapper_violations(write_methods, [unwrapped]) != []


def test_no_subsystem_module_reaches_the_rule_owner() -> None:
    offenders = [
        f"{path.name}:{line}"
        for path in sorted(SUBSYSTEMS_DIR.glob("*.py"))
        if path.name != "base.py"
        for line in owner_reaches(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_the_run_context_carries_no_principal() -> None:
    names = {field.name for field in dataclasses.fields(RunContext)}

    assert "owner" not in names
    assert not [f for f in dataclasses.fields(RunContext) if "Principal" in str(f.type)]


def test_the_owner_check_flags_a_subsystem_that_reads_the_owner_gate() -> None:
    source = "async def run(self, context):\n    return context.owner_gate\n"

    assert owner_reaches(source) == [2]


def test_the_owner_check_flags_a_subsystem_that_reads_the_owner_directly() -> None:
    source = "async def run(self, context):\n    return context.owner\n"

    assert owner_reaches(source) == [2]


def test_the_owner_check_passes_a_subsystem_that_only_asks_about_an_object() -> None:
    source = "async def run(self, context):\n    await context.authorise_object(key)\n"

    assert owner_reaches(source) == []


def test_the_derived_write_methods_include_the_scheduler_repositories_writes(
    write_methods: frozenset[str],
) -> None:
    assert {"prune", "claim", "finish", "record_skipped", "heartbeat", "set_next_run"} <= (
        write_methods
    )


def test_no_source_file_names_the_scheduler_as_an_actor() -> None:
    pattern = re.compile(
        r"SubsystemActor\(\s*[\"']scheduler[\"']|actor_subsystem\s*=\s*[\"']scheduler"
    )

    offenders = [
        relative for relative, source in _app_sources(APP, skip_db=False) if pattern.search(source)
    ]

    assert offenders == []


def _builds_a_rule_less_actor(call: ast.Call) -> bool:
    """Report whether the rule id is the literal None, which only a rule-less subsystem accepts."""
    if len(call.args) > SUBSYSTEM_ACTOR_RULE_ID_POSITION:
        rule_id: ast.expr | None = call.args[SUBSYSTEM_ACTOR_RULE_ID_POSITION]
    else:
        rule_id = next((k.value for k in call.keywords if k.arg == "rule_id"), None)
    return isinstance(rule_id, ast.Constant) and rule_id.value is None


def subsystem_actors_without_a_rule_name(source: str) -> list[int]:
    """Return the line of each rule-bearing SubsystemActor construction that passes no rule name."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and _type_name(node.func) == "SubsystemActor":
            named = len(node.args) >= SUBSYSTEM_ACTOR_RULE_NAME_POSITION or any(
                keyword.arg == "rule_name" for keyword in node.keywords
            )
            if not named and not _builds_a_rule_less_actor(node):
                lines.append(node.lineno)
    return lines


def test_every_subsystem_actor_the_product_builds_carries_the_rules_name() -> None:
    offenders = [
        f"{relative}:{line}"
        for relative, source in _app_sources(APP, skip_db=False)
        if relative != "app/services/activity.py"
        for line in subsystem_actors_without_a_rule_name(source)
    ]

    assert offenders == []


def test_the_rule_name_check_flags_an_actor_built_without_one() -> None:
    source = (
        "a = SubsystemActor(name, rule_id, run_id)\nb = SubsystemActor(name, 1, 2, rule_name=x)\n"
    )

    assert subsystem_actors_without_a_rule_name(source) == [1]


def test_the_rule_name_check_accepts_a_rule_less_actor_and_flags_a_variable_rule_id() -> None:
    source = (
        "a = SubsystemActor(name, None)\n"
        "b = SubsystemActor(name, rule_id=None)\n"
        "c = SubsystemActor(name, rule_id)\n"
        "d = SubsystemActor(name, rule_id=rule_id)\n"
    )

    assert subsystem_actors_without_a_rule_name(source) == [3, 4]


def start_run_calls(source: str) -> list[int]:
    """Return the line of every call to a method named start_run or _start_run."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in START_RUN_NAMES
    ]


def test_no_page_or_json_route_starts_a_run_without_going_through_a_checked_entry_point() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{line}"
        for directory in (APP / "web", APP / "api")
        for path in sorted(directory.rglob("*.py"))
        for line in start_run_calls(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_only_two_checked_job_service_entry_points_call_the_run_starter() -> None:
    callers = {
        f"{relative}:{node.name}"
        for relative, source in _app_sources(APP, skip_db=False)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in START_RUN_NAMES
    }

    assert callers == {"app/services/jobs.py:run_now", "app/services/jobs.py:run_rule_job"}


def test_the_start_run_check_flags_a_route_that_starts_a_run() -> None:
    assert start_run_calls("await services.jobs._start_run(actor, 1)\n") == [1]
    assert start_run_calls("await services.jobs.run_now(actor, 1)\n") == []


def test_an_anonymous_actor_is_constructed_only_under_a_self_recording_entry_point() -> None:
    offenders = [
        site
        for relative, source in _app_sources(APP, skip_db=False)
        for site in anonymous_sites_outside(source, relative, SELF_RECORDING)
    ]

    assert offenders == []


def test_the_scanner_flags_a_mutating_method_with_no_decorator() -> None:
    [entry] = scan_source(SERVICE_SOURCE, "memory.py", WRITES)

    assert (entry.mutates, entry.audited) == (True, False)


def test_the_scanner_accepts_the_same_method_once_decorated() -> None:
    [entry] = scan_source(AUDITED_SERVICE_SOURCE, "memory.py", WRITES)

    assert (entry.mutates, entry.audited) == (True, True)


def test_the_suite_check_reports_an_unwrapped_mutating_method_and_passes_the_wrapped_one() -> None:
    unwrapped = unaccounted(scan_source(SERVICE_SOURCE, "memory.py", WRITES))
    wrapped = unaccounted(scan_source(AUDITED_SERVICE_SOURCE, "memory.py", WRITES))

    assert (unwrapped, wrapped) == (["memory.py:TeamService.create"], [])


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("def change(db):\n    db.transaction()\n", id="opens-a-transaction"),
        pytest.param("def change(db):\n    db.savepoint()\n", id="opens-a-savepoint"),
        pytest.param("def change(db):\n    db.execute('x')\n", id="raw-session-execute"),
        pytest.param("def change(db):\n    db.executemany('x', [])\n", id="raw-executemany"),
        pytest.param("def change(db):\n    db.executescript('x')\n", id="raw-executescript"),
        pytest.param("def change(db):\n    db.fetch_written('x')\n", id="raw-fetch-written"),
        pytest.param("def change(db):\n    db._run(lambda c: c)\n", id="raw-run"),
        pytest.param("def change(db):\n    run(db.execute)\n", id="raw-writer-as-a-value"),
        pytest.param(
            "def change(db):\n    getattr(db, 'execute')('x')\n", id="raw-writer-by-getattr"
        ),
        pytest.param(
            "class S:\n    def change(self):\n        self._repo.create()\n", id="held-repository"
        ),
        pytest.param(
            "class S:\n    def change(self):\n        self.teams.create()\n",
            id="public-attribute-receiver",
        ),
        pytest.param(
            "class S:\n    def change(self):\n        self.a.b.create()\n", id="deep-receiver"
        ),
        pytest.param(
            "class S:\n    def change(self):\n        run(self._teams.create)\n",
            id="write-method-passed-as-a-value",
        ),
        pytest.param(
            "class S:\n    def change(self):\n        run(partial(self._teams.create, 'x'))\n",
            id="write-method-inside-partial",
        ),
        pytest.param(
            "class S:\n    def change(self):\n        getattr(self._teams, 'create')()\n",
            id="constant-getattr",
        ),
        pytest.param(
            "class S:\n    def change(self, name):\n        getattr(self._teams, name)()\n",
            id="dynamic-getattr",
        ),
        pytest.param(
            "class S:\n    def change(self):\n        t = self._teams\n        t.create()\n",
            id="local-alias",
        ),
        pytest.param(
            "class S:\n    def change(self):\n        a = self._teams\n        b = a\n"
            "        b.create()\n",
            id="alias-of-an-alias",
        ),
        pytest.param(
            "def change(db):\n    TeamRepository(db).create()\n", id="constructed-repository"
        ),
        pytest.param(
            "def change(db):\n    repo = TeamRepository(db)\n    repo.create()\n",
            id="repository-bound-to-a-local",
        ),
        pytest.param(
            "def change(repo: TeamRepository):\n    repo.create()\n",
            id="repository-held-in-an-annotated-parameter",
        ),
        pytest.param(
            "def change(repo: TeamRepository | None):\n    repo.create()\n",
            id="optional-annotation",
        ),
        pytest.param(
            "def change(repo: 'TeamRepository'):\n    repo.create()\n", id="string-annotation"
        ),
        pytest.param(
            "def change(repo: 'TeamRepository | None'):\n    repo.create()\n",
            id="string-optional-annotation",
        ),
        pytest.param(
            "def change(repo: Optional[TeamRepository]):\n    repo.create()\n",
            id="subscripted-annotation",
        ),
        pytest.param(
            "class S:\n    def change(self):\n        self._apply()\n"
            "    def _apply(self):\n        self._repo.create()\n",
            id="private-method-helper",
        ),
        pytest.param(
            "class S:\n    def change(self):\n        run(self._apply)\n"
            "    def _apply(self):\n        self._repo.create()\n",
            id="private-method-passed-as-a-value",
        ),
        pytest.param(
            "class S:\n    def change(self):\n        S._apply(self._db)\n"
            "    @staticmethod\n    def _apply(db):\n        db.transaction()\n",
            id="private-static-helper",
        ),
        pytest.param(
            "def change():\n    _apply()\ndef _apply(db):\n    db.transaction()\n",
            id="module-private-helper",
        ),
        pytest.param(
            "class S:\n    def change(self):\n        def inner():\n"
            "            self._repo.create()\n",
            id="nested-function",
        ),
        pytest.param(
            "if True:\n    def change(db):\n        db.transaction()\n", id="function-inside-an-if"
        ),
        pytest.param(
            "try:\n    import x\nexcept ImportError:\n    def change(db):\n"
            "        db.transaction()\n",
            id="function-inside-an-except",
        ),
        pytest.param(
            "class S:\n    if True:\n        def change(self):\n            self._repo.create()\n",
            id="method-inside-an-if",
        ),
        pytest.param(
            "def factory():\n    class S:\n        def change(self):\n"
            "            self._repo.create()\n    return S\n",
            id="method-of-a-class-nested-in-a-function",
        ),
    ],
)
def test_the_scanner_treats_each_write_form_as_mutating(source: str) -> None:
    entries = scan_source(source, "memory.py", WRITES)

    assert any(e.mutates for e in entries)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "class S:\n    def read(self):\n        self._repo.get()\n", id="repository-read"
        ),
        pytest.param(
            "def route(services):\n    services.teams.create()\n", id="call-through-a-service"
        ),
        pytest.param(
            "class S:\n    def read(self):\n        other.create()\n", id="write-name-on-a-stranger"
        ),
        pytest.param(
            "class S:\n    def read(self):\n        self.create()\n", id="write-name-on-self-itself"
        ),
        pytest.param(
            "class S:\n    def read(self):\n        getattr(other, 'create')()\n",
            id="getattr-on-a-stranger",
        ),
    ],
)
def test_the_scanner_does_not_flag_reads_or_calls_through_another_service(source: str) -> None:
    entries = scan_source(source, "memory.py", WRITES)

    assert [e.qualname for e in entries if e.mutates] == []


def audited_method(body: str) -> str:
    return (
        IMPORT
        + "class S:\n    @audited(action='x', target_type='t')\n"
        + "    async def change(self, actor, items):\n"
        + body
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            "        async with self._db.transaction():\n            pass\n", 1, id="plain"
        ),
        pytest.param(
            "        for item in items:\n            async with self._db.transaction():\n"
            "                pass\n",
            2,
            id="inside-a-for",
        ),
        pytest.param(
            "        while items:\n            async with self._db.transaction():\n"
            "                pass\n",
            2,
            id="inside-a-while",
        ),
        pytest.param(
            "        async for item in items:\n            async with self._db.transaction():\n"
            "                pass\n",
            2,
            id="inside-an-async-for",
        ),
        pytest.param(
            "        async with self._db.transaction():\n            pass\n"
            "        async with self._db.transaction():\n            pass\n",
            2,
            id="twice-in-sequence",
        ),
    ],
)
def test_a_transaction_inside_a_loop_counts_as_more_than_one(body: str, expected: int) -> None:
    [entry] = scan_source(audited_method(body), "memory.py", WRITES)

    assert entry.transactions == expected


HELPER_TRANSACTION = "        await retry_busy(partial(in_transaction, self._db, work))\n"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(HELPER_TRANSACTION, 1, id="partial-argument"),
        pytest.param(
            "        await to_thread(in_transaction, self._db, work)\n", 1, id="thread-argument"
        ),
        pytest.param(
            "        await host_objects.in_transaction(self._db, work)\n", 1, id="module-call"
        ),
        pytest.param(HELPER_TRANSACTION * 2, 1, id="two-references-count-once"),
        pytest.param(
            "        for item in items:\n    " + HELPER_TRANSACTION, 1, id="reference-inside-a-for"
        ),
        pytest.param(
            "        async with self._db.transaction():\n            pass\n" + HELPER_TRANSACTION,
            2,
            id="helper-beside-a-direct-transaction",
        ),
        pytest.param(
            "        await self._apply()\n    async def _apply(self):\n" + HELPER_TRANSACTION,
            1,
            id="reference-in-a-private-helper",
        ),
        pytest.param(
            "        await self._first()\n        await self._second()\n"
            "    async def _first(self):\n"
            + HELPER_TRANSACTION
            + "    async def _second(self):\n"
            + HELPER_TRANSACTION,
            1,
            id="references-in-two-private-helpers-count-once",
        ),
        pytest.param("        return self._db.in_transaction\n", 0, id="session-property-read"),
    ],
)
def test_each_use_of_the_transaction_helper_counts_as_a_transaction(
    body: str, expected: int
) -> None:
    [entry, *_] = scan_source(audited_method(body), "memory.py", WRITES)

    assert entry.transactions == expected


def test_an_audited_entry_point_with_the_helper_and_a_direct_transaction_is_reported() -> None:
    body = "        async with self._db.transaction():\n            pass\n" + HELPER_TRANSACTION
    entries = scan_source(audited_method(body), "memory.py", WRITES)

    assert audited_with_several_transactions(entries) == ["memory.py:S.change"]


def test_an_audited_entry_point_referencing_the_transaction_helper_twice_is_not_reported() -> None:
    entries = scan_source(audited_method(HELPER_TRANSACTION * 2), "memory.py", WRITES)

    assert audited_with_several_transactions(entries) == []


RUNNER_MEMBERS = frozenset({"run_up", "execute"})
PROVISIONING_MEMBERS = frozenset({"_pull", "_bind"})


def runner_class(*methods: str, audited_methods: bool = False) -> str:
    decorator = "    @audited(action='x', target_type='t')\n" if audited_methods else ""
    return IMPORT + "class S:\n" + "".join(f"{decorator}{method}" for method in methods)


UNAUDITED_RUN = "    async def go(self):\n        await self._runner.run_up()\n"
RUN_THROUGH_HELPER = (
    "    async def go(self):\n        await self._start()\n"
    "    async def _start(self):\n        await self._runner.run_up()\n"
)
RUN_ALIASED = "    async def go(self, runner: StackRunner):\n        await runner.execute()\n"
RUN_BY_GETATTR = "    async def go(self):\n        await getattr(self._runner, 'run_up')()\n"
RUN_BY_COMPUTED_GETATTR = (
    "    async def go(self, name):\n        await getattr(self._runner, name)()\n"
)
RUN_AS_VALUE = "    async def go(self):\n        schedule(self._runner.execute)\n"
RUN_AFTER_LOCAL_ALIAS = (
    "    async def go(self):\n        runner = self._runner\n        await runner.run_up()\n"
)


@pytest.mark.parametrize(
    "method",
    [
        pytest.param(UNAUDITED_RUN, id="direct-call"),
        pytest.param(RUN_THROUGH_HELPER, id="private-helper-reached-from-an-unaudited-method"),
        pytest.param(RUN_ALIASED, id="typed-parameter"),
        pytest.param(RUN_BY_GETATTR, id="getattr-by-name"),
        pytest.param(RUN_BY_COMPUTED_GETATTR, id="getattr-by-computed-name"),
        pytest.param(RUN_AS_VALUE, id="passed-as-a-value"),
        pytest.param(RUN_AFTER_LOCAL_ALIAS, id="local-alias"),
    ],
)
def test_the_runner_check_reports_a_mutating_entry_reached_outside_an_audited_function(
    method: str,
) -> None:
    source = runner_class(method)

    assert runner_calls_outside_audit(source, "memory.py", RUNNER_MEMBERS) != []


@pytest.mark.parametrize(
    "method",
    [
        pytest.param(UNAUDITED_RUN, id="direct-call"),
        pytest.param(RUN_THROUGH_HELPER, id="private-helper-reached-only-from-audited-methods"),
        pytest.param(RUN_AFTER_LOCAL_ALIAS, id="local-alias"),
    ],
)
def test_the_runner_check_accepts_a_mutating_entry_reached_inside_an_audited_function(
    method: str,
) -> None:
    source = runner_class(method, audited_methods=True)

    assert runner_calls_outside_audit(source, "memory.py", RUNNER_MEMBERS) == []


def test_the_runner_check_ignores_a_read_only_member_and_another_receiver() -> None:
    method = (
        "    async def go(self):\n        self._runner.version_text()\n"
        "        await self._other.run_up()\n"
    )

    assert runner_calls_outside_audit(runner_class(method), "memory.py", RUNNER_MEMBERS) == []


UNAUDITED_PULL = "    async def go(self):\n        await self._pull()\n"
PULL_PASSED_ON = "    async def go(self):\n        run(partial(self._pull, 1))\n"
PULL_FROM_BASE = "    async def go(self):\n        await super()._bind()\n"
PULL_BY_CLASS = "    async def go(self):\n        await ProvisioningBase._pull(self)\n"


@pytest.mark.parametrize(
    "method",
    [
        pytest.param(UNAUDITED_PULL, id="direct-call"),
        pytest.param(PULL_PASSED_ON, id="passed-on-as-a-value"),
        pytest.param(PULL_FROM_BASE, id="through-super"),
        pytest.param(PULL_BY_CLASS, id="through-the-base-class"),
    ],
)
def test_the_provisioning_check_reports_a_mutating_helper_reached_outside_an_audited_method(
    method: str,
) -> None:
    source = runner_class(method)

    assert provisioning_calls_outside_audit(source, "memory.py", PROVISIONING_MEMBERS) == [
        "memory.py:S.go"
    ]


def test_the_provisioning_check_accepts_a_helper_reached_from_audited_methods_only() -> None:
    method = (
        "    async def go(self):\n        await self._step()\n"
        "    async def _step(self):\n        await self._pull()\n"
    )
    source = runner_class(method, audited_methods=True)

    assert provisioning_calls_outside_audit(source, "memory.py", PROVISIONING_MEMBERS) == []


def test_the_provisioning_check_reports_a_helper_that_an_unaudited_method_also_reaches() -> None:
    audited = runner_class(
        "    async def go(self):\n        await self._step()\n"
        "    async def _step(self):\n        await self._pull()\n",
        audited_methods=True,
    )
    source = audited + "    async def sneaky(self):\n        await self._step()\n"

    reported = provisioning_calls_outside_audit(source, "memory.py", PROVISIONING_MEMBERS)

    assert reported == ["memory.py:S.sneaky"]


def test_the_provisioning_check_ignores_a_read_only_helper() -> None:
    method = "    async def go(self):\n        await self._reach()\n"

    assert (
        provisioning_calls_outside_audit(runner_class(method), "memory.py", PROVISIONING_MEMBERS)
        == []
    )


def test_the_construction_check_finds_a_runner_built_by_name_or_through_a_module() -> None:
    source = "a = StackRunner(x)\nb = stack_runs.StackRunner(y)\nc = other(z)\n"

    assert constructions_of(source, RUNNER_CLASS) == [1, 2]


def test_the_subclass_check_finds_a_class_that_lists_the_base_among_several() -> None:
    source = (
        "class A(ProvisioningBase):\n    pass\n"
        "class B(Mixin, base.ProvisioningBase):\n    pass\n"
        "class C(Mixin):\n    pass\n"
    )

    assert subclasses_of(source, PROVISIONING_CLASS) == ["A", "B"]


def decorated(import_line: str, decorator: str) -> list[Entry]:
    source = f"{import_line}\n@{decorator}\ndef change(self, actor):\n    self._teams.create()\n"
    return scan_source(source, "memory.py", WRITES)


@pytest.mark.parametrize(
    ("import_line", "decorator", "expected"),
    [
        pytest.param(IMPORT, "audited(action='x', target_type='t')", True, id="direct-import"),
        pytest.param(
            "from app.services.activity import audited as record",
            "record(action='x', target_type='t')",
            True,
            id="aliased-import",
        ),
        pytest.param(
            "from app.services import activity",
            "activity.audited(action='x', target_type='t')",
            True,
            id="module-import",
        ),
        pytest.param(
            "import app.services.activity",
            "app.services.activity.audited(action='x', target_type='t')",
            True,
            id="dotted-import",
        ),
        pytest.param(
            "",
            "audited(action='x', target_type='t')",
            False,
            id="never-imported",
        ),
        pytest.param(
            "def audited(**kwargs):\n    return lambda f: f",
            "audited(action='x', target_type='t')",
            False,
            id="local-stub",
        ),
        pytest.param(
            IMPORT + "def audited(**kwargs):\n    return lambda f: f",
            "audited(action='x', target_type='t')",
            False,
            id="local-stub-shadowing-the-import",
        ),
        pytest.param(
            "from other.module import audited",
            "audited(action='x', target_type='t')",
            False,
            id="same-name-from-another-module",
        ),
        pytest.param(
            "import other",
            "other.audited(action='x', target_type='t')",
            False,
            id="attribute-of-another-module",
        ),
    ],
)
def test_a_decorator_counts_only_when_it_resolves_to_the_real_audited(
    import_line: str, decorator: str, expected: bool
) -> None:
    [entry] = [e for e in decorated(import_line, decorator) if e.qualname.endswith(":change")]

    assert entry.audited is expected


def test_a_relative_import_of_audited_resolves_against_the_module_path() -> None:
    source = (
        "from .activity import audited\n"
        "@audited(action='x', target_type='t')\n"
        "def change(self):\n    pass\n"
    )

    [entry] = scan_source(source, "app/services/teams.py", WRITES)

    assert entry.audited is True


@pytest.mark.parametrize(
    ("source", "allowed", "expected"),
    [
        pytest.param(
            "def clear():\n    AnonymousActor()\n", {"m.py:clear"}, [], id="in-an-allowed-entry"
        ),
        pytest.param(
            "def clear():\n    other()\n    return x\ndef other():\n    AnonymousActor()\n",
            set(),
            ["m.py:other"],
            id="in-an-unlisted-entry",
        ),
        pytest.param(
            "def clear():\n    _helper()\ndef _helper():\n    AnonymousActor()\n",
            {"m.py:clear"},
            [],
            id="in-a-helper-of-an-allowed-entry",
        ),
        pytest.param(
            "def clear():\n    _helper()\ndef other():\n    _helper()\ndef _helper():\n"
            "    AnonymousActor()\n",
            {"m.py:clear"},
            ["m.py:other"],
            id="in-a-helper-shared-with-an-unlisted-entry",
        ),
        pytest.param(
            "def _orphan():\n    AnonymousActor()\n",
            {"m.py:clear"},
            ["m.py:_orphan"],
            id="in-a-helper-nothing-calls",
        ),
        pytest.param(
            "def clear():\n    activity.AnonymousActor()\n",
            set(),
            ["m.py:clear"],
            id="through-a-module-attribute",
        ),
    ],
)
def test_the_scanner_finds_an_anonymous_actor_built_outside_the_allowed_entries(
    source: str, allowed: set[str], expected: list[str]
) -> None:
    assert anonymous_sites_outside(source, "m.py", allowed) == expected


def test_write_derivation_follows_same_class_helpers_and_public_writers() -> None:
    source = """
class Base:
    async def _execute(self, sql):
        return await self._session.execute(sql)

class Repo(Base):
    async def insert(self):
        await self._execute("x")
    async def _stage(self):
        await self.insert()
    async def replace(self):
        await self._stage()
    async def read(self):
        return await self._session.fetch_one("x")
"""

    assert derive_write_methods([source]) == {"insert", "replace"}


ENGINE_API_SOURCE = """
class FooApi:
    async def get(self):
        return await self._http.request("GET", "/foo")

    async def make(self):
        await self._http.request("POST", "/foo")

    async def keyword(self):
        await self._http.request(method="DELETE", path="/foo")

    async def streamed(self):
        return decoded_stream(self._http, decoder, "PUT", "/foo")

    async def read_stream(self):
        return self._http.stream_bytes("GET", "/foo")

    async def dynamic(self, verb):
        await self._http.request(verb, "/foo")

    async def through_helper(self):
        await self._send()

    async def unknown_helper(self):
        await self._http.post_json("/foo")

    async def known_helper(self):
        return await self._http.get_parsed(Model, "/foo", max_bytes=1)

    async def _send(self):
        await self._http.request("POST", "/foo")

class Helper:
    async def make(self):
        await self._http.request("POST", "/foo")
"""
ENGINE_CLIENT_SOURCE = """
class EngineClient:
    def __init__(self, http):
        self._http = http
        self.foos = FooApi(http)
"""
DOCKER = frozenset({"containers.stop"})
UNWRAPPED_STOP = "class S:\n    async def change(self, client, cid):\n        {call}\n"
AUDITED_STOP = (
    IMPORT
    + "class S:\n    @audited(action='x', target_type='t')\n"
    + "    async def change(self, client, cid):\n        {call}\n"
)


def test_docker_write_derivation_follows_a_module_level_helper_that_sends_the_request() -> None:
    resources = """
async def _send(http, body):
    return await http.request("POST", "/foo", content=body)

async def _read(http):
    return await http.request("GET", "/foo")

class FooApi:
    async def create(self, body):
        return await _send(self._http, body)

    async def list(self):
        return await _read(self._http)
"""

    assert derive_docker_write_sources(resources, ENGINE_CLIENT_SOURCE) == {"foos.create"}


def test_docker_write_derivation_takes_every_non_get_request_and_nothing_else() -> None:
    derived = derive_docker_write_sources(ENGINE_API_SOURCE, ENGINE_CLIENT_SOURCE)

    assert derived == {
        "foos.make",
        "foos.keyword",
        "foos.streamed",
        "foos.dynamic",
        "foos.through_helper",
        "foos.unknown_helper",
    }


@pytest.mark.parametrize(
    "call",
    [
        pytest.param("await client.containers.stop(cid)", id="direct-call"),
        pytest.param("await self._client.containers.stop(cid)", id="held-client"),
        pytest.param("await shield(lambda: client.containers.stop(cid))", id="inside-a-lambda"),
        pytest.param("run(client.containers.stop)", id="passed-as-a-value"),
        pytest.param("api = client.containers\n        await api.stop(cid)", id="api-in-a-local"),
        pytest.param(
            "api = self._client.containers\n        other = api\n        await other.stop(cid)",
            id="api-in-a-local-of-a-local",
        ),
        pytest.param(
            "if (api := client.containers):\n            await api.stop(cid)", id="api-in-a-walrus"
        ),
        pytest.param("await getattr(client.containers, 'stop')(cid)", id="constant-getattr"),
        pytest.param("await getattr(client.containers, verb)(cid)", id="dynamic-getattr"),
        pytest.param(
            "api = client.containers\n        await getattr(api, 'stop')(cid)",
            id="getattr-on-a-local-api",
        ),
    ],
)
def test_an_unwrapped_docker_mutation_is_flagged_and_the_audited_form_is_not(call: str) -> None:
    [unwrapped] = scan_source(UNWRAPPED_STOP.format(call=call), "memory.py", WRITES, DOCKER)
    [wrapped] = scan_source(AUDITED_STOP.format(call=call), "memory.py", WRITES, DOCKER)

    assert (unwrapped.mutates, unwrapped.audited) == (True, False)
    assert (wrapped.mutates, wrapped.audited) == (True, True)
    assert unaccounted([unwrapped]) == ["memory.py:S.change"]
    assert unaccounted([wrapped]) == []


@pytest.mark.parametrize(
    "call",
    [
        pytest.param("await client.containers.inspect(cid)", id="engine-read"),
        pytest.param("await client.stop(cid)", id="method-on-the-client-itself"),
        pytest.param("await client.volumes.stop(cid)", id="same-method-on-another-api"),
        pytest.param("await stop(cid)", id="bare-function"),
        pytest.param(
            "api = client.containers\n        await api.inspect(cid)", id="read-via-local"
        ),
        pytest.param(
            "api = client.volumes\n        await api.stop(cid)", id="local-of-another-api"
        ),
        pytest.param("api = other.thing\n        await api.stop(cid)", id="local-of-a-stranger"),
    ],
)
def test_an_engine_read_or_an_unrelated_call_is_not_a_docker_write(call: str) -> None:
    [entry] = scan_source(UNWRAPPED_STOP.format(call=call), "memory.py", WRITES, DOCKER)

    assert entry.mutates is False


def test_an_unaudited_exec_resize_is_flagged_and_the_audited_form_is_not(
    docker_writes: frozenset[str],
) -> None:
    call = "await client.execs.resize(exec_id, 24, 80)"

    [unwrapped] = scan_source(UNWRAPPED_STOP.format(call=call), "memory.py", WRITES, docker_writes)
    [wrapped] = scan_source(AUDITED_STOP.format(call=call), "memory.py", WRITES, docker_writes)

    assert (unwrapped.mutates, unwrapped.audited) == (True, False)
    assert unaccounted([unwrapped]) == ["memory.py:S.change"]
    assert unaccounted([wrapped]) == []


def bridge_construction_sites(app: Path) -> list[str]:
    """Return `module:function` for every call that constructs a TerminalBridge under app/."""
    sites: list[str] = []
    for relative, source in _app_sources(app, skip_db=False):
        for function in ast.walk(ast.parse(source)):
            if not isinstance(function, FunctionNode):
                continue
            sites.extend(
                f"{relative}:{function.name}"
                for node in ast.walk(function)
                if isinstance(node, ast.Call) and _type_name(node.func) == "TerminalBridge"
            )
    return sites


def test_the_terminal_bridge_is_constructed_only_by_the_audited_open_operations(
    entries: list[Entry],
) -> None:
    source = (APP / "services" / "terminals.py").read_text(encoding="utf-8")
    bridge_callers = {
        function.name
        for function in ast.walk(ast.parse(source))
        if isinstance(function, FunctionNode)
        and any(
            isinstance(node, ast.Attribute) and node.attr == "_bridge"
            for node in ast.walk(function)
        )
    }
    audited_opens = {
        e.qualname.rsplit(".", 1)[-1]
        for e in entries
        if e.qualname.startswith("app/services/terminals.py:TerminalService.open_") and e.audited
    }

    assert bridge_construction_sites(APP) == ["app/services/terminals.py:_bridge"]
    assert bridge_callers == audited_opens == {"open_exec", "open_attach"}


HOOK_IMPORT = "from app.services.resource_controls import ResourceControlService\n"
HOOK_AUDITED = "    @audited(action='x', target_type='t')\n"
EVERY_HOOK = pytest.mark.parametrize("hook", sorted(CONTROL_HOOKS))


@EVERY_HOOK
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            HOOK_IMPORT
            + "class S:\n    async def remove(self, key):\n        self._controls.HOOK(key)\n",
            ["memory.py:S.remove"],
            id="unaudited-caller",
        ),
        pytest.param(
            IMPORT
            + HOOK_IMPORT
            + "class S:\n"
            + HOOK_AUDITED
            + "    async def remove(self, key):\n        self._controls.HOOK(key)\n",
            [],
            id="audited-caller",
        ),
        pytest.param(
            IMPORT
            + HOOK_IMPORT
            + "class S:\n"
            + HOOK_AUDITED
            + "    async def remove(self, key):\n        await self._finish(key)\n"
            + "    async def _finish(self, key):\n        self._controls.HOOK(key)\n",
            [],
            id="helper-reached-only-from-audited-code",
        ),
        pytest.param(
            IMPORT
            + HOOK_IMPORT
            + "class S:\n"
            + HOOK_AUDITED
            + "    async def remove(self, key):\n        await self._finish(key)\n"
            + "    async def cleanup(self, key):\n        await self._finish(key)\n"
            + "    async def _finish(self, key):\n        self._controls.HOOK(key)\n",
            ["memory.py:S.cleanup"],
            id="helper-also-reached-from-unaudited-code",
        ),
        pytest.param(
            HOOK_IMPORT
            + "class S:\n    async def _orphan(self, key):\n        self._controls.HOOK(key)\n",
            ["memory.py:S._orphan"],
            id="helper-nothing-reaches",
        ),
        pytest.param(
            IMPORT
            + HOOK_IMPORT
            + "class S:\n"
            + HOOK_AUDITED
            + "    async def remove(self, key):\n"
            + "        await shield(lambda: self._controls.HOOK(key))\n",
            [],
            id="inside-a-lambda-of-an-audited-caller",
        ),
        pytest.param(
            HOOK_IMPORT
            + "class S:\n    async def run(self, key):\n"
            + "        async def inner():\n            await self._controls.HOOK(key)\n"
            + "        await inner()\n",
            ["memory.py:S.run"],
            id="inside-a-nested-function-of-an-unaudited-caller",
        ),
        pytest.param(
            "class S:\n    async def free(self):\n        self._lock.HOOK()\n",
            [],
            id="module-that-does-not-use-resource-controls",
        ),
    ],
)
def test_a_control_hook_call_is_accepted_only_under_audited_code(
    hook: str, source: str, expected: list[str]
) -> None:
    assert control_hook_calls_outside_audit(source.replace("HOOK", hook), "memory.py") == expected


HOOK_ALIAS = "        r = self._controls.HOOK\n        await r(key)\n"


@EVERY_HOOK
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(HOOK_ALIAS, id="bound-method-in-a-local"),
        pytest.param("        await shield(self._controls.HOOK)\n", id="passed-as-a-value"),
        pytest.param("        await getattr(self._controls, 'HOOK')(key)\n", id="constant-getattr"),
        pytest.param("        await getattr(self._controls, verb)(key)\n", id="dynamic-getattr"),
        pytest.param("        await access.controls.HOOK(key)\n", id="through-a-service-bundle"),
        pytest.param(
            "        await ResourceControlService(self._db).HOOK(key)\n", id="constructed-inline"
        ),
        pytest.param(
            "        svc = ResourceControlService(self._db)\n        await svc.HOOK(key)\n",
            id="constructed-into-a-local",
        ),
        pytest.param(
            "        svc = self._controls\n        await svc.HOOK(key)\n",
            id="alias-of-the-attribute",
        ),
    ],
)
def test_a_control_hook_on_a_control_service_is_flagged_without_importing_the_module(
    hook: str, body: str
) -> None:
    source = "class S:\n    async def remove(self, key, access, verb):\n" + body.replace(
        "HOOK", hook
    )

    assert control_hook_calls_outside_audit(source, "memory.py") == ["memory.py:S.remove"]


@EVERY_HOOK
def test_a_hook_on_a_parameter_typed_as_the_service_is_flagged_without_the_import(
    hook: str,
) -> None:
    source = (
        "async def remove(controls_in: 'ResourceControlService', key):\n"
        f"    await controls_in.{hook}(key)\n"
    )

    assert control_hook_calls_outside_audit(source, "memory.py") == ["memory.py:remove"]


@EVERY_HOOK
def test_a_hook_on_an_attribute_set_from_a_typed_parameter_is_flagged(hook: str) -> None:
    source = (
        "class S:\n"
        "    def __init__(self, svc: ResourceControlService):\n"
        "        self._svc = svc\n"
        "    async def remove(self, key):\n"
        f"        await self._svc.{hook}(key)\n"
    )

    assert control_hook_calls_outside_audit(source, "memory.py") == ["memory.py:S.remove"]


@EVERY_HOOK
def test_a_hook_without_the_import_is_accepted_under_audited_code(hook: str) -> None:
    source = (
        IMPORT
        + "class S:\n"
        + HOOK_AUDITED
        + "    async def remove(self, key):\n"
        + HOOK_ALIAS.replace("HOOK", hook)
    )

    assert control_hook_calls_outside_audit(source, "memory.py") == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("        self._lock.release()\n", id="a-lock"),
        pytest.param("        self._semaphore.release()\n", id="a-semaphore"),
        pytest.param("        self._slots.claim(user)\n", id="a-slot-registry"),
        pytest.param("        self._funds.transfer(user)\n", id="an-unrelated-transfer"),
        pytest.param(
            "        r = self._lock.release\n        r()\n", id="a-lock-method-in-a-local"
        ),
    ],
)
def test_a_hook_name_on_something_else_is_not_flagged_without_the_import(body: str) -> None:
    source = "class S:\n    async def free(self, user):\n" + body

    assert control_hook_calls_outside_audit(source, "memory.py") == []


BINDING_CALL = "        await self._bindings.record_pull(actor, key)\n"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "class S:\n    async def pull(self, actor, key):\n" + BINDING_CALL,
            ["memory.py:S.pull"],
            id="unaudited-caller",
        ),
        pytest.param(
            IMPORT
            + "class S:\n"
            + HOOK_AUDITED
            + "    async def pull(self, actor, key):\n"
            + BINDING_CALL,
            [],
            id="audited-caller",
        ),
        pytest.param(
            IMPORT
            + "class S:\n"
            + HOOK_AUDITED
            + "    async def pull(self, actor, key):\n        await self._commit(actor, key)\n"
            + "    async def _commit(self, actor, key):\n"
            + BINDING_CALL,
            [],
            id="helper-reached-only-from-audited-code",
        ),
        pytest.param(
            IMPORT
            + "class S:\n"
            + HOOK_AUDITED
            + "    async def pull(self, actor, key):\n        await self._commit(actor, key)\n"
            + "    async def sync(self, actor, key):\n        await self._commit(actor, key)\n"
            + "    async def _commit(self, actor, key):\n"
            + BINDING_CALL,
            ["memory.py:S.sync"],
            id="helper-also-reached-from-unaudited-code",
        ),
        pytest.param(
            "class S:\n    async def pull(self, actor, key):\n"
            "        write = self._bindings.record_pull\n        await write(actor, key)\n",
            ["memory.py:S.pull"],
            id="bound-method-in-a-local",
        ),
        pytest.param(
            "class S:\n    async def pull(self, actor, key):\n"
            "        await getattr(self._bindings, 'record_pull')(actor, key)\n",
            ["memory.py:S.pull"],
            id="constant-getattr",
        ),
    ],
)
def test_the_binding_writer_is_accepted_only_under_audited_code(
    source: str, expected: list[str]
) -> None:
    assert record_pull_calls_outside_audit(source, "memory.py") == expected


COMPOSE_WRITES = frozenset({"compose_up", "compose_down", "compose_start", "compose_stop"})
COMPOSE_CALL = "        await run.compose_up(remove_orphans=False)\n"
COMPOSE_AUDITED = '    @audited(action="stack_deployed", target_type="stack")\n'


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "class S:\n    async def deploy(self, actor, run):\n" + COMPOSE_CALL,
            ["memory.py:S.deploy"],
            id="unaudited-caller",
        ),
        pytest.param(
            IMPORT
            + "class S:\n"
            + COMPOSE_AUDITED
            + "    async def deploy(self, actor, run):\n"
            + COMPOSE_CALL,
            [],
            id="audited-caller",
        ),
        pytest.param(
            IMPORT
            + "class S:\n"
            + COMPOSE_AUDITED
            + "    async def deploy(self, actor, run):\n        await self._up(run)\n"
            + "    async def _up(self, run):\n"
            + COMPOSE_CALL,
            [],
            id="helper-reached-only-from-audited-code",
        ),
        pytest.param(
            IMPORT
            + "class S:\n"
            + COMPOSE_AUDITED
            + "    async def deploy(self, actor, run):\n        await self._up(run)\n"
            + "    async def poke(self, run):\n        await self._up(run)\n"
            + "    async def _up(self, run):\n"
            + COMPOSE_CALL,
            ["memory.py:S.poke"],
            id="helper-also-reached-from-unaudited-code",
        ),
        pytest.param(
            "class S:\n    async def deploy(self, run):\n"
            "        call = run.compose_down\n        await call(remove_orphans=False)\n",
            ["memory.py:S.deploy"],
            id="bound-method-in-a-local",
        ),
        pytest.param(
            "class S:\n    async def deploy(self, run):\n"
            "        await getattr(run, 'compose_stop')()\n",
            ["memory.py:S.deploy"],
            id="constant-getattr",
        ),
        pytest.param(
            "class S:\n    async def inspect(self, run):\n        return await run.config()\n",
            [],
            id="a-read-is-not-a-write",
        ),
    ],
)
def test_a_compose_write_is_accepted_only_under_audited_code(
    source: str, expected: list[str]
) -> None:
    assert compose_calls_outside_audit(source, "memory.py", COMPOSE_WRITES) == expected


def test_an_unaudited_compose_call_makes_a_public_function_mutating() -> None:
    source = "class S:\n    async def deploy(self, actor, run):\n" + COMPOSE_CALL

    [entry] = scan_source(source, "memory.py", frozenset(), compose=COMPOSE_WRITES)

    assert entry.mutates
    assert unaccounted([entry]) == ["memory.py:S.deploy"]


def test_the_same_compose_call_under_an_audited_function_is_accounted_for() -> None:
    source = (
        IMPORT
        + "class S:\n"
        + COMPOSE_AUDITED
        + "    async def deploy(self, actor, run):\n"
        + COMPOSE_CALL
    )

    [entry] = scan_source(source, "memory.py", frozenset(), compose=COMPOSE_WRITES)

    assert entry.mutates
    assert unaccounted([entry]) == []


def test_a_compose_read_does_not_make_a_function_mutating() -> None:
    source = "class S:\n    async def inspect(self, run):\n        return await run.config()\n"

    [entry] = scan_source(source, "memory.py", frozenset(), compose=COMPOSE_WRITES)

    assert not entry.mutates


def test_the_compose_run_class_derives_its_write_sources_from_the_runner() -> None:
    source = (
        "class ComposeRun:\n"
        "    async def compose_up(self):\n        return await self._mutate('up')\n"
        "    async def compose_wrapper(self):\n        return await self.compose_up()\n"
        "    async def config(self):\n        return await self._read('config')\n"
    )

    assert derive_compose_write_sources(source) == {"compose_up", "compose_wrapper"}


@pytest.mark.parametrize(
    "call",
    [
        pytest.param("await client.swarm_services.update(cid, spec, version=1)", id="service"),
        pytest.param("await client.swarm_services.create(spec, registry_auth=None)", id="create"),
        pytest.param("await client.swarm_nodes.update(cid, spec, version=1)", id="node"),
        pytest.param("await client.swarm_configs.remove(cid)", id="config"),
        pytest.param(
            "await client.swarm_secrets.create(name='s', data=b'x', labels=None)", id="secret"
        ),
        pytest.param("await self._client.swarm_services.remove(cid)", id="held-client"),
    ],
)
def test_an_unwrapped_swarm_mutation_is_flagged_and_the_audited_form_is_not(
    docker_writes: frozenset[str], call: str
) -> None:
    [unwrapped] = scan_source(UNWRAPPED_STOP.format(call=call), "memory.py", WRITES, docker_writes)
    [wrapped] = scan_source(AUDITED_STOP.format(call=call), "memory.py", WRITES, docker_writes)

    assert (unwrapped.mutates, unwrapped.audited) == (True, False)
    assert unaccounted([unwrapped]) == ["memory.py:S.change"]
    assert unaccounted([wrapped]) == []


@pytest.mark.parametrize(
    "call",
    [
        pytest.param("await client.swarm_services.list()", id="service-list"),
        pytest.param("await client.swarm_services.document(cid)", id="document"),
        pytest.param("await client.swarm_tasks.list(filters=None)", id="tasks"),
        pytest.param("await client.swarm_nodes.list()", id="nodes"),
        pytest.param("await client.swarm_configs.inspect(cid)", id="config"),
        pytest.param("await client.distribution.inspect('a', registry_auth=None)", id="digest"),
        pytest.param("await client.swarm.stack_namespaces()", id="namespaces"),
    ],
)
def test_a_swarm_read_is_not_a_write(docker_writes: frozenset[str], call: str) -> None:
    [entry] = scan_source(UNWRAPPED_STOP.format(call=call), "memory.py", WRITES, docker_writes)

    assert entry.mutates is False


SWARM_STACK_ACTIONS = APP / "services" / "swarm_stack_actions.py"
SWARM_STACK_MODULE = "app/services/swarm_stack_actions.py"
STRAY_SWARM_WRITE = (
    "\n\nasync def stray(client):\n"
    "    await client.swarm_configs.create(name='x', data=b'y', labels=None)\n"
)
STRAY_CONTROL_RELEASE = (
    "\n\nclass Stray:\n    async def sneaky(self, key):\n"
    "        await self._controls.release(key)\n"
)


def swarm_stack_entries(
    source: str,
    write_methods: frozenset[str],
    docker_writes: frozenset[str],
    compose_writes: frozenset[str],
) -> list[Entry]:
    return scan_source(source, SWARM_STACK_MODULE, write_methods, docker_writes, compose_writes)


def test_the_swarm_stack_actions_leave_no_mutating_entry_point_unaccounted(
    write_methods: frozenset[str], docker_writes: frozenset[str], compose_writes: frozenset[str]
) -> None:
    source = SWARM_STACK_ACTIONS.read_text(encoding="utf-8")

    found = swarm_stack_entries(source, write_methods, docker_writes, compose_writes)

    assert {entry.qualname for entry in found if entry.mutates} >= {
        f"{SWARM_STACK_MODULE}:SwarmStackActions.create",
        f"{SWARM_STACK_MODULE}:SwarmStackActions.deploy",
        f"{SWARM_STACK_MODULE}:SwarmStackActions.redeploy",
        f"{SWARM_STACK_MODULE}:SwarmStackActions.remove",
    }
    assert unaccounted(found) == []
    assert audited_with_several_transactions(found) == []
    assert control_hook_calls_outside_audit(source, SWARM_STACK_MODULE) == []
    assert record_pull_calls_outside_audit(source, SWARM_STACK_MODULE) == []


def test_the_swarm_stack_adoption_is_an_audited_entry_point_with_one_transaction(
    write_methods: frozenset[str], docker_writes: frozenset[str], compose_writes: frozenset[str]
) -> None:
    module = "app/services/swarm_stack_adopt.py"
    source = (APP / "services" / "swarm_stack_adopt.py").read_text(encoding="utf-8")

    found = scan_source(source, module, write_methods, docker_writes, compose_writes)

    assert {entry.qualname for entry in found if entry.mutates} >= {
        f"{module}:SwarmStackAdopter.adopt"
    }
    assert unaccounted(found) == []
    assert audited_with_several_transactions(found) == []
    assert control_hook_calls_outside_audit(source, module) == []


def test_an_unaudited_swarm_write_added_to_the_stack_actions_is_flagged(
    write_methods: frozenset[str], docker_writes: frozenset[str], compose_writes: frozenset[str]
) -> None:
    source = SWARM_STACK_ACTIONS.read_text(encoding="utf-8") + STRAY_SWARM_WRITE

    found = swarm_stack_entries(source, write_methods, docker_writes, compose_writes)

    assert unaccounted(found) == [f"{SWARM_STACK_MODULE}:stray"]


def test_a_control_release_added_outside_audit_to_the_stack_actions_is_flagged() -> None:
    source = SWARM_STACK_ACTIONS.read_text(encoding="utf-8") + STRAY_CONTROL_RELEASE

    assert control_hook_calls_outside_audit(source, SWARM_STACK_MODULE) != []
