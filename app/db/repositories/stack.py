"""Repositories for stacks and their variables: the only writers of stack and stack_variable."""

import asyncio
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from secrets import choice
from typing import Any, Final, Literal, cast

from app.crypto.envelope import DecryptionError
from app.crypto.registry import STACK_VARIABLE_VALUE
from app.db.engine import DatabaseError
from app.db.repositories.base import Repository, like_prefix
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.encrypted import EncryptedStr, SecretRepository
from app.db.repositories.errors import (
    MissingReferenceError,
    RepositoryError,
    constraints_as_domain_errors,
)
from app.db.repositories.pagination import Page
from app.db.repositories.registry_credential import SecretUnreadableError
from app.db.repositories.stack_build_context import MAX_CONTEXTS, StackContextRepository
from app.db.repositories.stack_file import StackFileRepository
from app.db.repositories.visibility_filter import SqlFragment

StackKind = Literal["compose", "swarm"]
StackOrigin = Literal["created", "adopted"]
StackStatus = Literal["active", "partial", "inactive", "error", "unknown"]
StackOperation = Literal["deploy", "redeploy", "start", "stop", "adopt", "remove"]
StackOutcome = Literal["success", "failure", "interrupted", "outcome_unknown"]

MAX_VARIABLES: Final = 1000
MAX_NAMES: Final = 5000
MAX_LOOKUP: Final = 500
TOKEN_LENGTH: Final = 12

_TOKEN_ALPHABET: Final = "0123456789abcdefghijklmnopqrstuvwxyz"
_ASCII_LOWER: Final = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_SWARM_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")
_SWARM_NAME_CHECK: Final = "CHECK constraint failed: stack swarm name"

_VALUE = EncryptedStr(STACK_VARIABLE_VALUE)
_TABLE = "stack_variable"
_SUMMARY_COLUMNS = (
    "s.id AS id, s.environment_id AS environment_id, s.name AS name, s.kind AS kind, "
    "s.origin AS origin, s.project_directory AS project_directory, "
    "s.compose_file_digest AS compose_file_digest, "
    "s.deployed_file_digest AS deployed_file_digest, s.resolved_digest AS resolved_digest, "
    "s.status AS status, s.last_operation AS last_operation, s.last_outcome AS last_outcome, "
    "s.last_operation_at AS last_operation_at, s.last_deployed_at AS last_deployed_at, "
    "s.inputs_digest AS inputs_digest, s.deployed_inputs_digest AS deployed_inputs_digest, "
    "s.created_by_user_id AS created_by_user_id, s.created_at AS created_at, "
    "s.updated_at AS updated_at"
)
_COLUMNS = (
    "id, environment_id, name, kind, origin, project_directory, compose_file, "
    "compose_file_digest, deployed_file_digest, resolved_digest, status, last_operation, "
    "last_outcome, last_operation_at, last_deployed_at, inputs_digest, deployed_inputs_digest, "
    "created_by_user_id, created_at, updated_at"
)
_SELECT = f"SELECT {_COLUMNS} FROM stack"


class StackVariableError(RepositoryError):
    """Raised when a variable write cannot be carried out, for example a secret with no value."""


class StackNameError(RepositoryError):
    """Raised when a Swarm stack name is outside the grammar the schema enforces."""


@dataclass(frozen=True, slots=True)
class StoredStack:
    """A stack as stored, with the Compose file it deploys."""

    id: int
    environment_id: int
    name: str
    kind: StackKind
    origin: StackOrigin
    project_directory: str
    compose_file: str = field(repr=False)
    compose_file_digest: str
    deployed_file_digest: str | None
    resolved_digest: str | None
    status: StackStatus
    last_operation: StackOperation | None
    last_outcome: StackOutcome | None
    last_operation_at: str | None
    last_deployed_at: str | None
    inputs_digest: str | None
    deployed_inputs_digest: str | None
    created_by_user_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StackSummary:
    """A stack as listed: every column but the file, which can be 128 KiB."""

    id: int
    environment_id: int
    name: str
    kind: StackKind
    origin: StackOrigin
    project_directory: str
    compose_file_digest: str
    deployed_file_digest: str | None
    resolved_digest: str | None
    status: StackStatus
    last_operation: StackOperation | None
    last_outcome: StackOutcome | None
    last_operation_at: str | None
    last_deployed_at: str | None
    inputs_digest: str | None
    deployed_inputs_digest: str | None
    created_by_user_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StoredVariable:
    """A variable as stored; a secret's value is never carried, only the fact that it is set."""

    id: int
    name: str
    is_secret: bool
    value: str | None
    value_token: str | None = None


@dataclass(frozen=True, slots=True)
class OpenedVariable:
    """One variable with a secret's value opened; the value is kept out of its repr."""

    id: int
    name: str
    is_secret: bool
    value_token: str | None
    value: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class VariableWrite:
    """One variable of the complete set a stack should hold.

    A secret with value None keeps the value already stored for that name.
    """

    name: str
    is_secret: bool
    value: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class VariableChanges:
    """What a replacement changed; a secret given a new value counts as changed."""

    changed: int
    removed: int


@dataclass(frozen=True, slots=True)
class DeployedState:
    """What a deploy leaves behind: the file digest it ran and the digest of its resolved model."""

    file_digest: str
    resolved_digest: str


def inputs_digest_of(
    variables: Iterable[tuple[str, bool, str | None]],
    files: Iterable[tuple[str, bool, str | None]],
    contexts: Iterable[tuple[str, str]] = (),
) -> str:
    """Return the digest of a stack's variables, companion files and build contexts.

    Each entry is (name, is_secret, part): a plain entry's part is its value, or a file's content
    digest; a secret entry's part is its token. No secret value is hashed. A context is
    (name, content digest) and enters the digest only when the stack has one.
    """

    def entries(items: Iterable[tuple[str, bool, str | None]]) -> list[list[str]]:
        return [
            [name, "secret" if is_secret else "plain", part or ""]
            for name, is_secret, part in sorted(items, key=lambda item: item[0])
        ]

    document: dict[str, list[list[str]]] = {
        "files": entries(files),
        "variables": entries(variables),
    }
    if built := sorted([name.casefold(), digest] for name, digest in contexts):
        document["contexts"] = built
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_EMPTY_INPUTS_DIGEST: Final = inputs_digest_of((), ())
_MAX_FILES_READ: Final = 100


def _new_token() -> str:
    return "".join(choice(_TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _common(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "environment_id": int(row["environment_id"]),
        "name": str(row["name"]),
        "kind": cast(StackKind, row["kind"]),
        "origin": cast(StackOrigin, row["origin"]),
        "project_directory": str(row["project_directory"]),
        "compose_file_digest": str(row["compose_file_digest"]),
        "deployed_file_digest": row["deployed_file_digest"],
        "resolved_digest": row["resolved_digest"],
        "status": cast(StackStatus, row["status"]),
        "last_operation": cast(StackOperation | None, row["last_operation"]),
        "last_outcome": cast(StackOutcome | None, row["last_outcome"]),
        "last_operation_at": row["last_operation_at"],
        "last_deployed_at": row["last_deployed_at"],
        "inputs_digest": row["inputs_digest"],
        "deployed_inputs_digest": row["deployed_inputs_digest"],
        "created_by_user_id": row["created_by_user_id"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


@contextmanager
def _swarm_name_check() -> Iterator[None]:
    try:
        yield
    except sqlite3.IntegrityError as exc:
        if str(exc).startswith(_SWARM_NAME_CHECK):
            raise StackNameError("a Swarm stack name is outside the allowed grammar") from exc
        raise


class StackRepository(SecretRepository[StoredStack]):
    """Reads and writes stacks and their variables; the only code that seals or opens a secret."""

    _secret_columns = (_VALUE,)

    def _map_row(self, row: sqlite3.Row) -> StoredStack:
        return StoredStack(compose_file=str(row["compose_file"]), **_common(row))

    @property
    def files(self) -> StackFileRepository:
        """Return the companion file repository on this session; its writes refresh the digest."""
        return StackFileRepository(self._session, self._vault, self._clock, stacks=self)

    @property
    def contexts(self) -> StackContextRepository:
        """Return the build context repository on this session; its writes refresh the digest."""
        return StackContextRepository(self._session, self._clock, stacks=self)

    async def get(self, environment_id: int, name: str) -> StoredStack | None:
        """Fetch a stack by its exact name within an environment."""
        return await self._one(
            f"{_SELECT} WHERE environment_id = ? AND name = ?", (environment_id, name)
        )

    async def get_folded(self, environment_id: int, name: str) -> StoredStack | None:
        """Fetch the stack whose name equals `name` ignoring ASCII case, of either kind."""
        return await self._one(
            f"{_SELECT} WHERE environment_id = ? AND lower(name) = ?",
            (environment_id, name.translate(_ASCII_LOWER)),
        )

    async def get_by_id(self, stack_id: int) -> StoredStack | None:
        """Fetch a stack by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (stack_id,))

    async def names(self, environment_id: int, *, kind: StackKind | None = None) -> list[str]:
        """List the stack names of an environment, of one kind if given, at most MAX_NAMES."""
        rows = await self._session.fetch_limited(
            "SELECT name FROM stack WHERE environment_id = ? AND (? IS NULL OR kind = ?) "
            "ORDER BY name, id LIMIT ?",
            (environment_id, kind, kind, MAX_NAMES),
            limit=MAX_NAMES,
        )
        return [str(row["name"]) for row in rows]

    async def insert(
        self,
        *,
        environment_id: int,
        name: str,
        kind: StackKind,
        origin: StackOrigin,
        project_directory: str,
        compose_file: str,
        compose_file_digest: str,
        created_by_user_id: int | None,
    ) -> int:
        """Insert a stack with status unknown and return its id.

        DuplicateError if the name, ignoring case, or the project directory is taken,
        MissingReferenceError if the environment or user is gone, StackNameError for a Swarm name
        outside [A-Za-z0-9][A-Za-z0-9_-]{0,62}.
        """
        if kind == "swarm" and _SWARM_NAME.fullmatch(name) is None:
            raise StackNameError("a Swarm stack name is outside the allowed grammar")
        now = self._now()
        with _swarm_name_check(), constraints_as_domain_errors("stack"):
            result = await self._execute(
                "INSERT INTO stack (environment_id, name, kind, origin, project_directory, "
                "compose_file, compose_file_digest, inputs_digest, created_by_user_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    environment_id,
                    name,
                    kind,
                    origin,
                    project_directory,
                    compose_file,
                    compose_file_digest,
                    _EMPTY_INPUTS_DIGEST,
                    created_by_user_id,
                    now,
                    now,
                ),
            )
        if result.last_row_id is None:
            raise DatabaseError("an insert did not report a row id")
        return result.last_row_id

    async def update_file(
        self, stack_id: int, *, compose_file: str, compose_file_digest: str
    ) -> bool:
        """Replace the stored file; False if the stack does not exist."""
        result = await self._execute(
            "UPDATE stack SET compose_file = ?, compose_file_digest = ?, updated_at = ? "
            "WHERE id = ?",
            (compose_file, compose_file_digest, self._now(), stack_id),
        )
        return result.rowcount > 0

    async def record_operation(
        self,
        stack_id: int,
        *,
        operation: StackOperation,
        outcome: StackOutcome,
        status: StackStatus | None = None,
        deployed: DeployedState | None = None,
    ) -> bool:
        """Record the latest operation; False if the stack does not exist.

        A status left as None and a deployed state left as None keep what is stored.
        last_deployed_at moves, and deployed_inputs_digest takes the current inputs_digest, only
        when a deployed state is given, so variable and file writes come first in the transaction.
        """
        now = self._now()
        result = await self._execute(
            "UPDATE stack SET last_operation = ?, last_outcome = ?, last_operation_at = ?, "
            "status = COALESCE(?, status), "
            "deployed_file_digest = COALESCE(?, deployed_file_digest), "
            "resolved_digest = COALESCE(?, resolved_digest), "
            "last_deployed_at = CASE WHEN ? IS NULL THEN last_deployed_at ELSE ? END, "
            "deployed_inputs_digest = "
            "CASE WHEN ? IS NULL THEN deployed_inputs_digest ELSE inputs_digest END, "
            "updated_at = ? WHERE id = ?",
            (
                operation,
                outcome,
                now,
                status,
                None if deployed is None else deployed.file_digest,
                None if deployed is None else deployed.resolved_digest,
                None if deployed is None else now,
                None if deployed is None else now,
                None if deployed is None else now,
                now,
                stack_id,
            ),
        )
        return result.rowcount > 0

    async def record_running_state(
        self, stack_id: int, *, file_digest: str, resolved_digest: str
    ) -> bool:
        """Record the stored file, variables and files as what already runs; False if no stack.

        The deployed digests take the given file digest and the current inputs digest, so the
        stack reads unchanged. No deploy time is set, because nothing was deployed.
        """
        result = await self._execute(
            "UPDATE stack SET deployed_file_digest = ?, resolved_digest = ?, "
            "deployed_inputs_digest = inputs_digest, updated_at = ? WHERE id = ?",
            (file_digest, resolved_digest, self._now(), stack_id),
        )
        return result.rowcount > 0

    async def delete(self, stack_id: int) -> bool:
        """Delete a stack and, by cascade, its variables; False if it does not exist."""
        result = await self._execute("DELETE FROM stack WHERE id = ?", (stack_id,))
        return result.rowcount > 0

    async def variables(self, stack_id: int) -> list[StoredVariable]:
        """List a stack's variables by name; secrets are present by name only."""
        rows = await self._session.fetch_limited(
            "SELECT id, name, is_secret, value, value_token FROM stack_variable "
            "WHERE stack_id = ? ORDER BY name LIMIT ?",
            (stack_id, MAX_VARIABLES),
            limit=MAX_VARIABLES,
        )
        return [
            StoredVariable(
                id=int(row["id"]),
                name=str(row["name"]),
                is_secret=bool(row["is_secret"]),
                value=row["value"],
                value_token=row["value_token"],
            )
            for row in rows
        ]

    async def variable_tokens(self, stack_id: int) -> dict[str, str]:
        """Map each secret variable's name to the token that changes whenever its value is written.

        A secret stored before tokens existed has none and is absent.
        """
        rows = await self._session.fetch_limited(
            "SELECT name, value_token FROM stack_variable "
            "WHERE stack_id = ? AND is_secret = 1 AND value_token IS NOT NULL "
            "ORDER BY name LIMIT ?",
            (stack_id, MAX_VARIABLES),
            limit=MAX_VARIABLES,
        )
        return {str(row["name"]): str(row["value_token"]) for row in rows}

    async def reveal_secrets(self, stack_id: int) -> dict[str, str]:
        """Decrypt every secret of a stack, by variable name.

        SecretUnreadableError if any value cannot be decrypted with the keys this process holds.
        """
        rows = await self._session.fetch_limited(
            "SELECT id, name, value_ciphertext FROM stack_variable "
            "WHERE stack_id = ? AND is_secret = 1 ORDER BY name LIMIT ?",
            (stack_id, MAX_VARIABLES),
            limit=MAX_VARIABLES,
        )
        revealed: dict[str, str] = {}
        for row in rows:
            try:
                value = self._reveal(_VALUE, row)
            except DecryptionError:
                raise SecretUnreadableError("a stored variable cannot be read") from None
            if value is None:
                raise SecretUnreadableError("a stored variable cannot be read")
            revealed[str(row["name"])] = value
        return revealed

    async def reveal_variable(self, stack_id: int, variable_id: int) -> OpenedVariable | None:
        """Return one variable of the stack with a secret's value decrypted; None if it is gone.

        SecretUnreadableError if the value cannot be decrypted with the keys this process holds.
        """
        row = await self._session.fetch_one(
            "SELECT id, name, is_secret, value, value_ciphertext, value_token "
            "FROM stack_variable WHERE stack_id = ? AND id = ?",
            (stack_id, variable_id),
        )
        if row is None:
            return None
        if not row["is_secret"]:
            return OpenedVariable(
                id=int(row["id"]),
                name=str(row["name"]),
                is_secret=False,
                value_token=row["value_token"],
                value=row["value"],
            )
        try:
            value = await asyncio.to_thread(self._reveal, _VALUE, row)
        except DecryptionError:
            raise SecretUnreadableError("a stored variable cannot be read") from None
        if value is None:
            raise SecretUnreadableError("a stored variable cannot be read")
        return OpenedVariable(
            id=int(row["id"]),
            name=str(row["name"]),
            is_secret=True,
            value_token=row["value_token"],
            value=value,
        )

    async def shape_secrets(
        self, stack_id: int, shape: Callable[[str], str], *, only: int | None = None
    ) -> dict[int, str | None]:
        """Map each secret's id to `shape` of its decrypted value, or None if it cannot be read.

        Values are decrypted off the event loop and only the shaped text leaves this method.
        `only` narrows the result to one variable.
        """
        rows = await self._session.fetch_limited(
            "SELECT id, value_ciphertext FROM stack_variable "
            "WHERE stack_id = ? AND is_secret = 1 AND (? IS NULL OR id = ?) "
            "ORDER BY name LIMIT ?",
            (stack_id, only, only, MAX_VARIABLES),
            limit=MAX_VARIABLES,
        )

        def work() -> dict[int, str | None]:
            shaped: dict[int, str | None] = {}
            for row in rows:
                try:
                    value = self._reveal(_VALUE, row)
                except DecryptionError:
                    value = None
                shaped[int(row["id"])] = None if value is None else shape(value)
            return shaped

        return await asyncio.to_thread(work)

    async def replace_variables(
        self, stack_id: int, inputs: Sequence[VariableWrite], now: datetime | None = None
    ) -> VariableChanges:
        """Make the stack hold exactly `inputs`, all-or-nothing.

        Names absent from `inputs` are removed. A secret with no value keeps its stored value, and
        StackVariableError is raised if it has none or a plain value needs one.
        inputs_digest is recomputed in the same transaction; updated_at moves, to `now` or the
        clock, only when something changed.
        MissingReferenceError if the stack does not exist.
        """
        names = [item.name for item in inputs]
        kept = set(names)
        if len(kept) != len(names):
            raise ValueError("variable names must be unique")
        if len(names) > MAX_VARIABLES:
            raise ValueError(f"a stack holds at most {MAX_VARIABLES} variables")
        stamp = self._now() if now is None else format_timestamp(now)
        with constraints_as_domain_errors("stack variable"):
            async with self._atomic():
                if (
                    await self._session.fetch_one("SELECT 1 FROM stack WHERE id = ?", (stack_id,))
                    is None
                ):
                    raise MissingReferenceError(
                        "stack variable refers to a row that does not exist"
                    )
                rows = await self._session.fetch_limited(
                    "SELECT id, name, is_secret, value FROM stack_variable WHERE stack_id = ? "
                    "LIMIT ?",
                    (stack_id, MAX_VARIABLES + 1),
                    limit=MAX_VARIABLES + 1,
                )
                stored = {str(row["name"]): row for row in rows}
                changed = 0
                for item in inputs:
                    changed += await self._write_one(stack_id, stored.get(item.name), item, stamp)
                removed = [row for name, row in stored.items() if name not in kept]
                for row in removed:
                    await self._execute(
                        "DELETE FROM stack_variable WHERE id = ?", (int(row["id"]),)
                    )
                await self.refresh_inputs_digest(
                    stack_id, touched_at=stamp if changed or removed else None
                )
        return VariableChanges(changed=changed, removed=len(removed))

    async def refresh_inputs_digest(self, stack_id: int, *, touched_at: str | None = None) -> str:
        """Recompute and store a stack's inputs_digest from its variables, files and contexts.

        Run inside the transaction that wrote either, after the writes. `touched_at` also moves
        updated_at. Returns the digest.
        """
        variables = await self._session.fetch_limited(
            "SELECT name, is_secret, value, value_token FROM stack_variable WHERE stack_id = ? "
            "LIMIT ?",
            (stack_id, MAX_VARIABLES),
            limit=MAX_VARIABLES,
        )
        files = await self._session.fetch_limited(
            "SELECT name, is_secret, content_digest, value_token FROM stack_file "
            "WHERE stack_id = ? LIMIT ?",
            (stack_id, _MAX_FILES_READ),
            limit=_MAX_FILES_READ,
        )
        contexts = await self._session.fetch_limited(
            "SELECT name, content_digest FROM stack_build_context WHERE stack_id = ? LIMIT ?",
            (stack_id, MAX_CONTEXTS),
            limit=MAX_CONTEXTS,
        )
        digest = inputs_digest_of(
            (
                (
                    str(row["name"]),
                    bool(row["is_secret"]),
                    row["value_token"] if row["is_secret"] else row["value"],
                )
                for row in variables
            ),
            (
                (
                    str(row["name"]),
                    bool(row["is_secret"]),
                    row["value_token"] if row["is_secret"] else row["content_digest"],
                )
                for row in files
            ),
            ((str(row["name"]), str(row["content_digest"])) for row in contexts),
        )
        await self._execute(
            "UPDATE stack SET inputs_digest = ?, updated_at = COALESCE(?, updated_at) WHERE id = ?",
            (digest, touched_at, stack_id),
        )
        return digest

    async def _write_one(
        self, stack_id: int, existing: sqlite3.Row | None, item: VariableWrite, stamp: str
    ) -> int:
        """Apply one variable; return 1 if it changed, else 0."""
        if item.is_secret:
            if item.value is None:
                if existing is not None and bool(existing["is_secret"]):
                    return 0
                raise StackVariableError(f"secret variable {item.name} needs a value")
            secrets: dict[EncryptedStr, str | None] = {_VALUE: item.value}
            plain: dict[str, Any] = {"is_secret": 1, "value": None, "value_token": _new_token()}
        else:
            if item.value is None:
                raise StackVariableError(f"variable {item.name} needs a value")
            if (
                existing is not None
                and not bool(existing["is_secret"])
                and existing["value"] == item.value
            ):
                return 0
            secrets = {} if existing is None else {_VALUE: None}
            plain = {"is_secret": 0, "value": item.value, "value_token": _new_token()}
        if existing is None:
            await self._insert_with_secrets(
                _TABLE,
                {
                    "stack_id": stack_id,
                    "name": item.name,
                    **plain,
                    "created_at": stamp,
                    "updated_at": stamp,
                },
                secrets,
            )
        else:
            await self._update_with_secrets(
                _TABLE, int(existing["id"]), {**plain, "updated_at": stamp}, secrets
            )
        return 1


class StackListRepository(Repository[StackSummary]):
    """The keyset-paged stack list, without the files."""

    def _map_row(self, row: sqlite3.Row) -> StackSummary:
        return StackSummary(**_common(row))

    async def exists_folded(self, environment_id: int, name: str) -> bool:
        """Report whether a stack of either kind has this name, ignoring ASCII case."""
        row = await self._session.fetch_one(
            "SELECT 1 AS found FROM stack WHERE environment_id = ? AND lower(name) = ?",
            (environment_id, name.translate(_ASCII_LOWER)),
        )
        return row is not None

    async def kinds_folded(
        self, environment_id: int, names: Iterable[str], visible: SqlFragment | None = None
    ) -> dict[str, tuple[str, StackKind]]:
        """Map each given name that a stack has, ignoring ASCII case, to its name and kind.

        `visible` is built over s.name and s.environment_id. At most MAX_LOOKUP names are looked up.
        """
        given = sorted(set(names))[:MAX_LOOKUP]
        folded = sorted({name.translate(_ASCII_LOWER) for name in given})
        if not folded:
            return {}
        conditions = [
            "s.environment_id = ?",
            f"lower(s.name) IN ({', '.join('?' for _ in folded)})",
        ]
        params: list[Any] = [environment_id, *folded]
        if visible is not None:
            conditions.append(visible.clause)
            params.extend(visible.params)
        rows = await self._session.fetch_limited(
            f"SELECT s.name, s.kind FROM stack s WHERE {' AND '.join(conditions)}",
            tuple(params),
            limit=MAX_LOOKUP,
        )
        found = {
            str(row["name"]).translate(_ASCII_LOWER): (
                str(row["name"]),
                cast(StackKind, row["kind"]),
            )
            for row in rows
        }
        return {
            name: found[name.translate(_ASCII_LOWER)]
            for name in given
            if name.translate(_ASCII_LOWER) in found
        }

    async def page(
        self,
        environment_id: int,
        *,
        limit: int,
        cursor: str | None = None,
        name_contains: str | None = None,
        visible: SqlFragment | None = None,
        kind: StackKind | None = None,
        name_prefix: str | None = None,
        descending: bool = False,
    ) -> Page[StackSummary]:
        """Return one page ordered by name, from the last name down when `descending`.

        `visible` restricts the page to stacks a user may reach; it is built over s.name and
        s.environment_id. `kind` restricts it to one kind; None lists both.
        """
        conditions = ["s.environment_id = ?"]
        params: list[Any] = [environment_id]
        if kind is not None:
            conditions.append("s.kind = ?")
            params.append(kind)
        if name_contains:
            conditions.append("s.name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(name_contains)}%")
        if name_prefix:
            conditions.append("s.name LIKE ? ESCAPE '\\'")
            params.append(like_prefix(name_prefix))
        if visible is not None:
            conditions.append(visible.clause)
            params.extend(visible.params)
        return await self._page(
            f"SELECT {_SUMMARY_COLUMNS} FROM stack s",
            limit=limit,
            cursor=cursor,
            sort_column="s.name",
            descending=descending,
            id_column="s.id",
            where=" AND ".join(conditions),
            params=tuple(params),
        )
