"""The bulk binding pages: the review job, the review, the confirmed applies, and access."""

import asyncio
import re
from collections.abc import AsyncIterator
from html import unescape

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.auth.principal import Principal
from app.registry_client.manifests import ManifestCheckKind, check_message
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.references import ImageReference
from app.services.bulk_binding_plan import (
    BulkBindingReview,
    LeftOutImage,
    OtherImage,
    RegistryGroup,
    SetRef,
)
from app.services.bulk_bindings import (
    NEEDS_SET_MESSAGE,
    PLAN_NOT_OFFERED,
    BulkBindingService,
)
from app.services.image_bindings import ImageBindingService
from app.services.image_jobs import JobProgress, JobResult
from tests.auth_support import ADMIN_ID, csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.image_support import ScriptedManifests, digest, full_id
from tests.job_support import rows
from tests.registry_support import seed_default_entries
from tests.web.environment_support import World
from tests.web.flash_support import location
from tests.web.image_support import hidden_values, make_set
from tests.web.registry_support import visible_text

OK, NO_CONTENT, SEE_OTHER, BAD_REQUEST, NOT_FOUND, CONFLICT, UNAVAILABLE = (
    200,
    204,
    303,
    400,
    404,
    409,
    503,
)
HTMX = {"HX-Request": "true"}
NOSCRIPT_REFRESH = '<noscript><meta http-equiv="refresh" content="5"></noscript>'
POLL_EVERY = 'hx-trigger="every 2s"'
LOCAL = digest(1)
HUB_IMAGE = "busybox:1"
HUB_KEY = "docker.io/library/busybox:1"
MIRROR = "mirror.example.com:5000"
THIRD = "third.example"
FOURTH = "fourth.example"
TOKEN_IN_LINK = re.compile(r"/anonymous-binding/reviews/([A-Za-z0-9_-]{22})")
JOB_IN_LINK = re.compile(r"/anonymous-binding/jobs/([A-Za-z0-9_-]{22})")
OPTION = re.compile(r'<option value="([^"]*)"( selected)?>([^<]*)</option>')
STRANGER = Principal(999, "stranger", "administrator")
ACTION = "Bind unbound images"


class ByLogin(ScriptedManifests):
    """Answers by the login's username, None for anonymous; anything unlisted is refused."""

    def __init__(self, answers: dict[str | None, str]) -> None:
        super().__init__()
        self.by_login: dict[str | None, str | ManifestCheckKind] = dict(answers)

    async def head_digest(
        self, target: RegistryTarget, reference: ImageReference, login: RegistryLogin | None
    ) -> str:
        self.default = self.by_login.get(
            None if login is None else login.username, ManifestCheckKind.AUTHENTICATION
        )
        return await super().head_digest(target, reference, login)

    def logins(self, address: str) -> list[bool]:
        """Return, for each HEAD at the address, whether it carried no login."""
        return [login is None for target, _, login in self.calls if target.address == address]


@pytest.fixture
async def hub(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> AsyncIterator[ByLogin]:
    """The remote environment holds busybox pulled outside dockmin, and a set at Docker Hub."""
    container_host.add_image(HUB_IMAGE, full_id(1), digests=(f"docker.io/library/busybox@{LOCAL}",))
    await make_set(admin_client, world, "alpha", address="docker.io", username="user-alpha")
    registries = world.app.state.registries
    real = registries.manifests
    manifests = ByLogin({"user-alpha": LOCAL})
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


async def set_ids(world: World) -> dict[str, int]:
    found = await rows(world.app, "SELECT id, name FROM registry_credential")
    return {str(row["name"]): int(row["id"]) for row in found}


async def apply(
    client: AsyncClient, world: World, token: str, page: Response, **fields: str | list[str]
) -> Response:
    """Post the review's own registry and plan fields, as its form would, plus the extras."""
    return await client.post(
        f"{base(world)}/reviews/{token}/apply",
        data=csrf_form(
            client,
            registry=hidden_values(page, "registry"),
            expected_plan=hidden_values(page, "expected_plan"),
            **fields,
        ),
    )


async def test_every_route_answers_a_standard_user_as_missing_and_writes_nothing(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    hub: ByLogin,
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
    admin_client: AsyncClient, standard_client: AsyncClient, world: World, hub: ByLogin
) -> None:
    path = f"/environments/{world.remote_id}/images"

    administrator = await admin_client.get(path)
    standard = await standard_client.get(path)

    assert (administrator.status_code, standard.status_code) == (OK, OK)
    assert ACTION in administrator.text and "anonymously" not in text_of(administrator)
    assert ACTION not in standard.text


async def test_a_review_lists_each_registry_and_each_apply_binds_what_it_confirmed_alone(
    admin_client: AsyncClient,
    world: World,
    hub: ByLogin,
    container_host: ContainerEngine,
) -> None:
    for number, (name, address) in enumerate(
        (("mirror", MIRROR), ("third", THIRD), ("fourth", FOURTH)), start=2
    ):
        container_host.add_image(
            f"{address}/app:1", full_id(number), digests=(f"{address}/app@{LOCAL}",)
        )
        await make_set(admin_client, world, name, address=address, username=f"user-{name}")
    hub.by_login.update(
        {"user-mirror": LOCAL, "user-third": digest(9), "user-fourth": LOCAL, None: LOCAL}
    )
    _, token = await reviewed(admin_client, world)
    review = await admin_client.get(f"{base(world)}/reviews/{token}")
    ids = await set_ids(world)

    text = text_of(review)
    assert review.status_code == OK
    assert "Bind unbound images" in text and "anonymously" not in text
    assert "Bind these 1 image at docker.io" in text
    assert f"Bind these 1 image at {MIRROR}" in text and "Apply all bindings" in text
    assert "Works without binding." in text and f"{THIRD}/app:1" in text
    assert hub.logins("docker.io") == [False] and hub.logins(FOURTH) == [False]
    assert sorted(hub.logins(THIRD)) == [False, True]
    assert await stored_bindings(world) == []
    heads = len(hub.calls)

    only = await apply(admin_client, world, token, review, only=FOURTH)
    after_only = await admin_client.get(location(only))

    assert only.status_code == SEE_OTHER
    assert location(only).endswith(f"/reviews/{token}")
    assert [r["image_reference"] for r in await stored_bindings(world)] == [f"{FOURTH}/app:1"]
    page = text_of(after_only)
    assert "1 bound, 0 skipped, 0 failed, 0 changed at fourth.example." in page
    assert (
        f"Bind these 1 image at {FOURTH}" not in page and "Bind these 1 image at docker.io" in page
    )

    await make_set(admin_client, world, "extra", address="docker.io", username="user-extra")
    everything = await apply(admin_client, world, token, after_only)
    after_all = text_of(await admin_client.get(location(everything)))

    assert everything.status_code == SEE_OTHER
    assert "1 bound, 0 skipped, 0 failed, 1 changed at docker.io" in after_all
    assert "Changed since the review; nothing was bound at this address." in after_all
    assert "Bind these 1 image at docker.io" in after_all
    assert f"Bind these 1 image at {MIRROR}" not in after_all
    assert {
        r["image_reference"]: r["registry_credential_id"] for r in await stored_bindings(world)
    } == {
        f"{FOURTH}/app:1": ids["fourth"],
        f"{MIRROR}/app:1": ids["mirror"],
    }
    events = await rows(
        world.app, "SELECT target_id, detail FROM activity_event WHERE action = 'image_bound'"
    )
    assert [e["target_id"] for e in events] == [f"{FOURTH}/app:1", f"{MIRROR}/app:1"]
    assert all('"bulk":true' in e["detail"].replace(" ", "") for e in events)
    assert len(hub.calls) == heads
    assert container_host.mutations() == []
    assert container_host.verbs() == []


async def test_an_image_two_sets_read_is_a_picker_with_nothing_preselected_and_the_choice_is_bound(
    admin_client: AsyncClient, world: World, hub: ByLogin
) -> None:
    await make_set(admin_client, world, "bravo", address="docker.io", username="user-bravo")
    await make_set(admin_client, world, "charlie", address="docker.io", username="user-charlie")
    hub.by_login["user-bravo"] = LOCAL
    _, token = await reviewed(admin_client, world)
    review = await admin_client.get(f"{base(world)}/reviews/{token}")
    ids = await set_ids(world)

    options = OPTION.findall(review.text)
    assert [(unescape(v), bool(sel), name) for v, sel, name in options] == [
        (f"{HUB_KEY} none", True, "Leave unbound"),
        (f"{HUB_KEY} {ids['alpha']}", False, "alpha"),
        (f"{HUB_KEY} {ids['bravo']}", False, "bravo"),
    ]
    assert "Bind the chosen images at docker.io" in text_of(review)

    refused = await apply(admin_client, world, token, review, choice=f"{HUB_KEY} {ids['charlie']}")
    unchosen = await apply(admin_client, world, token, review, choice=f"{HUB_KEY} none")
    left = text_of(await admin_client.get(location(unchosen)))
    bound = await apply(admin_client, world, token, review, choice=f"{HUB_KEY} {ids['bravo']}")

    assert refused.status_code == BAD_REQUEST and "Check the selection" in text_of(refused)
    assert unchosen.status_code == SEE_OTHER
    assert "0 bound, 0 skipped, 0 failed, 0 changed, 1 left unbound" in left
    assert bound.status_code == SEE_OTHER
    assert [
        (r["image_reference"], r["registry_credential_id"]) for r in await stored_bindings(world)
    ] == [(HUB_KEY, ids["bravo"])]


async def test_a_plan_that_is_not_the_one_the_review_offered_is_refused_and_nothing_is_written(
    admin_client: AsyncClient, world: World, hub: ByLogin
) -> None:
    _, token = await reviewed(admin_client, world)

    refused = await admin_client.post(
        f"{base(world)}/reviews/{token}/apply",
        data=csrf_form(admin_client, registry="docker.io", expected_plan="0" * 64),
    )

    assert refused.status_code == BAD_REQUEST
    assert PLAN_NOT_OFFERED in text_of(refused)
    assert await stored_bindings(world) == []
    assert await rows(world.app, "SELECT id FROM activity_event WHERE action = 'image_bound'") == []


async def test_a_review_that_is_not_the_signed_in_users_reads_as_expired(
    admin_client: AsyncClient, world: World, hub: ByLogin
) -> None:
    engine = world.app.state.engine

    async def run(progress: JobProgress) -> JobResult:
        return JobResult("done")

    foreign_job = engine.image_jobs.start(
        "bulk_binding_review", STRANGER, world.remote_id, "someone else's", run
    )
    foreign = engine.bulk_binding_reviews.put(
        STRANGER.user_id,
        world.remote_id,
        BulkBindingReview(world.remote_id, "x", (), (), 0, 0),
    )
    await settle(world.app, foreign_job)

    job = await admin_client.get(f"{base(world)}/jobs/{foreign_job}")
    review = await admin_client.get(f"{base(world)}/reviews/{foreign}")
    apply_response = await admin_client.post(
        f"{base(world)}/reviews/{foreign}/apply",
        data=csrf_form(admin_client, registry="docker.io", expected_plan="x"),
    )

    assert job.status_code == NOT_FOUND
    assert (review.status_code, apply_response.status_code) == (CONFLICT, CONFLICT)
    assert "has expired" in text_of(review) and "has expired" in text_of(apply_response)


async def test_the_environment_page_names_the_registry_addresses_an_image_check_contacts(
    admin_client: AsyncClient, world: World, hub: ByLogin
) -> None:
    granted = text_of(await admin_client.get(f"/environments/{world.remote_id}"))
    ungranted = text_of(await admin_client.get(f"/environments/{world.local_id}"))

    assert "sends manifest HEAD requests to these registry addresses: docker.io ." in granted
    assert "No registry credential set is granted to this environment" in ungranted


async def test_the_registries_page_labels_the_defaults(
    admin_client: AsyncClient, world: World
) -> None:
    async with world.app.state.database.session() as db:
        await seed_default_entries(db)
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


async def test_the_review_page_states_the_unchecked_count_and_shows_the_sets_failures(
    admin_client: AsyncClient, world: World, hub: ByLogin
) -> None:
    group = RegistryGroup(
        address="docker.io",
        sets=(SetRef(1, "alpha"),),
        bindable=(),
        others=(
            OtherImage(
                "docker.io/library/x:1",
                "x:1",
                "needs_set",
                NEEDS_SET_MESSAGE,
                (f"alpha: {check_message(ManifestCheckKind.NO_DIGEST)}",),
            ),
        ),
        others_total=1,
    )
    left = LeftOutImage("custom.example/y:1", "custom.example/y:1", "no_credential_set")
    held = BulkBindingReview(world.remote_id, "x", (group,), (left,), 1, 7)
    token = world.app.state.engine.bulk_binding_reviews.put(ADMIN_ID, world.remote_id, held)

    page = text_of(await admin_client.get(f"{base(world)}/reviews/{token}"))

    assert "7 not checked; start another review to check the next ones." in page
    assert f"alpha: {check_message(ManifestCheckKind.NO_DIGEST)}" in page
    assert "No credential set at this address; updates use anonymous access." in page
    assert "No image is left to bind" not in page and "No unbound image can be bound" in page


async def test_a_review_that_is_being_applied_answers_a_second_apply_as_busy(
    admin_client: AsyncClient, world: World, hub: ByLogin
) -> None:
    _, token = await reviewed(admin_client, world)
    page = await admin_client.get(f"{base(world)}/reviews/{token}")
    reviews = world.app.state.engine.bulk_binding_reviews
    assert reviews.begin_apply(token, ADMIN_ID, world.remote_id)

    busy = await apply(admin_client, world, token, page)

    assert busy.status_code == CONFLICT
    assert "Already being applied" in text_of(busy)
    assert await stored_bindings(world) == []


async def test_apply_on_an_unreachable_environment_shows_the_unavailable_page_and_writes_nothing(
    admin_client: AsyncClient,
    world: World,
    hub: ByLogin,
    container_host: ContainerEngine,
) -> None:
    _, token = await reviewed(admin_client, world)
    page = await admin_client.get(f"{base(world)}/reviews/{token}")
    container_host.fail("image_list", 500)

    refused = await apply(admin_client, world, token, page)

    assert refused.status_code == UNAVAILABLE
    assert "Bind unbound images" in text_of(refused)
    assert await stored_bindings(world) == []


async def test_a_binding_the_write_refuses_reads_failed_on_the_review(
    admin_client: AsyncClient, world: World, hub: ByLogin, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, token = await reviewed(admin_client, world)
    page = await admin_client.get(f"{base(world)}/reviews/{token}")
    real = ImageBindingService.bind_verified

    async def withdrawn(self: ImageBindingService, *args: object, **kwargs: object) -> str:
        async with world.app.state.database.session() as db:
            await db.execute("DELETE FROM registry_environment_access")
        return await real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ImageBindingService, "bind_verified", withdrawn)

    applied = await apply(admin_client, world, token, page)
    result = text_of(await admin_client.get(location(applied)))

    assert applied.status_code == SEE_OTHER
    assert "0 bound, 0 skipped, 1 failed, 0 changed at docker.io" in result
    assert "Failed" in result
    assert await stored_bindings(world) == []


async def start_job(client: AsyncClient, world: World) -> str:
    started = await client.post(base(world), data=csrf_form(client))
    found = JOB_IN_LINK.search(location(started))
    assert found is not None
    return found.group(1)


async def test_a_review_job_that_fails_says_so_and_offers_no_review(
    admin_client: AsyncClient, world: World, hub: ByLogin, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(*args: object, **kwargs: object) -> BulkBindingReview:
        raise RuntimeError("boom")

    monkeypatch.setattr(BulkBindingService, "review", crash)
    job_id = await start_job(admin_client, world)
    await settle(world.app, job_id)

    page = await admin_client.get(f"{base(world)}/jobs/{job_id}")

    assert "The review failed." in text_of(page)
    assert "The transfer failed." not in text_of(page)
    assert TOKEN_IN_LINK.search(page.text) is None


async def test_a_running_review_polls_its_panel_and_the_poll_opens_the_finished_review(
    admin_client: AsyncClient, world: World, hub: ByLogin
) -> None:
    hub.gate = asyncio.Event()
    job_id = await start_job(admin_client, world)
    await asyncio.wait_for(hub.entered.wait(), timeout=30)
    job_path = f"{base(world)}/jobs/{job_id}"

    page = await admin_client.get(job_path)
    polled = await admin_client.get(job_path, headers=HTMX)
    hub.gate.set()
    await settle(world.app, job_id)
    finished = await admin_client.get(job_path, headers=HTMX)

    assert NOSCRIPT_REFRESH in page.text
    assert "<html" not in polled.text and POLL_EVERY in polled.text
    assert f'hx-get="{job_path}"' in polled.text
    assert finished.status_code == NO_CONTENT
    assert TOKEN_IN_LINK.search(finished.headers["HX-Redirect"]) is not None


async def test_an_owner_cancelling_a_running_review_ends_it_with_nothing_written(
    admin_client: AsyncClient, world: World, hub: ByLogin
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
