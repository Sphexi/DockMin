"""Repository for registry credential sets: the only writer of registry_credential."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from pydantic import SecretStr

from app.crypto.envelope import DecryptionError
from app.crypto.registry import REGISTRY_CREDENTIAL_DERIVED_SECRET, REGISTRY_CREDENTIAL_SECRET
from app.db.repositories.base import Repository
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.encrypted import EncryptedStr, SecretRepository
from app.db.repositories.errors import (
    FOREIGN_KEY_PREFIX,
    RepositoryError,
    constraints_as_domain_errors,
)
from app.db.repositories.pagination import Page
from app.registry_client.addresses import canonical_address
from app.registry_client.errors import AddressInvalid
from app.registry_client.providers import (
    PROVIDERS,
    ProviderExtras,
    ProviderKind,
    SecretKind,
    check_address_for_provider,
    parse_extras,
)

_SECRET = EncryptedStr(REGISTRY_CREDENTIAL_SECRET)
_DERIVED = EncryptedStr(REGISTRY_CREDENTIAL_DERIVED_SECRET)
_TABLE = "registry_credential"
_COLUMNS = (
    "id, name, provider, url, authentication_enabled, username, secret_kind, secret_ciphertext, "
    "derived_secret_ciphertext, derived_secret_expires_at, derived_secret_refreshed_at, "
    "provider_extras, verify_tls, ca_bundle, is_default_for_host, created_at, updated_at"
)
_SELECT = f"SELECT {_COLUMNS} FROM registry_credential"
_FOLD_SCAN_PAGE = 500
MAX_ADDRESS_MATCHES = 50


class StillReferencedError(RepositoryError):
    """Raised when deleting a credential set that an image binding still references."""


class SecretUnreadableError(RepositoryError):
    """Raised when a stored secret cannot be decrypted with the keys this process holds."""


@dataclass(frozen=True, slots=True)
class StoredRegistryCredential:
    """A credential set as stored; the secrets are present only as ciphertext."""

    id: int
    name: str
    provider: ProviderKind
    url: str
    authentication_enabled: bool
    username: str | None = field(repr=False)
    secret_kind: SecretKind
    secret_ciphertext: str | None = field(repr=False)
    derived_secret_ciphertext: str | None = field(repr=False)
    derived_secret_expires_at: str | None
    derived_secret_refreshed_at: str | None
    extras: ProviderExtras
    extras_json: str = field(repr=False)
    verify_tls: bool
    ca_bundle: str | None = field(repr=False)
    is_default_for_host: bool
    created_at: str
    updated_at: str

    @property
    def has_secret(self) -> bool:
        """Report whether a long-lived secret is stored."""
        return self.secret_ciphertext is not None


@dataclass(frozen=True, slots=True)
class RevealedSecrets:
    """Decrypted secrets for one use; neither value appears in a repr."""

    secret: SecretStr | None = field(repr=False)
    derived_secret: SecretStr | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class CredentialFields:
    """The columns an operator edits, as written; extras are validated against the provider."""

    name: str
    provider: str
    url: str
    authentication_enabled: bool
    username: str | None = field(repr=False)
    extras: Mapping[str, object]
    verify_tls: bool = True
    ca_bundle: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AddressMatch:
    """Another set at the same address, for duplicate detection."""

    id: int
    name: str
    authentication_enabled: bool
    username: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class RegistryListEntry:
    """One row of the list page."""

    id: int
    name: str
    provider: ProviderKind
    url: str
    authentication_enabled: bool
    verify_tls: bool
    secret_kind: SecretKind
    environment_count: int
    grantee_count: int
    is_default_for_host: bool


def _extras_json(extras: Mapping[str, object]) -> str:
    return json.dumps(dict(extras), sort_keys=True, separators=(",", ":"))


def _provider_of(name: str) -> ProviderKind:
    if name not in PROVIDERS:
        raise ValueError("unknown registry provider")
    return cast(ProviderKind, name)


def _prepared(fields: CredentialFields) -> tuple[dict[str, Any], ProviderKind]:
    """Validate against the provider again and build the plain column values."""
    provider = _provider_of(fields.provider)
    model = parse_extras(provider, fields.extras)
    if canonical_address(fields.url) != fields.url:
        raise AddressInvalid("The address is not in its canonical form.")
    check_address_for_provider(provider, fields.url, model)
    if not PROVIDERS[provider].tls_options_allowed and (not fields.verify_tls or fields.ca_bundle):
        raise ValueError("this provider takes no TLS options")
    values: dict[str, Any] = {
        "name": fields.name,
        "provider": provider,
        "url": fields.url,
        "authentication_enabled": int(fields.authentication_enabled),
        "username": fields.username,
        "secret_kind": PROVIDERS[provider].secret_kind,
        "provider_extras": _extras_json(model.model_dump()),
        "verify_tls": int(fields.verify_tls),
        "ca_bundle": fields.ca_bundle,
    }
    return values, provider


class RegistryCredentialRepository(SecretRepository[StoredRegistryCredential]):
    """Reads and writes credential sets; the only code that seals or opens their secrets."""

    _secret_columns = (_SECRET, _DERIVED)

    def _map_row(self, row: sqlite3.Row) -> StoredRegistryCredential:
        provider = _provider_of(str(row["provider"]))
        extras_json = str(row["provider_extras"])
        return StoredRegistryCredential(
            id=int(row["id"]),
            name=str(row["name"]),
            provider=provider,
            url=str(row["url"]),
            authentication_enabled=bool(row["authentication_enabled"]),
            username=row["username"],
            secret_kind=cast(SecretKind, row["secret_kind"]),
            secret_ciphertext=row["secret_ciphertext"],
            derived_secret_ciphertext=row["derived_secret_ciphertext"],
            derived_secret_expires_at=row["derived_secret_expires_at"],
            derived_secret_refreshed_at=row["derived_secret_refreshed_at"],
            extras=parse_extras(provider, json.loads(extras_json)),
            extras_json=extras_json,
            verify_tls=bool(row["verify_tls"]),
            ca_bundle=row["ca_bundle"],
            is_default_for_host=bool(row["is_default_for_host"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    async def get(self, credential_id: int) -> StoredRegistryCredential | None:
        """Fetch a set by id; its secrets stay encrypted."""
        return await self._one(f"{_SELECT} WHERE id = ?", (credential_id,))

    async def fetch_revealed(
        self, credential_id: int
    ) -> tuple[StoredRegistryCredential, RevealedSecrets] | None:
        """Fetch a set with its secrets decrypted from the same row read.

        SecretUnreadableError if the long-lived secret cannot be decrypted. A cached derived
        secret that cannot be decrypted is reported as absent, since the next refresh replaces it.
        """
        row = await self._session.fetch_one(f"{_SELECT} WHERE id = ?", (credential_id,))
        if row is None:
            return None
        stored = self._map_row(row)
        secret: str | None = None
        derived: str | None = None
        secret_unreadable = False
        try:
            secret = self._reveal(_SECRET, row)
        except DecryptionError:
            secret_unreadable = True
        if secret_unreadable:
            raise SecretUnreadableError("the stored secret cannot be read")
        try:
            derived = self._reveal(_DERIVED, row)
        except DecryptionError:
            derived = None
        return stored, RevealedSecrets(
            None if secret is None else SecretStr(secret),
            None if derived is None else SecretStr(derived),
        )

    async def find_by_folded_name(
        self, name: str, *, excluding: int | None = None
    ) -> StoredRegistryCredential | None:
        """Find a set whose name equals this one under full Unicode case folding.

        The column folds ASCII only. Callers run this inside the write transaction.
        """
        folded = name.casefold()
        after_id = 0
        while True:
            rows = await self._session.fetch_limited(
                "SELECT id, name FROM registry_credential WHERE id > ? ORDER BY id LIMIT ?",
                (after_id, _FOLD_SCAN_PAGE),
                limit=_FOLD_SCAN_PAGE,
            )
            if not rows:
                return None
            for row in rows:
                if int(row["id"]) != excluding and str(row["name"]).casefold() == folded:
                    return await self.get(int(row["id"]))
            after_id = int(rows[-1]["id"])

    async def find_at_address(self, url: str, *, excluding: int | None) -> list[AddressMatch]:
        """List other sets at the same canonical address, by name."""
        rows = await self._session.fetch_limited(
            "SELECT id, name, authentication_enabled, username FROM registry_credential "
            "WHERE url = ? AND id <> ? ORDER BY name, id LIMIT ?",
            (url, -1 if excluding is None else excluding, MAX_ADDRESS_MATCHES),
            limit=MAX_ADDRESS_MATCHES,
        )
        return [
            AddressMatch(
                id=int(row["id"]),
                name=str(row["name"]),
                authentication_enabled=bool(row["authentication_enabled"]),
                username=row["username"],
            )
            for row in rows
        ]

    async def count_bound_images(self, credential_id: int) -> int:
        """Count image bindings that reference the set."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM image_credential_binding "
            "WHERE registry_credential_id = ?",
            (credential_id,),
        )
        assert row is not None
        return int(row["total"])

    async def create(
        self, fields: CredentialFields, secret: SecretStr | None
    ) -> StoredRegistryCredential:
        """Insert a set and seal its secret against the id it receives.

        DuplicateError if the name exists. ExtrasInvalid if the extras are refused for the
        provider, including any key that names a secret.
        """
        values, _ = _prepared(fields)
        now = self._now()
        values["created_at"] = now
        values["updated_at"] = now
        with constraints_as_domain_errors("registry credential"):
            credential_id = await self._insert_with_secrets(
                _TABLE, values, {_SECRET: None if secret is None else secret.get_secret_value()}
            )
        return self._required(await self.get(credential_id))

    async def update_keeping_secret(self, credential_id: int, fields: CredentialFields) -> bool:
        """Change a set and clear its cached derived secret in one statement; False if gone."""
        values, _ = _prepared(fields)
        values.update(self._cleared_derived())
        values["updated_at"] = self._now()
        with constraints_as_domain_errors("registry credential"):
            return await self._update_with_secrets(_TABLE, credential_id, values, {_DERIVED: None})

    async def update_replacing_secret(
        self, credential_id: int, fields: CredentialFields, secret: SecretStr | None
    ) -> bool:
        """Change a set and replace its secret, or clear it with None, in one statement."""
        values, _ = _prepared(fields)
        values.update(self._cleared_derived())
        values["updated_at"] = self._now()
        with constraints_as_domain_errors("registry credential"):
            return await self._update_with_secrets(
                _TABLE,
                credential_id,
                values,
                {
                    _SECRET: None if secret is None else secret.get_secret_value(),
                    _DERIVED: None,
                },
            )

    async def store_derived(
        self,
        seen: StoredRegistryCredential,
        token: SecretStr,
        *,
        expires_at: datetime,
        refreshed_at: datetime,
    ) -> bool:
        """Cache a derived secret if the set is still as `seen`; False if it changed meanwhile.

        Compared on every input the exchange reads: provider, address, user name, extras and the
        sealed secret, which a re-key or a new secret changes. A refresh that raced an edit of any
        of these is discarded.
        """
        return await self._update_with_secrets(
            _TABLE,
            seen.id,
            {
                "derived_secret_expires_at": format_timestamp(expires_at),
                "derived_secret_refreshed_at": format_timestamp(refreshed_at),
            },
            {_DERIVED: token.get_secret_value()},
            expect={
                "provider": seen.provider,
                "url": seen.url,
                "username": seen.username,
                "provider_extras": seen.extras_json,
                "secret_ciphertext": seen.secret_ciphertext,
            },
        )

    async def clear_default_at(self, url: str, *, excluding: int | None = None) -> int:
        """Clear the default-for-host flag on every set at the address but the excluded one."""
        result = await self._execute(
            "UPDATE registry_credential SET is_default_for_host = 0 "
            "WHERE url = ? AND is_default_for_host = 1 AND id <> ?",
            (url, -1 if excluding is None else excluding),
        )
        return result.rowcount

    async def set_default_for_host(self, credential_id: int, *, enabled: bool) -> bool:
        """Set or clear the default-for-host flag of a set, leaving its other columns alone.

        False if the set is gone. DuplicateError if another default holds the address, which the
        caller prevents by clearing it first inside the same transaction.
        """
        with constraints_as_domain_errors("registry credential default"):
            result = await self._execute(
                "UPDATE registry_credential SET is_default_for_host = ? WHERE id = ?",
                (int(enabled), credential_id),
            )
        return result.rowcount > 0

    async def delete(self, credential_id: int) -> bool:
        """Delete a set; False if absent, StillReferencedError if an image binding uses it."""
        in_use = False
        try:
            result = await self._execute(
                "DELETE FROM registry_credential WHERE id = ?", (credential_id,)
            )
        except sqlite3.IntegrityError as exc:
            if not str(exc).startswith(FOREIGN_KEY_PREFIX):
                raise
            in_use = True
        if in_use:
            raise StillReferencedError("an image binding still uses this credential set")
        return result.rowcount > 0

    @staticmethod
    def _cleared_derived() -> dict[str, None]:
        return {"derived_secret_expires_at": None, "derived_secret_refreshed_at": None}


class RegistryListRepository(Repository[RegistryListEntry]):
    """The keyset-paged list of credential sets."""

    def _map_row(self, row: sqlite3.Row) -> RegistryListEntry:
        return RegistryListEntry(
            id=int(row["id"]),
            name=str(row["name"]),
            provider=_provider_of(str(row["provider"])),
            url=str(row["url"]),
            authentication_enabled=bool(row["authentication_enabled"]),
            verify_tls=bool(row["verify_tls"]),
            secret_kind=cast(SecretKind, row["secret_kind"]),
            environment_count=int(row["environment_count"]),
            grantee_count=int(row["grantee_count"]),
            is_default_for_host=bool(row["is_default_for_host"]),
        )

    async def count(self) -> int:
        """Count every credential set."""
        row = await self._session.fetch_one("SELECT COUNT(*) AS total FROM registry_credential")
        return 0 if row is None else int(row["total"])

    async def exists(self, credential_id: int) -> bool:
        """Report whether a credential set with this id is stored."""
        row = await self._session.fetch_one(
            "SELECT 1 AS found FROM registry_credential WHERE id = ?", (credential_id,)
        )
        return row is not None

    async def page(self, *, limit: int, cursor: str | None = None) -> Page[RegistryListEntry]:
        """Return one page ordered by name, with how many environments and grantees each has."""
        return await self._page(
            "SELECT rc.id AS id, rc.name AS name, rc.provider AS provider, rc.url AS url, "
            "rc.authentication_enabled AS authentication_enabled, rc.verify_tls AS verify_tls, "
            "rc.secret_kind AS secret_kind, rc.is_default_for_host AS is_default_for_host, "
            "(SELECT count(*) FROM registry_environment_access rea "
            "WHERE rea.registry_credential_id = rc.id) AS environment_count, "
            "(SELECT count(*) FROM registry_credential_user_access rua "
            "WHERE rua.registry_credential_id = rc.id) "
            "+ (SELECT count(*) FROM registry_credential_team_access rta "
            "WHERE rta.registry_credential_id = rc.id) AS grantee_count "
            "FROM registry_credential rc",
            limit=limit,
            cursor=cursor,
            sort_column="rc.name",
            id_column="rc.id",
        )
