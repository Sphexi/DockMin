"""The pure parts of a stack import plan: host paths, volume matching and the digest."""

from datetime import UTC, datetime

import pytest
import yaml

from app.services.stack_import_archive import VolumeSummary
from app.services.stack_import_plan import (
    Collision,
    HostPath,
    ImportPlan,
    candidate_sources,
    host_path_uses,
    match_volumes,
    plan_digest,
    replace_host_paths,
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


def test_host_paths_list_each_path_once_with_its_services_and_leave_companions_out() -> None:
    assert host_path_uses(FILE, COMPANIONS) == {
        "/srv/logs": ("web",),
        "/srv/site": ("web", "worker"),
    }
    assert "./nginx.conf" in host_path_uses(FILE, frozenset())


def test_a_file_that_is_not_yaml_has_no_host_paths() -> None:
    assert host_path_uses("services: [", COMPANIONS) == {}
    assert host_path_uses("- a\n- b\n", COMPANIONS) == {}


def test_replacing_a_path_rewrites_short_and_long_syntax_and_declares_the_volume() -> None:
    rewritten = yaml.safe_load(
        replace_host_paths(FILE, {"/srv/site": "hostpath-1", "/srv/logs": "hostpath-2"})
    )

    web = rewritten["services"]["web"]["volumes"]
    assert web[0] == "hostpath-1:/usr/share/nginx/html:ro"
    assert web[1] == "./nginx.conf:/etc/nginx/nginx.conf" and web[2] == "data:/data"
    assert web[3] == {"type": "volume", "source": "hostpath-2", "target": "/var/log"}
    assert rewritten["services"]["worker"]["volumes"] == ["hostpath-1:/other"]
    assert set(rewritten["volumes"]) == {"data", "hostpath-1", "hostpath-2"}


def test_nothing_replaced_returns_the_text_as_it_was_and_unreadable_text_refuses() -> None:
    assert replace_host_paths(FILE, {}) is FILE
    with pytest.raises(ValueError, match="rewritten"):
        replace_host_paths("services: [", {"/srv/site": "hostpath-1"})
    with pytest.raises(ValueError, match="rewritten"):
        replace_host_paths("name: only\n", {"/srv/site": "hostpath-1"})


def test_the_replacement_key_is_numbered() -> None:
    assert volume_key_for_path(3) == "hostpath-3"


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
