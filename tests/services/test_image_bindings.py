"""Image-to-credential bindings: the pull decision, binding changes, display and reaping."""

from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest

from app.auth.errors import PermissionDenied, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.image_binding import StoredBinding
from app.docker_engine.models import ContainerSummary, ImageSummary
from app.registry_client.references import parse_reference
from app.services.activity import ActivityRecorder, UserActor
from app.services.assembly import build_pull_credentials
from app.services.image_bindings import (
    UNSHOWN_SET,
    BindingPullCredentials,
    RebindRefused,
    bare_image_id,
    binding_view,
    canonical_keys,
    daemon_references,
    live_references,
    parse_pull_reference,
)
from app.services.pull_credentials import (
    BindingCredentialMissing,
    CandidateListTooLarge,
    CredentialChoice,
    CredentialChoiceRequired,
    CredentialSetMismatch,
    PullCredentials,
    SeenBinding,
)
from app.services.registry_resolution import CredentialSetUnavailable
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD
from tests.auth_support import STANDARD_ID
from tests.image_support import (
    HUB_KEY,
    HUB_REFERENCE,
    OTHER_REFERENCE,
    REFERENCE,
    REFERENCE_KEY,
    binding_rows,
    decode_auth,
    digest,
    full_id,
    image_id,
    mark_missing,
    register,
    rows_for,
    stage,
    stored_binding,
)
from tests.registry_support import (
    ADDRESS,
    ECR_ADDRESS,
    OTHER_ADDRESS,
    add_ecr,
    bind_image,
    grant_environment,
)
from tests.services.conftest import ImageRig

REFERENCE_PARSED = parse_reference(REFERENCE)
MAX_OFFERED = 200
ECR_IMAGE = f"{ECR_ADDRESS}/app:1"
NO_ONE = None

type Resolve = Callable[..., Awaitable[PullCredentials]]


@pytest.fixture
def credentials(image_rig: ImageRig) -> BindingPullCredentials:
    return build_pull_credentials(image_rig.db, image_rig.runtime, image_rig.registry.registries)


@pytest.fixture
def resolve(credentials: BindingPullCredentials) -> Resolve:
    async def run(
        principal: Principal | None,
        reference: str = REFERENCE,
        choice: CredentialChoice | None = None,
        environment_id: int = ENV_A,
    ) -> PullCredentials:
        return await credentials.resolve(
            environment_id, reference, principal=principal, choice=choice
        )

    return run


def username_of(credentials: PullCredentials) -> str:
    assert credentials.registry_auth is not None
    return decode_auth(credentials.registry_auth.get_secret_value())["username"]


@pytest.mark.parametrize(
    "principal",
    [ADMIN, STANDARD, NO_ONE],
    ids=["administrator", "standard-user", "automation"],
)
async def test_two_sets_on_one_host_resolve_to_the_bound_set_not_the_default_or_the_lowest_id(
    image_rig: ImageRig, resolve: Resolve, principal: Principal | None
) -> None:
    await register(image_rig, "first", default=True, users=(STANDARD_ID,))
    bound = await register(image_rig, "second", users=(STANDARD_ID,))
    await bind_image(image_rig.db, bound, ENV_A)

    resolved = await resolve(principal)

    assert (resolved.credential_set_id, resolved.source) == (bound, "binding")
    assert username_of(resolved) == "user-second"
    assert decode_auth(resolved.registry_auth.get_secret_value())["serveraddress"] == ADDRESS  # type: ignore[union-attr]


@pytest.mark.parametrize("principal", [ADMIN, STANDARD], ids=["administrator", "standard-user"])
async def test_an_unbound_reference_with_two_sets_asks_and_the_default_does_not_decide(
    image_rig: ImageRig, resolve: Resolve, principal: Principal
) -> None:
    first = await register(image_rig, "first", default=True, users=(STANDARD_ID,))
    second = await register(image_rig, "second", users=(STANDARD_ID,))

    with pytest.raises(CredentialChoiceRequired) as asked:
        await resolve(principal)

    assert {candidate.id for candidate in asked.value.candidates} == {first, second}
    assert "credential_choice" in asked.value.fields


async def test_automation_never_chooses_by_host_for_an_unbound_reference(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    await register(image_rig, "only")

    with pytest.raises(CredentialChoiceRequired) as asked:
        await resolve(NO_ONE)

    assert asked.value.candidates == ()


async def test_an_unbound_reference_with_one_fitting_set_uses_that_set(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    only = await register(image_rig, "only")
    await register(image_rig, "elsewhere", address=OTHER_ADDRESS)

    resolved = await resolve(ADMIN)

    assert (resolved.credential_set_id, resolved.source) == (only, "only_match")
    assert username_of(resolved) == "user-only"


async def test_an_unbound_reference_with_no_fitting_set_is_pulled_anonymously(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    await register(image_rig, "elsewhere", address=OTHER_ADDRESS)

    resolved = await resolve(ADMIN)

    assert (resolved.credential_set_id, resolved.registry_auth, resolved.source) == (
        None,
        None,
        "no_match",
    )


async def test_a_standard_user_is_offered_only_the_sets_they_hold_a_grant_on(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    await register(image_rig, "theirs", users=(STANDARD_ID,))
    await register(image_rig, "not-theirs")

    resolved = await resolve(STANDARD)

    assert username_of(resolved) == "user-theirs"
    assert resolved.source == "only_match"


async def test_a_set_not_granted_to_the_environment_is_never_a_candidate(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    await register(image_rig, "elsewhere", environments=(ENV_B,))

    resolved = await resolve(ADMIN)

    assert resolved.source == "no_match"


async def test_an_explicit_choice_of_a_set_wins_over_the_default_and_names_the_choice(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    await register(image_rig, "first", default=True)
    chosen = await register(image_rig, "second")

    resolved = await resolve(ADMIN, choice=CredentialChoice(chosen))

    assert (resolved.credential_set_id, resolved.source) == (chosen, "choice")
    assert username_of(resolved) == "user-second"


async def test_choosing_to_pull_anonymously_sends_no_credentials(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    await register(image_rig, "only")

    resolved = await resolve(ADMIN, choice=CredentialChoice(None))

    assert (resolved.credential_set_id, resolved.registry_auth, resolved.source) == (
        None,
        None,
        "choice",
    )


async def test_a_choice_of_a_set_for_another_registry_is_refused(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    other = await register(image_rig, "elsewhere", address=OTHER_ADDRESS)

    with pytest.raises(CredentialSetMismatch):
        await resolve(ADMIN, choice=CredentialChoice(other))


@pytest.mark.parametrize(
    ("principal", "granted_to_environment", "granted_to_user"),
    [
        pytest.param(ADMIN, False, False, id="administrator-without-an-environment-grant"),
        pytest.param(STANDARD, True, False, id="standard-user-without-a-user-grant"),
    ],
)
async def test_a_choice_of_a_set_the_caller_may_not_use_here_is_unavailable(
    image_rig: ImageRig,
    resolve: Resolve,
    principal: Principal,
    granted_to_environment: bool,
    granted_to_user: bool,
) -> None:
    unusable = await register(
        image_rig,
        "unusable",
        environments=(ENV_A,) if granted_to_environment else (),
        users=(STANDARD_ID,) if granted_to_user else (),
    )

    with pytest.raises(CredentialSetUnavailable) as refused:
        await resolve(principal, choice=CredentialChoice(unusable))

    assert refused.value.reason == "missing"


async def test_a_bound_set_whose_address_no_longer_matches_is_refused_not_re_matched(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    bound = await register(image_rig, "bound")
    await register(image_rig, "fitting-but-unbound")
    await bind_image(image_rig.db, bound, ENV_A)
    await image_rig.db.execute(
        "UPDATE registry_credential SET url = ? WHERE id = ?", (OTHER_ADDRESS, bound)
    )

    with pytest.raises(CredentialSetUnavailable) as refused:
        await resolve(ADMIN)

    assert refused.value.reason == "credential_mismatch"


async def test_a_bound_set_the_user_lost_their_grant_on_is_refused_without_falling_back(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    bound = await register(image_rig, "bound")
    await register(image_rig, "other", users=(STANDARD_ID,))
    await bind_image(image_rig.db, bound, ENV_A)

    with pytest.raises(CredentialSetUnavailable) as refused:
        await resolve(STANDARD)

    assert refused.value.reason == "not_granted_user"


async def test_an_anonymous_binding_resolves_to_no_credentials_even_when_a_set_fits(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    await register(image_rig, "fits")
    await bind_image(image_rig.db, None, ENV_A)

    resolved = await resolve(ADMIN)

    assert (resolved.credential_set_id, resolved.registry_auth, resolved.source) == (
        None,
        None,
        "binding",
    )


async def test_a_credential_missing_binding_refuses_without_a_choice(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    gone = await register(image_rig, "gone")
    await register(image_rig, "replacement")
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)

    with pytest.raises(BindingCredentialMissing):
        await resolve(ADMIN)


async def test_an_administrator_repairs_a_credential_missing_binding_by_choosing_a_set(
    image_rig: ImageRig, resolve: Resolve, credentials: BindingPullCredentials
) -> None:
    gone = await register(image_rig, "gone")
    replacement = await register(image_rig, "replacement")
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)

    resolved = await resolve(ADMIN, choice=CredentialChoice(replacement))

    stored = await stored_binding(image_rig.db, ENV_A, REFERENCE_KEY)
    assert stored is not None
    assert (resolved.credential_set_id, resolved.source) == (replacement, "choice")
    assert resolved.seen == SeenBinding("credential_missing", stored.id, gone)


@pytest.mark.parametrize("choose", ["another-set", "anonymous", "the-missing-set"])
async def test_a_standard_user_cannot_repair_a_credential_missing_binding(
    image_rig: ImageRig, resolve: Resolve, choose: str
) -> None:
    gone = await register(image_rig, "gone", users=(STANDARD_ID,))
    other = await register(image_rig, "other", users=(STANDARD_ID,))
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)
    choices = {
        "another-set": CredentialChoice(other),
        "anonymous": CredentialChoice(None),
        "the-missing-set": CredentialChoice(gone),
    }

    with pytest.raises(RebindRefused):
        await resolve(STANDARD, choice=choices[choose])


@pytest.mark.parametrize("choose", ["another-set", "anonymous"])
async def test_a_standard_user_cannot_change_a_bound_set(
    image_rig: ImageRig, resolve: Resolve, choose: str
) -> None:
    bound = await register(image_rig, "bound", users=(STANDARD_ID,))
    other = await register(image_rig, "other", users=(STANDARD_ID,))
    await bind_image(image_rig.db, bound, ENV_A)
    choices = {"another-set": CredentialChoice(other), "anonymous": CredentialChoice(None)}

    with pytest.raises(PermissionDenied) as refused:
        await resolve(STANDARD, choice=choices[choose])

    assert isinstance(refused.value, RebindRefused)
    assert "Only an administrator" in str(refused.value)


async def test_a_standard_user_choosing_the_set_that_is_already_bound_changes_nothing(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    bound = await register(image_rig, "bound", users=(STANDARD_ID,))
    await bind_image(image_rig.db, bound, ENV_A)

    resolved = await resolve(STANDARD, choice=CredentialChoice(bound))

    assert (resolved.credential_set_id, resolved.source) == (bound, "choice")


async def test_resolving_inside_a_write_transaction_is_refused(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    async with image_rig.db.transaction():
        with pytest.raises(RuntimeError, match="outside a write transaction"):
            await resolve(ADMIN)


async def test_a_reference_is_resolved_for_its_own_environment_only(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    bound = await register(image_rig, "bound", environments=(ENV_A, ENV_B))
    await register(image_rig, "second", environments=(ENV_A, ENV_B))
    await bind_image(image_rig.db, bound, ENV_A)

    with pytest.raises(CredentialChoiceRequired):
        await resolve(ADMIN, environment_id=ENV_B)


async def test_more_than_200_sets_at_one_address_refuse_instead_of_choosing(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    await image_rig.db.execute(
        "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < ?) "
        "INSERT INTO registry_credential (name, provider, url, authentication_enabled, "
        "secret_kind, created_at, updated_at) "
        "SELECT printf('set-%04d', n), 'custom', ?, 0, 'static', 'x', 'x' FROM seq",
        (MAX_OFFERED + 1, ADDRESS),
    )
    await image_rig.db.execute(
        "INSERT INTO registry_environment_access (registry_credential_id, environment_id) "
        "SELECT id, ? FROM registry_credential",
        (ENV_A,),
    )

    with pytest.raises(CandidateListTooLarge):
        await resolve(ADMIN)


async def test_an_invalid_reference_is_a_validation_error_on_the_reference_field(
    image_rig: ImageRig, resolve: Resolve
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await resolve(ADMIN, reference="Not A Reference")

    assert set(refused.value.fields) == {"reference"}


async def test_a_decision_records_the_binding_it_was_made_against(
    image_rig: ImageRig, credentials: BindingPullCredentials
) -> None:
    bound = await register(image_rig, "bound")
    await bind_image(image_rig.db, bound, ENV_A)
    stored = await stored_binding(image_rig.db, ENV_A, REFERENCE_KEY)
    assert stored is not None

    decision = await credentials.decide(ENV_A, REFERENCE, principal=ADMIN)
    fresh = await credentials.decide(ENV_A, OTHER_REFERENCE, principal=ADMIN)

    assert decision.seen == SeenBinding("bound", stored.id, bound)
    assert fresh.seen == SeenBinding()


RECORD_CASES = [
    pytest.param("admin-choice-matching-seen", "choice", True, True, "rebound", id="rebound"),
    pytest.param("admin-binding-source", "binding", True, True, "kept", id="source-not-choice"),
    pytest.param("admin-choice-stale-seen", "choice", False, True, "kept", id="seen-is-stale"),
    pytest.param("standard-choice-matching", "choice", True, False, "kept", id="not-admin"),
]


@pytest.mark.parametrize(("label", "source", "seen_matches", "as_admin", "expected"), RECORD_CASES)
async def test_an_existing_binding_changes_only_for_an_administrators_choice_against_what_they_saw(
    image_rig: ImageRig,
    label: str,
    source: str,
    seen_matches: bool,
    as_admin: bool,
    expected: str,
) -> None:
    original = await register(image_rig, "original")
    replacement = await register(image_rig, "replacement")
    await bind_image(image_rig.db, original, ENV_A)
    stored = await stored_binding(image_rig.db, ENV_A, REFERENCE_KEY)
    assert stored is not None
    seen = SeenBinding("bound", stored.id if seen_matches else stored.id + 1, original)
    attempt = PullCredentials(replacement, None, source, seen)  # type: ignore[arg-type]
    actor = ADMIN if as_admin else STANDARD

    outcome = await record(image_rig, actor, attempt, digest(1))

    after = await stored_binding(image_rig.db, ENV_A, REFERENCE_KEY)
    assert after is not None
    assert outcome == expected
    assert after.registry_credential_id == (replacement if expected == "rebound" else original)


async def record(
    rig: ImageRig,
    actor: Principal,
    credentials: PullCredentials,
    observed: str | None,
    reference: str = REFERENCE,
) -> str:
    recorder = ActivityRecorder(rig.db, rig.clock)
    async with (
        recorder.audit(UserActor(actor.user_id), action="image_pulled", target_type="image"),
        rig.db.transaction(),
    ):
        return await rig.images.bindings.record_pull(
            actor, ENV_A, parse_reference(reference), credentials, observed
        )


async def test_a_first_pull_creates_the_binding_and_a_related_row(image_rig: ImageRig) -> None:
    created = await register(image_rig, "created")

    outcome = await record(image_rig, ADMIN, PullCredentials(created, None, "choice"), digest(1))

    [row] = await binding_rows(image_rig.db)
    [related] = await rows_for(image_rig.db, "image_bound")
    assert outcome == "created"
    assert (row["registry_credential_id"], row["state"], row["observed_digest"]) == (
        created,
        "bound",
        digest(1),
    )
    assert related["target_type"] == "image_binding"
    assert related["target_id"] == REFERENCE_KEY
    assert related["detail"] == {"credential_set_id": created, "anonymous": False}


async def test_a_first_anonymous_pull_creates_a_binding_with_no_set(image_rig: ImageRig) -> None:
    outcome = await record(image_rig, ADMIN, PullCredentials(None, None, "no_match"), None)

    [row] = await binding_rows(image_rig.db)
    [related] = await rows_for(image_rig.db, "image_bound")
    assert outcome == "created"
    assert (row["registry_credential_id"], row["state"]) == (None, "bound")
    assert related["detail"] == {"credential_set_id": None, "anonymous": True}


async def test_a_pull_with_the_bound_set_keeps_the_binding_and_refreshes_the_digest(
    image_rig: ImageRig,
) -> None:
    bound = await register(image_rig, "bound")
    await bind_image(image_rig.db, bound, ENV_A)

    outcome = await record(image_rig, STANDARD, PullCredentials(bound, None, "binding"), digest(2))

    [row] = await binding_rows(image_rig.db)
    assert outcome == "kept"
    assert row["observed_digest"] == digest(2)
    assert await rows_for(image_rig.db, "image_rebound") == []
    assert await rows_for(image_rig.db, "image_bound") == []


async def test_a_rebind_by_an_administrators_choice_writes_the_previous_set_and_state(
    image_rig: ImageRig,
) -> None:
    original = await register(image_rig, "original")
    replacement = await register(image_rig, "replacement")
    await bind_image(image_rig.db, original, ENV_A)
    stored = await stored_binding(image_rig.db, ENV_A, REFERENCE_KEY)
    assert stored is not None
    attempt = PullCredentials(
        replacement, None, "choice", SeenBinding("bound", stored.id, original)
    )

    await record(image_rig, ADMIN, attempt, digest(3))

    [related] = await rows_for(image_rig.db, "image_rebound")
    assert related["detail"] == {
        "credential_set_id": replacement,
        "anonymous": False,
        "previous_credential_set_id": original,
        "previous_state": "bound",
    }


async def test_a_credential_missing_binding_returns_to_bound_when_an_administrator_repairs_it(
    image_rig: ImageRig,
) -> None:
    gone = await register(image_rig, "gone")
    replacement = await register(image_rig, "replacement")
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)
    stored = await stored_binding(image_rig.db, ENV_A, REFERENCE_KEY)
    assert stored is not None
    attempt = PullCredentials(
        replacement, None, "choice", SeenBinding("credential_missing", stored.id, gone)
    )

    outcome = await record(image_rig, ADMIN, attempt, None)

    [row] = await binding_rows(image_rig.db)
    [related] = await rows_for(image_rig.db, "image_rebound")
    assert outcome == "rebound"
    assert (row["state"], row["registry_credential_id"], row["missing_credential_id"]) == (
        "bound",
        replacement,
        None,
    )
    assert related["detail"]["previous_state"] == "credential_missing"
    assert related["detail"]["previous_credential_set_id"] == gone


async def test_a_credential_missing_binding_stays_when_a_standard_user_pulls(
    image_rig: ImageRig,
) -> None:
    gone = await register(image_rig, "gone")
    replacement = await register(image_rig, "replacement", users=(STANDARD_ID,))
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)
    attempt = PullCredentials(replacement, None, "choice")

    outcome = await record(image_rig, STANDARD, attempt, None)

    [row] = await binding_rows(image_rig.db)
    assert outcome == "skipped"
    assert row["state"] == "credential_missing"


async def test_a_pull_with_a_set_that_was_deleted_meanwhile_writes_no_binding(
    image_rig: ImageRig,
) -> None:
    outcome = await record(image_rig, ADMIN, PullCredentials(9999, None, "choice"), None)

    assert outcome == "skipped"
    assert await binding_rows(image_rig.db) == []


@pytest.mark.parametrize(
    "observed",
    ["sha256:short", "sha256:" + "A" * 64, "md5:" + "a" * 64, None],
    ids=["short", "upper-case", "other-algorithm", "absent"],
)
async def test_an_observed_digest_that_is_not_a_sha256_digest_is_stored_as_none(
    image_rig: ImageRig, observed: str | None
) -> None:
    await record(image_rig, ADMIN, PullCredentials(None, None, "no_match"), observed)

    [row] = await binding_rows(image_rig.db)
    assert row["observed_digest"] is None


async def test_the_offer_preselects_the_bound_set_and_may_rebind_only_for_administrators(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "first", default=True, users=(STANDARD_ID,))
    bound = await register(image_rig, "second", users=(STANDARD_ID,))
    await bind_image(image_rig.db, bound, ENV_A)
    bindings = image_rig.containers.bindings

    by_admin = await bindings.offer(ADMIN, ENV_A, REFERENCE)
    by_standard = await bindings.offer(STANDARD, ENV_A, REFERENCE)

    assert by_admin.preselected == CredentialChoice(bound)
    assert by_standard.preselected == CredentialChoice(bound)
    assert (by_admin.may_rebind, by_standard.may_rebind) == (True, False)
    assert by_admin.binding.state == "bound"


@pytest.mark.parametrize(
    ("sets", "expected"),
    [
        pytest.param((), CredentialChoice(None), id="no-set-means-anonymous"),
        pytest.param((("only", False),), "only", id="the-only-set"),
        pytest.param((("a", False), ("b", True)), "b", id="the-default"),
        pytest.param((("a", False), ("b", False)), None, id="several-and-no-default"),
    ],
)
async def test_the_offer_preselects_the_default_the_only_set_or_nothing(
    image_rig: ImageRig,
    sets: tuple[tuple[str, bool], ...],
    expected: CredentialChoice | str | None,
) -> None:
    ids = {name: await register(image_rig, name, default=default) for name, default in sets}

    offer = await image_rig.containers.bindings.offer(ADMIN, ENV_A, REFERENCE)

    wanted = CredentialChoice(ids[expected]) if isinstance(expected, str) else expected
    assert offer.preselected == wanted
    assert {candidate.name for candidate in offer.candidates} == set(ids)


async def test_the_offer_preselects_nothing_for_a_credential_missing_binding(
    image_rig: ImageRig,
) -> None:
    gone = await register(image_rig, "gone")
    await register(image_rig, "replacement", default=True)
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)

    offer = await image_rig.containers.bindings.offer(ADMIN, ENV_A, REFERENCE)

    assert offer.preselected is None
    assert offer.binding.state == "credential_missing"


@pytest.mark.parametrize(
    ("sets", "expected"),
    [
        pytest.param((), "anonymous", id="no-set-is-anonymous"),
        pytest.param((("only", False),), "anonymous", id="one-set-that-is-not-the-default"),
        pytest.param((("a", False), ("b", True)), "b", id="several-with-a-default"),
        pytest.param((("a", False), ("b", False)), "anonymous", id="several-without-a-default"),
    ],
)
async def test_the_picker_preselects_the_default_set_and_otherwise_anonymous_for_an_unbound_image(
    image_rig: ImageRig, sets: tuple[tuple[str, bool], ...], expected: str
) -> None:
    ids = {name: await register(image_rig, name, default=default) for name, default in sets}

    offer = await image_rig.containers.bindings.picker(ADMIN, ENV_A, REFERENCE)

    wanted = str(ids[expected]) if expected in ids else expected
    assert offer.preselected_value == wanted
    assert {candidate.name for candidate in offer.candidates} == set(ids)
    assert offer.binding.state == "unbound"
    assert offer.locked is False


async def test_the_picker_preselects_the_bound_set(image_rig: ImageRig) -> None:
    await register(image_rig, "first", default=True)
    bound = await register(image_rig, "second")
    await bind_image(image_rig.db, bound, ENV_A)

    offer = await image_rig.containers.bindings.picker(ADMIN, ENV_A, REFERENCE)

    assert offer.preselected_value == str(bound)
    assert offer.binding.state == "bound"


async def test_the_picker_preselects_anonymous_for_an_anonymous_binding(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "first", default=True)
    await bind_image(image_rig.db, None, ENV_A)

    offer = await image_rig.containers.bindings.picker(ADMIN, ENV_A, REFERENCE)

    assert offer.preselected_value == "anonymous"
    assert offer.binding.state == "anonymous"


async def test_the_picker_preselects_nothing_for_a_credential_missing_binding(
    image_rig: ImageRig,
) -> None:
    gone = await register(image_rig, "gone")
    await register(image_rig, "replacement", default=True)
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)

    offer = await image_rig.containers.bindings.picker(ADMIN, ENV_A, REFERENCE)

    assert offer.preselected is None
    assert offer.preselected_value is None
    assert offer.binding.state == "credential_missing"


async def test_the_picker_locks_a_standard_user_to_an_existing_binding_and_not_an_unbound_image(
    image_rig: ImageRig,
) -> None:
    bound = await register(image_rig, "bound", users=(STANDARD_ID,))
    await register(image_rig, "other", users=(STANDARD_ID,), address=OTHER_ADDRESS)
    bindings = image_rig.containers.bindings

    unbound = await bindings.picker(STANDARD, ENV_A, REFERENCE)
    await bind_image(image_rig.db, bound, ENV_A)
    held_to_it = await bindings.picker(STANDARD, ENV_A, REFERENCE)

    assert unbound.locked is False
    assert held_to_it.locked is True
    assert held_to_it.preselected_value == str(bound)
    assert held_to_it.may_rebind is False


async def test_the_picker_offers_a_standard_user_only_the_sets_that_user_may_use(
    image_rig: ImageRig,
) -> None:
    mine = await register(image_rig, "mine", users=(STANDARD_ID,))
    await register(image_rig, "not-mine")

    offer = await image_rig.containers.bindings.picker(STANDARD, ENV_A, REFERENCE)

    assert [candidate.id for candidate in offer.candidates] == [mine]


async def test_the_three_binding_cases_are_told_apart_and_names_follow_the_visibility_rule(
    image_rig: ImageRig,
) -> None:
    visible = await register(image_rig, "visible", users=(STANDARD_ID,))
    hidden = await register(image_rig, "hidden")
    gone = await register(image_rig, "gone")
    keys = {"visible": REFERENCE, "hidden": OTHER_REFERENCE, "gone": f"{ADDRESS}/app:3"}
    await bind_image(image_rig.db, visible, ENV_A, keys["visible"])
    await bind_image(image_rig.db, hidden, ENV_A, keys["hidden"])
    await bind_image(image_rig.db, gone, ENV_A, keys["gone"])
    await mark_missing(image_rig.db, gone)
    await bind_image(image_rig.db, None, ENV_A, f"{ADDRESS}/app:4")
    wanted = [*keys.values(), f"{ADDRESS}/app:4", f"{ADDRESS}/app:5"]
    service = image_rig.images.bindings

    by_admin = await service.views(ADMIN, ENV_A, wanted)
    by_standard = await service.views(STANDARD, ENV_A, wanted)

    assert [(v.state, v.set_name) for v in by_admin.values()] == [
        ("bound", "visible"),
        ("bound", "hidden"),
        ("credential_missing", "gone"),
        ("anonymous", None),
        ("unbound", None),
    ]
    assert [(v.state, v.set_name) for v in by_standard.values()] == [
        ("bound", "visible"),
        ("bound", UNSHOWN_SET),
        ("credential_missing", UNSHOWN_SET),
        ("anonymous", None),
        ("unbound", None),
    ]
    assert by_admin[keys["gone"]].missing_reason == "deleted"


async def test_a_deleted_set_leaves_a_binding_that_names_the_set_it_lost(
    image_rig: ImageRig,
) -> None:
    gone = await register(image_rig, "gone")
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)

    view = (await image_rig.images.bindings.views(ADMIN, ENV_A, [REFERENCE]))[REFERENCE]

    assert (view.state, view.credential_set_id, view.set_name) == (
        "credential_missing",
        gone,
        "gone",
    )


async def configured(rig: ImageRig, name: str, image: str, number: int) -> None:
    rig.host.add(f"{number:064x}", name, image=image, image_id=full_id(number), state="exited")


async def test_a_binding_with_no_image_and_no_container_is_reapable_and_one_in_use_is_not(
    image_rig: ImageRig,
) -> None:
    anywhere = await register(image_rig, "anywhere")
    await stage(image_rig, REFERENCE, 1)
    await configured(image_rig, "by-name", OTHER_REFERENCE, 2)
    for reference in (REFERENCE, OTHER_REFERENCE, f"{ADDRESS}/app:3"):
        await bind_image(image_rig.db, anywhere, ENV_A, reference)

    listed = await image_rig.images.bindings.reapable(ADMIN, ENV_A)

    assert [b.reference for b in listed.bindings] == [f"{ADDRESS}/app:3"]
    assert (listed.more, listed.unverified) == (False, False)


async def test_a_container_configured_by_image_id_is_resolved_to_the_reference_it_was_created_with(
    image_rig: ImageRig,
) -> None:
    anywhere = await register(image_rig, "anywhere")
    image_rig.host.add(
        "c" * 64,
        "by-id",
        image=image_id(2),
        image_id=full_id(2),
        config={"Image": OTHER_REFERENCE},
    )
    await bind_image(image_rig.db, anywhere, ENV_A, OTHER_REFERENCE)
    await bind_image(image_rig.db, anywhere, ENV_A, f"{ADDRESS}/app:3")

    listed = await image_rig.images.bindings.reapable(ADMIN, ENV_A)

    assert [b.reference for b in listed.bindings] == [f"{ADDRESS}/app:3"]


async def test_when_a_container_cannot_be_inspected_nothing_is_offered_for_reaping(
    image_rig: ImageRig,
) -> None:
    anywhere = await register(image_rig, "anywhere")
    box = image_rig.host.add(
        "c" * 64, "by-id", image=image_id(2), image_id=full_id(2), config={"Image": REFERENCE}
    )
    image_rig.host.fail("inspect", 500, target=box.id)
    await bind_image(image_rig.db, anywhere, ENV_A, f"{ADDRESS}/app:3")

    listed = await image_rig.images.bindings.reapable(ADMIN, ENV_A)

    assert listed.bindings == ()
    assert listed.unverified is True


async def test_more_than_100_containers_configured_by_image_id_offer_nothing_for_reaping(
    image_rig: ImageRig,
) -> None:
    anywhere = await register(image_rig, "anywhere")
    await bind_image(image_rig.db, anywhere, ENV_A, f"{ADDRESS}/app:3")
    for number in range(101):
        image_rig.host.add(
            f"{number + 1:064x}", f"c{number}", image=image_id(number + 10), image_id=full_id(1)
        )

    listed = await image_rig.images.bindings.reapable(ADMIN, ENV_A)

    assert (listed.bindings, listed.unverified) == ((), True)


async def test_only_an_administrator_may_list_or_reap_bindings(image_rig: ImageRig) -> None:
    with pytest.raises(PermissionDenied):
        await image_rig.images.bindings.reapable(STANDARD, ENV_A)
    with pytest.raises(PermissionDenied):
        await image_rig.images.bindings.reap(STANDARD, ENV_A, expected={1: REFERENCE})


async def test_a_reap_removes_exactly_the_listed_bindings_that_are_still_reapable(
    image_rig: ImageRig,
) -> None:
    anywhere = await register(image_rig, "anywhere")
    references = [f"{ADDRESS}/app:{n}" for n in (1, 2, 3, 4)]
    for reference in references:
        await bind_image(image_rig.db, anywhere, ENV_A, reference)
    await stage(image_rig, references[1], 7)
    rows = await binding_rows(image_rig.db)
    ids = {row["image_reference"]: n for n, row in enumerate(rows, start=1)}
    listed = {
        ids[references[0]]: references[0],
        ids[references[1]]: references[1],
        ids[references[2]]: f"{ADDRESS}/app:other",
    }

    removed = await image_rig.images.bindings.reap(ADMIN, ENV_A, expected=listed)

    remaining = {row["image_reference"] for row in await binding_rows(image_rig.db)}
    related = await rows_for(image_rig.db, "image_binding_removed")
    [summary] = await rows_for(image_rig.db, "image_bindings_reaped")
    assert removed == 1
    assert remaining == {references[1], references[2], references[3]}
    assert [r["target_id"] for r in related] == [references[0]]
    assert (summary["target_type"], summary["target_id"]) == ("environment", str(ENV_A))


async def test_a_reap_of_nothing_still_reapable_deletes_nothing(image_rig: ImageRig) -> None:
    anywhere = await register(image_rig, "anywhere")
    await stage(image_rig, REFERENCE, 1)
    await bind_image(image_rig.db, anywhere, ENV_A, REFERENCE)

    removed = await image_rig.images.bindings.reap(ADMIN, ENV_A, expected={1: REFERENCE})

    assert removed == 0
    assert len(await binding_rows(image_rig.db)) == 1


async def test_a_reap_row_carries_the_count_of_bindings_removed(image_rig: ImageRig) -> None:
    anywhere = await register(image_rig, "anywhere")
    await bind_image(image_rig.db, anywhere, ENV_A, REFERENCE)

    await image_rig.images.bindings.reap(ADMIN, ENV_A, expected={1: REFERENCE})

    [summary] = await rows_for(image_rig.db, "image_bindings_reaped")
    assert summary["detail"] == {"count": 1}


async def test_a_reap_that_removes_nothing_writes_no_row(image_rig: ImageRig) -> None:
    anywhere = await register(image_rig, "anywhere")
    await stage(image_rig, REFERENCE, 1)
    await bind_image(image_rig.db, anywhere, ENV_A, REFERENCE)

    await image_rig.images.bindings.reap(ADMIN, ENV_A, expected={1: REFERENCE})

    assert await rows_for(image_rig.db, "image_bindings_reaped") == []


@pytest.mark.parametrize(
    "expected",
    [pytest.param({}, id="none"), pytest.param(dict.fromkeys(range(1, 202), REFERENCE), id="201")],
)
async def test_a_reap_selection_must_hold_between_one_and_200_bindings(
    image_rig: ImageRig, expected: dict[int, str]
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await image_rig.images.bindings.reap(ADMIN, ENV_A, expected=expected)

    assert "bindings" in refused.value.fields


async def test_a_reap_refuses_when_a_container_could_not_be_checked(image_rig: ImageRig) -> None:
    anywhere = await register(image_rig, "anywhere")
    image_rig.host.add("c" * 64, "by-id", image=image_id(2), image_id=full_id(2))
    image_rig.host.fail("inspect", 500)
    await bind_image(image_rig.db, anywhere, ENV_A, REFERENCE)

    with pytest.raises(ValidationFailed) as refused:
        await image_rig.images.bindings.reap(ADMIN, ENV_A, expected={1: REFERENCE})

    assert "could not be checked" in refused.value.fields["bindings"]
    assert len(await binding_rows(image_rig.db)) == 1


def test_references_on_images_and_in_containers_are_collected_by_canonical_key() -> None:
    images = [
        ImageSummary.model_validate(
            {"Id": full_id(1), "Created": 1, "RepoTags": [HUB_REFERENCE], "RepoDigests": []}
        )
    ]
    containers = [
        ContainerSummary.model_validate(
            {
                "Id": "c" * 64,
                "Names": ["/x"],
                "Image": "ghcr.io/o/a:1",
                "ImageID": full_id(1),
                "Created": 1,
                "State": "running",
                "Status": "Up",
            }
        ),
        ContainerSummary.model_validate(
            {
                "Id": "d" * 64,
                "Names": ["/y"],
                "Image": image_id(2)[:12],
                "ImageID": full_id(2),
                "Created": 1,
                "State": "running",
                "Status": "Up",
            }
        ),
    ]

    live = live_references(images, containers)

    assert live.images == {HUB_KEY}
    assert live.containers == {"ghcr.io/o/a:1"}
    assert live.complete is True


def test_each_canonical_key_maps_to_the_daemons_first_text_for_it_tags_before_digests() -> None:
    texts = daemon_references(
        ["busybox:1", "docker.io/library/busybox:1", "busybox:latest", "not a reference"],
        [f"busybox@{digest(1)}"],
    )

    assert texts == {
        "docker.io/library/busybox:1": "busybox:1",
        "docker.io/library/busybox:latest": "busybox:latest",
        f"docker.io/library/busybox@{digest(1)}": f"busybox@{digest(1)}",
    }
    assert canonical_keys(["busybox:1"], [f"busybox@{digest(1)}"]) == (
        "docker.io/library/busybox:1",
        f"docker.io/library/busybox@{digest(1)}",
    )


@pytest.mark.parametrize(
    ("text", "bare"),
    [
        pytest.param(full_id(1), image_id(1), id="prefixed"),
        pytest.param(image_id(1), image_id(1), id="bare"),
        pytest.param(image_id(1)[:12], None, id="short"),
        pytest.param(image_id(1).upper() + "A", None, id="upper-case"),
        pytest.param("busybox", None, id="name"),
    ],
)
def test_only_a_64_hex_id_with_or_without_its_prefix_is_an_image_id(
    text: str, bare: str | None
) -> None:
    assert bare_image_id(text) == bare


def test_a_malformed_pull_reference_is_a_validation_error_on_the_reference_field() -> None:
    with pytest.raises(ValidationFailed) as refused:
        parse_pull_reference("Not A Reference")

    assert refused.value.fields == {"reference": "Image is not a valid image reference."}
    assert parse_pull_reference("busybox").key == HUB_KEY.replace(":1", ":latest")


def stored(**fields: object) -> StoredBinding:
    values: dict[str, object] = {
        "id": 1,
        "environment_id": ENV_A,
        "image_reference": REFERENCE_KEY,
        "registry_credential_id": 5,
        "credential_name": "alpha",
        "credential_url": ADDRESS,
        "missing_credential_id": None,
        "missing_credential_name": None,
        "missing_reason": None,
        "observed_digest": digest(1),
        "state": "bound",
        "bound_at": "x",
        "updated_at": "x",
        "credential_missing_at": None,
    }
    return StoredBinding(**{**values, **fields})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("record", "visible", "state", "name", "set_id"),
    [
        pytest.param(None, True, "unbound", None, None, id="no-row"),
        pytest.param(stored(), True, "bound", "alpha", 5, id="bound-and-visible"),
        pytest.param(stored(), False, "bound", UNSHOWN_SET, 5, id="bound-and-hidden"),
        pytest.param(
            stored(registry_credential_id=None, credential_name=None, credential_url=None),
            False,
            "anonymous",
            None,
            None,
            id="anonymous",
        ),
        pytest.param(
            stored(
                state="credential_missing",
                registry_credential_id=None,
                credential_name=None,
                credential_url=None,
                missing_credential_id=5,
                missing_credential_name="gone",
                missing_reason="address_changed",
                credential_missing_at="x",
            ),
            True,
            "credential_missing",
            "gone",
            5,
            id="missing-and-visible",
        ),
        pytest.param(
            stored(
                state="credential_missing",
                registry_credential_id=None,
                missing_credential_id=5,
                missing_credential_name="gone",
                missing_reason="deleted",
                credential_missing_at="x",
            ),
            False,
            "credential_missing",
            UNSHOWN_SET,
            5,
            id="missing-and-hidden",
        ),
    ],
)
def test_a_stored_binding_is_shown_by_its_case_and_a_hidden_set_keeps_its_name_back(
    record: StoredBinding | None,
    visible: bool,
    state: str,
    name: str | None,
    set_id: int | None,
) -> None:
    view = binding_view(REFERENCE_KEY, record, name_visible=visible)

    assert (view.state, view.set_name, view.credential_set_id) == (state, name, set_id)


async def test_a_bound_set_passes_the_usability_check_with_the_same_decision_and_reads_no_secret(
    image_rig: ImageRig, credentials: BindingPullCredentials
) -> None:
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)

    usable = await credentials.check_usable(ENV_A, REFERENCE, principal=None)
    decided = await credentials.decide(ENV_A, REFERENCE, principal=None)

    assert usable.choice.credential_set_id == set_id == decided.choice.credential_set_id
    assert usable.source == decided.source == "binding"


@pytest.mark.parametrize(
    ("prepare", "error"),
    [
        ("missing", BindingCredentialMissing),
        ("ungranted", CredentialSetUnavailable),
        ("other_address", CredentialSetUnavailable),
    ],
)
async def test_a_binding_that_could_not_pull_fails_the_usability_check_as_the_pull_would(
    image_rig: ImageRig,
    credentials: BindingPullCredentials,
    prepare: str,
    error: type[Exception],
) -> None:
    set_id = await register(image_rig, "bound")
    await bind_image(image_rig.db, set_id, ENV_A)
    changes: dict[str, Callable[[], Awaitable[object]]] = {
        "missing": lambda: mark_missing(image_rig.db, set_id),
        "ungranted": lambda: image_rig.db.execute(
            "DELETE FROM registry_environment_access WHERE registry_credential_id = ?", (set_id,)
        ),
        "other_address": lambda: image_rig.db.execute(
            "UPDATE registry_credential SET url = ? WHERE id = ?", (OTHER_ADDRESS, set_id)
        ),
    }
    await changes[prepare]()

    with pytest.raises(error):
        await credentials.check_usable(ENV_A, REFERENCE, principal=None)


@pytest.mark.parametrize(
    ("min_remaining", "elapsed", "exchanges"),
    [
        (None, timedelta(hours=2), 1),
        (timedelta(hours=11), timedelta(hours=2), 2),
    ],
    ids=["none-keeps-the-margin", "long-request-derives-anew"],
)
async def test_resolving_forwards_min_remaining_to_the_derived_token_refresh(
    image_rig: ImageRig,
    credentials: BindingPullCredentials,
    min_remaining: timedelta | None,
    elapsed: timedelta,
    exchanges: int,
) -> None:
    ecr = await add_ecr(image_rig.registry)
    await grant_environment(image_rig.db, ecr.id, ENV_A)
    await bind_image(image_rig.db, ecr.id, ENV_A, ECR_IMAGE)
    await credentials.resolve(ENV_A, ECR_IMAGE, principal=ADMIN)
    image_rig.clock.advance(elapsed)

    await credentials.resolve(ENV_A, ECR_IMAGE, principal=ADMIN, min_remaining=min_remaining)

    assert image_rig.registry.deriver.issued == exchanges
