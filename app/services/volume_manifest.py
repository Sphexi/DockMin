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
_USERINFO_PASSWORD: Final = re.compile(r"(?<![\w.:+-])(?P<user>[^\s:/@,;]{1,128}):[^\s@,;/]+(?=@)")
_PASSWORD_REMOVED: Final = "password"


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
    """Build the manifest of a volume, keeping its labels and options as far as Q4 allows."""
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

    An option whose own key names a credential is dropped whole. In every other value, a
    comma-separated entry that names a credential is dropped, the password of a `user%password`
    pair and the password of `user:password@host` are cut, and each is listed as `key.name`.
    No value is ever listed.
    """
    kept: dict[str, str] = {}
    removed: list[str] = []
    for key, value in sorted(options.items()):
        if _is_credential(key):
            removed.append(key)
            continue
        cleaned, cut = _clean_value(value)
        kept[key] = cleaned
        removed.extend(f"{key}.{name}" for name in cut)
    return kept, removed


def _is_credential(name: str) -> bool:
    return name.strip().casefold() in _CREDENTIAL_NAMES or DEFAULT_SCRUBBER.is_sensitive(name)


def _clean_value(value: str) -> tuple[str, list[str]]:
    cut: list[str] = []
    entries: list[str] = []
    for entry in value.split(","):
        name, separator, text = entry.partition("=")
        if separator and _is_credential(name):
            cut.append(name.strip())
            continue
        if separator and name.strip().casefold() in {"user", "username"} and "%" in text:
            entry = f"{name}={text.partition('%')[0]}"
            cut.append(_PASSWORD_REMOVED)
        entries.append(entry)
    cleaned = ",".join(entries)
    without_userinfo = _USERINFO_PASSWORD.sub(lambda match: match["user"], cleaned)
    if without_userinfo != cleaned:
        cut.append(_PASSWORD_REMOVED)
    return without_userinfo, cut
