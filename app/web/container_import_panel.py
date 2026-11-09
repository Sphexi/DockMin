"""What the create form says about the container archive it was filled from."""

import re
from dataclasses import dataclass
from typing import Final

from app.services.container_import_store import PreparedContainer

IMPORT_PARAM: Final = "imported"
IMPORT_FIELD: Final = "imported_ref"
_REF: Final = re.compile(r"[0-9a-f]{32}")
_REASONS: Final = {
    "labels_dropped": "Not exported, stack labels are not carried: ",
    "restricted": "Not exported, only administrators may set them: ",
    "withheld": "Not exported, withheld from the exporting user: ",
    "not_copied": "Not exported, the create form cannot set them: ",
    "dropped_invalid": "Not exported, the values were not valid in the form: ",
    "anonymous_volumes": "Anonymous volumes are created new and empty at: ",
    "converted": "Converted to the form's own terms: ",
}


@dataclass(frozen=True, slots=True)
class ImportPanel:
    """The facts of an import shown above the create form; `gone` when the state is not held."""

    gone: bool
    source_name: str = ""
    exported: str = ""
    networks: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    variables: int = 0
    secrets: int = 0
    notices: tuple[str, ...] = ()


def valid_ref(text: str | None) -> str | None:
    """Return the text when it has the form of an import id, else None."""
    return text if text is not None and _REF.fullmatch(text) is not None else None


def panel_of(held: PreparedContainer | None) -> ImportPanel:
    """Describe the held state, or say it is gone."""
    if held is None:
        return ImportPanel(gone=True)
    return ImportPanel(
        gone=False,
        source_name=held.source_name,
        exported=held.exported,
        networks=held.networks_created,
        volumes=held.volumes_created,
        variables=held.environment_variables,
        secrets=held.secret_values,
        notices=tuple(
            f"{_REASONS.get(reason, 'Not exported: ')}{', '.join(items)}"
            for reason, items in held.not_exported
            if items
        ),
    )
