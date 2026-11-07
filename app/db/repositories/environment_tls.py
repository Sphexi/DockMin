"""Repository for the TLS material of remote environments; the client key is stored encrypted."""

import sqlite3
from dataclasses import dataclass, field

from app.crypto.registry import ENVIRONMENT_TLS_CLIENT_KEY
from app.db.repositories.encrypted import EncryptedStr, SecretRepository
from app.db.repositories.errors import constraints_as_domain_errors

_CLIENT_KEY = EncryptedStr(ENVIRONMENT_TLS_CLIENT_KEY)
_TABLE = "environment_tls_material"
_SELECT = (
    "SELECT id, environment_id, ca_certificate, client_certificate, client_key_ciphertext, "
    "updated_at FROM environment_tls_material"
)


@dataclass(frozen=True, slots=True)
class StoredTls:
    """The stored material as held; the key is its ciphertext, never its plaintext."""

    environment_id: int
    ca_certificate: str | None = field(repr=False)
    client_certificate: str | None = field(repr=False)
    client_key_ciphertext: str | None = field(repr=False)
    updated_at: str


class EnvironmentTlsRepository(SecretRepository[StoredTls]):
    """Reads and writes TLS material; the only path that decrypts the key is reveal_client_key."""

    _secret_columns = (_CLIENT_KEY,)

    def _map_row(self, row: sqlite3.Row) -> StoredTls:
        return StoredTls(
            environment_id=int(row["environment_id"]),
            ca_certificate=row["ca_certificate"],
            client_certificate=row["client_certificate"],
            client_key_ciphertext=row["client_key_ciphertext"],
            updated_at=str(row["updated_at"]),
        )

    async def get(self, environment_id: int) -> StoredTls | None:
        """Fetch the stored material of an environment, with the key still encrypted."""
        return await self._one(f"{_SELECT} WHERE environment_id = ?", (environment_id,))

    async def reveal_client_key(self, environment_id: int) -> str | None:
        """Decrypt and return the client key; only the connector calls this."""
        row = await self._session.fetch_one(
            f"{_SELECT} WHERE environment_id = ?", (environment_id,)
        )
        return None if row is None else self._reveal(_CLIENT_KEY, row)

    async def replace(
        self,
        environment_id: int,
        *,
        ca_certificate: str,
        client_certificate: str,
        client_key: str,
    ) -> None:
        """Store all three items, replacing any earlier set.

        MissingReferenceError if the environment is gone.
        """
        now = self._now()
        with constraints_as_domain_errors("TLS material"):
            async with self._atomic():
                await self._execute(
                    "DELETE FROM environment_tls_material WHERE environment_id = ?",
                    (environment_id,),
                )
                await self._insert_with_secrets(
                    _TABLE,
                    {
                        "environment_id": environment_id,
                        "ca_certificate": ca_certificate,
                        "client_certificate": client_certificate,
                        "created_at": now,
                        "updated_at": now,
                    },
                    {_CLIENT_KEY: client_key},
                )

    async def delete(self, environment_id: int) -> bool:
        """Delete an environment's material; False if it held none."""
        result = await self._execute(
            "DELETE FROM environment_tls_material WHERE environment_id = ?", (environment_id,)
        )
        return result.rowcount > 0
