"""Startup self-check: each failed precondition names its problem and its remedy."""

import os
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from app.config import (
    ALLOW_KEY_ROTATION_VAR,
    ENV_PREFIX,
    JWT_AUDIENCE_VAR,
    JWT_HEADER_VAR,
    JWT_ISSUER_VAR,
    JWT_JWKS_URL_VAR,
    JWT_KEY_FILE_VAR,
    PREVIOUS_SECRET_KEY_FILE_VAR,
    PREVIOUS_SECRET_KEY_VAR,
    SECRET_KEY_DIR_VAR,
    SECRET_KEY_FILE_VAR,
    SECRET_KEY_VAR,
    TLS_CERT_FILE_VAR,
    TLS_KEY_FILE_VAR,
    TLS_SELF_SIGNED_VAR,
    ConfigError,
)
from app.db.engine import DatabaseError
from app.db.migrations import MigrationError, NewerSchemaError
from app.instance_lock import InstanceLocked
from app.logging import get_logger

if TYPE_CHECKING:
    from app.crypto.managed_key import ManagedKeyRefused
    from app.crypto.startup import KeyMismatchError
    from app.services.compose_setup import ComposeBinaryInvalid, ComposeRuntime

type CheckName = Literal[
    "encryption_key",
    "database",
    "data_directory",
    "compose_cli",
    "buildx",
    "tls_material",
    "jwt_settings",
]

logger = get_logger(__name__)

COMPOSE_BINARY_VAR: Final = f"{ENV_PREFIX}COMPOSE_BINARY"
BUILDX_BINARY_VAR: Final = f"{ENV_PREFIX}BUILDX_BINARY"
SQLITE_TMPDIR_VAR: Final = "SQLITE_TMPDIR"
_KEY_VARIABLES: Final = frozenset(
    {
        SECRET_KEY_VAR,
        SECRET_KEY_FILE_VAR,
        SECRET_KEY_DIR_VAR,
        PREVIOUS_SECRET_KEY_VAR,
        PREVIOUS_SECRET_KEY_FILE_VAR,
        ALLOW_KEY_ROTATION_VAR,
    }
)

ENCRYPTION_KEY_REMEDY: Final = (
    f"Set at most one of {SECRET_KEY_VAR} or {SECRET_KEY_FILE_VAR}, or set neither and mount a "
    f"volume at {SECRET_KEY_DIR_VAR}; a supplied key is base64 of 32 random bytes, for example "
    "the output of `openssl rand -base64 32`"
)
KEY_MISMATCH_REMEDY: Final = (
    "Start with the key this database was created with, or restore a backup taken with the "
    "current key"
)
TLS_REMEDY: Final = (
    f"Check that {TLS_CERT_FILE_VAR} and {TLS_KEY_FILE_VAR} are readable by the user the "
    "process runs as (uid 10001 in the image) and hold a PEM certificate and its key"
)
JWT_REMEDY: Final = (
    f"Set {JWT_HEADER_VAR}, {JWT_ISSUER_VAR}, {JWT_AUDIENCE_VAR} and exactly one of "
    f"{JWT_JWKS_URL_VAR} (an https: URL) or {JWT_KEY_FILE_VAR} (a PEM public key or a JWKS "
    f"document), or remove every {ENV_PREFIX}JWT_* setting to turn access platform sign-in off"
)
MIGRATION_REMEDY: Final = (
    "Reinstall the image the database was created with, or restore a backup taken with it"
)
_DATABASE_DAMAGE_REMEDY: Final = (
    "Check that the data directory holds the dockmin database and that the file is "
    "not damaged; restore a backup if it is"
)
NEWER_SCHEMA_REMEDY: Final = "The database has not been modified"


class StartupCheckFailed(Exception):
    """A startup precondition failed; problem and remedy are product text and carry no secret."""

    def __init__(self, check: CheckName, problem: str, remedy: str, *, fatal: bool = True) -> None:
        super().__init__(problem)
        self.check: CheckName = check
        self.problem = problem
        self.remedy = remedy
        self.fatal = fatal


def log_check_failure(
    failure: StartupCheckFailed, *, during: Literal["startup", "restore"] = "startup"
) -> None:
    """Write the one structured line that reports a failed check.

    A check that fails while a restore restarts the runtime does not end the process.
    """
    fatal = failure.fatal and during == "startup"
    log = logger.error if fatal else logger.warning
    log(
        "startup_check_failed",
        extra={
            "check": failure.check,
            "problem": failure.problem,
            "remedy": failure.remedy,
            "fatal": fatal,
            "during": during,
        },
    )


def check_data_directory(data_dir: Path) -> None:
    """Raise StartupCheckFailed unless data_dir is a real directory the process can write to."""
    remedy = (
        f"Create {data_dir} or mount a volume there, and make it writable by the user the "
        "process runs as (uid 10001 in the image)"
    )
    if data_dir.is_symlink():
        raise StartupCheckFailed(
            "data_directory",
            f"The data directory {data_dir} is a symbolic link",
            f"Point {ENV_PREFIX}DATA_DIR at the directory itself, or mount a volume at {data_dir}",
        )
    if not data_dir.exists():
        raise StartupCheckFailed(
            "data_directory", f"The data directory {data_dir} does not exist", remedy
        )
    if not data_dir.is_dir():
        raise StartupCheckFailed("data_directory", f"{data_dir} is not a directory", remedy)
    try:
        if not os.access(data_dir, os.W_OK | os.X_OK):
            raise OSError
        with tempfile.TemporaryFile(dir=data_dir):
            pass
    except OSError:
        raise StartupCheckFailed(
            "data_directory", f"The data directory {data_dir} is not writable", remedy
        ) from None


def check_sqlite_tmpdir(environ: Mapping[str, str] = os.environ) -> None:
    """Create the directory SQLite is told to put temporary files in, when one is configured."""
    configured = environ.get(SQLITE_TMPDIR_VAR)
    if not configured:
        return
    try:
        Path(configured).mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise StartupCheckFailed(
            "data_directory",
            f"The SQLite temporary directory {configured} cannot be created",
            f"Check that {SQLITE_TMPDIR_VAR} names a path the process user can create and write",
        ) from None


def workdir_unusable() -> StartupCheckFailed:
    """Describe a work directory under the data directory that cannot be prepared."""
    return StartupCheckFailed(
        "data_directory",
        "The Docker connection work directory under the data directory cannot be prepared",
        "Check that it is a real directory the process user owns, not a link or a file",
    )


def compose_directories_unusable() -> StartupCheckFailed:
    """Describe a Compose directory under the data directory that cannot be prepared."""
    return StartupCheckFailed(
        "data_directory",
        "A Compose work or stacks directory under the data directory cannot be prepared",
        "Check that they are real directories the process user owns, not links or files",
    )


def check_compose(compose: "ComposeRuntime") -> StartupCheckFailed | None:
    """Describe why Compose cannot run stacks, or None; the application starts either way."""
    floor = compose.floor.text
    remedy = (
        f"The image carries Compose; otherwise install Compose {floor} or later, or set "
        f"{COMPOSE_BINARY_VAR} to its path"
    )
    if compose.binary is None:
        problem = "The Docker Compose binary was not found; stacks are unavailable"
    elif compose.version is None:
        problem = "The Docker Compose version could not be read; stacks are unavailable"
    elif not compose.supported:
        problem = (
            f"Docker Compose {compose.version.text} is older than {floor}; stacks are unavailable"
        )
    else:
        return None
    return StartupCheckFailed("compose_cli", problem, remedy, fatal=False)


def check_buildx(*, available: bool) -> StartupCheckFailed | None:
    """Describe why stack builds cannot run, or None; the application starts either way."""
    if available:
        return None
    return StartupCheckFailed(
        "buildx",
        "The buildx binary was not found or is not trusted; stack builds are unavailable",
        f"The image carries buildx; otherwise install it, or set {BUILDX_BINARY_VAR} to an "
        "absolute path to an executable that only root or the process user can write",
        fatal=False,
    )


def from_compose_binary_error(error: "ComposeBinaryInvalid") -> StartupCheckFailed:
    """Describe a configured Compose binary that fails the trust check."""
    return StartupCheckFailed(
        "compose_cli",
        str(error),
        f"Set {COMPOSE_BINARY_VAR} to an absolute path to an executable that only root or the "
        "process user can write, or unset it to search PATH",
    )


def from_config_error(error: ConfigError) -> StartupCheckFailed | None:
    """Attribute a configuration failure to a check, or None when it is not one of the six."""
    variables = set(error.variables)
    if variables & _KEY_VARIABLES:
        return StartupCheckFailed("encryption_key", str(error), ENCRYPTION_KEY_REMEDY)
    files = variables - {TLS_SELF_SIGNED_VAR}
    if files and all(name.startswith(f"{ENV_PREFIX}TLS_") for name in files):
        return StartupCheckFailed("tls_material", str(error), TLS_REMEDY)
    if variables and all(name.startswith(f"{ENV_PREFIX}JWT_") for name in variables):
        return StartupCheckFailed("jwt_settings", str(error), JWT_REMEDY)
    return None


def from_key_mismatch(error: "KeyMismatchError") -> StartupCheckFailed:
    """Describe keys that do not open the stored data."""
    return StartupCheckFailed("encryption_key", str(error), KEY_MISMATCH_REMEDY)


def from_managed_key_refusal(error: "ManagedKeyRefused") -> StartupCheckFailed:
    """Describe a generated key that cannot be read or created."""
    return StartupCheckFailed("encryption_key", error.problem, error.remedy)


def from_database_error(
    error: MigrationError | sqlite3.Error | DatabaseError | OSError,
    *,
    during_key_check: bool = False,
) -> StartupCheckFailed:
    """Describe a database that cannot be opened, created or brought to the current schema."""
    match error:
        case sqlite3.Error() | DatabaseError() if during_key_check:
            return StartupCheckFailed(
                "database",
                f"The database could not be read while checking the encryption key: {error}",
                _DATABASE_DAMAGE_REMEDY,
            )
        case DatabaseError():
            return StartupCheckFailed(
                "database", f"The database could not be opened: {error}", _DATABASE_DAMAGE_REMEDY
            )
        case NewerSchemaError():
            return StartupCheckFailed(
                "database", f"The database schema check failed: {error}", NEWER_SCHEMA_REMEDY
            )
        case MigrationError():
            return StartupCheckFailed(
                "database", f"The database schema check failed: {error}", MIGRATION_REMEDY
            )
        case sqlite3.Error():
            return StartupCheckFailed(
                "database", f"The database could not be opened: {error}", _DATABASE_DAMAGE_REMEDY
            )
        case _:
            return StartupCheckFailed(
                "database",
                f"The database file could not be created or read: {error.strerror or 'I/O error'}",
                "Check the permissions and free space of the data directory",
            )


def from_instance_lock_error(error: InstanceLocked | OSError) -> StartupCheckFailed:
    """Describe a data directory whose lock cannot be taken."""
    if isinstance(error, InstanceLocked):
        return StartupCheckFailed(
            "data_directory",
            "Another dockmin process is using the data directory",
            "Stop the other process, or give this one its own data directory",
        )
    return StartupCheckFailed(
        "data_directory",
        "The data directory's lock file cannot be used",
        "Check that the lock file in the data directory is a regular file the process user owns",
    )
