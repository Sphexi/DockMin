"""Template catalogues: add, edit, enable, refresh, disable and delete, for administrators."""

import asyncio
import unicodedata
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.clocked import Clock
from app.db.repositories.errors import DuplicateError
from app.db.repositories.pagination import Page
from app.db.repositories.template_catalogue import (
    CatalogueFields,
    StoredCatalogue,
    TemplateCatalogueRepository,
    TemplateCatalogueStore,
)
from app.netcheck import HIDDEN_CATEGORIES
from app.netpolicy import literal_host_refusal
from app.services.activity import ActivityRecorder, UserActor, audited, current_audit
from app.services.guards import require_administrator
from app.services.template_fetch import (
    CatalogueFetcher,
    CatalogueFetchError,
    DocumentFetcher,
    FetchedBody,
    decode_body,
    failure_text,
    url_refusal,
)
from app.services.template_normalise import NormalisedCatalogue, parse_catalogue

NAME_MAX: Final = 64
MAX_CATALOGUES: Final = 20
DEFAULT_PAGE_SIZE: Final = 25
KINDS: Final = ("linuxserver", "portainer")
KIND_LABELS: Final = {"linuxserver": "linuxserver.io", "portainer": "Portainer format"}
_DETAIL_SKIPPED_MAX: Final = 500

NAME_EXISTS: Final = "A catalogue with this name already exists."
CHANGED_SINCE_REVIEW: Final = "Changed since it was reviewed; nothing was saved."
DELETE_CHANGED: Final = "Changed since it was reviewed; nothing was deleted."
TOO_MANY: Final = f"At most {MAX_CATALOGUES} catalogues can be added. Delete one first."
ALREADY_ENABLED: Final = "The catalogue is already enabled."
NOT_ENABLED: Final = "Enable the catalogue first. A disabled catalogue is never contacted."
KIND_TEXT: Final = "Choose a catalogue kind."
QUERY_REFUSED: Final = "The address may not have a query string."


@dataclass(frozen=True, slots=True)
class Preset:
    """A catalogue an administrator can add with one click."""

    key: str
    name: str
    kind: str
    url: str


LINUXSERVER_PRESET: Final = Preset(
    key="linuxserver",
    name="linuxserver.io",
    kind="linuxserver",
    url="https://api.linuxserver.io/api/v1/images?include_config=true&include_deprecated=false",
)
PRESETS: Final = (LINUXSERVER_PRESET,)


@dataclass(frozen=True, slots=True)
class CatalogueInput:
    """A create or update request as the form submits it."""

    name: str
    kind: str
    url: str


@dataclass(frozen=True, slots=True)
class CatalogueView:
    """A catalogue for its page; `last_failure_text` is the product wording of the stored code."""

    id: int
    name: str
    kind: str
    kind_label: str
    url: str
    host: str
    is_enabled: bool
    entry_count: int
    fetched_at: str | None
    last_failure: str | None
    last_failure_text: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CatalogueDeletePreview:
    """What a deletion names."""

    catalogue_id: int
    name: str
    url: str
    entry_count: int
    is_enabled: bool


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """What a fetch kept and what it skipped, by reason."""

    entry_count: int
    skipped: dict[str, int]
    content_changed: bool

    @property
    def skipped_total(self) -> int:
        """Return how many entries were skipped."""
        return sum(self.skipped.values())


def preset_by_key(key: str) -> Preset | None:
    """Return the preset with this key, or None."""
    return next((preset for preset in PRESETS if preset.key == key), None)


def _skipped_text(skipped: dict[str, int]) -> str:
    return ", ".join(f"{reason}={count}" for reason, count in skipped.items())[:_DETAIL_SKIPPED_MAX]


def _plan(form: CatalogueInput) -> CatalogueFields:
    problems: dict[str, str] = {}
    name = form.name.strip()
    if not name:
        problems["name"] = "Enter a name."
    elif len(name) > NAME_MAX:
        problems["name"] = f"The name is at most {NAME_MAX} characters."
    elif any(unicodedata.category(c) in HIDDEN_CATEGORIES for c in name):
        problems["name"] = "The name may not contain control characters."
    if form.kind not in KINDS:
        problems["kind"] = KIND_TEXT
    url = form.url.strip()
    refusal = url_refusal(url)
    if refusal is None and urlsplit(url).query and url not in {each.url for each in PRESETS}:
        refusal = QUERY_REFUSED
    if refusal is None:
        refusal = literal_host_refusal(urlsplit(url).hostname or "")
    if refusal is not None:
        problems["url"] = refusal
    if problems:
        raise ValidationFailed(problems)
    return CatalogueFields(name=name, kind=form.kind, url=url)


def _fields_detail(fields: CatalogueFields) -> dict[str, str]:
    return {"name": fields.name, "kind": fields.kind, "url_host": urlsplit(fields.url).netloc}


def _fields_of(stored: StoredCatalogue) -> CatalogueFields:
    return CatalogueFields(name=stored.name, kind=stored.kind, url=stored.url)


def _differences(current: StoredCatalogue, fields: CatalogueFields) -> tuple[str, ...]:
    pairs = (
        ("name", current.name, fields.name),
        ("kind", current.kind, fields.kind),
        ("url", current.url, fields.url),
    )
    return tuple(name for name, before, after in pairs if before != after)


def _decode_and_parse(kind: str, body: FetchedBody) -> NormalisedCatalogue:
    return parse_catalogue(kind, decode_body(body))


class TemplateCatalogueService:
    """Applies catalogue rules; every method takes the acting principal and decides itself."""

    def __init__(
        self,
        db: Session,
        clock: Clock,
        activity: ActivityRecorder,
        *,
        fetcher: DocumentFetcher | None = None,
    ) -> None:
        self._db = db
        self._activity = activity
        self._fetcher = fetcher or CatalogueFetcher()
        self._catalogues = TemplateCatalogueRepository(db)
        self._store = TemplateCatalogueStore(db, clock)

    async def page(
        self, actor: Principal, *, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
    ) -> Page[CatalogueView]:
        """Return one page of catalogues by name; administrators only."""
        require_administrator(actor)
        page = await self._catalogues.page_catalogues(limit=limit, cursor=cursor)
        return Page([_view_of(item) for item in page.items], page.next_cursor)

    async def count(self, actor: Principal) -> int:
        """Count the catalogues; administrators only."""
        require_administrator(actor)
        return await self._catalogues.count_catalogues()

    async def get(self, actor: Principal, catalogue_id: int) -> CatalogueView:
        """Return one catalogue; administrators only."""
        require_administrator(actor)
        return _view_of(await self._require(catalogue_id))

    async def delete_preview(self, actor: Principal, catalogue_id: int) -> CatalogueDeletePreview:
        """Name the catalogue a deletion removes; deletes nothing."""
        require_administrator(actor)
        stored = await self._require(catalogue_id)
        return CatalogueDeletePreview(
            catalogue_id=stored.id,
            name=stored.name,
            url=stored.url,
            entry_count=stored.entry_count,
            is_enabled=stored.is_enabled,
        )

    @audited(action="template_catalogue_created", target_type="template_catalogue")
    async def create(self, actor: Principal, form: CatalogueInput) -> int:
        """Add a disabled catalogue; nothing is fetched. Administrators only."""
        require_administrator(actor)
        fields = _plan(form)
        audit = current_audit()
        async with self._db.transaction():
            if await self._catalogues.count_catalogues() >= MAX_CATALOGUES:
                raise ValidationFailed({"form": TOO_MANY})
            try:
                catalogue_id = await self._store.add_catalogue(fields)
            except DuplicateError:
                raise ValidationFailed({"name": NAME_EXISTS}) from None
            audit.target_id = str(catalogue_id)
            audit.detail.update(_fields_detail(fields))
        return catalogue_id

    @audited(
        action="template_catalogue_updated",
        target_type="template_catalogue",
        target_id=lambda a: str(a["catalogue_id"]),
    )
    async def update(
        self,
        actor: Principal,
        catalogue_id: int,
        form: CatalogueInput,
        *,
        expected_updated_at: str,
    ) -> bool:
        """Change a catalogue if it is unchanged since the form was loaded; administrators only.

        A new kind or address discards the fetched templates and disables the catalogue; the
        result says whether that happened.
        """
        require_administrator(actor)
        stored = await self._require(catalogue_id)
        if stored.updated_at != expected_updated_at:
            raise ValidationFailed({"form": CHANGED_SINCE_REVIEW})
        fields = _plan(form)
        changed = _differences(stored, fields)
        audit = current_audit()
        if not changed:
            audit.unchanged()
            return False
        discard = "kind" in changed or "url" in changed
        async with self._db.transaction():
            try:
                written = await self._store.change_catalogue(
                    catalogue_id,
                    fields,
                    discard_content=discard,
                    expected_updated_at=expected_updated_at,
                )
            except DuplicateError:
                raise ValidationFailed({"name": NAME_EXISTS}) from None
            if not written:
                await self._require(catalogue_id)
                raise ValidationFailed({"form": CHANGED_SINCE_REVIEW})
            audit.detail.update(_fields_detail(fields), changed=", ".join(changed))
            audit.detail["content_discarded"] = discard
        return discard

    @audited(
        action="template_catalogue_updated",
        target_type="template_catalogue",
        target_id=lambda a: str(a["catalogue_id"]),
    )
    async def disable(self, actor: Principal, catalogue_id: int) -> None:
        """Stop offering a catalogue's templates, keeping what was fetched. Administrators only."""
        require_administrator(actor)
        stored = await self._require(catalogue_id)
        audit = current_audit()
        if not stored.is_enabled:
            audit.unchanged()
            return
        async with self._db.transaction():
            if not await self._store.disable_catalogue(
                catalogue_id, expected_updated_at=stored.updated_at
            ):
                await self._require(catalogue_id)
                raise ValidationFailed({"form": CHANGED_SINCE_REVIEW})
            audit.detail.update(_fields_detail(_fields_of(stored)), changed="is_enabled")

    @audited(
        action="template_catalogue_refreshed",
        target_type="template_catalogue",
        target_id=lambda a: str(a["catalogue_id"]),
    )
    async def enable(self, actor: Principal, catalogue_id: int) -> RefreshResult:
        """Fetch a disabled catalogue once and, if that works, enable it; administrators only.

        A failed fetch leaves the catalogue disabled and raises CatalogueFetchError.
        """
        require_administrator(actor)
        stored = await self._require(catalogue_id)
        if stored.is_enabled:
            raise ValidationFailed({"catalogue": ALREADY_ENABLED})
        return await self._fetch(actor, stored, enable=True)

    @audited(
        action="template_catalogue_refreshed",
        target_type="template_catalogue",
        target_id=lambda a: str(a["catalogue_id"]),
    )
    async def refresh(self, actor: Principal, catalogue_id: int) -> RefreshResult:
        """Fetch an enabled catalogue once; administrators only. Raises CatalogueFetchError."""
        require_administrator(actor)
        stored = await self._require(catalogue_id)
        if not stored.is_enabled:
            raise ValidationFailed({"catalogue": NOT_ENABLED})
        return await self._fetch(actor, stored, enable=False)

    @audited(
        action="template_catalogue_deleted",
        target_type="template_catalogue",
        target_id=lambda a: str(a["catalogue_id"]),
    )
    async def delete(self, actor: Principal, catalogue_id: int, *, expected_name: str) -> None:
        """Delete a catalogue and its fetched templates while its name is still expected_name."""
        require_administrator(actor)
        audit = current_audit()
        async with self._db.transaction():
            stored = await self._catalogues.get(catalogue_id)
            if stored is None:
                raise ResourceNotFound("not found")
            if stored.name != expected_name:
                raise ValidationFailed({"name": DELETE_CHANGED})
            if not await self._store.remove_catalogue(catalogue_id, expected_name=expected_name):
                raise ValidationFailed({"name": DELETE_CHANGED})
            audit.detail.update(_fields_detail(_fields_of(stored)), entry_count=stored.entry_count)

    async def _fetch(
        self, actor: Principal, stored: StoredCatalogue, *, enable: bool
    ) -> RefreshResult:
        audit = current_audit()
        audit.detail.update(_fields_detail(_fields_of(stored)))
        audit.detail["trigger"] = "enable" if enable else "refresh"
        failure: CatalogueFetchError | None = None
        parsed: NormalisedCatalogue | None = None
        try:
            body = await self._fetcher.fetch(stored.url)
            parsed = await asyncio.to_thread(_decode_and_parse, stored.kind, body)
        except CatalogueFetchError as error:
            failure = error
        changed = parsed is not None and parsed.digest != stored.content_digest
        async with self._db.transaction():
            if parsed is None or failure is not None:
                code = "network" if failure is None else failure.code
                written = await self._store.store_fetch_failure(
                    stored.id, code=code, expected_updated_at=stored.updated_at
                )
                audit.unchanged()
            else:
                written = await self._store.store_fetch(
                    stored.id,
                    enable=enable,
                    entry_count=parsed.entry_count,
                    content=parsed.content if changed else None,
                    digest=parsed.digest,
                    expected_updated_at=stored.updated_at,
                )
            if not written:
                raise ValidationFailed({"form": CHANGED_SINCE_REVIEW})
            if parsed is not None:
                audit.detail.update(
                    entry_count=parsed.entry_count,
                    skipped=_skipped_text(parsed.skipped),
                    skipped_total=sum(parsed.skipped.values()),
                    content_changed=changed,
                )
        if failure is not None:
            await self._activity.record_failure(
                UserActor.of(actor),
                action="template_catalogue_refreshed",
                target_type="template_catalogue",
                target_id=str(stored.id),
                reason=failure.code,
                message=str(failure),
                detail=dict(audit.detail),
            )
            raise failure
        if parsed is None:
            raise CatalogueFetchError("network")
        return RefreshResult(parsed.entry_count, parsed.skipped, changed)

    async def _require(self, catalogue_id: int) -> StoredCatalogue:
        stored = await self._catalogues.get(catalogue_id)
        if stored is None:
            raise ResourceNotFound("not found")
        return stored


def _view_of(stored: StoredCatalogue) -> CatalogueView:
    return CatalogueView(
        id=stored.id,
        name=stored.name,
        kind=stored.kind,
        kind_label=KIND_LABELS.get(stored.kind, stored.kind),
        url=stored.url,
        host=urlsplit(stored.url).netloc,
        is_enabled=stored.is_enabled,
        entry_count=stored.entry_count,
        fetched_at=stored.fetched_at,
        last_failure=stored.last_failure,
        last_failure_text=None
        if stored.last_failure is None
        else failure_text(stored.last_failure),
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )
