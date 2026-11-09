"""The pure parts of a stack import plan: host paths, volume matching and the digest."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
import yaml

from app.services.stack_import_archive import VolumeSummary
from app.services.stack_import_plan import (
    Collision,
    HostPath,
    HostPathUnresolved,
    ImportPlan,
    Rewrite,
    VolumePlan,
    candidate_sources,
    compose_host_uses,
    match_volumes,
    mount_target,
    plan_digest,
    replace_host_paths,
    swarm_host_uses,
    volume_key_for_path,
)
from app.services.volume_import_archive import ArchiveSummary
from app.services.volume_manifest import build_manifest

FILE = """\
services:
  web:
    image: nginx:1
    volumes:
      - /srv/site:/usr/share/nginx/html:ro
      - ./nginx.conf:/etc/nginx/nginx.conf
      - data:/data
      - type: bind
        source: /srv/logs
        target: /var/log
        bind:
          create_host_path: false
  worker:
    image: nginx:1
    volumes:
      - /srv/site:/other
volumes:
  data: {}
"""
COMPANIONS = frozenset({"nginx.conf"})


def archived(name: str) -> VolumeSummary:
    manifest = build_manifest(
        exported_at=datetime(2026, 1, 1, tzinfo=UTC),
        instance_version="1",
        name=name,
        driver="local",
        labels={},
        options={},
    )
    summary = ArchiveSummary(manifest, 1, 0, 0, 0, 5, 0, 0)
    return VolumeSummary("volumes/0", manifest, summary)


def plan(**changes: object) -> ImportPlan:
    fields: dict[str, object] = {
        "name": "shop",
        "kind": "compose",
        "source_name": "shop",
        "compose_text": FILE,
        "objects": (),
        "volumes": (),
        "skipped_volumes": (),
        "host_paths": (),
        "images": (),
        "collisions": (),
        "secrets": (),
        "variables": (),
        "files": (),
        "contexts": (),
        "warnings": (),
        "checked": True,
    }
    fields.update(changes)
    return ImportPlan(**fields)  # type: ignore[arg-type]


RESOLVED_SERVICES = {
    "web": {
        "volumes": [
            {"type": "bind", "source": "/srv/site", "target": "/usr/share/nginx/html"},
            {"type": "bind", "source": "./nginx.conf", "target": "/etc/nginx/nginx.conf"},
            {"type": "volume", "source": "data", "target": "/data"},
            {"type": "bind", "source": "/srv/logs", "target": "/var/log"},
        ]
    },
    "worker": {"volumes": [{"type": "bind", "source": "/srv/site", "target": "/other"}]},
}


def test_host_paths_list_each_resolved_path_once_with_its_uses_and_no_companion_files() -> None:
    found = compose_host_uses(RESOLVED_SERVICES, {}, COMPANIONS)

    assert list(found) == ["/srv/logs", "/srv/site"]
    assert found["/srv/site"].binds == (
        ("web", "/usr/share/nginx/html"),
        ("worker", "/other"),
    )
    assert found["/srv/site"].users == ("web", "worker")
    assert "./nginx.conf" in compose_host_uses(RESOLVED_SERVICES, {}, frozenset())


def test_a_path_below_the_scratch_directory_is_project_relative_and_companions_drop() -> None:
    services = {
        "web": {
            "volumes": [
                {"type": "bind", "source": "/data/work/ab12/nginx.conf", "target": "/c"},
                {"type": "bind", "source": "/data/work/ab12/site", "target": "/s"},
            ]
        }
    }

    found = compose_host_uses(services, {}, COMPANIONS, ["/data", "/data/work"])

    assert list(found) == ["./site"]


def test_a_volume_with_a_device_option_is_a_host_path_unless_it_is_external() -> None:
    volumes = {
        "data": {"driver_opts": {"type": "none", "o": "bind", "device": "/mnt/data"}},
        "remote": {"driver_opts": {"type": "nfs", "device": ":/export"}},
        "shared": {"external": True, "driver_opts": {"device": "/ignored"}},
        "plain": {},
    }

    found = compose_host_uses({}, volumes, COMPANIONS)

    assert {path: use.volumes for path, use in found.items()} == {
        ":/export": ("remote",),
        "/mnt/data": ("data",),
    }
    assert found["/mnt/data"].users == ("volume data",)


@pytest.mark.parametrize("source", [None, "", "   ", 7])
def test_a_bind_mount_with_no_usable_source_is_refused(source: object) -> None:
    services = {"web": {"volumes": [{"type": "bind", "source": source, "target": "/x"}]}}

    with pytest.raises(HostPathUnresolved, match="web"):
        compose_host_uses(services, {}, COMPANIONS)


def test_swarm_host_paths_are_the_bind_sources_and_volume_devices_of_the_services() -> None:
    spec = {
        "TaskTemplate": {
            "ContainerSpec": {
                "Mounts": [
                    {"Type": "bind", "Source": "/srv/site", "Target": "/site"},
                    {
                        "Type": "volume",
                        "Source": "shop_data",
                        "Target": "/data",
                        "VolumeOptions": {"DriverConfig": {"Options": {"device": "/mnt/d"}}},
                    },
                ]
            }
        }
    }

    found = swarm_host_uses([("web", spec)], "shop")

    assert found["/srv/site"].binds == (("web", "/site"),)
    assert found["/mnt/d"].volumes == ("data",)


def test_replacing_a_path_rewrites_short_and_long_syntax_and_declares_the_volume() -> None:
    rewrite = Rewrite(
        {
            ("web", "/usr/share/nginx/html"): "hostpath-1",
            ("worker", "/other"): "hostpath-1",
            ("web", "/var/log"): "hostpath-2",
        },
        declare=["hostpath-1", "hostpath-2"],
    )

    rewritten = yaml.safe_load(replace_host_paths(FILE, rewrite))

    web = rewritten["services"]["web"]["volumes"]
    assert web[0] == "hostpath-1:/usr/share/nginx/html:ro"
    assert web[1] == "./nginx.conf:/etc/nginx/nginx.conf" and web[2] == "data:/data"
    assert web[3] == {"type": "volume", "source": "hostpath-2", "target": "/var/log"}
    assert rewritten["services"]["worker"]["volumes"] == ["hostpath-1:/other"]
    assert set(rewritten["volumes"]) == {"data", "hostpath-1", "hostpath-2"}


YAML_ONE_ONE = """\
# the shop
services:
  web:
    image: nginx:1
    ports:
      - 2222:22
    environment:
      DEBUG: off
      MODE: "on"  # kept
    volumes:
      - /srv/site:/html  # the site
volumes: {}
"""


def test_a_rewrite_keeps_every_scalar_it_does_not_change_as_written() -> None:
    rewritten = replace_host_paths(
        YAML_ONE_ONE, Rewrite({("web", "/html"): "hostpath-1"}, declare=["hostpath-1"])
    )

    assert "      - 2222:22\n" in rewritten
    assert "      DEBUG: off\n" in rewritten
    assert '      MODE: "on"  # kept\n' in rewritten
    assert rewritten.startswith("# the shop\n")
    assert "      - hostpath-1:/html  # the site\n" in rewritten
    assert yaml.safe_load(rewritten)["services"]["web"]["ports"] == [133342]


def test_a_block_volumes_mapping_gains_the_new_volume_beside_the_others() -> None:
    text = "services:\n  web:\n    volumes:\n      - /a:/b\nvolumes:\n  data: {}  # kept\n"

    rewritten = replace_host_paths(
        text, Rewrite({("web", "/b"): "hostpath-1"}, declare=["hostpath-1"])
    )

    assert rewritten.endswith("volumes:\n  data: {}  # kept\n  hostpath-1: {}\n")
    assert "      - hostpath-1:/b\n" in rewritten


def test_a_file_without_a_volumes_mapping_gets_one_at_its_end() -> None:
    text = "services:\n  web:\n    volumes:\n      - /a:/b"

    rewritten = replace_host_paths(
        text, Rewrite({("web", "/b"): "hostpath-1"}, declare=["hostpath-1"])
    )

    assert yaml.safe_load(rewritten)["volumes"] == {"hostpath-1": {}}


def test_a_volume_made_plain_loses_its_driver_options_and_keeps_its_labels() -> None:
    text = (
        "services:\n  web:\n    image: x\nvolumes:\n  data:\n    driver: local\n"
        "    driver_opts:\n      device: /mnt/d\n    labels:\n      a: b\n  other: {}\n"
    )

    rewritten = yaml.safe_load(replace_host_paths(text, Rewrite(plain=["data"])))

    assert rewritten["volumes"] == {"data": {"labels": {"a": "b"}}, "other": {}}


def test_a_bind_mount_the_file_does_not_write_itself_cannot_be_replaced() -> None:
    text = "services:\n  web:\n    extends:\n      file: base.yml\n      service: web\n"

    with pytest.raises(ValueError, match="own services"):
        replace_host_paths(text, Rewrite({("web", "/b"): "hostpath-1"}, declare=["hostpath-1"]))


def test_nothing_replaced_returns_the_text_as_it_was_and_unreadable_text_refuses() -> None:
    assert replace_host_paths(FILE, Rewrite()) is FILE
    replacement = Rewrite({("web", "/b"): "hostpath-1"})
    with pytest.raises(ValueError, match="rewritten"):
        replace_host_paths("services: [", replacement)
    with pytest.raises(ValueError, match="rewritten"):
        replace_host_paths("name: only\n", replacement)


def test_the_replacement_key_is_numbered() -> None:
    assert volume_key_for_path(3) == "hostpath-3"


def test_a_mount_needs_confirming_when_the_options_name_a_device_or_a_filesystem() -> None:
    assert mount_target({}, name="v", archive_digest="a") is None
    assert mount_target({"type": "tmpfs"}, name="v", archive_digest="a") is None
    bound = mount_target({"type": "none", "device": "/srv/x"}, name="v", archive_digest="a")
    share = mount_target({"type": "nfs", "o": "addr=h"}, name="v", archive_digest="a")

    moved = mount_target({"type": "none", "device": "/srv/y"}, name="v", archive_digest="a")
    other_archive = mount_target({"type": "none", "device": "/srv/x"}, name="v", archive_digest="b")

    assert bound is not None and bound.label == "/srv/x"
    assert share is not None and share.label == "nfs"
    assert moved is not None and other_archive is not None
    assert len({bound.token, moved.token, other_archive.token}) == 3


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("copy_data", ("shop_data", "copy_data")),
        ("shared", ("shared",)),
        ("copyright", ("copyright",)),
    ],
)
def test_a_derived_name_is_looked_for_under_the_archives_project_and_others_as_they_are(
    target: str, expected: tuple[str, ...]
) -> None:
    assert candidate_sources(target, "copy", "shop") == expected


def test_declared_volumes_pair_with_archived_ones_and_the_rest_are_listed() -> None:
    declared = [("data", "copy_data"), ("logs", "copy_logs"), ("shared", "shared")]
    held = [archived("shop_data"), archived("shared"), archived("orphan")]

    matched, skipped = match_volumes(declared, held, "copy", "shop")

    assert {key: item.manifest.name for key, item in matched.items()} == {
        "data": "shop_data",
        "shared": "shared",
    }
    assert [item.name for item in skipped] == ["orphan"]


def test_the_digest_follows_the_name_and_the_host_but_not_the_choices_beside_them() -> None:
    plain = plan(host_paths=(HostPath("/srv/site", ("web",)),))
    chosen = plan(host_paths=(HostPath("/srv/site", ("web",), "volume", "hostpath-1"),))
    renamed = plan(name="copy", host_paths=(HostPath("/srv/site", ("web",)),))
    colliding = plan(
        host_paths=(HostPath("/srv/site", ("web",)),),
        collisions=(Collision("volume", "shop_data", "in use"),),
    )
    replacement = plan(
        host_paths=(HostPath("/srv/site", ("web",), "volume", "hostpath-1"),),
        collisions=(Collision("volume", "shop_hostpath-1", "in use"),),
    )

    digests = [plan_digest(item, "archive") for item in (plain, chosen, renamed, colliding)]

    assert digests[0] == digests[1]
    assert len(set(digests)) == 3
    assert plan_digest(replacement, "archive") == plan_digest(chosen, "archive")
    assert plan_digest(plain, "other") != digests[0]


def test_a_plan_can_be_confirmed_only_when_checked_and_nothing_collides() -> None:
    assert plan().can_import
    assert not plan(checked=False).can_import
    assert not plan(collisions=(Collision("stack", "shop", "in use"),)).can_import


def volume_with(options: dict[str, str]) -> VolumePlan:
    target = mount_target(options, name="shop_data", archive_digest="archive")
    return VolumePlan(
        "data",
        "shop_data",
        "shop-data",
        "volumes/0",
        1,
        5,
        "archive",
        options=options,
        mount=target,
    )


def test_the_digest_follows_a_volumes_declared_options_and_what_they_mount() -> None:
    plain = plan(volumes=(volume_with({}),))
    bound = plan(volumes=(volume_with({"type": "none", "device": "/srv/x"}),))
    moved = plan(volumes=(volume_with({"type": "none", "device": "/srv/y"}),))
    confirmed = plan(
        volumes=(
            replace(
                volume_with({"type": "none", "device": "/srv/x"}),
                mount_confirmed=True,
            ),
        )
    )

    digests = [plan_digest(item, "archive") for item in (plain, bound, moved)]

    assert len(set(digests)) == 3
    assert plan_digest(confirmed, "archive") == digests[1]


def test_a_protected_volume_blocks_the_plan_and_an_unconfirmed_mount_waits() -> None:
    bound = volume_with({"type": "none", "device": "/srv/x"})

    assert plan(volumes=(bound,)).unconfirmed_mounts == (bound,)
    assert plan(volumes=(replace(bound, mount_confirmed=True),)).unconfirmed_mounts == ()
    assert not plan(volumes=(replace(bound, protected=True),)).can_import
