"""Rebinding an image reference to another credential set, checked against the registry."""

import asyncio
import dataclasses
import io
import json
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ValidationFailed
from app.auth.principal import Principal
from app.config import Settings
from app.registry_client.manifests import ManifestCheckKind
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.references import ImageReference
from app.services.activity import ActivityRecorder
from app.services.assembly import build_host_clients, build_image_bindings, build_registry_resolver
from app.services.image_bindings import (
    REBIND_PINNED,
    REBIND_REGISTRY_NOT_CONFIGURED,
    REBIND_UNBOUND,
    BindingChangedSinceReview,
    ImageBindingService,
)
from app.services.pull_credentials import CredentialChoice, SeenBinding
from app.services.registry_resolution import CredentialSetUnavailable
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD
from tests.image_support import (
    REFERENCE,
    REFERENCE_KEY,
    ScriptedManifests,
    binding_rows,
    digest,
    mark_missing,
    register,
    rows_for,
    stored_binding,
)
from tests.registry_support import ADDRESS, SECRET, bind_image
from tests.services.conftest import ImageRig
from tests.updater_support import ACTION, World, build_world, container
from tests.web.registry_support import leaks

REBOUND = "image_rebound"
SERVER_DIGEST = digest(2)
STORED_DIGEST = digest(5)
PINNED_REFERENCE = f"{ADDRESS}/app@{digest(1)}"
SENTINEL_USER = "sentinel-user-7731"
ANONYMOUS = CredentialChoice(None)

type Rebind = Callable[..., Coroutine[Any, Any, None]]


class TransactionProbe(ScriptedManifests):
    """Records whether the database session was inside a transaction at each registry check."""

    def __init__(self, rig: ImageRig) -> None:
        super().__init__()
        self._rig = rig
        self.in_transaction: list[bool] = []

    async def head_digest(
        self, target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
    ) -> str:
        self.in_transaction.append(self._rig.db.in_transaction)
        return await super().head_digest(target, reference, login)


def bindings_over(rig: ImageRig, manifests: ScriptedManifests) -> ImageBindingService:
    return build_image_bindings(
        rig.db,
        rig.runtime,
        rig.access.authoriser,
        build_host_clients(rig.db, rig.runtime, rig.engine),  # type: ignore[arg-type]
        ActivityRecorder(rig.db, rig.clock),
        resolver=build_registry_resolver(rig.db, rig.runtime, rig.registry.registries),
        manifests=manifests,  # type: ignore[arg-type]
    )


def rebinder(service: ImageBindingService) -> Rebind:
    async def run(
        choice: CredentialChoice,
        seen: SeenBinding,
        *,
        actor: Principal = ADMIN,
        reference: str = REFERENCE,
    ) -> None:
        await service.rebind(actor, ENV_A, reference, choice, seen)

    return run


@pytest.fixture
def manifests(image_rig: ImageRig) -> TransactionProbe:
    return TransactionProbe(image_rig)


@pytest.fixture
def rebind(image_rig: ImageRig, manifests: TransactionProbe) -> Rebind:
    return rebinder(bindings_over(image_rig, manifests))


@pytest.fixture
async def world(image_rig: ImageRig, settings: Settings) -> World:
    return await build_world(image_rig, settings)


async def review_of(rig: ImageRig) -> SeenBinding:
    stored = await stored_binding(rig.db, ENV_A, REFERENCE_KEY)
    assert stored is not None
    set_id = (
        stored.missing_credential_id
        if stored.state == "credential_missing"
        else stored.registry_credential_id
    )
    return SeenBinding(stored.state, stored.id, set_id)


async def bound_pair(rig: ImageRig) -> tuple[int, int]:
    original = await register(rig, "original")
    replacement = await register(rig, "replacement")
    await bind_image(rig.db, original, ENV_A)
    return original, replacement


async def all_activity(rig: ImageRig) -> list[dict[str, Any]]:
    rows = await rig.db.fetch_limited("SELECT * FROM activity_event ORDER BY id", (), limit=1000)
    return [dict(row) for row in rows]


async def test_a_rebind_to_a_set_that_authenticates_repoints_the_binding_and_records_it(
    image_rig: ImageRig, manifests: TransactionProbe, rebind: Rebind
) -> None:
    original, replacement = await bound_pair(image_rig)
    await image_rig.db.execute(
        "UPDATE image_credential_binding SET observed_digest = ?", (STORED_DIGEST,)
    )
    review = await review_of(image_rig)

    await rebind(CredentialChoice(replacement), review)

    [row] = await binding_rows(image_rig.db)
    [event] = await rows_for(image_rig.db, REBOUND)
    [(target, reference, login)] = manifests.calls
    assert (row["registry_credential_id"], row["state"]) == (replacement, "bound")
    assert row["observed_digest"] == STORED_DIGEST
    assert (event["result"], event["target_type"], event["target_id"]) == (
        "success",
        "image_binding",
        REFERENCE_KEY,
    )
    assert (event["actor_type"], event["actor_user_id"], event["environment_id"]) == (
        "user",
        ADMIN.user_id,
        ENV_A,
    )
    assert event["detail"] == {
        "credential_set_id": replacement,
        "anonymous": False,
        "previous_credential_set_id": original,
        "previous_state": "bound",
        "registry_digest": SERVER_DIGEST,
        "verified": True,
    }
    assert (target.address, reference.key) == (ADDRESS, REFERENCE_KEY)
    assert login is not None and login.username == "user-replacement"


async def test_a_rebind_to_an_anonymous_pull_checks_the_registry_without_a_login(
    image_rig: ImageRig, manifests: TransactionProbe, rebind: Rebind
) -> None:
    original, _ = await bound_pair(image_rig)
    review = await review_of(image_rig)

    await rebind(ANONYMOUS, review)

    [row] = await binding_rows(image_rig.db)
    [event] = await rows_for(image_rig.db, REBOUND)
    [(_, _, login)] = manifests.calls
    assert (row["registry_credential_id"], row["state"]) == (None, "bound")
    assert event["detail"]["anonymous"] is True
    assert event["detail"]["previous_credential_set_id"] == original
    assert login is None


async def test_a_rebind_to_the_set_already_bound_changes_nothing_and_asks_the_registry_nothing(
    image_rig: ImageRig, manifests: TransactionProbe, rebind: Rebind
) -> None:
    original, _ = await bound_pair(image_rig)
    review = await review_of(image_rig)

    await rebind(CredentialChoice(original), review)

    assert manifests.calls == []
    assert [row["registry_credential_id"] for row in await binding_rows(image_rig.db)] == [original]


async def test_a_set_the_registry_refuses_changes_nothing_and_writes_a_failure_row_with_the_kind(
    image_rig: ImageRig, manifests: TransactionProbe, rebind: Rebind
) -> None:
    original, replacement = await bound_pair(image_rig)
    manifests.answers[REFERENCE_KEY] = ManifestCheckKind.AUTHENTICATION
    review = await review_of(image_rig)

    with pytest.raises(ValidationFailed) as refused:
        await rebind(CredentialChoice(replacement), review)

    [row] = await binding_rows(image_rig.db)
    [event] = await rows_for(image_rig.db, REBOUND)
    assert "credential_choice" in refused.value.fields
    assert row["registry_credential_id"] == original
    assert (event["result"], event["detail"]["reason"]) == ("failure", "authentication")
    assert "verified" not in event["detail"]


async def test_a_stale_review_is_refused_and_leaves_the_binding_as_it_is(
    image_rig: ImageRig, rebind: Rebind
) -> None:
    original, replacement = await bound_pair(image_rig)
    stale = dataclasses.replace(await review_of(image_rig), set_id=replacement)

    with pytest.raises(BindingChangedSinceReview):
        await rebind(CredentialChoice(replacement), stale)

    [row] = await binding_rows(image_rig.db)
    [event] = await rows_for(image_rig.db, REBOUND)
    assert row["registry_credential_id"] == original
    assert (event["result"], event["detail"]["reason"]) == ("failure", "binding_changed")


async def test_a_binding_changed_while_the_registry_was_asked_is_not_overwritten(
    image_rig: ImageRig, manifests: TransactionProbe, rebind: Rebind
) -> None:
    _, replacement = await bound_pair(image_rig)
    third = await register(image_rig, "third")
    gate = asyncio.Event()
    manifests.gate = gate
    review = await review_of(image_rig)
    attempt = asyncio.create_task(rebind(CredentialChoice(replacement), review))
    await manifests.entered.wait()
    await image_rig.db.execute(
        "UPDATE image_credential_binding SET registry_credential_id = ?", (third,)
    )

    gate.set()

    with pytest.raises(BindingChangedSinceReview):
        await attempt
    assert [row["registry_credential_id"] for row in await binding_rows(image_rig.db)] == [third]


async def test_a_credential_missing_binding_is_repaired_by_a_rebind(
    image_rig: ImageRig, rebind: Rebind
) -> None:
    gone, replacement = await bound_pair(image_rig)
    await mark_missing(image_rig.db, gone)
    review = await review_of(image_rig)

    await rebind(CredentialChoice(replacement), review)

    [row] = await binding_rows(image_rig.db)
    [event] = await rows_for(image_rig.db, REBOUND)
    assert (row["state"], row["registry_credential_id"], row["missing_credential_id"]) == (
        "bound",
        replacement,
        None,
    )
    assert event["detail"]["previous_state"] == "credential_missing"
    assert event["detail"]["previous_credential_set_id"] == gone


async def test_the_next_updater_run_is_no_longer_refused_for_a_reference_that_was_repaired(
    image_rig: ImageRig, world: World
) -> None:
    gone = (await image_rig.db.fetch_one("SELECT id FROM registry_credential"))["id"]  # type: ignore[index]
    replacement = await register(image_rig, "replacement")
    await mark_missing(image_rig.db, gone)
    rebind = rebinder(bindings_over(image_rig, world.manifests))
    _, job = await world.rule(container("web"), ACTION)
    refused = await world.run(job)
    world.advance(10)
    held = await world.run(job)
    await rebind(CredentialChoice(replacement), await review_of(image_rig))
    world.advance(10)

    repaired = await world.run(job)

    assert (refused.outcome, refused.reason) == ("failure", "credential_missing")
    assert held.outcome == "success"
    assert (repaired.outcome, repaired.reason) == ("success", None)
    assert len(world.rows("image_update_refused")) == 1
    assert "pull" in world.verbs()


async def test_an_anonymous_choice_is_refused_where_no_set_at_the_registry_is_granted(
    image_rig: ImageRig, manifests: TransactionProbe, rebind: Rebind
) -> None:
    ungranted = await register(image_rig, "ungranted", environments=(ENV_B,))
    await bind_image(image_rig.db, ungranted, ENV_A)
    review = await review_of(image_rig)

    with pytest.raises(ValidationFailed) as refused:
        await rebind(ANONYMOUS, review)

    [event] = await rows_for(image_rig.db, REBOUND)
    assert refused.value.fields == {"credential_choice": REBIND_REGISTRY_NOT_CONFIGURED}
    assert event["detail"]["reason"] == "registry_not_configured"
    assert manifests.calls == []


@pytest.mark.parametrize(
    ("reference", "message", "reason"),
    [
        pytest.param(PINNED_REFERENCE, REBIND_PINNED, "pinned", id="pinned-by-digest"),
        pytest.param(f"{ADDRESS}/unbound:1", REBIND_UNBOUND, "not_bound", id="without-a-binding"),
    ],
)
async def test_a_reference_that_cannot_be_rebound_is_refused_without_contacting_the_registry(
    image_rig: ImageRig,
    manifests: TransactionProbe,
    rebind: Rebind,
    reference: str,
    message: str,
    reason: str,
) -> None:
    _, replacement = await bound_pair(image_rig)

    with pytest.raises(ValidationFailed) as refused:
        await rebind(CredentialChoice(replacement), SeenBinding(), reference=reference)

    [event] = await rows_for(image_rig.db, REBOUND)
    assert refused.value.fields == {"reference": message}
    assert event["detail"]["reason"] == reason
    assert manifests.calls == []


async def test_a_set_not_granted_to_the_environment_is_refused_and_the_binding_stands(
    image_rig: ImageRig, manifests: TransactionProbe, rebind: Rebind
) -> None:
    original, _ = await bound_pair(image_rig)
    elsewhere = await register(image_rig, "elsewhere", environments=(ENV_B,))
    review = await review_of(image_rig)

    with pytest.raises(CredentialSetUnavailable):
        await rebind(CredentialChoice(elsewhere), review)

    [row] = await binding_rows(image_rig.db)
    assert row["registry_credential_id"] == original
    assert manifests.calls == []


async def test_a_standard_user_is_refused_and_nothing_is_checked_or_changed(
    image_rig: ImageRig, manifests: TransactionProbe, rebind: Rebind
) -> None:
    original, replacement = await bound_pair(image_rig)
    review = await review_of(image_rig)

    with pytest.raises(PermissionDenied):
        await rebind(CredentialChoice(replacement), review, actor=STANDARD)

    [row] = await binding_rows(image_rig.db)
    assert row["registry_credential_id"] == original
    assert manifests.calls == []


async def test_the_registry_is_asked_outside_any_write_transaction(
    image_rig: ImageRig, manifests: TransactionProbe, rebind: Rebind
) -> None:
    _, replacement = await bound_pair(image_rig)
    review = await review_of(image_rig)

    await rebind(CredentialChoice(replacement), review)

    assert manifests.in_transaction == [False]


async def test_resolving_a_login_inside_a_write_transaction_is_refused_by_the_resolver(
    image_rig: ImageRig,
) -> None:
    _, replacement = await bound_pair(image_rig)
    resolver = build_registry_resolver(
        image_rig.db, image_rig.runtime, image_rig.registry.registries
    )

    with pytest.raises(RuntimeError, match="outside a write transaction"):
        async with image_rig.db.transaction():
            await resolver.resolve_login(
                replacement, environment_id=ENV_A, principal=ADMIN, registry_address=ADDRESS
            )


async def test_no_secret_reaches_the_rows_or_the_log_though_the_registry_check_received_it(
    image_rig: ImageRig,
    manifests: TransactionProbe,
    rebind: Rebind,
    log_stream: io.StringIO,
) -> None:
    await bound_pair(image_rig)
    chosen = await register(image_rig, "sentinel", username=SENTINEL_USER, secret=SECRET)
    review = await review_of(image_rig)

    await rebind(CredentialChoice(chosen), review)

    [(_, _, login)] = manifests.calls
    shown = [
        json.dumps(await all_activity(image_rig), default=str),
        log_stream.getvalue(),
        repr(await binding_rows(image_rig.db)),
    ]
    assert login is not None and login.secret.get_secret_value() == SECRET
    assert leaks([f"registry check saw {SECRET} {SENTINEL_USER}"], [SECRET, SENTINEL_USER]) == [
        SECRET,
        SENTINEL_USER,
    ]
    assert leaks(shown, [SECRET, SENTINEL_USER]) == []
