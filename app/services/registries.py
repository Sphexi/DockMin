"""Registry credential sets: create, edit, delete, test, and who may use them."""

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, cast

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.clocked import Clock, parse_timestamp
from app.db.repositories.environment import EnvironmentRepository
from app.db.repositories.environment_access import MAX_GRANT_LISTING, EnvironmentAccessRepository
from app.db.repositories.errors import DuplicateError, MissingReferenceError
from app.db.repositories.image_binding import (
    AffectedBinding,
    ImageBindingRepository,
    MissingReason,
)
from app.db.repositories.pagination import Page
from app.db.repositories.registry_access import (
    MAX_GRANTED_ENVIRONMENTS,
    GrantedEnvironment,
    OfferedCredentialSet,
    RegistryAccessRepository,
)
from app.db.repositories.registry_credential import (
    AddressMatch,
    CredentialFields,
    RegistryCredentialRepository,
    RegistryListEntry,
    RegistryListRepository,
    SecretUnreadableError,
    StoredRegistryCredential,
)
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.registry_client.probe import (
    RegistryLogin,
    RegistryProbeCoordinator,
    RegistryProbeResult,
    RegistryTarget,
)
from app.registry_client.providers import PROVIDERS, ProviderKind, SecretKind
from app.registry_client.references import ReferenceInvalid, parse_reference
from app.services.activity import ActivityRecorder, audited, current_audit
from app.services.environment_access import (
    EnvironmentAccessService,
    Grantee,
    GrantList,
    resolve_grantee,
)
from app.services.environment_connector import StageReport
from app.services.guards import require_administrator
from app.services.registry_input import (
    CHANGED_SINCE_REVIEW,
    NAME_EXISTS,
    PROVIDER_ORDER,
    CaCertificateSummary,
    CheckedBundle,
    ProviderShape,
    RegistrySpec,
    check_ca_bundle,
    extras_defaults,
    extras_for_form,
    plan_registry,
    provider_shape,
    summarise_ca_bundle,
)

DEFAULT_PAGE_SIZE: Final = 25
PULLED_NOTE: Final = "Images already pulled and containers already running are not touched."
BINDINGS_NOTE: Final = (
    'The bindings stay and read "credential set deleted". Automatic updates for these images '
    "stop until each is pulled again with another credential set."
)
MAX_AFFECTED_SHOWN: Final = 500
ACTIVITY_NOTE: Final = "Activity history is kept."
NOT_OFFERED_NOTE: Final = "Until it is granted to an environment, it is offered nowhere."
SECRET_UNREADABLE: Final = (
    "The stored secret cannot be read with the current encryption key. Enter it again."
)
_PROVIDER_DEFAULT: Final[ProviderKind] = PROVIDER_ORDER[0]


@dataclass(frozen=True, slots=True)
class RegistryListRow:
    """One credential set on the list page; it carries no username and no secret."""

    id: int
    name: str
    provider: ProviderKind
    address: str
    authentication_enabled: bool
    verify_tls: bool
    secret_kind: SecretKind
    environment_count: int
    grantee_count: int
    is_default_for_host: bool
    seeded_default: bool


@dataclass(frozen=True, slots=True)
class DuplicateReference:
    """Another set that uses the same address and user name."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class DerivedSecretStatus:
    """The cached short-lived secret of a derived set: its stamps only, never the token."""

    cached: bool
    expires_at: str | None
    refreshed_at: str | None
    expired: bool


@dataclass(frozen=True, slots=True)
class EnvironmentReference:
    """An environment named in a grant form."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class RegistryDetail:
    """A credential set for its page; the secret is only ever reported as stored or not."""

    id: int
    name: str
    provider: ProviderKind
    address: str
    authentication_enabled: bool
    username: str | None
    secret_kind: SecretKind
    secret_stored: bool
    verify_tls: bool
    ca_bundle: tuple[CaCertificateSummary, ...]
    extras: Mapping[str, str | bool]
    derived: DerivedSecretStatus | None
    environments: tuple[GrantedEnvironment, ...]
    environments_total: int
    access: GrantList
    duplicates: tuple[DuplicateReference, ...]
    is_default_for_host: bool
    seeded_default: bool
    bound_images: int
    created_at: str
    updated_at: str

    @property
    def tls_unverified(self) -> bool:
        """Report whether certificates are not checked for this set."""
        return not self.verify_tls

    @property
    def offered_nowhere(self) -> bool:
        """Report whether no environment can use the set: it has no environment grant."""
        return self.environments_total == 0


@dataclass(frozen=True, slots=True)
class RegistryFormState:
    """Everything one render of the form needs; it holds no secret.

    `secret_stored` is true only when a blank secret would keep the stored one: an edit that
    has not changed provider. Extras holds only the selected provider's fields, as strings and
    flags. `ca_bundle` summarises the stored bundle; its PEM is never rendered. `bound_images`
    counts the images bound to the set, which a change of address moves to credential_missing.
    """

    credential_id: int | None
    shape: ProviderShape
    name: str
    address: str
    authentication_enabled: bool
    username: str
    secret_stored: bool
    verify_tls: bool
    ca_bundle: tuple[CaCertificateSummary, ...]
    extras: Mapping[str, str | bool]
    providers: tuple[ProviderKind, ...] = PROVIDER_ORDER
    bound_images: int = 0

    @property
    def provider(self) -> ProviderKind:
        """Return the selected provider."""
        return self.shape.kind


@dataclass(frozen=True, slots=True)
class AffectedImage:
    """An image reference whose binding a deletion moves to credential_missing."""

    environment_id: int
    environment_name: str
    reference: str


@dataclass(frozen=True, slots=True)
class EnvironmentImageCount:
    """How many bound images one environment holds for a set."""

    environment_name: str
    count: int


@dataclass(frozen=True, slots=True)
class RegistryDeleteImpact:
    """What a deletion names: the set, who loses it, the images it leaves unbound, and what stays.

    `affected` lists up to 500 images by environment name then reference; `affected_total` is
    the count the confirmation must post back as `expected_bindings`. `per_environment` is set
    only when the list is cut, so each environment's share of the rest is stated.
    """

    credential_id: int
    name: str
    provider: ProviderKind
    address: str
    environments: tuple[GrantedEnvironment, ...]
    environments_total: int
    access: GrantList
    affected: tuple[AffectedImage, ...]
    affected_total: int
    per_environment: tuple[EnvironmentImageCount, ...]
    pulled_note: str = PULLED_NOTE
    bindings_note: str = BINDINGS_NOTE
    activity_note: str = ACTIVITY_NOTE

    @property
    def affected_hidden(self) -> int:
        """Return how many affected images the list leaves out."""
        return max(0, self.affected_total - len(self.affected))


@dataclass(frozen=True, slots=True)
class RegistryTestReport:
    """The result of a connectivity test, run from this server."""

    passed: bool
    failed_stage: str | None
    message: str
    duration_ms: int
    stages: tuple[StageReport, ...]
    tls_verified: bool


class RegistryCredentialService:
    """Applies credential set rules; every method takes the acting principal and decides itself."""

    def __init__(
        self,
        *,
        db: Session,
        credentials: RegistryCredentialRepository,
        listing: RegistryListRepository,
        access: RegistryAccessRepository,
        bindings: ImageBindingRepository,
        environments: EnvironmentRepository,
        environment_access: EnvironmentAccessRepository,
        grantees: EnvironmentAccessService,
        users: UserRepository,
        teams: TeamRepository,
        probes: RegistryProbeCoordinator,
        activity: ActivityRecorder,
        clock: Clock,
    ) -> None:
        self._db = db
        self._credentials = credentials
        self._list = listing
        self._access = access
        self._bindings = bindings
        self._environments = environments
        self._environment_access = environment_access
        self._grantees = grantees
        self._users = users
        self._teams = teams
        self._probes = probes
        self._activity = activity
        self._clock = clock

    async def page(
        self, actor: Principal, *, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
    ) -> Page[RegistryListRow]:
        """Return one page of credential sets by name; administrators only."""
        require_administrator(actor)
        page = await self._list.page(limit=limit, cursor=cursor)
        return Page([_row_of(entry) for entry in page.items], page.next_cursor)

    async def count(self, actor: Principal) -> int:
        """Count the credential sets page lists; administrators only."""
        require_administrator(actor)
        return await self._list.count()

    async def detail(self, actor: Principal, credential_id: int) -> RegistryDetail:
        """Return a credential set with its grants and duplicate warnings; administrators only."""
        require_administrator(actor)
        return await self._detail_of(await self._require(credential_id))

    async def form_context(
        self, actor: Principal, *, credential_id: int | None = None, provider: str | None = None
    ) -> RegistryFormState:
        """Return the form for a new set, or for editing one; administrators only.

        A provider that differs from the stored one starts from that provider's defaults and
        drops the secret, since changing provider requires entering it again.
        """
        require_administrator(actor)
        if credential_id is None:
            return _blank_state(_known(provider) or _PROVIDER_DEFAULT)
        stored = await self._require(credential_id)
        kind = _known(provider) or stored.provider
        state = replace(
            _state_from_stored(stored),
            bound_images=await self._bindings.count_for_credential(credential_id),
        )
        if kind == stored.provider:
            return await self._with_bundle(state, stored)
        return replace(_switched_state(state, kind), bound_images=state.bound_images)

    async def form_state(
        self, actor: Principal, spec: RegistrySpec, *, credential_id: int | None = None
    ) -> RegistryFormState:
        """Return the form as the submitted values describe it, for a re-render; no write.

        The secret is never part of it. For an edit, the stored bundle and whether a blank
        secret would be kept come from the saved set.
        """
        require_administrator(actor)
        kind = _known(spec.provider) or _PROVIDER_DEFAULT
        shape = provider_shape(kind)
        stored = None if credential_id is None else await self._require(credential_id)
        state = RegistryFormState(
            credential_id=credential_id,
            shape=shape,
            name=spec.name,
            address=shape.fixed_address or spec.address.strip() or shape.default_address or "",
            authentication_enabled=spec.authentication_enabled or not shape.anonymous_allowed,
            username=spec.username,
            secret_stored=stored is not None and stored.has_secret and stored.provider == kind,
            verify_tls=spec.verify_tls or not shape.tls_options_allowed,
            ca_bundle=(),
            extras={
                **extras_defaults(kind),
                **{k: v for k, v in spec.extras.items() if k in shape.extras_fields},
            },
            bound_images=0
            if stored is None
            else await self._bindings.count_for_credential(stored.id),
        )
        if stored is None or not shape.tls_options_allowed:
            return state
        return await self._with_bundle(state, stored)

    async def delete_impact(self, actor: Principal, credential_id: int) -> RegistryDeleteImpact:
        """Name what deleting a set takes away, before anything is deleted; administrators only."""
        require_administrator(actor)
        stored = await self._require(credential_id)
        environments, total = await self._environments_of(credential_id)
        listed = await self._bindings.list_affected(credential_id, limit=MAX_AFFECTED_SHOWN)
        count = (
            len(listed)
            if len(listed) < MAX_AFFECTED_SHOWN
            else await self._bindings.count_for_credential(credential_id)
        )
        per_environment: tuple[EnvironmentImageCount, ...] = ()
        if count > len(listed):
            per_environment = tuple(
                EnvironmentImageCount(row.environment_name, row.count)
                for row in await self._bindings.counts_by_environment(credential_id)
            )
        return RegistryDeleteImpact(
            credential_id=stored.id,
            name=stored.name,
            provider=stored.provider,
            address=stored.url,
            environments=environments,
            environments_total=total,
            access=await self._grant_list(credential_id),
            affected=tuple(_affected_image(row) for row in listed),
            affected_total=count,
            per_environment=per_environment,
        )

    async def eligible_for_pull(
        self, principal: Principal, environment_id: int
    ) -> tuple[OfferedCredentialSet, ...]:
        """List the sets this caller may pull with on an environment, by name.

        A set must be granted to the environment. A standard user must also see the environment
        and hold a user or team grant on the set; an administrator needs neither. Raises
        ResourceNotFound for a standard user who cannot see the environment. Not an
        administrator-only read.
        """
        user_id = None
        if not principal.is_administrator:
            if await self._environment_access.gate(principal.user_id, environment_id) is None:
                raise ResourceNotFound("not found")
            user_id = principal.user_id
        return tuple(await self._access.eligible_for_pull(environment_id, user_id=user_id))

    async def resolve_grantee(
        self, actor: Principal, *, username: str | None = None, team_name: str | None = None
    ) -> Grantee:
        """Find the one enabled standard user or team a grant form names, by exact name."""
        return await self._grantees.resolve_grantee(actor, username=username, team_name=team_name)

    async def resolve_environment(self, actor: Principal, name: str) -> EnvironmentReference:
        """Find the environment a grant form names, by exact name."""
        require_administrator(actor)
        environment = await self._environments.get_by_name(name.strip())
        if environment is None:
            raise ValidationFailed({"environment": "No environment has that name."})
        return EnvironmentReference(environment.id, environment.name)

    @audited(action="registry_credential_created", target_type="registry_credential")
    async def create(self, actor: Principal, spec: RegistrySpec) -> RegistryDetail:
        """Create a credential set; administrators only.

        Anonymous sets need no user name or secret. Two sets may share an address and user name;
        the page of each then warns, and nothing is refused.
        """
        require_administrator(actor)
        bundle, spec = await self._checked_bundle(spec)
        plan = plan_registry(spec, None, bundle)
        fields = plan.fields
        audit = current_audit()
        audit.detail.update(
            name=fields.name,
            provider=fields.provider,
            address=fields.url,
            authentication_enabled=fields.authentication_enabled,
            verify_tls=fields.verify_tls,
            ca_bundle=fields.ca_bundle is not None,
        )
        async with self._db.transaction():
            await self._refuse_folded_name(fields.name, excluding=None)
            try:
                created = await self._credentials.create(fields, plan.secret)
            except DuplicateError:
                raise ValidationFailed({"name": NAME_EXISTS}) from None
            audit.target_id = str(created.id)
            audit.detail["duplicates"] = len(await self._duplicates_of(created))
        return await self._detail_of(created)

    @audited(
        action="registry_credential_updated",
        target_type="registry_credential",
        target_id=lambda a: str(a["credential_id"]),
    )
    async def update(
        self, actor: Principal, credential_id: int, spec: RegistrySpec
    ) -> RegistryDetail:
        """Change a credential set; administrators only.

        A blank secret keeps the stored one, except when the provider, address, region, GitLab
        instance URL, certificate bundle or certificate verification changes, which requires it
        again. Every edit clears the cached derived secret. A new address clears the set's
        default-for-host flag and moves every image bound to it to credential_missing. Turning
        authentication on, or changing the provider or address, removes the mark that the set
        was added with the instance.
        """
        require_administrator(actor)
        await self._require(credential_id)
        bundle, spec = await self._checked_bundle(spec)
        audit = current_audit()
        async with self._db.transaction():
            current = await self._credentials.get(credential_id)
            if current is None:
                raise ResourceNotFound("not found")
            plan = plan_registry(spec, current, bundle)
            updated = current
            if not plan.changed and not plan.secret_replaced:
                audit.unchanged()
            else:
                if plan.fields.name != current.name:
                    await self._refuse_folded_name(plan.fields.name, excluding=credential_id)
                moved_address = plan.fields.url != current.url
                if moved_address and current.is_default_for_host:
                    await self._credentials.set_default_for_host(credential_id, enabled=False)
                    audit.detail["default_cleared"] = True
                try:
                    if plan.keep_secret:
                        written = await self._credentials.update_keeping_secret(
                            credential_id, plan.fields
                        )
                    else:
                        written = await self._credentials.update_replacing_secret(
                            credential_id, plan.fields, plan.secret
                        )
                except DuplicateError:
                    raise ValidationFailed({"name": NAME_EXISTS}) from None
                if not written:
                    raise ResourceNotFound("not found")
                if current.seeded_default and _leaves_default(current, plan.fields):
                    await self._credentials.clear_seeded_default(credential_id)
                    audit.detail["seeded_default_cleared"] = True
                updated = await self._require(credential_id)
                audit.detail.update(
                    name=updated.name,
                    changed=", ".join(plan.changed),
                    secret_replaced=plan.secret_replaced,
                    duplicates=len(await self._duplicates_of(updated)),
                )
                if updated.url != current.url:
                    audit.detail["address"] = updated.url
                    audit.detail["bindings_marked_missing"] = await self._move_bindings(
                        actor, updated, "address_changed"
                    )
        return await self._detail_of(updated)

    @audited(
        action="registry_credential_deleted",
        target_type="registry_credential",
        target_id=lambda a: str(a["credential_id"]),
    )
    async def delete(
        self, actor: Principal, credential_id: int, *, expected_name: str, expected_bindings: int
    ) -> None:
        """Delete a credential set and its grants; administrators only.

        Deleted only while its name is still expected_name and the number of images bound to it is
        still expected_bindings, the two the caller reviewed. Those bindings move to
        credential_missing in the same transaction, one related row each; none is deleted.
        """
        require_administrator(actor)
        await self._require(credential_id)
        audit = current_audit()
        async with self._db.transaction():
            stored = await self._credentials.get(credential_id)
            if stored is None:
                raise ResourceNotFound("not found")
            if stored.name != expected_name:
                raise ValidationFailed({"name": CHANGED_SINCE_REVIEW})
            if await self._bindings.count_for_credential(credential_id) != expected_bindings:
                audit.reason = "bindings_changed"
                raise ValidationFailed({"bindings": CHANGED_SINCE_REVIEW})
            moved = await self._move_bindings(actor, stored, "deleted")
            await self._credentials.delete(credential_id)
            audit.detail.update(
                name=stored.name,
                provider=stored.provider,
                address=stored.url,
                bindings_marked_missing=moved,
            )

    @audited(
        action=lambda a: (
            "registry_credential_default_set"
            if a["enabled"]
            else "registry_credential_default_cleared"
        ),
        target_type="registry_credential",
        target_id=lambda a: str(a["credential_id"]),
    )
    async def set_default_for_host(
        self, actor: Principal, credential_id: int, *, enabled: bool
    ) -> None:
        """Mark a set as its registry address's default, or clear the mark; administrators only.

        Setting it clears any other default at the same address first. The default only
        preselects a form, and never decides a pull.
        """
        require_administrator(actor)
        await self._require(credential_id)
        audit = current_audit()
        async with self._db.transaction():
            stored = await self._credentials.get(credential_id)
            if stored is None:
                raise ResourceNotFound("not found")
            audit.detail.update(name=stored.name, address=stored.url)
            if stored.is_default_for_host == enabled:
                audit.unchanged()
                return
            if enabled:
                await self._credentials.clear_default_at(stored.url, excluding=credential_id)
            try:
                await self._credentials.set_default_for_host(credential_id, enabled=enabled)
            except DuplicateError:
                raise ValidationFailed({"default": CHANGED_SINCE_REVIEW}) from None

    @audited(
        action="registry_credential_tested",
        target_type="registry_credential",
        target_id=lambda a: str(a["credential_id"]),
    )
    async def test_connection(self, actor: Principal, credential_id: int) -> RegistryTestReport:
        """Test a saved credential set from this server; administrators only.

        Only the saved set is tested, with its stored secret, so no secret is ever sent to an
        address that was not saved. A failed test is a result, not an error, and never blocks
        saving. A derived set performs a fresh exchange whose token is discarded.
        """
        require_administrator(actor)
        unreadable = False
        loaded = None
        try:
            loaded = await self._credentials.fetch_revealed(credential_id)
        except SecretUnreadableError:
            unreadable = True
        if unreadable:
            raise ValidationFailed({"secret": SECRET_UNREADABLE})
        if loaded is None:
            raise ResourceNotFound("not found")
        stored, secrets = loaded
        login = None
        if stored.authentication_enabled and stored.username and secrets.secret is not None:
            login = RegistryLogin(stored.username, secrets.secret)
        target = RegistryTarget(
            provider=stored.provider,
            address=stored.url,
            verify_tls=stored.verify_tls,
            ca_bundle=stored.ca_bundle,
            extras=stored.extras,
        )
        result = await self._probes.probe(target, login, key=(stored.id, _fingerprint(stored)))
        current_audit().detail.update(
            address=stored.url,
            passed=result.passed,
            failed_stage=None if result.failed_stage is None else result.failed_stage.value,
            duration_ms=result.duration_ms,
        )
        return _report_of(result, stored.verify_tls)

    @audited(
        action="registry_credential_environment_granted",
        target_type="registry_credential",
        target_id=lambda a: str(a["credential_id"]),
        environment_id=lambda a: int(a["environment_id"]),
    )
    async def grant_environment(
        self, actor: Principal, credential_id: int, environment_id: int
    ) -> None:
        """Grant a credential set to an environment; administrators only."""
        require_administrator(actor)
        await self._require(credential_id)
        environment = await self._environments.get(environment_id)
        if environment is None:
            raise ValidationFailed({"environment": "That environment no longer exists."})
        async with self._db.transaction():
            try:
                await self._access.add_environment(credential_id, environment_id)
            except DuplicateError:
                raise ValidationFailed(
                    {"environment": "That environment already has this credential set."}
                ) from None
            except MissingReferenceError:
                raise ValidationFailed(
                    {"environment": "That environment no longer exists."}
                ) from None
            current_audit().detail["environment_name"] = environment.name

    @audited(
        action="registry_credential_environment_revoked",
        target_type="registry_credential",
        target_id=lambda a: str(a["credential_id"]),
        environment_id=lambda a: int(a["environment_id"]),
    )
    async def revoke_environment(
        self, actor: Principal, credential_id: int, environment_id: int
    ) -> None:
        """Withdraw a credential set from an environment; administrators only."""
        require_administrator(actor)
        await self._require(credential_id)
        environment = await self._environments.get(environment_id)
        if environment is None:
            raise ValidationFailed({"environment": "That environment no longer exists."})
        async with self._db.transaction():
            if not await self._access.remove_environment(credential_id, environment_id):
                raise ValidationFailed(
                    {"environment": "That environment does not have this credential set."}
                )
            current_audit().detail["environment_name"] = environment.name

    @audited(
        action="registry_credential_access_granted",
        target_type="registry_credential",
        target_id=lambda a: str(a["credential_id"]),
    )
    async def grant(
        self,
        actor: Principal,
        credential_id: int,
        *,
        user_id: int | None = None,
        team_id: int | None = None,
    ) -> None:
        """Let a user or a team use a credential set; administrators only."""
        require_administrator(actor)
        await self._require(credential_id)
        grantee = await resolve_grantee(self._users, self._teams, user_id=user_id, team_id=team_id)
        async with self._db.transaction():
            try:
                if grantee.kind == "user":
                    await self._access.add_user(credential_id, grantee.id)
                else:
                    await self._access.add_team(credential_id, grantee.id)
            except DuplicateError:
                raise ValidationFailed({"grantee": "That grant already exists."}) from None
            except MissingReferenceError:
                raise ValidationFailed({"grantee": "The grantee no longer exists."}) from None
            current_audit().detail.update(grantee_kind=grantee.kind, grantee_name=grantee.name)

    @audited(
        action="registry_credential_access_revoked",
        target_type="registry_credential",
        target_id=lambda a: str(a["credential_id"]),
    )
    async def revoke(
        self,
        actor: Principal,
        credential_id: int,
        *,
        user_id: int | None = None,
        team_id: int | None = None,
    ) -> None:
        """Withdraw a user's or a team's grant on a credential set; administrators only."""
        require_administrator(actor)
        await self._require(credential_id)
        grantee = await resolve_grantee(self._users, self._teams, user_id=user_id, team_id=team_id)
        async with self._db.transaction():
            if grantee.kind == "user":
                removed = await self._access.remove_user(credential_id, grantee.id)
            else:
                removed = await self._access.remove_team(credential_id, grantee.id)
            if not removed:
                raise ValidationFailed({"grantee": "That grant does not exist."})
            current_audit().detail.update(grantee_kind=grantee.kind, grantee_name=grantee.name)

    async def _move_bindings(
        self, actor: Principal, stored: StoredRegistryCredential, reason: MissingReason
    ) -> int:
        """Move the set's bindings to credential_missing and write one related row for each.

        Runs inside the caller's transaction, so the rows commit with the delete or the edit.
        """
        refs = []
        after = 0
        while page := await self._bindings.refs_for_credential(stored.id, after=after):
            refs.extend(page)
            after = page[-1].id
        if not refs:
            return 0
        moved = await self._bindings.mark_missing_for_credential(
            stored.id, name=stored.name, reason=reason, now=self._clock()
        )
        if moved != len(refs):
            raise RuntimeError("the bindings of a credential set changed inside its transaction")
        for ref in refs:
            await self._activity.record_related(
                actor,
                action="image_binding_credential_missing",
                target_type="image_binding",
                target_id=ref.image_reference,
                environment_id=ref.environment_id,
                detail={
                    "credential_set_id": stored.id,
                    "credential_set_name": stored.name,
                    "missing_reason": reason,
                },
            )
        return moved

    async def _require(self, credential_id: int) -> StoredRegistryCredential:
        stored = await self._credentials.get(credential_id)
        if stored is None:
            raise ResourceNotFound("not found")
        return stored

    async def _checked_bundle(
        self, spec: RegistrySpec
    ) -> tuple[CheckedBundle | None, RegistrySpec]:
        text = spec.ca_bundle.strip()
        if not text or spec.provider not in PROVIDERS:
            return None, spec
        if not PROVIDERS[cast(ProviderKind, spec.provider)].tls_options_allowed:
            return None, spec
        try:
            return await asyncio.to_thread(check_ca_bundle, text, self._clock()), spec
        except ValidationFailed as invalid:
            return None, replace(spec, ca_bundle_problem=invalid.fields["ca_bundle"])

    async def _refuse_folded_name(self, name: str, *, excluding: int | None) -> None:
        clash = await self._credentials.find_by_folded_name(name, excluding=excluding)
        if clash is not None:
            raise ValidationFailed({"name": NAME_EXISTS})

    async def _duplicates_of(
        self, stored: StoredRegistryCredential
    ) -> tuple[DuplicateReference, ...]:
        matches = await self._credentials.find_at_address(stored.url, excluding=stored.id)
        return tuple(
            DuplicateReference(match.id, match.name)
            for match in matches
            if _same_login(stored, match)
        )

    async def _environments_of(
        self, credential_id: int
    ) -> tuple[tuple[GrantedEnvironment, ...], int]:
        listed = await self._access.list_environments(credential_id)
        total = (
            len(listed)
            if len(listed) < MAX_GRANTED_ENVIRONMENTS
            else await self._access.count_environments(credential_id)
        )
        return tuple(listed), total

    async def _grant_list(self, credential_id: int) -> GrantList:
        users = await self._access.list_users(credential_id, limit=MAX_GRANT_LISTING)
        teams = await self._access.list_teams(credential_id, limit=MAX_GRANT_LISTING)
        user_total, team_total = (
            (len(users), len(teams))
            if len(users) < MAX_GRANT_LISTING and len(teams) < MAX_GRANT_LISTING
            else await self._access.count_grants(credential_id)
        )
        return GrantList(
            users=tuple(Grantee("user", user.user_id, user.username) for user in users),
            teams=tuple(Grantee("team", team.team_id, team.name) for team in teams),
            users_total=user_total,
            teams_total=team_total,
        )

    async def _detail_of(self, stored: StoredRegistryCredential) -> RegistryDetail:
        now = self._clock()
        environments, environments_total = await self._environments_of(stored.id)
        bundle = (
            ()
            if stored.ca_bundle is None
            else await asyncio.to_thread(summarise_ca_bundle, stored.ca_bundle, now)
        )
        return RegistryDetail(
            id=stored.id,
            name=stored.name,
            provider=stored.provider,
            address=stored.url,
            authentication_enabled=stored.authentication_enabled,
            username=stored.username,
            secret_kind=stored.secret_kind,
            secret_stored=stored.has_secret,
            verify_tls=stored.verify_tls,
            ca_bundle=bundle,
            extras=extras_for_form(stored.extras),
            derived=_derived_status(stored, now),
            environments=environments,
            environments_total=environments_total,
            access=await self._grant_list(stored.id),
            duplicates=await self._duplicates_of(stored),
            is_default_for_host=stored.is_default_for_host,
            seeded_default=stored.seeded_default,
            bound_images=await self._bindings.count_for_credential(stored.id),
            created_at=stored.created_at,
            updated_at=stored.updated_at,
        )

    async def _with_bundle(
        self, state: RegistryFormState, stored: StoredRegistryCredential
    ) -> RegistryFormState:
        if stored.ca_bundle is None:
            return state
        summary = await asyncio.to_thread(summarise_ca_bundle, stored.ca_bundle, self._clock())
        return replace(state, ca_bundle=summary)


def _affected_image(row: AffectedBinding) -> AffectedImage:
    try:
        reference = parse_reference(row.image_reference).familiar
    except ReferenceInvalid:
        reference = row.image_reference
    return AffectedImage(row.environment_id, row.environment_name, reference)


def _leaves_default(current: StoredRegistryCredential, fields: CredentialFields) -> bool:
    return (
        fields.authentication_enabled
        or fields.provider != current.provider
        or fields.url != current.url
    )


def _known(text: str | None) -> ProviderKind | None:
    return cast(ProviderKind, text) if text in PROVIDERS else None


def _blank_state(kind: ProviderKind) -> RegistryFormState:
    shape = provider_shape(kind)
    return RegistryFormState(
        credential_id=None,
        shape=shape,
        name="",
        address=shape.fixed_address or shape.default_address or "",
        authentication_enabled=not shape.anonymous_allowed,
        username="",
        secret_stored=False,
        verify_tls=True,
        ca_bundle=(),
        extras=extras_defaults(kind),
    )


def _state_from_stored(stored: StoredRegistryCredential) -> RegistryFormState:
    return RegistryFormState(
        credential_id=stored.id,
        shape=provider_shape(stored.provider),
        name=stored.name,
        address=stored.url,
        authentication_enabled=stored.authentication_enabled,
        username=stored.username or "",
        secret_stored=stored.has_secret,
        verify_tls=stored.verify_tls,
        ca_bundle=(),
        extras=extras_for_form(stored.extras),
    )


def _switched_state(state: RegistryFormState, kind: ProviderKind) -> RegistryFormState:
    blank = _blank_state(kind)
    return RegistryFormState(
        credential_id=state.credential_id,
        shape=blank.shape,
        name=state.name,
        address=blank.address,
        authentication_enabled=state.authentication_enabled or not blank.shape.anonymous_allowed,
        username=state.username,
        secret_stored=False,
        verify_tls=True,
        ca_bundle=(),
        extras=blank.extras,
    )


def _row_of(entry: RegistryListEntry) -> RegistryListRow:
    return RegistryListRow(
        id=entry.id,
        name=entry.name,
        provider=entry.provider,
        address=entry.url,
        authentication_enabled=entry.authentication_enabled,
        verify_tls=entry.verify_tls,
        secret_kind=entry.secret_kind,
        environment_count=entry.environment_count,
        grantee_count=entry.grantee_count,
        is_default_for_host=entry.is_default_for_host,
        seeded_default=entry.seeded_default,
    )


def _same_login(stored: StoredRegistryCredential, other: AddressMatch) -> bool:
    if stored.authentication_enabled != other.authentication_enabled:
        return False
    if not stored.authentication_enabled:
        return True
    return (
        stored.username is not None
        and other.username is not None
        and stored.username.casefold() == other.username.casefold()
    )


def _derived_status(stored: StoredRegistryCredential, now: datetime) -> DerivedSecretStatus | None:
    if stored.secret_kind != "derived":
        return None
    expires = stored.derived_secret_expires_at
    return DerivedSecretStatus(
        cached=expires is not None,
        expires_at=expires,
        refreshed_at=stored.derived_secret_refreshed_at,
        expired=expires is not None and parse_timestamp(expires) <= now,
    )


def _fingerprint(stored: StoredRegistryCredential) -> str:
    """Name the connection a row describes, so only identical tests share a run."""
    digest = hashlib.sha256()
    for part in (
        stored.provider,
        stored.url,
        stored.extras_json,
        "1" if stored.verify_tls else "0",
        stored.ca_bundle,
        stored.username,
        stored.secret_ciphertext,
    ):
        raw = b"\x00" if part is None else b"\x01" + part.encode("utf-8")
        digest.update(len(raw).to_bytes(4, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _report_of(result: RegistryProbeResult, tls_verified: bool) -> RegistryTestReport:
    return RegistryTestReport(
        passed=result.passed,
        failed_stage=None if result.failed_stage is None else result.failed_stage.value,
        message=result.message,
        duration_ms=result.duration_ms,
        stages=tuple(
            StageReport(
                stage=stage.stage.value,
                outcome=stage.outcome.value,
                duration_ms=stage.duration_ms,
                message=stage.message,
                facts=dict(stage.facts),
            )
            for stage in result.stages
        ),
        tls_verified=tls_verified,
    )
