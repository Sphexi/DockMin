"""The pieces of a container import plan: mount rows, name suggestions and the digest."""

from app.services.container_import_plan import (
    ContainerImportPlan,
    NetworkPlan,
    mount_entries,
    plan_digest,
    suggest_name,
)

ARCHIVE = "a" * 64


def plan(**changes: object) -> ContainerImportPlan:
    fields: dict[str, object] = {
        "source_name": "web",
        "image": "nginx:1",
        "image_status": "same",
        "archive_digest": None,
        "local_digest": None,
        "name_taken": False,
        "networks": (),
        "volumes": (),
        "skipped_volumes": (),
        "host_paths": (),
        "environment": (),
        "not_exported": (),
        "helper_image": None,
    }
    fields.update(changes)
    return ContainerImportPlan(**fields)  # type: ignore[arg-type]


def test_mount_rows_are_returned_in_row_order_with_their_columns() -> None:
    form = {
        "mount-2-type": "bind",
        "mount-2-source": "/srv",
        "mount-2-target": "/s",
        "mount-0-type": "volume",
        "mount-0-source": "data",
        "mount-0-target": "/d",
        "mount-0-read_only": "on",
        "image": "nginx",
    }

    rows = mount_entries(form)

    assert [(r.index, r.kind, r.source, r.target, r.read_only) for r in rows] == [
        (0, "volume", "data", "/d", True),
        (2, "bind", "/srv", "/s", False),
    ]


def test_a_suggested_name_takes_the_smallest_free_suffix_from_two() -> None:
    assert suggest_name("data", {"data", "data-2", "data-4"}) == "data-3"
    assert suggest_name("data", {"data"}) == "data-2"


def test_the_digest_changes_with_the_archive_and_with_what_the_plan_shows() -> None:
    base = plan()

    assert plan_digest(base, ARCHIVE) == plan_digest(plan(), ARCHIVE)
    assert plan_digest(base, ARCHIVE) != plan_digest(base, "b" * 64)
    assert plan_digest(base, ARCHIVE) != plan_digest(plan(name_taken=True), ARCHIVE)


def net(**changes: object) -> NetworkPlan:
    fields: dict[str, object] = {"name": "n", "driver": "bridge", "action": "create"}
    fields.update(changes)
    return NetworkPlan(**fields)  # type: ignore[arg-type]


def test_the_digest_follows_each_option_label_pool_and_flag_of_a_network() -> None:
    changes: list[dict[str, object]] = [
        {"options": (("com.docker.network.bridge.name", "br0"),)},
        {"labels": (("tier", "db"),)},
        {"pools": (("172.30.0.0/24", "", ""),)},
        {"pools": (("172.30.0.0/24", "172.30.0.1", ""),)},
        {"flags": ("internal",)},
    ]
    digests = {plan_digest(plan(networks=(net(),)), ARCHIVE)}
    digests |= {plan_digest(plan(networks=(net(**change),)), ARCHIVE) for change in changes}

    assert len(digests) == len(changes) + 1
