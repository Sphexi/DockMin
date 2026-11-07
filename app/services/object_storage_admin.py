"""Object storage targets: create, edit, delete and test, each checked against the role."""

import asyncio
import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.clocked import Clock
from app.db.repositories.errors import DuplicateError
from app.db.repositories.object_storage_target import (
    ObjectStorageTargetRepository,
    ObjectStorageTargetStore,
    RuleReference,
    StoredObjectStorageTarget,
    TargetFields,
)
from app.db.repositories.pagination import MAX_PAGE_SIZE, Page
from app.netcheck import HIDDEN_CATEGORIES
from app.netpolicy import is_internal_host, literal_host_refusal
from app.object_storage import (
    PERMISSION_NOTE,
    ObjectStorageClient,
    StorageCheck,
    StorageFailure,
    StorageStage,
    addressing,
    check_target,
    stage_message,
    validate_prefix,
)
from app.services.activity import ActivityRecorder, audited, current_audit
from app.services.guards import require_administrator
from app.services.object_storage import CredentialUnreadable, ObjectStorageTargets
from app.services.registry_input import (
    CaCertificateSummary,
    CheckedBundle,
    check_ca_bundle,
    summarise_ca_bundle,
)

DEFAULT_REGION: Final = "us-east-1"
DEFAULT_BACKUP_PREFIX: Final = "backups"
DEFAULT_ACTIVITY_LOG_PREFIX: Final = "activity-log"
DEFAULT_PAGE_SIZE: Final = 25
NAME_MAX: Final = 64
SECRET_MAX: Final = 1024
RETENTION_MAX: Final = 1000
MAX_RULES_SHOWN: Final = 100
MAX_SUMMARIES: Final = 500
RETENTION_FOLDER: Final = "<backup prefix>/rule-<rule id>-<rule created>/"
RETENTION_FILES: Final = "dockmin-backup-<date and time>.tar.gz"
CREDENTIAL_ADVICE: Final = (
    "Use a credential scoped to this bucket. A credential that is a server administrator can "
    "create buckets, and a mistyped bucket name would then be created silently."
)

NAME_EXISTS: Final = "An object storage target with this name already exists."
CHANGED_SINCE_REVIEW: Final = "Changed since it was reviewed; nothing was saved."
DELETE_CHANGED: Final = "Changed since it was reviewed; nothing was deleted."
IN_USE: Final = "Automation rules use this target. Change or delete those rules first."
SECRET_UNREADABLE: Final = (
    "The stored secret cannot be read with the current encryption key. Enter it again."
)
RETENTION_CONFIRMATION: Final = "Confirm that older backup archives in this bucket will be deleted."

_PROBE_BUCKET: Final = "dockmin-probe"
_ACCESS_KEY_ID: Final = re.compile(r"[A-Za-z0-9._@+=-]{1,128}", re.ASCII)
_STAGES: Final[tuple[StorageStage, ...]] = ("bucket", "backup_prefix", "activity_log_prefix")

__all__ = [
    "CREDENTIAL_ADVICE",
    "DEFAULT_ACTIVITY_LOG_PREFIX",
    "DEFAULT_BACKUP_PREFIX",
    "DEFAULT_REGION",
    "PERMISSION_NOTE",
    "ObjectStorageTargetService",
    "StorageCheck",
    "TargetDeletePreview",
    "TargetInput",
    "TargetView",
    "stage_message",
    "target_warnings",
]


@dataclass(frozen=True, slots=True)
class TargetInput:
    """A create or update request as the form submits it.

    A blank secret on an update keeps the stored one, except when the endpoint, bucket,
    addressing style, certificate verification or certificate bundle changes. A blank bundle
    keeps the stored one unless remove_ca_bundle is set. A blank retention count keeps every
    archive.
    """

    name: str
    endpoint_url: str
    bucket: str
    access_key_id: str
    backup_prefix: str
    activity_log_prefix: str
    region: str = DEFAULT_REGION
    secret_access_key: SecretStr = field(default=SecretStr(""), repr=False)
    use_path_style: bool = True
    verify_tls: bool = True
    ca_bundle: str = field(default="", repr=False)
    remove_ca_bundle: bool = False
    retention_count: str = ""
    confirm_retention: bool = False


@dataclass(frozen=True, slots=True)
class TargetView:
    """A target for its page; the secret is only ever reported as stored."""

    id: int
    name: str
    endpoint_url: str
    region: str
    bucket: str
    access_key_id: str
    use_path_style: bool
    verify_tls: bool
    ca_bundle_set: bool
    ca_certificates: tuple[CaCertificateSummary, ...]
    backup_prefix: str
    activity_log_prefix: str
    retention_count: int | None
    created_at: str
    updated_at: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TargetDeletePreview:
    """What a deletion names: the target and the rules that stop it."""

    target_id: int
    name: str
    endpoint_url: str
    bucket: str
    rules: tuple[RuleReference, ...]
    more_rules: bool

    @property
    def blocked(self) -> bool:
        """Report whether a rule still uses the target."""
        return bool(self.rules)


@dataclass(frozen=True, slots=True)
class _Plan:
    fields: TargetFields
    secret: str | None = field(repr=False)
    changed: tuple[str, ...]
    secret_replaced: bool


def target_warnings(endpoint_url: str, verify_tls: bool) -> tuple[str, ...]:
    """Return what an administrator should know about an endpoint before trusting it."""
    found: list[str] = []
    parts = urlsplit(endpoint_url.strip())
    if parts.scheme == "http":
        found.append("Credentials and archives cross the network unencrypted over plain http.")
    if not verify_tls:
        found.append("Certificates are not verified, so any server at this address is accepted.")
    if parts.hostname is not None and is_internal_host(parts.hostname):
        found.append(
            "This address is on this server or a link-local network. It is accepted as configured."
        )
    return tuple(found)


def _host_is_ip(endpoint_url: str) -> bool:
    hostname = urlsplit(endpoint_url).hostname
    if hostname is None:
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _resolves(endpoint_url: str, bucket: str, region: str, path_style: bool) -> bool:
    try:
        addressing.resolve(
            endpoint_url=endpoint_url, bucket=bucket, region=region, use_path_style=path_style
        )
    except StorageFailure:
        return False
    return True


def _addressing_problem(
    endpoint_url: str, region: str, bucket: str, path_style: bool
) -> tuple[str, str] | None:
    if not _resolves(endpoint_url, _PROBE_BUCKET, DEFAULT_REGION, True):
        return (
            "endpoint_url",
            "Enter an http or https address with a host name and no user name, path, query or "
            "fragment.",
        )
    refusal = literal_host_refusal(urlsplit(endpoint_url).hostname or "")
    if refusal is not None:
        return "endpoint_url", refusal
    if not _resolves(endpoint_url, _PROBE_BUCKET, region, True):
        return (
            "region",
            "Use letters, digits, '.', '_' and '-', starting with a letter or digit.",
        )
    if not path_style and _host_is_ip(endpoint_url):
        return (
            "use_path_style",
            "Virtual-host addressing needs a host name, not an IP address. Use path-style "
            "addressing for this endpoint.",
        )
    if not _resolves(endpoint_url, bucket, region, path_style):
        if path_style:
            return (
                "bucket",
                "Use letters, digits, '.', '_' and '-', starting with a letter or digit.",
            )
        return "bucket", "Virtual-host addressing needs a single lower-case DNS label."
    return None


def _name(text: str, problems: dict[str, str]) -> str:
    name = text.strip()
    if not name:
        problems["name"] = "Enter a name."
    elif len(name) > NAME_MAX:
        problems["name"] = f"The name is at most {NAME_MAX} characters."
    elif any(unicodedata.category(c) in HIDDEN_CATEGORIES for c in name):
        problems["name"] = "The name may not contain control characters."
    return name


def _prefix(text: str, field_name: str, problems: dict[str, str]) -> str:
    try:
        return validate_prefix(text.strip())
    except StorageFailure:
        problems[field_name] = (
            "Use letters, digits, '.', '_', '-' and '/', with no empty or dot-only segment."
        )
        return ""


def _segments(prefix: str) -> list[str]:
    return [segment for segment in prefix.split("/") if segment]


def _prefix_relation(backup: str, log: str, problems: dict[str, str]) -> None:
    if backup == log:
        problems["activity_log_prefix"] = "The backup and activity log prefixes must differ."
        return
    first, second = _segments(backup), _segments(log)
    if not first or not second:
        return
    shorter = min(len(first), len(second))
    if first[:shorter] == second[:shorter]:
        problems["activity_log_prefix"] = "Neither prefix may be a folder inside the other."


def _retention(text: str, problems: dict[str, str]) -> int | None:
    value = text.strip()
    if not value:
        return None
    if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= RETENTION_MAX:
        problems["retention_count"] = (
            f"Enter a whole number from 1 to {RETENTION_MAX}, or leave it blank to keep every "
            "archive."
        )
        return None
    return int(value)


def _secret(secret: SecretStr, problems: dict[str, str]) -> str:
    text = secret.get_secret_value()
    if len(text) > SECRET_MAX:
        problems["secret_access_key"] = f"The secret is at most {SECRET_MAX} characters."
    elif any(unicodedata.category(c) == "Cc" for c in text):
        problems["secret_access_key"] = "The secret may not contain control characters."
    return text


def _ca_bundle(
    form: TargetInput,
    current: StoredObjectStorageTarget | None,
    bundle: CheckedBundle | None,
    problems: dict[str, str],
) -> str | None:
    if not form.verify_tls:
        if bundle is not None:
            problems["ca_bundle"] = (
                "A certificate bundle is used only when certificates are verified."
            )
        return None
    if bundle is not None:
        return bundle.pem
    if form.remove_ca_bundle or current is None:
        return None
    return current.ca_bundle


def _differences(current: StoredObjectStorageTarget, fields: TargetFields) -> tuple[str, ...]:
    pairs = (
        ("name", current.name, fields.name),
        ("endpoint_url", current.endpoint_url, fields.endpoint_url),
        ("region", current.region, fields.region),
        ("bucket", current.bucket, fields.bucket),
        ("access_key_id", current.access_key_id, fields.access_key_id),
        ("use_path_style", current.use_path_style, fields.use_path_style),
        ("verify_tls", current.verify_tls, fields.verify_tls),
        ("ca_bundle", current.ca_bundle, fields.ca_bundle),
        ("backup_prefix", current.backup_prefix, fields.backup_prefix),
        ("activity_log_prefix", current.activity_log_prefix, fields.activity_log_prefix),
        ("retention_count", current.retention_count, fields.retention_count),
    )
    return tuple(name for name, before, after in pairs if before != after)


def _redirected(current: StoredObjectStorageTarget, fields: TargetFields) -> bool:
    return (
        fields.endpoint_url != current.endpoint_url
        or fields.bucket != current.bucket
        or fields.use_path_style != current.use_path_style
        or fields.verify_tls != current.verify_tls
        or fields.ca_bundle != current.ca_bundle
    )


def _needs_confirmation(retention: int | None, current: StoredObjectStorageTarget | None) -> bool:
    if retention is None:
        return False
    stored = None if current is None else current.retention_count
    return stored is None or retention < stored


def _plan(
    form: TargetInput,
    current: StoredObjectStorageTarget | None,
    bundle: CheckedBundle | None,
    bundle_problem: str | None,
) -> _Plan:
    problems: dict[str, str] = {}
    name = _name(form.name, problems)
    endpoint_url = form.endpoint_url.strip()
    region = form.region.strip()
    bucket = form.bucket.strip()
    access_key_id = form.access_key_id.strip()
    found = _addressing_problem(endpoint_url, region, bucket, form.use_path_style)
    if found is not None:
        problems[found[0]] = found[1]
    if _ACCESS_KEY_ID.fullmatch(access_key_id) is None:
        problems["access_key_id"] = "Use 1 to 128 letters, digits and . _ @ + = -."
    backup_prefix = _prefix(form.backup_prefix, "backup_prefix", problems)
    log_prefix = _prefix(form.activity_log_prefix, "activity_log_prefix", problems)
    if "backup_prefix" not in problems and "activity_log_prefix" not in problems:
        _prefix_relation(backup_prefix, log_prefix, problems)
    retention = _retention(form.retention_count, problems)
    if _needs_confirmation(retention, current) and not form.confirm_retention:
        problems["confirm_retention"] = RETENTION_CONFIRMATION
    ca_bundle = _ca_bundle(form, current, bundle, problems)
    if bundle_problem is not None:
        problems["ca_bundle"] = bundle_problem
    secret = _secret(form.secret_access_key, problems)
    fields = TargetFields(
        name=name,
        endpoint_url=endpoint_url,
        region=region,
        bucket=bucket,
        access_key_id=access_key_id,
        use_path_style=form.use_path_style,
        verify_tls=form.verify_tls,
        ca_bundle=ca_bundle,
        backup_prefix=backup_prefix,
        activity_log_prefix=log_prefix,
        retention_count=retention,
    )
    if not secret and "secret_access_key" not in problems:
        if current is None:
            problems["secret_access_key"] = "Enter the secret access key."
        elif _redirected(current, fields):
            problems["secret_access_key"] = (
                "The endpoint, bucket, addressing style or certificate settings changed, so "
                "enter the secret access key again."
            )
    if problems:
        raise ValidationFailed(problems)
    changed = () if current is None else _differences(current, fields)
    return _Plan(fields, secret or None, changed, bool(secret) and current is not None)


def _fields_detail(fields: TargetFields) -> dict[str, str | int | bool | None]:
    return {
        "name": fields.name,
        "endpoint_host": urlsplit(fields.endpoint_url).netloc,
        "bucket": fields.bucket,
        "use_path_style": fields.use_path_style,
        "verify_tls": fields.verify_tls,
        "ca_bundle_set": fields.ca_bundle is not None,
        "backup_prefix": fields.backup_prefix,
        "activity_log_prefix": fields.activity_log_prefix,
        "retention_count": fields.retention_count,
    }


def _fields_of(stored: StoredObjectStorageTarget) -> TargetFields:
    return TargetFields(
        name=stored.name,
        endpoint_url=stored.endpoint_url,
        region=stored.region,
        bucket=stored.bucket,
        access_key_id=stored.access_key_id,
        use_path_style=stored.use_path_style,
        verify_tls=stored.verify_tls,
        ca_bundle=stored.ca_bundle,
        backup_prefix=stored.backup_prefix,
        activity_log_prefix=stored.activity_log_prefix,
        retention_count=stored.retention_count,
    )


class ObjectStorageTargetService:
    """Applies target rules; every method takes the acting principal and decides itself."""

    def __init__(
        self,
        db: Session,
        vault: Vault,
        clock: Clock,
        activity: ActivityRecorder,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._db = db
        self._vault = vault
        self._clock = clock
        self._activity = activity
        self._transport = transport
        self._targets = ObjectStorageTargetRepository(db)
        self._store = ObjectStorageTargetStore(db, vault, clock)

    async def page(
        self, actor: Principal, *, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
    ) -> Page[TargetView]:
        """Return one page of targets by name; administrators only."""
        require_administrator(actor)
        page = await self._targets.page_targets(limit=limit, cursor=cursor)
        return Page([self._view_of(item) for item in page.items], page.next_cursor)

    async def count(self, actor: Principal) -> int:
        """Count the targets page lists; administrators only."""
        require_administrator(actor)
        return await self._targets.count_targets()

    async def summaries(self, actor: Principal) -> list[tuple[int, str]]:
        """Return (id, name) of the targets by name, for a rule's target choice."""
        require_administrator(actor)
        return await self._targets.summaries(limit=min(MAX_SUMMARIES, MAX_PAGE_SIZE * 10))

    async def get(self, actor: Principal, target_id: int) -> TargetView:
        """Return one target; administrators only."""
        require_administrator(actor)
        return self._view_of(await self._require(target_id))

    async def delete_preview(self, actor: Principal, target_id: int) -> TargetDeletePreview:
        """Name the target and the rules that stop its deletion; deletes nothing."""
        require_administrator(actor)
        stored = await self._require(target_id)
        rules = await self._targets.rules_referencing(target_id, limit=MAX_RULES_SHOWN + 1)
        return TargetDeletePreview(
            target_id=stored.id,
            name=stored.name,
            endpoint_url=stored.endpoint_url,
            bucket=stored.bucket,
            rules=tuple(rules[:MAX_RULES_SHOWN]),
            more_rules=len(rules) > MAX_RULES_SHOWN,
        )

    @audited(action="object_storage_target_created", target_type="object_storage_target")
    async def create(self, actor: Principal, form: TargetInput) -> int:
        """Create a target and seal its secret; administrators only."""
        require_administrator(actor)
        bundle, bundle_problem = await self._checked_bundle(form)
        plan = _plan(form, None, bundle, bundle_problem)
        if plan.secret is None:
            raise ValidationFailed({"secret_access_key": "Enter the secret access key."})
        audit = current_audit()
        async with self._db.transaction():
            try:
                target_id = await self._store.add_target(plan.fields, secret_access_key=plan.secret)
            except DuplicateError:
                raise ValidationFailed({"name": NAME_EXISTS}) from None
            audit.target_id = str(target_id)
            audit.detail.update(_fields_detail(plan.fields), secret_replaced=True)
        return target_id

    @audited(
        action="object_storage_target_updated",
        target_type="object_storage_target",
        target_id=lambda a: str(a["target_id"]),
    )
    async def update(
        self, actor: Principal, target_id: int, form: TargetInput, *, expected_updated_at: str
    ) -> None:
        """Change a target if it is unchanged since the form was loaded; administrators only.

        A blank secret keeps the stored one unless the endpoint, bucket, addressing style,
        certificate verification or certificate bundle changes, which requires it again.
        """
        require_administrator(actor)
        stored = await self._require(target_id)
        if stored.updated_at != expected_updated_at:
            raise ValidationFailed({"form": CHANGED_SINCE_REVIEW})
        bundle, bundle_problem = await self._checked_bundle(form)
        plan = _plan(form, stored, bundle, bundle_problem)
        audit = current_audit()
        if not plan.changed and not plan.secret_replaced:
            audit.unchanged()
            return
        async with self._db.transaction():
            try:
                written = await self._store.change_target(
                    target_id,
                    plan.fields,
                    secret_access_key=plan.secret,
                    expected_updated_at=expected_updated_at,
                )
            except DuplicateError:
                raise ValidationFailed({"name": NAME_EXISTS}) from None
            if not written:
                await self._require(target_id)
                raise ValidationFailed({"form": CHANGED_SINCE_REVIEW})
            audit.detail.update(
                _fields_detail(plan.fields),
                changed=", ".join(plan.changed),
                secret_replaced=plan.secret_replaced,
            )

    @audited(
        action="object_storage_target_deleted",
        target_type="object_storage_target",
        target_id=lambda a: str(a["target_id"]),
    )
    async def delete(self, actor: Principal, target_id: int, *, expected_name: str) -> None:
        """Delete a target no rule uses, while its name is still expected_name; administrators only.

        Objects in the bucket are untouched, and backup records keep their rows.
        """
        require_administrator(actor)
        audit = current_audit()
        async with self._db.transaction():
            stored = await self._targets.get(target_id)
            if stored is None:
                raise ResourceNotFound("not found")
            if stored.name != expected_name:
                raise ValidationFailed({"name": DELETE_CHANGED})
            if await self._targets.rules_referencing(target_id, limit=1):
                audit.reason = "target_in_use"
                raise ValidationFailed({"target": IN_USE})
            if not await self._store.remove_target(target_id, expected_name=expected_name):
                raise ValidationFailed({"name": DELETE_CHANGED})
            audit.detail.update(_fields_detail(_fields_of(stored)))

    @audited(
        action="object_storage_target_tested",
        target_type="object_storage_target",
        target_id=lambda a: str(a["target_id"]),
    )
    async def test(self, actor: Principal, target_id: int) -> tuple[StorageCheck, ...]:
        """Probe the saved target from this server; a failed probe is a result, not an error.

        Only the saved target is tested, with its stored secret, so no secret is sent to an
        address that was not saved. The probe checks neither write nor delete permission.
        """
        require_administrator(actor)
        try:
            opened = await ObjectStorageTargets(self._db, self._vault).open(target_id)
        except CredentialUnreadable:
            raise ValidationFailed({"secret_access_key": SECRET_UNREADABLE}) from None
        if opened is None:
            raise ResourceNotFound("not found")
        started = self._clock()
        try:
            client = ObjectStorageClient(
                opened.endpoint, opened.credentials, transport=self._transport, clock=self._clock
            )
        except StorageFailure as failure:
            checks = tuple(
                StorageCheck(stage, False, failure.kind, None, None) for stage in _STAGES
            )
        else:
            async with client:
                checks = await check_target(
                    client,
                    backup_prefix=opened.backup_prefix,
                    activity_log_prefix=opened.activity_log_prefix,
                )
        failed = next((check for check in checks if not check.passed), None)
        current_audit().detail.update(
            passed=failed is None,
            failed_stage=None if failed is None else failed.stage,
            storage_status=None if failed is None else failed.status,
            storage_code=None if failed is None else failed.code,
            duration_ms=int((self._clock() - started).total_seconds() * 1000),
        )
        return checks

    async def _require(self, target_id: int) -> StoredObjectStorageTarget:
        stored = await self._targets.get(target_id)
        if stored is None:
            raise ResourceNotFound("not found")
        return stored

    async def _checked_bundle(self, form: TargetInput) -> tuple[CheckedBundle | None, str | None]:
        text = form.ca_bundle.strip()
        if not text:
            return None, None
        try:
            return await asyncio.to_thread(check_ca_bundle, text, self._clock()), None
        except ValidationFailed as invalid:
            return None, invalid.fields["ca_bundle"]

    def _view_of(self, stored: StoredObjectStorageTarget) -> TargetView:
        certificates = (
            () if stored.ca_bundle is None else summarise_ca_bundle(stored.ca_bundle, self._clock())
        )
        return TargetView(
            id=stored.id,
            name=stored.name,
            endpoint_url=stored.endpoint_url,
            region=stored.region,
            bucket=stored.bucket,
            access_key_id=stored.access_key_id,
            use_path_style=stored.use_path_style,
            verify_tls=stored.verify_tls,
            ca_bundle_set=stored.ca_bundle is not None,
            ca_certificates=certificates,
            backup_prefix=stored.backup_prefix,
            activity_log_prefix=stored.activity_log_prefix,
            retention_count=stored.retention_count,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            warnings=target_warnings(stored.endpoint_url, stored.verify_tls),
        )
