"""Tests for app/services/swarm_stack_reconstruct.py: a namespace's specs back to a Compose file."""

import base64
import copy
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.compose_cli.parsers import ResolvedProject
from app.services.stack_read_result import HostReadResult, NeedsFile
from app.services.swarm_stack_plan import spec_differs
from app.services.swarm_stack_reconstruct import KNOWN_LOSSES, reconstruct_stack
from app.services.swarm_stack_translate import (
    Source,
    Translation,
    TranslationInput,
    bind_references,
    translate,
)

NAMESPACE = "Shop"
LABEL = "com.docker.stack.namespace"
VXLAN = "com.docker.network.driver.overlay.vxlanid_list"
WEB_IMAGE = "registry:2"
API_IMAGE = "busybox:latest"
SECRET_VALUE = "abc123-secret"
SECRET_TOKEN = "abcdefghij12"
WORK_DIRECTORY = Path("/srv/dockmin/stack-work")
API_VERSION = (1, 47)
VARIABLES = {"SECVAR": "swarm-secret-value"}
INTERPOLATION = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def pinned(image: str) -> str:
    return f"{image}@sha256:{hashlib.sha256(image.encode()).hexdigest()}"


def original_model() -> dict[str, Any]:
    """The resolved model of a two-service stack using each Swarm construct the file can hold."""
    return {
        "name": "shop",
        "networks": {
            "default": {"name": "shop_default", "ipam": {}},
            "front": {
                "name": "shop_front",
                "attachable": True,
                "labels": {"tier": "edge"},
                "driver_opts": {"encrypted": "true"},
                "ipam": {"config": [{"subnet": "10.20.0.0/24", "gateway": "10.20.0.1"}]},
            },
        },
        "volumes": {"data": {"name": "shop_data"}},
        "configs": {"cfg": {"name": "shop_cfg", "content": "listen 80;\n"}},
        "secrets": {"sec": {"name": "shop_sec", "environment": "SECVAR"}},
        "services": {
            "web": {
                "image": WEB_IMAGE,
                "command": ["serve", "--port", "80"],
                "entrypoint": None,
                "user": "1000:1000",
                "working_dir": "/srv",
                "environment": {"APP_ENV": "production", "PRICE": "$$5", "API_TOKEN": SECRET_VALUE},
                "labels": {"tier": "frontend"},
                "networks": {"front": {"aliases": ["site"]}, "default": None},
                "ports": [
                    {"target": 80, "published": "8080", "protocol": "tcp", "mode": "ingress"},
                    {"target": 53, "published": "5353", "protocol": "udp", "mode": "host"},
                ],
                "volumes": [
                    {"type": "volume", "source": "data", "target": "/data", "volume": {}},
                    {
                        "type": "bind",
                        "source": "/srv/shared",
                        "target": "/shared",
                        "read_only": True,
                    },
                    {
                        "type": "tmpfs",
                        "target": "/scratch",
                        "tmpfs": {"size": 1048576, "mode": 1023},
                    },
                ],
                "configs": [{"source": "cfg", "target": "/etc/web.conf", "mode": 0o440}],
                "secrets": [
                    {"source": "sec", "target": "/run/secrets/sec", "uid": "1000", "gid": "1000"}
                ],
                "healthcheck": {
                    "test": ["CMD-SHELL", "wget -q localhost || exit 1"],
                    "interval": "30s",
                    "timeout": "5s",
                    "retries": 3,
                },
                "ulimits": {"nofile": {"soft": 1024, "hard": 2048}},
                "extra_hosts": ["db.internal=10.0.0.5"],
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "logging": {"driver": "json-file", "options": {"max-size": "10m"}},
                "stop_grace_period": "20s",
                "init": True,
                "deploy": {
                    "replicas": 2,
                    "labels": {"owner": "ops"},
                    "endpoint_mode": "vip",
                    "placement": {
                        "constraints": ["node.role==worker"],
                        "preferences": [{"spread": "node.labels.zone"}],
                        "max_replicas_per_node": 1,
                    },
                    "resources": {
                        "limits": {"cpus": 0.5, "memory": "268435456", "pids": 100},
                        "reservations": {"memory": "134217728"},
                    },
                    "restart_policy": {
                        "condition": "on-failure",
                        "delay": "5s",
                        "max_attempts": 3,
                        "window": "1m0s",
                    },
                    "update_config": {
                        "parallelism": 2,
                        "delay": "10s",
                        "order": "start-first",
                        "failure_action": "rollback",
                        "monitor": "30s",
                        "max_failure_ratio": 0.25,
                    },
                    "rollback_config": {"parallelism": 1, "failure_action": "pause"},
                },
            },
            "api": {
                "image": API_IMAGE,
                "command": ["sleep", "3600"],
                "entrypoint": [],
                "environment": {"MODE": "api"},
                "networks": {"default": None},
            },
        },
    }


def project_of(document: dict[str, Any]) -> ResolvedProject:
    return ResolvedProject(
        name=document.get("name", "shop"),
        services=document.get("services", {}),
        networks=document.get("networks", {}),
        volumes=document.get("volumes", {}),
        configs=document.get("configs", {}),
        secrets=document.get("secrets", {}),
        document=document,
    )


def translated(
    document: dict[str, Any], sources: Mapping[str, Source] | None = None
) -> Translation:
    return translate(
        TranslationInput(
            namespace=NAMESPACE,
            project=project_of(document),
            variables=VARIABLES,
            secret_variables=frozenset({"SECVAR"}),
            variable_tokens={"SECVAR": SECRET_TOKEN},
            project_directory=WORK_DIRECTORY,
            api_version=API_VERSION,
            sources=sources or {},
        )
    )


class Cluster:
    """The objects a namespace holds on the cluster, as the Engine would return them."""

    def __init__(self) -> None:
        self.translation = translated(original_model())
        self.network_ids = {
            network.name: f"net{number:022d}"
            for number, network in enumerate(self.translation.networks)
        }
        self.file_ids: dict[tuple[str, str], tuple[str, str]] = {
            (item.kind, item.name): (f"{item.kind[:3]}{number:022d}", item.name)
            for number, item in enumerate(self.translation.files)
        }
        self.services = [self._service(item) for item in self.translation.services]
        self.networks = [self._network(item) for item in self.translation.networks]
        self.configs = [
            self._file(item, base64.b64encode(item.data.reveal()).decode("ascii"))  # type: ignore[union-attr]
            for item in self.translation.files
            if item.kind == "config"
        ]
        self.secrets = [
            self._file(item, None) for item in self.translation.files if item.kind == "secret"
        ]

    def _service(self, item: Any) -> dict[str, Any]:
        image = pinned(item.image)
        spec = bind_references(
            item.spec, image=image, files=self.file_ids, networks=self.network_ids
        )
        spec["TaskTemplate"]["ForceUpdate"] = 0
        spec["TaskTemplate"]["Runtime"] = "container"
        spec["TaskTemplate"]["ContainerSpec"]["Isolation"] = "default"
        return {"ID": f"svc{len(item.name):022d}", "Spec": spec}

    def _network(self, item: Any) -> dict[str, Any]:
        body = item.body
        pools = [
            {**pool, "Gateway": pool.get("Gateway", "10.0.9.1")}
            for pool in body.get("IPAM", {}).get("Config", [{"Subnet": "10.0.9.0/24"}])
        ]
        return {
            "Id": self.network_ids[item.name],
            "Name": item.name,
            "Driver": "overlay",
            "Scope": "swarm",
            "Attachable": body["Attachable"],
            "Internal": body["Internal"],
            "EnableIPv6": body["EnableIPv6"],
            "Ingress": False,
            "ConfigFrom": {"Network": ""},
            "ConfigOnly": False,
            "Labels": body["Labels"],
            "Options": {**body.get("Options", {}), VXLAN: "4097"},
            "IPAM": {"Driver": "default", "Options": None, "Config": pools},
        }

    def _file(self, item: Any, data: str | None) -> dict[str, Any]:
        spec: dict[str, Any] = {"Name": item.name, "Labels": item.labels}
        if data is not None:
            spec["Data"] = data
        return {"ID": self.file_ids[(item.kind, item.name)][0], "Spec": spec}

    def reconstruct(self) -> HostReadResult:
        outcome = reconstruct_stack(
            NAMESPACE, self.services, self.networks, self.configs, self.secrets
        )
        assert isinstance(outcome, HostReadResult), outcome
        return outcome


def resolve(result: HostReadResult) -> tuple[dict[str, Any], dict[str, Source]]:
    """Resolve a reconstructed file the way `config` hands it to the translator."""
    values = {variable.name: variable.value for variable in result.variables}
    files = {item.name: item.content for item in result.files}

    def interpolate(node: Any) -> Any:
        if isinstance(node, str):
            return INTERPOLATION.sub(
                lambda m: "$$" if m[1] is None else values[m[1]].replace("$", "$$"), node
            )
        if isinstance(node, dict):
            return {key: interpolate(value) for key, value in node.items()}
        if isinstance(node, list):
            return [interpolate(item) for item in node]
        return node

    document = interpolate(yaml.safe_load(result.compose_text))
    sources: dict[str, Source] = {}
    for number, (key, entry) in enumerate(sorted(document.get("configs", {}).items()), start=1):
        if "file" in entry:
            name = entry.pop("file").removeprefix("./")
            variable = f"DOCKMIN_SOURCE_{number}"
            entry["environment"] = variable
            sources[variable] = Source("configs", key, files[name], False, "", name)
    document["name"] = "shop"
    document.setdefault("networks", {})
    for service in document["services"].values():
        service.setdefault("networks", {"default": None})
        if "default" in service["networks"]:
            document["networks"].setdefault("default", {"name": "shop_default", "ipam": {}})
    return document, sources


def retranslate(result: HostReadResult) -> Translation:
    document, sources = resolve(result)
    return translated(document, sources)


@pytest.fixture(scope="module")
def cluster() -> Cluster:
    return Cluster()


@pytest.fixture(scope="module")
def result(cluster: Cluster) -> HostReadResult:
    return cluster.reconstruct()


def test_a_reconstructed_namespace_translates_back_to_the_live_specs(
    cluster: Cluster, result: HostReadResult
) -> None:
    again = retranslate(result)

    live = {item["Spec"]["Name"]: item["Spec"] for item in cluster.services}
    assert sorted(live) == [item.name for item in again.services]
    for item in again.services:
        held = live[item.name]
        sent = bind_references(
            item.spec,
            image=held["TaskTemplate"]["ContainerSpec"]["Image"],
            files=cluster.file_ids,
            networks=cluster.network_ids,
        )
        sent["TaskTemplate"]["ForceUpdate"] = 0
        assert not spec_differs(sent, held, cluster.network_ids), item.name


def test_a_reconstructed_namespace_translates_back_to_the_same_networks_configs_and_secrets(
    cluster: Cluster, result: HostReadResult
) -> None:
    again = retranslate(result)

    assert [(n.name, n.external) for n in again.networks] == [
        (n.name, n.external) for n in cluster.translation.networks
    ]
    front = next(n for n in again.networks if n.key == "front")
    assert front.body["Attachable"] is True
    assert front.body["Labels"] == {"tier": "edge", LABEL: NAMESPACE}
    assert front.body["Options"] == {"encrypted": "true"}
    assert [(f.kind, f.name) for f in again.files] == [
        (f.kind, f.name) for f in cluster.translation.files
    ]
    assert [f.external for f in again.files if f.kind == "secret"] == [True]
    live_config = next(f for f in cluster.translation.files if f.kind == "config")
    config = next(f for f in again.files if f.kind == "config")
    assert config.data is not None and live_config.data is not None
    assert config.data.reveal() == live_config.data.reveal()


def test_the_config_becomes_a_plain_companion_file_and_the_secret_an_external_reference(
    cluster: Cluster, result: HostReadResult
) -> None:
    document = yaml.safe_load(result.compose_text)

    [config_file] = result.files
    assert config_file.name == "config-cfg"
    assert config_file.content == b"listen 80;\n"
    assert config_file.secret is False
    assert document["configs"] == {"cfg": {"file": "./config-cfg"}}
    [secret_name] = [f.name for f in cluster.translation.files if f.kind == "secret"]
    assert document["secrets"] == {"sec": {"external": True, "name": secret_name}}


def test_secret_named_environment_values_move_to_secret_variables(result: HostReadResult) -> None:
    web = yaml.safe_load(result.compose_text)["services"]["web"]

    assert web["environment"]["API_TOKEN"] == "${API_TOKEN}"
    assert web["environment"]["PRICE"] == "$$5"
    assert [(v.name, v.value, v.secret) for v in result.variables] == [
        ("API_TOKEN", SECRET_VALUE, True)
    ]
    assert SECRET_VALUE not in result.compose_text


def test_the_text_keys_and_source_are_the_namespace_without_its_prefix(
    result: HostReadResult,
) -> None:
    document = yaml.safe_load(result.compose_text)

    assert result.file_source == "reconstructed"
    assert sorted(document["services"]) == ["api", "web"]
    assert sorted(document["networks"]) == ["default", "front"]
    assert document["volumes"] == {"data": {}}
    assert result.losses[: len(KNOWN_LOSSES)] == KNOWN_LOSSES


def test_image_references_are_written_without_their_digest_pin(result: HostReadResult) -> None:
    services = yaml.safe_load(result.compose_text)["services"]

    assert services["web"]["image"] == WEB_IMAGE
    assert services["api"]["image"] == API_IMAGE


def test_a_service_attached_only_to_the_default_network_has_no_networks_key(
    result: HostReadResult,
) -> None:
    assert "networks" not in yaml.safe_load(result.compose_text)["services"]["api"]


def attach_mutation(cluster: Cluster, mutate: Any) -> HostReadResult | NeedsFile:
    services = copy.deepcopy(cluster.services)
    mutate(next(item["Spec"] for item in services if item["Spec"]["Name"] == "Shop_web"))
    return reconstruct_stack(
        NAMESPACE, services, cluster.networks, cluster.configs, cluster.secrets
    )


def test_a_job_mode_service_makes_the_namespace_need_a_file_and_names_the_field(
    cluster: Cluster,
) -> None:
    def make_job(spec: dict[str, Any]) -> None:
        spec["Mode"] = {"ReplicatedJob": {"MaxConcurrent": 1, "TotalCompletions": 3}}

    outcome = attach_mutation(cluster, make_job)

    assert outcome == NeedsFile("no_translator_row", "services.web.Mode.ReplicatedJob")


def test_an_empty_job_mode_is_still_a_job_mode(cluster: Cluster) -> None:
    outcome = attach_mutation(cluster, lambda spec: spec.update(Mode={"GlobalJob": {}}))

    assert outcome == NeedsFile("no_translator_row", "services.web.Mode.GlobalJob")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("TaskTemplate.ContainerSpec.PidsLimit", 5),
        ("TaskTemplate.ContainerSpec.Privileges.CredentialSpec", {"File": "spec.json"}),
        ("TaskTemplate.PluginSpec", {"Name": "plugin"}),
        ("TaskTemplate.Placement.Unknown", ["x"]),
        ("TaskTemplate.Resources.Limits.Mystery", 4),
        ("Annotations", {"Name": "x"}),
    ],
)
def test_a_spec_field_without_a_translator_row_names_the_field(
    cluster: Cluster, path: str, value: Any
) -> None:
    def add(spec: dict[str, Any]) -> None:
        node: Any = spec
        *parents, leaf = path.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value

    outcome = attach_mutation(cluster, add)

    assert outcome == NeedsFile("no_translator_row", f"services.web.{path}")


def test_engine_defaults_and_zero_values_are_not_fields_without_a_row(cluster: Cluster) -> None:
    def add_defaults(spec: dict[str, Any]) -> None:
        task = spec["TaskTemplate"]
        task["Placement"]["Platforms"] = [{"Architecture": "amd64", "OS": "linux"}]
        task["ContainerSpec"]["Privileges"] = {
            "CredentialSpec": None,
            "SELinuxContext": None,
            "NoNewPrivileges": False,
        }
        task["ContainerSpec"]["DNSConfig"] = {}
        task["Resources"]["Reservations"] = {**task["Resources"]["Reservations"], "NanoCPUs": 0}

    outcome = attach_mutation(cluster, add_defaults)

    assert isinstance(outcome, HostReadResult)


def test_a_spec_with_no_isolation_writes_no_isolation_key(cluster: Cluster) -> None:
    def drop_isolation(spec: dict[str, Any]) -> None:
        del spec["TaskTemplate"]["ContainerSpec"]["Isolation"]

    outcome = attach_mutation(cluster, drop_isolation)

    assert isinstance(outcome, HostReadResult)
    assert "isolation" not in yaml.safe_load(outcome.compose_text)["services"]["web"]


def test_a_namespace_with_no_service_needs_a_file(cluster: Cluster) -> None:
    outcome = reconstruct_stack(NAMESPACE, [], cluster.networks, cluster.configs, cluster.secrets)

    assert outcome == NeedsFile("no_services", NAMESPACE)


def test_services_of_another_namespace_are_not_members(cluster: Cluster) -> None:
    other = copy.deepcopy(cluster.services[0])
    other["Spec"]["Labels"][LABEL] = "Other"
    other["Spec"]["Name"] = "Other_web"

    outcome = reconstruct_stack(
        NAMESPACE, [*cluster.services, other], cluster.networks, cluster.configs, cluster.secrets
    )

    assert isinstance(outcome, HostReadResult)
    assert sorted(yaml.safe_load(outcome.compose_text)["services"]) == ["api", "web"]


def test_the_namespace_matches_its_label_ignoring_case(cluster: Cluster) -> None:
    outcome = reconstruct_stack(
        "shop", cluster.services, cluster.networks, cluster.configs, cluster.secrets
    )

    assert isinstance(outcome, HostReadResult)


def test_a_service_name_without_the_namespace_prefix_needs_a_file(cluster: Cluster) -> None:
    outcome = attach_mutation(cluster, lambda spec: spec.update(Name="unprefixed"))

    assert outcome == NeedsFile("name_without_namespace", "services.unprefixed")


def test_a_network_that_cannot_be_resolved_needs_a_file_naming_the_attachment(
    cluster: Cluster,
) -> None:
    outcome = reconstruct_stack(NAMESPACE, cluster.services, [], cluster.configs, cluster.secrets)

    assert isinstance(outcome, NeedsFile)
    assert outcome.reason == "network_unresolved"
    assert outcome.path.startswith("services.")
    assert outcome.path.endswith(".Target")


def test_a_network_without_the_namespace_label_is_an_external_reference(
    cluster: Cluster,
) -> None:
    networks = copy.deepcopy(cluster.networks)
    front = next(n for n in networks if n["Name"] == "Shop_front")
    front["Labels"] = {}

    outcome = reconstruct_stack(
        NAMESPACE, cluster.services, networks, cluster.configs, cluster.secrets
    )

    assert isinstance(outcome, HostReadResult)
    document = yaml.safe_load(outcome.compose_text)
    assert document["networks"]["Shop_front"] == {"external": True}
    assert "Shop_front" in document["services"]["web"]["networks"]


def test_a_project_network_with_another_driver_needs_a_file(cluster: Cluster) -> None:
    networks = copy.deepcopy(cluster.networks)
    networks[0]["Driver"] = "bridge"

    outcome = reconstruct_stack(
        NAMESPACE, cluster.services, networks, cluster.configs, cluster.secrets
    )

    assert isinstance(outcome, NeedsFile)
    assert outcome.path.endswith(".Driver")


def test_a_config_whose_data_cannot_be_read_needs_a_file(cluster: Cluster) -> None:
    configs = copy.deepcopy(cluster.configs)
    configs[0]["Spec"]["Data"] = "not base64 !!"

    outcome = reconstruct_stack(
        NAMESPACE, cluster.services, cluster.networks, configs, cluster.secrets
    )

    assert isinstance(outcome, NeedsFile)
    assert outcome.reason == "config_unreadable"


def test_a_config_holding_a_binary_file_needs_a_file_naming_it(cluster: Cluster) -> None:
    configs = copy.deepcopy(cluster.configs)
    configs[0]["Spec"]["Data"] = base64.b64encode(b"\xff\xfe\x00binary").decode("ascii")

    outcome = reconstruct_stack(
        NAMESPACE, cluster.services, cluster.networks, configs, cluster.secrets
    )

    assert isinstance(outcome, NeedsFile)
    assert outcome.reason == "invalid"
    assert outcome.path.startswith("configs.cfg")


def test_a_config_without_the_namespace_label_is_an_external_reference(cluster: Cluster) -> None:
    configs = copy.deepcopy(cluster.configs)
    configs[0]["Spec"]["Labels"] = {}

    outcome = reconstruct_stack(
        NAMESPACE, cluster.services, cluster.networks, configs, cluster.secrets
    )

    assert isinstance(outcome, HostReadResult)
    assert outcome.files == ()
    config_name = configs[0]["Spec"]["Name"]
    assert yaml.safe_load(outcome.compose_text)["configs"] == {config_name: {"external": True}}


def test_a_secret_value_that_cannot_be_a_variable_needs_a_file_without_the_value(
    cluster: Cluster,
) -> None:
    def break_value(spec: dict[str, Any]) -> None:
        spec["TaskTemplate"]["ContainerSpec"]["Env"].append("DB_PASSWORD=two\nlines")

    outcome = attach_mutation(cluster, break_value)

    assert outcome == NeedsFile(
        "secret_value_unusable", "services.web.TaskTemplate.ContainerSpec.Env.DB_PASSWORD"
    )


def test_an_environment_entry_with_no_value_needs_a_file(cluster: Cluster) -> None:
    outcome = attach_mutation(
        cluster, lambda spec: spec["TaskTemplate"]["ContainerSpec"]["Env"].append("INHERITED")
    )

    assert outcome == NeedsFile("no_translator_row", "services.web.TaskTemplate.ContainerSpec.Env")


def test_a_duration_the_translator_would_refuse_needs_a_file(cluster: Cluster) -> None:
    def too_long(spec: dict[str, Any]) -> None:
        spec["TaskTemplate"]["ContainerSpec"]["StopGracePeriod"] = 48 * 3600 * 1_000_000_000

    outcome = attach_mutation(cluster, too_long)

    assert outcome == NeedsFile(
        "duration_out_of_range", "services.web.TaskTemplate.ContainerSpec.StopGracePeriod"
    )


def test_reconstruction_does_not_change_the_objects_it_reads(cluster: Cluster) -> None:
    before = copy.deepcopy((cluster.services, cluster.networks, cluster.configs, cluster.secrets))

    reconstruct_stack(
        NAMESPACE, cluster.services, cluster.networks, cluster.configs, cluster.secrets
    )

    assert (cluster.services, cluster.networks, cluster.configs, cluster.secrets) == before
