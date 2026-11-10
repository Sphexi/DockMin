"""What the onboarding pages show, and how the review form is read back into a selection."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from starlette.datastructures import FormData

from app.auth.errors import ValidationFailed
from app.services.onboarding_ownership import Ownership
from app.services.onboarding_plan import (
    ANONYMOUS,
    ImageTarget,
    ObjectTarget,
    OnboardingReview,
    Selection,
    StackTarget,
)
from app.services.pull_credentials import CredentialChoice
from app.web.stack_read_pages import REASONS, SOURCES

__all__ = [
    "ImageRow",
    "ObjectRow",
    "StackRow",
    "image_rows",
    "object_rows",
    "owner_fields",
    "read_selection",
    "stack_rows",
]

INDEX_DIGITS: Final = 4
ID_DIGITS: Final = 18
FIELD_MAX: Final = 256
EXTRA_REASONS: Final = {
    "invalid_name": "The name cannot be a stack name.",
    "name_in_use": "A Compose stack of this name is managed.",
    "busy": "Another action on it is in progress.",
    "unreadable": "The files could not be read.",
}
OUTCOMES: Final = {
    "ambiguous": "More than one set can read this image. Choose one.",
    "anonymous_only": "The registry answered without credentials; the image needs no binding.",
    "no_match": "No credential set could be matched to the image on the host.",
    "no_local_digest": "The host holds no registry digest for it; it was built or tagged locally.",
    "pinned": "It names a digest, so it needs no binding.",
    "not_checked": "Not checked: the time or reference limit was reached.",
}
CHECK_ERRORS: Final = {
    "authentication": "refused the credentials",
    "not_found": "has no such repository or tag",
    "timeout": "did not answer in time",
    "dns": "could not be resolved",
    "tcp": "could not be reached",
    "tls": "failed the TLS handshake",
    "certificate": "certificate not accepted",
    "rate_limited": "is limiting requests",
    "protocol": "reply not understood",
    "missing": "credential set unavailable",
}
_INDEX = re.compile(rf"[0-9]{{1,{INDEX_DIGITS}}}")


@dataclass(frozen=True, slots=True)
class StackRow:
    """One unmanaged project or namespace of the review."""

    index: int
    kind: str
    name: str
    offered: bool
    source: str | None
    needs: str | None
    services: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImageRow:
    """One unbound image reference of the review."""

    index: int
    reference: str
    outcome: str
    offered: bool
    ticked: bool
    ambiguous: bool
    unneeded: bool
    set_name: str | None
    choices: tuple[tuple[str, str], ...]
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObjectRow:
    """One uncontrolled object of the review."""

    index: int
    type: str
    name: str
    candidate: str | None


def _needs_text(target: StackTarget) -> str | None:
    needs = target.needs
    if needs is None:
        return None
    base = REASONS.get(needs.reason) or EXTRA_REASONS.get(needs.reason)
    text = base or f"The file could not be produced ({needs.reason})."
    return f"{text} ({needs.path})" if needs.path and target.report is not None else text


def stack_rows(review: OnboardingReview) -> tuple[StackRow, ...]:
    """Describe the stacks of the review in their review order."""
    rows: list[StackRow] = []
    for index, target in enumerate(review.stacks):
        result = target.result
        rows.append(
            StackRow(
                index=index,
                kind=target.kind,
                name=target.name,
                offered=target.offered,
                source=SOURCES[result.file_source] if result is not None else None,
                needs=_needs_text(target),
                services=target.report.services if target.report is not None else (),
            )
        )
    return tuple(rows)


def _image_row(index: int, target: ImageTarget) -> ImageRow:
    outcome = target.outcome
    names = {check.choice: check.name for check in outcome.matches}
    set_name: str | None = None
    text = OUTCOMES.get(outcome.outcome, "")
    if outcome.outcome == "binds":
        (set_name,) = names.values()
        text = f"Binds to {set_name}."
    choices = tuple((str(choice.credential_set_id), name or "") for choice, name in names.items())
    notes = tuple(
        f"{check.name or 'Anonymous'}: {CHECK_ERRORS.get(check.error or '', check.error or '')}"
        for check in outcome.tried
        if check.status == "failed"
    ) + tuple(
        f"{check.name or 'Anonymous'}: a different image"
        for check in outcome.tried
        if check.status == "mismatch"
    )
    return ImageRow(
        index=index,
        reference=target.reference,
        outcome=text,
        offered=target.offered,
        ticked=outcome.outcome == "binds",
        ambiguous=outcome.outcome == "ambiguous",
        unneeded=outcome.outcome == "anonymous_only",
        set_name=set_name,
        choices=choices,
        notes=notes,
    )


def image_rows(review: OnboardingReview) -> tuple[ImageRow, ...]:
    """Describe the images of the review in their review order."""
    return tuple(_image_row(index, target) for index, target in enumerate(review.images))


def object_rows(review: OnboardingReview) -> tuple[ObjectRow, ...]:
    """Describe the uncontrolled objects of the review in their review order."""
    return tuple(_object_row(index, target) for index, target in enumerate(review.objects))


def _object_row(index: int, target: ObjectTarget) -> ObjectRow:
    candidate = None
    if target.owner is not None:
        candidate = f"Owner {target.owner.username}, from the containers that mount it."
    return ObjectRow(index, target.type, target.name, candidate)


def _indexes(values: Sequence[Any], limit: int) -> frozenset[int]:
    found: set[int] = set()
    for value in values:
        if not isinstance(value, str) or _INDEX.fullmatch(value) is None or int(value) >= limit:
            raise ValidationFailed({"selection": "A ticked target is not part of this review."})
        found.add(int(value))
    return frozenset(found)


def _choice_of(text: str) -> CredentialChoice | None:
    value = text.strip()
    if not value:
        return None
    if value == ANONYMOUS:
        return CredentialChoice(None)
    if value.isascii() and value.isdigit() and len(value) <= ID_DIGITS:
        return CredentialChoice(int(value))
    raise ValidationFailed({"images": "Choose one of the listed credential sets."})


def owner_fields(form: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return the typed owner, team and team level of the form, cut to a sane length."""
    fields = []
    for name, default in (("owner", ""), ("team", ""), ("level", "write")):
        value = form.get(name, default)
        fields.append(value.strip()[:FIELD_MAX] if isinstance(value, str) else default)
    return fields[0], fields[1], fields[2]


def read_selection(form: FormData, review: OnboardingReview, ownership: Ownership) -> Selection:
    """Read the ticked indexes and chosen sets of the review form; ValidationFailed if invalid."""
    stacks = _indexes(form.getlist("stack"), len(review.stacks))
    images = _indexes(form.getlist("image"), len(review.images))
    objects = _indexes(form.getlist("object"), len(review.objects))
    chosen: dict[int, CredentialChoice | None] = {}
    for index in sorted(images):
        text = form.get(f"choice_{index}", "")
        chosen[index] = _choice_of(text) if isinstance(text, str) else None
    return Selection(stacks, chosen, objects, ownership)
