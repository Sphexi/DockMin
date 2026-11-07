"""Tests for app/services/stack_restrictions.py over resolved `config --format json` models."""

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from app.logging import REDACTED
from app.services import stack_restrictions as sr
from app.services.container_restrictions import OPTION_IDS, find_restricted, option_label
from app.services.stack_restrictions import StackFinding

PROJECT = "sample"
SECRET = "alpha-secret-value"
OTHER_SECRET = "bravo-secret-value"
RESERVED_PROJECT_LABEL = "com.docker.compose.project"
RESERVED_STACK_LABEL = "com.docker.stack.namespace"
DEFAULT_RUNTIME = "runc"

INTERPOLATED_SOURCES = [
    ("DOCKER_HOST", "the variable DOCKER_HOST is reserved and cannot be used in a stack."),
    (
        "DOCKER_TLS_VERIFY",
        "the variable DOCKER_TLS_VERIFY is reserved and cannot be used in a stack.",
    ),
    (
        "DOCKER_CERT_PATH",
        "the variable DOCKER_CERT_PATH is reserved and cannot be used in a stack.",
    ),
    ("DOCKER_CONFIG", "the variable DOCKER_CONFIG is reserved and cannot be used in a stack."),
    ("HOME", "the variable HOME is reserved and cannot be used in a stack."),
    ("TMPDIR", "the variable TMPDIR is reserved and cannot be used in a stack."),
    ("A$B", "an environment variable name cannot be interpolated."),
]

BASE: dict[str, Any] = {
    "name": PROJECT,
    "configs": {"motd": {"name": f"{PROJECT}_motd", "content": "hello\n$$literal\n"}},
    "networks": {"appnet": {"name": f"{PROJECT}_appnet", "ipam": {}}},
    "secrets": {"tok": {"name": f"{PROJECT}_tok", "environment": "SAMPLE_SECRET"}},
    "volumes": {"data": {"name": f"{PROJECT}_data"}},
    "services": {
        "once": {
            "command": None,
            "entrypoint": ["/bin/sh", "-c", "exit 3"],
            "image": "registry:2",
            "networks": {"appnet": None},
            "restart": "no",
        },
        "web": {
            "command": None,
            "configs": [{"source": "motd", "target": "/etc/motd", "mode": "0444"}],
            "deploy": {
                "resources": {
                    "limits": {"cpus": 0.5, "memory": "134217728"},
                    "reservations": {"memory": "67108864"},
                },
                "placement": {},
            },
            "environment": {"FROM_DOTENV": "dotenv-not-read"},
            "image": "registry:2",
            "networks": {"appnet": None},
            "ports": [
                {
                    "mode": "ingress",
                    "host_ip": "127.0.0.1",
                    "target": 5000,
                    "published": "23781",
                    "protocol": "tcp",
                }
            ],
            "secrets": [{"source": "tok", "target": "tok"}],
            "volumes": [
                {"type": "volume", "source": "data", "target": "/var/lib/registry", "volume": {}},
                {"type": "volume", "target": "/anon", "volume": {}},
            ],
            "post_start": [{"command": ["/bin/sh", "-c", "echo hooked"]}],
        },
    },
}


@dataclass(frozen=True)
class Model:
    name: str
    services: dict[str, Any]
    networks: dict[str, Any]
    volumes: dict[str, Any]
    configs: dict[str, Any]
    secrets: dict[str, Any]
    document: dict[str, Any]


def model(doc: dict[str, Any]) -> Model:
    return Model(
        doc["name"],
        doc.get("services", {}),
        doc.get("networks", {}),
        doc.get("volumes", {}),
        doc.get("configs", {}),
        doc.get("secrets", {}),
        doc,
    )


def web_doc(
    changes: dict[str, Any] | None = None, *, without: tuple[str, ...] = ()
) -> dict[str, Any]:
    doc = copy.deepcopy(BASE)
    service = doc["services"]["web"]
    service.update(changes or {})
    for key in without:
        service.pop(key)
    return doc


def top_doc(section: str, key: str, value: Any) -> dict[str, Any]:
    doc = copy.deepcopy(BASE)
    doc.setdefault(section, {})[key] = value
    return doc


def scanned(doc: dict[str, Any], runtime: str | None = None) -> sr.StackScan:
    return sr.scan(model(doc), default_runtime=runtime)


def found(doc: dict[str, Any], runtime: str | None = None) -> set[tuple[str, str]]:
    return {(f.option, f.path) for f in scanned(doc, runtime).findings}


def options(doc: dict[str, Any], runtime: str | None = None) -> set[str]:
    return {option for option, _ in found(doc, runtime)}


def with_mounts(*mounts: Any) -> dict[str, Any]:
    return web_doc({"volumes": list(mounts)})


def with_endpoint(**config: Any) -> dict[str, Any]:
    return web_doc({"networks": {"appnet": config or None}})


def service_key_of(path: str) -> str:
    return ".".join(path.split(".")[:3]).split("[")[0]


def host_mode(mode: str) -> dict[str, Any]:
    return web_doc({"network_mode": mode}, without=("networks",))


class TestScanCleanModel:
    def test_a_realistic_model_has_no_findings_and_reports_its_objects(self) -> None:
        result = scanned(BASE)

        assert result.findings == ()
        assert result.external == ()
        assert result.project_volumes == (f"{PROJECT}_data",)
        assert result.project_networks == (f"{PROJECT}_appnet",)
        assert dict(result.images) == {"registry:2": ("once", "missing")}

    def test_the_default_network_is_a_project_network_only_when_a_service_uses_it(self) -> None:
        doc = web_doc({"networks": {"default": None}})

        result = scanned(doc)

        assert result.project_networks == (f"{PROJECT}_appnet", f"{PROJECT}_default")
        assert result.findings == ()

    def test_images_keep_the_service_with_the_strictest_pull_policy(self) -> None:
        doc = copy.deepcopy(BASE)
        doc["services"].update(
            {
                "a": {"image": "busybox", "pull_policy": "never"},
                "b": {"image": "busybox", "pull_policy": "always"},
                "c": {"image": "busybox"},
                "e": {"image": "alpine", "pull_policy": "daily"},
                "f": {"image": "debian", "pull_policy": "never"},
                "g": {"image": "debian", "pull_policy": "missing"},
            }
        )

        images = dict(scanned(doc).images)

        assert images["busybox"] == ("b", "always")
        assert images["alpine"] == ("e", "daily")
        assert images["debian"] == ("g", "missing")


RESTRICTED_KEYS = {
    "privileged": (True, "privileged"),
    "cap_add": (["SYS_ADMIN"], "cap_add"),
    "devices": (["/dev/sda:/dev/sda"], "devices"),
    "device_cgroup_rules": (["c 1:3 mr"], "devices"),
    "gpus": ([{"driver": "nvidia", "count": 1}], "devices"),
    "volumes_from": (["db"], "volumes_from"),
    "links": (["db"], "links"),
    "external_links": (["x"], "links"),
    "cgroup_parent": ("/x", "cgroup_parent"),
    "userns_mode": ("host", "userns_mode"),
    "uts": ("host", "uts_namespace"),
    "sysctls": ({"net.core.somaxconn": "1"}, "sysctls"),
    "storage_opt": ({"size": "1G"}, "storage_opt"),
    "oom_kill_disable": (True, "oom_kill_disable"),
    "cpu_rt_period": (1000, "realtime_cpu"),
    "cpu_rt_runtime": (1000, "realtime_cpu"),
    "annotations": ({"a": "b"}, "annotations"),
    "oom_score_adj": (-500, "oom_score_adj"),
    "runtime": ("nvidia", "runtime"),
    "isolation": ("hyperv", "isolation"),
    "ipc": ("host", "ipc_namespace"),
    "pid": ("host", "pid_namespace"),
    "cgroup": ("host", "cgroupns_mode"),
    "security_opt": (["seccomp=unconfined"], "security_opt"),
    "logging": ({"driver": "syslog"}, "log_driver"),
}

EMPTY_VALUES_ALLOWED = [
    ("privileged", False),
    ("cap_add", []),
    ("devices", []),
    ("sysctls", {}),
    ("oom_score_adj", 0),
    ("oom_score_adj", 100),
    ("oom_kill_disable", False),
    ("links", []),
    ("annotations", {}),
    ("runtime", None),
    ("isolation", "default"),
    ("security_opt", []),
]


class TestScanServiceKeys:
    @pytest.mark.parametrize(
        ("key", "value", "option"),
        [(key, *pair) for key, pair in RESTRICTED_KEYS.items()],
        ids=RESTRICTED_KEYS,
    )
    def test_a_restricted_key_is_found_at_its_compose_path(
        self, key: str, value: Any, option: str
    ) -> None:
        doc = web_doc({key: value})

        result = {(o, service_key_of(path)) for o, path in found(doc)}

        assert result == {(option, f"services.web.{key}")}

    @pytest.mark.parametrize(("key", "value"), EMPTY_VALUES_ALLOWED)
    def test_an_empty_or_false_value_is_not_a_use(self, key: str, value: Any) -> None:
        assert found(web_doc({key: value})) == set()

    @pytest.mark.parametrize(
        "spelled",
        [
            "Privileged",
            "PRIVILEGED",
            "privileged ",
            " privileged",
            "cap-add",
            "Cap_Add",
            "pr\u0131vileged",
            "privileged\u200b",
        ],
    )
    def test_a_respelled_restricted_key_is_unknown_not_allowed(self, spelled: str) -> None:
        result = found(web_doc({spelled: True}))

        assert {option for option, _ in result} == {"unknown_option"}

    @pytest.mark.parametrize(
        "key",
        [
            "use_api_socket",
            "credential_spec",
            "pull_refresh_after",
            "models",
            "build",
            "provider",
            "develop",
            "extends",
            "totally_new_key",
            "X-upper",
        ],
    )
    def test_a_key_outside_the_allow_list_is_unknown_even_with_a_false_value(
        self, key: str
    ) -> None:
        result = found(web_doc({key: False}))

        assert result == {("unknown_option", f"services.web.{key}")}

    @pytest.mark.parametrize("key", ["env_file", "label_file"])
    def test_a_file_key_is_allowed_because_its_effect_is_read_from_the_model(
        self, key: str
    ) -> None:
        assert found(web_doc({key: ["/stacks/t/a.env"]})) == set()

    def test_extension_keys_are_ignored(self) -> None:
        assert found(web_doc({"x-anything": {"env_file": "ignored"}})) == set()

    def test_a_non_text_service_key_is_unknown(self) -> None:
        doc = copy.deepcopy(BASE)
        doc["services"]["web"] = {1: "x"}

        assert found(doc) == {("unknown_option", "services.web.<key>")}

    def test_a_service_that_is_not_a_mapping_is_unknown(self) -> None:
        doc = copy.deepcopy(BASE)
        doc["services"]["web"] = "nginx"

        assert found(doc) == {("unknown_option", "services.web")}

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("host", {"network_host"}),
            ("Host", {"network_host"}),
            (" host ", {"network_host"}),
            ("service:db", {"network_container"}),
            ("container:abc", {"network_container"}),
            ("Container:abc", {"network_container"}),
            ("bridge", set()),
            ("none", set()),
            ("Bridge", {"unknown_option"}),
            ("othernet", {"unknown_option"}),
            ("default", {"unknown_option"}),
        ],
    )
    def test_network_mode_allows_only_bridge_and_none(self, mode: str, expected: Any) -> None:
        assert options(host_mode(mode)) == expected

    @pytest.mark.parametrize(
        ("key", "mode", "expected"),
        [
            ("ipc", "shareable", set()),
            ("ipc", "private", set()),
            ("ipc", "none", set()),
            ("ipc", "service:x", {"ipc_namespace"}),
            ("ipc", "Host", {"ipc_namespace"}),
            ("pid", "private", set()),
            ("pid", "host", {"pid_namespace"}),
            ("pid", "service:x", {"pid_namespace"}),
            ("cgroup", "private", set()),
            ("cgroup", "host", {"cgroupns_mode"}),
        ],
    )
    def test_namespace_modes_are_checked_against_the_allowed_set(
        self, key: str, mode: str, expected: Any
    ) -> None:
        assert options(web_doc({key: mode})) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (["no-new-privileges"], set()),
            (["no-new-privileges:true"], set()),
            (["apparmor=unconfined"], {"security_opt"}),
            ("no-new-privileges", {"security_opt"}),
        ],
    )
    def test_security_opt_allows_only_the_listed_entries(
        self, value: list[Any] | str, expected: Any
    ) -> None:
        assert options(web_doc({"security_opt": value})) == expected

    def test_a_refused_security_opt_entry_is_found_by_position(self) -> None:
        doc = web_doc({"security_opt": ["no-new-privileges", "seccomp=unconfined"]})

        assert found(doc) == {("security_opt", "services.web.security_opt[1]")}

    def test_runtime_is_allowed_only_when_it_is_the_daemon_default(self) -> None:
        doc = web_doc({"runtime": DEFAULT_RUNTIME})

        assert options(doc, DEFAULT_RUNTIME) == set()
        assert options(doc, "other") == {"runtime"}
        assert options(doc, None) == {"runtime"}

    @pytest.mark.parametrize(
        ("logging", "expected"),
        [
            ({"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}, set()),
            ({"driver": "local"}, set()),
            ({"driver": "none"}, set()),
            ({"driver": "json-file", "options": {"labels": "x"}}, {"log_driver"}),
            ({"driver": "json-file", "x": 1}, {"unknown_option"}),
            ("syslog", {"log_driver"}),
        ],
    )
    def test_logging_allows_only_safe_drivers_and_options(
        self, logging: dict[str, Any] | str, expected: Any
    ) -> None:
        assert options(web_doc({"logging": logging})) == expected

    def test_healthchecks_ulimits_and_limits_are_plain_keys(self) -> None:
        doc = web_doc(
            {
                "healthcheck": {"test": ["CMD", "true"], "interval": "5s"},
                "ulimits": {"nofile": {"soft": 1, "hard": 2}},
                "cap_drop": ["ALL"],
                "mem_limit": "1g",
            }
        )

        assert found(doc) == set()


class TestScanMounts:
    @pytest.mark.parametrize(
        "mount",
        [
            {"type": "volume", "target": "/a", "volume": {}},
            {"type": "volume", "source": "data", "target": "/a", "volume": {}},
            {
                "type": "volume",
                "source": "data",
                "target": "/a",
                "read_only": True,
                "consistency": "cached",
                "volume": {"nocopy": True, "subpath": "x"},
            },
            {"type": "tmpfs", "target": "/t", "tmpfs": {"size": 1048576, "mode": 1777}},
        ],
        ids=["anonymous", "project volume", "volume options", "tmpfs"],
    )
    def test_volume_and_tmpfs_mounts_are_allowed(self, mount: dict[str, Any]) -> None:
        assert found(with_mounts(mount)) == set()

    @pytest.mark.parametrize("kind", ["bind", "Bind", " BIND "])
    def test_a_bind_mount_is_found_on_its_type_in_any_spelling(self, kind: str) -> None:
        mount = {"type": kind, "source": "/etc", "target": "/e", "bind": {"create_host_path": True}}

        assert found(with_mounts(mount)) == {("bind_mount", "services.web.volumes[0].type")}

    @pytest.mark.parametrize("kind", ["npipe", "cluster", "image", "Volume", "VOLUME", "", None, 5])
    def test_any_other_mount_type_is_refused(self, kind: int | str | None) -> None:
        mount = {"type": kind, "source": "x", "target": "/e"}

        assert options(with_mounts(mount)) == {"mount_type"}

    @pytest.mark.parametrize(
        "mount",
        [
            {"type": "volume", "source": "data", "target": "/a", "bind": {"propagation": "x"}},
            {"type": "volume", "source": "data", "target": "/a", "volume": {"labels": {"a": "b"}}},
            {"type": "tmpfs", "target": "/a", "tmpfs": {"options": "x"}},
            {"type": "volume", "target": "/a", "x-y": 1},
            {"type": "volume", "source": "undeclared", "target": "/a"},
            "/etc:/e",
        ],
        ids=[
            "bind sub-object on a volume",
            "unknown volume option",
            "unknown tmpfs option",
            "extra mount key",
            "source not declared",
            "short syntax",
        ],
    )
    def test_an_unrecognised_mount_shape_is_unknown(self, mount: dict[str, Any] | str) -> None:
        assert options(with_mounts(mount)) == {"unknown_option"}

    def test_external_volumes_are_reported_by_resolved_name_and_mode(self) -> None:
        doc = top_doc("volumes", "shared", {"name": "teamdata", "external": True})
        doc["services"]["web"]["volumes"] = [
            {"type": "volume", "source": "shared", "target": "/s", "read_only": True, "volume": {}},
            {"type": "volume", "source": "shared", "target": "/w", "volume": {}},
            {"type": "volume", "source": "shared", "target": "/x", "read_only": "true"},
        ]

        result = scanned(doc)

        assert result.external == (
            sr.ExternalUse("volume", "teamdata", "services.web.volumes[0].source", True),
            sr.ExternalUse("volume", "teamdata", "services.web.volumes[1].source", False),
            sr.ExternalUse("volume", "teamdata", "services.web.volumes[2].source", False),
        )
        assert result.project_volumes == (f"{PROJECT}_data",)

    def test_external_volumes_are_not_counted_as_created_by_the_project(self) -> None:
        doc = top_doc("volumes", "shared", {"name": "teamdata", "external": {"name": "x"}})

        assert "teamdata" not in scanned(doc).project_volumes


class TestScanNetworks:
    def test_external_networks_are_reported_with_their_service_path(self) -> None:
        doc = top_doc("networks", "ext", {"name": "corp-net", "external": True})
        doc["services"]["web"]["networks"]["ext"] = None

        result = scanned(doc)

        assert result.external == (
            sr.ExternalUse("network", "corp-net", "services.web.networks.ext", False),
        )
        assert "corp-net" not in result.project_networks

    def test_an_external_network_named_host_is_a_host_network(self) -> None:
        doc = top_doc("networks", "hostnet", {"name": "host", "external": True})
        doc["services"]["web"]["networks"]["hostnet"] = None

        assert "network_host" in options(doc)

    @pytest.mark.parametrize(
        "config",
        [
            {
                "aliases": ["a"],
                "ipv4_address": "10.0.0.2",
                "ipv6_address": "fd00::2",
                "mac_address": "02:42:ac:11:00:02",
                "priority": 1,
                "gw_priority": 2,
            },
            {"driver_opts": {}},
        ],
    )
    def test_endpoint_settings_are_allowed(self, config: dict[str, Any]) -> None:
        assert found(with_endpoint(**config)) == set()

    def test_endpoint_driver_options_are_refused(self) -> None:
        assert found(with_endpoint(driver_opts={"x": "y"})) == {
            ("endpoint_driver_opts", "services.web.networks.appnet.driver_opts")
        }

    @pytest.mark.parametrize(
        "config", [{"link_local_ips": ["169.254.1.1"]}, {"interface_name": "e"}]
    )
    def test_other_endpoint_settings_are_unknown(self, config: dict[str, Any]) -> None:
        assert options(with_endpoint(**config)) == {"unknown_option"}

    def test_a_list_of_network_names_is_allowed(self) -> None:
        assert found(web_doc({"networks": ["appnet"]})) == set()

    def test_a_network_that_is_not_declared_is_unknown(self) -> None:
        assert options(web_doc({"networks": {"missing": None}})) == {"unknown_option"}

    @pytest.mark.parametrize(
        ("changes", "expected"),
        [
            (
                {"driver": "bridge", "internal": True, "attachable": True, "labels": {"a": "b"}},
                set(),
            ),
            ({"enable_ipv6": True, "ipam": {}, "x-a": 1, "external": True}, set()),
            (
                {
                    "driver_opts": {
                        "com.docker.network.driver.mtu": "1400",
                        "com.docker.network.bridge.enable_icc": "true",
                        "com.docker.network.bridge.enable_ip_masquerade": "true",
                    }
                },
                set(),
            ),
            ({"ipam": {"config": [{"subnet": "10.5.0.0/24"}]}}, {"network_addressing"}),
            ({"ipam": {"driver": "default"}}, {"network_addressing"}),
            ({"driver": "overlay"}, {"network_driver"}),
            ({"driver": "macvlan"}, {"network_driver"}),
            ({"driver": "Bridge"}, {"network_driver"}),
            ({"driver": "none"}, {"network_driver"}),
            ({"driver_opts": {"com.docker.network.bridge.name": "br0"}}, {"network_option"}),
            ({"driver_opts": {"com.docker.network.driver.MTU": "1"}}, {"network_option"}),
            ({"driver_opts": "x"}, {"network_option"}),
            ({"enable_ipv4": False}, {"unknown_option"}),
        ],
    )
    def test_top_level_network_keys_follow_the_standard_bridge_rules(
        self, changes: dict[str, Any], expected: Any
    ) -> None:
        doc = top_doc("networks", "n", {"name": f"{PROJECT}_n", **changes})

        assert options(doc) == expected

    def test_a_refused_network_option_is_found_at_the_option_path(self) -> None:
        value = {"name": "p_n", "driver_opts": {"com.docker.network.bridge.name": "br0"}}

        result = found(top_doc("networks", "n", value))

        assert result == {
            ("network_option", "networks.n.driver_opts.com.docker.network.bridge.name")
        }

    def test_a_network_entry_that_is_not_a_mapping_is_unknown(self) -> None:
        assert found(top_doc("networks", "n", "x")) == {("unknown_option", "networks.n")}


class TestScanVolumesAndSources:
    @pytest.mark.parametrize(
        ("changes", "expected"),
        [
            ({"driver": "local", "labels": {"a": "b"}, "driver_opts": {}}, set()),
            ({"external": True}, set()),
            ({"driver_opts": {"type": "none", "o": "bind", "device": "/etc"}}, {"volume_option"}),
            ({"driver": "rexray"}, {"volume_option"}),
            ({"driver": "Local"}, {"volume_option"}),
            ({"x": 1}, {"unknown_option"}),
        ],
    )
    def test_top_level_volume_keys_allow_only_the_local_driver_without_options(
        self, changes: dict[str, Any], expected: Any
    ) -> None:
        doc = top_doc("volumes", "v", {"name": "p_v", **changes})

        assert options(doc) == expected

    def test_a_volume_driver_option_is_found_at_the_volume(self) -> None:
        doc = top_doc("volumes", "v", {"name": "p_v", "driver_opts": {"device": "/etc"}})

        assert found(doc) == {("volume_option", "volumes.v.driver_opts")}

    @pytest.mark.parametrize(
        ("section", "value", "expected"),
        [
            ("configs", {"name": "p_c", "content": "x", "driver": "d"}, {"unknown_option"}),
            ("secrets", {"name": "p_s"}, {"unknown_option"}),
            ("configs", {"name": "p_c", "content": "x", "x-a": 1}, set()),
        ],
    )
    def test_top_level_configs_and_secrets_need_content_or_environment(
        self, section: str, value: dict[str, Any], expected: Any
    ) -> None:
        assert options(top_doc(section, "c", value)) == expected


class TestScanDeploy:
    @pytest.mark.parametrize(
        ("deploy", "expected"),
        [
            (
                {
                    "mode": "replicated",
                    "replicas": 2,
                    "restart_policy": {
                        "condition": "on-failure",
                        "delay": "5s",
                        "max_attempts": 3,
                        "window": "10s",
                    },
                    "placement": {},
                },
                set(),
            ),
            ({"resources": {"limits": {"cpus": 1, "memory": "1", "pids": 5}}}, set()),
            ({"mode": "global"}, {"unknown_option"}),
            ({"placement": {"constraints": ["node.role==manager"]}}, {"unknown_option"}),
            ({"update_config": {}}, {"unknown_option"}),
            ({"labels": {"a": "b"}}, {"unknown_option"}),
            ({"endpoint_mode": "dnsrr"}, {"unknown_option"}),
            ({"resources": {"limits": {"devices": []}}}, {"unknown_option"}),
            ({"resources": {"reservations": {"generic_resources": []}}}, {"unknown_option"}),
            ({"restart_policy": {"x": 1}}, {"unknown_option"}),
            ({"resources": {"x": 1}}, {"unknown_option"}),
            (
                {"resources": {"reservations": {"devices": [{"capabilities": ["gpu"]}]}}},
                {"devices"},
            ),
        ],
    )
    def test_deploy_keys_outside_the_plain_subset_are_refused(
        self, deploy: dict[str, Any], expected: Any
    ) -> None:
        assert options(web_doc({"deploy": deploy})) == expected

    def test_a_device_reservation_is_found_at_its_path(self) -> None:
        deploy = {"resources": {"reservations": {"devices": [{"capabilities": ["gpu"]}]}}}

        assert found(web_doc({"deploy": deploy})) == {
            ("devices", "services.web.deploy.resources.reservations.devices")
        }


class TestScanFileEntriesHooksAndBlkio:
    def test_service_config_and_secret_entries_within_the_allowed_keys_pass(self) -> None:
        doc = web_doc(
            {
                "configs": [
                    {"source": "motd", "target": "/m", "uid": "1", "gid": "1", "mode": "0444"}
                ],
                "secrets": [{"source": "tok"}],
            }
        )

        assert found(doc) == set()

    def test_a_service_config_entry_with_an_unknown_key_is_unknown(self) -> None:
        assert options(web_doc({"configs": [{"source": "motd", "x": 1}]})) == {"unknown_option"}

    def test_plain_lifecycle_hooks_pass(self) -> None:
        hook = {"command": ["x"], "user": "u", "working_dir": "/", "environment": {"A": "b"}}

        assert found(web_doc({"post_start": [hook], "pre_stop": [{"command": ["y"]}]})) == set()

    def test_a_privileged_hook_is_found_at_the_hook(self) -> None:
        doc = web_doc({"post_start": [{"command": ["x"], "privileged": True}]})

        assert found(doc) == {("privileged", "services.web.post_start[0].privileged")}

    def test_a_hook_with_an_unknown_key_is_unknown(self) -> None:
        assert options(web_doc({"pre_stop": [{"command": ["x"], "tty": True}]})) == {
            "unknown_option"
        }

    def test_blkio_weight_passes(self) -> None:
        assert found(web_doc({"blkio_config": {"weight": 300}})) == set()

    @pytest.mark.parametrize(
        "key",
        [
            "weight_device",
            "device_read_bps",
            "device_write_bps",
            "device_read_iops",
            "device_write_iops",
        ],
    )
    def test_blkio_device_settings_are_refused(self, key: str) -> None:
        doc = web_doc({"blkio_config": {key: [{"path": "/dev/sda", "rate": 1}]}})

        assert found(doc) == {("blkio_devices", f"services.web.blkio_config.{key}")}

    def test_an_unknown_blkio_key_is_unknown(self) -> None:
        assert options(web_doc({"blkio_config": {"x": 1}})) == {"unknown_option"}


class TestReservedLabels:
    @pytest.mark.parametrize(
        "key",
        [
            RESERVED_PROJECT_LABEL,
            RESERVED_PROJECT_LABEL.upper(),
            f" {RESERVED_PROJECT_LABEL} ",
            RESERVED_STACK_LABEL,
            "Com.Docker.Stack.Namespace\t",
        ],
    )
    def test_a_membership_label_on_a_service_is_found_in_any_spelling(self, key: str) -> None:
        result = sr.reserved_labels(model(web_doc({"labels": {key: "x"}})))

        assert result == (StackFinding("reserved_label", "services.web.labels"),)

    @pytest.mark.parametrize(
        ("doc", "path"),
        [
            (web_doc({"labels": [f"{RESERVED_PROJECT_LABEL}=x"]}), "services.web.labels"),
            (
                web_doc({"deploy": {"labels": {RESERVED_STACK_LABEL: "x"}}}),
                "services.web.deploy.labels",
            ),
            (
                top_doc("networks", "n", {"name": "p_n", "labels": {RESERVED_PROJECT_LABEL: "x"}}),
                "networks.n.labels",
            ),
            (
                top_doc("volumes", "v", {"name": "p_v", "labels": {RESERVED_PROJECT_LABEL: "x"}}),
                "volumes.v.labels",
            ),
        ],
        ids=["list form", "deploy labels", "network labels", "volume labels"],
    )
    def test_a_membership_label_is_found_wherever_labels_are_accepted(
        self, doc: dict[str, Any], path: str
    ) -> None:
        assert sr.reserved_labels(model(doc)) == (StackFinding("reserved_label", path),)

    def test_similar_label_names_and_the_clean_model_are_not_found(self) -> None:
        doc = web_doc(
            {"labels": {f"{RESERVED_PROJECT_LABEL}.extra": "x", "com.docker.compose.service": "x"}}
        )

        assert sr.reserved_labels(model(doc)) == ()
        assert sr.reserved_labels(model(BASE)) == ()


def lim(doc: dict[str, Any]) -> dict[str, str]:
    return sr.limits(model(doc))


def many(section: str, count: int, **entry: Any) -> dict[str, Any]:
    doc = copy.deepcopy(BASE)
    doc[section] = {
        f"k{i}": ({"image": "x"} if section == "services" else {"name": f"p_k{i}", **entry})
        for i in range(count)
    }
    return doc


class TestLimits:
    def test_the_model_is_within_every_limit(self) -> None:
        assert lim(BASE) == {}

    @pytest.mark.parametrize(
        ("section", "cap"),
        [
            ("services", sr.MAX_SERVICES),
            ("networks", sr.MAX_NETWORKS),
            ("volumes", sr.MAX_VOLUMES),
        ],
    )
    def test_one_object_over_the_cap_is_reported_and_the_cap_itself_is_not(
        self, section: str, cap: int
    ) -> None:
        assert lim(many(section, cap)) == {}
        assert list(lim(many(section, cap + 1))) == [section]

    def test_external_networks_and_volumes_do_not_count(self) -> None:
        assert lim(many("networks", 60, external=True)) == {}
        assert lim(many("volumes", 60, external=True)) == {}

    @pytest.mark.parametrize(
        ("changes", "paths"),
        [
            ({"scale": sr.MAX_REPLICAS + 1}, ["services.web.scale"]),
            ({"scale": sr.MAX_REPLICAS}, []),
            ({"scale": "25"}, ["services.web.scale"]),
            ({"scale": True}, []),
            ({"deploy": {"replicas": sr.MAX_REPLICAS + 1}}, ["services.web.deploy.replicas"]),
            ({"deploy": {"replicas": sr.MAX_REPLICAS}}, []),
            ({"container_name": "a" * 64}, ["services.web.container_name"]),
            ({"container_name": "A" * 64}, ["services.web.container_name"]),
            ({"container_name": "web-1"}, []),
        ],
    )
    def test_replica_counts_and_container_ids_are_limited_per_service(
        self, changes: dict[str, Any], paths: list[Any]
    ) -> None:
        assert list(lim(web_doc(changes))) == paths


class TestResolvedRefusals:
    def test_the_realistic_model_has_none(self) -> None:
        assert sr.resolved_refusals(model(BASE)) == {}

    @pytest.mark.parametrize(
        ("doc", "expected"),
        [
            ({**copy.deepcopy(BASE), "include": []}, {"include"}),
            ({**copy.deepcopy(BASE), "x-a": 1}, set()),
            (web_doc({"env_file": ["a"]}), {"services.web.env_file[0]"}),
            (web_doc({"label_file": ["/etc/passwd"]}), {"services.web.label_file[0]"}),
            (web_doc({"label_file": "/etc/passwd"}), {"services.web.label_file"}),
            (web_doc({"build": {"context": "."}}), {"services.web.build"}),
            (web_doc({"provider": {"type": "x"}}), {"services.web.provider"}),
            (web_doc({"extends": "x"}), set()),
            (web_doc({"extends": {"service": "x", "file": "f"}}), {"services.web.extends.file"}),
            (web_doc({"pull_policy": "build"}), {"services.web.pull_policy"}),
            (top_doc("configs", "c", {"name": "p_c", "file": "/etc/passwd"}), {"configs.c.file"}),
            (top_doc("secrets", "s", {"name": "p_s", "file": "/k"}), {"secrets.s.file"}),
        ],
        ids=[
            "top-level include",
            "top-level extension",
            "env_file",
            "label_file list",
            "label_file string",
            "build",
            "provider",
            "extends in the same file",
            "extends with a file",
            "build pull policy",
            "config file",
            "secret file",
        ],
    )
    def test_a_resolved_model_keeps_the_file_reading_refusals(
        self, doc: dict[str, Any], expected: Any
    ) -> None:
        assert set(sr.resolved_refusals(model(doc))) == expected

    def test_a_file_path_that_is_a_companion_of_the_project_directory_is_kept(self) -> None:
        companion = "/stacks/t/a.env"
        doc = web_doc({"label_file": [companion]})

        assert sr.resolved_refusals(model(doc), companions=[companion]) == {}
        assert set(sr.resolved_refusals(model(doc), companions=["/stacks/t/b.env"])) == {
            "services.web.label_file[0]"
        }

    @pytest.mark.parametrize("section", ["configs", "secrets"])
    def test_a_file_source_that_survived_resolution_is_refused_even_for_a_companion(
        self, section: str
    ) -> None:
        companion = "/stacks/t/a.txt"
        doc = top_doc(section, "c", {"name": "p_c", "file": companion})

        found = sr.resolved_refusals(model(doc), companions=[companion])

        assert set(found) == {f"{section}.c.file"}

    @pytest.mark.parametrize("section", ["configs", "secrets"])
    def test_a_generated_source_is_kept_only_for_its_own_entry(self, section: str) -> None:
        name = "DOCKMIN_SOURCE_1"
        doc = top_doc(section, "c", {"name": "p_c", "environment": name})

        assert sr.resolved_refusals(model(doc), generated={(section, "c"): name}) == {}
        assert set(sr.resolved_refusals(model(doc), generated={(section, "other"): name})) == {
            f"{section}.c.environment"
        }

    @pytest.mark.parametrize("section", ["configs", "secrets"])
    @pytest.mark.parametrize(("variable", "message"), INTERPOLATED_SOURCES)
    def test_a_source_that_became_a_disclosing_name_after_interpolation_is_refused(
        self, section: str, variable: str, message: str
    ) -> None:
        doc = top_doc(section, "c", {"name": "p_c", "environment": variable})

        assert sr.resolved_refusals(model(doc)) == {f"{section}.c.environment": message}

    @pytest.mark.parametrize("section", ["configs", "secrets"])
    def test_a_source_outside_the_disclosing_set_is_kept(self, section: str) -> None:
        doc = top_doc(section, "c", {"name": "p_c", "environment": "COMPOSE_PROJECT_NAME"})

        assert sr.resolved_refusals(model(doc)) == {}


def backstopped(doc: dict[str, Any], covered: Sequence[StackFinding] = ()) -> set[tuple[str, str]]:
    result = sr.backstop(model(doc), default_runtime=None, covered=covered)
    return {(finding.option, finding.path) for finding in result}


def hostnet_doc() -> dict[str, Any]:
    doc = top_doc("networks", "hostnet", {"name": "host", "external": True})
    doc["services"]["web"]["networks"] = {"hostnet": None}
    return doc


BACKSTOP_CASES = {
    "privileged": (web_doc({"privileged": True}), "privileged"),
    "cap_add": (web_doc({"cap_add": ["X"]}), "cap_add"),
    "network_mode service": (web_doc({"network_mode": "service:db"}), "network_container"),
    "privileged hook": (
        web_doc({"post_start": [{"command": ["x"], "privileged": True}]}),
        "privileged",
    ),
    "bind mount": (with_mounts({"type": "bind", "source": "/etc", "target": "/e"}), "bind_mount"),
    "npipe mount": (with_mounts({"type": "npipe", "source": "x", "target": "/e"}), "mount_type"),
    "unmapped key": (web_doc({"use_api_socket": True}), "unknown_option"),
    "build": (web_doc({"build": "."}), "unknown_option"),
    "host network by name": (hostnet_doc(), "network_host"),
    "generic resources": (
        web_doc({"deploy": {"resources": {"reservations": {"generic_resources": []}}}}),
        "unknown_option",
    ),
    "blkio device": (
        web_doc({"blkio_config": {"device_read_bps": [{"path": "/dev/sda", "rate": 1}]}}),
        "blkio_devices",
    ),
    "runtime": (web_doc({"runtime": "nvidia"}), "runtime"),
    "log driver": (web_doc({"logging": {"driver": "syslog"}}), "log_driver"),
    "endpoint driver options": (with_endpoint(driver_opts={"a": "b"}), "endpoint_driver_opts"),
    "oom_score_adj": (web_doc({"oom_score_adj": -1}), "oom_score_adj"),
}


class TestBackstop:
    @pytest.mark.parametrize(("doc", "option"), BACKSTOP_CASES.values(), ids=BACKSTOP_CASES.keys())
    def test_it_finds_a_restricted_use_the_scan_was_not_told_about(
        self, doc: dict[str, Any], option: str
    ) -> None:
        result = backstopped(doc)

        assert result == {(option, "services.web")}

    def test_it_returns_the_engine_path_of_the_use(self) -> None:
        result = sr.backstop(model(web_doc({"privileged": True})), default_runtime=None, covered=())

        assert result == (StackFinding("privileged", "services.web", "HostConfig.Privileged"),)

    def test_it_returns_nothing_for_the_realistic_model(self) -> None:
        assert backstopped(BASE) == set()

    def test_it_does_not_repeat_findings_the_scan_already_made(self) -> None:
        doc = web_doc({"privileged": True})
        covered = scanned(doc).findings

        assert backstopped(doc, covered=covered) == set()

    def test_covered_findings_of_another_service_do_not_hide_a_use(self) -> None:
        doc = web_doc({"privileged": True})
        covered = (StackFinding("privileged", "services.once.privileged"),)

        assert backstopped(doc, covered=covered) == {("privileged", "services.web")}

    @pytest.mark.parametrize(
        ("key", "value", "option"),
        [(key, *pair) for key, pair in RESTRICTED_KEYS.items()],
        ids=RESTRICTED_KEYS,
    )
    def test_every_scan_finding_is_also_found_by_the_mapped_engine_body(
        self, key: str, value: Any, option: str
    ) -> None:
        doc = web_doc({key: value})

        result = {
            finding.option for finding in find_restricted(engine_of(doc), default_runtime=None)
        }

        assert option in result

    def test_the_backstop_respects_the_default_runtime(self) -> None:
        doc = web_doc({"runtime": DEFAULT_RUNTIME})

        result = sr.backstop(model(doc), default_runtime=DEFAULT_RUNTIME)

        assert result == ()


def engine_of(doc: dict[str, Any], service: str = "web") -> dict[str, Any]:
    return sr.engine_body(doc["services"][service], model(doc))


class TestEngineBody:
    def test_the_realistic_service_maps_to_a_body_with_no_restricted_use(self) -> None:
        body = engine_of(BASE)

        assert find_restricted(body) == []
        assert body["HostConfig"]["Mounts"][0] == {
            "Type": "volume",
            "Source": f"{PROJECT}_data",
            "Target": "/var/lib/registry",
            "VolumeOptions": {},
        }
        assert body["HostConfig"]["Memory"] == "134217728"
        assert body["NetworkingConfig"]["EndpointsConfig"] == {f"{PROJECT}_appnet": {}}

    @pytest.mark.parametrize(
        ("changes", "path", "value"),
        [
            ({"privileged": True}, ("HostConfig", "Privileged"), True),
            ({"image": "nginx:1"}, ("Image",), "nginx:1"),
            ({"environment": {"A": "b"}}, ("Env",), {"A": "b"}),
            ({"read_only": True}, ("HostConfig", "ReadonlyRootfs"), True),
            ({"logging": {"driver": "local"}}, ("HostConfig", "LogConfig", "Type"), "local"),
            (
                {"logging": {"options": {"max-size": "1m"}}},
                ("HostConfig", "LogConfig", "Config"),
                {"max-size": "1m"},
            ),
            ({"network_mode": "service:db"}, ("HostConfig", "NetworkMode"), "container:db"),
            ({"network_mode": "bridge"}, ("HostConfig", "NetworkMode"), "bridge"),
            ({"blkio_config": {"weight": 5}}, ("HostConfig", "BlkioWeight"), 5),
            (
                {"blkio_config": {"device_read_bps": [1]}},
                ("HostConfig", "BlkioDeviceReadBps"),
                [1],
            ),
            (
                {"deploy": {"resources": {"limits": {"memory": "1", "pids": 3}}}},
                ("HostConfig", "PidsLimit"),
                3,
            ),
            (
                {"deploy": {"resources": {"reservations": {"devices": [{}]}}}},
                ("HostConfig", "DeviceRequests"),
                [{}],
            ),
            (
                {"post_start": [{"command": ["x"], "privileged": True}]},
                ("HostConfig", "Privileged"),
                True,
            ),
        ],
    )
    def test_a_compose_key_lands_on_its_engine_field(
        self, changes: dict[str, Any], path: tuple[Any, ...], value: Any
    ) -> None:
        body = sr.engine_body(web_doc(changes)["services"]["web"])
        for part in path:
            body = body[part]

        assert body == value

    @pytest.mark.parametrize(
        ("changes", "key"),
        [
            ({"use_api_socket": True}, "compose.use_api_socket"),
            ({"deploy": {"mode": "global"}}, "compose.deploy.mode"),
            ({"deploy": {"update_config": {}}}, "compose.deploy.update_config"),
            (
                {"deploy": {"resources": {"limits": {"devices": []}}}},
                "compose.deploy.resources.limits.devices",
            ),
            (
                {"deploy": {"resources": {"reservations": {"generic_resources": []}}}},
                "compose.deploy.resources.reservations.generic_resources",
            ),
            ({"blkio_config": {"x": 1}}, "compose.blkio_config.x"),
            ({"post_start": [{"command": ["x"], "tty": True}]}, "compose.post_start.tty"),
            ({"configs": [{"source": "m", "x": 1}]}, "compose.configs.x"),
        ],
    )
    def test_a_key_with_no_engine_field_is_carried_under_a_compose_prefix(
        self, changes: dict[str, Any], key: str
    ) -> None:
        body = sr.engine_body(web_doc(changes)["services"]["web"])

        assert key in body

    def test_keys_that_do_not_reach_the_engine_are_dropped(self) -> None:
        service = {"profiles": ["a"], "depends_on": ["db"], "x-note": 1, "pull_policy": "never"}

        assert sr.engine_body(service) == {}

    def test_a_non_text_key_is_carried_so_the_scan_sees_it(self) -> None:
        assert sr.engine_body({1: "x"}) == {"compose.<key>": "x"}  # type: ignore[dict-item]

    def test_links_and_external_links_are_merged(self) -> None:
        body = sr.engine_body({"links": ["a"], "external_links": ["b"]})

        assert body["HostConfig"]["Links"] == ["a", "b"]

    def test_a_named_volume_source_is_resolved_to_the_object_name(self) -> None:
        doc = web_doc({"volumes": [{"type": "volume", "source": "data", "target": "/a"}]})

        assert engine_of(doc)["HostConfig"]["Mounts"][0]["Source"] == f"{PROJECT}_data"

    def test_a_bind_source_stays_a_host_path(self) -> None:
        doc = web_doc({"volumes": [{"type": "bind", "source": "data", "target": "/a"}]})

        assert engine_of(doc)["HostConfig"]["Mounts"][0]["Source"] == "data"

    def test_endpoint_settings_use_engine_field_names(self) -> None:
        doc = with_endpoint(aliases=["a"], ipv4_address="10.0.0.2", gw_priority=1)

        endpoint = engine_of(doc)["NetworkingConfig"]["EndpointsConfig"][f"{PROJECT}_appnet"]

        assert endpoint == {
            "Aliases": ["a"],
            "IPAMConfig": {"IPv4Address": "10.0.0.2"},
            "GwPriority": 1,
        }


ALL_OPTION_IDS = (*OPTION_IDS, *sr.STACK_OPTION_IDS)


class TestOptionLabels:
    @pytest.mark.parametrize("option", ALL_OPTION_IDS)
    def test_no_option_id_is_shown_as_a_raw_id(self, option: str) -> None:
        label = sr.stack_option_label(option)

        assert label != option
        assert "_" not in label

    @pytest.mark.parametrize("option", OPTION_IDS)
    def test_container_option_labels_are_unchanged(self, option: str) -> None:
        assert sr.stack_option_label(option) == option_label(option)

    @pytest.mark.parametrize(
        "option", [option for option in sr.STACK_OPTION_IDS if option != "reserved_label"]
    )
    def test_a_stack_option_has_its_own_phrase_and_message(self, option: str) -> None:
        finding = StackFinding(option, "networks.n")

        assert finding.label == sr.stack_option_label(option)
        assert finding.message == f"Only administrators may use {finding.label}."

    def test_a_reserved_label_message_says_the_product_sets_them(self) -> None:
        message = StackFinding("reserved_label", "services.web.labels").message

        assert message == "The stack membership labels are set by the product."

    def test_an_unknown_option_message_does_not_name_a_phrase(self) -> None:
        message = StackFinding("unknown_option", "services.web.x").message

        assert message == "This option is not recognised, so only administrators may use it."

    def test_a_container_option_message_names_the_phrase(self) -> None:
        message = StackFinding("bind_mount", "services.web.volumes[0].type").message

        assert message == "Only administrators may use host bind mounts."

    def test_refused_options_list_container_ids_first_then_stack_ids_without_repeats(self) -> None:
        findings = [
            StackFinding("network_driver", "a"),
            StackFinding("privileged", "b"),
            StackFinding("reserved_label", "c"),
            StackFinding("privileged", "d"),
            StackFinding("not_an_id", "e"),
        ]

        assert sr.refused_stack_options(findings) == (
            "privileged",
            "network_driver",
            "reserved_label",
        )

    @pytest.mark.parametrize(
        ("doc", "option"),
        [
            (top_doc("networks", "n", {"name": "p", "driver": "overlay"}), "network_driver"),
            (
                top_doc("networks", "n", {"name": "p", "ipam": {"driver": "x"}}),
                "network_addressing",
            ),
            (top_doc("networks", "n", {"name": "p", "driver_opts": {"a": "b"}}), "network_option"),
            (top_doc("volumes", "v", {"name": "p", "driver": "x"}), "volume_option"),
        ],
    )
    def test_every_stack_specific_finding_the_scan_makes_has_a_phrase(
        self, doc: dict[str, Any], option: str
    ) -> None:
        findings = scanned(doc).findings

        assert refused_labels(findings) == [sr.stack_option_label(option)]


def refused_labels(findings: Sequence[StackFinding]) -> list[str]:
    return [sr.stack_option_label(option) for option in sr.refused_stack_options(findings)]


def secret_doc() -> dict[str, Any]:
    doc = web_doc(
        {
            "environment": {
                "TOKEN": SECRET,
                "URL": f"http://u:{SECRET}@h/x",
                "PLAIN": "kept",
                "N": 1234,
            }
        }
    )
    doc["configs"]["motd"]["content"] = f"key={SECRET}\n"
    return doc


class TestMasking:
    def test_every_occurrence_of_a_secret_is_replaced(self) -> None:
        result = sr.masked(model(secret_doc()), [SECRET])

        environment = result["services"]["web"]["environment"]
        assert environment["TOKEN"] == REDACTED
        assert environment["URL"] == f"http://u:{REDACTED}@h/x"
        assert environment["PLAIN"] == "kept"
        assert result["configs"]["motd"]["content"] == f"key={REDACTED}\n"
        assert SECRET not in json.dumps(result)

    def test_a_number_equal_to_a_secret_is_replaced(self) -> None:
        result = sr.masked(model(secret_doc()), ["1234"])

        assert result["services"]["web"]["environment"]["N"] == REDACTED

    def test_the_dollar_escaped_form_of_a_secret_is_replaced(self) -> None:
        doc = web_doc({"environment": {"DOLLAR": "pa$$word"}})

        result = sr.masked(model(doc), ["pa$word"])

        assert result["services"]["web"]["environment"]["DOLLAR"] == REDACTED

    def test_an_empty_secret_masks_nothing(self) -> None:
        result = sr.masked(model(secret_doc()), [""])

        assert result == secret_doc()

    def test_a_secret_in_a_key_is_replaced(self) -> None:
        doc = web_doc({"labels": {f"k-{SECRET}": "v"}})

        result = sr.masked(model(doc), [SECRET])

        assert result["services"]["web"]["labels"] == {f"k-{REDACTED}": "v"}

    def test_the_input_document_is_not_changed(self) -> None:
        doc = secret_doc()

        sr.masked(model(doc), [SECRET])

        assert doc == secret_doc()

    def test_a_longer_secret_wins_over_one_it_contains(self) -> None:
        doc = web_doc({"environment": {"A": "abcdef"}})

        result = sr.masked(model(doc), ["abc", "abcdef"])

        assert result["services"]["web"]["environment"]["A"] == REDACTED


def digest_with(token: str, secrets: list[str], **changes: Any) -> str:
    doc = web_doc({"environment": {"TOKEN": token}, **changes})
    return sr.resolved_digest(model(doc), secrets)


class TestResolvedDigest:
    def test_a_different_secret_value_gives_the_same_digest(self) -> None:
        first = digest_with(SECRET, [SECRET])
        second = digest_with(OTHER_SECRET, [OTHER_SECRET])

        assert first == second
        assert len(first) == 64

    def test_without_masking_the_secret_value_changes_the_digest(self) -> None:
        assert digest_with(SECRET, []) != digest_with(OTHER_SECRET, [])

    def test_a_change_to_a_non_secret_setting_changes_the_digest(self) -> None:
        first = digest_with(SECRET, [SECRET])
        second = digest_with(SECRET, [SECRET], image="other:2")

        assert first != second

    def test_key_order_does_not_change_the_digest(self) -> None:
        forward = web_doc({"environment": {"A": "1", "B": "2"}})
        reverse = web_doc({"environment": {"B": "2", "A": "1"}})

        assert sr.resolved_digest(model(forward), []) == sr.resolved_digest(model(reverse), [])


SWARM_NAMESPACE = "Shop"
STANDARD_REPLICA_CEILING = 100
ADMINISTRATOR_REPLICA_CEILING = 1000
MTU_OPTION = "com.docker.network.driver.mtu"
SWARM_BASE: dict[str, Any] = {
    "name": "shop",
    "networks": {"default": {"name": "shop_default", "ipam": {}}},
    "services": {
        "web": {
            "command": None,
            "entrypoint": None,
            "image": "registry:2",
            "networks": {"default": None},
        }
    },
}


def swarm_doc(**service: Any) -> dict[str, Any]:
    doc = copy.deepcopy(SWARM_BASE)
    doc["services"]["web"].update(service)
    return doc


def swarm_section(section: str, entries: dict[str, Any], **service: Any) -> dict[str, Any]:
    """A swarm model declaring `entries` and referencing every one from web."""
    doc = swarm_doc(**service)
    doc[section] = entries
    if section == "networks":
        doc["services"]["web"]["networks"] = dict.fromkeys(entries)
    elif section == "volumes":
        doc["services"]["web"]["volumes"] = [
            {"type": "volume", "source": key, "target": f"/{key}"} for key in entries
        ]
    else:
        doc["services"]["web"][section] = [{"source": key} for key in entries]
    return doc


def swarm_found(doc: dict[str, Any]) -> set[tuple[str, str]]:
    result = sr.scan(model(doc), default_runtime=None, kind="swarm")
    return {(f.option, f.path) for f in result.findings}


def swarm_uses(doc: dict[str, Any]) -> set[tuple[str, str, str]]:
    result = sr.scan(model(doc), default_runtime=None, kind="swarm")
    return {(u.kind, u.name, u.path) for u in result.external}


class TestSwarmScan:
    def test_a_file_with_nothing_a_standard_user_may_not_do_is_clean(self) -> None:
        doc = swarm_section(
            "networks",
            {
                "front": {
                    "name": "shop_front",
                    "driver": "overlay",
                    "driver_opts": {"encrypted": "true", MTU_OPTION: "1400"},
                    "labels": {"a": "b"},
                    "ipam": {},
                }
            },
            environment={"A": "1"},
            labels={"x": "y"},
            ports=[{"mode": "host", "target": 80, "published": "8080", "protocol": "tcp"}],
            healthcheck={"test": ["CMD", "true"], "interval": "30s"},
            user="1000",
            working_dir="/w",
            read_only=True,
            cap_drop=["ALL"],
            logging={"driver": "json-file", "options": {"max-size": "1m"}},
            security_opt=["no-new-privileges:true"],
            volumes=[{"type": "tmpfs", "target": "/t"}, {"type": "volume", "target": "/anon"}],
            deploy={
                "mode": "replicated",
                "replicas": 3,
                "labels": {"d": "e"},
                "endpoint_mode": "vip",
                "placement": {"constraints": ["node.role == worker"], "max_replicas_per_node": 2},
                "resources": {"limits": {"cpus": 0.5, "memory": "134217728"}},
                "restart_policy": {"condition": "on-failure"},
                "update_config": {"parallelism": 2, "order": "start-first"},
                "rollback_config": {"failure_action": "pause"},
            },
        )

        assert swarm_found(doc) == set()

    @pytest.mark.parametrize(
        ("changes", "expected"),
        [
            pytest.param(
                {"cap_add": ["NET_ADMIN"]}, ("cap_add", "services.web.cap_add"), id="cap-add"
            ),
            pytest.param(
                {"sysctls": {"net.core.somaxconn": "1"}},
                ("sysctls", "services.web.sysctls"),
                id="sysctls",
            ),
            pytest.param(
                {"security_opt": ["seccomp=unconfined"]},
                ("security_opt", "services.web.security_opt[0]"),
                id="seccomp-unconfined",
            ),
            pytest.param(
                {"security_opt": ["apparmor=unconfined"]},
                ("security_opt", "services.web.security_opt[0]"),
                id="apparmor-unconfined",
            ),
            pytest.param(
                {"logging": {"driver": "syslog"}},
                ("log_driver", "services.web.logging.driver"),
                id="log-driver",
            ),
            pytest.param(
                {"logging": {"driver": "json-file", "options": {"labels": "x"}}},
                ("log_driver", "services.web.logging.options.labels"),
                id="log-option",
            ),
            pytest.param(
                {"isolation": "process"}, ("isolation", "services.web.isolation"), id="isolation"
            ),
            pytest.param(
                {"oom_score_adj": -100},
                ("oom_score_adj", "services.web.oom_score_adj"),
                id="negative-oom-score",
            ),
            pytest.param(
                {"volumes": [{"type": "bind", "source": "/etc", "target": "/e"}]},
                ("bind_mount", "services.web.volumes[0].type"),
                id="bind-mount",
            ),
            pytest.param(
                {"networks": {"default": {"driver_opts": {"a": "b"}}}},
                ("endpoint_driver_opts", "services.web.networks.default.driver_opts"),
                id="endpoint-driver-options",
            ),
            pytest.param(
                {
                    "deploy": {
                        "resources": {
                            "reservations": {
                                "generic_resources": [
                                    {"discrete_resource_spec": {"kind": "gpu", "value": 1}}
                                ]
                            }
                        }
                    }
                },
                ("devices", "services.web.deploy.resources.reservations.generic_resources"),
                id="generic-resources",
            ),
        ],
    )
    def test_a_restricted_service_option_is_found_at_its_compose_path(
        self, changes: dict[str, Any], expected: tuple[Any, ...]
    ) -> None:
        assert swarm_found(swarm_doc(**changes)) == {expected}

    @pytest.mark.parametrize("value", [0, 100, 1000])
    def test_a_non_negative_oom_score_adjustment_is_allowed(self, value: int) -> None:
        assert swarm_found(swarm_doc(oom_score_adj=value)) == set()

    @pytest.mark.parametrize(
        ("section", "entries", "expected"),
        [
            pytest.param(
                "networks",
                {"n": {"name": "shop_n", "ipam": {"config": [{"subnet": "10.0.0.0/24"}]}}},
                ("network_addressing", "networks.n.ipam"),
                id="ipam",
            ),
            pytest.param(
                "networks",
                {"n": {"name": "shop_n", "driver_opts": {"foo": "bar"}}},
                ("network_option", "networks.n.driver_opts.foo"),
                id="unknown-network-option",
            ),
            pytest.param(
                "networks",
                {"n": {"name": "shop_n", "driver_opts": {"mtu": "1400"}}},
                ("network_option", "networks.n.driver_opts.mtu"),
                id="short-mtu-spelling-is-not-the-option",
            ),
            pytest.param(
                "volumes",
                {"v": {"name": "shop_v", "driver": "local", "driver_opts": {"type": "nfs"}}},
                ("volume_option", "volumes.v.driver_opts"),
                id="volume-driver-options",
            ),
            pytest.param(
                "volumes",
                {"v": {"name": "shop_v", "driver": "nfs"}},
                ("volume_option", "volumes.v.driver"),
                id="volume-driver",
            ),
        ],
    )
    def test_a_restricted_top_level_option_is_found_at_its_compose_path(
        self, section: str, entries: dict[str, Any], expected: tuple[Any, ...]
    ) -> None:
        assert swarm_found(swarm_section(section, entries)) == {expected}

    @pytest.mark.parametrize("option", ["encrypted", MTU_OPTION])
    def test_the_overlay_options_of_the_networks_page_are_allowed_on_a_stack_network(
        self, option: str
    ) -> None:
        doc = swarm_section("networks", {"n": {"name": "shop_n", "driver_opts": {option: "x"}}})

        assert swarm_found(doc) == set()

    def test_an_overlay_network_may_name_its_driver_and_any_other_driver_is_a_finding(self) -> None:
        allowed = swarm_section("networks", {"n": {"name": "shop_n", "driver": "overlay"}})

        assert swarm_found(allowed) == set()

    def test_the_external_objects_a_stack_uses_are_returned_for_the_reach_checks(self) -> None:
        doc = swarm_doc(
            networks={"n": None},
            configs=[{"source": "c"}],
            secrets=[{"source": "s"}],
            volumes=[{"type": "volume", "source": "v", "target": "/v"}],
        )
        doc["networks"] = {"n": {"name": "outside", "external": True}}
        doc["configs"] = {"c": {"name": "extcfg", "external": True}}
        doc["secrets"] = {"s": {"name": "extsec", "external": True}}
        doc["volumes"] = {"v": {"name": "extvol", "external": True}}

        uses = swarm_uses(doc)

        assert uses == {
            ("network", "outside", "services.web.networks.n"),
            ("config", "extcfg", "services.web.configs[0]"),
            ("secret", "extsec", "services.web.secrets[0]"),
            ("volume", "extvol", "services.web.volumes[0].source"),
        }
        assert swarm_found(doc) == set()

    def test_the_host_network_is_a_finding_as_well_as_a_use(self) -> None:
        doc = swarm_section("networks", {"n": {"name": "host", "external": True}})

        assert swarm_found(doc) == {("network_host", "services.web.networks.n")}
        assert swarm_uses(doc) == {("network", "host", "services.web.networks.n")}

    def test_project_networks_and_volumes_are_listed_by_their_resolved_names(self) -> None:
        doc = swarm_section("volumes", {"v": {"name": "shop_v"}})

        result = sr.scan(model(doc), default_runtime=None, kind="swarm")

        assert result.project_networks == ("shop_default",)
        assert result.project_volumes == ("shop_v",)

    def test_the_compose_scan_of_the_same_model_still_refuses_what_a_stack_may_do(self) -> None:
        doc = swarm_section("configs", {"c": {"name": "shop_c", "content": "x"}})

        compose = {(f.option, f.path) for f in sr.scan(model(doc), default_runtime=None).findings}
        swarm = swarm_found(doc)

        assert swarm == set()
        assert compose >= swarm


class TestSwarmReservedLabels:
    @pytest.mark.parametrize(
        ("doc", "path"),
        [
            pytest.param(
                swarm_doc(labels={RESERVED_STACK_LABEL: "x"}), "services.web.labels", id="service"
            ),
            pytest.param(
                swarm_doc(deploy={"labels": {RESERVED_PROJECT_LABEL: "x"}}),
                "services.web.deploy.labels",
                id="deploy",
            ),
            pytest.param(
                swarm_section(
                    "networks", {"n": {"name": "shop_n", "labels": {RESERVED_STACK_LABEL: "x"}}}
                ),
                "networks.n.labels",
                id="network",
            ),
            pytest.param(
                swarm_section(
                    "volumes", {"v": {"name": "shop_v", "labels": {RESERVED_STACK_LABEL: "x"}}}
                ),
                "volumes.v.labels",
                id="volume",
            ),
            pytest.param(
                swarm_section(
                    "configs",
                    {
                        "c": {
                            "name": "shop_c",
                            "content": "x",
                            "labels": {RESERVED_STACK_LABEL: "x"},
                        }
                    },
                ),
                "configs.c.labels",
                id="config",
            ),
            pytest.param(
                swarm_section(
                    "secrets",
                    {
                        "s": {
                            "name": "shop_s",
                            "environment": "V",
                            "labels": ["Com.Docker.Stack.Namespace=e"],
                        }
                    },
                ),
                "secrets.s.labels",
                id="secret-in-list-form-any-case",
            ),
        ],
    )
    def test_a_stack_label_is_found_on_every_object_a_swarm_stack_labels(
        self, doc: dict[str, Any], path: str
    ) -> None:
        found = sr.reserved_labels(model(doc), kind="swarm")

        assert [(f.option, f.path) for f in found] == [("reserved_label", path)]

    @pytest.mark.parametrize("section", ["configs", "secrets"])
    def test_a_compose_stack_does_not_check_config_or_secret_labels(self, section: str) -> None:
        entry = {"name": "shop_x", "content": "x", "labels": {RESERVED_STACK_LABEL: "x"}}
        doc = swarm_section(section, {"x": entry})

        assert sr.reserved_labels(model(doc)) == ()
        assert len(sr.reserved_labels(model(doc), kind="swarm")) == 1

    def test_an_ordinary_label_is_not_reserved(self) -> None:
        doc = swarm_section(
            "configs", {"c": {"name": "shop_c", "content": "x", "labels": {"team": "web"}}}
        )

        assert sr.reserved_labels(model(doc), kind="swarm") == ()


class TestSwarmLimits:
    @pytest.mark.parametrize(
        ("replicas", "administrator", "limited"),
        [
            (STANDARD_REPLICA_CEILING, False, False),
            (STANDARD_REPLICA_CEILING + 1, False, True),
            (STANDARD_REPLICA_CEILING + 1, True, False),
            (ADMINISTRATOR_REPLICA_CEILING, True, False),
            (ADMINISTRATOR_REPLICA_CEILING + 1, True, True),
        ],
    )
    def test_the_replica_ceiling_depends_on_the_role(
        self, replicas: int, administrator: bool, limited: bool
    ) -> None:
        doc = swarm_doc(deploy={"replicas": replicas})

        found = sr.limits(model(doc), kind="swarm", administrator=administrator)

        assert bool(found) is limited
        assert set(found) <= {"services.web.deploy.replicas"}

    @pytest.mark.parametrize(
        ("section", "path"),
        [("configs", "configs"), ("secrets", "secrets"), ("networks", "networks")],
    )
    def test_more_than_fifty_configs_secrets_or_networks_are_over_the_limit(
        self, section: str, path: str
    ) -> None:
        entries = {f"o{i}": {"name": f"shop_o{i}", "content": "x"} for i in range(51)}
        doc = swarm_doc()
        doc[section] = entries

        assert path in sr.limits(model(doc), kind="swarm", administrator=True)

    def test_more_than_fifty_services_are_over_the_limit(self) -> None:
        doc = swarm_doc()
        doc["services"] = {f"s{i}": doc["services"]["web"] for i in range(51)}

        assert "services" in sr.limits(model(doc), kind="swarm")

    def test_a_compose_stack_keeps_its_own_replica_limit(self) -> None:
        over_compose = swarm_doc(deploy={"replicas": sr.MAX_REPLICAS + 1})

        assert list(sr.limits(model(over_compose))) == ["services.web.deploy.replicas"]
        assert sr.limits(model(over_compose), kind="swarm", administrator=False) == {}


class TestSwarmRefusals:
    def test_a_key_with_no_swarm_form_is_refused_at_its_path_for_everyone(self) -> None:
        found = sr.swarm_refusals(model(swarm_doc(container_name="x", depends_on={"db": {}})))

        assert set(found) == {"services.web.container_name", "services.web.depends_on"}

    def test_a_clean_model_has_no_refusal(self) -> None:
        assert sr.swarm_refusals(model(swarm_doc())) == {}

    def test_the_compose_refusals_are_included(self) -> None:
        doc = swarm_doc()
        doc["models"] = {}

        assert "models" in sr.swarm_refusals(model(doc))

    def test_a_key_the_model_does_not_know_is_refused_rather_than_ignored(self) -> None:
        found = sr.swarm_refusals(model(swarm_doc(deploy={"surprise": 1})))

        assert found == {
            "services.web.deploy.surprise": "This key is not supported for a Swarm stack."
        }

    def test_an_extension_key_is_ignored(self) -> None:
        assert sr.swarm_refusals(model(swarm_doc(**{"x-note": {"free": "form"}}))) == {}

    @pytest.mark.parametrize("section", ["configs", "secrets"])
    @pytest.mark.parametrize(("variable", "message"), INTERPOLATED_SOURCES)
    def test_a_source_that_became_a_disclosing_name_after_interpolation_is_refused(
        self, section: str, variable: str, message: str
    ) -> None:
        doc = swarm_section(section, {"c": {"name": "shop_c", "environment": variable}})

        assert sr.swarm_refusals(model(doc)) == {f"{section}.c.environment": message}
