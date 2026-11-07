"""Images for the binding and image tests: identifiers, decoded registry auth and stored rows."""

import asyncio
import base64
import json
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import SecretStr

from app.db.engine import Session
from app.db.repositories.image_binding import ImageBindingRepository, StoredBinding
from app.registry_client.manifests import ManifestCheckFailed, ManifestCheckKind
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.references import ImageReference
from tests.access_support import ENV_A
from tests.registry_support import (
    ADDRESS,
    SECRET,
    RegistryRig,
    grant_environment,
    grant_user,
    static_fields,
)

if TYPE_CHECKING:
    from tests.services.conftest import ImageRig

REFERENCE = f"{ADDRESS}/app:1"
REFERENCE_KEY = REFERENCE
OTHER_REFERENCE = f"{ADDRESS}/app:2"
HUB_REFERENCE = "busybox:1"
HUB_KEY = "docker.io/library/busybox:1"


def image_id(number: int) -> str:
    """Return the bare 64-hex id of the numbered image; the number leads, as in a real digest."""
    return f"{number:04x}".ljust(64, "0")


def full_id(number: int) -> str:
    """Return the id the Engine prints, with its `sha256:` prefix."""
    return f"sha256:{image_id(number)}"


def digest(number: int) -> str:
    """Return a well-formed content digest."""
    return f"sha256:{number:064x}"


def decode_auth(header: str) -> dict[str, str]:
    """Decode an `X-Registry-Auth` header value."""
    padded = header + "=" * (-len(header) % 4)
    document: dict[str, str] = json.loads(base64.urlsafe_b64decode(padded))
    return document


async def stored_binding(db: Session, environment_id: int, key: str) -> StoredBinding | None:
    return await ImageBindingRepository(db).get(environment_id, key)


async def binding_rows(db: Session) -> list[dict[str, Any]]:
    rows = await db.fetch_limited(
        "SELECT environment_id, image_reference, registry_credential_id, missing_credential_id, "
        "missing_credential_name, missing_reason, observed_digest, state "
        "FROM image_credential_binding ORDER BY id",
        (),
        limit=1000,
    )
    return [dict(row) for row in rows]


async def rows_for(db: Session, action: str) -> list[dict[str, Any]]:
    """Return the activity rows of one action, oldest first, with detail decoded."""
    rows = await db.fetch_limited(
        "SELECT action, result, actor_type, actor_user_id, target_type, target_id, "
        "environment_id, detail FROM activity_event WHERE action = ? ORDER BY id",
        (action,),
        limit=1000,
    )
    return [{**dict(row), "detail": json.loads(row["detail"] or "{}")} for row in rows]


class CredentialRig(Protocol):
    registry: RegistryRig
    db: Session


async def register(
    rig: CredentialRig,
    name: str,
    *,
    address: str = ADDRESS,
    username: str | None = None,
    secret: str = SECRET,
    environments: tuple[int, ...] = (ENV_A,),
    users: tuple[int, ...] = (),
    default: bool = False,
) -> int:
    """Create a credential set, grant it to environments and users, and return its id."""
    fields = static_fields(name, url=address, username=username or f"user-{name}")
    created = await rig.registry.credentials.create(fields, SecretStr(secret))
    for environment_id in environments:
        await grant_environment(rig.db, created.id, environment_id)
    for user_id in users:
        await grant_user(rig.db, created.id, user_id)
    if default:
        await rig.db.execute(
            "UPDATE registry_credential SET is_default_for_host = 1 WHERE id = ?", (created.id,)
        )
    return created.id


async def stage(rig: "ImageRig", reference: str, number: int = 1, **fields: Any) -> str:
    """Put an image on the daemon under a reference and return its bare id."""
    rig.host.add_image(reference, full_id(number), **fields)
    return image_id(number)


async def mark_missing(db: Session, set_id: int) -> None:
    """Move every binding of a set to credential_missing, as deleting the set does."""
    await db.execute(
        "UPDATE image_credential_binding SET state = 'credential_missing', "
        "registry_credential_id = NULL, missing_credential_id = ?, "
        "missing_credential_name = 'gone', missing_reason = 'deleted', "
        "credential_missing_at = '2026-01-02T03:04:05Z' WHERE registry_credential_id = ?",
        (set_id, set_id),
    )


class ScriptedManifests:
    """Answers each reference with a digest or a failure and records every call."""

    def __init__(self, default: str | ManifestCheckKind | None = None) -> None:
        self.default = digest(2) if default is None else default
        self.answers: dict[str, str | ManifestCheckKind] = {}
        self.calls: list[tuple[RegistryTarget, ImageReference, RegistryLogin | None]] = []
        self.entered = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def head_digest(
        self, target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
    ) -> str:
        self.calls.append((target, reference, login))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        answer = self.answers.get(reference.key, self.default)
        if isinstance(answer, ManifestCheckKind):
            raise ManifestCheckFailed(answer)
        return answer

    def keys(self) -> list[str]:
        return [reference.key for _, reference, _ in self.calls]
