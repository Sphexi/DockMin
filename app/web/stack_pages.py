"""What the Compose and Swarm stack pages share: form reading, problems, offers and job streams."""

import asyncio
import re
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

import anyio
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile
from starlette.types import Receive, Scope, Send

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.stack import StackKind
from app.services.assembly import StackServices
from app.services.image_bindings import CredentialOffer, parse_pull_reference
from app.services.object_locks import StreamSlot
from app.services.pull_credentials import CredentialChoice
from app.services.stack_deploy import FindingView, StackRejected
from app.services.stack_document import (
    MAX_COMPOSE_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_VARIABLES,
    CompanionFile,
    ComposeDocumentRejected,
    VariableInput,
    decode_compose,
    decode_file,
    line_index,
    parse_files,
    parse_variables,
)
from app.services.stack_jobs import StackJobRegistry
from app.services.stack_views import ValidationReport, VariableView, read_intake
from app.web.image_helpers import credential_choice_of, selected_choice
from app.web.object_pages import Failure
from app.web.templating import render_fragment

VARIABLE_CAP: Final = MAX_VARIABLES
FILE_CAP: Final = MAX_FILES
BLANK_ROWS: Final = 2
BLANK_FILE_ROWS: Final = 1
BOM_BYTES: Final = 3
ROW_STEP: Final = 2
MAX_ROW_INDEX: Final = 9999
MAX_OFFERS: Final = 50
ID_DIGITS: Final = 18
CHECKED: Final = frozenset({"on", "true", "1", "yes"})
UPLOAD_FIELD: Final = "compose_upload"
SCALAR_FIELDS: Final = frozenset(
    {
        "name",
        "compose_file",
        "rows",
        "next",
        "pull",
        "rebuild",
        "remove_orphans",
        "confirm_force",
        "remove_volumes",
        "deploy",
        "expected_file_digest",
        "expected_inputs_digest",
        "expected_stack_id",
        "expected_containers",
        "expected_builds",
        "expected_networks",
        "expected_volumes",
        "expected_plan",
        "expected_members",
    }
)
VARIABLE_FIELD: Final = re.compile(r"variable-([0-9]{1,4})-(name|value|secret|remove|stored)")
CREDENTIAL_FIELD: Final = re.compile(r"credential-([0-9]{1,3})-(reference|choice)")
FILE_FIELD: Final = re.compile(r"file-([0-9]{1,4})-(name|content|secret)")
FILE_UPLOAD_FIELD: Final = re.compile(r"file-([0-9]{1,4})-upload")
FORM_FIELD: Final = re.compile(
    r"name|compose_file|variables|files|variable-[0-9]{1,4}-(?:name|value)"
    r"|file-[0-9]{1,4}-(?:name|content)"
)
CREDENTIAL_REASONS: Final = frozenset({"credential_choice_required", "credential_unavailable"})
TITLES: Final = {
    "invalid_compose": "Docker Compose rejected the file",
    "refused_construct": "The file uses something dockmin does not allow",
    "restricted": "The file uses options your role cannot deploy",
    "too_large": "The file is too large",
    "image_missing": "An image the stack needs is not on the host",
    "build_administrator_only": "Only an administrator can deploy a stack that builds images",
    "build_not_available": "Builds are not available here",
    "buildx_unavailable": "Docker buildx is not available",
    "tag_in_use": "An image name is already in use",
    "built_image_missing": "The built image is missing",
    "secret_in_build_arg": "A build argument holds a secret",
    "credential_choice_required": "Choose how to pull the images",
    "credential_unavailable": "A credential set cannot be used",
    "secrets_unreadable": "A stored secret cannot be read",
    "file_missing": "The file names a file that is not added",
    "file_in_use": "The file is still in use",
    "container_name_in_use": "Container name in use",
    "port_in_use": "Port in use",
    "port_range_too_large": "Port range too large",
}
DISMISSED_PAGE: Final = "1"
DISMISSED_PARAM: Final = "dismissed"
SSE_MEDIA_TYPE: Final = "text/event-stream"
STREAM_HEADERS: Final = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
POLL_SECONDS: Final = 1.0
KEEP_ALIVE_SECONDS: Final = 15.0
LIFETIME_SECONDS: Final = 300.0
RETRY_MILLISECONDS: Final = 3000
PROGRESS_EVENT: Final = "progress"
DONE_EVENT: Final = "done"


@dataclass(frozen=True, slots=True)
class Rejection:
    """What a rejected file or variable set is shown with."""

    title: str
    reason: str
    problems: tuple[tuple[str, str], ...]
    findings: tuple[FindingView, ...]
    stderr: str
    restricted: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    finding_lines: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VariableRow:
    """One variable row of the form; a secret never carries its value."""

    index: int
    name: str
    value: str | None
    secret: bool
    stored: bool
    remove: bool
    name_error: str | None = None
    value_error: str | None = None


@dataclass(frozen=True, slots=True)
class FileRow:
    """One companion file row of the form; a secret file never carries its content."""

    index: int
    name: str
    content: str | None
    secret: bool
    name_error: str | None = None
    content_error: str | None = None
    content_line: int | None = None


@dataclass(frozen=True, slots=True)
class OfferChoice:
    """The credential choice one image reference needs."""

    index: int
    reference: str
    offer: CredentialOffer
    selected: str
    error: str | None


@dataclass(frozen=True, slots=True)
class FormState:
    """Everything a stack form shows beyond its fixed text."""

    name: str = ""
    compose_text: str = ""
    rows: tuple[VariableRow, ...] = ()
    file_rows: tuple[FileRow, ...] = ()
    errors: Mapping[str, str] = field(default_factory=dict)
    lines: Mapping[str, int] = field(default_factory=dict)
    rejection: Rejection | None = None
    report: ValidationReport | None = None
    failure: Failure | None = None
    offers: tuple[OfferChoice, ...] = ()
    deploy: bool = False


@dataclass(frozen=True, slots=True)
class Problem:
    """How a refused action is answered."""

    status: int
    errors: Mapping[str, str] = field(default_factory=dict)
    rejection: Rejection | None = None
    failure: Failure | None = None
    reference_errors: Mapping[str, str] = field(default_factory=dict)
    lines: Mapping[str, int] = field(default_factory=dict)

    @property
    def references(self) -> tuple[str, ...]:
        """Return the image references that still need a credential choice."""
        return tuple(self.reference_errors)


@dataclass(slots=True)
class StackForm:
    """The fields of a posted stack form, split by kind."""

    scalars: dict[str, str] = field(default_factory=dict)
    uploaded: bytes | None = None
    variables: dict[str, str] = field(default_factory=dict)
    files: dict[str, str | bytes] = field(default_factory=dict)
    credentials: dict[int, dict[str, str]] = field(default_factory=dict)

    def get(self, key: str) -> str:
        """Return a scalar field, or the empty string."""
        return self.scalars.get(key, "")

    def flag(self, key: str) -> bool:
        """Report whether a checkbox field was ticked."""
        return self.get(key).strip().casefold() in CHECKED

    def adding_rows(self) -> bool:
        """Report whether the form was posted only to show more variable or file rows."""
        return self.get("rows") in {"variable", "file"}

    def posted_choices(self) -> dict[str, str]:
        """Return the credential choice text by image reference, as submitted."""
        chosen: dict[str, str] = {}
        for _, pair in sorted(self.credentials.items()):
            reference = pair.get("reference", "").strip()
            if reference:
                chosen[reference] = pair.get("choice", "")
        return chosen

    def choices(self) -> dict[str, CredentialChoice]:
        """Return the credential choices made; ValidationFailed for a value not on offer."""
        made: dict[str, CredentialChoice] = {}
        for reference, text in self.posted_choices().items():
            choice = credential_choice_of(text)
            if choice is not None:
                made[reference] = choice
        return made

    def variable_inputs(self) -> tuple[VariableInput, ...]:
        """Return the submitted variable rows."""
        return parse_variables(self.variables)

    def file_inputs(self) -> tuple[CompanionFile, ...]:
        """Return the submitted companion file rows; StackRejected keyed by the row's field."""
        try:
            return parse_files(self.files)
        except ComposeDocumentRejected as rejected:
            raise StackRejected(rejected.fields, rejected.reason, lines=rejected.lines) from None

    def text(self) -> str:
        """Return the Compose file from the paste or the upload; StackRejected otherwise."""
        return read_intake(pasted=self.scalars.get("compose_file"), uploaded=self.uploaded)

    def display_text(self) -> str:
        """Return the file to show again: the paste, else the upload when it is readable."""
        pasted = self.get("compose_file")
        if pasted.strip() or not self.uploaded:
            return pasted
        try:
            return decode_compose(self.uploaded)
        except ComposeDocumentRejected:
            return ""


async def read_stack_form(request: Request) -> StackForm:
    """Read a posted stack form, taking only the fields the pages define."""
    posted = await request.form()
    form = StackForm()
    for key, value in posted.multi_items():
        if isinstance(value, UploadFile):
            if key == UPLOAD_FIELD and value.filename:
                form.uploaded = await value.read(MAX_COMPOSE_BYTES + 1)
            elif FILE_UPLOAD_FIELD.fullmatch(key) and value.filename:
                form.files[key] = await value.read(MAX_FILE_BYTES + BOM_BYTES + 1)
            continue
        if key in SCALAR_FIELDS:
            form.scalars[key] = value
        elif VARIABLE_FIELD.fullmatch(key):
            form.variables[key] = value
        elif FILE_FIELD.fullmatch(key):
            form.files[key] = value
        elif (found := CREDENTIAL_FIELD.fullmatch(key)) is not None:
            form.credentials.setdefault(int(found[1]), {})[found[2]] = value
    return form


def rows_from_stored(variables: Sequence[VariableView]) -> list[VariableRow]:
    """Build the editor's rows from the stored variables."""
    return [
        VariableRow(index, item.name, item.value, item.secret, True, False)
        for index, item in enumerate(variables)
    ]


def rows_from_form(variables: Mapping[str, str], errors: Mapping[str, str]) -> list[VariableRow]:
    """Build rows from a posted form, dropping blank ones and every secret value."""
    grouped: dict[int, dict[str, str]] = {}
    for key, value in variables.items():
        found = VARIABLE_FIELD.fullmatch(key)
        if found is not None:
            grouped.setdefault(int(found[1]), {})[found[2]] = value
    rows: list[VariableRow] = []
    for index in sorted(grouped):
        data = grouped[index]
        stored = data.get("stored") == "1"
        name = data.get("name", "").strip()
        value = data.get("value", "")
        if not stored and not name and value == "":
            continue
        secret = data.get("secret", "").strip().casefold() in CHECKED
        rows.append(
            VariableRow(
                index=index,
                name=name,
                value=None if secret else value,
                secret=secret,
                stored=stored,
                remove=data.get("remove", "").strip().casefold() in CHECKED,
                name_error=errors.get(f"variable-{index}-name"),
                value_error=errors.get(f"variable-{index}-value"),
            )
        )
    return rows


def padded(rows: Sequence[VariableRow], *, add: bool = False) -> tuple[VariableRow, ...]:
    """Return the rows followed by blank ones, within the most a stack takes."""
    start = max((row.index for row in rows), default=-1) + 1
    wanted = BLANK_ROWS + (ROW_STEP if add else 0)
    room = min(max(0, VARIABLE_CAP - len(rows)), MAX_ROW_INDEX + 1 - start)
    blanks = [
        VariableRow(start + offset, "", "", False, False, False)
        for offset in range(min(wanted, room))
    ]
    return (*rows, *blanks)


def _file_text(value: str | bytes | None) -> str:
    return value if isinstance(value, str) else (value or b"").decode("utf-8", "replace")


def file_rows_from_form(
    files: Mapping[str, str | bytes], errors: Mapping[str, str], lines: Mapping[str, int]
) -> list[FileRow]:
    """Build file rows from a posted form, dropping blank ones and every secret file's content.

    A plain file given as an upload is shown as the text it holds when that reads as text.
    """
    grouped: dict[int, dict[str, str | bytes]] = {}
    for key, value in files.items():
        found = FILE_FIELD.fullmatch(key) or FILE_UPLOAD_FIELD.fullmatch(key)
        if found is not None:
            part = found[2] if found.re is FILE_FIELD else "upload"
            grouped.setdefault(int(found[1]), {})[part] = value
    rows: list[FileRow] = []
    for index in sorted(grouped):
        data = grouped[index]
        name = _file_text(data.get("name")).strip()
        pasted = _file_text(data.get("content"))
        uploaded = data.get("upload")
        if not name and not pasted.strip() and not uploaded:
            continue
        secret = _file_text(data.get("secret")).strip().casefold() in CHECKED
        shown = pasted
        if not shown and isinstance(uploaded, bytes) and not secret:
            try:
                shown = decode_file(uploaded)
            except ComposeDocumentRejected:
                shown = ""
        content_field = f"file-{index}-content"
        rows.append(
            FileRow(
                index=index,
                name=name,
                content=None if secret else shown,
                secret=secret,
                name_error=errors.get(f"file-{index}-name"),
                content_error=errors.get(content_field),
                content_line=lines.get(content_field),
            )
        )
    return rows


def padded_files(rows: Sequence[FileRow], *, add: bool = False) -> tuple[FileRow, ...]:
    """Return the file rows followed by blank ones, within the most a stack takes."""
    start = max((row.index for row in rows), default=-1) + 1
    wanted = BLANK_FILE_ROWS + (ROW_STEP if add else 0)
    room = min(max(0, FILE_CAP - len(rows)), MAX_ROW_INDEX + 1 - start)
    blanks = [FileRow(start + offset, "", "", False) for offset in range(min(wanted, room))]
    return (*rows, *blanks)


def rejection_of(error: StackRejected) -> Rejection:
    """Describe a rejection: what to list beside the fields, and Compose's own text."""
    covered = {finding.path for finding in error.findings}
    credential = error.reason in CREDENTIAL_REASONS
    problems = tuple(
        (key, message)
        for key, message in error.fields.items()
        if FORM_FIELD.fullmatch(key) is None and key not in covered and not credential
    )
    return Rejection(
        title=TITLES.get(error.reason, "Check the form"),
        reason=error.reason,
        problems=problems,
        findings=error.findings,
        stderr=error.stderr,
        restricted=error.restricted,
        warnings=error.warnings,
    )


async def located(problem: Problem, text: str) -> Problem:
    """Return the problem with a line for each restricted option the file's own text places.

    Compose's findings are keyed by path on the resolved model; a path the text spells out
    literally gets its line, and others stay without one.
    """
    rejection = problem.rejection
    if rejection is None or not rejection.findings or rejection.finding_lines:
        return problem
    index = await asyncio.to_thread(line_index, text)
    found = {
        finding.path: index[finding.path] for finding in rejection.findings if finding.path in index
    }
    if not found:
        return problem
    return replace(problem, rejection=replace(rejection, finding_lines=found))


def posted_stack_id(text: str) -> int:
    """Return the posted stack id, or -1, which no stack has, for anything that is not one."""
    if text.isascii() and text.isdigit() and len(text) <= ID_DIGITS:
        return int(text)
    return -1


def failure_of(problem: Problem) -> Failure:
    return problem.failure or Failure(problem.status, "Check the request", "The request failed.")


async def offers_for(
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    references: Sequence[str],
    posted: Mapping[str, str],
    errors: Mapping[str, str],
    *,
    include_posted: bool = True,
) -> tuple[tuple[OfferChoice, ...], str | None]:
    """Build the credential pickers to show: the ones already posted, then the ones now needed."""
    wanted = list(dict.fromkeys([*(posted if include_posted else ()), *references]))[:MAX_OFFERS]
    if not wanted:
        return (), None
    try:
        offers = await stacks.queries.credential_pickers(actor, environment_id, wanted)
    except ValidationFailed as invalid:
        return (), "; ".join(invalid.fields.values())
    return (
        tuple(
            OfferChoice(
                index=index,
                reference=reference,
                offer=offer,
                selected=selected_choice(offer, posted.get(reference, "")),
                error=errors.get(reference),
            )
            for index, (reference, offer) in enumerate(zip(wanted, offers, strict=True))
        ),
        None,
    )


def pullable(reference: str) -> bool:
    try:
        parse_pull_reference(reference)
    except ValidationFailed:
        return False
    return True


async def offers_for_report(
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    form: StackForm,
    report: ValidationReport | None,
    *,
    every_image: bool = False,
) -> tuple[OfferChoice, ...]:
    """Build the credential pickers for a validated file: its pulls, or every image when asked."""
    if report is None:
        return ()
    references = [
        reference
        for reference in (report.images if every_image else report.to_pull)
        if pullable(reference)
    ]
    offers, _ = await offers_for(
        stacks,
        actor,
        environment_id,
        references,
        form.posted_choices(),
        {},
        include_posted=False,
    )
    return offers


def state_from(
    form: StackForm,
    *,
    problem: Problem | None = None,
    report: ValidationReport | None = None,
    offers: tuple[OfferChoice, ...] = (),
    name: str | None = None,
    stored_rows: Sequence[VariableRow] = (),
) -> FormState:
    errors = problem.errors if problem else {}
    lines = problem.lines if problem else {}
    posted_rows = rows_from_form(form.variables, errors)
    rows = posted_rows or list(stored_rows)
    return FormState(
        name=form.get("name").strip() if name is None else name,
        compose_text=form.display_text(),
        rows=padded(rows, add=form.get("rows") == "variable"),
        file_rows=padded_files(
            file_rows_from_form(form.files, errors, lines), add=form.get("rows") == "file"
        ),
        errors=errors,
        lines=lines,
        rejection=problem.rejection if problem else None,
        report=report,
        failure=problem.failure if problem else None,
        offers=offers,
        deploy=form.flag("deploy"),
    )


async def problem_state(
    stacks: StackServices,
    actor: Principal,
    environment_id: int,
    form: StackForm,
    problem: Problem,
    *,
    name: str | None = None,
    stored_rows: Sequence[VariableRow] = (),
) -> FormState:
    """Build the form state for a refused post, with the credential choices it now needs."""
    offers, offers_error = await offers_for(
        stacks,
        actor,
        environment_id,
        problem.references,
        form.posted_choices(),
        problem.reference_errors,
    )
    problem = await located(problem, form.display_text())
    state = state_from(form, problem=problem, offers=offers, name=name, stored_rows=stored_rows)
    if offers_error:
        return replace(state, failure=Failure(400, "Check the request", offers_error))
    return state


def event(name: str, data: str) -> str:
    """Format one server-sent event."""
    lines = data.replace("\r", "").split("\n")
    return f"event: {name}\n" + "".join(f"data: {line}\n" for line in lines) + "\n"


def _render(request: Request, template: str, macro: str, job: object) -> str:
    response = render_fragment(request, template, macro, job=job)
    return bytes(response.body).decode("utf-8").strip()


async def job_frames(
    request: Request,
    jobs: StackJobRegistry,
    actor: Principal,
    environment_id: int,
    job_id: str,
    slot: StreamSlot,
    *,
    stack_kind: StackKind,
    template: str,
    macro: str,
    poll: float = POLL_SECONDS,
    keep_alive: float = KEEP_ALIVE_SECONDS,
    lifetime: float = LIFETIME_SECONDS,
    retry: int = RETRY_MILLISECONDS,
) -> AsyncGenerator[str, None]:
    """Send the job's state on each change, then a last event once it has ended.

    Nothing is sent for a job of another environment or another kind of stack.
    """
    loop = asyncio.get_running_loop()
    started = quiet_since = loop.time()
    seen: int | None = None
    try:
        while True:
            try:
                job = jobs.view(job_id, actor)
            except ResourceNotFound:
                return
            if job.environment_id != environment_id or job.stack_kind != stack_kind:
                return
            if job.version != seen:
                seen = job.version
                quiet_since = loop.time()
                yield event(
                    PROGRESS_EVENT if job.running else DONE_EVENT,
                    _render(request, template, macro, job),
                )
                if not job.running:
                    return
            elif loop.time() - quiet_since >= keep_alive:
                quiet_since = loop.time()
                yield ": keep-alive\n\n"
            if loop.time() - started >= lifetime:
                yield f"retry: {retry}\n\n"
                return
            await asyncio.sleep(poll)
    finally:
        slot.close()


class JobResponse(StreamingResponse):
    """A stream response that returns its slot on every way out, even before the first chunk."""

    def __init__(self, slot: StreamSlot, content: AsyncGenerator[str, None]) -> None:
        super().__init__(content, media_type=SSE_MEDIA_TYPE, headers=STREAM_HEADERS)
        self._slot = slot
        self._content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._content.aclose()
                self._slot.close()
