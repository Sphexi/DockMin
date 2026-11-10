"""Pull and push: credentials, the binding they write, failures and cancellation."""

import asyncio
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.docker_engine.models import PullProgress, PushProgress
from app.services.activity import UserActor
from app.services.image_bindings import RebindRefused
from app.services.image_views import ImageChangedSinceReview, ImagePullFailed, ImagePushFailed
from app.services.object_inspector import EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.pull_credentials import (
    CredentialChoice,
    CredentialChoiceRequired,
)
from tests.access_support import ADMIN, ENV_A, ENV_B, MEMBER, STANDARD
from tests.auth_support import STANDARD_ID
from tests.docker_engine.container_engine import DAEMON_DIGEST, ENGINE_TEXT
from tests.docker_engine.fake_engine import Refuse, Stall
from tests.image_support import (
    HUB_KEY,
    HUB_REFERENCE,
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
    stored_binding,
)
from tests.registry_support import ADDRESS, SECRET, bind_image
from tests.services.conftest import ImageRig
from tests.support import present

PULLED = 7
PUSHED = 8
TAG_NAME = "myorg/app"
ANONYMOUS_AUTH = "e30="


class Sink:
    def __init__(self) -> None:
        self.pulled: list[PullProgress] = []
        self.pushed: list[PushProgress] = []

    def on_pull(self, event: PullProgress) -> None:
        self.pulled.append(event)

    def on_push(self, event: PushProgress) -> None:
        self.pushed.append(event)


async def pull(
    rig: ImageRig,
    reference: str = REFERENCE,
    who: Any = ADMIN,
    choice: CredentialChoice | None = None,
) -> Any:
    plan = await rig.images.actions.prepare_pull(who, ENV_A, reference, choice)
    return await rig.images.actions.pull(who, ENV_A, plan)


def auth_of(rig: ImageRig, path: str, method: str = "POST") -> str | None:
    [request] = rig.host.calls(method, path)
    return request.headers.get("x-registry-auth")


async def every_activity_text(rig: ImageRig) -> str:
    rows = await rig.db.fetch_limited("SELECT detail FROM activity_event", (), limit=1000)
    return "\n".join(str(row["detail"]) for row in rows)


async def test_a_pull_with_the_only_fitting_set_sends_its_login_and_binds_the_reference(
    image_rig: ImageRig,
) -> None:
    chosen = await register(image_rig, "chosen")
    image_rig.host.pull_id = full_id(PULLED)
    image_rig.host.pull_digest = digest(PULLED)

    outcome = await pull(image_rig)

    sent = decode_auth(auth_of(image_rig, "/images/create") or "")
    binding = await stored_binding(image_rig.db, ENV_A, REFERENCE_KEY)
    assert (sent["username"], sent["serveraddress"]) == ("user-chosen", ADDRESS)
    assert (outcome.binding, outcome.image_id, outcome.digest) == (
        "created",
        image_id(PULLED),
        digest(PULLED),
    )
    assert binding is not None
    assert (binding.registry_credential_id, binding.state, binding.observed_digest) == (
        chosen,
        "bound",
        digest(PULLED),
    )


async def test_a_pull_writes_a_pull_row_and_a_bound_row_that_carry_the_set_and_no_secret(
    image_rig: ImageRig,
) -> None:
    chosen = await register(image_rig, "chosen")
    image_rig.host.pull_id = full_id(PULLED)

    await pull(image_rig)

    [pulled] = await rows_for(image_rig.db, "image_pulled")
    [bound] = await rows_for(image_rig.db, "image_bound")
    texts = await every_activity_text(image_rig)
    assert (pulled["result"], pulled["target_type"], pulled["target_id"]) == (
        "success",
        "image",
        image_id(PULLED),
    )
    assert pulled["detail"] == {
        "reference": REFERENCE_KEY,
        "credential_set_id": chosen,
        "anonymous": False,
        "credential_source": "only_match",
        "digest": None,
        "binding": "created",
    }
    assert (bound["target_type"], bound["target_id"]) == ("image_binding", REFERENCE_KEY)
    assert SECRET not in texts
    assert present(auth_of(image_rig, "/images/create")) not in texts
    assert pulled["environment_id"] == ENV_A


async def test_an_anonymous_pull_sends_no_credentials_and_writes_no_binding(
    image_rig: ImageRig,
) -> None:
    outcome = await pull(image_rig, HUB_REFERENCE)

    assert auth_of(image_rig, "/images/create") is None
    assert outcome.binding == "skipped"
    assert await binding_rows(image_rig.db) == []
    assert await rows_for(image_rig.db, "image_bound") == []


async def test_pulling_a_hub_name_asks_the_daemon_for_the_repository_and_the_tag_separately(
    image_rig: ImageRig,
) -> None:
    await pull(image_rig, HUB_REFERENCE)

    [request] = image_rig.host.calls("POST", "/images/create")
    assert request.query == {"fromImage": "docker.io/library/busybox", "tag": "1"}


async def test_a_pull_by_digest_asks_for_the_digest_and_binds_the_repository_digest_key(
    image_rig: ImageRig,
) -> None:
    pinned = f"busybox@{digest(3)}"
    await register(image_rig, "hub", address="docker.io")

    outcome = await pull(image_rig, pinned)

    [request] = image_rig.host.calls("POST", "/images/create")
    [row] = await binding_rows(image_rig.db)
    assert request.query == {"fromImage": f"docker.io/library/busybox@{digest(3)}"}
    assert row["image_reference"] == f"docker.io/library/busybox@{digest(3)}"
    assert outcome.digest == digest(3)


async def test_pulling_again_keeps_the_binding_and_follows_the_image_and_digest_to_the_new_pull(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "first")
    bound = await register(image_rig, "second")
    image_rig.host.pull_id = full_id(PULLED)
    image_rig.host.pull_digest = digest(PULLED)
    await pull(image_rig, choice=CredentialChoice(bound))
    image_rig.host.pull_id = full_id(PUSHED)
    image_rig.host.pull_digest = digest(PUSHED)

    outcome = await pull(image_rig)

    [row] = await binding_rows(image_rig.db)
    assert (outcome.binding, outcome.image_id) == ("kept", image_id(PUSHED))
    assert (row["registry_credential_id"], row["observed_digest"]) == (bound, digest(PUSHED))
    assert len(await rows_for(image_rig.db, "image_bound")) == 1
    assert await rows_for(image_rig.db, "image_rebound") == []
    sent = decode_auth(image_rig.host.calls("POST", "/images/create")[1].headers["x-registry-auth"])
    assert sent["username"] == "user-second"


async def test_a_pull_of_an_unbound_reference_with_two_sets_asks_which_and_pulls_nothing(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "first")
    await register(image_rig, "second")

    with pytest.raises(CredentialChoiceRequired):
        await image_rig.images.actions.prepare_pull(ADMIN, ENV_A, REFERENCE)

    assert image_rig.host.calls("POST", "/images/create") == []
    assert await rows_for(image_rig.db, "image_pulled") == []


async def test_the_chosen_set_is_bound_and_recorded_as_a_choice(image_rig: ImageRig) -> None:
    await register(image_rig, "first", default=True)
    chosen = await register(image_rig, "second")

    await pull(image_rig, choice=CredentialChoice(chosen))

    [pulled] = await rows_for(image_rig.db, "image_pulled")
    binding = await stored_binding(image_rig.db, ENV_A, REFERENCE_KEY)
    assert binding is not None
    assert binding.registry_credential_id == chosen
    assert pulled["detail"]["credential_source"] == "choice"


async def test_a_standard_user_pulls_with_a_set_they_hold_and_a_new_binding_is_created(
    image_rig: ImageRig,
) -> None:
    mine = await register(image_rig, "mine", users=(STANDARD_ID,))
    await register(image_rig, "not-mine")

    outcome = await pull(image_rig, who=STANDARD)

    binding = await stored_binding(image_rig.db, ENV_A, REFERENCE_KEY)
    [pulled] = await rows_for(image_rig.db, "image_pulled")
    assert outcome.binding == "created"
    assert binding is not None
    assert binding.registry_credential_id == mine
    assert pulled["actor_user_id"] == STANDARD_ID


async def test_a_standard_user_cannot_rebind_a_bound_reference_and_nothing_is_pulled(
    image_rig: ImageRig,
) -> None:
    bound = await register(image_rig, "bound", users=(STANDARD_ID,))
    other = await register(image_rig, "other", users=(STANDARD_ID,))
    await bind_image(image_rig.db, bound, ENV_A)

    with pytest.raises(RebindRefused):
        await pull(image_rig, who=STANDARD, choice=CredentialChoice(other))

    assert image_rig.host.calls("POST", "/images/create") == []
    assert await rows_for(image_rig.db, "image_pulled") == []
    assert (await binding_rows(image_rig.db))[0]["registry_credential_id"] == bound


async def test_a_credential_missing_binding_asks_a_standard_user_like_an_unbound_image(
    image_rig: ImageRig,
) -> None:
    gone = await register(image_rig, "gone", users=(STANDARD_ID,))
    other = await register(image_rig, "other", users=(STANDARD_ID,))
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)

    with pytest.raises(CredentialChoiceRequired):
        await image_rig.images.actions.prepare_pull(STANDARD, ENV_A, REFERENCE)
    outcome = await pull(image_rig, who=STANDARD, choice=CredentialChoice(other))

    [row] = await binding_rows(image_rig.db)
    assert outcome.binding == "rebound"
    assert (row["state"], row["registry_credential_id"]) == ("bound", other)


async def test_an_administrator_repairs_a_credential_missing_binding_by_pulling_with_another_set(
    image_rig: ImageRig,
) -> None:
    gone = await register(image_rig, "gone")
    replacement = await register(image_rig, "replacement")
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)

    outcome = await pull(image_rig, choice=CredentialChoice(replacement))

    [row] = await binding_rows(image_rig.db)
    [rebound] = await rows_for(image_rig.db, "image_rebound")
    sent = decode_auth(auth_of(image_rig, "/images/create") or "")
    assert outcome.binding == "rebound"
    assert (row["state"], row["registry_credential_id"], row["missing_credential_id"]) == (
        "bound",
        replacement,
        None,
    )
    assert rebound["detail"]["previous_state"] == "credential_missing"
    assert sent["username"] == "user-replacement"


async def test_a_binding_made_after_the_plan_was_decided_wins_over_a_plan_that_chose_by_match(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "only")
    plan = await image_rig.images.actions.prepare_pull(ADMIN, ENV_A, REFERENCE)
    later = await register(image_rig, "later")
    await bind_image(image_rig.db, later, ENV_A)

    await image_rig.images.actions.pull(ADMIN, ENV_A, plan)

    sent = decode_auth(auth_of(image_rig, "/images/create") or "")
    assert sent["username"] == "user-later"


async def test_an_explicit_choice_does_not_survive_a_binding_made_after_the_plan(
    image_rig: ImageRig,
) -> None:
    mine = await register(image_rig, "mine", users=(STANDARD_ID,))
    await register(image_rig, "other", users=(STANDARD_ID,))
    plan = await image_rig.images.actions.prepare_pull(
        STANDARD, ENV_A, REFERENCE, CredentialChoice(mine)
    )
    elsewhere = await register(image_rig, "elsewhere", users=(STANDARD_ID,))
    await bind_image(image_rig.db, elsewhere, ENV_A)

    with pytest.raises(RebindRefused):
        await image_rig.images.actions.pull(STANDARD, ENV_A, plan)

    assert image_rig.host.calls("POST", "/images/create") == []
    [row] = await binding_rows(image_rig.db)
    assert row["registry_credential_id"] == elsewhere
    assert await rows_for(image_rig.db, "image_pulled") == []


@pytest.mark.parametrize(
    ("setup", "error", "reason"),
    [
        pytest.param(
            lambda host: setattr(host, "pull_status", 404),
            ImagePullFailed,
            "not_found_or_denied",
            id="not-found-or-denied",
        ),
        pytest.param(
            lambda host: setattr(host, "pull_status", 500),
            ImagePullFailed,
            "pull_failed",
            id="engine-error",
        ),
        pytest.param(
            lambda host: setattr(host, "pull_error", ENGINE_TEXT),
            ImagePullFailed,
            "pull_failed",
            id="error-line-in-the-stream",
        ),
        pytest.param(
            lambda host: host.route("POST", "/images/create", Refuse()),
            EnvironmentUnavailable,
            "unreachable",
            id="unreachable",
        ),
        pytest.param(
            lambda host: host.route("POST", "/images/create", Stall()),
            EnvironmentUnavailable,
            "outcome_unknown",
            id="no-answer",
        ),
    ],
)
async def test_a_failed_pull_writes_a_failure_row_with_a_reason_and_no_binding(
    image_rig: ImageRig, setup: Any, error: type[Exception], reason: str
) -> None:
    await register(image_rig, "only")
    setup(image_rig.host)

    with pytest.raises(error) as failed:
        await pull(image_rig)

    [row] = await rows_for(image_rig.db, "image_pulled")
    assert await binding_rows(image_rig.db) == []
    assert (row["result"], row["target_id"]) == ("failure", None)
    assert row["detail"]["reason"] == reason
    assert ENGINE_TEXT not in str(failed.value) + await every_activity_text(image_rig)
    assert SECRET not in await every_activity_text(image_rig)


async def test_a_pull_whose_inspect_afterwards_fails_still_binds_with_no_image_id_or_digest(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "only")
    image_rig.host.fail("image_inspect", 500)

    outcome = await pull(image_rig)

    [row] = await binding_rows(image_rig.db)
    [pulled] = await rows_for(image_rig.db, "image_pulled")
    assert (outcome.image_id, outcome.digest, outcome.binding) == (None, None, "created")
    assert row["observed_digest"] is None
    assert pulled["target_id"] is None


async def test_a_pull_by_digest_whose_inspect_fails_keeps_the_digest_the_reference_names(
    image_rig: ImageRig,
) -> None:
    image_rig.host.fail("image_inspect", 500)

    outcome = await pull(image_rig, f"busybox@{digest(3)}")

    assert (outcome.image_id, outcome.digest) == (None, digest(3))


async def test_progress_events_reach_the_sink_as_the_stream_delivers_them(
    image_rig: ImageRig,
) -> None:
    image_rig.host.pull_lines = [
        {"status": "Pulling fs layer", "id": "a1b2c3d4e5f6"},
        {"status": "Pull complete", "id": "a1b2c3d4e5f6"},
    ]
    sink = Sink()
    plan = await image_rig.images.actions.prepare_pull(ADMIN, ENV_A, HUB_REFERENCE)

    await image_rig.images.actions.pull(ADMIN, ENV_A, plan, progress=sink)

    assert [event.status for event in sink.pulled] == ["Pulling fs layer", "Pull complete"]


async def test_a_pull_while_another_holds_the_reference_is_busy_and_names_nobody_to_a_standard_user(
    image_rig: ImageRig,
) -> None:
    key = ObjectKey(ENV_A, "image_reference", HUB_KEY)
    holder = LockHolder(UserActor(STANDARD_ID), "standard-user", "pull")

    async with image_rig.engine.locks.hold(key, holder):
        with pytest.raises(ObjectBusy) as busy:
            await pull(image_rig, HUB_REFERENCE)

    [row] = await rows_for(image_rig.db, "image_pulled")
    assert row["detail"]["reason"] == "object_busy"
    assert image_rig.host.calls("POST", "/images/create") == []
    by_admin = image_rig.images.actions.describe_busy(busy.value, ADMIN)
    by_peer = image_rig.images.actions.describe_busy(busy.value, MEMBER)
    by_holder = image_rig.images.actions.describe_busy(busy.value, STANDARD)
    assert (by_admin.holder, by_peer.holder, by_holder.holder) == (
        "standard-user",
        "another user",
        "you",
    )
    assert by_peer.action == "pull"


async def test_cancelling_a_sent_pull_records_an_unknown_outcome_and_no_binding(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "only")
    gate = image_rig.host.hold("pull")
    plan = await image_rig.images.actions.prepare_pull(ADMIN, ENV_A, REFERENCE)
    task = asyncio.ensure_future(image_rig.images.actions.pull(ADMIN, ENV_A, plan))
    await asyncio.wait_for(gate.entered.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [row] = await rows_for(image_rig.db, "image_pulled")
    assert await binding_rows(image_rig.db) == []
    assert (row["result"], row["detail"]["reason"], row["detail"]["interrupted"]) == (
        "failure",
        "outcome_unknown",
        True,
    )


@pytest.mark.parametrize(
    ("who", "environment_id", "error"),
    [
        pytest.param(STANDARD, ENV_B, PermissionDenied, id="standard-user-on-the-local-socket"),
        pytest.param(STANDARD, 999, ResourceNotFound, id="unknown-environment"),
        pytest.param(ADMIN, 999, ResourceNotFound, id="administrator-unknown-environment"),
    ],
)
async def test_preparing_a_pull_the_caller_may_not_make_is_refused_without_a_row(
    image_rig: ImageRig, who: Any, environment_id: int, error: type[Exception]
) -> None:
    with pytest.raises(error):
        await image_rig.images.actions.prepare_pull(who, environment_id, REFERENCE)

    assert await rows_for(image_rig.db, "image_pulled") == []
    assert image_rig.host.requests == []


async def test_preparing_a_pull_of_a_malformed_reference_is_a_validation_error(
    image_rig: ImageRig,
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await image_rig.images.actions.prepare_pull(ADMIN, ENV_A, "Not A Reference")

    assert set(refused.value.fields) == {"reference"}


async def test_a_plan_for_another_environment_is_refused(image_rig: ImageRig) -> None:
    plan = await image_rig.images.actions.prepare_pull(ADMIN, ENV_A, HUB_REFERENCE)

    with pytest.raises(ValueError, match="another environment"):
        await image_rig.images.actions.pull(ADMIN, ENV_B, plan)


PUSH_TARGET = f"{ADDRESS}/{TAG_NAME}:1"
PUSH_PATH = f"/images/{ADDRESS}/{TAG_NAME}/push"


async def stage_for_push(rig: ImageRig, reference: str = PUSH_TARGET) -> str:
    rig.host.add_image(reference, full_id(PUSHED))
    return image_id(PUSHED)


async def push(
    rig: ImageRig,
    who: Any = ADMIN,
    reference: str = PUSH_TARGET,
    choice: CredentialChoice | None = None,
) -> Any:
    plan = await rig.images.actions.prepare_push(who, ENV_A, image_id(PUSHED), reference, choice)
    return await rig.images.actions.push(who, ENV_A, plan)


async def test_a_push_with_no_set_sends_the_empty_auth_document_and_binds_nothing(
    image_rig: ImageRig,
) -> None:
    await stage_for_push(image_rig)

    outcome = await push(image_rig)

    [pushed] = await rows_for(image_rig.db, "image_pushed")
    assert auth_of(image_rig, PUSH_PATH) == ANONYMOUS_AUTH
    assert (outcome.binding, outcome.digest) == ("skipped", DAEMON_DIGEST)
    assert await binding_rows(image_rig.db) == []
    assert (pushed["target_type"], pushed["target_id"]) == ("image", image_id(PUSHED))
    assert pushed["detail"]["digest"] == DAEMON_DIGEST
    assert image_rig.host.calls("POST", PUSH_PATH)[0].query == {"tag": "1"}


async def test_a_push_with_the_bound_set_sends_its_login_and_keeps_the_binding(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "first", default=True)
    bound = await register(image_rig, "second")
    await bind_image(image_rig.db, bound, ENV_A, PUSH_TARGET)
    await stage_for_push(image_rig)

    outcome = await push(image_rig)

    sent = decode_auth(auth_of(image_rig, PUSH_PATH) or "")
    assert outcome.binding == "kept"
    assert (sent["username"], sent["serveraddress"]) == ("user-second", ADDRESS)
    assert SECRET not in await every_activity_text(image_rig)


async def test_a_push_names_the_repository_as_the_daemon_holds_it_not_the_canonical_key(
    image_rig: ImageRig,
) -> None:
    await stage_for_push(image_rig, f"{TAG_NAME}:1")
    await register(image_rig, "hub", address="docker.io")

    await push(image_rig, reference=f"{TAG_NAME}:1")

    assert image_rig.host.calls("POST", f"/images/{TAG_NAME}/push")[0].query == {"tag": "1"}
    [row] = await binding_rows(image_rig.db)
    assert row["image_reference"] == f"docker.io/{TAG_NAME}:1"


async def test_a_push_without_an_aux_digest_takes_the_digest_the_daemon_holds_for_the_reference(
    image_rig: ImageRig,
) -> None:
    await stage_for_push(image_rig)
    image_rig.host.push_digest = None
    image_rig.host.add_image(None, full_id(PUSHED), digests=(f"{ADDRESS}/{TAG_NAME}@{digest(9)}",))

    outcome = await push(image_rig)

    assert outcome.digest == digest(9)


@pytest.mark.parametrize(
    ("reference", "field"),
    [
        pytest.param(f"{ADDRESS}/{TAG_NAME}:2", "reference", id="tag-not-on-the-image"),
        pytest.param(f"{ADDRESS}/{TAG_NAME}@{digest(1)}", "reference", id="digest"),
        pytest.param("Not A Reference", "reference", id="malformed"),
    ],
)
async def test_preparing_a_push_of_a_reference_the_image_does_not_hold_is_refused(
    image_rig: ImageRig, reference: str, field: str
) -> None:
    await stage_for_push(image_rig)

    with pytest.raises(ValidationFailed) as refused:
        await image_rig.images.actions.prepare_push(ADMIN, ENV_A, image_id(PUSHED), reference, None)

    assert set(refused.value.fields) == {field}


async def test_preparing_a_push_of_an_unknown_or_malformed_image_id_is_not_found(
    image_rig: ImageRig,
) -> None:
    for image in (image_id(77), "nginx:1"):
        with pytest.raises(ResourceNotFound):
            await image_rig.images.actions.prepare_push(ADMIN, ENV_A, image, PUSH_TARGET, None)


async def test_a_standard_user_may_push_to_a_remote_host_but_not_the_local_socket(
    image_rig: ImageRig,
) -> None:
    await stage_for_push(image_rig)
    await push(image_rig, who=STANDARD)

    with pytest.raises(PermissionDenied):
        await image_rig.images.actions.prepare_push(
            STANDARD, ENV_B, image_id(PUSHED), PUSH_TARGET, None
        )


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        pytest.param(
            lambda host: setattr(host, "push_status", 403), "not_found_or_denied", id="403"
        ),
        pytest.param(lambda host: setattr(host, "push_status", 500), "push_failed", id="500"),
        pytest.param(
            lambda host: setattr(host, "push_error", ENGINE_TEXT), "push_failed", id="line"
        ),
    ],
)
async def test_a_failed_push_writes_a_failure_row_and_no_binding(
    image_rig: ImageRig, setup: Any, reason: str
) -> None:
    await stage_for_push(image_rig)
    setup(image_rig.host)

    with pytest.raises(ImagePushFailed) as failed:
        await push(image_rig)

    [row] = await rows_for(image_rig.db, "image_pushed")
    assert await binding_rows(image_rig.db) == []
    assert (row["result"], row["detail"]["reason"], failed.value.reason) == (
        "failure",
        reason,
        reason,
    )
    assert ENGINE_TEXT not in str(failed.value) + await every_activity_text(image_rig)


async def test_a_push_whose_tag_moved_to_another_image_after_the_plan_is_refused(
    image_rig: ImageRig,
) -> None:
    await stage_for_push(image_rig)
    plan = await image_rig.images.actions.prepare_push(
        ADMIN, ENV_A, image_id(PUSHED), PUSH_TARGET, None
    )
    image_rig.host.add_image(PUSH_TARGET, full_id(PULLED))

    with pytest.raises(ImageChangedSinceReview):
        await image_rig.images.actions.push(ADMIN, ENV_A, plan)

    [row] = await rows_for(image_rig.db, "image_pushed")
    assert image_rig.host.calls("POST", PUSH_PATH) == []
    assert row["detail"]["reason"] == "reference_changed"


async def test_progress_events_of_a_push_reach_the_sink_with_the_final_aux_object(
    image_rig: ImageRig,
) -> None:
    await stage_for_push(image_rig)
    sink = Sink()
    plan = await image_rig.images.actions.prepare_push(
        ADMIN, ENV_A, image_id(PUSHED), PUSH_TARGET, None
    )

    await image_rig.images.actions.push(ADMIN, ENV_A, plan, progress=sink)

    assert [event.status for event in sink.pushed] == ["Preparing", "pushed"]
    assert sink.pushed[-1].aux is not None
    assert sink.pushed[-1].aux.digest == DAEMON_DIGEST


async def test_cancelling_a_push_records_an_unknown_outcome_against_the_image(
    image_rig: ImageRig,
) -> None:
    await stage_for_push(image_rig)
    gate = image_rig.host.hold("push")
    plan = await image_rig.images.actions.prepare_push(
        ADMIN, ENV_A, image_id(PUSHED), PUSH_TARGET, None
    )
    task = asyncio.ensure_future(image_rig.images.actions.push(ADMIN, ENV_A, plan))
    await asyncio.wait_for(gate.entered.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [row] = await rows_for(image_rig.db, "image_pushed")
    assert await binding_rows(image_rig.db) == []
    assert (row["target_id"], row["detail"]["reason"], row["detail"]["interrupted"]) == (
        image_id(PUSHED),
        "outcome_unknown",
        True,
    )
