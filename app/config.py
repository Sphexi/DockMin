"""Application settings read from DOCKMIN_* environment variables."""

import base64
import binascii
import hmac
import ipaddress
import os
import re
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

from fastapi import Request
from pydantic import (
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.auth.jwt_key_documents import (
    PERMITTED_ALGORITHMS,
    KeyDocumentError,
    VerificationKey,
    parse_key_document,
)

ENV_PREFIX = "DOCKMIN_"
DEFAULT_INSTANCE_NAME: Final = "dockmin"
KEY_BYTES = 32
DB_FILENAME = "dockmin.db"
SECRET_KEY_VAR = f"{ENV_PREFIX}SECRET_KEY"
SECRET_KEY_FILE_VAR = f"{ENV_PREFIX}SECRET_KEY_FILE"
PREVIOUS_SECRET_KEY_VAR = f"{ENV_PREFIX}PREVIOUS_SECRET_KEY"
PREVIOUS_SECRET_KEY_FILE_VAR = f"{ENV_PREFIX}PREVIOUS_SECRET_KEY_FILE"
ALLOW_KEY_ROTATION_VAR = f"{ENV_PREFIX}ALLOW_KEY_ROTATION"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
type HttpMode = Literal["serve", "redirect", "off"]
type KeySource = Literal["variable", "file", "generated"]
type JwtPasswordSignIn = Literal["all", "administrators"]
type TlsSource = Literal["supplied", "self_signed"]

SECRET_KEY_DIR_VAR = f"{ENV_PREFIX}SECRET_KEY_DIR"
MANAGED_KEY_FILENAME = "secret.key"
TLS_SELF_SIGNED_VAR = f"{ENV_PREFIX}TLS_SELF_SIGNED"
SELF_SIGNED_DIRECTORY = "server-tls"
SELF_SIGNED_CERT_FILENAME = "cert.pem"
SELF_SIGNED_KEY_FILENAME = "key.pem"
TLS_CERT_FILE_VAR = f"{ENV_PREFIX}TLS_CERT_FILE"
TLS_KEY_FILE_VAR = f"{ENV_PREFIX}TLS_KEY_FILE"
HTTPS_PORT_VAR = f"{ENV_PREFIX}HTTPS_PORT"
HTTP_MODE_VAR = f"{ENV_PREFIX}HTTP_MODE"
PERMITTED_INTERNAL_NETWORKS_VAR = f"{ENV_PREFIX}PERMITTED_INTERNAL_NETWORKS"
JWT_HEADER_VAR = f"{ENV_PREFIX}JWT_HEADER"
JWT_ISSUER_VAR = f"{ENV_PREFIX}JWT_ISSUER"
JWT_AUDIENCE_VAR = f"{ENV_PREFIX}JWT_AUDIENCE"
JWT_JWKS_URL_VAR = f"{ENV_PREFIX}JWT_JWKS_URL"
JWT_KEY_FILE_VAR = f"{ENV_PREFIX}JWT_KEY_FILE"
JWT_ALGORITHMS_VAR = f"{ENV_PREFIX}JWT_ALGORITHMS"
JWT_DEFAULT_ALGORITHM: Final = "ES256"
JWT_URL_MAX_CHARS: Final = 2048
_JWT_HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}", re.ASCII)
_JWT_CLAIM_PATH = re.compile(r"[A-Za-z0-9_:/@-]{1,64}(?:\.[A-Za-z0-9_:/@-]{1,64}){0,7}", re.ASCII)
_JWT_URL_TEXT = re.compile(r"[\x21-\x7e]+", re.ASCII)
_VARIABLE_NAME = re.compile(rf"{ENV_PREFIX}[A-Z0-9_]+")


class ConfigError(Exception):
    """Raised when the environment does not describe a valid configuration."""

    def __init__(self, message: str, variables: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.variables = variables


class Settings(BaseSettings):
    """Validated application configuration."""

    # Input echo is disabled so a rejected key can never appear in an error message.
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=None,
        extra="ignore",
        hide_input_in_errors=True,
    )

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    data_dir: Path = Path("/data")
    log_level: LogLevel = "INFO"
    access_log: bool = False
    instance_name: str = Field(default=DEFAULT_INSTANCE_NAME, min_length=1, max_length=64)
    session_lifetime: timedelta = Field(default=timedelta(hours=12), gt=timedelta(0))
    session_idle_timeout: timedelta = Field(default=timedelta(hours=1), gt=timedelta(0))
    cookie_secure: bool = True
    # NoDecode keeps pydantic-settings from demanding JSON for a comma-separated list.
    trusted_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Addresses or networks whose forwarded-client headers are believed. Empty means client
    # addresses are not known to be per-client, so a failed login is counted against an address
    # but never refused on its account.
    trusted_proxies: Annotated[list[str], NoDecode] = Field(default_factory=list)
    login_max_failures: int = Field(default=5, ge=1, le=1000)
    login_lockout_window: timedelta = Field(default=timedelta(minutes=15), gt=timedelta(0))
    password_min_length: int = Field(default=12, ge=8, le=128)
    password_required_classes: int = Field(default=0, ge=0, le=4)
    argon2_memory_kib: int = Field(default=65_536, ge=8_192, le=4_194_304)
    argon2_iterations: int = Field(default=3, ge=1, le=100)
    argon2_lanes: int = Field(default=4, ge=1, le=64)
    argon2_salt_bytes: int = Field(default=16, ge=16, le=64)
    argon2_hash_bytes: int = Field(default=32, ge=16, le=64)
    db_busy_timeout_ms: int = Field(default=5000, ge=0, le=600_000)
    snapshot_interval_seconds: int = Field(default=60, ge=15, le=3600)
    compose_binary: Path | None = None
    buildx_binary: Path | None = None
    compose_timeout_seconds: int = Field(default=600, ge=60, le=3600)
    update_health_timeout_seconds: int = Field(default=180, ge=30, le=1800)
    activity_retention_days: int | None = Field(default=None, ge=7, le=3650)
    secret_key: SecretStr | None = None
    secret_key_file: Path | None = None
    secret_key_dir: Path = Path("/key")
    previous_secret_key: SecretStr | None = None
    previous_secret_key_file: Path | None = None
    allow_key_rotation: bool = False
    development_checks: bool = False
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    tls_self_signed: bool = True
    https_port: int = Field(default=8443, ge=1, le=65535)
    https_public_port: int | None = Field(default=None, ge=1, le=65535)
    http_mode: HttpMode | None = None
    max_concurrent_requests: int = Field(default=256, ge=8, le=4096)
    permitted_internal_networks: Annotated[list[str], NoDecode] = Field(default_factory=list)
    jwt_header: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_jwks_url: str | None = None
    jwt_key_file: Path | None = None
    jwt_algorithms: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [JWT_DEFAULT_ALGORITHM]
    )
    jwt_identity_claim: str = "sub"
    jwt_username_claim: str | None = None
    jwt_create_users: bool = False
    jwt_max_age: timedelta = Field(default=timedelta(hours=24), gt=timedelta(0))
    jwt_password_sign_in: JwtPasswordSignIn = "all"

    _encryption_key: bytes | None = PrivateAttr(default=None)
    _previous_encryption_key: bytes | None = PrivateAttr(default=None)
    _jwt_file_keys: tuple[VerificationKey, ...] = PrivateAttr(default=())

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Restrict sources to init arguments and the process environment."""
        return (init_settings, env_settings)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator(
        "session_lifetime",
        "session_idle_timeout",
        "login_lockout_window",
        "jwt_max_age",
        mode="before",
    )
    @classmethod
    def _bare_number_is_seconds(cls, value: object) -> object:
        # Environment values arrive as strings, and pydantic reads a bare numeric string as
        # an invalid ISO 8601 duration rather than as seconds.
        if isinstance(value, str) and value.strip().isdigit():
            return timedelta(seconds=int(value))
        return value

    @field_validator(
        "secret_key",
        "secret_key_file",
        "previous_secret_key",
        "previous_secret_key_file",
        "compose_binary",
        "buildx_binary",
        "activity_retention_days",
        "tls_cert_file",
        "tls_key_file",
        "https_public_port",
        "http_mode",
        "jwt_header",
        "jwt_issuer",
        "jwt_audience",
        "jwt_jwks_url",
        "jwt_key_file",
        "jwt_username_claim",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # Compose substitutes an unset variable with an empty string.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "https_port",
        "max_concurrent_requests",
        "secret_key_dir",
        "jwt_identity_claim",
        "jwt_max_age",
        "jwt_password_sign_in",
        mode="before",
    )
    @classmethod
    def _blank_is_default(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str) and not value.strip():
            assert info.field_name is not None
            return cls.model_fields[info.field_name].default
        return value

    @field_validator("http_mode", mode="before")
    @classmethod
    def _normalise_http_mode(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("jwt_create_users", mode="before")
    @classmethod
    def _blank_is_no_jit(cls, value: object) -> object:
        return False if isinstance(value, str) and not value.strip() else value

    @field_validator("cookie_secure", "tls_self_signed", mode="before")
    @classmethod
    def _blank_is_secure(cls, value: object) -> object:
        return True if isinstance(value, str) and not value.strip() else value

    @field_validator("trusted_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            value = [part for part in (item.strip() for item in value.split(",")) if part]
        if isinstance(value, list):
            return [_normalise_origin(str(item)) for item in value]
        return value

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_proxies(cls, value: object) -> object:
        if isinstance(value, str):
            value = [part for part in (item.strip() for item in value.split(",")) if part]
        if isinstance(value, list):
            return [_normalise_proxy(str(item)) for item in value]
        return value

    @field_validator("permitted_internal_networks", mode="before")
    @classmethod
    def _split_permitted_networks(cls, value: object) -> object:
        if isinstance(value, str):
            value = [part for part in (item.strip() for item in value.split(",")) if part]
        if isinstance(value, list):
            return [_normalise_network(str(item)) for item in value]
        return value

    @field_validator("jwt_header")
    @classmethod
    def _check_jwt_header(cls, value: str | None) -> str | None:
        if value is not None and _JWT_HEADER_NAME.fullmatch(value.strip()) is None:
            raise ValueError("must be a header name of letters, digits and hyphens")
        return None if value is None else value.strip()

    @field_validator("jwt_algorithms", mode="before")
    @classmethod
    def _split_jwt_algorithms(cls, value: object) -> object:
        if isinstance(value, str):
            value = [part for part in (item.strip() for item in value.split(",")) if part]
        if isinstance(value, list):
            if not value:
                return [JWT_DEFAULT_ALGORITHM]
            if any(str(item) not in PERMITTED_ALGORITHMS for item in value):
                raise ValueError(f"entries must be among {', '.join(PERMITTED_ALGORITHMS)}")
            return list(dict.fromkeys(str(item) for item in value))
        return value

    @field_validator("jwt_identity_claim", "jwt_username_claim")
    @classmethod
    def _check_jwt_claim_path(cls, value: str | None) -> str | None:
        if value is not None and _JWT_CLAIM_PATH.fullmatch(value.strip()) is None:
            raise ValueError("must be a claim name, or dotted names for a nested claim")
        return None if value is None else value.strip()

    @field_validator("jwt_jwks_url")
    @classmethod
    def _check_jwt_jwks_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value.strip())
        if (
            len(value) > JWT_URL_MAX_CHARS
            or _JWT_URL_TEXT.fullmatch(value.strip()) is None
            or parts.scheme != "https"
            or not parts.hostname
            or "@" in parts.netloc
            or parts.fragment
        ):
            raise ValueError("must be an https: URL without credentials or a fragment")
        return value.strip()

    @field_validator("jwt_issuer", "jwt_audience")
    @classmethod
    def _check_jwt_expected_claim(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("must be text without control characters")
        return value

    @field_validator("allow_key_rotation", "development_checks", "access_log", mode="before")
    @classmethod
    def _blank_is_off(cls, value: object) -> object:
        return False if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _check_listeners(self) -> "Settings":
        if (self.tls_cert_file is None) != (self.tls_key_file is None):
            raise ValueError(f"{TLS_CERT_FILE_VAR} and {TLS_KEY_FILE_VAR} must be set together")
        if self.tls_enabled:
            if self.https_port == self.port:
                raise ValueError(f"{HTTPS_PORT_VAR} must differ from {ENV_PREFIX}PORT")
        elif self.http_mode in ("redirect", "off"):
            raise ValueError(
                f"{HTTP_MODE_VAR}={self.http_mode} needs TLS: set {TLS_CERT_FILE_VAR} and "
                f"{TLS_KEY_FILE_VAR}, or remove {TLS_SELF_SIGNED_VAR}=false; without TLS the "
                "plain listener is the only one"
            )
        return self

    @model_validator(mode="after")
    def _check_access_platform(self) -> "Settings":
        configured = (
            (JWT_ISSUER_VAR, self.jwt_issuer),
            (JWT_AUDIENCE_VAR, self.jwt_audience),
            (JWT_JWKS_URL_VAR, self.jwt_jwks_url),
            (JWT_KEY_FILE_VAR, self.jwt_key_file),
        )
        if self.jwt_header is None:
            stray = [name for name, value in configured if value is not None]
            if self.jwt_create_users:
                stray.append(f"{ENV_PREFIX}JWT_CREATE_USERS")
            if stray:
                raise ValueError(f"{JWT_HEADER_VAR} must be set when {stray[0]} is set")
            return self
        for name, value in configured[:2]:
            if value is None:
                raise ValueError(f"{name} must be set when {JWT_HEADER_VAR} is set")
        if (self.jwt_jwks_url is None) == (self.jwt_key_file is None):
            raise ValueError(
                f"exactly one of {JWT_JWKS_URL_VAR} or {JWT_KEY_FILE_VAR} must be set when "
                f"{JWT_HEADER_VAR} is set"
            )
        if self.jwt_key_file is not None:
            self._jwt_file_keys = self._read_jwt_key_file(self.jwt_key_file)
        return self

    def _read_jwt_key_file(self, path: Path) -> tuple[VerificationKey, ...]:
        try:
            data = path.read_bytes()
        except OSError:
            raise ValueError(f"{JWT_KEY_FILE_VAR} does not point to a readable file") from None
        try:
            keys = parse_key_document(data, self.jwt_algorithms)
        except KeyDocumentError as error:
            raise ValueError(f"{JWT_KEY_FILE_VAR} is not usable: {error}") from None
        if not keys:
            raise ValueError(
                f"{JWT_KEY_FILE_VAR} holds no key that can verify a permitted algorithm"
            )
        return tuple(keys)

    @model_validator(mode="after")
    def _resolve_encryption_key(self) -> "Settings":
        if self.secret_key is not None and self.secret_key_file is not None:
            raise ValueError(f"at most one of {SECRET_KEY_VAR} or {SECRET_KEY_FILE_VAR} may be set")
        if self.previous_secret_key is not None and self.previous_secret_key_file is not None:
            raise ValueError(
                f"at most one of {PREVIOUS_SECRET_KEY_VAR} or {PREVIOUS_SECRET_KEY_FILE_VAR} "
                "may be set"
            )
        current: bytes | None = None
        match self.key_source:
            case "variable":
                assert self.secret_key is not None
                current = _decode_key(self.secret_key.get_secret_value(), SECRET_KEY_VAR)
            case "file":
                assert self.secret_key_file is not None
                encoded = self._read_key_file(self.secret_key_file, SECRET_KEY_FILE_VAR)
                current = _decode_key(encoded, SECRET_KEY_FILE_VAR)
            case "generated":
                self._check_key_directory()
        previous: bytes | None = None
        if self.previous_secret_key is not None:
            previous = _decode_key(
                self.previous_secret_key.get_secret_value(), PREVIOUS_SECRET_KEY_VAR
            )
        elif self.previous_secret_key_file is not None:
            encoded = self._read_key_file(
                self.previous_secret_key_file, PREVIOUS_SECRET_KEY_FILE_VAR
            )
            previous = _decode_key(encoded, PREVIOUS_SECRET_KEY_FILE_VAR)
        if previous is not None:
            # Equal keys would make the re-key window a no-op that looks like progress.
            if current is not None and hmac.compare_digest(previous, current):
                raise ValueError(f"{self.previous_key_variable} must differ from {SECRET_KEY_VAR}")
            self._previous_encryption_key = previous
        self._encryption_key = current
        return self

    def _check_key_directory(self) -> None:
        if not self.secret_key_dir.is_absolute():
            raise ValueError(f"{SECRET_KEY_DIR_VAR} must be an absolute path")
        key_dir = str(self.secret_key_dir.resolve())
        data_dir = str(self.data_dir.resolve())
        if key_dir == data_dir or _is_inside(key_dir, data_dir) or _is_inside(data_dir, key_dir):
            raise ValueError(
                f"{SECRET_KEY_DIR_VAR} must be a different directory from {ENV_PREFIX}DATA_DIR, "
                "neither inside it nor containing it"
            )

    @staticmethod
    def _read_key_file(path: Path, variable: str) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise ValueError(f"{variable} does not point to a readable file") from None

    @property
    def jwt_enabled(self) -> bool:
        """Report whether sign-in from an access platform's signed token is on."""
        return self.jwt_header is not None

    @property
    def jwt_file_keys(self) -> tuple[VerificationKey, ...]:
        """Return the keys read from the key file, empty when the keys come from a URL."""
        return self._jwt_file_keys

    @property
    def key_source(self) -> KeySource:
        """Return where the encryption key comes from."""
        if self.secret_key is not None:
            return "variable"
        if self.secret_key_file is not None:
            return "file"
        return "generated"

    @property
    def key_description(self) -> str:
        """Return a phrase naming where the encryption key comes from, for mismatch messages."""
        match self.key_source:
            case "variable":
                return f"the configured {SECRET_KEY_VAR}"
            case "file":
                return f"the key in {self.secret_key_file} ({SECRET_KEY_FILE_VAR})"
            case "generated":
                return f"the key in {self.managed_key_file}"

    @property
    def previous_key_variable(self) -> str:
        """Return the variable that supplies the previous key, the file form when it is set."""
        if self.previous_secret_key_file is not None:
            return PREVIOUS_SECRET_KEY_FILE_VAR
        return PREVIOUS_SECRET_KEY_VAR

    @property
    def previous_key_description(self) -> str:
        """Return a phrase naming where the previous key comes from, for refusal messages."""
        if self.previous_secret_key_file is not None:
            return f"the key in {self.previous_secret_key_file} ({PREVIOUS_SECRET_KEY_FILE_VAR})"
        return f"the configured {PREVIOUS_SECRET_KEY_VAR}"

    @property
    def managed_key_file(self) -> Path | None:
        """Return the generated key's file, or None when the key is supplied."""
        if self.key_source != "generated":
            return None
        return self.secret_key_dir / MANAGED_KEY_FILENAME

    @property
    def tls_source(self) -> TlsSource | None:
        """Return where the served certificate comes from, or None when HTTPS is off."""
        if self.tls_cert_file is not None and self.tls_key_file is not None:
            return "supplied"
        return "self_signed" if self.tls_self_signed else None

    @property
    def tls_enabled(self) -> bool:
        """Report whether HTTPS is served directly: a supplied or a generated certificate."""
        return self.tls_source is not None

    @property
    def served_cert_file(self) -> Path | None:
        """Return the certificate file the HTTPS listener serves, or None when HTTPS is off."""
        match self.tls_source:
            case "supplied":
                return self.tls_cert_file
            case "self_signed":
                return self._self_signed_directory / SELF_SIGNED_CERT_FILENAME
            case None:
                return None

    @property
    def served_key_file(self) -> Path | None:
        """Return the private key file the HTTPS listener serves, or None when HTTPS is off."""
        match self.tls_source:
            case "supplied":
                return self.tls_key_file
            case "self_signed":
                return self._self_signed_directory / SELF_SIGNED_KEY_FILENAME
            case None:
                return None

    @property
    def _self_signed_directory(self) -> Path:
        return self.data_dir / SELF_SIGNED_DIRECTORY

    @property
    def effective_http_mode(self) -> HttpMode:
        """Return the plain listener's mode: as configured, else redirect with TLS, else serve."""
        if self.http_mode is not None:
            return self.http_mode
        return "redirect" if self.tls_enabled else "serve"

    @property
    def redirect_port(self) -> int:
        """Return the HTTPS port a redirect names: the public port when set, else the listener's."""
        return self.https_public_port or self.https_port

    @property
    def db_path(self) -> Path:
        """Return the location of the SQLite database file."""
        return self.data_dir / DB_FILENAME

    @property
    def encryption_key(self) -> bytes | None:
        """Return the decoded 32-byte encryption key, or None when it is generated."""
        return self._encryption_key

    @property
    def previous_encryption_key(self) -> bytes | None:
        """Return the decoded previous key during a re-key window, otherwise None."""
        return self._previous_encryption_key

    @classmethod
    def from_mapping(cls, environ: Mapping[str, str]) -> "Settings":
        """Build settings from a mapping, ignoring the process environment entirely."""
        values: dict[str, Any] = {}
        for name, field in cls.model_fields.items():
            raw = environ.get(f"{ENV_PREFIX}{name.upper()}")
            values[name] = raw if raw is not None else field.get_default(call_default_factory=True)
        return cls(**values)


def _is_inside(path: str, directory: str) -> bool:
    return path.startswith(directory.rstrip(os.sep) + os.sep)


def _normalise_origin(value: str) -> str:
    """Return scheme://host[:port] in lower case, rejecting anything that is not an origin."""
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.hostname is None:
        raise ValueError(f"{ENV_PREFIX}TRUSTED_ORIGINS entries must look like https://host[:port]")
    if "@" in parts.netloc:
        raise ValueError(f"{ENV_PREFIX}TRUSTED_ORIGINS entries must not carry credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"{ENV_PREFIX}TRUSTED_ORIGINS entries must not carry a path or query")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _normalise_proxy(value: str) -> str:
    """Return a proxy entry as it will be matched, rejecting anything that is not an address."""
    entry = value.strip()
    if entry == "*":
        return entry
    try:
        return str(ipaddress.ip_network(entry, strict=False))
    except ValueError:
        raise ValueError(
            f"{ENV_PREFIX}TRUSTED_PROXIES entries must be IP addresses, networks, or *"
        ) from None


def _normalise_network(value: str) -> str:
    """Return an address or network as a network string, rejecting anything else."""
    try:
        return str(ipaddress.ip_network(value.strip(), strict=False))
    except ValueError:
        raise ValueError(
            f"{PERMITTED_INTERNAL_NETWORKS_VAR} entries must be IP addresses or networks"
        ) from None


def _decode_key(encoded: str, source: str) -> bytes:
    try:
        key = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{source} is not valid base64") from None
    if len(key) != KEY_BYTES:
        raise ValueError(f"{source} must be base64 of exactly {KEY_BYTES} bytes")
    return key


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Validate configuration, raising ConfigError with value-free messages."""
    try:
        if environ is None:
            return Settings()
        return Settings.from_mapping(environ)
    except ValidationError as exc:
        message, variables = _describe(exc)
        raise ConfigError(message, variables) from None


def _describe(exc: ValidationError) -> tuple[str, tuple[str, ...]]:
    lines: list[str] = []
    variables: dict[str, None] = {}
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        message = error["msg"].removeprefix("Value error, ")
        if error["loc"]:
            variable = f"{ENV_PREFIX}{str(error['loc'][0]).upper()}"
            variables[variable] = None
            message = message if message.startswith(ENV_PREFIX) else f"{variable}: {message}"
        variables.update(dict.fromkeys(_VARIABLE_NAME.findall(message)))
        lines.append(message)
    return "invalid configuration: " + "; ".join(lines), tuple(variables)


def get_settings(request: Request) -> Settings:
    """Return the settings the application was constructed with."""
    settings: Settings = request.app.state.settings
    return settings
