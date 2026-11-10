"""The bulk anonymous binding pages: the review job, the review, the confirmed apply, and access."""

import asyncio
import re
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.auth.principal import Principal
from app.db.repositories.registry_access import RegistryAccessRepository
from app.registry_client.manifests import ManifestCheckKind, check_message
from app.registry_client.probe import RegistryTarget
from app.registry_client.references import ImageReference
from app.services.anonymous_binding_plan import (
    AnonymousBindingReview,
    LeftOutImage,
    RegistryGroup,
    UnmatchedImage,
)
from app.services.anonymous_bindings import AnonymousBindingService
from app.services.image_bindings import ImageBindingService
from app.services.image_jobs import JobProgress, JobResult
from tests.auth_support import ADMIN_ID, csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.image_support import ScriptedManifests, digest, full_id
from tests.job_support import rows
from tests.registry_support import seed_default_entries
from tests.web.environment_support import World
from tests.web.flash_support import location
from tests.web.registry_support import visible_text

OK, SEE_OTHER, NOT_FOUND, CONFLICT, UNAVAILABLE = 200, 303, 404, 409, 503
LOCAL = digest(1)
OTHER_LOCAL = digest(2)
HUB_IMAGE = "busybox:1"
HUB_KEY = "docker.io/library/busybox:1"
TOKEN_IN_LINK = re.compile(r"/anonymous-binding/reviews/([A-Za-z0-9_-]{22})")
JOB_IN_LINK = re.compile(r"/anonymous-binding/jobs/([A-Za-z0-9_-]{22})")
EXPECTED_PLAN = re.compile(r'name="expected_plan" value="([0-9a-f]{64})"')
STRANGER = Principal(999, "stranger", "administrator")
ACTION = "Bind unbound images anonymously"


class AnonymousOnly(ScriptedManifests):
    """Answers the local digest to an anonymous HEAD and refuses a request carrying a login."""

    async def head_digest(
        self, target: RegistryTarget, reference: ImageReference, login: object | None
    ) -> str:
        assert login is None, "a credential was sent"
        return await super().head_digest(target, reference, None)


@pytest.fixture
async def hub(world: World, container_host: ContainerEngine) -> AsyncIterator[ScriptedManifests]:
    """The remote environment holds busybox pulled outside dockmin, and the Hub entry is granted."""
    container_host.add_image(HUB_IMAGE, full_id(1), digests=(f"docker.io/library/busybox@{LOCAL}",))
    async with world.app.state.database.session() as db:
        hub_entry, _ = await seed_default_entries(db)
        await RegistryAccessRepository(db).add_environment(hub_entry, world.remote_id)
    registries = world.app.state.registries
    real = registries.manifests
    manifests = AnonymousOnly(LOCAL)
    registries.manifests = manifests
    yield manifests
    registries.manifests = real


def base(world: World) -> str:
    return f"/environments/{world.remote_id}/images/anonymous-binding"


def text_of(response: Response) -> str:
    return " ".join(visible_text(response.text).split())


async def settle(app: FastAPI, job_id: str) -> None:
    task = app.state.engine.image_jobs._jobs[job_id].task
    done, _ = await asyncio.wait({task}, timeout=30)
    assert done, "the review job did not finish"


async def reviewed(client: AsyncClient, world: World) -> tuple[str, str]:
    """Run the review and return the job id and the review token."""
    started = await client.post(base(world), data=csrf_form(client))
    assert started.status_code == SEE_OTHER, started.text
    found = JOB_IN_LINK.search(location(started))
    assert found is not None
    await settle(world.app, found.group(1))
    page = await client.get(f"{base(world)}/jobs/{found.group(1)}")
    token = TOKEN_IN_LINK.search(page.text)
    assert token is not None, text_of(page)
    return found.group(1), token.group(1)


async def stored_bindings(world: World) -> list[dict[str, object]]:
    return await rows(
        world.app,
        "SELECT image_reference, registry_credential_id, observed_digest, state "
        "FROM image_credential_binding",
    )


async def test_every_route_answers_a_standard_user_as_missing_and_writes_nothing(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    hub: ScriptedManifests,
) -> None:
    job_id, token = await reviewed(admin_client, world)
    calls = len(hub.calls)
    form = csrf_form(standard_client, registry="docker.io", expected_plan="x")

    answers = [
        (await standard_client.post(base(world), data=form)).status_code,
        (await standard_client.get(f"{base(world)}/jobs/{job_id}")).status_code,
        (await standard_client.post(f"{base(world)}/jobs/{job_id}/cancel", data=form)).status_code,
        (await standard_client.get(f"{base(world)}/reviews/{token}")).status_code,
        (await standard_client.post(f"{base(world)}/reviews/{token}/apply", data=form)).status_code,
    ]

    assert answers == [NOT_FOUND] * len(answers)
    assert len(hub.calls) == calls
    assert await stored_bindings(world) == []
    assert await rows(world.app, "SELECT id FROM activity_event WHERE action = 'image_bound'") == []


async def test_the_images_list_offers_the_action_to_an_administrator_only(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World, hub: ScriptedManifests
) -> None:
    path = f"/environments/{world.remote_id}/images"

    administrator = await admin_client.get(path)
    standard = await standard_client.get(path)

    assert (administrator.status_code, standard.status_code) == (OK, OK)
    assert ACTION in administrator.text
    assert ACTION not in standard.text


async def test_the_review_lists_each_registry_with_its_references_and_binds_nothing_yet(
    admin_client: AsyncClient, world: World, hub: ScriptedManifests
) -> None:
    job_id, token = await reviewed(admin_client, world)

    job = await admin_client.get(f"{base(world)}/jobs/{job_id}")
    review = await admin_client.get(f"{base(world)}/reviews/{token}")

    text = text_of(review)
    assert review.status_code == OK
    assert "docker.io" in text and HUB_KEY.removeprefix("docker.io/library/") in text
    assert LOCAL in text and "Docker Hub (anonymous)" in text
    assert "The review is ready." in text_of(job)
    assert "Bind these 1 image at docker.io anonymously" in text
    assert [call[1].key for call in hub.calls] == [HUB_KEY]
    assert await stored_bindings(world) == []


async def test_confirming_a_registry_binds_its_images_and_the_result_page_says_so(
    admin_client: AsyncClient,
    world: World,
    hub: ScriptedManifests,
    container_host: ContainerEngine,
) -> None:
    _, token = await reviewed(admin_client, world)
    review = await admin_client.get(f"{base(world)}/reviews/{token}")
    plan = EXPECTED_PLAN.search(review.text)
    assert plan is not None
    heads = len(hub.calls)

    applied = await admin_client.post(
        f"{base(world)}/reviews/{token}/apply",
        data=csrf_form(admin_client, registry="docker.io", expected_plan=plan.group(1)),
    )
    result = await admin_client.get(location(applied))

    assert applied.status_code == SEE_OTHER
    assert "1 bound, 0 skipped, 0 failed at docker.io" in text_of(result)
    assert await stored_bindings(world) == [
        {
            "image_reference": HUB_KEY,
            "registry_credential_id": None,
            "observed_digest": LOCAL,
            "state": "bound",
        }
    ]
    [event] = await rows(
        world.app, "SELECT detail FROM activity_event WHERE action = 'image_bound'"
    )
    assert '"bulk":true' in event["detail"].replace(" ", "")
    assert len(hub.calls) == heads
    assert container_host.mutations() == []
    assert container_host.verbs() == []


async def test_a_plan_that_changed_since_the_review_is_refused_and_nothing_is_written(
    admin_client: AsyncClient, world: World, hub: ScriptedManifests
) -> None:
    _, token = await reviewed(admin_client, world)
    review = await admin_client.get(f"{base(world)}/reviews/{token}")
    plan = EXPECTED_PLAN.search(review.text)
    assert plan is not None
    async with world.app.state.database.session() as db:
        await db.execute(
            "INSERT INTO image_credential_binding (environment_id, image_reference, "
            "registry_credential_id, state, bound_at, updated_at) "
            "VALUES (?, ?, NULL, 'bound', '2026-01-02T03:04:05Z', '2026-01-02T03:04:05Z')",
            (world.remote_id, HUB_KEY),
        )

    refused = await admin_client.post(
        f"{base(world)}/reviews/{token}/apply",
        data=csrf_form(admin_client, registry="docker.io", expected_plan=plan.group(1)),
    )

    assert refused.status_code == CONFLICT
    assert "The host changed" in text_of(refused)
    assert await rows(world.app, "SELECT id FROM activity_event WHERE action = 'image_bound'") == []


async def test_a_review_that_is_not_the_signed_in_users_reads_as_expired(
    admin_client: AsyncClient, world: World, hub: ScriptedManifests
) -> None:
    engine = world.app.state.engine

    async def run(progress: JobProgress) -> JobResult:
        return JobResult("done")

    foreign_job = engine.image_jobs.start(
        "anonymous_binding_review", STRANGER, world.remote_id, "someone else's", run
    )
    foreign = engine.anonymous_binding_reviews.put(
        STRANGER.user_id,
        world.remote_id,
        AnonymousBindingReview(world.remote_id, "x", (), (), 0, 0),
    )
    await settle(world.app, foreign_job)

    job = await admin_client.get(f"{base(world)}/jobs/{foreign_job}")
    review = await admin_client.get(f"{base(world)}/reviews/{foreign}")
    apply = await admin_client.post(
        f"{base(world)}/reviews/{foreign}/apply",
        data=csrf_form(admin_client, registry="docker.io", expected_plan="x"),
    )

    assert job.status_code == NOT_FOUND
    assert (review.status_code, apply.status_code) == (CONFLICT, CONFLICT)
    assert "has expired" in text_of(review) and "has expired" in text_of(apply)


async def test_the_environment_page_names_the_registry_addresses_an_image_check_contacts(
    admin_client: AsyncClient, world: World, hub: ScriptedManifests
) -> None:
    granted = text_of(await admin_client.get(f"/environments/{world.remote_id}"))
    ungranted = text_of(await admin_client.get(f"/environments/{world.local_id}"))

    assert "sends manifest HEAD requests to these registry addresses: docker.io ." in granted
    assert "No registry credential set is granted to this environment" in ungranted


async def test_the_registries_page_labels_the_defaults(
    admin_client: AsyncClient, world: World, hub: ScriptedManifests
) -> None:
    page = await admin_client.get("/registries")
    [hub_id] = [
        row["id"]
        for row in await rows(
            world.app, "SELECT id FROM registry_credential WHERE url = 'docker.io'"
        )
    ]
    detail = await admin_client.get(f"/registries/{hub_id}")

    assert text_of(page).count("Added with this instance") == 2
    assert "Added with this instance" in text_of(detail)


async def test_the_review_page_states_the_unchecked_count_and_shows_the_clients_messages(
    admin_client: AsyncClient, world: World, hub: ScriptedManifests
) -> None:
    group = RegistryGroup(
        address="docker.io",
        entries=("Docker Hub (anonymous)",),
        authenticated_sets=0,
        matched=(),
        unmatched=(
            UnmatchedImage(
                "docker.io/library/x:1",
                "x:1",
                "no_digest",
                check_message(ManifestCheckKind.NO_DIGEST),
            ),
        ),
        unmatched_total=1,
    )
    left = LeftOutImage("custom.example/y:1", "custom.example/y:1", "entry_tls_settings")
    held = AnonymousBindingReview(world.remote_id, "x", (group,), (left,), 1, 7)
    token = world.app.state.engine.anonymous_binding_reviews.put(ADMIN_ID, world.remote_id, held)

    page = text_of(await admin_client.get(f"{base(world)}/reviews/{token}"))

    assert "7 not checked; start another review to check the next ones." in page
    assert check_message(ManifestCheckKind.NO_DIGEST) in page
    assert "uses its own certificate settings" in page


async def test_a_review_that_is_being_applied_answers_a_second_apply_as_busy(
    admin_client: AsyncClient, world: World, hub: ScriptedManifests
) -> None:
    _, token = await reviewed(admin_client, world)
    plan = EXPECTED_PLAN.search((await admin_client.get(f"{base(world)}/reviews/{token}")).text)
    assert plan is not None
    reviews = world.app.state.engine.anonymous_binding_reviews
    assert reviews.begin_apply(token, ADMIN_ID, world.remote_id)

    busy = await admin_client.post(
        f"{base(world)}/reviews/{token}/apply",
        data=csrf_form(admin_client, registry="docker.io", expected_plan=plan.group(1)),
    )

    assert busy.status_code == CONFLICT
    assert "Already being applied" in text_of(busy)
    assert await stored_bindings(world) == []


async def test_apply_on_an_unreachable_environment_shows_the_unavailable_page_and_writes_nothing(
    admin_client: AsyncClient,
    world: World,
    hub: ScriptedManifests,
    container_host: ContainerEngine,
) -> None:
    _, token = await reviewed(admin_client, world)
    plan = EXPECTED_PLAN.search((await admin_client.get(f"{base(world)}/reviews/{token}")).text)
    assert plan is not None
    container_host.fail("image_list", 500)

    refused = await admin_client.post(
        f"{base(world)}/reviews/{token}/apply",
        data=csrf_form(admin_client, registry="docker.io", expected_plan=plan.group(1)),
    )

    assert refused.status_code == UNAVAILABLE
    assert "Bind unbound images anonymously" in text_of(refused)
    assert await stored_bindings(world) == []


async def test_a_binding_the_write_refuses_reads_failed_on_the_result_page(
    admin_client: AsyncClient, world: World, hub: ScriptedManifests, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = await reviewed(admin_client, world)
    plan = EXPECTED_PLAN.search((await admin_client.get(f"{base(world)}/reviews/{token}")).text)
    assert plan is not None
    real = ImageBindingService.bind_verified

    async def withdrawn(self: ImageBindingService, *args: object, **kwargs: object) -> str:
        async with world.app.state.database.session() as db:
            await db.execute("DELETE FROM registry_environment_access")
        return await real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ImageBindingService, "bind_verified", withdrawn)

    applied = await admin_client.post(
        f"{base(world)}/reviews/{token}/apply",
        data=csrf_form(admin_client, registry="docker.io", expected_plan=plan.group(1)),
    )
    result = text_of(await admin_client.get(location(applied)))

    assert applied.status_code == SEE_OTHER
    assert "0 bound, 0 skipped, 1 failed at docker.io" in result
    assert "Failed" in result
    assert await stored_bindings(world) == []


async def start_job(client: AsyncClient, world: World) -> str:
    started = await client.post(base(world), data=csrf_form(client))
    found = JOB_IN_LINK.search(location(started))
    assert found is not None
    return found.group(1)


async def test_a_review_job_that_fails_says_so_and_offers_no_review(
    admin_client: AsyncClient, world: World, hub: ScriptedManifests, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(*args: object, **kwargs: object) -> AnonymousBindingReview:
        raise RuntimeError("boom")

    monkeypatch.setattr(AnonymousBindingService, "review", crash)
    job_id = await start_job(admin_client, world)
    await settle(world.app, job_id)

    page = await admin_client.get(f"{base(world)}/jobs/{job_id}")

    assert "The review failed." in text_of(page)
    assert "The transfer failed." not in text_of(page)
    assert TOKEN_IN_LINK.search(page.text) is None


async def test_an_owner_cancelling_a_running_review_ends_it_with_nothing_written(
    admin_client: AsyncClient, world: World, hub: ScriptedManifests
) -> None:
    hub.gate = asyncio.Event()
    job_id = await start_job(admin_client, world)
    await asyncio.wait_for(hub.entered.wait(), timeout=30)

    cancelled = await admin_client.post(
        f"{base(world)}/jobs/{job_id}/cancel", data=csrf_form(admin_client)
    )
    await settle(world.app, job_id)
    page = await admin_client.get(f"{base(world)}/jobs/{job_id}")

    assert cancelled.status_code == SEE_OTHER
    assert "The review was cancelled. Nothing was written." in text_of(page)
    assert TOKEN_IN_LINK.search(page.text) is None
    assert await stored_bindings(world) == []
