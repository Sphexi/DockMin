"""What the per-file pages of Compose and Swarm stacks share: the form and a refusal."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Final

from fastapi import Request
from starlette.datastructures import UploadFile

from app.services.stack_deploy import StackRejected
from app.services.stack_document import (
    MAX_FILE_BYTES,
    ComposeDocumentRejected,
    decode_file,
    file_name,
)
from app.web.object_pages import Failure
from app.web.stack_pages import CHECKED, Problem, Rejection

BOM_BYTES: Final = 3
BLANK_CONTENT: Final = "Enter the file's content or choose a file to upload."
BOTH_CONTENT: Final = "Use either the text box or a file, not both."
FIELD_KEYS: Final = ("name", "content")


@dataclass(frozen=True, slots=True)
class Submission:
    """A posted file form after the checks that need no stack.

    `content` is the text to store, or None to keep a secret file's value. `errors` are the
    field problems found while reading, and the page is shown again when there are any.
    """

    name: str
    content: str | None = field(repr=False)
    secret: bool
    expected_inputs_digest: str
    errors: Mapping[str, str]
    shown: str = field(repr=False, default="")


@dataclass(frozen=True, slots=True)
class FileState:
    """What a file page shows: the values, the problems and where the first one sits."""

    name: str
    content: str | None = field(repr=False)
    secret: bool
    errors: Mapping[str, str] = field(default_factory=dict)
    content_line: int | None = None
    rejection: Rejection | None = None
    failure: Failure | None = None


async def read_submission(request: Request, *, fixed_name: str | None, keeps: bool) -> Submission:
    """Read one file form.

    `fixed_name` is the file being edited. `keeps` is true when a secret file is stored under
    it, so that a secret left with no content keeps its value.
    """
    posted = await request.form()
    name = fixed_name if fixed_name is not None else _text(posted.get("name")).strip()
    pasted = _text(posted.get("content"))
    secret = _text(posted.get("secret")).strip().casefold() in CHECKED
    digest = _text(posted.get("expected_inputs_digest"))
    uploaded = posted.get("upload")
    raw = b""
    if isinstance(uploaded, UploadFile) and uploaded.filename:
        raw = await uploaded.read(MAX_FILE_BYTES + BOM_BYTES + 1)
    errors: dict[str, str] = {}
    if fixed_name is None:
        try:
            file_name(name)
        except ComposeDocumentRejected as rejected:
            errors.update(rejected.fields)
    content: str | None = None
    shown = pasted
    if raw and pasted != "":
        errors["content"] = BOTH_CONTENT
    elif raw or pasted != "":
        try:
            content = decode_file(raw or pasted)
        except ComposeDocumentRejected as rejected:
            errors.update(rejected.fields)
        else:
            shown = content
    elif not (keeps and secret):
        errors["content"] = BLANK_CONTENT
    return Submission(name, content, secret, digest, errors, "" if secret else shown)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def state_of_submission(submission: Submission) -> FileState:
    """Return the state to show for a form that has problems of its own."""
    return FileState(
        submission.name,
        None if submission.secret else submission.shown,
        submission.secret,
        submission.errors,
    )


def state_of_refusal(
    submission: Submission, problem: Problem, error: Exception, shown_file: str
) -> FileState:
    """Return the state to show for a form the stack refused.

    A refusal found in the text of this very file is placed on its content, with its line.
    """
    fields = error.fields if isinstance(error, StackRejected) else {}
    lines = error.lines if isinstance(error, StackRejected) else {}
    errors: dict[str, str] = {key: fields[key] for key in FIELD_KEYS if key in fields}
    line = lines.get("content")
    carried = dict(problem.errors)
    inside = fields.get("files", "")
    if inside.startswith(f"{shown_file}: ") and "content" not in errors:
        errors["content"] = inside
        line = lines.get("files")
        carried.pop("files", None)
    rejection = problem.rejection
    if rejection is not None:
        rejection = replace(
            rejection,
            problems=tuple(
                (key, message) for key, message in rejection.problems if key not in FIELD_KEYS
            ),
        )
    base = state_of_submission(submission)
    return replace(
        base,
        errors={**carried, **errors},
        content_line=line,
        rejection=rejection,
        failure=problem.failure,
    )
