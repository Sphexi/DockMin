"""Tags: labels that are applied to environments and filter the dashboard; they carry no access."""

import re
from dataclasses import dataclass

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.errors import DuplicateError
from app.db.repositories.pagination import Page
from app.db.repositories.tag import TagRepository, TagSummary, TagSummaryRepository
from app.services.activity import ActivityRecorder, audited, current_audit
from app.services.environment_groups import MemberEnvironment
from app.services.environments import CHANGED_SINCE_REVIEW, MAX_IMPACT_NAMES, TagView
from app.services.guards import require_administrator

TAG_NAME_MAX = 64
TAG_NAME_PATTERN = re.compile(r"[A-Za-z0-9 ._-]+")
TAG_NAME_RULE = f"Use 1 to {TAG_NAME_MAX} letters, digits, spaces, dots, underscores or hyphens."
TAG_NAME_TAKEN = "That tag already exists."
DETACH_NOTE = "The tag is removed from these environments. The environments are not changed."


@dataclass(frozen=True, slots=True)
class TagDeletionPreview:
    """What deleting a tag does: the environments that lose it."""

    tag_id: int
    name: str
    environments: tuple[MemberEnvironment, ...]
    environments_total: int
    detach_note: str = DETACH_NOTE


class TagService:
    """Applies tag rules; tags are managed by administrators only."""

    def __init__(
        self,
        *,
        db: Session,
        tags: TagRepository,
        summaries: TagSummaryRepository,
        activity: ActivityRecorder,
    ) -> None:
        self._db = db
        self._tags = tags
        self._summaries = summaries
        self._activity = activity

    async def list_page(
        self, actor: Principal, *, limit: int, cursor: str | None = None
    ) -> Page[TagSummary]:
        """Return a page of tags with the number of environments carrying each."""
        require_administrator(actor)
        return await self._summaries.list_page(limit=limit, cursor=cursor)

    @audited(action="tag_created", target_type="tag")
    async def create(self, actor: Principal, *, name: str) -> TagView:
        """Create a tag; administrators only."""
        require_administrator(actor)
        checked = _check_name(name)
        async with self._db.transaction():
            try:
                tag = await self._tags.create(checked)
            except DuplicateError:
                raise ValidationFailed({"name": TAG_NAME_TAKEN}) from None
            audit = current_audit()
            audit.target_id = str(tag.id)
            audit.detail["name"] = checked
        return TagView(tag.id, tag.name)

    async def delete_preview(self, actor: Principal, tag_id: int) -> TagDeletionPreview:
        """Name, before the delete, the environments carrying the tag."""
        require_administrator(actor)
        tag = await self._tags.get(tag_id)
        if tag is None:
            raise ResourceNotFound("not found")
        members = await self._tags.list_environments(tag_id, limit=MAX_IMPACT_NAMES)
        return TagDeletionPreview(
            tag_id=tag.id,
            name=tag.name,
            environments=tuple(MemberEnvironment(i, n) for i, n in members),
            environments_total=await self._tags.count_environments(tag_id),
        )

    @audited(action="tag_deleted", target_type="tag", target_id=lambda a: str(a["tag_id"]))
    async def delete(self, actor: Principal, tag_id: int, *, expected_name: str) -> None:
        """Delete a tag and its links while its name is still expected_name; administrators only."""
        require_administrator(actor)
        carried = await self._tags.count_environments(tag_id)
        async with self._db.transaction():
            tag = await self._tags.get(tag_id)
            if tag is None:
                raise ResourceNotFound("not found")
            if tag.name != expected_name:
                raise ValidationFailed({"name": CHANGED_SINCE_REVIEW})
            await self._tags.delete(tag_id)
            current_audit().detail.update(name=tag.name, environments=carried)


def _check_name(name: str) -> str:
    checked = name.strip()
    if not checked or len(checked) > TAG_NAME_MAX or TAG_NAME_PATTERN.fullmatch(checked) is None:
        raise ValidationFailed({"name": TAG_NAME_RULE})
    return checked
