"""Tests for app/services/swarm_stack_translate.py: the resolved Compose model to Swarm objects."""

import copy
import json
import pickle
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.compose_cli.parsers import ResolvedProject
from app.services.swarm_stack_translate import (
    ServiceItem,
    Source,
    Translation,
    TranslationInput,
    TranslationRejected,
    bind_references,
    compose_path,
    translate,
)

SAMPLES = Path(__file__).parents[1] / "swarm_stack_samples"
NAMESPACE = "Shop"
GENERATED = "DOCKMIN_SOURCE_1"
NAMESPACE_LABEL = "com.docker.stack.namespace"
IMAGE = "registry:2"
PINNED = "registry:2@sha256:" + "a" * 64
WORK_DIRECTORY = Path("/srv/dockmin/stack-work")
DATA_DIRECTORY = "/srv/dockmin/data"
API_47 = (1, 47)
API_46 = (1, 46)
PLAIN_VALUE = "plain config value"
SECRET_VALUE = "super-secret-value"
SECRET_TOKEN = "abcdefghij12"
VARIABLES = {"CFGVAR": PLAIN_VALUE, "SECVAR": SECRET_VALUE}
SUFFIX_LENGTH = 12
SECOND = 1_000_000_000
CONTAINER = "TaskTemplate.ContainerSpec"
TASK = "TaskTemplate"
MAX_SERVICES = 50
MAX_REPLICAS = 1000


def sample(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return loaded


def model(**service: Any) -> dict[str, Any]:
    """A resolved model with one service named web on the default network."""
    return {
        "name": "shop",
        "networks": {"default": {"name": "shop_default", "ipam": {}}},
        "services": {
            "web": {
                "command": None,
                "entrypoint": None,
                "image": IMAGE,
                "networks": {"default": None},
                **service,
            }
        },
    }


def with_section(section: str, entries: dict[str, Any], **service: Any) -> dict[str, Any]:
    """A model declaring `entries` under `section`, with web referencing every one of them."""
    doc = model(**service)
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


def translate_model(
    doc: dict[str, Any],
    *,
    namespace: str = NAMESPACE,
    api: tuple[int, int] = API_47,
    tokens: dict[str, str] | None = None,
    protected: tuple[str, ...] = (),
    sources: Mapping[str, Source] | None = None,
) -> Translation:
    project = ResolvedProject(
        name=doc.get("name", "shop"),
        services=doc.get("services", {}),
        networks=doc.get("networks", {}),
        volumes=doc.get("volumes", {}),
        configs=doc.get("configs", {}),
        secrets=doc.get("secrets", {}),
        document=doc,
    )
    return translate(
        TranslationInput(
            namespace=namespace,
            project=project,
            variables=VARIABLES,
            secret_variables=frozenset({"SECVAR"}),
            variable_tokens={"SECVAR": SECRET_TOKEN} if tokens is None else tokens,
            project_directory=WORK_DIRECTORY,
            api_version=api,
            protected_paths=protected,
            sources=sources or {},
        )
    )


def spec_of(**service: Any) -> dict[str, Any]:
    return dict(translate_model(model(**service)).services[0].spec)


def dig(spec: Mapping[str, Any], path: str) -> Any:
    """Walk dotted keys into a spec; a missing key gives None."""
    node: Any = spec
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def refusal(doc: dict[str, Any], **kwargs: Any) -> TranslationRejected:
    with pytest.raises(TranslationRejected) as raised:
        translate_model(doc, **kwargs)
    return raised.value


class TestNamingAndLabels:
    def test_every_object_is_named_from_the_namespace_in_its_stored_spelling(self) -> None:
        doc = with_section("configs", {"cfg": {"name": "shop_cfg", "content": "hello"}})

        result = translate_model(doc, namespace="MiXed-Case")

        assert [s.name for s in result.services] == ["MiXed-Case_web"]
        assert [n.name for n in result.networks] == ["MiXed-Case_default"]
        [config] = result.files
        assert config.name.startswith("MiXed-Case_cfg_")
        assert len(config.name) == len("MiXed-Case_cfg_") + SUFFIX_LENGTH

    def test_the_namespace_label_is_set_on_the_service_its_container_spec_and_each_created_object(
        self,
    ) -> None:
        doc = with_section(
            "configs",
            {"cfg": {"name": "shop_cfg", "content": "hello", "labels": {"own": "label"}}},
            volumes=[{"type": "volume", "source": "data", "target": "/data"}],
            labels={"on": "container"},
            deploy={"labels": {"on": "service"}},
        )
        doc["volumes"] = {"data": {"name": "shop_data"}}
        doc["secrets"] = {"sec": {"name": "shop_sec", "environment": "SECVAR"}}
        doc["services"]["web"]["secrets"] = [{"source": "sec"}]

        result = translate_model(doc)

        [service] = result.services
        [network] = result.networks
        config, secret = result.files
        [mount] = service.spec["TaskTemplate"]["ContainerSpec"]["Mounts"]
        assert service.spec["Labels"] == {NAMESPACE_LABEL: NAMESPACE, "on": "service"}
        assert dig(dict(service.spec), f"{CONTAINER}.Labels") == {
            NAMESPACE_LABEL: NAMESPACE,
            "on": "container",
        }
        assert network.body["Labels"] == {NAMESPACE_LABEL: NAMESPACE}
        assert config.labels == {NAMESPACE_LABEL: NAMESPACE, "own": "label"}
        assert secret.labels == {NAMESPACE_LABEL: NAMESPACE}
        assert mount["VolumeOptions"]["Labels"] == {NAMESPACE_LABEL: NAMESPACE}

    def test_the_namespace_label_is_not_put_on_an_external_volume_mount(self) -> None:
        doc = with_section("volumes", {"data": {"name": "outside", "external": True}})

        mount = translate_model(doc).services[0].spec["TaskTemplate"]["ContainerSpec"]["Mounts"][0]

        assert mount["Source"] == "outside"
        assert "VolumeOptions" not in mount or NAMESPACE_LABEL not in mount["VolumeOptions"].get(
            "Labels", {}
        )

    def test_a_service_with_no_network_joins_the_default_network_under_its_key(self) -> None:
        doc = model()
        doc["services"]["web"].pop("networks")

        spec = translate_model(doc).services[0].spec

        assert dig(dict(spec), f"{TASK}.Networks") == [
            {"Target": f"{NAMESPACE}_default", "Aliases": ["web"]}
        ]

    def test_aliases_are_the_listed_aliases_then_the_service_key(self) -> None:
        doc = with_section("networks", {"front": {"name": "shop_front"}})
        doc["services"]["web"]["networks"] = {"front": {"aliases": ["w1", "w2"]}}

        spec = translate_model(doc).services[0].spec

        assert dig(dict(spec), f"{TASK}.Networks") == [
            {"Target": "Shop_front", "Aliases": ["w1", "w2", "web"]}
        ]

    def test_an_external_network_is_resolved_by_its_name_and_not_created(self) -> None:
        doc = with_section("networks", {"outer": {"name": "outside", "external": True}})

        result = translate_model(doc)

        [network] = result.networks
        assert (network.key, network.name, network.external) == ("outer", "outside", True)
        assert dig(dict(result.services[0].spec), f"{TASK}.Networks")[0]["Target"] == "outside"

    def test_a_declared_name_equal_to_the_default_is_treated_as_absent(self) -> None:
        doc = with_section("networks", {"front": {"name": "shop_front"}})

        result = translate_model(doc)

        assert [n.name for n in result.networks] == ["Shop_front"]

    def test_the_images_of_the_model_are_grouped_with_the_services_that_use_them(self) -> None:
        doc = model()
        doc["services"]["api"] = {**doc["services"]["web"], "image": "other:1"}
        doc["services"]["worker"] = dict(doc["services"]["web"])

        result = translate_model(doc)

        assert dict(result.images) == {IMAGE: ("web", "worker"), "other:1": ("api",)}


class TestFullTranslation:
    def test_every_supported_service_key_lands_on_its_engine_field(self) -> None:
        doc = model(
            entrypoint=["/bin/sh", "-c"],
            command=["echo", "$$HOME"],
            environment={"B": "2", "A": "a$$b"},
            labels={"l": "v"},
            restart="always",
            deploy={
                "mode": "replicated",
                "replicas": 3,
                "labels": {"dl": "x"},
                "placement": {
                    "constraints": ["node.role==worker"],
                    "preferences": [{"spread": "node.labels.zone"}],
                    "max_replicas_per_node": 2,
                },
                "resources": {
                    "limits": {"cpus": 0.5, "memory": "134217728", "pids": 100},
                    "reservations": {
                        "cpus": 0.25,
                        "memory": "67108864",
                        "generic_resources": [
                            {"discrete_resource_spec": {"kind": "gpu", "value": 1}}
                        ],
                    },
                },
                "update_config": {
                    "parallelism": 2,
                    "delay": "10s",
                    "failure_action": "rollback",
                    "monitor": "30s",
                    "max_failure_ratio": 0.5,
                    "order": "start-first",
                },
                "rollback_config": {
                    "parallelism": 1,
                    "failure_action": "pause",
                    "order": "stop-first",
                },
                "endpoint_mode": "VIP",
            },
            ports=[
                {
                    "target": 81,
                    "published": "9001",
                    "mode": "host",
                    "protocol": "udp",
                    "name": "p2",
                },
                {"target": 80, "published": "9000", "mode": "ingress", "protocol": "tcp"},
            ],
            healthcheck={
                "test": ["CMD", "true"],
                "interval": "1m30s",
                "timeout": "10s",
                "retries": 3,
                "start_period": "40s",
                "start_interval": "5s",
            },
            user="1000:1000",
            working_dir="/app",
            hostname="h",
            stop_signal="SIGTERM",
            stop_grace_period="1m0s",
            tty=True,
            stdin_open=True,
            init=True,
            read_only=True,
            group_add=["7"],
            dns=["1.1.1.1"],
            dns_search=["a.b"],
            dns_opt=["ndots:2"],
            extra_hosts=["h1=10.0.0.1"],
            ulimits={"nofile": {"soft": 1, "hard": 2}, "nproc": 3},
            cap_add=["NET_ADMIN"],
            cap_drop=["ALL"],
            sysctls={"net.core.somaxconn": "1024"},
            oom_score_adj=10,
            isolation="default",
            logging={"driver": "json-file", "options": {"max-size": "1m"}},
            security_opt=["no-new-privileges:true", "seccomp=unconfined", "apparmor=unconfined"],
        )

        spec = translate_model(doc).services[0].spec

        assert spec == {
            "Name": "Shop_web",
            "Labels": {NAMESPACE_LABEL: NAMESPACE, "dl": "x"},
            "Mode": {"Replicated": {"Replicas": 3}},
            "UpdateConfig": {
                "Parallelism": 2,
                "Delay": 10 * SECOND,
                "FailureAction": "rollback",
                "Monitor": 30 * SECOND,
                "MaxFailureRatio": 0.5,
                "Order": "start-first",
            },
            "RollbackConfig": {"Parallelism": 1, "FailureAction": "pause", "Order": "stop-first"},
            "EndpointSpec": {
                "Mode": "vip",
                "Ports": [
                    {
                        "Protocol": "tcp",
                        "PublishMode": "ingress",
                        "PublishedPort": 9000,
                        "TargetPort": 80,
                    },
                    {
                        "Name": "p2",
                        "Protocol": "udp",
                        "PublishMode": "host",
                        "PublishedPort": 9001,
                        "TargetPort": 81,
                    },
                ],
            },
            "TaskTemplate": {
                "ContainerSpec": {
                    "Image": IMAGE,
                    "Command": ["/bin/sh", "-c"],
                    "Args": ["echo", "$HOME"],
                    "Env": ["A=a$b", "B=2"],
                    "Labels": {NAMESPACE_LABEL: NAMESPACE, "l": "v"},
                    "User": "1000:1000",
                    "Dir": "/app",
                    "Hostname": "h",
                    "StopSignal": "SIGTERM",
                    "StopGracePeriod": 60 * SECOND,
                    "TTY": True,
                    "OpenStdin": True,
                    "Init": True,
                    "ReadOnly": True,
                    "Groups": ["7"],
                    "DNSConfig": {
                        "Nameservers": ["1.1.1.1"],
                        "Search": ["a.b"],
                        "Options": ["ndots:2"],
                    },
                    "Hosts": ["10.0.0.1 h1"],
                    "Ulimits": [
                        {"Name": "nofile", "Soft": 1, "Hard": 2},
                        {"Name": "nproc", "Soft": 3, "Hard": 3},
                    ],
                    "CapabilityAdd": ["NET_ADMIN"],
                    "CapabilityDrop": ["ALL"],
                    "Sysctls": {"net.core.somaxconn": "1024"},
                    "OomScoreAdj": 10,
                    "Isolation": "default",
                    "Healthcheck": {
                        "Test": ["CMD", "true"],
                        "Interval": 90 * SECOND,
                        "Timeout": 10 * SECOND,
                        "Retries": 3,
                        "StartPeriod": 40 * SECOND,
                        "StartInterval": 5 * SECOND,
                    },
                    "Privileges": {
                        "NoNewPrivileges": True,
                        "Seccomp": {"Mode": "unconfined"},
                        "AppArmor": {"Mode": "disabled"},
                    },
                },
                "Placement": {
                    "Constraints": ["node.role==worker"],
                    "Preferences": [{"Spread": {"SpreadDescriptor": "node.labels.zone"}}],
                    "MaxReplicas": 2,
                },
                "Resources": {
                    "Limits": {"NanoCPUs": 500_000_000, "MemoryBytes": 134217728, "Pids": 100},
                    "Reservations": {
                        "NanoCPUs": 250_000_000,
                        "MemoryBytes": 67108864,
                        "GenericResources": [{"DiscreteResourceSpec": {"Kind": "gpu", "Value": 1}}],
                    },
                },
                "RestartPolicy": {"Condition": "any"},
                "LogDriver": {"Name": "json-file", "Options": {"max-size": "1m"}},
                "Networks": [{"Target": "Shop_default", "Aliases": ["web"]}],
            },
        }

    def test_the_defaults_sample_translates_to_a_minimal_service(self) -> None:
        result = translate_model(sample("defaults"))

        [service] = result.services
        assert service.spec == {
            "Name": "Shop_web",
            "Labels": {NAMESPACE_LABEL: NAMESPACE},
            "Mode": {"Replicated": {"Replicas": 1}},
            "TaskTemplate": {
                "ContainerSpec": {"Image": IMAGE, "Labels": {NAMESPACE_LABEL: NAMESPACE}},
                "Networks": [{"Target": "Shop_default", "Aliases": ["web"]}],
            },
        }
        assert [n.name for n in result.networks] == ["Shop_default"]


SERVICE_ROWS = [
    pytest.param(
        {"entrypoint": ["/bin/sh", "-c"]},
        f"{CONTAINER}.Command",
        ["/bin/sh", "-c"],
        id="entrypoint",
    ),
    pytest.param({"command": ["a", "b"]}, f"{CONTAINER}.Args", ["a", "b"], id="command"),
    pytest.param({"entrypoint": []}, f"{CONTAINER}.Command", [""], id="empty-entrypoint-clears-it"),
    pytest.param({"command": None}, f"{CONTAINER}.Args", None, id="null-command-is-absent"),
    pytest.param(
        {"environment": {"B": "2", "A": "1"}},
        f"{CONTAINER}.Env",
        ["A=1", "B=2"],
        id="environment-sorted",
    ),
    pytest.param(
        {"environment": {"E": ""}}, f"{CONTAINER}.Env", ["E="], id="empty-environment-value"
    ),
    pytest.param({}, "Mode", {"Replicated": {"Replicas": 1}}, id="default-replicas"),
    pytest.param(
        {"deploy": {"replicas": 0}}, "Mode", {"Replicated": {"Replicas": 0}}, id="zero-replicas"
    ),
    pytest.param({"deploy": {"mode": "global"}}, "Mode", {"Global": {}}, id="global-mode"),
    pytest.param({"deploy": {"endpoint_mode": "dnsrr"}}, "EndpointSpec.Mode", "dnsrr", id="dnsrr"),
    pytest.param(
        {"deploy": {"endpoint_mode": "VIP"}},
        "EndpointSpec.Mode",
        "vip",
        id="endpoint-mode-lower-cased",
    ),
    pytest.param(
        {"restart": "no"}, f"{TASK}.RestartPolicy", {"Condition": "none"}, id="restart-no"
    ),
    pytest.param(
        {"restart": "always"}, f"{TASK}.RestartPolicy", {"Condition": "any"}, id="restart-always"
    ),
    pytest.param(
        {"restart": "unless-stopped"},
        f"{TASK}.RestartPolicy",
        {"Condition": "any"},
        id="restart-unless-stopped",
    ),
    pytest.param(
        {"restart": "on-failure"},
        f"{TASK}.RestartPolicy",
        {"Condition": "on-failure"},
        id="restart-on-failure",
    ),
    pytest.param(
        {"restart": "on-failure:3"},
        f"{TASK}.RestartPolicy",
        {"Condition": "on-failure", "MaxAttempts": 3},
        id="restart-on-failure-with-attempts",
    ),
    pytest.param({}, f"{TASK}.RestartPolicy", None, id="no-restart-key-sends-no-policy"),
    pytest.param(
        {
            "deploy": {
                "restart_policy": {
                    "condition": "on-failure",
                    "delay": "5s",
                    "max_attempts": 3,
                    "window": "1m0s",
                }
            }
        },
        f"{TASK}.RestartPolicy",
        {"Condition": "on-failure", "Delay": 5 * SECOND, "MaxAttempts": 3, "Window": 60 * SECOND},
        id="restart-policy-block",
    ),
    pytest.param(
        {"deploy": {"placement": {"max_replicas_per_node": 2}}},
        f"{TASK}.Placement.MaxReplicas",
        2,
        id="max-replicas-per-node",
    ),
    pytest.param(
        {"deploy": {"resources": {"limits": {"cpus": 0.333}}}},
        f"{TASK}.Resources.Limits.NanoCPUs",
        333_000_000,
        id="fractional-cpus",
    ),
    pytest.param(
        {"stop_grace_period": "0s"}, f"{CONTAINER}.StopGracePeriod", 0, id="zero-grace-period"
    ),
    pytest.param(
        {"stop_grace_period": "1h2m3.5s"},
        f"{CONTAINER}.StopGracePeriod",
        3723 * SECOND + SECOND // 2,
        id="fractional-grace-period",
    ),
    pytest.param(
        {"healthcheck": {"disable": True}},
        f"{CONTAINER}.Healthcheck",
        {"Test": ["NONE"]},
        id="disabled-healthcheck",
    ),
    pytest.param(
        {"healthcheck": {"test": ["CMD-SHELL", "x"], "interval": "0s"}},
        f"{CONTAINER}.Healthcheck",
        {"Test": ["CMD-SHELL", "x"], "Interval": 0},
        id="zero-health-interval",
    ),
    pytest.param(
        {"extra_hosts": ["h2=10.0.0.3", "h2=10.0.0.2", "h3=::1"]},
        f"{CONTAINER}.Hosts",
        ["10.0.0.3 h2", "10.0.0.2 h2", "::1 h3"],
        id="extra-hosts-in-the-cli-form",
    ),
    pytest.param(
        {"cap_add": ["cap_net_admin"]},
        f"{CONTAINER}.CapabilityAdd",
        ["NET_ADMIN"],
        id="capability-normalised",
    ),
    pytest.param(
        {"security_opt": ["no-new-privileges"]},
        f"{CONTAINER}.Privileges",
        {"NoNewPrivileges": True},
        id="no-new-privileges",
    ),
    pytest.param(
        {
            "volumes": [
                {"type": "tmpfs", "target": "/t", "tmpfs": {"size": "1048576", "mode": 1023}}
            ]
        },
        f"{CONTAINER}.Mounts",
        [{"Type": "tmpfs", "Target": "/t", "TmpfsOptions": {"SizeBytes": 1048576, "Mode": 1023}}],
        id="tmpfs-mount",
    ),
    pytest.param(
        {"volumes": [{"type": "volume", "target": "/anon"}]},
        f"{CONTAINER}.Mounts",
        [{"Type": "volume", "Target": "/anon"}],
        id="anonymous-volume",
    ),
    pytest.param(
        {
            "volumes": [
                {
                    "type": "bind",
                    "source": "/srv/data",
                    "target": "/d",
                    "read_only": True,
                    "bind": {"propagation": "rslave"},
                }
            ]
        },
        f"{CONTAINER}.Mounts",
        [
            {
                "Type": "bind",
                "Source": "/srv/data",
                "Target": "/d",
                "ReadOnly": True,
                "BindOptions": {"Propagation": "rslave"},
            }
        ],
        id="bind-mount",
    ),
    pytest.param(
        {
            "volumes": [
                {
                    "type": "bind",
                    "source": "/srv/data",
                    "target": "/d",
                    "bind": {"create_host_path": True},
                }
            ]
        },
        f"{CONTAINER}.Mounts",
        [{"Type": "bind", "Source": "/srv/data", "Target": "/d"}],
        id="create-host-path-has-no-effect",
    ),
    pytest.param(
        {"ports": [{"target": 80}]},
        "EndpointSpec.Ports",
        [{"TargetPort": 80, "Protocol": "tcp", "PublishMode": "ingress"}],
        id="dynamic-port",
    ),
    pytest.param(
        {"ports": [{"target": 80, "published": 8080}]},
        "EndpointSpec.Ports",
        [{"TargetPort": 80, "PublishedPort": 8080, "Protocol": "tcp", "PublishMode": "ingress"}],
        id="numeric-published-port",
    ),
]


@pytest.mark.parametrize(("fields", "path", "expected"), SERVICE_ROWS)
def test_a_service_key_translates_to_its_engine_field(
    fields: dict[str, Any], path: str, expected: Any
) -> None:
    spec = spec_of(**fields)

    assert dig(spec, path) == expected


def test_ports_networks_mounts_and_environment_come_out_in_a_fixed_order() -> None:
    first = model(
        environment={"B": "2", "A": "1"},
        ports=[{"target": 81, "published": "9001"}, {"target": 80, "published": "9000"}],
        volumes=[{"type": "tmpfs", "target": "/z"}, {"type": "tmpfs", "target": "/a"}],
        cap_add=["NET_ADMIN", "CHOWN"],
    )
    second = model(
        environment={"A": "1", "B": "2"},
        ports=[{"target": 80, "published": "9000"}, {"target": 81, "published": "9001"}],
        volumes=[{"type": "tmpfs", "target": "/a"}, {"type": "tmpfs", "target": "/z"}],
        cap_add=["CHOWN", "NET_ADMIN"],
    )

    assert translate_model(first).services[0].spec == translate_model(second).services[0].spec


class TestVolumes:
    def test_a_project_volume_mount_names_the_volume_after_the_stack_and_stamps_the_namespace(
        self,
    ) -> None:
        doc = with_section("volumes", {"data": {"name": "shop_data"}})
        doc["services"]["web"]["volumes"] = [
            {
                "type": "volume",
                "source": "data",
                "target": "/data",
                "read_only": True,
                "volume": {"nocopy": True},
            }
        ]

        result = translate_model(doc)

        [mount] = dig(dict(result.services[0].spec), f"{CONTAINER}.Mounts")
        assert mount == {
            "Type": "volume",
            "Source": "Shop_data",
            "Target": "/data",
            "ReadOnly": True,
            "VolumeOptions": {"NoCopy": True, "Labels": {NAMESPACE_LABEL: NAMESPACE}},
        }
        [use] = result.volumes
        assert (use.service, use.source, use.read_only, use.external) == (
            "web",
            "Shop_data",
            True,
            False,
        )

    def test_a_declared_volume_driver_becomes_the_mount_driver_config(self) -> None:
        doc = with_section(
            "volumes",
            {"data": {"name": "shop_data", "driver": "local", "driver_opts": {"type": "nfs"}}},
        )

        [mount] = dig(dict(translate_model(doc).services[0].spec), f"{CONTAINER}.Mounts")

        assert mount["VolumeOptions"]["DriverConfig"] == {
            "Name": "local",
            "Options": {"type": "nfs"},
        }

    def test_an_external_volume_is_recorded_for_the_reach_check_under_its_own_name(self) -> None:
        doc = with_section("volumes", {"data": {"name": "outside", "external": True}})

        [use] = translate_model(doc).volumes

        assert (use.source, use.external) == ("outside", True)

    def test_a_subpath_is_sent_on_a_daemon_that_supports_it(self) -> None:
        doc = model(volumes=[{"type": "volume", "target": "/a", "volume": {"subpath": "sub/dir"}}])

        [mount] = dig(dict(translate_model(doc).services[0].spec), f"{CONTAINER}.Mounts")

        assert mount["VolumeOptions"]["Subpath"] == "sub/dir"

    @pytest.mark.parametrize(
        "source",
        [
            WORK_DIRECTORY.as_posix(),
            f"{WORK_DIRECTORY}/data",
            DATA_DIRECTORY,
            f"{DATA_DIRECTORY}/dockmin.db",
        ],
        ids=[
            "work-directory",
            "beneath-work-directory",
            "data-directory",
            "beneath-data-directory",
        ],
    )
    def test_a_bind_source_on_this_server_is_refused(self, source: str) -> None:
        doc = model(volumes=[{"type": "bind", "source": source, "target": "/a"}])

        failure = refusal(doc, protected=(DATA_DIRECTORY,))

        assert list(failure.fields) == ["services.web.volumes[0].source"]
        assert failure.reason == "invalid"

    def test_a_sibling_of_the_protected_directories_is_a_node_path(self) -> None:
        doc = model(volumes=[{"type": "bind", "source": f"{WORK_DIRECTORY}2/data", "target": "/a"}])

        [mount] = dig(
            dict(translate_model(doc, protected=(DATA_DIRECTORY,)).services[0].spec),
            f"{CONTAINER}.Mounts",
        )

        assert mount["Source"] == f"{WORK_DIRECTORY}2/data"


class TestConfigsAndSecrets:
    def configs_and_secrets(self) -> dict[str, Any]:
        doc = sample("configs-and-secrets")
        for kind in ("configs", "secrets"):
            for key in [k for k, v in doc[kind].items() if "file" in v]:
                del doc[kind][key]
            doc["services"]["web"][kind] = [
                ref for ref in doc["services"]["web"][kind] if ref["source"] in doc[kind]
            ]
        return doc

    def numeric_modes(self, doc: dict[str, Any]) -> dict[str, Any]:
        numeric = copy.deepcopy(doc)
        for kind in ("configs", "secrets"):
            for reference in numeric["services"]["web"][kind]:
                if "mode" in reference:
                    reference["mode"] = int(reference["mode"], 8)
        return numeric

    def test_the_sample_translates_references_with_the_cli_defaults(self) -> None:
        result = translate_model(self.configs_and_secrets())

        container = result.services[0].spec["TaskTemplate"]["ContainerSpec"]
        configs, secrets = container["Configs"], container["Secrets"]
        assert [r["ConfigName"][: -(SUFFIX_LENGTH + 1)] for r in configs[:3]] == [
            "Shop_c1",
            "Shop_c2",
            "Shop_c4",
        ]
        assert configs[3]["ConfigName"] == "extcfg"
        assert [r["File"] for r in configs] == [
            {"Name": "c1", "UID": "0", "GID": "0", "Mode": 0o444},
            {"Name": "/etc/c2.conf", "UID": "1000", "GID": "1001", "Mode": 0o440},
            {"Name": "rel/c4", "UID": "0", "GID": "0", "Mode": 0o644},
            {"Name": "extcfg", "UID": "0", "GID": "0", "Mode": 0o444},
        ]
        assert secrets[0]["SecretName"] == f"Shop_s1_{SECRET_TOKEN}"
        assert secrets[1]["SecretName"] == "ext-explicit"
        assert [r["File"] for r in secrets] == [
            {"Name": "/run/secrets/s1", "UID": "0", "GID": "0", "Mode": 0o444},
            {"Name": "/run/secrets/exts", "UID": "0", "GID": "0", "Mode": 0o444},
        ]

    def test_a_numeric_mode_and_an_octal_string_mode_give_the_same_spec(self) -> None:
        strings = self.configs_and_secrets()
        numbers = self.numeric_modes(strings)

        by_string = translate_model(strings).services[0].spec
        by_number = translate_model(numbers).services[0].spec

        assert by_string == by_number

    def test_a_config_is_named_from_the_digest_of_its_content_and_the_content_is_sent_whole(
        self,
    ) -> None:
        first = with_section("configs", {"cfg": {"name": "shop_cfg", "content": "one"}})
        second = with_section("configs", {"cfg": {"name": "shop_cfg", "content": "two"}})

        [one] = translate_model(first).files
        [other] = translate_model(second).files
        [again] = translate_model(first).files

        assert one.name == again.name and one.name != other.name
        assert one.data is not None and one.data.reveal() == b"one"

    def test_a_config_read_from_a_variable_carries_that_variables_value(self) -> None:
        doc = with_section("configs", {"cfg": {"name": "shop_cfg", "environment": "CFGVAR"}})

        [config] = translate_model(doc).files

        assert config.data is not None and config.data.reveal() == PLAIN_VALUE.encode()

    def test_a_secret_is_named_from_its_token_and_not_from_its_value(self) -> None:
        doc = with_section("secrets", {"sec": {"name": "shop_sec", "environment": "SECVAR"}})

        [secret] = translate_model(doc).files
        [rotated] = translate_model(doc, tokens={"SECVAR": "zzzzzzzzzzzz"}).files

        assert secret.name == f"Shop_sec_{SECRET_TOKEN}"
        assert rotated.name == "Shop_sec_zzzzzzzzzzzz"
        assert secret.data is not None and secret.data.reveal() == SECRET_VALUE.encode()

    def test_the_secret_value_is_in_no_repr_and_cannot_be_serialised(self) -> None:
        doc = with_section("secrets", {"sec": {"name": "shop_sec", "environment": "SECVAR"}})
        result = translate_model(doc)

        text = repr(result) + str(result.files) + repr(result.files[0].data)

        assert SECRET_VALUE not in text
        with pytest.raises(TypeError):
            pickle.dumps(result.files[0].data)

    def test_a_config_from_a_plain_file_is_named_by_the_digest_of_its_content(self) -> None:
        doc = with_section("configs", {"cfg": {"name": "shop_cfg", "environment": GENERATED}})
        first = {GENERATED: Source("configs", "cfg", b"one", False, SECRET_TOKEN, "a.conf")}
        second = {GENERATED: Source("configs", "cfg", b"two", False, SECRET_TOKEN, "a.conf")}

        [config] = translate_model(doc, sources=first).files
        [changed] = translate_model(doc, sources=second).files

        assert config.data is not None and config.data.reveal() == b"one"
        assert config.name != changed.name

    def test_a_secret_from_a_secret_file_is_named_by_the_files_token(self) -> None:
        doc = with_section("secrets", {"sec": {"name": "shop_sec", "environment": GENERATED}})
        held = {GENERATED: Source("secrets", "sec", b"file-secret", True, SECRET_TOKEN, "s.txt")}
        rotated = {GENERATED: Source("secrets", "sec", b"other", True, "zzzzzzzzzzzz", "s.txt")}

        [secret] = translate_model(doc, sources=held).files
        [next_secret] = translate_model(doc, sources=rotated).files

        assert secret.name == f"Shop_sec_{SECRET_TOKEN}"
        assert secret.data is not None and secret.data.reveal() == b"file-secret"
        assert next_secret.name == "Shop_sec_zzzzzzzzzzzz"
        assert b"file-secret" not in repr(held).encode()

    @pytest.mark.parametrize(
        ("section", "secret", "path"),
        [("secrets", False, "secrets.sec.file"), ("configs", True, "configs.cfg.file")],
    )
    def test_a_secret_from_a_plain_file_and_a_config_from_a_secret_file_are_refused(
        self, section: str, secret: bool, path: str
    ) -> None:
        key = "sec" if section == "secrets" else "cfg"
        doc = with_section(section, {key: {"name": f"shop_{key}", "environment": GENERATED}})
        sources = {GENERATED: Source(section, key, b"data", secret, SECRET_TOKEN, "f.txt")}  # type: ignore[arg-type]

        assert set(refusal(doc, sources=sources).fields) == {path}

    def test_the_translation_input_never_shows_a_variable_value_in_its_repr(self) -> None:
        project = ResolvedProject("shop", {}, {}, {}, {}, {}, {})

        shown = repr(
            TranslationInput(
                NAMESPACE,
                project,
                VARIABLES,
                frozenset(),
                {"SECVAR": SECRET_TOKEN},
                WORK_DIRECTORY,
                API_47,
            )
        )

        assert SECRET_VALUE not in shown and PLAIN_VALUE not in shown and SECRET_TOKEN not in shown


class TestEscapesAndSamples:
    def test_dollar_signs_are_unescaped_once_in_every_string_taken_from_the_model(self) -> None:
        doc = sample("escapes")
        del doc["services"]["web"]["environment"]["F"]

        spec = translate_model(doc).services[0].spec

        assert dig(dict(spec), f"{CONTAINER}.Args") == ["echo", "$HOME", "plain", "pa$word$$x"]
        assert dig(dict(spec), f"{CONTAINER}.Env")[:3] == ["A=a$b", "B=$$x", "C=pa$word$$x"]
        assert dig(dict(spec), f"{CONTAINER}.Labels")["l2"] == "pa$word$$x"
        assert spec["Labels"]["dl"] == "p$q"
        assert dig(dict(spec), f"{CONTAINER}.Healthcheck.Test") == ["CMD-SHELL", "echo $HOME"]

    def test_the_empty_commands_sample_keeps_the_entrypoint_and_the_arguments_apart(self) -> None:
        doc = sample("empty-commands")
        del doc["services"]["b"]

        result = {s.key: s.spec for s in translate_model(doc).services}

        assert dig(result["a"], f"{CONTAINER}.Command") == ["sh", "-c", "echo 'a b' && sleep 1"]
        assert dig(result["a"], f"{CONTAINER}.Args") == ["x y", "z"]
        assert dig(result["c"], f"{CONTAINER}.Command") == [""]
        assert dig(result["d"], f"{CONTAINER}.Args") == ["foo", "$BAR", "$BAZ"]
        assert dig(result["d"], f"{CONTAINER}.Labels")["a.b/c"] == "multi $ dollar $$ two"

    def test_the_restart_sample_maps_every_spelling(self) -> None:
        doc = sample("restart-policies")
        for key in ("r6", "r8"):
            del doc["services"][key]

        policies = {
            s.key: dig(dict(s.spec), f"{TASK}.RestartPolicy") for s in translate_model(doc).services
        }

        assert policies == {
            "r1": {"Condition": "none"},
            "r2": {"Condition": "any"},
            "r3": {"Condition": "any"},
            "r4": {"Condition": "on-failure"},
            "r5": {"Condition": "on-failure", "MaxAttempts": 3},
            "r7": None,
        }

    def test_the_health_check_sample_translates_every_form(self) -> None:
        doc = sample("health-checks")
        del doc["services"]["f"]

        checks = {
            s.key: dig(dict(s.spec), f"{CONTAINER}.Healthcheck")
            for s in translate_model(doc).services
        }

        assert checks["a"] == {
            "Test": ["CMD", "true"],
            "Interval": 90 * SECOND,
            "Timeout": 10 * SECOND,
            "Retries": 3,
            "StartPeriod": 40 * SECOND,
            "StartInterval": 5 * SECOND,
        }
        assert checks["b"] == {"Test": ["CMD-SHELL", "echo hi"]}
        assert checks["c"] == checks["d"] == {"Test": ["NONE"]}
        assert checks["e"] == {"Test": ["CMD-SHELL", "x"], "Interval": 0}

    def test_the_extra_hosts_sample_keeps_two_addresses_for_one_host(self) -> None:
        doc = sample("extra-hosts")
        doc["services"]["a"]["extra_hosts"].remove("h4=host-gateway")

        hosts = {
            s.key: dig(dict(s.spec), f"{CONTAINER}.Hosts") for s in translate_model(doc).services
        }

        assert hosts["a"] == ["10.0.0.1 h1", "10.0.0.2 h2", "::1 h3", "fe80::1 h5"]
        assert hosts["b"] == ["10.0.0.1 h1", "10.0.0.2 h2", "10.0.0.3 h2"]

    def test_the_durations_and_sizes_sample_reads_go_durations_and_byte_strings(self) -> None:
        doc = sample("durations-and-sizes")
        del doc["services"]["b"]
        doc["services"]["web"]["deploy"]["resources"]["reservations"]["cpus"] = 0.05

        specs = {s.key: dict(s.spec) for s in translate_model(doc).services}

        assert dig(specs["c"], f"{CONTAINER}.Healthcheck") == {
            "Test": ["CMD", "x"],
            "Interval": 3600 * SECOND,
            "Timeout": SECOND // 10,
        }
        assert dig(specs["c"], f"{CONTAINER}.StopGracePeriod") == 3723 * SECOND + SECOND // 2
        assert dig(specs["web"], f"{TASK}.Resources") == {
            "Limits": {"NanoCPUs": 100_000_000, "MemoryBytes": 1610612736},
            "Reservations": {"NanoCPUs": 50_000_000, "MemoryBytes": 100_000_000},
        }
        assert dig(specs["d"], "EndpointSpec.Mode") == "vip"


SERVICE_REFUSALS = [
    pytest.param({key: "x"}, "refused_construct", f"services.web.{key}", id=key)
    for key in (
        "annotations",
        "attach",
        "blkio_config",
        "cgroup",
        "cgroup_parent",
        "container_name",
        "cpu_count",
        "cpu_percent",
        "cpu_period",
        "cpu_quota",
        "cpu_rt_period",
        "cpu_rt_runtime",
        "cpu_shares",
        "cpus",
        "cpuset",
        "credential_spec",
        "depends_on",
        "device_cgroup_rules",
        "devices",
        "domainname",
        "expose",
        "external_links",
        "gpus",
        "ipc",
        "links",
        "mac_address",
        "mem_limit",
        "mem_reservation",
        "mem_swappiness",
        "memswap_limit",
        "network_mode",
        "oom_kill_disable",
        "pid",
        "pids_limit",
        "platform",
        "post_start",
        "pre_stop",
        "privileged",
        "profiles",
        "pull_policy",
        "runtime",
        "scale",
        "shm_size",
        "storage_opt",
        "tmpfs",
        "use_api_socket",
        "userns_mode",
        "uts",
        "volumes_from",
        "build",
        "env_file",
        "unknown_key",
    )
] + [
    pytest.param(
        {"deploy": {"unknown": 1}},
        "refused_construct",
        "services.web.deploy.unknown",
        id="deploy-unknown",
    ),
    pytest.param(
        {"deploy": {"mode": "replicated-job"}},
        "refused_construct",
        "services.web.deploy.mode",
        id="replicated-job",
    ),
    pytest.param(
        {"deploy": {"mode": "global-job"}},
        "refused_construct",
        "services.web.deploy.mode",
        id="global-job",
    ),
    pytest.param(
        {"deploy": {"resources": {"reservations": {"devices": [{"capabilities": ["gpu"]}]}}}},
        "refused_construct",
        "services.web.deploy.resources.reservations.devices",
        id="device-reservation",
    ),
    pytest.param(
        {"deploy": {"resources": {"limits": {"unknown": 1}}}},
        "refused_construct",
        "services.web.deploy.resources.limits.unknown",
        id="limits-unknown",
    ),
    pytest.param(
        {"deploy": {"placement": {"unknown": 1}}},
        "refused_construct",
        "services.web.deploy.placement.unknown",
        id="placement-unknown",
    ),
    pytest.param(
        {"deploy": {"restart_policy": {"unknown": 1}}},
        "refused_construct",
        "services.web.deploy.restart_policy.unknown",
        id="restart-policy-unknown",
    ),
    pytest.param(
        {"deploy": {"update_config": {"unknown": 1}}},
        "refused_construct",
        "services.web.deploy.update_config.unknown",
        id="update-config-unknown",
    ),
    pytest.param(
        {"healthcheck": {"unknown": 1}},
        "refused_construct",
        "services.web.healthcheck.unknown",
        id="healthcheck-unknown",
    ),
    pytest.param(
        {"ports": [{"target": 80, "published": "80", "host_ip": "127.0.0.1"}]},
        "refused_construct",
        "services.web.ports[0].host_ip",
        id="port-host-ip",
    ),
    pytest.param(
        {"ports": [{"target": 80, "app_protocol": "http"}]},
        "refused_construct",
        "services.web.ports[0].app_protocol",
        id="port-app-protocol",
    ),
    pytest.param(
        {"ports": [{"target": 80, "unknown": 1}]},
        "refused_construct",
        "services.web.ports[0].unknown",
        id="port-unknown",
    ),
    pytest.param(
        {"networks": {"default": {"ipv4_address": "10.0.0.5"}}},
        "refused_construct",
        "services.web.networks.default.ipv4_address",
        id="network-ipv4-address",
    ),
    pytest.param(
        {"networks": {"default": {"x": 1}}},
        "refused_construct",
        "services.web.networks.default.x",
        id="network-entry-unknown",
    ),
    pytest.param(
        {"volumes": [{"type": "volume", "target": "/a", "consistency": "cached"}]},
        "refused_construct",
        "services.web.volumes[0].consistency",
        id="mount-consistency",
    ),
    pytest.param(
        {"volumes": [{"type": "npipe", "source": "a", "target": "/a"}]},
        "refused_construct",
        "services.web.volumes[0].type",
        id="mount-npipe",
    ),
    pytest.param(
        {"volumes": [{"type": "cluster", "source": "g:a", "target": "/a"}]},
        "refused_construct",
        "services.web.volumes[0].type",
        id="mount-cluster",
    ),
    pytest.param(
        {"volumes": [{"type": "image", "source": "a", "target": "/a"}]},
        "refused_construct",
        "services.web.volumes[0].type",
        id="mount-image",
    ),
    pytest.param(
        {"volumes": [{"type": "volume", "target": "/a", "volume": {"labels": {"a": "b"}}}]},
        "refused_construct",
        "services.web.volumes[0].volume.labels",
        id="mount-labels-are-the-products",
    ),
    pytest.param(
        {"volumes": [{"type": "bind", "source": "/x", "target": "/a", "bind": {"selinux": "z"}}]},
        "refused_construct",
        "services.web.volumes[0].bind.selinux",
        id="bind-selinux",
    ),
    pytest.param(
        {"configs": [{"source": "c", "unknown": 1}]},
        "refused_construct",
        "services.web.configs[0].unknown",
        id="config-entry-unknown",
    ),
    pytest.param(
        {"logging": {"driver": "json-file", "unknown": 1}},
        "refused_construct",
        "services.web.logging.unknown",
        id="logging-unknown",
    ),
    pytest.param(
        {"security_opt": ["label:disable"]},
        "refused_construct",
        "services.web.security_opt[0]",
        id="security-opt-label",
    ),
    pytest.param(
        {"security_opt": ["seccomp=profile.json"]},
        "refused_construct",
        "services.web.security_opt[0]",
        id="security-opt-profile-file",
    ),
    pytest.param(
        {"deploy": {"mode": "Replicated"}},
        "invalid",
        "services.web.deploy.mode",
        id="mode-case-matters",
    ),
    pytest.param(
        {"deploy": {"mode": "global", "replicas": 2}},
        "invalid",
        "services.web.deploy.replicas",
        id="global-with-replicas",
    ),
    pytest.param(
        {"deploy": {"replicas": MAX_REPLICAS + 1}},
        "invalid",
        "services.web.deploy.replicas",
        id="too-many-replicas",
    ),
    pytest.param(
        {"deploy": {"replicas": -1}},
        "invalid",
        "services.web.deploy.replicas",
        id="negative-replicas",
    ),
    pytest.param(
        {"deploy": {"replicas": True}},
        "invalid",
        "services.web.deploy.replicas",
        id="boolean-replicas",
    ),
    pytest.param(
        {
            "deploy": {
                "resources": {
                    "limits": {"memory": "134217728"},
                    "reservations": {"memory": "268435456"},
                }
            }
        },
        "invalid",
        "services.web.deploy.resources.reservations.memory",
        id="reservation-over-limit",
    ),
    pytest.param(
        {"deploy": {"resources": {"limits": {"cpus": 0.0001}}}},
        "invalid",
        "services.web.deploy.resources.limits.cpus",
        id="cpus-below-minimum",
    ),
    pytest.param(
        {"deploy": {"resources": {"limits": {"memory": "1"}}}},
        "invalid",
        "services.web.deploy.resources.limits.memory",
        id="memory-below-minimum",
    ),
    pytest.param(
        {"deploy": {"resources": {"limits": {"pids": 0}}}},
        "invalid",
        "services.web.deploy.resources.limits.pids",
        id="zero-pids",
    ),
    pytest.param(
        {
            "deploy": {
                "resources": {
                    "reservations": {
                        "generic_resources": [
                            {"named_resource_spec": {"kind": "gpu", "value": "a"}}
                        ]
                    }
                }
            }
        },
        "invalid",
        "services.web.deploy.resources.reservations.generic_resources[0]",
        id="named-generic-resource",
    ),
    pytest.param(
        {"deploy": {"placement": {"constraints": ["node.role"]}}},
        "invalid",
        "services.web.deploy.placement.constraints",
        id="malformed-constraint",
    ),
    pytest.param(
        {"deploy": {"placement": {"constraints": ["node.role==worker"] * 33}}},
        "invalid",
        "services.web.deploy.placement.constraints",
        id="too-many-constraints",
    ),
    pytest.param(
        {"deploy": {"placement": {"preferences": [{"spread": "a b"}]}}},
        "invalid",
        "services.web.deploy.placement.preferences[0].spread",
        id="malformed-preference",
    ),
    pytest.param(
        {"deploy": {"restart_policy": {"condition": "sometimes"}}},
        "invalid",
        "services.web.deploy.restart_policy.condition",
        id="restart-condition",
    ),
    pytest.param(
        {"deploy": {"restart_policy": {"delay": "500us"}}},
        "invalid",
        "services.web.deploy.restart_policy.delay",
        id="delay-below-a-millisecond",
    ),
    pytest.param(
        {"deploy": {"restart_policy": {"delay": "25h"}}},
        "invalid",
        "services.web.deploy.restart_policy.delay",
        id="delay-over-a-day",
    ),
    pytest.param(
        {"deploy": {"restart_policy": {"delay": "soon"}}},
        "invalid",
        "services.web.deploy.restart_policy.delay",
        id="delay-not-a-duration",
    ),
    pytest.param(
        {"deploy": {"update_config": {"order": "middle"}}},
        "invalid",
        "services.web.deploy.update_config.order",
        id="update-order",
    ),
    pytest.param(
        {"deploy": {"rollback_config": {"failure_action": "rollback"}}},
        "invalid",
        "services.web.deploy.rollback_config.failure_action",
        id="rollback-action-rollback",
    ),
    pytest.param(
        {"deploy": {"update_config": {"max_failure_ratio": 2}}},
        "invalid",
        "services.web.deploy.update_config.max_failure_ratio",
        id="failure-ratio",
    ),
    pytest.param(
        {"deploy": {"endpoint_mode": "round"}},
        "invalid",
        "services.web.deploy.endpoint_mode",
        id="endpoint-mode",
    ),
    pytest.param(
        {"deploy": {"labels": {NAMESPACE_LABEL: "x"}}},
        "restricted",
        "services.web.deploy.labels",
        id="reserved-service-label",
    ),
    pytest.param(
        {"deploy": {"labels": {"Com.Docker.Stack.Namespace": "x"}}},
        "restricted",
        "services.web.deploy.labels",
        id="reserved-label-any-case",
    ),
    pytest.param(
        {"labels": {"com.docker.compose.project": "x"}},
        "restricted",
        "services.web.labels",
        id="reserved-compose-label",
    ),
    pytest.param({"labels": {"a": 1}}, "invalid", "services.web.labels", id="label-value-not-text"),
    pytest.param(
        {"environment": {"A": None}},
        "invalid",
        "services.web.environment.A",
        id="environment-without-a-value",
    ),
    pytest.param(
        {"environment": {"A=B": "x"}},
        "invalid",
        "services.web.environment.<key>",
        id="environment-name-with-equals",
    ),
    pytest.param(
        {"environment": {"A": 5}},
        "invalid",
        "services.web.environment.A",
        id="environment-value-not-text",
    ),
    pytest.param({"command": []}, "invalid", "services.web.command", id="empty-command"),
    pytest.param({"restart": "bogus"}, "invalid", "services.web.restart", id="restart-value"),
    pytest.param(
        {"restart": "no:3"}, "invalid", "services.web.restart", id="restart-no-with-attempts"
    ),
    pytest.param(
        {"restart": "always", "deploy": {"restart_policy": {"condition": "any"}}},
        "invalid",
        "services.web.restart",
        id="restart-with-restart-policy",
    ),
    pytest.param(
        {"healthcheck": {"test": ["CMD", "x"], "disable": True}},
        "invalid",
        "services.web.healthcheck",
        id="healthcheck-test-and-disable",
    ),
    pytest.param(
        {"healthcheck": {"test": ["NONE", "x"]}},
        "invalid",
        "services.web.healthcheck.test",
        id="none-with-arguments",
    ),
    pytest.param(
        {"healthcheck": {"test": ["CMD"]}},
        "invalid",
        "services.web.healthcheck.test",
        id="cmd-without-a-command",
    ),
    pytest.param(
        {"healthcheck": {"test": ["CMD", "x"], "interval": "500us"}},
        "invalid",
        "services.web.healthcheck.interval",
        id="health-interval-below-a-millisecond",
    ),
    pytest.param(
        {"healthcheck": {"retries": -1}},
        "invalid",
        "services.web.healthcheck.retries",
        id="negative-retries",
    ),
    pytest.param(
        {"ports": [{"target": 80, "published": "8000-8002"}]},
        "invalid",
        "services.web.ports[0].published",
        id="published-range",
    ),
    pytest.param(
        {"ports": [{"target": 80, "published": "0"}]},
        "invalid",
        "services.web.ports[0].published",
        id="published-zero",
    ),
    pytest.param(
        {"ports": [{"target": 80, "published": 65536}]},
        "invalid",
        "services.web.ports[0].published",
        id="published-too-high",
    ),
    pytest.param(
        {"ports": [{"target": 0}]}, "invalid", "services.web.ports[0].target", id="target-zero"
    ),
    pytest.param(
        {"ports": [{"target": 80, "protocol": "icmp"}]},
        "invalid",
        "services.web.ports[0].protocol",
        id="port-protocol",
    ),
    pytest.param(
        {"ports": [{"target": 80, "mode": "bridge"}]},
        "invalid",
        "services.web.ports[0].mode",
        id="port-mode",
    ),
    pytest.param(
        {
            "ports": [{"target": 80, "published": "80", "mode": "ingress"}],
            "deploy": {"endpoint_mode": "dnsrr"},
        },
        "invalid",
        "services.web.ports[0].mode",
        id="ingress-port-with-dnsrr",
    ),
    pytest.param(
        {"ports": [{"target": 80, "published": "80"}, {"target": 81, "published": "80"}]},
        "invalid",
        "services.web.ports[1].published",
        id="published-twice",
    ),
    pytest.param(
        {"ports": [{"target": 80 + i % 100, "published": str(1000 + i)} for i in range(257)]},
        "invalid",
        "services.web.ports",
        id="too-many-ports",
    ),
    pytest.param(
        {"networks": {"default": {"aliases": ["bad alias"]}}},
        "invalid",
        "services.web.networks.default.aliases",
        id="bad-alias",
    ),
    pytest.param({"networks": {"nope": None}}, "invalid", "networks.nope", id="undeclared-network"),
    pytest.param(
        {"volumes": [{"type": "volume", "source": "nope", "target": "/a"}]},
        "invalid",
        "services.web.volumes[0].source",
        id="undeclared-volume",
    ),
    pytest.param(
        {"volumes": [{"type": "volume", "target": "a"}]},
        "invalid",
        "services.web.volumes[0].target",
        id="relative-mount-target",
    ),
    pytest.param(
        {"volumes": [{"type": "volume", "target": "/"}]},
        "invalid",
        "services.web.volumes[0].target",
        id="root-mount-target",
    ),
    pytest.param(
        {"volumes": [{"type": "volume", "target": "/a"}, {"type": "tmpfs", "target": "/a"}]},
        "invalid",
        "services.web.volumes[1].target",
        id="mount-target-twice",
    ),
    pytest.param(
        {
            "volumes": [
                {"type": "bind", "source": "/x", "target": "/a", "bind": {"propagation": "up"}}
            ]
        },
        "invalid",
        "services.web.volumes[0].bind.propagation",
        id="propagation",
    ),
    pytest.param(
        {"volumes": [{"type": "bind", "source": "rel", "target": "/a"}]},
        "invalid",
        "services.web.volumes[0].source",
        id="relative-bind-source",
    ),
    pytest.param(
        {"volumes": [{"type": "tmpfs", "source": "x", "target": "/a"}]},
        "invalid",
        "services.web.volumes[0].source",
        id="tmpfs-with-source",
    ),
    pytest.param(
        {"volumes": [{"type": "tmpfs", "target": "/a", "tmpfs": {"size": "0"}}]},
        "invalid",
        "services.web.volumes[0].tmpfs.size",
        id="tmpfs-size-zero",
    ),
    pytest.param(
        {"volumes": [{"type": "tmpfs", "target": "/a", "tmpfs": {"mode": 16384}}]},
        "invalid",
        "services.web.volumes[0].tmpfs.mode",
        id="tmpfs-mode",
    ),
    pytest.param(
        {"volumes": [{"type": "volume", "target": "/a", "volume": {"x": 1}}]},
        "refused_construct",
        "services.web.volumes[0].volume.x",
        id="volume-option-unknown",
    ),
    pytest.param(
        {"volumes": [{"type": "volume", "target": "/a", "volume": {"subpath": "../x"}}]},
        "invalid",
        "services.web.volumes[0].volume.subpath",
        id="subpath-escapes",
    ),
    pytest.param(
        {"configs": [{"source": "nope"}]}, "invalid", "configs.nope", id="undeclared-config"
    ),
    pytest.param(
        {"logging": {"options": {"max-size": "1m"}}},
        "invalid",
        "services.web.logging",
        id="log-options-without-driver",
    ),
    pytest.param({"cap_add": ["NOT_A_CAP"]}, "invalid", "services.web.cap_add", id="capability"),
    pytest.param(
        {"ulimits": {"bogus": 1}}, "invalid", "services.web.ulimits.bogus", id="ulimit-name"
    ),
    pytest.param(
        {"ulimits": {"nofile": {"soft": 1}}},
        "invalid",
        "services.web.ulimits.nofile.hard",
        id="ulimit-without-hard",
    ),
    pytest.param({"sysctls": {"a b": "1"}}, "invalid", "services.web.sysctls", id="sysctl-key"),
    pytest.param({"isolation": "vm"}, "invalid", "services.web.isolation", id="isolation"),
    pytest.param(
        {"oom_score_adj": 2000}, "invalid", "services.web.oom_score_adj", id="oom-score-range"
    ),
    pytest.param(
        {"extra_hosts": ["h=host-gateway"]},
        "invalid",
        "services.web.extra_hosts",
        id="host-gateway-address",
    ),
    pytest.param(
        {"extra_hosts": ["h"]},
        "invalid",
        "services.web.extra_hosts",
        id="extra-host-without-address",
    ),
    pytest.param(
        {"extra_hosts": ["a_b=10.0.0.1"]},
        "invalid",
        "services.web.extra_hosts",
        id="extra-host-name",
    ),
    pytest.param({"dns": ["example.com"]}, "invalid", "services.web.dns", id="dns-not-an-address"),
    pytest.param(
        {"stop_grace_period": "48h"},
        "invalid",
        "services.web.stop_grace_period",
        id="grace-period-over-a-day",
    ),
    pytest.param({"user": "a b"}, "invalid", "services.web.user", id="user"),
    pytest.param(
        {"working_dir": "a"}, "invalid", "services.web.working_dir", id="relative-working-dir"
    ),
    pytest.param({"hostname": "a.b"}, "invalid", "services.web.hostname", id="hostname-with-dot"),
    pytest.param({"image": "Bad Image"}, "invalid", "services.web.image", id="image-reference"),
]


@pytest.mark.parametrize(("fields", "reason", "path"), SERVICE_REFUSALS)
def test_a_service_key_with_no_swarm_form_or_a_bad_value_is_refused_with_its_compose_path(
    fields: dict[str, Any], reason: str, path: str
) -> None:
    failure = refusal(model(**fields))

    assert failure.reason == reason
    assert path in failure.fields
    assert all(message for message in failure.fields.values())


def test_a_service_without_an_image_is_refused() -> None:
    doc = model()
    del doc["services"]["web"]["image"]

    assert "services.web.image" in refusal(doc).fields


NETWORKS = {"n": {"name": "shop_n", "ipam": {}}}
SECTION_REFUSALS = [
    pytest.param(
        "networks",
        {"n": {"name": "shop_n", "driver": "bridge"}},
        "refused_construct",
        "networks.n.driver",
        id="bridge-network",
    ),
    pytest.param(
        "networks",
        {"n": {"name": "custom"}},
        "refused_construct",
        "networks.n.name",
        id="network-name",
    ),
    pytest.param(
        "networks",
        {"n": {"name": "shop_n", "enable_ipv4": True}},
        "refused_construct",
        "networks.n.enable_ipv4",
        id="network-key-unknown",
    ),
    pytest.param(
        "networks",
        {"n": {"name": "shop_n", "ipam": {"options": {"a": "b"}}}},
        "refused_construct",
        "networks.n.ipam.options",
        id="ipam-options",
    ),
    pytest.param(
        "networks",
        {
            "n": {
                "name": "shop_n",
                "ipam": {"config": [{"subnet": "10.0.0.0/24", "aux_addresses": {"a": "10.0.0.2"}}]},
            }
        },
        "refused_construct",
        "networks.n.ipam.config[0].aux_addresses",
        id="ipam-aux-addresses",
    ),
    pytest.param(
        "networks",
        {"n": {"name": "shop_n", "ipam": {"config": [{"subnet": "10.0.0.5/24"}]}}},
        "invalid",
        "networks.n.ipam.config[0].subnet",
        id="subnet-with-host-bits",
    ),
    pytest.param(
        "networks",
        {
            "n": {
                "name": "shop_n",
                "ipam": {"config": [{"subnet": "10.0.0.0/24", "gateway": "10.1.0.1"}]},
            }
        },
        "invalid",
        "networks.n.ipam.config[0].gateway",
        id="gateway-outside",
    ),
    pytest.param(
        "networks",
        {
            "n": {
                "name": "shop_n",
                "ipam": {"config": [{"subnet": "10.0.0.0/24", "ip_range": "10.1.0.0/25"}]},
            }
        },
        "invalid",
        "networks.n.ipam.config[0].ip_range",
        id="range-outside",
    ),
    pytest.param(
        "networks",
        {"n": {"name": "shop_n", "labels": {NAMESPACE_LABEL: "x"}}},
        "restricted",
        "networks.n.labels",
        id="reserved-network-label",
    ),
    pytest.param(
        "networks",
        {"n": {"name": "shop_n", "driver_opts": {"encrypted": "x" * 300}}},
        "invalid",
        "networks.n.driver_opts.encrypted",
        id="driver-option-value",
    ),
    pytest.param(
        "volumes",
        {"v": {"name": "custom"}},
        "refused_construct",
        "volumes.v.name",
        id="volume-name",
    ),
    pytest.param(
        "volumes",
        {"v": {"name": "shop_v", "x": 1}},
        "refused_construct",
        "volumes.v.x",
        id="volume-key-unknown",
    ),
    pytest.param(
        "volumes",
        {"v": {"name": "shop_v", "labels": {NAMESPACE_LABEL: "x"}}},
        "restricted",
        "volumes.v.labels",
        id="reserved-volume-label",
    ),
    pytest.param(
        "configs",
        {"c": {"name": "shop_c", "content": "x", "template_driver": "golang"}},
        "refused_construct",
        "configs.c.template_driver",
        id="config-template",
    ),
    pytest.param(
        "configs",
        {"c": {"name": "shop_c", "file": "/x"}},
        "refused_construct",
        "configs.c.file",
        id="config-file",
    ),
    pytest.param(
        "configs",
        {"c": {"name": "custom", "content": "x"}},
        "refused_construct",
        "configs.c.name",
        id="config-name",
    ),
    pytest.param(
        "configs",
        {"c": {"name": "shop_c", "content": "x", "labels": {NAMESPACE_LABEL: "x"}}},
        "restricted",
        "configs.c.labels",
        id="reserved-config-label",
    ),
    pytest.param(
        "configs",
        {"c": {"name": "shop_c", "content": "x", "environment": "CFGVAR"}},
        "invalid",
        "configs.c",
        id="config-two-sources",
    ),
    pytest.param(
        "configs", {"c": {"name": "shop_c"}}, "invalid", "configs.c", id="config-no-source"
    ),
    pytest.param(
        "configs",
        {"c": {"name": "shop_c", "content": ""}},
        "invalid",
        "configs.c.content",
        id="empty-config",
    ),
    pytest.param(
        "configs",
        {"c": {"name": "shop_c", "content": "x" * (128 * 1024 + 1)}},
        "invalid",
        "configs.c.content",
        id="config-over-the-cap",
    ),
    pytest.param(
        "configs",
        {"c": {"name": "shop_c", "environment": "SECVAR"}},
        "invalid",
        "configs.c.environment",
        id="config-from-a-secret-variable",
    ),
    pytest.param(
        "configs",
        {"c": {"name": "shop_c", "environment": "NOPE"}},
        "invalid",
        "configs.c.environment",
        id="config-from-an-unknown-variable",
    ),
    pytest.param(
        "secrets",
        {"s": {"name": "shop_s", "content": "x"}},
        "refused_construct",
        "secrets.s.content",
        id="secret-content",
    ),
    pytest.param(
        "secrets",
        {"s": {"name": "shop_s", "file": "/x"}},
        "refused_construct",
        "secrets.s.file",
        id="secret-file",
    ),
    pytest.param(
        "secrets",
        {"s": {"name": "shop_s", "environment": "SECVAR", "driver": "vault"}},
        "refused_construct",
        "secrets.s.driver",
        id="secret-driver",
    ),
    pytest.param(
        "secrets",
        {"s": {"name": "shop_s", "environment": "SECVAR", "driver_opts": {"a": "b"}}},
        "refused_construct",
        "secrets.s.driver_opts",
        id="secret-driver-options",
    ),
    pytest.param(
        "secrets",
        {"s": {"name": "shop_s", "environment": "SECVAR", "template_driver": "golang"}},
        "refused_construct",
        "secrets.s.template_driver",
        id="secret-template",
    ),
    pytest.param(
        "secrets",
        {"s": {"name": "custom", "environment": "SECVAR"}},
        "refused_construct",
        "secrets.s.name",
        id="secret-name",
    ),
    pytest.param(
        "secrets",
        {"s": {"name": "shop_s", "environment": "SECVAR", "labels": {NAMESPACE_LABEL: "x"}}},
        "restricted",
        "secrets.s.labels",
        id="reserved-secret-label",
    ),
    pytest.param(
        "secrets",
        {"s": {"name": "shop_s", "environment": "CFGVAR"}},
        "invalid",
        "secrets.s.environment",
        id="secret-from-a-plain-variable",
    ),
    pytest.param(
        "secrets",
        {"s": {"name": "shop_s", "environment": "NOPE"}},
        "invalid",
        "secrets.s.environment",
        id="secret-from-an-unknown-variable",
    ),
]


@pytest.mark.parametrize(("section", "entries", "reason", "path"), SECTION_REFUSALS)
def test_a_top_level_object_with_no_swarm_form_or_a_bad_value_is_refused_with_its_path(
    section: str, entries: dict[str, Any], reason: str, path: str
) -> None:
    failure = refusal(with_section(section, entries))

    assert failure.reason == reason
    assert path in failure.fields


FILE_REFERENCE_REFUSALS = [
    pytest.param({"mode": 0o17777}, "mode", id="mode-too-wide"),
    pytest.param({"mode": "0999"}, "mode", id="mode-not-octal"),
    pytest.param({"uid": "root"}, "uid", id="uid-not-numeric"),
    pytest.param({"target": "/a/../b"}, "target", id="target-with-dotdot"),
]


@pytest.mark.parametrize(("fields", "key"), FILE_REFERENCE_REFUSALS)
def test_a_config_reference_with_a_bad_field_is_refused_at_its_index(
    fields: dict[str, Any], key: str
) -> None:
    doc = with_section("configs", {"c": {"name": "shop_c", "content": "x"}})
    doc["services"]["web"]["configs"] = [{"source": "c", **fields}]

    failure = refusal(doc)

    assert f"services.web.configs[0].{key}" in failure.fields


def test_two_references_to_one_file_target_are_refused_on_the_second() -> None:
    doc = with_section(
        "configs",
        {"c": {"name": "shop_c", "content": "x"}, "d": {"name": "shop_d", "content": "y"}},
    )
    doc["services"]["web"]["configs"] = [
        {"source": "c", "target": "/t"},
        {"source": "d", "target": "/t"},
    ]

    assert "services.web.configs[1].target" in refusal(doc).fields


def test_two_networks_resolving_to_one_docker_network_are_refused() -> None:
    doc = with_section(
        "networks",
        {"a": {"name": "same", "external": True}, "b": {"name": "same", "external": True}},
    )

    assert "networks.b" in refusal(doc).fields


@pytest.mark.parametrize(
    ("fields", "api"),
    [
        pytest.param({"oom_score_adj": 10}, API_46, id="oom-score-adj"),
        pytest.param(
            {"volumes": [{"type": "volume", "target": "/a", "volume": {"subpath": "x"}}]},
            API_46,
            id="volume-subpath",
        ),
    ],
)
def test_an_option_the_negotiated_api_would_drop_silently_is_refused(
    fields: dict[str, Any], api: tuple[int, int]
) -> None:
    failure = refusal(model(**fields), api=api)

    assert "needs Docker API 1.47" in " ".join(failure.fields.values())


def test_names_that_overrun_the_limits_are_refused_with_the_compose_path() -> None:
    long_key = "k" * 60
    doc = model()
    doc["services"] = {long_key: doc["services"]["web"]}

    assert refusal(doc).fields == {
        f"services.{long_key}": "The stack name and this key are too long together."
    }


@pytest.mark.parametrize("key", ["we.b", "web_"])
def test_a_service_key_that_cannot_be_a_swarm_service_name_is_refused(key: str) -> None:
    doc = model()
    doc["services"] = {key: doc["services"]["web"]}

    assert f"services.{key}" in refusal(doc).fields


def test_a_secret_name_over_the_cap_is_refused_because_of_its_suffix() -> None:
    key = "k" * 50
    doc = with_section("secrets", {key: {"name": f"shop_{key}", "environment": "SECVAR"}})

    assert f"secrets.{key}" in refusal(doc).fields


def test_more_services_than_the_cap_are_refused() -> None:
    doc = {
        "name": "shop",
        "services": {f"s{i}": {"image": IMAGE} for i in range(MAX_SERVICES + 1)},
    }

    assert "services" in refusal(doc).fields


def test_a_namespace_outside_the_stack_grammar_is_refused() -> None:
    failure = refusal(model(), namespace="bad name")

    assert list(failure.fields) == ["name"]


def test_a_top_level_key_outside_the_known_set_is_refused_and_an_extension_is_not() -> None:
    doc = model()
    doc["x-anything"] = {"free": "form"}
    translate_model(doc)
    doc["models"] = {}

    assert "models" in refusal(doc).fields


def test_every_refusal_found_is_reported_together() -> None:
    doc = model(container_name="x", privileged=True)

    failure = refusal(doc)

    assert set(failure.fields) == {"services.web.container_name", "services.web.privileged"}


class TestBinding:
    def stack(self) -> Translation:
        doc = with_section(
            "configs",
            {"c": {"name": "shop_c", "content": "x"}},
            volumes=[{"type": "tmpfs", "target": "/a"}],
        )
        doc["secrets"] = {"s": {"name": "shop_s", "environment": "SECVAR"}}
        doc["services"]["web"]["secrets"] = [{"source": "s"}]
        doc["networks"]["ext"] = {"name": "outside", "external": True}
        doc["services"]["web"]["networks"] = {"default": None, "ext": None}
        return translate_model(doc)

    def resolved_files(self, result: Translation) -> dict[tuple[str, str], tuple[str, str]]:
        return {(f.kind, f.name): (f"{f.kind}-id-{f.key}", f.name) for f in result.files}

    def test_bind_references_sets_the_image_the_file_ids_and_the_external_network_id(self) -> None:
        result = self.stack()
        item = result.services[0]

        bound = bind_references(
            item.spec,
            image=PINNED,
            files=self.resolved_files(result),
            networks={"outside": "netid1"},
        )

        container = bound["TaskTemplate"]["ContainerSpec"]
        assert container["Image"] == PINNED
        assert container["Configs"][0]["ConfigID"] == "config-id-c"
        assert container["Secrets"][0]["SecretID"] == "secret-id-s"
        assert [n["Target"] for n in bound["TaskTemplate"]["Networks"]] == [
            "Shop_default",
            "netid1",
        ]

    def test_bind_references_leaves_the_translated_spec_untouched(self) -> None:
        result = self.stack()
        item = result.services[0]
        before = copy.deepcopy(dict(item.spec))

        bind_references(
            item.spec, image=PINNED, files=self.resolved_files(result), networks={"outside": "n"}
        )

        assert item.spec == before

    def test_a_file_that_was_not_resolved_is_a_lookup_error(self) -> None:
        item = self.stack().services[0]

        with pytest.raises(LookupError, match="is not resolved"):
            bind_references(item.spec, image=IMAGE, files={}, networks={})

    @pytest.mark.parametrize(
        ("engine_path", "compose"),
        [
            ("TaskTemplate.ContainerSpec.Mounts[0]", "services.web.volumes[0]"),
            ("TaskTemplate.ContainerSpec.Secrets[0].File", "services.web.secrets[0]"),
            ("TaskTemplate.Networks[1].Target", "services.web.networks.ext"),
            ("TaskTemplate.ContainerSpec.Image", "services.web.image"),
            ("Mode.Replicated.Replicas", "services.web.deploy.replicas"),
            ("TaskTemplate.ContainerSpec.CapabilityAdd", "services.web"),
            ("Unknown.Path", "services.web"),
        ],
    )
    def test_compose_path_maps_a_service_spec_path_back_to_the_compose_path(
        self, engine_path: str, compose: str
    ) -> None:
        item: ServiceItem = self.stack().services[0]

        assert compose_path(item, engine_path) == compose
