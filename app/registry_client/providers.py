"""Registry providers: the extras each takes, the rules that hold for each, and the key denylist."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

from app.registry_client.addresses import DOCKER_HUB_ADDRESS, canonical_address
from app.registry_client.errors import AddressInvalid, ExtrasInvalid

ProviderKind = Literal["dockerhub", "ghcr", "acr", "gitlab", "ecr", "custom"]
SecretKind = Literal["static", "derived"]

GHCR_ADDRESS: Final = "ghcr.io"
GITLAB_DEFAULT_ADDRESS: Final = "registry.gitlab.com"
GITLAB_DEFAULT_INSTANCE: Final = "https://gitlab.com"
MAX_REGION_LENGTH: Final = 32
MAX_INSTANCE_URL_LENGTH: Final = 256
MAX_PROJECT_PATH_LENGTH: Final = 512
MAX_ID: Final = 2**53 - 1

REGION_PATTERN: Final = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
_ECR_ADDRESS = re.compile(r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(\.cn)?$")
_GITHUB_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_PROJECT_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")
_INSTANCE_PATH = re.compile(r"^(?:/[A-Za-z0-9._~-]+)*$")

DENIED_KEY_FRAGMENTS: Final = ("secret", "password", "token", "credential", "private")


class _Refusal(ValueError):
    """A validation failure whose text is written here and is safe to show."""


def _frozen() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid", strict=True)


class DockerHubExtras(BaseModel):
    """Docker Hub takes no extras."""

    model_config = _frozen()


class AcrExtras(BaseModel):
    """Azure Container Registry takes no extras."""

    model_config = _frozen()


class CustomExtras(BaseModel):
    """A custom registry takes no extras."""

    model_config = _frozen()


class GhcrExtras(BaseModel):
    """GitHub Container Registry: an optional organisation to act under."""

    model_config = _frozen()

    organisation: str | None = None
    use_organisation: bool = False

    @field_validator("organisation")
    @classmethod
    def _organisation_is_a_github_name(cls, value: str | None) -> str | None:
        if value is not None and not _GITHUB_NAME.match(value):
            raise _Refusal("is not a valid GitHub organisation or user name")
        return value

    @field_validator("use_organisation")
    @classmethod
    def _use_needs_organisation(cls, value: bool, info: ValidationInfo) -> bool:
        if value and info.data.get("organisation") is None:
            raise _Refusal("needs an organisation name")
        return value


class GitLabExtras(BaseModel):
    """GitLab: the instance that issues tokens, and optionally the project the set is for."""

    model_config = _frozen()

    instance_url: str = GITLAB_DEFAULT_INSTANCE
    project_id: int | None = Field(default=None, gt=0, le=MAX_ID)
    project_path: str | None = None

    @field_validator("instance_url")
    @classmethod
    def _instance_is_an_https_origin(cls, value: str) -> str:
        text = value.removesuffix("/")
        if not text.isascii() or len(text) > MAX_INSTANCE_URL_LENGTH:
            raise _Refusal("is not a valid https address")
        try:
            parts = urlsplit(text)
        except ValueError:
            raise _Refusal("is not a valid https address") from None
        problem = (
            parts.scheme != "https"
            or not parts.netloc
            or parts.query
            or parts.fragment
            or "@" in parts.netloc
            or not _INSTANCE_PATH.match(parts.path)
        )
        if problem:
            raise _Refusal("must be an https address without credentials, query or fragment")
        try:
            canonical = canonical_address(parts.netloc)
        except AddressInvalid:
            raise _Refusal("does not have a valid host") from None
        return f"https://{canonical}{parts.path}"

    @field_validator("project_path")
    @classmethod
    def _path_has_group_and_project(cls, value: str | None) -> str | None:
        if value is None:
            return None
        segments = value.split("/")
        if (
            len(value) > MAX_PROJECT_PATH_LENGTH
            or len(segments) < 2
            or not all(_PROJECT_SEGMENT.match(segment) for segment in segments)
        ):
            raise _Refusal("must look like group/project")
        return value


class EcrExtras(BaseModel):
    """Amazon ECR: the region the registry and its token service are in."""

    model_config = _frozen()

    region: str

    @field_validator("region")
    @classmethod
    def _region_has_the_aws_shape(cls, value: str) -> str:
        if len(value) > MAX_REGION_LENGTH or not REGION_PATTERN.match(value):
            raise _Refusal("is not an AWS region such as eu-west-2")
        return value


type ProviderExtras = (
    DockerHubExtras | GhcrExtras | AcrExtras | GitLabExtras | EcrExtras | CustomExtras
)


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """What is fixed about a provider."""

    kind: ProviderKind
    secret_kind: SecretKind
    fixed_address: str | None
    anonymous_allowed: bool
    tls_options_allowed: bool
    extras_model: type[BaseModel]
    default_address: str | None = None
    destination_extras: tuple[str, ...] = ()


PROVIDERS: Final[Mapping[ProviderKind, ProviderSpec]] = {
    "dockerhub": ProviderSpec(
        "dockerhub", "static", DOCKER_HUB_ADDRESS, True, False, DockerHubExtras
    ),
    "ghcr": ProviderSpec("ghcr", "static", GHCR_ADDRESS, True, False, GhcrExtras),
    "acr": ProviderSpec("acr", "static", None, True, False, AcrExtras),
    "gitlab": ProviderSpec(
        "gitlab",
        "static",
        None,
        True,
        True,
        GitLabExtras,
        GITLAB_DEFAULT_ADDRESS,
        destination_extras=("instance_url",),
    ),
    "ecr": ProviderSpec(
        "ecr", "derived", None, False, False, EcrExtras, destination_extras=("region",)
    ),
    "custom": ProviderSpec("custom", "static", None, True, True, CustomExtras),
}


def forbidden_extras_keys(data: Mapping[str, object]) -> tuple[str, ...]:
    """Return every key, at any depth, whose name suggests it holds a secret."""
    found: list[str] = []
    _collect_denied(data, found)
    return tuple(found)


def _collect_denied(value: object, found: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if any(fragment in name.lower() for fragment in DENIED_KEY_FRAGMENTS):
                found.append(name)
            _collect_denied(item, found)
    elif isinstance(value, list | tuple):
        for item in value:
            _collect_denied(item, found)


def parse_extras(kind: ProviderKind, data: Mapping[str, object]) -> ProviderExtras:
    """Validate extras against the provider's model, or raise ExtrasInvalid.

    A key naming a secret is refused before the model sees it. No message quotes a value.
    """
    spec = PROVIDERS[kind]
    if forbidden_extras_keys(data):
        raise ExtrasInvalid({"extras": "may not hold secrets"})
    model: BaseModel | None = None
    problems: dict[str, str] = {}
    try:
        model = spec.extras_model.model_validate(dict(data))
    except ValidationError as exc:
        problems = _field_errors(exc)
    if model is None:
        raise ExtrasInvalid(problems)
    return cast(ProviderExtras, model)


def _field_errors(exc: ValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for item in exc.errors(include_input=False, include_url=False):
        location = item["loc"]
        name = str(location[0]) if location else "extras"
        kind = item["type"]
        if kind == "extra_forbidden":
            errors["extras"] = "contains a setting this provider does not take"
        elif kind == "missing":
            errors[name] = "is required"
        elif kind == "value_error":
            errors[name] = _value_error_text(item)
        else:
            errors.setdefault(name, "is not valid")
    return errors


def _value_error_text(item: Mapping[str, Any]) -> str:
    context = item.get("ctx") or {}
    error = context.get("error")
    return str(error) if isinstance(error, _Refusal) else "is not valid"


def check_address_for_provider(kind: ProviderKind, address: str, extras: ProviderExtras) -> None:
    """Raise AddressInvalid unless the canonical address is one this provider can have."""
    spec = PROVIDERS[kind]
    if spec.fixed_address is not None:
        if address != spec.fixed_address:
            raise AddressInvalid(f"The address for this provider is always {spec.fixed_address}.")
        return
    if kind != "ecr":
        return
    if not isinstance(extras, EcrExtras):
        raise TypeError("ECR needs ECR extras")
    match = _ECR_ADDRESS.match(address)
    if match is None:
        raise AddressInvalid(
            "An ECR address looks like 123456789012.dkr.ecr.eu-west-2.amazonaws.com."
        )
    in_china = match.group(3) is not None
    if match.group(2) != extras.region or in_china != extras.region.startswith("cn-"):
        raise AddressInvalid("The region in the ECR address must match the region setting.")
