"""Validation of a stack before it is stored or deployed, and the errors stack services raise."""

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, Protocol

from app.auth.errors import PermissionDenied, ProductError, ResourceNotFound, ValidationFailed
from app.compose_cli.cli import ComposeCli, ComposeProject
from app.compose_cli.errors import (
    ComposeBusy,
    ComposeCommandFailed,
    ComposeError,
    ComposeHostRefused,
    ComposeOutputInvalid,
    ComposeOutputTooLarge,
    ComposeTimedOut,
    ComposeUnavailable,
    ComposeVersionUnsupported,
)
from app.compose_cli.host_check import ComposeHostUnresolved
from app.compose_cli.parsers import ResolvedProject
from app.compose_cli.sources import ComposeSource, number_sources
from app.compose_cli.target import ComposeTarget
from app.db.repositories.environment import Environment
from app.db.repositories.resource_control import ResourceControlRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, ResponseTooLarge
from app.docker_engine.errors import NotFound as EngineNotFound
from app.docker_engine.models import Network
from app.logging import REDACTED, Scrubber, get_logger
from app.registry_client.references import ImageReference
from app.services.access import COMPOSE_PROJECT_LABEL, ResourceKey, canonical_stack_key
from app.services.acting import Acting, AutomationAuthority, is_administrator
from app.services.authorisation import Authoriser
from app.services.container_provisioning import NETWORK_UNAVAILABLE, VOLUME_UNAVAILABLE
from app.services.image_bindings import RebindRefused, parse_pull_reference
from app.services.imported_volumes import ImportedVolumes
from app.services.object_inspector import (
    NETWORK_ID,
    VOLUME_OR_NETWORK_NAME,
    EngineObjectInspector,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.object_locks import ObjectKey
from app.services.pull_credentials import (
    BindingCredentialMissing,
    CandidateListTooLarge,
    CredentialChoice,
    CredentialChoiceRequired,
    CredentialSetMismatch,
    PullCredentialResolver,
    PullCredentials,
)
from app.services.registry_resolution import CredentialSetUnavailable
from app.services.stack_build_plan import (
    BuildInputs,
    BuildPlan,
    builds_digest,
    plan_builds,
    services_with_build,
)
from app.services.stack_builds import SECRET_IN_BUILD_ARG, BuildAdmission, secret_build_arguments
from app.services.stack_collisions import CollisionChecker, CollisionReport
from app.services.stack_common import stronger_reason
from app.services.stack_document import (
    COMPOSE_FIELD,
    CompanionFile,
    ComposeDocumentRejected,
    FileUses,
    precheck,
)
from app.services.stack_restrictions import (
    StackFinding,
    StackScan,
    backstop,
    limits,
    masked,
    refused_stack_options,
    reserved_labels,
    resolved_digest,
    resolved_refusals,
    scan,
)

__all__ = [
    "CONFIG_SLOT_WAIT_SECONDS",
    "ChangedSinceReview",
    "ComposeNotReady",
    "FindingView",
    "JobResult",
    "NotDeployed",
    "PendingChanges",
    "PreexistingObjects",
    "PullPlan",
    "ServiceSummary",
    "StackBusy",
    "StackError",
    "StackNameInUse",
    "StackOperationFailed",
    "StackRejected",
    "StackValidator",
    "Validated",
    "ValidationRequest",
    "compose_to_stack_error",
    "delivered_sources",
    "file_digest",
    "file_secret_values",
    "generated_names",
    "hide_keys",
    "in_project",
    "make_scrubber",
    "raise_if_refused",
    "refuse_builds_over_json",
    "require_build_rights",
    "secret_env_keys",
    "secret_forms",
]

logger = get_logger(__name__)

CONFIG_SLOT_WAIT_SECONDS: Final = 5.0
MAX_SHOWN_PORTS: Final = 8
MAX_SHOWN_CHARS: Final = 256

BUILD_ADMINISTRATOR_ONLY: Final = "Only an administrator can deploy a stack that builds images."
BUILD_NOT_AVAILABLE: Final = "The JSON interface cannot deploy a stack that builds images."

PULL_ALWAYS: Final = "always"
PULL_NEVER: Final = "never"

type HoldVolumes = Callable[[Sequence[str]], Awaitable[None]]
type Level = Literal["read", "write"]


class JobResult(Protocol):
    """What a finished stack operation hands back: a product-written message and its details."""

    @property
    def message(self) -> str:
        """Return the product-written summary of what the operation did."""
        ...


class StackError(ProductError):
    """A refusal or failure of a stack operation; `reason` is the activity failure reason."""

    reason = "failed"


class StackNameInUse(StackError):
    """A stack, or a Compose project on the host, already has this name."""

    reason = "name_in_use"

    def __init__(self) -> None:
        super().__init__(
            "A stack or Compose project with this name already exists on this environment."
        )


class PreexistingObjects(StackError):
    """Networks or volumes the project would create already exist and are not this stack's."""

    reason = "preexisting_objects"

    def __init__(self, objects: Sequence[str]) -> None:
        super().__init__(
            "; ".join(
                f"{description} already exists and does not belong to this stack."
                for description in objects
            )
        )
        self.objects = tuple(objects)


class ChangedSinceReview(StackError):
    """The stack or its containers changed after the confirmation was shown."""

    reason = "changed_since_review"

    def __init__(self) -> None:
        super().__init__("This stack changed since you reviewed it; nothing was changed.")


class NotDeployed(StackError):
    """A stack with no containers cannot be started or stopped."""

    reason = "not_deployed"

    def __init__(self) -> None:
        super().__init__("Deploy the stack first.")


class StackBusy(StackError):
    """The server has no free Compose process slot."""

    reason = "compose_busy"

    def __init__(self) -> None:
        super().__init__("The server is busy. Try again shortly.")


class ComposeNotReady(StackError):
    """Docker Compose is missing on this server or older than the floor."""

    def __init__(
        self, *, installed: str | None = None, required: str | None = None, message: str
    ) -> None:
        super().__init__(message)
        self.installed = installed
        self.required = required
        self.reason = "compose_unavailable" if installed is None else "compose_version_unsupported"


class StackOperationFailed(StackError):
    """Docker or Compose did not carry an operation out; stderr is Compose's, already scrubbed.

    `result` is what the operation had done when it stopped, for an operation that tracks it.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        exit_code: int | None = None,
        stderr: str = "",
        event_count: int = 0,
        progress: str = "",
        result: JobResult | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.exit_code = exit_code
        self.stderr = stderr
        self.event_count = event_count
        self.progress = progress
        self.result = result


def compose_to_stack_error(error: ComposeError) -> StackError:
    """Turn a wrapper error that is not a failed command into the product error for it."""
    if isinstance(error, ComposeBusy):
        return StackBusy()
    if isinstance(error, ComposeVersionUnsupported):
        return ComposeNotReady(
            installed=error.installed, required=error.required, message=str(error)
        )
    if isinstance(error, ComposeUnavailable):
        return ComposeNotReady(message=str(error))
    if isinstance(error, ComposeHostUnresolved):
        return StackOperationFailed("host_unresolved", str(error))
    if isinstance(error, ComposeHostRefused):
        return StackOperationFailed("address_refused", str(error))
    return StackOperationFailed("compose_failed", str(error))


@dataclass(frozen=True, slots=True)
class FindingView:
    """One refused use, at a Compose path, with the sentence shown beside it."""

    path: str
    option: str
    label: str
    message: str


class StackRejected(ValidationFailed):
    """A stack name, file, variable set or deployment that cannot be accepted.

    `fields` is keyed by form field (name, compose_file, variable-N-name, variable-N-value) or by
    Compose path (services.web.privileged). `findings` lists the restricted uses; `stderr` is
    Compose's own error text, scrubbed, for a file Compose could not read. `summary` replaces the
    joined field messages as the error's text, which is what an activity row stores; `warnings`
    are those of a check that also refused. `lines` maps a form field key to the 1-based line of
    its text that the refusal is found at.
    """

    def __init__(
        self,
        fields: dict[str, str],
        reason: str,
        *,
        findings: Sequence[FindingView] = (),
        stderr: str = "",
        restricted: Sequence[str] = (),
        summary: str = "",
        warnings: Sequence[str] = (),
        lines: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(fields)
        if summary:
            self.args = (summary,)
        self.reason = reason
        self.lines = dict(lines or {})
        self.findings = tuple(findings)
        self.stderr = stderr
        self.restricted = tuple(restricted)
        self.warnings = tuple(warnings)


class PendingChanges(StackRejected):
    """The stored file or variables differ from what was last deployed."""

    def __init__(self) -> None:
        super().__init__(
            {"stack": "The stored file or variables have not been deployed yet."},
            "pending_changes",
        )


@dataclass(frozen=True, slots=True)
class ServiceSummary:
    """One service of the resolved model; values masked."""

    name: str
    image: str | None
    ports: tuple[str, ...]
    networks: tuple[str, ...]
    volumes: int


@dataclass(frozen=True, slots=True)
class PullPlan:
    """One image reference to pull before `up`, with the credentials already resolved."""

    reference: str
    parsed: ImageReference
    credentials: PullCredentials = field(repr=False)
    service: str


@dataclass(frozen=True, slots=True)
class Validated:
    """Everything a deployment needs from a successful validation."""

    project: ResolvedProject = field(repr=False)
    scan: StackScan
    restricted: tuple[str, ...]
    services: tuple[ServiceSummary, ...]
    networks: tuple[str, ...]
    volumes: tuple[str, ...]
    compose_digest: str
    resolved_digest: str
    images: tuple[str, ...]
    to_pull: tuple[str, ...]
    pulls: tuple[PullPlan, ...]
    stale_volume_controls: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    secrets: frozenset[str] = field(default=frozenset(), repr=False)
    builds: tuple[BuildPlan, ...] = ()

    @property
    def builds_digest(self) -> str:
        """Return the digest of what a review of the builds names; empty without builds."""
        return builds_digest(self.builds)


@dataclass(frozen=True, slots=True)
class ValidationRequest:
    """One validation: the candidate file and variables, and where they would run."""

    environment: Environment
    client: EngineClient
    cli: ComposeCli
    target: ComposeTarget
    project: ComposeProject
    text: str
    values: Mapping[str, str] = field(repr=False)
    secrets: frozenset[str] = field(repr=False)
    slot_wait: float | None = CONFIG_SLOT_WAIT_SECONDS
    deploy: bool = False
    pull: bool = False
    choices: Mapping[str, CredentialChoice] = field(default_factory=dict)
    collisions: bool = False
    files: Sequence[CompanionFile] = field(default=(), repr=False)
    builds: BuildInputs | None = None


def delivered_sources(uses: FileUses, files: Sequence[CompanionFile]) -> tuple[ComposeSource, ...]:
    """Return the `file` sources of configs and secrets, each with the content of its file.

    StackRejected when a config or secret name cannot be written into the override, or two
    documents name different files for one entry.
    """
    by_name = {item.name: item for item in files}
    found: dict[tuple[str, str], ComposeSource] = {}
    for name, named in uses.items():
        for use in named:
            if use.role != "source":
                continue
            section = use.path.partition(".")[0]
            key = use.path.removeprefix(f"{section}.").removesuffix(".file")
            item = by_name.get(name)
            if section not in ("configs", "secrets") or item is None or key == "<key>":
                raise StackRejected(
                    {COMPOSE_FIELD: f"`{use.path}`: this name cannot be read from a file."},
                    "refused_construct",
                )
            slot = (section, key)
            if slot in found and found[slot].file != name:
                raise StackRejected(
                    {COMPOSE_FIELD: f"`{use.path}`: this is read from two different files."},
                    "refused_construct",
                )
            found[slot] = ComposeSource(
                "configs" if section == "configs" else "secrets",
                key,
                item.content,
                name,
                item.secret,
            )
    return tuple(found.values())


_MIN_LINE_TERM: Final = 8
_ENV_KEY: Final = re.compile(r"^\s*(?:export\s+)?([^\s=:#'\"]+)\s*(?:[=:]|$)")


def file_secret_values(files: Sequence[CompanionFile], uses: FileUses) -> frozenset[str]:
    """Return the strings of every secret file that must never be shown.

    Each file is taken whole; a line is taken only when it is at least eight characters once
    stripped. A file read as an environment file also adds each value after the first `=`,
    trimmed and with one layer of matching quotes removed.
    """
    read_as_env = {name for name, named in uses.items() if any(u.role == "env" for u in named)}
    values: set[str] = set()
    for item in files:
        if not item.secret:
            continue
        values.update({item.content, item.content.strip()})
        for line in item.content.splitlines():
            if len(line.strip()) >= _MIN_LINE_TERM:
                values.update({line, line.strip()})
            if item.name in read_as_env:
                _, separator, value = line.partition("=")
                value = value.strip()
                if separator and len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                values.add(value)
    return frozenset(value for value in values if value)


def secret_env_keys(files: Sequence[CompanionFile], uses: FileUses) -> frozenset[str]:
    """Return the variable and label keys that the secret files read as environment files define."""
    read_as_env = {name for name, named in uses.items() if any(u.role == "env" for u in named)}
    keys: set[str] = set()
    for item in files:
        if not item.secret or item.name not in read_as_env:
            continue
        for line in item.content.splitlines():
            found = _ENV_KEY.match(line)
            if found is not None:
                keys.add(found[1])
    return frozenset(keys)


def _hidden_entries(entries: object, keys: Collection[str]) -> object:
    if isinstance(entries, Mapping):
        return {key: REDACTED if key in keys else value for key, value in entries.items()}
    if isinstance(entries, list):
        return [
            f"{entry.partition('=')[0]}={REDACTED}"
            if isinstance(entry, str) and entry.partition("=")[0] in keys
            else entry
            for entry in entries
        ]
    return entries


def _hidden_services(
    services: Mapping[str, Mapping[str, Any]], keys: Collection[str]
) -> dict[str, Any]:
    hidden: dict[str, Any] = {}
    for name, service in services.items():
        if not isinstance(service, Mapping):
            hidden[name] = service
            continue
        entry = dict(service)
        for section in ("environment", "labels"):
            if section in entry:
                entry[section] = _hidden_entries(entry[section], keys)
        hidden[name] = entry
    return hidden


def hide_keys(project: ResolvedProject, keys: Collection[str]) -> ResolvedProject:
    """Return the model with each key's value masked in every service's environment and labels."""
    if not keys:
        return project
    document = dict(project.document)
    services = document.get("services")
    if isinstance(services, Mapping):
        document["services"] = _hidden_services(services, keys)
    return replace(project, services=_hidden_services(project.services, keys), document=document)


def generated_names(project: ComposeProject) -> dict[tuple[str, str], str]:
    """Return the generated variable each delivered source is read from, by (section, key)."""
    return {(item.section, item.key): name for name, item in number_sources(project.sources)}


def file_digest(text: str) -> str:
    """Return the SHA-256 of a Compose file as stored."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_GO_JSON_ESCAPES: Final = (
    ("<", "\\u003c"),
    (">", "\\u003e"),
    ("&", "\\u0026"),
    ("\u2028", "\\u2028"),
    ("\u2029", "\\u2029"),
)
_HTML_ESCAPES: Final = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&#34;"),
    ("'", "&#39;"),
)
_HTML_NAMED_ESCAPES: Final = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
    ("'", "&#x27;"),
)


def secret_forms(value: str) -> frozenset[str]:
    """Return the spellings a secret can take in Compose's output.

    Compose interpolates `$$` for `$`, and its JSON messages escape quotes, backslashes, control
    characters and, in Go's HTML-safe encoding, `<`, `>` and `&`. A message that quotes the value
    with Go's `%q` is escaped a second time when it is wrapped in JSON.
    """
    plain = {value, value.replace("$", "$$")}
    forms = set(plain)
    for text in plain:
        compact = json.dumps(text, ensure_ascii=False)[1:-1]
        go = compact
        for raw, escaped in _GO_JSON_ESCAPES:
            go = go.replace(raw, escaped)
        forms.update({json.dumps(text)[1:-1], compact, go})
        for table in (_HTML_ESCAPES, _HTML_NAMED_ESCAPES):
            html = text
            for raw, escaped in table:
                html = html.replace(raw, escaped)
            forms.add(html)
    forms.update({json.dumps(form)[1:-1] for form in tuple(forms)})
    return frozenset(form for form in forms if form)


def make_scrubber(secrets: Sequence[str] | frozenset[str]) -> Scrubber:
    """Return a scrubber that redacts each secret value in every spelling Compose can print."""
    scrubber = Scrubber()
    for value in secrets:
        for form in secret_forms(value):
            scrubber.register_secret(form)
    return scrubber


class StackValidator:
    """Runs the validation steps over a candidate file; it writes nothing and takes no lock."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        controls: ResourceControlRepository,
        credentials: PullCredentialResolver,
        imported: ImportedVolumes | None = None,
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._controls = controls
        self._credentials = credentials
        self._imported = imported
        self._collisions = CollisionChecker(authoriser=authoriser, inspector=inspector)

    async def validate(
        self,
        actor: Acting,
        request: ValidationRequest,
        *,
        hold: HoldVolumes | None = None,
    ) -> Validated:
        """Validate the candidate; raise StackRejected, or a product error for the host.

        `hold` is awaited with the project's volume names once they are known and before the
        checks that depend on them, so the caller can lock them first. An automation run is
        validated as its owner: the owner's role selects the branch, and pulls use the image
        bindings only.
        """
        administrator = is_administrator(actor)
        admission = (
            BuildAdmission(frozenset(await request.builds.contexts.digests()))
            if administrator and request.builds is not None
            else None
        )
        resolved, project, secrets = await self._resolve(request, admission)
        self._everyone(resolved, request, project, admission)
        environment_id = request.environment.id
        name = request.project.name
        runtime = await self._default_runtime(request.client, resolved)
        scanned = scan(resolved, default_runtime=runtime)
        found = list(scanned.findings)
        if not administrator:
            found.extend(backstop(resolved, default_runtime=runtime, covered=found))
            if len(found) > len(scanned.findings):
                logger.error("stack_scan_gap", extra={"count": len(found) - len(scanned.findings)})
            errors = await self._reach(actor, environment_id, scanned, found)
            if found or errors:
                raise _restricted(found, errors)
        _check_object_names(scanned)
        warnings = (
            await self._collisions_of(actor, request, resolved, secrets)
            if request.collisions
            else ()
        )
        if hold is not None:
            await hold(scanned.project_volumes)
        stale = await self._preexisting(environment_id, name, scanned)
        references = tuple(scanned.images)
        to_pull = await self._to_pull(request, scanned)
        entries = [(reference, scanned.images[reference][0]) for reference in to_pull]
        built: tuple[BuildPlan, ...] = ()
        if request.deploy and admission is not None and request.builds is not None:
            built = await self._plan_builds(actor, request, resolved, admission)
            entries.extend(
                await _base_pulls(request, built, {reference for reference, _ in entries})
            )
        to_pull = tuple(reference for reference, _ in entries)
        pulls = await self._resolve_pulls(actor, request, entries) if request.deploy else ()
        masked_document = masked(resolved, secrets)
        return Validated(
            project=resolved,
            scan=scanned,
            restricted=refused_stack_options(found) if administrator else (),
            services=_service_summaries(masked_document),
            networks=scanned.project_networks,
            volumes=scanned.project_volumes,
            compose_digest=file_digest(request.text),
            resolved_digest=resolved_digest(resolved, secrets),
            images=references,
            to_pull=to_pull,
            pulls=pulls,
            stale_volume_controls=stale,
            warnings=warnings,
            secrets=secrets,
            builds=built,
        )

    async def _plan_builds(
        self,
        actor: Acting,
        request: ValidationRequest,
        resolved: ResolvedProject,
        admission: BuildAdmission,
    ) -> tuple[BuildPlan, ...]:
        """Plan the stack's builds, refusing any that cannot run."""
        inputs = request.builds
        if inputs is None:
            return ()
        planned = await plan_builds(
            resolved.services,
            project=request.project.name,
            admission=replace(admission, directory=request.project.directory.as_posix()),
            contexts=inputs.contexts,
            client=request.client,
            rebuild=inputs.rebuild,
            allow_build=not isinstance(actor, AutomationAuthority),
        )
        if planned.refusals:
            raise StackRejected(dict(planned.refusals), planned.reason)
        needed = [plan for plan in planned.plans if not plan.reuse]
        if needed and not inputs.buildx_available:
            raise StackRejected(
                {
                    f"services.{needed[0].service}.build": (
                        "Docker buildx was not found on this server."
                    )
                },
                "buildx_unavailable",
            )
        return planned.plans

    async def _collisions_of(
        self,
        actor: Acting,
        request: ValidationRequest,
        resolved: ResolvedProject,
        secrets: frozenset[str],
    ) -> tuple[str, ...]:
        """Refuse a name or a port a running container holds; return the warnings."""
        if isinstance(actor, AutomationAuthority):
            raise ValueError("collision checks are for a user-initiated validation")
        report = await self._collisions.compose(
            actor,
            request.environment.id,
            request.client,
            project=request.project.name,
            services=resolved.services,
            scrubber=make_scrubber(secrets),
        )
        return raise_if_refused(report)

    async def _resolve(
        self, request: ValidationRequest, admission: BuildAdmission | None
    ) -> tuple[ResolvedProject, ComposeProject, frozenset[str]]:
        try:
            uses = await asyncio.to_thread(
                precheck,
                request.text,
                files=request.files,
                builds=None if admission is None else admission.check,
            )
        except ComposeDocumentRejected as rejected:
            raise StackRejected(rejected.fields, rejected.reason, lines=rejected.lines) from None
        sensitive = file_secret_values(request.files, uses)
        project = replace(
            request.project,
            sources=delivered_sources(uses, request.files),
            sensitive=sensitive,
        )
        secrets = request.secrets | sensitive
        scrubber = make_scrubber(secrets)
        try:
            async with request.cli.invocation(
                project, request.target, request.values, scrubber
            ) as run:
                resolved = await run.config(slot_wait=request.slot_wait)
            return hide_keys(resolved, secret_env_keys(request.files, uses)), project, secrets
        except ComposeBusy:
            raise StackBusy from None
        except ComposeTimedOut as error:
            raise StackRejected(
                {COMPOSE_FIELD: "Docker Compose did not finish checking this file in time."},
                "invalid_compose",
                stderr=error.stderr,
            ) from None
        except ComposeCommandFailed as error:
            raise StackRejected(
                {COMPOSE_FIELD: "Docker Compose rejected this file."},
                "invalid_compose",
                stderr=error.stderr,
            ) from None
        except (ComposeOutputInvalid, ComposeOutputTooLarge):
            raise StackRejected(
                {COMPOSE_FIELD: "Docker Compose returned a result dockmin cannot use."},
                "invalid_compose",
            ) from None
        except ComposeError as error:
            raise compose_to_stack_error(error) from None

    def _everyone(
        self,
        resolved: ResolvedProject,
        request: ValidationRequest,
        project: ComposeProject,
        admission: BuildAdmission | None,
    ) -> None:
        directory = project.directory.as_posix()
        refused = resolved_refusals(
            resolved,
            companions=[f"{directory}/{item.name}" for item in request.files],
            generated=generated_names(project),
            builds=None if admission is None else replace(admission, directory=directory).check,
        )
        if refused:
            raise StackRejected(refused, "refused_construct")
        if admission is not None:
            leaked = secret_build_arguments(resolved.services, request.secrets | project.sensitive)
            if leaked:
                raise StackRejected(leaked, SECRET_IN_BUILD_ARG)
        problems = limits(resolved)
        if problems:
            raise StackRejected(problems, "invalid")
        labels = reserved_labels(resolved)
        if labels:
            raise _restricted(labels, {})

    async def _default_runtime(self, client: EngineClient, resolved: ResolvedProject) -> str | None:
        if not any(service.get("runtime") for service in resolved.services.values()):
            return None
        try:
            return (await client.system.info()).default_runtime or None
        except EngineError as error:
            raise unavailable_from(error) from None

    async def _reach(
        self,
        actor: Acting,
        environment_id: int,
        scanned: StackScan,
        found: list[StackFinding],
    ) -> dict[str, str]:
        errors: dict[str, str] = {}
        volumes: dict[str, tuple[str, bool]] = {}
        for use in scanned.external:
            if use.kind == "volume":
                path, read_only = volumes.get(use.name, (use.path, True))
                volumes[use.name] = (path, read_only and use.read_only)
        for volume_name, (path, read_only) in volumes.items():
            await self._reach_volume(
                actor, environment_id, volume_name, path, read_only, found, errors
            )
        for use in scanned.external:
            if use.kind == "network":
                await self._reach_network(actor, environment_id, use.name, use.path, found, errors)
        return errors

    async def _reach_volume(
        self,
        actor: Acting,
        environment_id: int,
        name: str,
        path: str,
        read_only: bool,
        found: list[StackFinding],
        errors: dict[str, str],
    ) -> None:
        try:
            volume = await self._inspector.volume(environment_id, name)
            await self._require_access(
                actor, environment_id, "volume", name, "read" if read_only else "write"
            )
        except (ResourceNotFound, PermissionDenied, ObjectTooLarge):
            errors.setdefault(path, VOLUME_UNAVAILABLE)
            return
        if volume.driver != "local" or volume.options:
            found.append(StackFinding("host_backed_volume", path))

    async def _reach_network(
        self,
        actor: Acting,
        environment_id: int,
        name: str,
        path: str,
        found: list[StackFinding],
        errors: dict[str, str],
    ) -> None:
        try:
            network = await self._network(environment_id, name)
        except ResourceNotFound:
            errors.setdefault(path, NETWORK_UNAVAILABLE)
            return
        except ObjectTooLarge:
            errors.setdefault(path, NETWORK_UNAVAILABLE)
            return
        try:
            await self._require_access(actor, environment_id, "network", network.id, "write")
        except (ResourceNotFound, PermissionDenied):
            errors.setdefault(path, NETWORK_UNAVAILABLE)
            return
        if network.driver == "host":
            found.append(StackFinding("network_host", path))

    async def _require_access(
        self,
        actor: Acting,
        environment_id: int,
        object_type: Literal["volume", "network"],
        object_id: str,
        level: Level,
    ) -> None:
        if isinstance(actor, AutomationAuthority):
            key = ObjectKey(environment_id, object_type, object_id)
            if level == "read":
                await actor.authorise_read(key)
            else:
                await actor.authorise(key)
            return
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, object_type, object_id), level
        )

    async def _network(self, environment_id: int, key: str) -> Network:
        if NETWORK_ID.fullmatch(key):
            try:
                return await self._inspector.network(environment_id, key)
            except ResourceNotFound:
                pass
        return await self._inspector.network_named(environment_id, key)

    async def _preexisting(
        self, environment_id: int, name: str, scanned: StackScan
    ) -> tuple[str, ...]:
        """Refuse project objects that exist and are not this stack's.

        Returns the project volumes that do not exist yet but carry a control left by a volume
        removed outside the product, which the deployment replaces.
        """
        stack = canonical_stack_key(name)
        objects: list[str] = []
        stale: list[str] = []
        for volume_name in scanned.project_volumes:
            try:
                volume = await self._inspector.volume(environment_id, volume_name)
            except ResourceNotFound:
                if await self._controls.get_for(environment_id, "volume", volume_name):
                    stale.append(volume_name)
                continue
            except ObjectTooLarge:
                objects.append(f"Volume {volume_name}")
                continue
            control = await self._controls.get_for(environment_id, "volume", volume.name)
            if self._imported is not None and self._imported.registered(environment_id, stack):
                own = self._imported.accepts(environment_id, stack, volume.name, volume.created_at)
            else:
                own = in_project(volume.labels, stack)
            if not own or control is not None:
                objects.append(f"Volume {volume_name}")
        for network_name in scanned.project_networks:
            try:
                network = await self._inspector.network_named(environment_id, network_name)
            except ResourceNotFound:
                continue
            except ObjectTooLarge:
                objects.append(f"Network {network_name}")
                continue
            control = await self._controls.get_for(environment_id, "network", network.id)
            if not in_project(network.labels, stack) or control is not None:
                objects.append(f"Network {network_name}")
        if objects:
            raise PreexistingObjects(objects)
        return tuple(stale)

    async def _to_pull(self, request: ValidationRequest, scanned: StackScan) -> tuple[str, ...]:
        wanted: list[str] = []
        invalid: dict[str, str] = {}
        for reference, (service, _) in scanned.images.items():
            try:
                parse_pull_reference(reference)
            except ValidationFailed:
                invalid[f"services.{service}.image"] = "This image reference is not valid."
        if invalid:
            raise StackRejected(invalid, "invalid")
        for reference, (service, policy) in scanned.images.items():
            folded = policy.casefold()
            if request.pull or folded == PULL_ALWAYS:
                wanted.append(reference)
                continue
            if await _present(request.client, reference):
                continue
            if folded == PULL_NEVER:
                if request.deploy:
                    raise StackRejected(
                        {
                            f"services.{service}.pull_policy": (
                                "The image is not on this host and the pull policy is never."
                            )
                        },
                        "image_missing",
                    )
                continue
            wanted.append(reference)
        return tuple(wanted)

    async def _resolve_pulls(
        self,
        actor: Acting,
        request: ValidationRequest,
        wanted: Sequence[tuple[str, str]],
    ) -> tuple[PullPlan, ...]:
        plans: list[PullPlan] = []
        problems: dict[str, str] = {}
        reason = "credential_choice_required"
        principal = None if isinstance(actor, AutomationAuthority) else actor
        for reference, service in wanted:
            try:
                parsed = parse_pull_reference(reference)
            except ValidationFailed:
                problems[f"services.{service}.image"] = "This image reference is not valid."
                reason = stronger_reason(reason, "invalid")
                continue
            try:
                credentials = await self._credentials.resolve(
                    request.environment.id,
                    reference,
                    principal=principal,
                    choice=None if principal is None else request.choices.get(reference),
                )
                if (
                    isinstance(actor, AutomationAuthority)
                    and credentials.credential_set_id is not None
                ):
                    await actor.authorise_credential_set(credentials.credential_set_id)
            except (CredentialChoiceRequired, CredentialSetMismatch) as error:
                problems[reference] = str(error)
            except BindingCredentialMissing as error:
                problems[reference] = str(error)
                reason = stronger_reason(reason, "credential_missing")
            except CredentialSetUnavailable as error:
                problems[reference] = str(error)
                reason = stronger_reason(reason, "credential_unavailable")
            except RebindRefused as error:
                problems[reference] = str(error)
                reason = stronger_reason(reason, "restricted")
            except CandidateListTooLarge as error:
                problems[reference] = str(error)
                reason = stronger_reason(reason, "invalid")
            else:
                plans.append(PullPlan(reference, parsed, credentials, service))
        if problems:
            raise StackRejected(problems, reason)
        return tuple(plans)


async def require_build_rights(actor: Acting, text: str) -> None:
    """Raise StackRejected (`build_administrator_only`) unless an administrator deploys a build."""
    if is_administrator(actor):
        return
    names = await services_with_build(text)
    if names:
        raise StackRejected(
            {f"services.{names[0]}.build": BUILD_ADMINISTRATOR_ONLY}, "build_administrator_only"
        )


async def refuse_builds_over_json(text: str) -> None:
    """Raise StackRejected (`build_not_available`) when the file has a service that builds."""
    names = await services_with_build(text)
    if names:
        raise StackRejected(
            {f"services.{names[0]}.build": BUILD_NOT_AVAILABLE}, "build_not_available"
        )


def raise_if_refused(report: CollisionReport) -> tuple[str, ...]:
    """Raise StackRejected for a check that refused; otherwise return its warnings."""
    if report.fields:
        raise StackRejected(
            dict(report.fields), report.reason, summary=report.summary, warnings=report.warnings
        )
    return report.warnings


def in_project(labels: Mapping[str, str], key: str) -> bool:
    """Report whether the labels place an object in the Compose project with this stack key."""
    value = labels.get(COMPOSE_PROJECT_LABEL)
    return bool(value) and canonical_stack_key(value or "") == key


def _restricted(found: Sequence[StackFinding], errors: Mapping[str, str]) -> StackRejected:
    views = [FindingView(f.path, f.option, f.label, f.message) for f in found]
    fields = {view.path: view.message for view in views}
    fields.update(errors)
    return StackRejected(
        fields, "restricted", findings=views, restricted=refused_stack_options(found)
    )


async def _base_pulls(
    request: ValidationRequest, built: Sequence[BuildPlan], taken: set[str]
) -> list[tuple[str, str]]:
    """Return the (reference, service) of each base a build needs that has to be pulled."""
    wanted: list[tuple[str, str]] = []
    for plan in built:
        if plan.reuse:
            continue
        for base in plan.bases:
            if base in taken:
                continue
            taken.add(base)
            if request.pull or not await _present(request.client, base):
                wanted.append((base, plan.service))
    return wanted


async def _present(client: EngineClient, reference: str) -> bool:
    try:
        await client.images.inspect(reference)
    except EngineNotFound:
        return False
    except ResponseTooLarge:
        return True
    except EngineError as error:
        raise unavailable_from(error) from None
    return True


def _check_object_names(scanned: StackScan) -> None:
    bad = [
        name
        for name in (*scanned.project_volumes, *scanned.project_networks)
        if VOLUME_OR_NETWORK_NAME.fullmatch(name) is None
    ]
    if bad:
        raise StackRejected(
            {"volumes": "A volume or network name is not a valid Docker name."}, "invalid"
        )


def _shown(value: object) -> str:
    return str(value)[:MAX_SHOWN_CHARS]


def _port_text(port: object) -> str | None:
    if isinstance(port, Mapping):
        target = port.get("target")
        published = port.get("published")
        protocol = port.get("protocol") or "tcp"
        if target is None:
            return None
        head = f"{published}:" if published not in (None, "") else ""
        return _shown(f"{head}{target}/{protocol}")
    if isinstance(port, str | int):
        return _shown(port)
    return None


def _service_summaries(document: Mapping[str, object]) -> tuple[ServiceSummary, ...]:
    services = document.get("services")
    if not isinstance(services, Mapping):
        return ()
    result: list[ServiceSummary] = []
    for key, service in services.items():
        if not isinstance(service, Mapping):
            continue
        image = service.get("image")
        raw_ports = service.get("ports")
        ports = tuple(
            text
            for text in (
                _port_text(port) for port in (raw_ports if isinstance(raw_ports, list) else ())
            )
            if text is not None
        )[:MAX_SHOWN_PORTS]
        networks = service.get("networks")
        volumes = service.get("volumes")
        result.append(
            ServiceSummary(
                name=_shown(key),
                image=_shown(image) if isinstance(image, str) else None,
                ports=ports,
                networks=tuple(_shown(n) for n in networks)
                if isinstance(networks, Mapping | list)
                else (),
                volumes=len(volumes) if isinstance(volumes, list) else 0,
            )
        )
    return tuple(result)
