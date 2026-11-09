"""The host-wide image total: the environment gate, the figure and what a failing host gives."""

import pytest

from app.auth.errors import ResourceNotFound
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.services.image_listing import DiskUsageTooLarge
from app.services.object_inspector import EnvironmentUnavailable
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.auth_support import STANDARD_ID
from tests.docker_engine.fake_engine import Reply, error_reply, json_reply
from tests.services.conftest import ImageRig

DF = "/system/df"
LAYERS_SIZE = 2_684_354_560
OVER_THE_CAP = 17 * 1024 * 1024
UNKNOWN_ENVIRONMENT = 9999


async def test_every_role_with_access_reads_the_same_host_wide_figure_from_one_image_only_call(
    image_rig: ImageRig,
) -> None:
    image_rig.host.add_image("nginx:1", size=10)
    image_rig.host.route("GET", DF, json_reply({"LayersSize": LAYERS_SIZE}))

    admin = await image_rig.images.queries.disk_usage(ADMIN, ENV_A)
    standard = await image_rig.images.queries.disk_usage(STANDARD, ENV_A)

    assert admin == standard == LAYERS_SIZE
    assert {call.query["type"] for call in image_rig.host.calls("GET", DF)} == {"image"}


async def test_a_caller_without_the_environment_is_refused_before_any_engine_call(
    image_rig: ImageRig,
) -> None:
    await EnvironmentAccessRepository(image_rig.db).remove_user("environment", ENV_A, STANDARD_ID)

    with pytest.raises(ResourceNotFound):
        await image_rig.images.queries.disk_usage(STANDARD, ENV_A)
    with pytest.raises(ResourceNotFound):
        await image_rig.images.queries.disk_usage(ADMIN, UNKNOWN_ENVIRONMENT)

    assert image_rig.host.calls("GET", DF) == []


@pytest.mark.parametrize(
    "behaviour",
    [error_reply(500), json_reply({}), json_reply({"LayersSize": -4})],
    ids=["engine error", "no figure", "negative figure"],
)
async def test_a_host_that_cannot_give_the_figure_raises_the_unavailable_product_error(
    image_rig: ImageRig, behaviour: Reply
) -> None:
    image_rig.host.route("GET", DF, behaviour)

    with pytest.raises(EnvironmentUnavailable) as unavailable:
        await image_rig.images.queries.disk_usage(ADMIN, ENV_A)

    assert unavailable.value.kind == "engine"


async def test_a_disk_usage_reply_over_the_cap_raises_a_product_error_that_names_no_engine_text(
    image_rig: ImageRig,
) -> None:
    image_rig.host.route("GET", DF, Reply(200, b"{}", {"Content-Length": str(OVER_THE_CAP)}))

    with pytest.raises(DiskUsageTooLarge) as too_large:
        await image_rig.images.queries.disk_usage(ADMIN, ENV_A)

    assert "engine" not in str(too_large.value).lower()
