"""Reading images: the live list and its filters, the detail, history, impacts and visibility."""

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.services.image_listing import ImageListTooLarge
from app.services.image_queries import remaining_after_untag
from app.services.image_views import ImageFilters, ImageTooLarge, PrunePlan, references_digest
from app.services.object_inspector import EnvironmentUnavailable
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, container_id, container_key
from tests.access_support import control as put_control
from tests.auth_support import STANDARD_ID
from tests.docker_engine.container_engine import ENGINE_TEXT
from tests.docker_engine.fake_engine import Reply
from tests.image_support import (
    digest,
    full_id,
    image_id,
    mark_missing,
    register,
)
from tests.registry_support import ADDRESS, bind_image
from tests.services.conftest import ImageRig

MIB = 1024 * 1024
NGINX = 1
GHCR = 2
DANGLING = 3
DIGEST_ONLY = 4
SECRET_LABEL = "label-secret-marker-5531"
SECRET_ENV = "env-secret-marker-7712"
BUILD_COMMAND = "RUN build-command-marker-3321"
UNKNOWN_ENVIRONMENT = 999

FIRST_PAGE_ORDER = [image_id(GHCR), image_id(NGINX), image_id(DANGLING), image_id(DIGEST_ONLY)]
KEYS_BY_IMAGE = {
    NGINX: ["docker.io/library/nginx:1", "docker.io/library/nginx:latest"],
    GHCR: ["ghcr.io/o/app:2"],
}


@pytest.fixture
async def stocked(image_rig: ImageRig) -> ImageRig:
    """A daemon with a used two-tag image, an unused one, a dangling one and a digest-only one."""
    host = image_rig.host
    host.add_image("nginx:1", full_id(NGINX), size=300)
    host.add_image("nginx:latest", full_id(NGINX))
    host.add_image("ghcr.io/o/app:2", full_id(GHCR), size=100)
    host.add_image(None, full_id(DANGLING), size=50)
    host.add_image(None, full_id(DIGEST_ONLY), digests=(f"ghcr.io/o/old@{digest(4)}",), size=70)
    host.add(container_id(1), "web", image="nginx:1", image_id=full_id(NGINX))
    return image_rig


def test_untagging_the_last_tag_of_a_repository_also_drops_its_digest_references() -> None:
    keys = [
        "docker.io/library/busybox:1",
        f"docker.io/library/busybox@{digest(1)}",
        f"ghcr.io/o/app@{digest(2)}",
    ]

    left = remaining_after_untag(keys, "docker.io/library/busybox:1")

    assert left == (f"ghcr.io/o/app@{digest(2)}",)


@pytest.mark.parametrize(
    ("keys", "target", "left"),
    [
        pytest.param(
            ["docker.io/library/busybox:1", "docker.io/library/busybox:2"],
            "docker.io/library/busybox:1",
            ("docker.io/library/busybox:2",),
            id="another-tag-of-the-repository-remains",
        ),
        pytest.param(
            ["docker.io/library/busybox:1", "ghcr.io/o/busybox:1"],
            "docker.io/library/busybox:1",
            ("ghcr.io/o/busybox:1",),
            id="a-tag-of-another-repository-remains",
        ),
        pytest.param(
            [f"docker.io/library/busybox@{digest(1)}", f"docker.io/library/busybox@{digest(2)}"],
            f"docker.io/library/busybox@{digest(1)}",
            (f"docker.io/library/busybox@{digest(2)}",),
            id="a-digest-only-image",
        ),
        pytest.param(
            ["docker.io/library/busybox:1"], "docker.io/library/busybox:1", (), id="the-only-tag"
        ),
    ],
)
def test_what_remains_after_an_untag_is_worked_out_as_the_daemon_does(
    keys: list[str], target: str, left: tuple[str, ...]
) -> None:
    assert remaining_after_untag(keys, target) == left


def test_a_reference_digest_does_not_depend_on_order_or_repeats() -> None:
    assert references_digest(["b", "a", "b"]) == references_digest(["a", "b"])
    assert references_digest(["a"]) != references_digest(["a", "b"])


async def test_the_list_shows_every_image_with_dangling_ones_last_and_each_reference_bound_or_not(
    stocked: ImageRig,
) -> None:
    anywhere = await register(stocked, "anywhere", address="ghcr.io")
    await bind_image(stocked.db, anywhere, ENV_A, "ghcr.io/o/app:2")

    page = await stocked.images.queries.page(ADMIN, ENV_A)

    nginx = next(row for row in page.page.items if row.id == image_id(NGINX))
    ghcr = next(row for row in page.page.items if row.id == image_id(GHCR))
    assert [row.id for row in page.page.items] == FIRST_PAGE_ORDER
    assert page.total == 4
    assert [(r.text, r.binding.state) for r in nginx.references] == [
        ("nginx:1", "unbound"),
        ("nginx:latest", "unbound"),
    ]
    assert [(r.text, r.binding.state, r.binding.set_name) for r in ghcr.references] == [
        ("ghcr.io/o/app:2", "bound", "anywhere")
    ]
    assert (nginx.in_use, [c.name for c in nginx.containers]) == (1, ["web"])
    assert (nginx.size, nginx.dangling, ghcr.dangling) == (300, False, False)
    assert page.page.next_cursor is None
    assert (page.environment_name, page.is_local, page.can_remove) == ("environment-a", False, True)


async def test_a_digest_only_image_is_dangling_and_shows_its_digest_reference(
    stocked: ImageRig,
) -> None:
    page = await stocked.images.queries.page(ADMIN, ENV_A, ImageFilters(dangling="yes"))

    [dangling, digest_only] = page.page.items
    assert (dangling.id, dangling.references) == (image_id(DANGLING), ())
    assert [(r.kind, r.key) for r in digest_only.references] == [
        ("digest", f"ghcr.io/o/old@{digest(4)}")
    ]
    assert digest_only.dangling is True


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        pytest.param(ImageFilters(q="NGINX"), [NGINX], id="text-is-case-folded"),
        pytest.param(ImageFilters(q="o/app"), [GHCR], id="text-matches-inside-a-reference"),
        pytest.param(ImageFilters(q=image_id(DANGLING)), [DANGLING], id="text-matches-an-id"),
        pytest.param(ImageFilters(q="nothing-like-this"), [], id="text-matches-nothing"),
        pytest.param(ImageFilters(dangling="yes"), [DANGLING, DIGEST_ONLY], id="dangling"),
        pytest.param(ImageFilters(dangling="no"), [GHCR, NGINX], id="not-dangling"),
        pytest.param(ImageFilters(usage="used"), [NGINX], id="used"),
        pytest.param(ImageFilters(usage="unused"), [GHCR, DANGLING, DIGEST_ONLY], id="unused"),
        pytest.param(ImageFilters(binding="unbound"), [NGINX, DIGEST_ONLY], id="unbound"),
        pytest.param(
            ImageFilters(binding="anonymous"), [NGINX, DIGEST_ONLY], id="anonymous-reads-as-unbound"
        ),
        pytest.param(ImageFilters(binding="bound"), [GHCR], id="bound"),
        pytest.param(ImageFilters(binding="credential_missing"), [], id="credential-missing"),
        pytest.param(
            ImageFilters(q="  ", dangling=" "), [GHCR, NGINX, DANGLING, DIGEST_ONLY], id="blank"
        ),
    ],
)
async def test_each_filter_narrows_the_list_the_same_way_for_the_page_and_the_total(
    stocked: ImageRig, filters: ImageFilters, expected: list[int]
) -> None:
    anywhere = await register(stocked, "anywhere", address="ghcr.io")
    await bind_image(stocked.db, anywhere, ENV_A, "ghcr.io/o/app:2")

    page = await stocked.images.queries.page(ADMIN, ENV_A, filters)

    assert [row.id for row in page.page.items] == [image_id(n) for n in expected]
    assert page.total == len(expected)


async def test_a_binding_filter_matches_credential_missing_references(stocked: ImageRig) -> None:
    gone = await register(stocked, "gone", address="ghcr.io")
    await bind_image(stocked.db, gone, ENV_A, "ghcr.io/o/app:2")
    await mark_missing(stocked.db, gone)

    page = await stocked.images.queries.page(
        ADMIN, ENV_A, ImageFilters(binding="credential_missing")
    )

    [row] = page.page.items
    assert row.id == image_id(GHCR)
    assert row.references[0].binding.missing_reason == "deleted"


async def test_the_binding_filter_looks_only_at_the_references_the_row_shows(
    image_rig: ImageRig,
) -> None:
    anywhere = await register(image_rig, "anywhere", address="docker.io")
    image_rig.host.add_image("nginx:1", full_id(NGINX), digests=(f"nginx@{digest(NGINX)}",))
    await bind_image(image_rig.db, anywhere, ENV_A, f"docker.io/library/nginx@{digest(NGINX)}")

    bound = await image_rig.images.queries.page(ADMIN, ENV_A, ImageFilters(binding="bound"))
    unbound = await image_rig.images.queries.page(ADMIN, ENV_A, ImageFilters(binding="unbound"))

    assert [row.id for row in bound.page.items] == []
    assert [row.id for row in unbound.page.items] == [image_id(NGINX)]


@pytest.mark.parametrize(
    ("filters", "field"),
    [
        pytest.param(ImageFilters(q="q" * 129), "q", id="long-text"),
        pytest.param(ImageFilters(dangling="maybe"), "dangling", id="dangling"),
        pytest.param(ImageFilters(usage="always"), "usage", id="usage"),
        pytest.param(ImageFilters(binding="tied"), "binding", id="binding"),
    ],
)
async def test_an_invalid_filter_is_refused_on_its_own_field_before_the_host_is_read(
    stocked: ImageRig, filters: ImageFilters, field: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await stocked.images.queries.page(ADMIN, ENV_A, filters)

    assert set(refused.value.fields) == {field}
    assert stocked.host.requests == []


async def test_a_page_ends_with_a_cursor_that_continues_the_list_without_repeats(
    stocked: ImageRig,
) -> None:
    queries = stocked.images.queries

    first = await queries.page(ADMIN, ENV_A, limit=3)
    second = await queries.page(ADMIN, ENV_A, limit=3, cursor=first.page.next_cursor)

    assert [row.id for row in first.page.items] == FIRST_PAGE_ORDER[:3]
    assert first.page.next_cursor is not None
    assert [row.id for row in second.page.items] == FIRST_PAGE_ORDER[3:]
    assert second.page.next_cursor is None
    assert first.total == second.total == 4


@pytest.mark.parametrize(
    ("cursor", "limit", "field"),
    [
        pytest.param("not-a-cursor", 25, "cursor", id="cursor"),
        pytest.param(None, 0, "limit", id="zero-limit"),
        pytest.param(None, 501, "limit", id="limit-over-the-cap"),
    ],
)
async def test_a_bad_cursor_or_page_size_is_a_validation_error(
    stocked: ImageRig, cursor: str | None, limit: int, field: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await stocked.images.queries.page(ADMIN, ENV_A, limit=limit, cursor=cursor)

    assert set(refused.value.fields) == {field}


async def test_bindings_for_references_on_no_image_are_listed_on_the_first_page_for_administrators(
    stocked: ImageRig,
) -> None:
    anywhere = await register(stocked, "anywhere")
    await bind_image(stocked.db, anywhere, ENV_A, f"{ADDRESS}/removed:1")
    queries = stocked.images.queries

    first = await queries.page(ADMIN, ENV_A, limit=1)
    later = await queries.page(ADMIN, ENV_A, limit=1, cursor=first.page.next_cursor)
    standard = await queries.page(STANDARD, ENV_A)

    assert first.orphans is not None
    assert [b.reference for b in first.orphans.bindings] == [f"{ADDRESS}/removed:1"]
    assert later.orphans is None
    assert standard.orphans is None


async def test_a_standard_user_sees_every_image_but_no_removal_and_names_only_visible_containers(
    stocked: ImageRig,
) -> None:
    stocked.host.add(container_id(2), "private", image="nginx:1", image_id=full_id(NGINX))
    await put_control(stocked.access, container_key(1, ENV_A), public=True)
    await put_control(stocked.access, container_key(2, ENV_A), administrators_only=True)

    page = await stocked.images.queries.page(STANDARD, ENV_A)

    nginx = next(row for row in page.page.items if row.id == image_id(NGINX))
    assert [row.id for row in page.page.items] == FIRST_PAGE_ORDER
    assert (page.can_remove, page.can_create) == (False, True)
    assert (nginx.in_use, [c.name for c in nginx.containers], nginx.containers_hidden) == (
        2,
        ["web"],
        1,
    )


async def test_a_standard_user_cannot_create_on_the_local_socket(stocked: ImageRig) -> None:
    page = await stocked.images.queries.page(STANDARD, ENV_B)

    assert (page.is_local, page.can_create) == (True, False)


async def test_a_standard_user_without_a_grant_gets_the_same_not_found_as_a_missing_environment(
    stocked: ImageRig,
) -> None:
    await EnvironmentAccessRepository(stocked.db).remove_user("environment", ENV_A, STANDARD_ID)
    queries = stocked.images.queries

    with pytest.raises(ResourceNotFound) as hidden:
        await queries.page(STANDARD, ENV_A)
    with pytest.raises(ResourceNotFound) as missing:
        await queries.page(STANDARD, UNKNOWN_ENVIRONMENT)
    with pytest.raises(ResourceNotFound):
        await queries.detail(STANDARD, ENV_A, image_id(NGINX))

    assert str(hidden.value) == str(missing.value)
    assert stocked.host.requests == []


async def test_an_unreachable_host_is_an_environment_error_not_a_partial_list(
    stocked: ImageRig,
) -> None:
    stocked.host.fail("image_list", 500)

    with pytest.raises(EnvironmentUnavailable):
        await stocked.images.queries.page(ADMIN, ENV_A)


async def test_an_image_list_over_the_read_limit_is_refused_not_shown_in_part(
    stocked: ImageRig,
) -> None:
    stocked.host.route("GET", "/images/json", Reply(200, b"[]", {"Content-Length": str(20 * MIB)}))

    with pytest.raises(ImageListTooLarge) as refused:
        await stocked.images.queries.page(ADMIN, ENV_A)

    assert "16 MiB" in str(refused.value)


def configure(rig: ImageRig) -> None:
    rig.host.add_image(
        "app:1",
        full_id(NGINX),
        config={
            "Env": [f"DB_PASSWORD={SECRET_ENV}", "MODE=prod"],
            "Cmd": ["serve", "--port", "80"],
            "Entrypoint": ["/entry"],
            "Labels": {"maintainer": "ops", "registry.api_key": SECRET_LABEL},
            "ExposedPorts": {"80/tcp": {}, "443/tcp": {}},
            "WorkingDir": "/srv",
            "User": "app",
        },
        history=[
            {
                "Id": full_id(NGINX),
                "Created": 1_700_000_000,
                "CreatedBy": f"{BUILD_COMMAND}\x1b[31m{'x' * 600}",
                "Tags": ["app:1"],
                "Size": 12,
            },
            {"Id": "<missing>", "Created": 0, "CreatedBy": "ADD rootfs", "Tags": [], "Size": 1},
        ],
    )


async def test_an_administrator_sees_the_command_and_variables_with_secret_names_redacted(
    image_rig: ImageRig,
) -> None:
    configure(image_rig)

    detail = await image_rig.images.queries.detail(ADMIN, ENV_A, image_id(NGINX))

    assert (detail.command, detail.entrypoint) == ("serve --port 80", "/entry")
    assert detail.variables is not None
    assert [(v.key, v.value, v.redacted) for v in detail.variables] == [
        ("DB_PASSWORD", "[REDACTED]", True),
        ("MODE", "prod", False),
    ]
    assert {label.key: label.value for label in detail.labels}["maintainer"] == "ops"
    assert detail.exposed_ports == ("443/tcp", "80/tcp")
    assert (detail.working_dir, detail.user, detail.can_remove) == ("/srv", "app", True)


async def test_a_standard_user_sees_no_command_or_variables_and_redacted_label_values(
    image_rig: ImageRig,
) -> None:
    configure(image_rig)

    detail = await image_rig.images.queries.detail(STANDARD, ENV_A, image_id(NGINX))

    labels = {label.key: (label.value, label.redacted) for label in detail.labels}
    assert (detail.command, detail.entrypoint, detail.variables) == (None, None, None)
    assert labels["registry.api_key"] == ("[REDACTED]", True)
    assert labels["maintainer"] == ("ops", False)
    assert (detail.can_remove, detail.can_create) == (False, True)
    assert SECRET_LABEL not in repr(detail)
    assert SECRET_ENV not in repr(detail)


async def test_history_shows_build_commands_to_an_administrator_stripped_and_cut_to_512(
    image_rig: ImageRig,
) -> None:
    configure(image_rig)

    layers = await image_rig.images.queries.history(ADMIN, ENV_A, image_id(NGINX))

    assert layers[0].created_by is not None
    assert layers[0].created_by.startswith(BUILD_COMMAND)
    assert "\x1b" not in layers[0].created_by
    assert len(layers[0].created_by) <= 512
    assert layers[0].layer == image_id(NGINX)[:12]
    assert layers[1].layer is None
    assert layers[0].tags == ("app:1",)


async def test_history_withholds_build_commands_from_a_standard_user(image_rig: ImageRig) -> None:
    configure(image_rig)

    layers = await image_rig.images.queries.history(STANDARD, ENV_A, image_id(NGINX))

    assert [layer.created_by for layer in layers] == [None, None]
    assert BUILD_COMMAND not in repr(layers)


@pytest.mark.parametrize(
    "image",
    [
        pytest.param(f"sha256:{image_id(NGINX)}", id="with-the-prefix"),
        pytest.param(image_id(0xAB).upper(), id="upper-case"),
        pytest.param(image_id(NGINX)[:63], id="too-short"),
        pytest.param(image_id(NGINX)[:12], id="a-prefix-of-the-id"),
        pytest.param("nginx:1", id="a-name"),
        pytest.param("../etc", id="traversal"),
    ],
)
@pytest.mark.parametrize("viewer", [ADMIN, STANDARD], ids=["administrator", "standard-user"])
async def test_anything_but_a_bare_64_hex_id_is_not_found_before_the_engine_is_asked(
    stocked: ImageRig, viewer: object, image: str
) -> None:
    queries = stocked.images.queries

    with pytest.raises(ResourceNotFound):
        await queries.detail(viewer, ENV_A, image)  # type: ignore[arg-type]
    with pytest.raises(ResourceNotFound):
        await queries.history(viewer, ENV_A, image)  # type: ignore[arg-type]

    assert stocked.host.requests == []


async def test_an_id_the_daemon_answers_with_a_different_image_is_not_found(
    stocked: ImageRig,
) -> None:
    stocked.host.image_store[full_id(9)] = stocked.host.image_store[full_id(NGINX)]

    with pytest.raises(ResourceNotFound):
        await stocked.images.queries.detail(ADMIN, ENV_A, image_id(9))


async def test_an_image_the_daemon_does_not_have_is_not_found(stocked: ImageRig) -> None:
    with pytest.raises(ResourceNotFound):
        await stocked.images.queries.detail(ADMIN, ENV_A, image_id(77))


async def test_an_inspect_reply_over_the_read_limit_is_too_large_to_show(stocked: ImageRig) -> None:
    stocked.host.route(
        "GET",
        f"/images/sha256:{image_id(NGINX)}/json",
        Reply(200, b"{}", {"Content-Length": str(5 * MIB)}),
    )

    with pytest.raises(ImageTooLarge):
        await stocked.images.queries.detail(ADMIN, ENV_A, image_id(NGINX))


async def test_the_remove_impact_names_the_image_every_reference_and_every_container(
    stocked: ImageRig,
) -> None:
    impact = await stocked.images.queries.remove_impact(ADMIN, ENV_A, image_id(NGINX))

    assert impact.references == ("nginx:1", "nginx:latest")
    assert impact.references_total == 2
    assert impact.references_digest == references_digest(KEYS_BY_IMAGE[NGINX])
    assert (impact.in_use, [c.name for c in impact.containers]) == (1, ["web"])
    assert (impact.environment_name, impact.size) == ("environment-a", 300)


async def test_the_untag_impact_says_when_the_untag_removes_the_image(stocked: ImageRig) -> None:
    queries = stocked.images.queries

    not_last = await queries.untag_impact(ADMIN, ENV_A, image_id(NGINX), "nginx:1")
    last = await queries.untag_impact(ADMIN, ENV_A, image_id(GHCR), "ghcr.io/o/app:2")

    assert (not_last.is_last, not_last.deletes_image) == (False, False)
    assert (last.is_last, last.deletes_image, last.in_use) == (True, True, 0)
    assert not_last.reference_key == "docker.io/library/nginx:1"


async def test_the_untag_impact_of_a_used_images_last_tag_does_not_delete_it(
    stocked: ImageRig,
) -> None:
    stocked.host.add_image("nginx:latest", full_id(GHCR))
    await stocked.images.actions.untag(
        ADMIN, ENV_A, image_id(NGINX), "nginx:1", expected_last=True, expected_in_use=1, force=True
    )

    impact = await stocked.images.queries.untag_impact(ADMIN, ENV_A, image_id(GHCR), "nginx:latest")

    assert (impact.is_last, impact.deletes_image) == (False, False)


async def test_the_untag_impact_of_a_reference_the_image_does_not_have_is_not_found(
    stocked: ImageRig,
) -> None:
    with pytest.raises(ResourceNotFound):
        await stocked.images.queries.untag_impact(ADMIN, ENV_A, image_id(GHCR), "nginx:1")


async def test_the_tag_impact_names_the_image_that_would_lose_the_reference(
    stocked: ImageRig,
) -> None:
    queries = stocked.images.queries

    taken = await queries.tag_impact(ADMIN, ENV_A, image_id(GHCR), "nginx:1")
    free = await queries.tag_impact(ADMIN, ENV_A, image_id(GHCR), "fresh:1")
    same = await queries.tag_impact(ADMIN, ENV_A, image_id(GHCR), "ghcr.io/o/app:2")

    assert taken.replaced is not None
    assert (taken.replaced.id, taken.replaced.references) == (
        image_id(NGINX),
        ("nginx:1", "nginx:latest"),
    )
    assert taken.expected_replaced_id == image_id(NGINX)
    assert (free.replaced, free.expected_replaced_id) == (None, "none")
    assert same.unchanged is True


async def test_a_tag_target_that_is_a_digest_or_malformed_is_refused_on_the_reference_field(
    stocked: ImageRig,
) -> None:
    for target in (f"app@{digest(1)}", "Not A Reference"):
        with pytest.raises(ValidationFailed) as refused:
            await stocked.images.queries.tag_impact(ADMIN, ENV_A, image_id(GHCR), target)
        assert set(refused.value.fields) == {"reference"}


@pytest.mark.parametrize("call", ["remove_impact", "untag_impact", "tag_impact", "prune_plan"])
async def test_a_standard_user_may_not_ask_what_a_removal_tag_or_prune_would_do(
    stocked: ImageRig, call: str
) -> None:
    queries = stocked.images.queries
    attempts = {
        "remove_impact": queries.remove_impact(STANDARD, ENV_A, image_id(NGINX)),
        "untag_impact": queries.untag_impact(STANDARD, ENV_A, image_id(NGINX), "nginx:1"),
        "tag_impact": queries.tag_impact(STANDARD, ENV_A, image_id(NGINX), "x:1"),
        "prune_plan": queries.prune_plan(STANDARD, ENV_A, "dangling"),
    }

    with pytest.raises(PermissionDenied):
        await attempts.pop(call)
    for unused in attempts.values():
        unused.close()


async def test_the_dangling_prune_plan_lists_untagged_unused_images_largest_first(
    stocked: ImageRig,
) -> None:
    plan = await stocked.images.queries.prune_plan(ADMIN, ENV_A, "dangling")

    assert [c.id for c in plan.candidates] == [image_id(DIGEST_ONLY), image_id(DANGLING)]
    assert (plan.total, plan.truncated, plan.reclaimable) == (2, False, 120)
    assert plan.candidates[0].references == (f"ghcr.io/o/old@{digest(4)}",)
    assert plan.candidates[1].references_digest == references_digest([])


async def test_the_unused_prune_plan_adds_tagged_images_no_container_uses(
    stocked: ImageRig,
) -> None:
    plan = await stocked.images.queries.prune_plan(ADMIN, ENV_A, "unused")

    assert [c.id for c in plan.candidates] == [
        image_id(GHCR),
        image_id(DIGEST_ONLY),
        image_id(DANGLING),
    ]


async def test_a_prune_plan_lists_at_most_100_images_and_says_how_many_matched(
    image_rig: ImageRig,
) -> None:
    for number in range(1, 131):
        image_rig.host.add_image(None, full_id(number), size=number)

    plan: PrunePlan = await image_rig.images.queries.prune_plan(ADMIN, ENV_A, "dangling")

    assert (len(plan.candidates), plan.total, plan.truncated) == (100, 130, True)
    assert plan.candidates[0].size == 130


async def test_an_unknown_prune_mode_is_refused(stocked: ImageRig) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await stocked.images.queries.prune_plan(ADMIN, ENV_A, "everything")  # type: ignore[arg-type]

    assert set(refused.value.fields) == {"mode"}


async def test_an_engine_failure_is_reported_without_the_engines_text(stocked: ImageRig) -> None:
    stocked.host.fail("image_history", 500)

    with pytest.raises(EnvironmentUnavailable) as failed:
        await stocked.images.queries.history(ADMIN, ENV_A, image_id(NGINX))

    assert ENGINE_TEXT not in str(failed.value) + repr(failed.value)


async def test_the_pull_rights_check_is_not_found_for_a_hidden_environment_and_forbidden_locally(
    stocked: ImageRig,
) -> None:
    queries = stocked.images.queries
    await queries.require_pull_rights(STANDARD, ENV_A)

    with pytest.raises(PermissionDenied):
        await queries.require_pull_rights(STANDARD, ENV_B)
    with pytest.raises(ResourceNotFound):
        await queries.require_pull_rights(STANDARD, UNKNOWN_ENVIRONMENT)
    await queries.require_visible(STANDARD, ENV_B)


def test_the_list_too_large_errors_are_object_too_large_so_every_layer_settles_them_alike() -> None:
    from app.services.containers import ContainerListTooLarge
    from app.services.object_inspector import ObjectTooLarge

    assert issubclass(ImageListTooLarge, ObjectTooLarge)
    assert issubclass(ContainerListTooLarge, ObjectTooLarge)
    assert not issubclass(ImageListTooLarge, PermissionDenied)
