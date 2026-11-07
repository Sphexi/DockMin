"""Image lists, details and the query service."""

import re
import unicodedata
from collections.abc import (
    Callable,
    Collection,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.image_update_status import (
    ImageUpdateStatusRepository,
    StoredUpdateStatus,
)
from app.db.repositories.pagination import (
    MAX_PAGE_SIZE,
    CursorPosition,
    InvalidCursorError,
    Page,
    decode_cursor,
    encode_cursor,
)
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    EngineError,
    NotFound,
    ResponseTooLarge,
)
from app.docker_engine.models import (
    ContainerSummary,
    ImageInspect,
    ImageSummary,
)
from app.logging import DEFAULT_SCRUBBER, REDACTED, get_logger
from app.registry_client.references import (
    ImageReference,
    ReferenceInvalid,
    digests_for,
    parse_reference,
)
from app.services.access import ResourceRef
from app.services.activity import (
    Arguments,
)
from app.services.authorisation import Authoriser
from app.services.container_actions import (
    MAX_BATCH,
)
from app.services.containers import (
    EnvVar,
    LabelView,
    display_name,
    parse_engine_time,
    redact_label,
    short_id,
)
from app.services.guards import require_administrator
from app.services.image_bindings import (
    BindingView,
    ImageBindingService,
    bare_image_id,
    canonical_keys,
    parse_pull_reference,
    resolve_live_references,
)
from app.services.image_listing import (
    read_containers,
    read_host_listing,
    read_images,
)
from app.services.image_views import (
    ContainerUse,
    HistoryRow,
    ImageBatchConfirmation,
    ImageBatchTargetView,
    ImageDetail,
    ImageFilters,
    ImagePage,
    ImageReferenceView,
    ImageRow,
    ImageTooLarge,
    PruneCandidate,
    PruneMode,
    PrunePlan,
    PullPlan,
    RemoveImpact,
    ReplacedImage,
    TagImpact,
    UntagImpact,
    references_digest,
)
from app.services.object_inspector import (
    HostClients,
    unavailable_from,
)
from app.services.pull_credentials import (
    CredentialChoice,
)

logger = get_logger(__name__)

__all__ = [
    "BINDING_FILTERS",
    "DANGLING_FILTERS",
    "DEFAULT_PAGE_SIZE",
    "IMAGE_ID",
    "MAX_PRUNE",
    "PRUNE_MODES",
    "UPDATE_FILTERS",
    "USAGE_FILTERS",
    "Holder",
    "ImageQueryService",
    "automation_plan",
    "distinct_images",
    "holder_of",
    "image_argument",
    "inspect_image",
    "parse_tag_target",
    "plan_image",
    "prune_candidates",
    "remaining_after_untag",
    "require_id",
    "usage_of",
]

DEFAULT_PAGE_SIZE: Final = 25


MAX_QUERY: Final = 128


MAX_REFERENCES_SHOWN: Final = 100


MAX_CONTAINERS_SHOWN: Final = 10


MAX_DETAIL_ROWS: Final = 500


MAX_PRUNE: Final = 100


MAX_CREATED_BY: Final = 512


ROW_ID_HEX_DIGITS: Final = 15


DANGLING_SORT_VALUE: Final = "~"


DANGLING_FILTERS: Final = ("yes", "no")


USAGE_FILTERS: Final = ("used", "unused")


BINDING_FILTERS: Final = ("bound", "anonymous", "credential_missing", "unbound")


UPDATE_FILTERS: Final = ("available",)


IMAGE_ID = re.compile(r"[0-9a-f]{64}")


_EPOCH = datetime.fromtimestamp(0, UTC)


PRUNE_MODES: Final[tuple[PruneMode, ...]] = ("dangling", "unused")


@dataclass(frozen=True, slots=True)
class _Entry:
    summary: ImageSummary
    id: str
    keys: tuple[str, ...]
    texts: tuple[str, ...]
    shown: tuple[str, ...]
    tag_keys: frozenset[str]
    sort_value: str
    row_id: int
    dangling: bool


@dataclass(slots=True)
class _Criteria:
    q: str | None
    dangling: str | None
    usage: str | None
    binding: str | None
    update: str | None

    @property
    def is_empty(self) -> bool:
        return not (self.q or self.dangling or self.usage or self.binding or self.update)


def automation_plan(environment_id: int, reference_text: str) -> PullPlan:
    """Return the plan of an automated pull; the reference's binding alone chooses the credentials.

    Raises ValidationFailed for a reference that does not parse.
    """
    return PullPlan(
        environment_id, parse_pull_reference(reference_text), CredentialChoice(None), "binding"
    )


def remaining_after_untag(keys: Sequence[str], target_key: str) -> tuple[str, ...]:
    """Return the references left once the daemon has untagged one, as the daemon reckons them.

    Untagging a tag that was the repository's last tag also drops that repository's digests.
    """
    target = parse_reference(target_key)
    left = [parse_reference(key) for key in keys if key != target_key]
    if target.digest is None and not any(
        r.digest is None and r.repository == target.repository for r in left
    ):
        left = [r for r in left if not (r.digest is not None and r.repository == target.repository)]
    return tuple(r.key for r in left)


def require_id(image_id: str) -> None:
    if IMAGE_ID.fullmatch(image_id) is None:
        raise ResourceNotFound("not found")


def distinct_images[T](items: Sequence[T], key: Callable[[T], str]) -> list[T]:
    unique = list({key(item): item for item in items}.values())
    if not unique:
        raise ValidationFailed({"images": "Select at least one image."})
    if len(unique) > MAX_BATCH:
        raise ValidationFailed({"images": f"Select at most {MAX_BATCH} images."})
    return unique


def _from_epoch(seconds: int) -> datetime:
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return _EPOCH


def image_argument(arguments: Arguments) -> str:
    return str(arguments["image_id"])[:64]


def plan_image(arguments: Arguments) -> str:
    return str(arguments["plan"].image_id)


def _clean(value: str | None) -> str | None:
    stripped = None if value is None else value.strip()
    return stripped or None


def _compile(filters: ImageFilters) -> _Criteria:
    fields: dict[str, str] = {}
    text = _clean(filters.q)
    if text is not None and len(text) > MAX_QUERY:
        fields["q"] = f"Search for at most {MAX_QUERY} characters."
    dangling = _clean(filters.dangling)
    if dangling is not None and dangling not in DANGLING_FILTERS:
        fields["dangling"] = "Choose yes or no."
    usage = _clean(filters.usage)
    if usage is not None and usage not in USAGE_FILTERS:
        fields["usage"] = "Choose used or unused."
    binding = _clean(filters.binding)
    if binding is not None and binding not in BINDING_FILTERS:
        fields["binding"] = "Choose one of the listed binding states."
    update = _clean(filters.update)
    if update is not None and update not in UPDATE_FILTERS:
        fields["update"] = "Choose available."
    if fields:
        raise ValidationFailed(fields)
    return _Criteria(None if text is None else text.casefold(), dangling, usage, binding, update)


def _decode_position(cursor: str | None) -> CursorPosition | None:
    if cursor is None:
        return None
    try:
        position = decode_cursor(cursor)
    except InvalidCursorError:
        raise ValidationFailed({"cursor": "The page link is not valid."}) from None
    if not isinstance(position.sort_value, str):
        raise ValidationFailed({"cursor": "The page link is not valid."})
    return position


def _check_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValidationFailed({"limit": f"Choose a page size from 1 to {MAX_PAGE_SIZE}."})
    return limit


def _entry_of(summary: ImageSummary) -> _Entry | None:
    image_id = bare_image_id(summary.id)
    if image_id is None:
        logger.warning("image_list_entry_skipped", extra={"reason": "malformed_id"})
        return None
    tags = _references(summary.repo_tags)
    digests = _references(summary.repo_digests)
    shown = tags or digests
    first = min((reference.familiar for reference in tags), key=str.casefold, default=None)
    return _Entry(
        summary=summary,
        id=image_id,
        keys=tuple(reference.key for reference in (*tags, *digests)),
        texts=tuple(reference.familiar for reference in (*tags, *digests)),
        shown=tuple(reference.key for reference in shown),
        tag_keys=frozenset(reference.key for reference in tags),
        sort_value=DANGLING_SORT_VALUE if first is None else first.casefold(),
        row_id=int(image_id[:ROW_ID_HEX_DIGITS], 16),
        dangling=not tags,
    )


def _references(texts: Sequence[str]) -> list[ImageReference]:
    found: dict[str, ImageReference] = {}
    for text in texts:
        try:
            reference = parse_reference(text)
        except ReferenceInvalid:
            continue
        found.setdefault(reference.key, reference)
    return sorted(found.values(), key=lambda r: r.familiar.casefold())


def _order(entry: _Entry) -> tuple[str, int, str]:
    return entry.sort_value, entry.row_id, entry.id


def usage_of(containers: Sequence[ContainerSummary]) -> dict[str, list[ContainerSummary]]:
    used: dict[str, list[ContainerSummary]] = {}
    for container in containers:
        image_id = bare_image_id(container.image_id)
        if image_id is not None:
            used.setdefault(image_id, []).append(container)
    return used


def _matches(entry: _Entry, criteria: _Criteria, used: int) -> bool:
    if criteria.dangling == "yes" and not entry.dangling:
        return False
    if criteria.dangling == "no" and entry.dangling:
        return False
    if criteria.usage == "used" and used == 0:
        return False
    if criteria.usage == "unused" and used > 0:
        return False
    if criteria.q is not None:
        needle = criteria.q
        return entry.id.startswith(needle) or any(
            needle in text.casefold() or needle in key.casefold()
            for text, key in zip(entry.texts, entry.keys, strict=True)
        )
    return True


def _binding_matches(entry: _Entry, wanted: str, views: Mapping[str, BindingView]) -> bool:
    return any(views[key].state == wanted for key in entry.shown)


def _update_available(entry: _Entry, stored: Mapping[str, StoredUpdateStatus]) -> bool:
    for key in entry.shown:
        state, _ = _shown_check(
            stored.get(key),
            digests_for(entry.summary.repo_digests, parse_reference(key).repository),
        )
        if state == "update_available":
            return True
    return False


def _reference_views(
    keys: Sequence[str],
    views: Mapping[str, BindingView],
    *,
    tags: Collection[str],
    limit: int = MAX_REFERENCES_SHOWN,
    stored: Mapping[str, StoredUpdateStatus] | None = None,
    repo_digests: Sequence[str] | None = None,
) -> tuple[tuple[ImageReferenceView, ...], int]:
    shown = keys[:limit]
    checks = stored or {}
    views_of: list[ImageReferenceView] = []
    for key in shown:
        parsed = parse_reference(key)
        state, checked_at = _shown_check(
            checks.get(key),
            None if repo_digests is None else digests_for(repo_digests, parsed.repository),
        )
        views_of.append(
            ImageReferenceView(
                text=parsed.familiar,
                key=key,
                kind="tag" if key in tags else "digest",
                binding=views[key],
                update_state=state,
                update_checked_at=checked_at,
            )
        )
    return tuple(views_of), max(0, len(keys) - len(shown))


def _shown_check(
    stored: StoredUpdateStatus | None, digests: tuple[str, ...] | None
) -> tuple[str | None, str | None]:
    if stored is None:
        return None, None
    if digests is None or stored.local_digest is None or stored.local_digest in digests:
        return stored.state, stored.checked_at
    if stored.remote_digest is not None and stored.remote_digest in digests:
        return "up_to_date", stored.checked_at
    return None, None


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value[:MAX_DETAIL_ROWS] if isinstance(item, str))


def _joined(arguments: Sequence[str]) -> str:
    return " ".join(arguments)[:2048]


def _variable(pair: str) -> EnvVar:
    key, _, value = pair.partition("=")
    if DEFAULT_SCRUBBER.is_sensitive(key):
        return EnvVar(key, REDACTED, True)
    return EnvVar(key, value, False)


def _label_view(key: str, value: str, viewer_is_admin: bool) -> LabelView:
    if viewer_is_admin:
        return LabelView(key, value, False)
    shown, redacted = redact_label(key, value)
    return LabelView(key, shown, redacted)


def _created_by(text: str) -> str:
    kept = "".join(c for c in text[:MAX_CREATED_BY] if unicodedata.category(c) not in {"Cc", "Cf"})
    return kept[:MAX_CREATED_BY]


def _layer_of(layer_id: str) -> str | None:
    bare = layer_id.removeprefix("sha256:")
    return bare[:12] if IMAGE_ID.fullmatch(bare) else None


async def inspect_image(client: EngineClient, image_id: str) -> ImageInspect:
    """Inspect an image by full id; ResourceNotFound unless the Engine returned that very image."""
    full = f"sha256:{image_id}"
    try:
        inspected = await client.images.inspect(full)
    except NotFound:
        raise ResourceNotFound("not found") from None
    except ResponseTooLarge:
        raise ImageTooLarge() from None
    except EngineError as error:
        raise unavailable_from(error) from None
    if inspected.id != full:
        raise ResourceNotFound("not found")
    return inspected


class ImageQueryService:
    """Reads images through the environment gate; nothing here changes an image."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        hosts: HostClients,
        bindings: ImageBindingService,
        statuses: ImageUpdateStatusRepository | None = None,
    ) -> None:
        self._authoriser = authoriser
        self._hosts = hosts
        self._bindings = bindings
        self._statuses = statuses

    async def require_visible(self, actor: Principal, environment_id: int) -> None:
        """Raise ResourceNotFound unless the caller may see the environment."""
        await self._authoriser.require_environment(actor, environment_id)

    async def count_update_available(self, actor: Principal, environment_id: int) -> int:
        """Count the images `page` lists under the update filter, from one capped image read.

        EnvironmentUnavailable and ImageListTooLarge when the host's images cannot be read in
        full.
        """
        await self._authoriser.require_environment(actor, environment_id)
        images = await read_images(await self._hosts.client(environment_id))
        entries = [entry for entry in map(_entry_of, images) if entry is not None]
        checks = await self._stored_checks(
            environment_id, [key for entry in entries for key in entry.shown]
        )
        return sum(_update_available(entry, checks) for entry in entries)

    async def require_pull_rights(self, actor: Principal, environment_id: int) -> None:
        """Raise unless the caller may pull into the environment.

        ResourceNotFound without a grant on it or when it does not exist; PermissionDenied for a
        standard user on the local socket.
        """
        await self._authoriser.require_create(actor, environment_id)
        await self._hosts.environment(environment_id)

    async def tag_names(
        self, actor: Principal, environment_id: int, prefix: str, *, limit: int
    ) -> list[str]:
        """Return the first `limit` tag references, by name, starting with `prefix`, ignoring case.

        One host read; EnvironmentUnavailable and ImageListTooLarge when the host cannot be read.
        """
        await self._authoriser.require_environment(actor, environment_id)
        client = await self._hosts.client(environment_id)
        wanted = prefix.casefold()
        found = {
            reference.familiar
            for summary in await read_images(client)
            if bare_image_id(summary.id) is not None
            for reference in _references(summary.repo_tags)
            if reference.familiar.casefold().startswith(wanted)
        }
        return sorted(found, key=str.casefold)[:limit]

    async def page(
        self,
        actor: Principal,
        environment_id: int,
        filters: ImageFilters | None = None,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> ImagePage:
        """Return one page of the host's images, read live, with each reference's binding.

        Every image on the host is listed. EnvironmentUnavailable, ImageListTooLarge and
        ContainerListTooLarge when the host cannot be read in full; ValidationFailed for a bad
        filter, limit or cursor. Only an administrator's first page scans for orphaned bindings.
        """
        criteria = _compile(filters or ImageFilters())
        size = _check_limit(limit)
        position = _decode_position(cursor)
        await self._authoriser.require_environment(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        images, containers = await read_host_listing(client)
        users = usage_of(containers)
        entries = [entry for entry in map(_entry_of, images) if entry is not None]
        matched = [e for e in entries if _matches(e, criteria, len(users.get(e.id, ())))]
        if criteria.binding is not None:
            views = await self._bindings.views(
                actor, environment_id, [key for e in matched for key in e.shown]
            )
            matched = [e for e in matched if _binding_matches(e, criteria.binding, views)]
        if criteria.update is not None:
            checks = await self._stored_checks(
                environment_id, [k for e in matched for k in e.shown]
            )
            matched = [e for e in matched if _update_available(e, checks)]
        ordered = sorted(matched, key=_order)
        remaining = (
            ordered
            if position is None
            else [e for e in ordered if _order(e)[:2] > (position.sort_value, position.row_id)]
        )
        window = remaining[:size]
        shown = [key for e in window for key in e.shown]
        views = await self._bindings.views(actor, environment_id, shown)
        stored = await self._stored_checks(environment_id, shown)
        rows = [
            await self._row_of(actor, environment_id, e, users.get(e.id, []), views, stored)
            for e in window
        ]
        next_cursor = None
        if len(remaining) > size and window:
            last = _order(window[-1])
            next_cursor = encode_cursor(CursorPosition(last[0], last[1]))
        orphans = (
            await self._bindings.orphaned(
                actor, environment_id, await resolve_live_references(client, images, containers)
            )
            if cursor is None and actor.is_administrator
            else None
        )
        return ImagePage(
            page=Page(rows, next_cursor),
            total=len(ordered),
            limit=size,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            can_create=await self._authoriser.may_create(actor, environment_id),
            can_remove=actor.is_administrator,
            orphans=orphans,
        )

    async def origin_name(self, actor: Principal, environment_id: int, image_id: str) -> str:
        """Return the first tag of an image, or its short id; ResourceNotFound unless readable."""
        require_id(image_id)
        await self._authoriser.require_environment(actor, environment_id)
        inspected = await inspect_image(await self._hosts.client(environment_id), image_id)
        return inspected.repo_tags[0] if inspected.repo_tags else short_id(image_id)

    async def detail(self, actor: Principal, environment_id: int, image_id: str) -> ImageDetail:
        """Return one image for the detail page; ResourceNotFound unless the caller may see it."""
        require_id(image_id)
        await self._authoriser.require_environment(actor, environment_id)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        inspected = await inspect_image(client, image_id)
        using = await self._containers_using(client, image_id)
        tags = canonical_keys(inspected.repo_tags)
        keys = canonical_keys(inspected.repo_tags, inspected.repo_digests)
        views = await self._bindings.views(actor, environment_id, keys)
        references, hidden = _reference_views(
            keys,
            views,
            tags=set(tags),
            stored=await self._stored_checks(environment_id, keys),
            repo_digests=inspected.repo_digests,
        )
        names, names_hidden = await self._names(actor, environment_id, using)
        config = inspected.config
        viewer_is_admin = actor.is_administrator
        labels = config.get("Labels")
        pairs = (
            sorted((k, v) for k, v in labels.items() if isinstance(k, str) and isinstance(v, str))
            if isinstance(labels, dict)
            else []
        )
        env_pairs = _strings(config.get("Env"))
        ports = config.get("ExposedPorts")
        exposed = (
            tuple(sorted(str(p) for p in ports)[:MAX_DETAIL_ROWS])
            if isinstance(ports, dict)
            else ()
        )
        working = config.get("WorkingDir")
        user = config.get("User")
        return ImageDetail(
            environment_id=environment_id,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            id=image_id,
            short_id=short_id(image_id),
            references=references,
            references_hidden=hidden,
            size=inspected.size,
            created_at=parse_engine_time(inspected.created),
            architecture=inspected.architecture,
            os=inspected.os,
            containers=names,
            containers_total=len(using),
            containers_hidden=names_hidden,
            command=_joined(_strings(config.get("Cmd"))) if viewer_is_admin else None,
            entrypoint=_joined(_strings(config.get("Entrypoint"))) if viewer_is_admin else None,
            variables=tuple(_variable(pair) for pair in env_pairs) if viewer_is_admin else None,
            labels=tuple(_label_view(k, v, viewer_is_admin) for k, v in pairs[:MAX_DETAIL_ROWS]),
            working_dir=working if isinstance(working, str) else "",
            user=user if isinstance(user, str) else "",
            exposed_ports=exposed,
            can_create=await self._authoriser.may_create(actor, environment_id),
            can_remove=viewer_is_admin,
            truncated=len(pairs) > MAX_DETAIL_ROWS or len(env_pairs) >= MAX_DETAIL_ROWS,
        )

    async def history(
        self, actor: Principal, environment_id: int, image_id: str
    ) -> tuple[HistoryRow, ...]:
        """Return the image's layers, newest first; the build commands are administrators' only."""
        require_id(image_id)
        await self._authoriser.require_environment(actor, environment_id)
        client = await self._hosts.client(environment_id)
        await inspect_image(client, image_id)
        try:
            layers = await client.images.history(f"sha256:{image_id}")
        except NotFound:
            raise ResourceNotFound("not found") from None
        except ResponseTooLarge:
            raise ImageTooLarge() from None
        except EngineError as error:
            raise unavailable_from(error) from None
        return tuple(
            HistoryRow(
                layer=_layer_of(layer.id),
                created_at=_from_epoch(layer.created),
                created_by=_created_by(layer.created_by) if actor.is_administrator else None,
                tags=tuple(layer.tags[:MAX_REFERENCES_SHOWN]),
                size=layer.size,
            )
            for layer in layers
        )

    async def remove_impact(
        self, actor: Principal, environment_id: int, image_id: str
    ) -> RemoveImpact:
        """Name what removing the image takes away; administrators only."""
        client, inspected, environment_name = await self._for_removal(
            actor, environment_id, image_id
        )
        keys = canonical_keys(inspected.repo_tags, inspected.repo_digests)
        using = await self._containers_using(client, image_id)
        names, hidden = await self._names(actor, environment_id, using)
        return RemoveImpact(
            environment_id=environment_id,
            environment_name=environment_name,
            image_id=image_id,
            short_id=short_id(image_id),
            size=inspected.size,
            references=tuple(parse_reference(k).familiar for k in keys[:MAX_REFERENCES_SHOWN]),
            references_total=len(keys),
            references_digest=references_digest(keys),
            in_use=len(using),
            containers=names,
            containers_hidden=hidden,
        )

    async def batch_remove_confirmation(
        self, actor: Principal, environment_id: int, image_ids: Sequence[str]
    ) -> ImageBatchConfirmation:
        """Name every image of a batch removal that exists; administrators only.

        ValidationFailed when nothing is selected or more than 25 images are. An id that is not an
        image of the host is only counted.
        """
        unique = distinct_images(image_ids, str)
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        usage = usage_of(await read_containers(client))
        targets: list[ImageBatchTargetView] = []
        unavailable = 0
        for image_id in unique:
            try:
                require_id(image_id)
                inspected = await inspect_image(client, image_id)
            except ResourceNotFound:
                unavailable += 1
                continue
            keys = canonical_keys(inspected.repo_tags, inspected.repo_digests)
            using = usage.get(image_id, [])
            names, hidden = await self._names(actor, environment_id, using)
            targets.append(
                ImageBatchTargetView(
                    image_id=image_id,
                    short_id=short_id(image_id),
                    size=inspected.size,
                    references=tuple(
                        parse_reference(k).familiar for k in keys[:MAX_REFERENCES_SHOWN]
                    ),
                    references_total=len(keys),
                    references_digest=references_digest(keys),
                    in_use=len(using),
                    containers=names,
                    containers_hidden=hidden,
                )
            )
        return ImageBatchConfirmation(
            environment_id=environment_id,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            targets=tuple(targets),
            unavailable=unavailable,
        )

    async def untag_impact(
        self, actor: Principal, environment_id: int, image_id: str, reference_text: str
    ) -> UntagImpact:
        """Name what untagging one reference does; administrators only.

        What remains is worked out as the daemon does: untagging the last tag of a repository also
        drops that repository's digest references. ResourceNotFound when the reference is not one
        of the image's.
        """
        client, inspected, environment_name = await self._for_removal(
            actor, environment_id, image_id
        )
        reference = parse_pull_reference(reference_text)
        keys = canonical_keys(inspected.repo_tags, inspected.repo_digests)
        if reference.key not in keys:
            raise ResourceNotFound("not found")
        using = await self._containers_using(client, image_id)
        names, hidden = await self._names(actor, environment_id, using)
        is_last = not remaining_after_untag(keys, reference.key)
        return UntagImpact(
            environment_id=environment_id,
            environment_name=environment_name,
            image_id=image_id,
            short_id=short_id(image_id),
            reference=reference.familiar,
            reference_key=reference.key,
            is_last=is_last,
            deletes_image=is_last and not using,
            in_use=len(using),
            containers=names,
            containers_hidden=hidden,
        )

    async def tag_impact(
        self, actor: Principal, environment_id: int, image_id: str, target_text: str
    ) -> TagImpact:
        """Name what giving the image a new reference does, including an image that loses it.

        Administrators only.
        """
        require_id(image_id)
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        await inspect_image(client, image_id)
        reference = parse_tag_target(target_text)
        replaced = await holder_of(client, reference)
        shown: ReplacedImage | None = None
        if replaced is not None and replaced.id != image_id:
            shown = ReplacedImage(
                replaced.id,
                short_id(replaced.id),
                tuple(
                    parse_reference(k).familiar
                    for k in canonical_keys(replaced.inspect.repo_tags)[:MAX_REFERENCES_SHOWN]
                ),
            )
        return TagImpact(
            environment_id=environment_id,
            environment_name=environment.name,
            image_id=image_id,
            short_id=short_id(image_id),
            reference=reference.familiar,
            reference_key=reference.key,
            replaced=shown,
            unchanged=replaced is not None and replaced.id == image_id,
        )

    async def prune_plan(self, actor: Principal, environment_id: int, mode: PruneMode) -> PrunePlan:
        """List the images a prune would remove, at most 100; administrators only."""
        if mode not in PRUNE_MODES:
            raise ValidationFailed({"mode": "Choose dangling or unused."})
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        images, containers = await read_host_listing(client)
        found = prune_candidates(images, containers, mode)
        return PrunePlan(
            environment_id, environment.name, mode, tuple(found[:MAX_PRUNE]), len(found)
        )

    async def _for_removal(
        self, actor: Principal, environment_id: int, image_id: str
    ) -> tuple[EngineClient, ImageInspect, str]:
        require_id(image_id)
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        environment = await self._hosts.environment(environment_id)
        client = await self._hosts.client(environment_id)
        return client, await inspect_image(client, image_id), environment.name

    async def _containers_using(
        self, client: EngineClient, image_id: str
    ) -> list[ContainerSummary]:
        return usage_of(await read_containers(client)).get(image_id, [])

    async def _names(
        self, actor: Principal, environment_id: int, using: Sequence[ContainerSummary]
    ) -> tuple[tuple[ContainerUse, ...], int]:
        visible = await self._authoriser.filter_visible(
            actor, [ResourceRef(environment_id, "container", c.id, c.labels) for c in using]
        )
        allowed = {ref.resource_id for ref in visible}
        shown = [c for c in using if c.id in allowed][:MAX_CONTAINERS_SHOWN]
        return (
            tuple(ContainerUse(c.id, short_id(c.id), display_name(c.names, c.id)) for c in shown),
            len(using) - len(shown),
        )

    async def _stored_checks(
        self, environment_id: int, keys: Sequence[str]
    ) -> dict[str, StoredUpdateStatus]:
        if self._statuses is None or not keys:
            return {}
        return await self._statuses.for_references(environment_id, list(dict.fromkeys(keys)))

    async def _row_of(
        self,
        actor: Principal,
        environment_id: int,
        entry: _Entry,
        using: Sequence[ContainerSummary],
        views: Mapping[str, BindingView],
        stored: Mapping[str, StoredUpdateStatus],
    ) -> ImageRow:
        references, hidden = _reference_views(
            entry.shown,
            views,
            tags=entry.tag_keys,
            stored=stored,
            repo_digests=entry.summary.repo_digests,
        )
        names, names_hidden = await self._names(actor, environment_id, using)
        return ImageRow(
            environment_id=environment_id,
            id=entry.id,
            short_id=short_id(entry.id),
            references=references,
            references_hidden=hidden,
            size=entry.summary.size,
            created_at=_from_epoch(entry.summary.created),
            dangling=entry.dangling,
            in_use=len(using),
            containers=names,
            containers_hidden=names_hidden,
        )


@dataclass(frozen=True, slots=True)
class Holder:
    id: str
    inspect: ImageInspect


def parse_tag_target(text: str) -> ImageReference:
    """Parse the reference a tag would create; a digest cannot be created, only a tag."""
    reference = parse_pull_reference(text)
    if reference.pinned:
        raise ValidationFailed({"reference": "A tag is needed; a digest cannot be assigned."})
    return reference


async def holder_of(client: EngineClient, reference: ImageReference) -> Holder | None:
    """Return the image that currently holds a tag, or None; ImageTooLarge past the read limit."""
    try:
        inspected = await client.images.inspect(reference.key)
    except NotFound:
        return None
    except ResponseTooLarge:
        raise ImageTooLarge() from None
    except EngineError as error:
        raise unavailable_from(error) from None
    bare = bare_image_id(inspected.id)
    if bare is None or reference.key not in canonical_keys(inspected.repo_tags):
        return None
    return Holder(bare, inspected)


def prune_candidates(
    images: Sequence[ImageSummary], containers: Sequence[ContainerSummary], mode: PruneMode
) -> list[PruneCandidate]:
    users = usage_of(containers)
    found: list[PruneCandidate] = []
    for summary in images:
        entry = _entry_of(summary)
        if entry is None or users.get(entry.id):
            continue
        if mode == "dangling" and not entry.dangling:
            continue
        found.append(
            PruneCandidate(
                id=entry.id,
                short_id=short_id(entry.id),
                references=entry.texts[:MAX_REFERENCES_SHOWN],
                references_total=len(entry.keys),
                references_digest=references_digest(entry.keys),
                size=summary.size,
                created_at=_from_epoch(summary.created),
            )
        )
    found.sort(key=lambda c: (-c.size, c.id))
    return found
