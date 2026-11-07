"""Pieces shared by the image routers and the container create pages."""

import re
from typing import Annotated, Final

from fastapi import Depends, Path

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.services.image_bindings import CredentialOffer
from app.services.image_jobs import JOB_ID_PATTERN
from app.services.pull_credentials import CHOICE_FIELD, CredentialChoice

PREFIX: Final = "/environments/{environment_id}/images"
SEGMENT_MAX: Final = 128
ID_DIGITS: Final = 18
ANONYMOUS: Final = "anonymous"
JOB_ID: Final = re.compile(JOB_ID_PATTERN)


def _canonical_job_id(job_id: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    """Refuse a job id of the wrong shape as a missing one."""
    if JOB_ID.fullmatch(job_id) is None:
        raise ResourceNotFound("not found")
    return job_id


JobId = Annotated[str, Depends(_canonical_job_id)]


def credential_choice_of(text: str) -> CredentialChoice | None:
    """Read the submitted credential choice: nothing, `anonymous`, or a set id."""
    value = text.strip()
    if not value:
        return None
    if value == ANONYMOUS:
        return CredentialChoice(None)
    if value.isascii() and value.isdigit() and len(value) <= ID_DIGITS:
        return CredentialChoice(int(value))
    raise ValidationFailed({CHOICE_FIELD: "Choose one of the listed credential sets."})


def selected_choice(offer: CredentialOffer, submitted: str = "") -> str:
    """Return the radio value to check: the submitted one if offered, else the preselection."""
    value = submitted.strip()
    if value == ANONYMOUS or value in {str(candidate.id) for candidate in offer.candidates}:
        return value
    preselected = offer.preselected
    if preselected is None:
        return ""
    return (
        ANONYMOUS if preselected.credential_set_id is None else str(preselected.credential_set_id)
    )
