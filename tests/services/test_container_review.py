"""Tests for app/services/container_review.py: change summary, review sections, mount uses."""

from typing import Any

import pytest

from app.services.container_review import (
    CHANGE_CATEGORIES,
    changed_categories,
    endpoint_keys,
    review_sections,
    volume_uses,
)
from app.services.container_spec import spec_from_form
from tests.container_support import inspect_document

NEW_ID = "2" * 64
NEW_IMAGE_ID = "sha256:" + "c" * 64
FRONT = "f" * 64
MIB = 1024 * 1024
SECRET = "hunter2-secret"


def old(**parts: Any) -> dict[str, Any]:
    return inspect_document(**parts)


def new(**parts: Any) -> dict[str, Any]:
    config = {"Hostname": NEW_ID[:12], **parts.pop("config", {})}
    return inspect_document(Id=NEW_ID, config=config, **parts)


def mount(name: str, destination: str, *, writable: bool = True) -> dict[str, Any]:
    return {"Type": "volume", "Name": name, "Destination": destination, "RW": writable}


def endpoint(**fields: Any) -> dict[str, Any]:
    return {"NetworkID": FRONT, "Aliases": [], **fields}


CHANGES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("image", {}, {"Image": NEW_IMAGE_ID}),
    ("command", {"config": {"Cmd": ["a"]}}, {"config": {"Cmd": ["b"]}}),
    ("entrypoint", {"config": {"Entrypoint": ["a"]}}, {"config": {}}),
    ("env", {"config": {"Env": ["A=1"]}}, {"config": {"Env": ["A=2"]}}),
    ("labels", {"config": {"Labels": {"a": "1"}}}, {"config": {"Labels": {"a": "1", "b": "2"}}}),
    ("ports", {"config": {"ExposedPorts": {"80/tcp": {}}}}, {"config": {}}),
    ("ports", {"host": {"PortBindings": {"80/tcp": [{"HostPort": "1"}]}}}, {}),
    ("ports", {"host": {"PublishAllPorts": True}}, {}),
    ("user", {"config": {"User": "a"}}, {"config": {"User": "b"}}),
    ("working_dir", {"config": {"WorkingDir": "/a"}}, {"config": {}}),
    ("healthcheck", {"config": {"Healthcheck": {"Test": ["NONE"]}}}, {"config": {}}),
    ("restart_policy", {"host": {"RestartPolicy": {"Name": "always"}}}, {}),
    ("resources", {"host": {"Memory": MIB}}, {"host": {"Memory": 2 * MIB}}),
    ("resources", {"host": {"Ulimits": [{"Name": "nofile"}]}}, {}),
    ("log_config", {"host": {"LogConfig": {"Type": "local"}}}, {}),
    ("mounts", {"mounts": [mount("a", "/a")]}, {"mounts": [mount("a", "/b")]}),
    ("mounts", {"mounts": [mount("a", "/a")]}, {"mounts": [mount("a", "/a", writable=False)]}),
    ("mounts", {"host": {"Tmpfs": {"/run": ""}}}, {}),
    ("networks", {"networks": {"front": endpoint()}}, {"networks": {"back": endpoint()}}),
    (
        "networks",
        {"networks": {"front": endpoint(Aliases=["web"])}},
        {"networks": {"front": endpoint(Aliases=["other"])}},
    ),
    (
        "networks",
        {"networks": {"front": endpoint(IPAMConfig={"IPv4Address": "10.0.0.2"})}},
        {"networks": {"front": endpoint(IPAMConfig={"IPv4Address": "10.0.0.3"})}},
    ),
    ("networks", {"host": {"NetworkMode": "front"}}, {"host": {"NetworkMode": "bridge"}}),
    ("other", {"config": {"Tty": True}}, {"config": {}}),
    ("other", {"host": {"Privileged": True}}, {}),
    ("other", {"config": {"Hostname": "custom"}}, {"config": {"Hostname": "other"}}),
]


@pytest.mark.parametrize(
    ("category", "before", "after"),
    [pytest.param(c, b, a, id=f"{c}-{i}") for i, (c, b, a) in enumerate(CHANGES)],
)
def test_a_difference_in_one_category_of_configuration_is_reported_under_that_category(
    category: str, before: dict[str, Any], after: dict[str, Any]
) -> None:
    assert changed_categories(old(**before), new(**after)) == (category,)


def test_two_containers_that_differ_only_in_their_ids_report_no_change() -> None:
    assert changed_categories(old(), new()) == ("none",)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"config": {"Env": ["A=1", "B=2"]}}, {"config": {"Env": ["B=2", "A=1"]}}),
        ({"config": {"Labels": {}}}, {"config": {}}),
        ({"config": {"Env": []}}, {"config": {}}),
        (
            {"networks": {"front": endpoint(Aliases=["web", "1" * 12])}},
            {"networks": {"front": endpoint(Aliases=["web", "2" * 12])}},
        ),
        ({"config": {"MacAddress": "aa"}}, {"config": {"MacAddress": "bb"}}),
        ({"host": {"ContainerIDFile": "/a"}}, {"host": {"ContainerIDFile": "/b"}}),
        ({"config": {"Image": "nginx:1"}}, {"config": {"Image": "nginx:1"}}),
    ],
    ids=[
        "env-order",
        "empty-labels",
        "empty-env",
        "alias-short-id",
        "mac-address",
        "container-id-file",
        "same-reference",
    ],
)
def test_a_difference_that_is_not_a_configuration_change_is_not_reported(
    before: dict[str, Any], after: dict[str, Any]
) -> None:
    assert changed_categories(old(**before), new(**after)) == ("none",)


def test_several_changes_come_back_in_the_fixed_vocabulary_order() -> None:
    before = old(
        config={"Cmd": ["a"], "User": "u"}, host={"Memory": MIB}, mounts=[mount("a", "/a")]
    )
    after = new(config={"Cmd": ["b"]}, Image=NEW_IMAGE_ID)

    result = changed_categories(before, after)

    assert result == ("image", "command", "mounts", "resources", "user")
    assert list(result) == sorted(result, key=CHANGE_CATEGORIES.index)


def test_the_summary_names_categories_and_never_a_value() -> None:
    before = old(config={"Env": [f"TOKEN={SECRET}"], "Labels": {"k": SECRET}})
    after = new(config={"Env": ["TOKEN=other"], "Labels": {"k": "other"}})

    result = changed_categories(before, after)

    assert result == ("env", "labels")
    assert SECRET not in str(result)


def sections(**fields: str) -> dict[str, dict[str, tuple[str, ...]]]:
    spec = spec_from_form({"image": "nginx", **fields})
    return {
        section.key: {item.label: item.values for item in section.items}
        for section in review_sections(spec)
    }


def test_a_bare_spec_reviews_only_the_default_network_mode() -> None:
    assert sections() == {"network": {"Mode": ("bridge",)}}


def test_sensitive_environment_variables_are_listed_with_their_values_withheld() -> None:
    spec = spec_from_form(
        {
            "image": "nginx",
            "env-0-key": "DB_PASSWORD",
            "env-0-value": SECRET,
            "env-1-key": "API_TOKEN",
            "env-1-value": SECRET,
            "env-2-key": "MODE",
            "env-2-value": "fast",
        }
    )

    items = review_sections(spec)[0].items

    assert [(item.label, item.hidden, item.values) for item in items] == [
        ("DB_PASSWORD", True, ()),
        ("API_TOKEN", True, ()),
        ("MODE", False, ("fast",)),
    ]


REVIEWED = [
    pytest.param(
        {"command": "serve 'a b'", "entrypoint": "/e", "working_dir": "/srv", "user": "1000"},
        "command",
        {
            "Command": ("serve", "a b"),
            "Entrypoint": ("/e",),
            "Working directory": ("/srv",),
            "User": ("1000",),
        },
        id="command",
    ),
    pytest.param(
        {
            "port-0-container_port": "80",
            "port-1-container_port": "53",
            "port-1-host_port": "5353",
            "port-1-host_ip": "127.0.0.1",
            "port-1-protocol": "udp",
            "port-2-container_port": "8080",
            "port-2-host_port": "9090",
            "publish_all": "on",
        },
        "ports",
        {
            "Published": (
                "assigned → 80/tcp",
                "127.0.0.1:5353 → 53/udp",
                "9090 → 8080/tcp",
            ),
            "Publish all exposed ports": ("yes",),
        },
        id="ports",
    ),
    pytest.param(
        {
            "mount-0-type": "volume",
            "mount-0-source": "data",
            "mount-0-target": "/data",
            "mount-0-read_only": "on",
            "mount-1-type": "volume",
            "mount-1-target": "/anon",
            "mount-2-type": "tmpfs",
            "mount-2-target": "/tmp",
            "mount-2-tmpfs_size": "64m",
            "mount-3-type": "bind",
            "mount-3-source": "/srv",
            "mount-3-target": "/x",
        },
        "mounts",
        {
            "Mounts": (
                "volume data → /data (read-only)",
                "volume (anonymous) → /anon",
                "tmpfs → /tmp, size 64 MiB",
                "bind /srv → /x",
            )
        },
        id="mounts",
    ),
    pytest.param(
        {"network_mode": "networks", "network-0-name": "front", "network-0-aliases": "web www"},
        "network",
        {"Mode": ("networks",), "front": ("aliases web, www",)},
        id="networks",
    ),
    pytest.param(
        {"restart_policy": "on-failure", "restart_max_retries": "3"},
        "restart",
        {"Policy": ("on-failure, at most 3 retries",)},
        id="restart",
    ),
    pytest.param(
        {
            "cpus": "1.50",
            "cpu_shares": "512",
            "memory": "512m",
            "memory_swap": "-1",
            "pids_limit": "9",
        },
        "resources",
        {
            "CPUs": ("1.5",),
            "CPU shares": ("512",),
            "Memory": ("512 MiB",),
            "Swap": ("unlimited",),
            "PIDs": ("9",),
        },
        id="resources",
    ),
    pytest.param(
        {"healthcheck_mode": "none"},
        "healthcheck",
        {"Health check": ("disabled",)},
        id="health-check-disabled",
    ),
    pytest.param(
        {
            "healthcheck_mode": "command",
            "healthcheck_test": "true",
            "healthcheck_interval": "1m30s",
            "healthcheck_retries": "3",
        },
        "healthcheck",
        {"Command": ("true",), "Interval": ("1m30s",), "Retries": ("3",)},
        id="health-check-command",
    ),
    pytest.param(
        {"log_driver": "local", "logopt-0-key": "max-size", "logopt-0-value": "1m"},
        "logging",
        {"Driver": ("local",), "max-size": ("1m",)},
        id="logging",
    ),
    pytest.param(
        {
            "dns_servers": "1.1.1.1",
            "dns_search": "x.test",
            "dns_options": "ndots:2",
            "host-0-name": "db",
            "host-0-address": "10.0.0.5",
        },
        "dns",
        {
            "DNS servers": ("1.1.1.1",),
            "Search domains": ("x.test",),
            "DNS options": ("ndots:2",),
            "Extra hosts": ("db:10.0.0.5",),
        },
        id="dns",
    ),
    pytest.param(
        {
            "init": "on",
            "privileged": "on",
            "cap_drop": "ALL",
            "cap_add": "NET_ADMIN",
            "device-0-host_path": "/dev/fuse",
            "device-0-permissions": "rw",
            "runtime": "nvidia",
            "pid_host": "on",
            "ipc_host": "on",
        },
        "security",
        {
            "Init process": ("yes",),
            "Privileged": ("yes",),
            "Capabilities dropped": ("ALL",),
            "Capabilities added": ("NET_ADMIN",),
            "Devices": ("/dev/fuse → /dev/fuse (rw)",),
            "Runtime": ("nvidia",),
            "Host PID namespace": ("yes",),
            "Host IPC namespace": ("yes",),
        },
        id="security",
    ),
]


@pytest.mark.parametrize(("fields", "key", "expected"), REVIEWED)
def test_each_section_of_the_review_resolves_its_values_to_what_the_engine_will_receive(
    fields: dict[str, str], key: str, expected: dict[str, tuple[str, ...]]
) -> None:
    assert sections(**fields)[key] == expected


def test_the_sections_follow_the_order_of_the_form() -> None:
    result = sections(
        init="on",
        memory="64m",
        restart_policy="always",
        **{"env-0-key": "A", "port-0-container_port": "80", "label-0-key": "k"},
    )

    assert list(result) == [
        "environment",
        "labels",
        "ports",
        "network",
        "restart",
        "resources",
        "security",
    ]


def test_a_volume_use_is_found_in_a_mounts_entry_and_in_a_binds_entry() -> None:
    host = {
        "Mounts": [
            {"Type": "volume", "Source": "data", "Target": "/data", "ReadOnly": True},
            {"Type": "volume", "Source": "", "Target": "/anon"},
            {"Type": "tmpfs", "Target": "/run"},
            {"Type": "bind", "Source": "/etc", "Target": "/etc"},
        ],
        "Binds": ["cache:/c:ro", "work:/w", "/etc:/e", "bad"],
    }

    assert volume_uses(host, None) == [
        ("data", True, "HostConfig.Mounts[0].Source", None),
        ("cache", True, "HostConfig.Binds[0]", None),
        ("work", False, "HostConfig.Binds[1]", None),
    ]


def test_a_volume_use_in_mounts_names_the_form_row_it_came_from() -> None:
    host = {"Mounts": [{"Type": "volume", "Source": "data", "Target": "/data"}]}

    uses = volume_uses(host, {"mounts": (6,)})

    assert uses == [("data", False, "HostConfig.Mounts[0].Source", "mount-6-source")]


def test_the_networks_a_body_attaches_are_listed_by_their_keys() -> None:
    body: dict[str, Any] = {"NetworkingConfig": {"EndpointsConfig": {FRONT: {}, "back": {}}}}

    assert endpoint_keys(body) == [FRONT, "back"]
    assert endpoint_keys({}) == []
