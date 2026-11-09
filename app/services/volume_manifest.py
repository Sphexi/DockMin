"""The manifest of a volume export archive, and what it may record of a volume."""

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.logging import DEFAULT_SCRUBBER

FORMAT_VERSION: Final = 1
KIND: Final = "volume"
MANIFEST_NAME: Final = "manifest.json"
SKIPPED_NAME: Final = "skipped.json"
DATA_DIRECTORY: Final = "data"

_EXCLUDED_LABEL_PREFIXES: Final = ("com.docker.compose.", "dockmin.")
_EXCLUDED_LABELS: Final = frozenset({"com.docker.stack.namespace"})
_CREDENTIAL_NAMES: Final = frozenset(
    {
        "credential",
        "credentials",
        "cred",
        "creds",
        "password",
        "passwd",
        "pass",
        "pwd",
        "passphrase",
        "secret",
        "secretfile",
        "token",
        "key",
        "apikey",
    }
)
_USER_NAMES: Final = frozenset({"user", "username"})
_USERINFO: Final = re.compile(r"(?:^|[/\s,;=])[^\s:/@,;=]{1,128}:\S*@")
_ENTRY_SEPARATORS: Final = re.compile(r"[,;\s]+")


class VolumeManifest(BaseModel):
    """What an archive records of the volume it holds; `data_path` is the directory of its files."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: int = Field(ge=1)
    kind: Literal["volume"]
    exported_at: datetime
    instance_version: str
    name: str
    driver: str
    labels: dict[str, str]
    options: dict[str, str]
    removed_option_keys: list[str]
    data_path: str


def manifest_bytes(manifest: VolumeManifest) -> bytes:
    """Encode the manifest as JSON with sorted keys."""
    document = manifest.model_dump(mode="json")
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")


def build_manifest(
    *,
    exported_at: datetime,
    instance_version: str,
    name: str,
    driver: str,
    labels: Mapping[str, str],
    options: Mapping[str, str],
) -> VolumeManifest:
    """Build a volume's manifest without credential options or access and product labels."""
    kept, removed = filter_options(options)
    return VolumeManifest(
        format_version=FORMAT_VERSION,
        kind=KIND,
        exported_at=exported_at,
        instance_version=instance_version,
        name=name,
        driver=driver,
        labels=filter_labels(labels),
        options=kept,
        removed_option_keys=removed,
        data_path=DATA_DIRECTORY,
    )


def filter_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """Return the labels without those that grant access or belong to the product."""
    return {
        key: value
        for key, value in sorted(labels.items())
        if key.casefold() not in _EXCLUDED_LABELS
        and not key.casefold().startswith(_EXCLUDED_LABEL_PREFIXES)
    }


def filter_options(options: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Return the options without credentials, and the keys that were removed.

    An option is dropped whole when its key names a credential or its value carries one: an
    entry that names a credential, the password of a `user%password` pair, or the password of
    `user:password@host`. Each dropped option is listed by its key; no value is ever listed.
    """
    kept: dict[str, str] = {}
    removed: list[str] = []
    for key, value in sorted(options.items()):
        if _is_credential(key) or _carries_credential(value):
            removed.append(key)
        else:
            kept[key] = value
    return kept, removed


def _is_credential(name: str) -> bool:
    return name.strip().casefold() in _CREDENTIAL_NAMES or DEFAULT_SCRUBBER.is_sensitive(name)


def _carries_credential(value: str) -> bool:
    for entry in _ENTRY_SEPARATORS.split(value):
        name, separator, text = entry.partition("=")
        if not separator:
            continue
        if _is_credential(name) or (name.casefold() in _USER_NAMES and "%" in text):
            return True
    return _USERINFO.search(value) is not None
