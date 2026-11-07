"""What the read-from-host result page shows: the source, the files, the refusals and the text."""

from dataclasses import dataclass
from typing import Annotated, Final, Literal

from fastapi import Depends

from app.services.assembly import get_engine_runtime
from app.services.engine_setup import EngineRuntime
from app.services.host_read_stash import TOKEN, HostReadStash
from app.services.stack_document import ComposeDocumentRejected, decode_file
from app.services.stack_host_read import HostReadReport
from app.services.stack_read_result import HostReadResult, NeedsFile, ReadFile, mask_secret_values
from app.web.stack_pages import FileRow, FormState, VariableRow, padded, padded_files

__all__ = [
    "TOKEN",
    "HostReads",
    "ReadFileView",
    "ReadPage",
    "ReadVariableView",
    "prefilled_state",
    "read_page",
]

type Kind = Literal["compose", "swarm"]

SOURCES: Final = {
    "read": "Read from the project's files on the host",
    "merged": "Read from the project's files on the host and merged into one file",
    "generated": "Generated from the project's running containers",
    "reconstructed": "Reconstructed from the cluster's service specifications",
}
SWARM_REDEPLOY: Final = (
    "The first redeploy creates each config under a name taken from its content and re-points "
    "the services to it, and replaces the running tasks of every service. Secrets are kept by "
    "name, and their values stay in the cluster. The redeploy confirmation names each change."
)
COMPOSE_REDEPLOY: Final = (
    "The first redeploy recreates the containers whose configuration differs from this file."
)
SECRET_NOTE: Final = "Secret. The value is stored encrypted and is never shown."

REASONS: Final = {
    "labels_disagree": "The project's containers carry different file locations.",
    "labels_missing": "The project's containers do not record where its files are.",
    "working_directory_not_absolute": "The recorded working directory is not an absolute path.",
    "working_directory_root": "The recorded working directory is the root of the host.",
    "working_directory_not_normalised": "The recorded working directory is not a plain path.",
    "working_directory_control_characters": "The working directory holds a control character.",
    "working_directory_bind_separator": "The recorded working directory holds a colon.",
    "working_directory_too_long": "The recorded working directory is longer than 1,024 bytes.",
    "own_mount": "The working directory holds or lies inside a mount of dockmin's own.",
    "config_file_outside": "A Compose file is not inside the working directory.",
    "too_many_config_files": "The project has more than eight Compose files.",
    "environment_file_outside": "An environment file is not inside the working directory.",
    "environment_file_unsupported": "A line of the environment file is not supported.",
    "helper_image_missing": "No image of the project's containers is on the host to read with.",
    "missing": "The file is not on the host.",
    "symlink": "The path is a symbolic link.",
    "not_regular_file": "The path is not a regular file.",
    "too_large": "The file or its set is over the size limit.",
    "malformed": "The host's reply was not a single file.",
    "member_count": "The host's reply held more than one file.",
    "member_name": "The host's reply named another file.",
    "invalid_path": "The path is not a plain path below the working directory.",
    "working_directory_unmountable": "The host would not mount the working directory.",
    "reference_outside": "A referenced file is outside the working directory.",
    "reference_has_variable": "A referenced path holds a variable.",
    "reference_aliased": "A referenced path is shared through a YAML alias.",
    "reference_not_plain": "A referenced path is not a plain or quoted single-line value.",
    "file_name_unusable": "A referenced file's path cannot be used as a file name.",
    "file_name_collision": "Two referenced files would take the same name.",
    "file_claimed_twice": "A file is named both as a Compose file and as another kind of file.",
    "identity_unverified": "dockmin's own mounts on this host could not be established.",
    "too_many_files": "The project references more files than a stack can hold.",
    "too_deep": "Compose files include each other too deeply.",
    "merge_unsupported": "Several Compose files with an include or extends cannot be merged.",
    "merge_failed": "Docker Compose could not merge the files.",
    "refused_construct": "The file uses a construct a stack cannot hold.",
    "invalid_compose": "The file is not valid Compose YAML.",
    "invalid": "The file or a value in it is not acceptable.",
    "file_missing": "A referenced file is not among the files read.",
    "no_containers": "The project has no containers to write a file from.",
    "invalid_service_key": "A container's service name cannot be a Compose key.",
    "too_many_services": "The project has more services than can be read.",
    "inspect_mismatch": "The host answered for another object.",
    "secret_value_unusable": "A secret value cannot be kept as a variable.",
    "no_translator_row": "A setting of a service has no Compose equivalent.",
    "no_services": "The namespace has no services.",
    "network_unresolved": "A network a service uses cannot be found.",
    "config_unreadable": "A config's content could not be read.",
    "name_without_namespace": "A service name does not start with the stack name.",
}


@dataclass(frozen=True, slots=True)
class ReadFileView:
    """One companion file of the result."""

    name: str
    secret: bool
    size: int


@dataclass(frozen=True, slots=True)
class ReadVariableView:
    """One variable of the result; a secret carries no value."""

    name: str
    secret: bool
    value: str | None


@dataclass(frozen=True, slots=True)
class ReadPage:
    """The result page's content."""

    kind: Kind
    name: str
    source: str | None
    needed: str | None
    attempted: str | None
    services: tuple[str, ...]
    files: tuple[ReadFileView, ...]
    variables: tuple[ReadVariableView, ...]
    losses: tuple[str, ...]
    read_paths: tuple[str, ...]
    refusals: tuple[tuple[str, str], ...]
    text: str | None
    redeploy: str
    token: str | None


def _stash(engine: Annotated[EngineRuntime, Depends(get_engine_runtime)]) -> HostReadStash:
    return engine.host_reads


HostReads = Annotated[HostReadStash, Depends(_stash)]


def prefilled_state(name: str, held: HostReadResult) -> FormState:
    """Return the paste form's state holding a read result; a secret carries no value."""
    variables = [
        VariableRow(
            index, item.name, None if item.secret else item.value, item.secret, False, False
        )
        for index, item in enumerate(held.variables)
    ]
    files = [
        FileRow(index, item.name, _plain_text(item), item.secret)
        for index, item in enumerate(held.files)
    ]
    return FormState(
        name=name,
        compose_text=held.compose_text,
        rows=padded(variables),
        file_rows=padded_files(files),
    )


def _plain_text(item: ReadFile) -> str | None:
    if item.secret:
        return None
    try:
        return decode_file(item.content, field="content")
    except ComposeDocumentRejected:
        return None


def _sentence(reason: str, path: str) -> str:
    base = REASONS.get(reason, f"The file could not be produced ({reason}).")
    return f"{base} ({path})" if path else base


def read_page(kind: Kind, report: HostReadReport, token: str | None) -> ReadPage:
    """Describe a read for display; no secret value and no refused file's content is carried."""
    result = report.result
    outcome = report.outcome
    needed = _sentence(outcome.reason, outcome.path) if isinstance(outcome, NeedsFile) else None
    attempted = (
        _sentence(report.attempted.reason, report.attempted.path) if report.attempted else None
    )
    return ReadPage(
        kind=kind,
        name=report.name,
        source=SOURCES[result.file_source] if result is not None else None,
        needed=needed,
        attempted=attempted,
        services=report.services,
        files=tuple(
            ReadFileView(item.name, item.secret, len(item.content))
            for item in (result.files if result else ())
        ),
        variables=tuple(
            ReadVariableView(item.name, item.secret, None if item.secret else item.value)
            for item in (result.variables if result else ())
        ),
        losses=result.losses if result else (),
        read_paths=report.files_read,
        refusals=tuple((path, REASONS.get(reason, reason)) for path, reason in report.refused),
        text=mask_secret_values(result.compose_text) if result else None,
        redeploy=SWARM_REDEPLOY if kind == "swarm" else COMPOSE_REDEPLOY,
        token=token,
    )
