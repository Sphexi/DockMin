"""The normalised shape of one app template and the reference that names it."""

import re
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.repositories.pagination import MAX_ROW_ID

MAX_ENTRIES: Final = 2000
MAX_CONTENT_BYTES: Final = 4 * 1024 * 1024
CONTENT_FORMAT: Final = 1

TITLE_MAX: Final = 128
DESCRIPTION_MAX: Final = 2000
NOTE_MAX: Final = 2000
CATEGORY_MAX: Final = 32
CATEGORIES_MAX: Final = 8
ENV_DEFAULT_MAX: Final = 1024
ENV_OPTIONS_MAX: Final = 32
LABELS_MAX: Final = 64
PORTS_MAX: Final = 64
VOLUMES_MAX: Final = 64
ENV_MAX: Final = 128
SETTINGS_LIST_MAX: Final = 32
PATH_MAX: Final = 1024
URL_MAX: Final = 2048
COMMAND_MAX: Final = 2048

KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
ENV_NAME_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")
NAME_PATTERN: Final = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}")

type Protocol = Literal["tcp", "udp", "sctp"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class TemplatePort(_Model):
    """A container port, with the host port the template suggests."""

    container_port: int = Field(ge=1, le=65535)
    protocol: Protocol = "tcp"
    host_port: int | None = Field(default=None, ge=1, le=65535)
    description: str = Field(default="", max_length=DESCRIPTION_MAX)
    optional: bool = False


class TemplateVolume(_Model):
    """A path inside the container; `bind` is a host path, set by Portainer templates only."""

    container_path: str = Field(min_length=1, max_length=PATH_MAX)
    description: str = Field(default="", max_length=DESCRIPTION_MAX)
    optional: bool = False
    bind: str | None = Field(default=None, max_length=PATH_MAX)
    read_only: bool = False


class TemplateEnvOption(_Model):
    """One choice offered for an environment variable."""

    text: str = Field(max_length=TITLE_MAX)
    value: str = Field(max_length=ENV_DEFAULT_MAX)


class TemplateEnv(_Model):
    """An environment variable with its suggested value."""

    name: str = Field(min_length=1, max_length=128)
    default: str = Field(default="", max_length=ENV_DEFAULT_MAX)
    description: str = Field(default="", max_length=DESCRIPTION_MAX)
    optional: bool = False
    preset: bool = False
    options: tuple[TemplateEnvOption, ...] = Field(default=(), max_length=ENV_OPTIONS_MAX)


class TemplateLabel(_Model):
    """A container label the template sets."""

    name: str = Field(min_length=1, max_length=PATH_MAX)
    value: str = Field(default="", max_length=ENV_DEFAULT_MAX)


class AppTemplate(_Model):
    """One application a catalogue offers, in the one shape both catalogue kinds normalise to."""

    key: str = Field(pattern=rf"^{KEY_PATTERN.pattern}$")
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    description: str = Field(default="", max_length=DESCRIPTION_MAX)
    categories: tuple[str, ...] = Field(default=(), max_length=CATEGORIES_MAX)
    image: str = Field(min_length=1, max_length=512)
    name: str | None = Field(default=None, max_length=255)
    architectures: tuple[str, ...] = Field(default=(), max_length=SETTINGS_LIST_MAX)
    ports: tuple[TemplatePort, ...] = Field(default=(), max_length=PORTS_MAX)
    volumes: tuple[TemplateVolume, ...] = Field(default=(), max_length=VOLUMES_MAX)
    env: tuple[TemplateEnv, ...] = Field(default=(), max_length=ENV_MAX)
    devices: tuple[str, ...] = Field(default=(), max_length=SETTINGS_LIST_MAX)
    capabilities: tuple[str, ...] = Field(default=(), max_length=SETTINGS_LIST_MAX)
    sysctls: tuple[str, ...] = Field(default=(), max_length=SETTINGS_LIST_MAX)
    security_options: tuple[str, ...] = Field(default=(), max_length=SETTINGS_LIST_MAX)
    host_network: bool = False
    hostname: str | None = Field(default=None, max_length=253)
    shm_size: str | None = Field(default=None, max_length=32)
    privileged: bool = False
    interactive: bool = False
    restart_policy: str | None = Field(default=None, max_length=32)
    command: str | None = Field(default=None, max_length=COMMAND_MAX)
    labels: tuple[TemplateLabel, ...] = Field(default=(), max_length=LABELS_MAX)
    note: str = Field(default="", max_length=NOTE_MAX)
    setup_url: str | None = Field(default=None, max_length=URL_MAX)
    administrator_only: bool = False


class TemplateDocument(_Model):
    """The stored form of a catalogue's templates."""

    format: int = CONTENT_FORMAT
    templates: tuple[AppTemplate, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateRef:
    """A template named by its catalogue and key."""

    catalogue_id: int
    key: str

    @property
    def text(self) -> str:
        """Return the reference as `<catalogue id>:<key>`."""
        return f"{self.catalogue_id}:{self.key}"


def template_ref(catalogue_id: int, key: str) -> str:
    """Return the text reference of a template."""
    return TemplateRef(catalogue_id, key).text


def parse_template_ref(text: str) -> TemplateRef | None:
    """Parse `<catalogue id>:<key>`; anything else is None."""
    identifier, separator, key = text.partition(":")
    if (
        not separator
        or not identifier.isascii()
        or not identifier.isdigit()
        or len(identifier) > len(str(MAX_ROW_ID))
        or not 1 <= int(identifier) <= MAX_ROW_ID
        or KEY_PATTERN.fullmatch(key) is None
    ):
        return None
    return TemplateRef(int(identifier), key)
