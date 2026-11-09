"""Sorting the object lists: each sortable column both ways, paging, visibility and refusals."""

import pytest

from app.auth.principal import Principal
from app.db.repositories.pagination import decode_cursor
from app.services.access import ResourceKey
from app.services.assembly import NetworkServices, VolumeServices
from tests.access_support import (
    ADMIN,
    ENV_A,
    STANDARD,
    control,
    stack_key,
    volume_key,
)
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.swarm_engine import plain_spec
from tests.image_support import full_id, image_id
from tests.list_sort_support import (
    CURSOR_FIELD,
    CURSOR_MESSAGE,
    DIRECTION_FIELD,
    SORT_FIELD,
    Fetch,
    Fetched,
    paged,
    refused_fields,
    whole,
)
from tests.object_support import STACK_LABEL
from tests.services.conftest import ContainerRig, ImageRig, SwarmRig
from tests.stack_support import Rig, seed_stack_row
from tests.swarm_support import NAMESPACE_LABEL

SMALL_PAGE = 2
BOTH_WAYS = ["asc", "desc"]
STACK_TEXT = "services:\n  web:\n    image: nginx:1\n"


def spread_id(number: int) -> str:
    return image_id(number)


def container_ref(number: int) -> ResourceKey:
    return ResourceKey(ENV_A, "container", spread_id(number))


def container_fetch(rig: ContainerRig, actor: Principal = ADMIN) -> Fetch:
    async def fetch(
        sort: str | None, direction: str | None, size: int, cursor: str | None
    ) -> Fetched:
        page = await rig.services.queries.page(
            actor, ENV_A, limit=size, cursor=cursor, sort=sort, direction=direction
        )
        return Fetched([r.name for r in page.page.items], page.total, page.page.next_cursor)

    return fetch


def volume_fetch(vols: VolumeServices, actor: Principal = ADMIN) -> Fetch:
    async def fetch(
        sort: str | None, direction: str | None, size: int, cursor: str | None
    ) -> Fetched:
        page = await vols.queries.page(
            actor, ENV_A, limit=size, cursor=cursor, sort=sort, direction=direction
        )
        return Fetched([r.name for r in page.page.items], page.total, page.page.next_cursor)

    return fetch


def network_fetch(nets: NetworkServices, actor: Principal = ADMIN) -> Fetch:
    async def fetch(
        sort: str | None, direction: str | None, size: int, cursor: str | None
    ) -> Fetched:
        page = await nets.queries.page(
            actor, ENV_A, limit=size, cursor=cursor, sort=sort, direction=direction
        )
        return Fetched([r.name for r in page.page.items], page.total, page.page.next_cursor)

    return fetch


def image_fetch(rig: ImageRig) -> Fetch:
    async def fetch(
        sort: str | None, direction: str | None, size: int, cursor: str | None
    ) -> Fetched:
        page = await rig.images.queries.page(
            ADMIN, ENV_A, limit=size, cursor=cursor, sort=sort, direction=direction
        )
        return Fetched([r.id for r in page.page.items], page.total, page.page.next_cursor)

    return fetch


def service_fetch(rig: SwarmRig, actor: Principal = ADMIN) -> Fetch:
    async def fetch(
        sort: str | None, direction: str | None, size: int, cursor: str | None
    ) -> Fetched:
        page = await rig.sw.services.page(
            actor, ENV_A, limit=size, cursor=cursor, sort=sort, direction=direction
        )
        return Fetched([r.name for r in page.page.items], page.total, page.page.next_cursor)

    return fetch


def object_fetch(rig: SwarmRig, kind: str, actor: Principal = ADMIN) -> Fetch:
    async def fetch(
        sort: str | None, direction: str | None, size: int, cursor: str | None
    ) -> Fetched:
        queries = rig.sw.configs if kind == "configs" else rig.sw.secrets
        page = await queries.page(
            actor, ENV_A, limit=size, cursor=cursor, sort=sort, direction=direction
        )
        return Fetched([r.name for r in page.page.items], page.total, page.page.next_cursor)

    return fetch


def stack_fetch(rig: Rig, actor: Principal = ADMIN) -> Fetch:
    async def fetch(
        sort: str | None, direction: str | None, size: int, cursor: str | None
    ) -> Fetched:
        page = await rig.fresh().queries.page(
            actor,
            ENV_A,
            limit=size,
            cursor=cursor,
            sort=sort,
            direction=direction,
            include_unmanaged=False,
        )
        return Fetched([r.name for r in page.items], len(page.items), page.next_cursor)

    return fetch


async def assert_every_page_continues_the_whole_list(
    fetch: Fetch, sort: str | None, direction: str | None, expected_names: set[str]
) -> list[str]:
    pages = await paged(fetch, sort, direction, SMALL_PAGE)
    everything = await whole(fetch, sort, direction)
    assert pages == everything.names
    assert set(pages) == expected_names and len(pages) == len(expected_names)
    assert everything.total == len(expected_names)
    return pages


async def assert_sort_input_is_refused(fetch: Fetch, sort: str) -> None:
    other = "asc"
    minted_named = (await fetch(sort, other, 1, None)).next_cursor
    minted_flipped = (await fetch(sort, "desc", 1, None)).next_cursor
    minted_unsorted = (await fetch(None, None, 1, None)).next_cursor
    assert None not in (minted_named, minted_flipped, minted_unsorted)

    assert await refused_fields(fetch("bogus", None, 1, None)) == {
        SORT_FIELD: "Sort by one of the listed columns."
    }
    assert await refused_fields(fetch(None, "sideways", 1, None)) == {
        DIRECTION_FIELD: "Choose ascending or descending."
    }
    assert await refused_fields(fetch(sort, "desc", 1, minted_named)) == {
        CURSOR_FIELD: CURSOR_MESSAGE
    }
    assert await refused_fields(fetch(sort, "asc", 1, minted_flipped)) == {
        CURSOR_FIELD: CURSOR_MESSAGE
    }
    assert await refused_fields(fetch(sort, "asc", 1, minted_unsorted)) == {
        CURSOR_FIELD: CURSOR_MESSAGE
    }
    assert await refused_fields(fetch(None, None, 1, minted_named)) == {
        CURSOR_FIELD: CURSOR_MESSAGE
    }


CONTAINER_NAMES = {"web", "Api", "db", "cache", "queue", "worker", "batch"}
CONTAINER_KEYS = ["name", "state", "image", "stack", "created"]


@pytest.fixture
def stocked_containers(host: ContainerEngine) -> None:
    host.add(spread_id(1), "web", state="running", image="nginx:1", labels={STACK_LABEL: "shop"})
    host.add(spread_id(2), "Api", state="exited", image="redis:7")
    host.add(spread_id(3), "db", state="running", image="postgres:16", labels={STACK_LABEL: "bill"})
    host.add(spread_id(4), "cache", state="paused", image="redis:7")
    host.add(spread_id(5), "queue", state="created", image="rabbit:3")
    host.add(
        spread_id(6), "worker", state="restarting", image="app:2", labels={STACK_LABEL: "shop"}
    )
    host.add(spread_id(7), "batch", state="dead", image="alpine:3")


@pytest.mark.parametrize("direction", BOTH_WAYS)
@pytest.mark.parametrize("sort", CONTAINER_KEYS)
async def test_containers_page_every_sortable_column_both_ways_without_repeat_or_skip(
    rig: ContainerRig, stocked_containers: None, sort: str, direction: str
) -> None:
    await assert_every_page_continues_the_whole_list(
        container_fetch(rig), sort, direction, CONTAINER_NAMES
    )


@pytest.mark.parametrize(
    ("sort", "direction", "expected"),
    [
        ("name", "asc", ["Api", "batch", "cache", "db", "queue", "web", "worker"]),
        ("state", "asc", ["db", "web", "worker", "cache", "queue", "Api", "batch"]),
        ("state", "desc", ["batch", "Api", "queue", "cache", "worker", "web", "db"]),
        ("stack", "asc", ["Api", "batch", "cache", "queue", "db", "web", "worker"]),
        ("image", "asc", ["batch", "worker", "web", "db", "queue", "Api", "cache"]),
        ("created", "asc", ["Api", "batch", "cache", "db", "queue", "web", "worker"]),
    ],
)
async def test_containers_order_by_name_state_rank_stack_image_and_the_tie_break(
    rig: ContainerRig, stocked_containers: None, sort: str, direction: str, expected: list[str]
) -> None:
    page = await whole(container_fetch(rig), sort, direction)

    assert page.names == expected


async def test_a_standard_users_sorted_container_list_holds_exactly_the_rows_they_may_see(
    rig: ContainerRig, stocked_containers: None
) -> None:
    await control(rig.access, container_ref(1), public=True)
    await control(rig.access, container_ref(4), public=True)
    await control(rig.access, container_ref(6), public=True)
    fetch = container_fetch(rig, STANDARD)

    unsorted = await whole(fetch, None, None)
    by_state = await assert_every_page_continues_the_whole_list(
        fetch, "state", "desc", {"web", "cache", "worker"}
    )

    assert set(unsorted.names) == set(by_state)
    assert by_state == ["cache", "worker", "web"] and unsorted.names != by_state


async def test_container_sort_input_and_cursors_of_another_sort_are_refused(
    rig: ContainerRig, stocked_containers: None
) -> None:
    await assert_sort_input_is_refused(container_fetch(rig), "state")


async def test_an_unsorted_container_cursor_keeps_the_released_form_and_still_pages(
    rig: ContainerRig, stocked_containers: None
) -> None:
    fetch = container_fetch(rig)
    first = await fetch(None, None, SMALL_PAGE, None)

    position = decode_cursor(first.next_cursor or "")
    rest = await fetch(None, None, SMALL_PAGE, first.next_cursor)

    assert position.sort_value == first.names[-1].casefold()
    assert first.names + rest.names == (await whole(fetch, None, None)).names[: 2 * SMALL_PAGE]


VOLUME_NAMES = {"data", "Alpha", "nfs-vol", "logs", "cache", "zed"}
VOLUME_KEYS = ["name", "driver", "created", "stack"]


@pytest.fixture
def stocked_volumes(host: ContainerEngine) -> None:
    host.add_volume("data", labels={STACK_LABEL: "shop"}, created="2024-01-03T00:00:00Z")
    host.add_volume("Alpha", created="2024-01-01T00:00:00Z")
    host.add_volume("nfs-vol", driver="nfs", options={"o": "addr=1"}, created="")
    host.add_volume("logs", labels={STACK_LABEL: "bill"}, created="2024-01-02T00:00:00Z")
    host.add_volume("cache", driver="rexray", created="")
    host.add_volume("zed", labels={STACK_LABEL: "shop"}, created="2024-01-04T00:00:00Z")


@pytest.mark.parametrize("direction", BOTH_WAYS)
@pytest.mark.parametrize("sort", VOLUME_KEYS)
async def test_volumes_page_every_sortable_column_both_ways_without_repeat_or_skip(
    vols: VolumeServices, stocked_volumes: None, sort: str, direction: str
) -> None:
    await assert_every_page_continues_the_whole_list(
        volume_fetch(vols), sort, direction, VOLUME_NAMES
    )


@pytest.mark.parametrize(
    ("sort", "direction", "expected"),
    [
        ("name", "asc", ["Alpha", "cache", "data", "logs", "nfs-vol", "zed"]),
        ("driver", "asc", ["Alpha", "data", "logs", "zed", "nfs-vol", "cache"]),
        ("created", "asc", ["cache", "nfs-vol", "Alpha", "logs", "data", "zed"]),
        ("created", "desc", ["zed", "data", "logs", "Alpha", "nfs-vol", "cache"]),
        ("stack", "asc", ["Alpha", "cache", "nfs-vol", "logs", "data", "zed"]),
    ],
)
async def test_volumes_order_by_name_driver_creation_time_with_none_lowest_and_stack(
    vols: VolumeServices, stocked_volumes: None, sort: str, direction: str, expected: list[str]
) -> None:
    page = await whole(volume_fetch(vols), sort, direction)

    assert page.names == expected


async def test_a_standard_users_sorted_volume_list_holds_exactly_the_rows_they_may_see(
    rig: ContainerRig, vols: VolumeServices, stocked_volumes: None
) -> None:
    await control(rig.access, volume_key("zed"), public=True)
    await control(rig.access, volume_key("Alpha"), public=True)
    await control(rig.access, stack_key("shop"), public=True)
    fetch = volume_fetch(vols, STANDARD)

    unsorted = await whole(fetch, None, None)
    by_created = await assert_every_page_continues_the_whole_list(
        fetch, "created", "desc", {"zed", "Alpha", "data"}
    )

    assert set(unsorted.names) == set(by_created)
    assert by_created == ["zed", "data", "Alpha"]


async def test_volume_sort_input_and_cursors_of_another_sort_are_refused(
    vols: VolumeServices, stocked_volumes: None
) -> None:
    await assert_sort_input_is_refused(volume_fetch(vols), "created")


NETWORK_NAMES = {"shop_net", "Alpha", "backend", "mesh", "bridge", "host", "none"}
NETWORK_KEYS = ["name", "driver", "scope", "stack"]


@pytest.fixture
def stocked_networks(host: ContainerEngine) -> None:
    host.add_network("shop_net", 11, "bridge", Labels={STACK_LABEL: "shop"})
    host.add_network("Alpha", 12, "overlay", Scope="swarm")
    host.add_network("backend", 13, "bridge", Labels={STACK_LABEL: "bill"})
    host.add_network("mesh", 14, "overlay", Scope="swarm")


@pytest.mark.parametrize("direction", BOTH_WAYS)
@pytest.mark.parametrize("sort", NETWORK_KEYS)
async def test_networks_page_every_sortable_column_both_ways_without_repeat_or_skip(
    nets: NetworkServices, stocked_networks: None, sort: str, direction: str
) -> None:
    await assert_every_page_continues_the_whole_list(
        network_fetch(nets), sort, direction, NETWORK_NAMES
    )


@pytest.mark.parametrize(
    ("sort", "direction", "expected"),
    [
        ("name", "desc", ["shop_net", "none", "mesh", "host", "bridge", "backend", "Alpha"]),
        ("driver", "asc", ["backend", "bridge", "shop_net", "host", "none", "Alpha", "mesh"]),
        ("scope", "asc", ["backend", "bridge", "host", "none", "shop_net", "Alpha", "mesh"]),
        ("stack", "asc", ["Alpha", "bridge", "host", "mesh", "none", "backend", "shop_net"]),
    ],
)
async def test_networks_order_by_name_driver_scope_and_stack(
    nets: NetworkServices, stocked_networks: None, sort: str, direction: str, expected: list[str]
) -> None:
    page = await whole(network_fetch(nets), sort, direction)

    assert page.names == expected


async def test_network_sort_input_and_cursors_of_another_sort_are_refused(
    nets: NetworkServices, stocked_networks: None
) -> None:
    await assert_sort_input_is_refused(network_fetch(nets), "driver")


IMAGE_KEYS = ["image", "size", "created"]
ALPINE, GHCR, NGINX, REDIS, DANGLING_A, DANGLING_B = 4, 2, 1, 5, 3, 6
IMAGE_IDS = {image_id(n) for n in (ALPINE, GHCR, NGINX, REDIS, DANGLING_A, DANGLING_B)}


def ids(*numbers: int) -> list[str]:
    return [image_id(n) for n in numbers]


@pytest.fixture
def stocked_images(image_rig: ImageRig) -> None:
    host = image_rig.host
    host.add_image("nginx:1", full_id(NGINX), size=300, created=100)
    host.add_image("ghcr.io/o/app:2", full_id(GHCR), size=50, created=300)
    host.add_image(None, full_id(DANGLING_A), size=70, created=200)
    host.add_image("alpine:3", full_id(ALPINE), size=5, created=400)
    host.add_image("redis:7", full_id(REDIS), size=120, created=50)
    host.add_image(None, full_id(DANGLING_B), size=70, created=500)


@pytest.mark.parametrize("direction", BOTH_WAYS)
@pytest.mark.parametrize("sort", IMAGE_KEYS)
async def test_images_page_every_sortable_column_both_ways_without_repeat_or_skip(
    image_rig: ImageRig, stocked_images: None, sort: str, direction: str
) -> None:
    await assert_every_page_continues_the_whole_list(
        image_fetch(image_rig), sort, direction, IMAGE_IDS
    )


@pytest.mark.parametrize(
    ("sort", "direction", "expected"),
    [
        (None, None, ids(ALPINE, GHCR, NGINX, REDIS, DANGLING_A, DANGLING_B)),
        ("image", "asc", ids(DANGLING_A, DANGLING_B, ALPINE, GHCR, NGINX, REDIS)),
        ("image", "desc", ids(REDIS, NGINX, GHCR, ALPINE, DANGLING_B, DANGLING_A)),
        ("size", "asc", ids(ALPINE, GHCR, DANGLING_A, DANGLING_B, REDIS, NGINX)),
        ("created", "asc", ids(REDIS, NGINX, DANGLING_A, GHCR, ALPINE, DANGLING_B)),
        ("created", "desc", ids(DANGLING_B, ALPINE, GHCR, DANGLING_A, NGINX, REDIS)),
    ],
)
async def test_images_order_untagged_ones_last_unsorted_and_lowest_when_a_sort_is_named(
    image_rig: ImageRig,
    stocked_images: None,
    sort: str | None,
    direction: str | None,
    expected: list[str],
) -> None:
    page = await whole(image_fetch(image_rig), sort, direction)

    assert page.names == expected


async def test_image_sort_input_and_cursors_of_another_sort_are_refused(
    image_rig: ImageRig, stocked_images: None
) -> None:
    await assert_sort_input_is_refused(image_fetch(image_rig), "size")


async def test_an_unsorted_image_cursor_at_an_untagged_image_keeps_the_released_marker(
    image_rig: ImageRig, stocked_images: None
) -> None:
    fetch = image_fetch(image_rig)
    page = await fetch(None, None, 5, None)

    position = decode_cursor(page.next_cursor or "")
    rest = await fetch(None, None, 5, page.next_cursor)

    assert position.sort_value == "~"
    assert rest.names == ids(DANGLING_B)


SERVICE_NAMES = {"web", "agent", "db", "api"}
SERVICE_KEYS = ["name", "mode", "replicas", "image", "stack", "updated"]


@pytest.fixture
def stocked_services(swarm: SwarmRig) -> None:
    host = swarm.host
    host.add_service(plain_spec("web", replicas=3, Labels={NAMESPACE_LABEL: "shop"}))
    host.add_service(
        {
            "Name": "agent",
            "Mode": {"Global": {}},
            "TaskTemplate": {"ContainerSpec": {"Image": "redis:7"}},
        }
    )
    host.add_service(plain_spec("db", "postgres:16", replicas=1))
    host.add_service(plain_spec("api", "nginx:1", replicas=2, Labels={NAMESPACE_LABEL: "bill"}))


@pytest.mark.parametrize("direction", BOTH_WAYS)
@pytest.mark.parametrize("sort", SERVICE_KEYS)
async def test_swarm_services_page_every_sortable_column_both_ways_without_repeat_or_skip(
    swarm: SwarmRig, stocked_services: None, sort: str, direction: str
) -> None:
    await assert_every_page_continues_the_whole_list(
        service_fetch(swarm), sort, direction, SERVICE_NAMES
    )


@pytest.mark.parametrize(
    ("sort", "direction", "expected"),
    [
        ("name", "desc", ["web", "db", "api", "agent"]),
        ("mode", "asc", ["agent", "api", "db", "web"]),
        ("stack", "asc", ["agent", "db", "api", "web"]),
        ("image", "asc", ["web", "api", "db", "agent"]),
        ("updated", "asc", ["web", "agent", "db", "api"]),
    ],
)
async def test_swarm_services_order_by_name_mode_stack_image_and_update_time(
    swarm: SwarmRig, stocked_services: None, sort: str, direction: str, expected: list[str]
) -> None:
    page = await whole(service_fetch(swarm), sort, direction)

    assert page.names == expected


async def test_a_standard_users_sorted_service_list_holds_exactly_the_rows_they_may_see(
    swarm: SwarmRig, stocked_services: None
) -> None:
    await control(swarm.access, ResourceKey(ENV_A, "stack", "shop"), public=True)
    swarm.fresh()
    fetch = service_fetch(swarm, STANDARD)

    unsorted = await whole(fetch, None, None)
    by_mode = await whole(fetch, "mode", "desc")

    assert unsorted.names == by_mode.names == ["web"]
    assert unsorted.total == by_mode.total == 1


async def test_swarm_service_sort_input_and_cursors_of_another_sort_are_refused(
    swarm: SwarmRig, stocked_services: None
) -> None:
    await assert_sort_input_is_refused(service_fetch(swarm), "replicas")


OBJECT_NAMES = {"web-conf", "Alpha", "db-conf", "zeta"}
OBJECT_KEYS = ["name", "stack", "created"]


@pytest.fixture(params=["configs", "secrets"])
def object_kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def stocked_objects(swarm: SwarmRig, object_kind: str) -> None:
    add = swarm.host.add_config if object_kind == "configs" else swarm.host.add_secret
    add("web-conf", labels={NAMESPACE_LABEL: "shop"})
    add("Alpha")
    add("db-conf", labels={NAMESPACE_LABEL: "bill"})
    add("zeta")


@pytest.mark.parametrize("direction", BOTH_WAYS)
@pytest.mark.parametrize("sort", OBJECT_KEYS)
async def test_swarm_configs_and_secrets_page_every_sortable_column_without_repeat_or_skip(
    swarm: SwarmRig, object_kind: str, stocked_objects: None, sort: str, direction: str
) -> None:
    await assert_every_page_continues_the_whole_list(
        object_fetch(swarm, object_kind), sort, direction, OBJECT_NAMES
    )


@pytest.mark.parametrize(
    ("sort", "direction", "expected"),
    [
        ("name", "asc", ["Alpha", "db-conf", "web-conf", "zeta"]),
        ("stack", "asc", ["Alpha", "zeta", "db-conf", "web-conf"]),
        ("created", "asc", ["web-conf", "Alpha", "db-conf", "zeta"]),
        ("created", "desc", ["zeta", "db-conf", "Alpha", "web-conf"]),
    ],
)
async def test_swarm_configs_and_secrets_order_by_name_stack_and_creation_time(
    swarm: SwarmRig,
    object_kind: str,
    stocked_objects: None,
    sort: str,
    direction: str,
    expected: list[str],
) -> None:
    page = await whole(object_fetch(swarm, object_kind), sort, direction)

    assert page.names == expected


async def test_swarm_object_sort_input_and_cursors_of_another_sort_are_refused(
    swarm: SwarmRig, object_kind: str, stocked_objects: None
) -> None:
    await assert_sort_input_is_refused(object_fetch(swarm, object_kind), "created")


STACK_NAMES = {"alpha", "bravo", "charlie", "delta", "echo"}


async def seed_stacks(rig: Rig) -> None:
    for name in sorted(STACK_NAMES):
        await seed_stack_row(rig, ENV_A, name, STACK_TEXT)


@pytest.mark.parametrize("direction", BOTH_WAYS)
async def test_compose_stacks_page_by_name_both_ways_without_repeat_or_skip(
    stack_rig: Rig, direction: str
) -> None:
    await seed_stacks(stack_rig)

    pages = await assert_every_page_continues_the_whole_list(
        stack_fetch(stack_rig), "name", direction, STACK_NAMES
    )

    assert pages == sorted(STACK_NAMES, reverse=direction == "desc")


async def test_a_standard_users_sorted_stack_list_holds_exactly_the_rows_they_may_see(
    stack_rig: Rig,
) -> None:
    await seed_stacks(stack_rig)
    await control(stack_rig.access, stack_key("bravo"), public=True)
    await control(stack_rig.access, stack_key("delta"), public=True)
    fetch = stack_fetch(stack_rig, STANDARD)

    unsorted = await whole(fetch, None, None)
    descending = await whole(fetch, "name", "desc")

    assert unsorted.names == ["bravo", "delta"] and descending.names == ["delta", "bravo"]


async def test_compose_stack_sort_input_and_cursors_of_another_direction_are_refused(
    stack_rig: Rig,
) -> None:
    await seed_stacks(stack_rig)
    fetch = stack_fetch(stack_rig)
    ascending = (await fetch("name", "asc", 1, None)).next_cursor
    unsorted = (await fetch(None, None, 1, None)).next_cursor

    assert await refused_fields(fetch("bogus", None, 1, None)) == {
        SORT_FIELD: "Sort by one of the listed columns."
    }
    assert await refused_fields(fetch("name", "sideways", 1, None)) == {
        DIRECTION_FIELD: "Choose ascending or descending."
    }
    assert await refused_fields(fetch("name", "desc", 1, ascending)) == {
        CURSOR_FIELD: CURSOR_MESSAGE
    }
    assert await refused_fields(fetch("name", "asc", 1, unsorted)) == {CURSOR_FIELD: CURSOR_MESSAGE}


async def test_an_unsorted_stack_cursor_keeps_the_released_form_and_still_pages(
    stack_rig: Rig,
) -> None:
    await seed_stacks(stack_rig)
    fetch = stack_fetch(stack_rig)
    first = await fetch(None, None, SMALL_PAGE, None)

    position = decode_cursor(first.next_cursor or "")
    rest = await fetch(None, None, SMALL_PAGE, first.next_cursor)

    assert position.sort_value == first.names[-1]
    assert first.names + rest.names == sorted(STACK_NAMES)[: 2 * SMALL_PAGE]


@pytest.fixture
def stocked_namespaces(swarm: SwarmRig) -> None:
    host = swarm.host
    for index in range(3):
        host.add_service(plain_spec(f"alpha_{index}", Labels={NAMESPACE_LABEL: "Alpha"}))
    host.add_service(plain_spec("other_web", Labels={NAMESPACE_LABEL: "Other"}))
    for index in range(2):
        host.add_service(plain_spec(f"shop_{index}", Labels={NAMESPACE_LABEL: "Shop"}))
    host.add_config("zulu-conf", labels={NAMESPACE_LABEL: "Zulu"})


@pytest.mark.parametrize(
    ("sort", "direction", "expected"),
    [
        (None, None, ["Alpha", "Other", "Shop", "Zulu"]),
        ("name", "desc", ["Zulu", "Shop", "Other", "Alpha"]),
        ("services", "asc", ["Zulu", "Other", "Shop", "Alpha"]),
        ("services", "desc", ["Alpha", "Shop", "Other", "Zulu"]),
        ("state", "asc", ["Alpha", "Other", "Shop", "Zulu"]),
        ("last", "desc", ["Zulu", "Shop", "Other", "Alpha"]),
    ],
)
async def test_swarm_stacks_order_their_one_page_by_name_services_state_and_last_operation(
    swarm: SwarmRig,
    stocked_namespaces: None,
    sort: str | None,
    direction: str | None,
    expected: list[str],
) -> None:
    page = await swarm.sw.stacks.page(ADMIN, ENV_A, sort=sort, direction=direction)

    assert [row.name for row in page.rows] == expected and page.total == len(expected)


async def test_swarm_stack_sort_input_is_refused_naming_the_field(
    swarm: SwarmRig, stocked_namespaces: None
) -> None:
    sort = await refused_fields(swarm.sw.stacks.page(ADMIN, ENV_A, sort="bogus"))
    direction = await refused_fields(swarm.sw.stacks.page(ADMIN, ENV_A, direction="up"))

    assert set(sort) == {SORT_FIELD} and set(direction) == {DIRECTION_FIELD}
