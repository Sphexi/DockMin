"""Binding images by a manifest digest match: the candidate scan and the verified write."""

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.registry_access import RegistryAccessRepository
from app.docker_engine.models import SwarmService
from app.registry_client.manifests import ManifestCheckKind
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.references import ImageReference
from app.services.activity import ActivityRecorder
from app.services.assembly import build_host_clients, build_image_bindings, build_registry_resolver
from app.services.binding_candidates import (
    BindingCandidateScanner,
    ReferenceOutcome,
    ScanLimits,
    UnboundImageScan,
    service_references,
)
from app.services.image_bindings import ImageBindingService, SetCandidates
from app.services.pull_credentials import CredentialChoice, CredentialSetMismatch
from app.services.registry_resolution import CredentialSetUnavailable
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.image_support import (
    REFERENCE,
    REFERENCE_KEY,
    ScriptedManifests,
    binding_rows,
    digest,
    full_id,
    register,
    rows_for,
)
from tests.registry_support import (
    ADDRESS,
    ECR_ADDRESS,
    ISSUED_USERNAME,
    OTHER_ADDRESS,
    add_ecr,
    bind_image,
    grant_environment,
)
from tests.services.conftest import ImageRig

LOCAL = digest(1)
STALE = digest(9)
UNCONFIGURED = "elsewhere.example/app:1"
PINNED = f"{ADDRESS}/app@{digest(5)}"
BUILT_LOCALLY = f"{ADDRESS}/built:1"
HUB_REFERENCE = "docker.io/library/busybox:1"
ECR_IMAGE = f"{ECR_ADDRESS}/app:1"
CAP = 200
BUDGET_SECONDS = 120.0
STEP_SECONDS = 60.0
ANONYMOUS = None

type Answers = dict[str | None, str | ManifestCheckKind]


class ByLogin(ScriptedManifests):
    """Answers by the login's username, None for anonymous; anything unlisted is refused."""

    def __init__(self, answers: Answers) -> None:
        super().__init__()
        self.by_login = answers
        self.on_call: Callable[[], None] | None = None

    async def head_digest(
        self, target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
    ) -> str:
        self.default = self.by_login.get(
            None if login is None else login.username, ManifestCheckKind.AUTHENTICATION
        )
        if self.on_call is not None:
            self.on_call()
        return await super().head_digest(target, reference, login)

    def addresses(self) -> set[str]:
        return {target.address for target, _, _ in self.calls}


class Tripwire(ByLogin):
    """Fails on any use of the manifest client other than a HEAD for a digest."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the scan used the manifest client's {name}")


def scanner_over(
    rig: ImageRig,
    manifests: ScriptedManifests,
    *,
    resolver: Any = None,
    limits: ScanLimits | None = None,
    monotonic: Callable[[], float] | None = None,
) -> BindingCandidateScanner:
    options: dict[str, Any] = {} if monotonic is None else {"monotonic": monotonic}
    return BindingCandidateScanner(
        bindings=ImageBindingRepository(rig.db),
        candidates=SetCandidates(
            access=RegistryAccessRepository(rig.db),
            environments=EnvironmentAccessRepository(rig.db),
        ),
        resolver=resolver or build_registry_resolver(rig.db, rig.runtime, rig.registry.registries),
        manifests=manifests,  # type: ignore[arg-type]
        hosts=build_host_clients(rig.db, rig.runtime, rig.engine),  # type: ignore[arg-type]
        authoriser=rig.access.authoriser,
        limits=limits,
        **options,
    )


def writer_over(rig: ImageRig) -> ImageBindingService:
    return build_image_bindings(
        rig.db,
        rig.runtime,
        rig.access.authoriser,
        build_host_clients(rig.db, rig.runtime, rig.engine),  # type: ignore[arg-type]
        ActivityRecorder(rig.db, rig.clock),
    )


def in_use(
    rig: ImageRig, reference: str = REFERENCE, number: int = 1, local: str | None = LOCAL
) -> None:
    """Put an image pulled outside dockmin on the host, and a container running it."""
    repository = reference.rpartition(":")[0]
    digests = () if local is None else (f"{repository}@{local}",)
    rig.host.add_image(reference, full_id(number), digests=digests)
    rig.host.add(f"{number:064x}", f"container-{number}", image=reference)


def outcome_of(scan: UnboundImageScan, reference: str = REFERENCE_KEY) -> ReferenceOutcome:
    [found] = [o for o in scan.outcomes if o.reference == reference]
    return found


def statuses(outcome: ReferenceOutcome) -> dict[str | None, tuple[str, str | None]]:
    return {c.name: (c.status, c.error) for c in outcome.tried}


async def test_a_reference_pulled_outside_dockmin_binds_to_the_one_set_whose_digest_is_local(
    image_rig: ImageRig,
) -> None:
    alpha = await register(image_rig, "alpha")
    await register(image_rig, "bravo")
    await register(image_rig, "charlie")
    in_use(image_rig)
    manifests = ByLogin(
        {
            "user-alpha": LOCAL,
            "user-bravo": STALE,
            "user-charlie": ManifestCheckKind.TIMEOUT,
        }
    )

    scan = await scanner_over(image_rig, manifests).scan(ADMIN, ENV_A)

    found = outcome_of(scan)
    assert found.outcome == "binds"
    assert found.choices == (CredentialChoice(alpha),)
    assert found.local_digests == (LOCAL,)
    assert statuses(found) == {
        "alpha": ("match", None),
        "bravo": ("mismatch", None),
        "charlie": ("failed", "timeout"),
        None: ("failed", "authentication"),
    }
    assert await binding_rows(image_rig.db) == []


async def test_two_sets_that_read_the_image_are_ambiguous_and_nothing_is_bound(
    image_rig: ImageRig,
) -> None:
    alpha = await register(image_rig, "alpha")
    bravo = await register(image_rig, "bravo")
    in_use(image_rig)
    manifests = ByLogin({"user-alpha": LOCAL, "user-bravo": LOCAL})

    scan = await scanner_over(image_rig, manifests).scan(ADMIN, ENV_A)

    found = outcome_of(scan)
    assert found.outcome == "ambiguous"
    assert found.choices == (CredentialChoice(alpha), CredentialChoice(bravo))
    assert await binding_rows(image_rig.db) == []


async def test_a_reference_no_set_can_read_is_no_match_with_each_failure_shown(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    await register(image_rig, "bravo")
    in_use(image_rig)
    manifests = ByLogin({"user-alpha": STALE})

    scan = await scanner_over(image_rig, manifests).scan(ADMIN, ENV_A)

    found = outcome_of(scan)
    assert (found.outcome, found.choices) == ("no_match", ())
    assert statuses(found) == {
        "alpha": ("mismatch", None),
        "bravo": ("failed", "authentication"),
        None: ("failed", "authentication"),
    }


async def test_the_scan_contacts_only_configured_addresses_and_sends_only_head_requests(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    await register(image_rig, "elsewhere", address=OTHER_ADDRESS)
    bound = await register(image_rig, "bound")
    in_use(image_rig, REFERENCE, 1)
    in_use(image_rig, UNCONFIGURED, 2)
    in_use(image_rig, HUB_REFERENCE, 3)
    in_use(image_rig, PINNED, 4, local=None)
    in_use(image_rig, BUILT_LOCALLY, 5, local=None)
    in_use(image_rig, f"{ADDRESS}/bound:1", 6)
    await bind_image(image_rig.db, bound, ENV_A, f"{ADDRESS}/bound:1")
    manifests = Tripwire({"user-alpha": LOCAL})

    scan = await scanner_over(image_rig, manifests).scan(ADMIN, ENV_A)

    assert {o.reference: o.outcome for o in scan.outcomes} == {
        REFERENCE_KEY: "binds",
        UNCONFIGURED: "no_match",
        "docker.io/library/busybox:1": "no_match",
        PINNED: "pinned",
        BUILT_LOCALLY: "no_local_digest",
    }
    assert manifests.addresses() == {ADDRESS}
    assert {reference.key for _, reference, _ in manifests.calls} == {REFERENCE_KEY}
    assert image_rig.host.mutations() == []
    assert image_rig.host.verbs() == []
    assert image_rig.host.calls("GET", "/services") == []


async def test_an_ecr_set_is_resolved_outside_every_transaction(image_rig: ImageRig) -> None:
    ecr = await add_ecr(image_rig.registry)
    await grant_environment(image_rig.db, ecr.id, ENV_A)
    in_use(image_rig, ECR_IMAGE)
    real = build_registry_resolver(image_rig.db, image_rig.runtime, image_rig.registry.registries)
    seen: list[bool] = []

    class ProbingResolver:
        async def resolve_login(self, *args: Any, **kwargs: Any) -> Any:
            seen.append(image_rig.db.in_transaction)
            return await real.resolve_login(*args, **kwargs)

    manifests = ByLogin({ISSUED_USERNAME: LOCAL})
    manifests.on_call = lambda: seen.append(image_rig.db.in_transaction)

    scan = await scanner_over(image_rig, manifests, resolver=ProbingResolver()).scan(ADMIN, ENV_A)

    found = outcome_of(scan, ECR_IMAGE)
    assert (found.outcome, found.choices) == ("binds", (CredentialChoice(ecr.id),))
    assert seen == [False, False]
    assert image_rig.registry.deriver.issued == 1
    assert len(manifests.calls) == 1


async def test_a_scan_inside_a_write_transaction_is_refused_before_any_check(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    in_use(image_rig)
    manifests = ByLogin({})

    async with image_rig.db.transaction():
        with pytest.raises(RuntimeError):
            await scanner_over(image_rig, manifests).scan(ADMIN, ENV_A)

    assert manifests.calls == []


async def test_a_standard_user_cannot_scan(image_rig: ImageRig) -> None:
    await register(image_rig, "alpha", users=(STANDARD.user_id,))
    in_use(image_rig)
    manifests = ByLogin({})

    with pytest.raises((PermissionDenied, ResourceNotFound)):
        await scanner_over(image_rig, manifests).scan(STANDARD, ENV_A)

    assert manifests.calls == []


async def test_the_time_budget_leaves_the_remaining_references_not_checked(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    references = [f"{ADDRESS}/app:{n}" for n in range(1, 4)]
    for number, reference in enumerate(references, start=1):
        in_use(image_rig, reference, number)
    now = [0.0]

    def advance() -> None:
        now[0] += STEP_SECONDS

    manifests = ByLogin({"user-alpha": LOCAL})
    manifests.on_call = advance
    limits = ScanLimits(in_flight=1, total_seconds=BUDGET_SECONDS)

    scan = await scanner_over(image_rig, manifests, limits=limits, monotonic=lambda: now[0]).scan(
        ADMIN, ENV_A
    )

    assert [o.outcome for o in scan.outcomes] == ["binds", "not_checked", "not_checked"]
    assert len(manifests.calls) == 2
    assert all(check.status == "not_checked" for check in scan.outcomes[1].tried)


async def test_references_past_the_cap_are_not_checked_and_never_contacted(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    for number in range(1, CAP + 2):
        in_use(image_rig, f"{ADDRESS}/app:{number}", number)
    manifests = ByLogin({"user-alpha": LOCAL})

    scan = await scanner_over(image_rig, manifests).scan(ADMIN, ENV_A)

    left = [o for o in scan.outcomes if o.outcome == "not_checked"]
    assert len(scan.outcomes) == CAP + 1
    assert len(left) == 1
    assert left[0].tried == ()
    assert left[0].reference not in {reference.key for _, reference, _ in manifests.calls}
    assert len({reference.key for _, reference, _ in manifests.calls}) == CAP


def test_service_images_are_read_without_their_digest_suffix() -> None:
    def service(image: object) -> SwarmService:
        return SwarmService.model_validate(
            {
                "ID": "s1",
                "Version": {"Index": 1},
                "Spec": {"Name": "web", "TaskTemplate": {"ContainerSpec": {"Image": image}}},
            }
        )

    found = service_references(
        [
            service(f"{ADDRESS}/app:1@{digest(3)}"),
            service(f"{ADDRESS}/untagged@{digest(4)}"),
            service(f"{ADDRESS}/plain"),
            service(full_id(7)),
            service(None),
        ]
    )

    assert found == {
        REFERENCE_KEY,
        f"{ADDRESS}/untagged@{digest(4)}",
        f"{ADDRESS}/plain:latest",
    }


async def test_bind_verified_writes_one_audited_binding_without_touching_the_host(
    image_rig: ImageRig,
) -> None:
    alpha = await register(image_rig, "alpha")
    writer = writer_over(image_rig)

    outcome = await writer.bind_verified(
        ADMIN, ENV_A, REFERENCE, CredentialChoice(alpha), digest=LOCAL
    )

    [row] = await binding_rows(image_rig.db)
    [event] = await rows_for(image_rig.db, "image_bound")
    assert outcome == "bound"
    assert (row["registry_credential_id"], row["observed_digest"], row["state"]) == (
        alpha,
        LOCAL,
        "bound",
    )
    assert (event["target_type"], event["target_id"], event["environment_id"]) == (
        "image_binding",
        REFERENCE_KEY,
        ENV_A,
    )
    assert event["actor_user_id"] == ADMIN.user_id
    assert event["detail"] == {
        "credential_set_id": alpha,
        "anonymous": False,
        "digest": LOCAL,
        "verified": True,
        "pulled": False,
    }
    assert image_rig.host.requests == []


async def test_bind_verified_records_an_anonymous_choice_where_a_set_is_configured(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "alpha")
    writer = writer_over(image_rig)

    await writer.bind_verified(ADMIN, ENV_A, REFERENCE, CredentialChoice(None), digest=LOCAL)

    [row] = await binding_rows(image_rig.db)
    [event] = await rows_for(image_rig.db, "image_bound")
    assert row["registry_credential_id"] is None
    assert event["detail"]["anonymous"] is True
    assert event["detail"]["credential_set_id"] is None


async def test_bind_verified_skips_a_reference_bound_meanwhile_and_writes_no_row(
    image_rig: ImageRig,
) -> None:
    alpha = await register(image_rig, "alpha")
    bravo = await register(image_rig, "bravo")
    writer = writer_over(image_rig)
    await bind_image(image_rig.db, bravo, ENV_A, REFERENCE_KEY)

    outcome = await writer.bind_verified(
        ADMIN, ENV_A, REFERENCE, CredentialChoice(alpha), digest=LOCAL
    )

    assert outcome == "skipped"
    assert [r["registry_credential_id"] for r in await binding_rows(image_rig.db)] == [bravo]
    assert await rows_for(image_rig.db, "image_bound") == []


async def test_bind_verified_refuses_what_it_may_not_bind_and_writes_nothing(
    image_rig: ImageRig,
) -> None:
    alpha = await register(image_rig, "alpha")
    elsewhere = await register(image_rig, "elsewhere", address=OTHER_ADDRESS)
    ungranted = await register(image_rig, "ungranted", environments=())
    writer = writer_over(image_rig)

    with pytest.raises((PermissionDenied, ResourceNotFound)):
        await writer.bind_verified(
            STANDARD, ENV_A, REFERENCE, CredentialChoice(alpha), digest=LOCAL
        )
    with pytest.raises(ValidationFailed):
        await writer.bind_verified(ADMIN, ENV_A, PINNED, CredentialChoice(alpha), digest=LOCAL)
    with pytest.raises(ValidationFailed):
        await writer.bind_verified(ADMIN, ENV_A, REFERENCE, CredentialChoice(alpha), digest="x")
    with pytest.raises(ValidationFailed):
        await writer.bind_verified(ADMIN, ENV_A, UNCONFIGURED, CredentialChoice(None), digest=LOCAL)
    with pytest.raises(CredentialSetMismatch):
        await writer.bind_verified(
            ADMIN, ENV_A, REFERENCE, CredentialChoice(elsewhere), digest=LOCAL
        )
    with pytest.raises(CredentialSetUnavailable):
        await writer.bind_verified(
            ADMIN, ENV_A, REFERENCE, CredentialChoice(ungranted), digest=LOCAL
        )

    assert await binding_rows(image_rig.db) == []
    assert "success" not in [r["result"] for r in await rows_for(image_rig.db, "image_bound")]
