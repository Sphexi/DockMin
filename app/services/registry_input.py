"""Validation of the credential set form: what is kept, and what must be entered again."""

import re
import ssl
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, cast, get_args

from cryptography import x509
from pydantic import SecretStr

from app.auth.errors import ValidationFailed
from app.db.repositories.registry_credential import CredentialFields, StoredRegistryCredential
from app.docker_engine.tls_validation import (
    MAX_CA_CERTIFICATES,
    MAX_PEM_BYTES,
    TlsMetadata,
    describe_certificate,
    normalise_pem,
)
from app.netcheck import HIDDEN_CATEGORIES
from app.netpolicy import literal_host_refusal
from app.registry_client.addresses import canonical_address, split_address
from app.registry_client.errors import AddressInvalid, ExtrasInvalid
from app.registry_client.providers import (
    MAX_ID,
    PROVIDERS,
    ProviderExtras,
    ProviderKind,
    SecretKind,
    check_address_for_provider,
    parse_extras,
)
from app.registry_client.tls import registry_context

NAME_MAX: Final = 64
USERNAME_MAX: Final = 256
SECRET_MAX: Final = 16384
MAX_ID_DIGITS: Final = len(str(MAX_ID))

PROVIDER_ORDER: Final[tuple[ProviderKind, ...]] = get_args(ProviderKind)

CaCertificateSummary = TlsMetadata

_FIELD_LABELS: Final = {
    "organisation": "The organisation",
    "use_organisation": "Use organisation",
    "instance_url": "The instance URL",
    "project_id": "The project ID",
    "project_path": "The project path",
    "region": "The region",
}
_SECRET_WHITESPACE = "\t\r\n"
_INT_FIELDS = frozenset({"project_id"})
_BOOL_FIELDS = frozenset({"use_organisation"})
_TRUTHY = frozenset({"1", "true", "on", "yes"})
_ACCESS_KEY_ID = re.compile(r"^AKIA[A-Z0-9]{16}$")
_CERTIFICATE_BLOCK = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\n", re.DOTALL
)
_PEM_LABEL = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----")

NAME_EXISTS: Final = "A registry credential set with this name already exists."
CHANGED_SINCE_REVIEW: Final = "Changed since it was reviewed; nothing was deleted."
_REENTER_SECRET: Final = (
    "Enter the secret again. Changing the provider, address, region, GitLab instance URL, "
    "certificate bundle or certificate verification requires it."
)
_SECRET_REQUIRED: Final = "Enter the secret."
_EXTRAS_REFUSED: Final = "These settings are not valid for this provider."
_UNSET: Final = object()
_INVALID: Final = object()


@dataclass(frozen=True, slots=True)
class ProviderShape:
    """What a form needs to know about a provider."""

    kind: ProviderKind
    secret_kind: SecretKind
    fixed_address: str | None
    default_address: str | None
    address_editable: bool
    anonymous_allowed: bool
    tls_options_allowed: bool
    extras_fields: tuple[str, ...]


def provider_shape(kind: ProviderKind) -> ProviderShape:
    """Return the form-facing facts about a provider."""
    spec = PROVIDERS[kind]
    return ProviderShape(
        kind=kind,
        secret_kind=spec.secret_kind,
        fixed_address=spec.fixed_address,
        default_address=spec.default_address,
        address_editable=spec.fixed_address is None,
        anonymous_allowed=spec.anonymous_allowed,
        tls_options_allowed=spec.tls_options_allowed,
        extras_fields=tuple(spec.extras_model.model_fields),
    )


def extras_defaults(kind: ProviderKind) -> dict[str, str | bool]:
    """Return a provider's extras as the strings and flags a blank form shows."""
    fields = PROVIDERS[kind].extras_model.model_fields
    return _as_form(
        {
            name: None if info.is_required() else info.get_default(call_default_factory=True)
            for name, info in fields.items()
        }
    )


def extras_for_form(extras: ProviderExtras) -> dict[str, str | bool]:
    """Return stored extras as the strings and flags a form shows."""
    return _as_form(extras.model_dump())


def _as_form(values: Mapping[str, object]) -> dict[str, str | bool]:
    return {
        name: value if isinstance(value, bool) else "" if value is None else str(value)
        for name, value in values.items()
    }


@dataclass(frozen=True, slots=True)
class RegistrySpec:
    """A create or update request as the form submits it.

    A blank secret on an update keeps the stored one, unless the provider, address, region,
    certificate bundle or certificate verification changes, or any extra that names a destination
    (the ECR region, the GitLab instance URL). `extras` holds the provider's own
    fields by name; empty strings mean unset. `ca_bundle_problem` is a fault already found in the
    supplied bundle; it is reported with the other problems.
    """

    name: str
    provider: str
    address: str = ""
    authentication_enabled: bool = False
    username: str = ""
    secret: SecretStr = field(default=SecretStr(""), repr=False)
    verify_tls: bool = True
    ca_bundle: str = field(default="", repr=False)
    ca_bundle_problem: str = ""
    remove_ca_bundle: bool = False
    extras: Mapping[str, str | bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CheckedBundle:
    """A CA bundle that passed validation, re-emitted as clean PEM, with what it holds."""

    pem: str = field(repr=False)
    certificates: tuple[CaCertificateSummary, ...]


@dataclass(frozen=True, slots=True)
class PlannedRegistry:
    """A validated request, ready to write.

    `secret` is the new plaintext to seal; with `keep_secret` the stored one stays, and with
    neither the secret is cleared because authentication is off.
    """

    fields: CredentialFields
    secret: SecretStr | None = field(repr=False)
    keep_secret: bool
    secret_replaced: bool
    changed: tuple[str, ...]


def check_ca_bundle(text: str, now: datetime) -> CheckedBundle:
    """Validate pasted CA certificates; raise ValidationFailed on the ca_bundle field.

    Blocking: it loads the system roots to prove the bundle is usable, so async callers run it
    in a thread. A bundle need not be marked as a certificate authority; a self-signed server
    certificate is a valid anchor. No message quotes the PEM.
    """
    problem = _bundle_problem(text, now)
    if isinstance(problem, str):
        raise ValidationFailed({"ca_bundle": problem})
    return problem


def summarise_ca_bundle(pem: str, now: datetime) -> tuple[CaCertificateSummary, ...]:
    """Describe each certificate of a stored bundle."""
    return tuple(
        summary
        for block in _CERTIFICATE_BLOCK.findall(pem)
        if (summary := describe_certificate(block, now)) is not None
    )


def _bundle_problem(text: str, now: datetime) -> CheckedBundle | str:
    if len(text) > MAX_PEM_BYTES or len(text.encode("utf-8")) > MAX_PEM_BYTES:
        return f"The certificate bundle is larger than {MAX_PEM_BYTES // 1024} KiB."
    labels = _PEM_LABEL.findall(text)
    if not labels:
        return "The certificate bundle is not in PEM format."
    if any(label.endswith("PRIVATE KEY") for label in labels):
        return "The certificate bundle contains a private key. Provide only certificates."
    if any(label != "CERTIFICATE" for label in labels):
        return "The certificate bundle must contain only CERTIFICATE blocks."
    pem = normalise_pem(text)
    try:
        certificates = x509.load_pem_x509_certificates(pem.encode("utf-8"))
    except ValueError:
        certificates = []
    if not certificates or len(certificates) != len(labels):
        return "The certificate bundle could not be read as PEM certificates."
    if len(certificates) > MAX_CA_CERTIFICATES:
        return f"The certificate bundle contains more than {MAX_CA_CERTIFICATES} certificates."
    if len(pem) > MAX_PEM_BYTES:
        return f"The certificate bundle is larger than {MAX_PEM_BYTES // 1024} KiB."
    for index, certificate in enumerate(certificates, start=1):
        window = _window_problem(certificate, now)
        if window is not None:
            return f"Certificate {index} of the bundle {window}."
    try:
        registry_context(verify_tls=True, ca_bundle=pem)
    except ssl.SSLError:
        return "The connection layer could not load this certificate bundle."
    return CheckedBundle(pem, summarise_ca_bundle(pem, now))


def _window_problem(certificate: x509.Certificate, now: datetime) -> str | None:
    try:
        starts, ends = certificate.not_valid_before_utc, certificate.not_valid_after_utc
    except ValueError:
        return "has an unreadable validity period"
    if starts > now:
        return f"is not valid until {starts:%Y-%m-%d}"
    if ends < now:
        return f"expired on {ends:%Y-%m-%d}"
    return None


def plan_registry(
    spec: RegistrySpec, current: StoredRegistryCredential | None, bundle: CheckedBundle | None
) -> PlannedRegistry:
    """Validate a request against the set it edits (None to create) and decide what is written.

    Raises ValidationFailed with every problem found, keyed by form field. `bundle` is the
    checked form of spec.ca_bundle, or None when none was supplied.
    """
    problems: dict[str, str] = {}
    name = _name(spec.name, problems)
    kind = _provider(spec.provider, problems)
    if kind is None:
        raise ValidationFailed(problems)
    shape = PROVIDERS[kind]
    extras = _extras(kind, spec.extras, problems)
    address = _address(kind, spec.address, extras, problems)

    authentication = spec.authentication_enabled
    if not authentication and not shape.anonymous_allowed:
        problems["authentication_enabled"] = "This provider always needs credentials."
    username = _username(kind, spec.username, problems) if authentication else None

    verify_tls = spec.verify_tls if shape.tls_options_allowed else True
    if not verify_tls and authentication:
        problems["verify_tls"] = (
            "Certificate verification can be turned off only for a registry without "
            "authentication. Supply the registry's certificate as a bundle instead."
        )
    ca_bundle = _ca_bundle(spec, current, bundle, shape.tls_options_allowed, verify_tls, problems)

    new_secret: SecretStr | None = None
    keep_secret = False
    if authentication and spec.secret.get_secret_value().strip():
        new_secret = _secret(spec.secret, problems)
    elif authentication:
        keep_secret = _may_keep(current, kind, address, extras, ca_bundle, verify_tls, problems)

    if problems or name is None or extras is None or address is None:
        raise ValidationFailed(problems)

    fields = CredentialFields(
        name=name,
        provider=kind,
        url=address,
        authentication_enabled=authentication,
        username=username,
        extras=extras.model_dump(),
        verify_tls=verify_tls,
        ca_bundle=ca_bundle,
    )
    return PlannedRegistry(
        fields=fields,
        secret=new_secret,
        keep_secret=keep_secret,
        secret_replaced=new_secret is not None,
        changed=() if current is None else _changed(current, fields, extras),
    )


def _name(text: str, problems: dict[str, str]) -> str | None:
    name = text.strip()
    if not name:
        problems["name"] = "Enter a name."
    elif len(name) > NAME_MAX:
        problems["name"] = f"The name is at most {NAME_MAX} characters."
    elif any(unicodedata.category(c) in HIDDEN_CATEGORIES for c in name):
        problems["name"] = "The name may not contain control characters."
    else:
        return name
    return None


def _provider(text: str, problems: dict[str, str]) -> ProviderKind | None:
    if text in PROVIDERS:
        return cast(ProviderKind, text)
    problems["provider"] = "Choose a registry provider."
    return None


def _extras(
    kind: ProviderKind, raw: Mapping[str, str | bool], problems: dict[str, str]
) -> ProviderExtras | None:
    data: dict[str, object] = {}
    unreadable: dict[str, str] = {}
    for key, value in raw.items():
        coerced = _coerce_extra(key, value)
        if coerced is _INVALID:
            unreadable[key] = "The value is not valid."
        elif coerced is not _UNSET:
            data[key] = coerced
    if unreadable:
        problems.update(unreadable)
        return None
    try:
        return parse_extras(kind, data)
    except ExtrasInvalid as error:
        for name, message in error.errors.items():
            problems[name] = _EXTRAS_REFUSED if name == "extras" else _extras_text(name, message)
    return None


def _coerce_extra(key: str, value: str | bool) -> object:
    if isinstance(value, bool):
        return value
    text = value.strip()
    if key in _BOOL_FIELDS:
        return text.lower() in _TRUTHY
    if not text:
        return _UNSET
    if key in _INT_FIELDS:
        if not (text.isascii() and text.isdecimal()) or len(text) > MAX_ID_DIGITS:
            return _INVALID
        return int(text)
    return text


def _extras_text(name: str, message: str) -> str:
    label = _FIELD_LABELS.get(name) or f"The {name.replace('_', ' ')}"
    return f"{label} {message}."


def _address(
    kind: ProviderKind, text: str, extras: ProviderExtras | None, problems: dict[str, str]
) -> str | None:
    fixed = PROVIDERS[kind].fixed_address
    entered = text.strip()
    if fixed is not None and not entered:
        return fixed
    try:
        address = canonical_address(entered)
        if extras is not None:
            check_address_for_provider(kind, address, extras)
    except AddressInvalid as error:
        problems["address"] = error.message
        return None
    refusal = literal_host_refusal(split_address(address)[0])
    if refusal is not None:
        problems["address"] = refusal
        return None
    return None if extras is None else address


def _username(kind: ProviderKind, text: str, problems: dict[str, str]) -> str | None:
    username = text.strip()
    if not username:
        problems["username"] = "Enter the user name."
    elif len(username) > USERNAME_MAX:
        problems["username"] = f"The user name is at most {USERNAME_MAX} characters."
    elif ":" in username:
        problems["username"] = "The user name may not contain a colon."
    elif any(unicodedata.category(c) in HIDDEN_CATEGORIES for c in username):
        problems["username"] = "The user name may not contain control characters."
    elif kind == "ecr" and not _ACCESS_KEY_ID.match(username):
        problems["username"] = (
            "An access key ID looks like AKIAIOSFODNN7EXAMPLE. Temporary keys are not supported."
        )
    else:
        return username
    return None


def _secret(secret: SecretStr, problems: dict[str, str]) -> SecretStr | None:
    text = secret.get_secret_value()
    if len(text) > SECRET_MAX:
        problems["secret"] = f"The secret is at most {SECRET_MAX} characters."
    elif any(unicodedata.category(c) == "Cc" and c not in _SECRET_WHITESPACE for c in text):
        problems["secret"] = "The secret may not contain control characters."
    else:
        return secret
    return None


def _ca_bundle(
    spec: RegistrySpec,
    current: StoredRegistryCredential | None,
    bundle: CheckedBundle | None,
    offered: bool,
    verify_tls: bool,
    problems: dict[str, str],
) -> str | None:
    if not offered:
        return None
    if spec.ca_bundle_problem:
        problems["ca_bundle"] = spec.ca_bundle_problem
        return None if current is None else current.ca_bundle
    if not verify_tls:
        if bundle is not None:
            problems["ca_bundle"] = (
                "A certificate bundle is used only when certificates are verified."
            )
        return None
    if bundle is not None:
        return bundle.pem
    if spec.remove_ca_bundle or current is None:
        return None
    return current.ca_bundle


def _may_keep(
    current: StoredRegistryCredential | None,
    kind: ProviderKind,
    address: str | None,
    extras: ProviderExtras | None,
    ca_bundle: str | None,
    verify_tls: bool,
    problems: dict[str, str],
) -> bool:
    """Decide whether a blank secret keeps the stored one; record a problem if it cannot."""
    if current is None or not current.authentication_enabled or not current.has_secret:
        problems["secret"] = _SECRET_REQUIRED
        return False
    if address is None or extras is None:
        return False
    redirected = (
        kind != current.provider
        or address != current.url
        or _destinations(kind, extras) != _destinations(kind, current.extras)
        or ca_bundle != current.ca_bundle
        or verify_tls != current.verify_tls
    )
    if redirected:
        problems["secret"] = _REENTER_SECRET
        return False
    return True


def _destinations(kind: ProviderKind, extras: ProviderExtras) -> tuple[object, ...]:
    return tuple(getattr(extras, name) for name in PROVIDERS[kind].destination_extras)


def _changed(
    current: StoredRegistryCredential, fields: CredentialFields, extras: ProviderExtras
) -> tuple[str, ...]:
    differences = {
        "name": fields.name != current.name,
        "provider": fields.provider != current.provider,
        "address": fields.url != current.url,
        "authentication": fields.authentication_enabled != current.authentication_enabled,
        "username": fields.username != current.username,
        "extras": extras != current.extras,
        "verify_tls": fields.verify_tls != current.verify_tls,
        "ca_bundle": fields.ca_bundle != current.ca_bundle,
    }
    return tuple(name for name, differs in differences.items() if differs)
