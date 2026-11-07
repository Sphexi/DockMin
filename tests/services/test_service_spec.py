"""Service form parsing, spec building and merging, change categories and form values."""

import copy
import json
from typing import Any

import pytest

from app.auth.errors import ValidationFailed
from app.services import service_spec as ss
from app.services.service_restrictions import find_restricted_service, pinned_to
from tests.swarm_support import NAMESPACE_LABEL, NODE, sample_json, sample_specs

Spec = dict[str, Any]
LATEST = "busybox:latest"
BARE = {"name": "web", "image": "nginx"}
NO_REFS = ss.ResolvedRefs(networks={}, configs={}, secrets={}, image=LATEST)
NETWORK_ID = "n" * 25
CONFIG_ID = "c" * 25
SECRET_ID = "s" * 25
DIGEST = "a" * 64

FULL_FORM = {
    "name": "web",
    "image": "nginx",
    "mode": "replicated",
    "replicas": "3",
    "command": "sh -c 'echo hi'",
    "env-0-key": "A",
    "env-0-value": "1",
    "env-1-key": "TOKEN",
    "env-1-value": "s3",
    "label-0-key": "app",
    "label-0-value": "w",
    "constraints": "node.role==worker\nnode.labels.zone != a",
    "preferences": "spread=node.labels.zone",
    "max_replicas_per_node": "2",
    "cpu_limit": "0.5",
    "memory_limit": "128m",
    "cpu_reservation": "0.1",
    "memory_reservation": "32m",
    "pids_limit": "100",
    "restart_condition": "on-failure",
    "restart_delay": "5s",
    "restart_max_attempts": "3",
    "restart_window": "10s",
    "update_parallelism": "1",
    "update_delay": "1s",
    "update_failure_action": "pause",
    "update_monitor": "2s",
    "update_max_failure_ratio": "0.1",
    "update_order": "start-first",
    "rollback_parallelism": "0",
    "rollback_delay": "0s",
    "rollback_failure_action": "pause",
    "endpoint_mode": "vip",
    "network-0-name": "backend",
    "network-0-aliases": "api web",
    "port-0-target": "80",
    "port-0-published": "8080",
    "port-0-protocol": "tcp",
    "port-0-mode": "ingress",
    "port-1-target": "53",
    "port-1-protocol": "udp",
    "port-1-mode": "host",
    "mount-0-type": "volume",
    "mount-0-source": "data",
    "mount-0-target": "/data",
    "mount-0-read_only": "on",
    "mount-1-type": "tmpfs",
    "mount-1-target": "/tmp",
    "mount-1-tmpfs_size": "64m",
    "mount-2-type": "volume",
    "mount-2-target": "/anon",
    "config-0-name": "cfg",
    "config-0-target": "",
    "config-0-mode": "0440",
    "secret-0-name": "sec",
    "secret-0-uid": "1000",
    "secret-0-gid": "1000",
}
FULL_REFS = ss.ResolvedRefs(
    networks={0: NETWORK_ID},
    configs={0: (CONFIG_ID, "cfg")},
    secrets={0: (SECRET_ID, "sec")},
    image=f"nginx:latest@sha256:{DIGEST}",
    manager_node_id=NODE,
)


def parse(form: dict[str, Any], *, creating: bool = True, admin: bool = False) -> ss.ServiceInput:
    return ss.parse_service_form(form, creating=creating, administrator=admin)


def errors(form: dict[str, Any], *, creating: bool = True, admin: bool = False) -> dict[str, str]:
    with pytest.raises(ValidationFailed) as raised:
        parse(form, creating=creating, admin=admin)
    return dict(raised.value.fields)


def build(form: dict[str, Any], refs: ss.ResolvedRefs = NO_REFS) -> Spec:
    return ss.build_spec(parse(form), refs)


def refs_for(spec: Spec) -> ss.ResolvedRefs:
    task = spec["TaskTemplate"]
    container = task["ContainerSpec"]
    return ss.ResolvedRefs(
        networks={i: n["Target"] for i, n in enumerate(task.get("Networks") or [])},
        configs={
            i: (e["ConfigID"], e["ConfigName"])
            for i, e in enumerate(container.get("Configs") or [])
            if "File" in e
        },
        secrets={
            i: (e["SecretID"], e["SecretName"])
            for i, e in enumerate(container.get("Secrets") or [])
            if "File" in e
        },
        image=container["Image"],
    )


def stripped(spec: Spec) -> Any:
    return ss._strip(spec)


def sample_spec() -> Spec:
    return copy.deepcopy(sample_json("05-GET-services-list-status.bin")[0]["Spec"])


@pytest.mark.parametrize(
    ("label", "form", "field"),
    [
        ("name required", {"image": "x"}, "name"),
        ("name with a period", {**BARE, "name": "a.b"}, "name"),
        ("name with a leading hyphen", {**BARE, "name": "-a"}, "name"),
        ("name with a trailing underscore", {**BARE, "name": "a_"}, "name"),
        ("name of 64 characters", {**BARE, "name": "a" * 64}, "name"),
        ("name shaped like an id", {**BARE, "name": "a" * 25}, "name"),
        (
            "name shaped like an id with digits",
            {**BARE, "name": "abcde12345" * 2 + "abcde"},
            "name",
        ),
        ("image required", {"name": "w"}, "image"),
        ("image malformed", {**BARE, "image": "UPPER Case"}, "image"),
        ("mode unknown", {**BARE, "mode": "job"}, "mode"),
        ("replicas over the standard ceiling", {**BARE, "replicas": "101"}, "replicas"),
        ("replicas negative", {**BARE, "replicas": "-1"}, "replicas"),
        ("replicas with global mode", {**BARE, "mode": "global", "replicas": "1"}, "replicas"),
        ("command with an unbalanced quote", {**BARE, "command": "sh -c 'x"}, "command"),
        ("environment name repeated", {**BARE, "env-0-key": "A", "env-1-key": "A"}, "env-1-key"),
        ("environment name with an equals sign", {**BARE, "env-0-key": "A=B"}, "env-0-key"),
        ("environment value without a name", {**BARE, "env-0-value": "x"}, "env-0-key"),
        (
            "environment value with NUL",
            {**BARE, "env-0-key": "A", "env-0-value": "x\0"},
            "env-0-value",
        ),
        ("101 environment rows", {**BARE, **{f"env-{i}-key": f"K{i}" for i in range(101)}}, "env"),
        ("environment over 64 KiB", {**BARE, "env-0-key": "A", "env-0-value": "v" * 70000}, "env"),
        ("reserved label", {**BARE, "label-0-key": NAMESPACE_LABEL}, "label-0-key"),
        (
            "reserved label padded and recased",
            {**BARE, "label-0-key": " COM.docker.compose.Project "},
            "label-0-key",
        ),
        ("label key repeated", {**BARE, "label-0-key": "a", "label-1-key": "a"}, "label-1-key"),
        (
            "labels over 64 KiB",
            {**BARE, "label-0-key": "a", "label-0-value": "v" * 70000},
            "labels",
        ),
        ("constraint without an operator", {**BARE, "constraints": "node.role"}, "constraints"),
        (
            "33 constraints",
            {**BARE, "constraints": "\n".join(f"node.id==a{i}" for i in range(33))},
            "constraints",
        ),
        (
            "constraint over 256 characters",
            {**BARE, "constraints": "node.id==" + "a" * 250},
            "constraints",
        ),
        (
            "constraint value with an equals sign",
            {**BARE, "constraints": "node.role==a=b"},
            "constraints",
        ),
        ("preference that is not a spread", {**BARE, "preferences": "binpack=x"}, "preferences"),
        (
            "nine preferences",
            {**BARE, "preferences": "\n".join(f"spread=a{i}" for i in range(9))},
            "preferences",
        ),
        ("cpu limit below the minimum", {**BARE, "cpu_limit": "0.0001"}, "cpu_limit"),
        ("cpu limit as text", {**BARE, "cpu_limit": "lots"}, "cpu_limit"),
        ("memory limit below 4 MiB", {**BARE, "memory_limit": "1m"}, "memory_limit"),
        ("memory limit as text", {**BARE, "memory_limit": "x"}, "memory_limit"),
        (
            "reservation over the limit",
            {**BARE, "memory_limit": "10m", "memory_reservation": "20m"},
            "memory_reservation",
        ),
        ("pids limit of zero", {**BARE, "pids_limit": "0"}, "pids_limit"),
        ("unknown restart condition", {**BARE, "restart_condition": "always"}, "restart_condition"),
        ("restart delay as text", {**BARE, "restart_delay": "soon"}, "restart_delay"),
        (
            "unknown update failure action",
            {**BARE, "update_failure_action": "stop"},
            "update_failure_action",
        ),
        (
            "rollback failure action of rollback",
            {**BARE, "rollback_failure_action": "rollback"},
            "rollback_failure_action",
        ),
        ("unknown update order", {**BARE, "update_order": "random"}, "update_order"),
        (
            "failure ratio over one",
            {**BARE, "update_max_failure_ratio": "1.5"},
            "update_max_failure_ratio",
        ),
        (
            "failure ratio of NaN",
            {**BARE, "rollback_max_failure_ratio": "NaN"},
            "rollback_max_failure_ratio",
        ),
        ("unknown endpoint mode", {**BARE, "endpoint_mode": "none"}, "endpoint_mode"),
        ("network name with a space", {**BARE, "network-0-name": "a b"}, "network-0-name"),
        (
            "network repeated",
            {**BARE, "network-0-name": "a", "network-1-name": "a"},
            "network-1-name",
        ),
        (
            "network alias with an underscore",
            {**BARE, "network-0-name": "a", "network-0-aliases": "x_y"},
            "network-0-aliases",
        ),
        ("port target of zero", {**BARE, "port-0-target": "0"}, "port-0-target"),
        (
            "published port over 65535",
            {**BARE, "port-0-target": "1", "port-0-published": "70000"},
            "port-0-published",
        ),
        (
            "unknown protocol",
            {**BARE, "port-0-target": "1", "port-0-protocol": "icmp"},
            "port-0-protocol",
        ),
        (
            "unknown publish mode",
            {**BARE, "port-0-target": "1", "port-0-mode": "mesh"},
            "port-0-mode",
        ),
        (
            "published port twice",
            {
                **BARE,
                "port-0-target": "1",
                "port-0-published": "80",
                "port-1-target": "2",
                "port-1-published": "80",
            },
            "port-1-published",
        ),
        (
            "ingress port on a dnsrr endpoint",
            {**BARE, "endpoint_mode": "dnsrr", "port-0-target": "1"},
            "port-0-mode",
        ),
        (
            "mount type the form does not model",
            {**BARE, "mount-0-type": "image", "mount-0-target": "/a"},
            "mount-0-type",
        ),
        ("relative mount target", {**BARE, "mount-0-target": "a"}, "mount-0-target"),
        ("mount target of root", {**BARE, "mount-0-target": "/"}, "mount-0-target"),
        ("mount target with dot-dot", {**BARE, "mount-0-target": "/a/../b"}, "mount-0-target"),
        (
            "mount target repeated",
            {**BARE, "mount-0-target": "/a", "mount-1-target": "/a"},
            "mount-1-target",
        ),
        (
            "bind source relative",
            {**BARE, "mount-0-type": "bind", "mount-0-source": "rel", "mount-0-target": "/a"},
            "mount-0-source",
        ),
        (
            "tmpfs with a source",
            {**BARE, "mount-0-type": "tmpfs", "mount-0-source": "x", "mount-0-target": "/a"},
            "mount-0-source",
        ),
        (
            "tmpfs size on a volume",
            {**BARE, "mount-0-target": "/a", "mount-0-tmpfs_size": "1m"},
            "mount-0-tmpfs_size",
        ),
        (
            "volume source with a space",
            {**BARE, "mount-0-source": "a b", "mount-0-target": "/a"},
            "mount-0-source",
        ),
        ("only a mount checkbox ticked", {**BARE, "mount-0-read_only": "on"}, "mount-0-target"),
        ("config name with a space", {**BARE, "config-0-name": "bad name"}, "config-0-name"),
        (
            "config target with dot-dot",
            {**BARE, "config-0-name": "c", "config-0-target": "/a/../b"},
            "config-0-target",
        ),
        (
            "config mode with a non-octal digit",
            {**BARE, "config-0-name": "c", "config-0-mode": "0999"},
            "config-0-mode",
        ),
        (
            "config uid as text",
            {**BARE, "config-0-name": "c", "config-0-uid": "root"},
            "config-0-uid",
        ),
        (
            "config target repeated",
            {**BARE, "config-0-name": "a", "config-1-name": "a"},
            "config-1-target",
        ),
        (
            "secret target with dot-dot",
            {**BARE, "secret-0-name": "s", "secret-0-target": "../x"},
            "secret-0-target",
        ),
        ("secret name of 65 characters", {**BARE, "secret-0-name": "s" * 65}, "secret-0-name"),
        ("65 mounts", {**BARE, **{f"mount-{i}-target": f"/m{i}" for i in range(65)}}, "mounts"),
    ],
)
def test_a_form_value_outside_the_grammar_is_a_field_error_on_that_field(
    label: str, form: dict[str, str], field: str
) -> None:
    assert field in errors(form), label


def test_every_field_error_found_is_returned_together() -> None:
    assert len(errors({"name": "-", "image": "", "replicas": "x"})) >= 2


def test_replicas_up_to_1000_are_allowed_for_an_administrator_and_100_for_anyone() -> None:
    assert parse({**BARE, "replicas": "1000"}, admin=True).replicas == ss.MAX_REPLICAS
    assert parse({**BARE, "replicas": "100"}).replicas == ss.MAX_REPLICAS_STANDARD
    assert "replicas" in errors({**BARE, "replicas": "1001"}, admin=True)


def test_parse_replicas_reports_an_out_of_range_count_keyed_replicas() -> None:
    with pytest.raises(ValidationFailed) as raised:
        ss.parse_replicas("101", administrator=False)

    assert ss.parse_replicas("0", administrator=False) == 0
    assert "replicas" in raised.value.fields


def test_an_update_ignores_the_name_field() -> None:
    parsed = parse({"image": "x", "name": "!!"}, creating=False)

    assert parsed.name == ""


def test_a_bind_mount_parses_for_everyone_because_the_walker_decides() -> None:
    form = {**BARE, "mount-0-type": "bind", "mount-0-source": "/etc", "mount-0-target": "/a"}

    assert parse(form).mounts[0].type == "bind"


@pytest.mark.parametrize("image", ["sha256:" + "a" * 64, f"nginx@sha256:{DIGEST}"])
def test_an_image_id_and_a_digest_reference_are_accepted(image: str) -> None:
    assert parse({**BARE, "image": image}).image.endswith("a" * 64)


def test_blank_rows_and_spare_defaulted_columns_are_skipped() -> None:
    parsed = parse(
        {
            **BARE,
            "env-3-key": "",
            "env-3-value": "",
            "mount-0-type": "volume",
            "port-0-protocol": "tcp",
        }
    )

    assert (parsed.env, parsed.mounts, parsed.ports) == ((), (), ())


def test_a_row_keeps_the_slot_it_was_posted_under() -> None:
    assert [r.slot for r in parse({**BARE, "env-4-key": "a", "env-9-key": "b"}).env] == [4, 9]


def test_form_field_names_that_are_not_row_fields_are_ignored() -> None:
    hostile = {**BARE, "env-99999-key": "x", "env--1-key": "y", "../x": "z"}

    assert parse(hostile).env == ()


def test_values_that_are_not_text_are_ignored() -> None:
    odd: dict[str, Any] = {
        "name": "w",
        "image": "x",
        "env-0-key": None,
        "replicas": 3,
        "command": ["a"],
    }

    assert parse(odd).env == ()


def test_a_fully_loaded_form_builds_a_spec_the_walker_accepts() -> None:
    assert find_restricted_service(build(FULL_FORM, FULL_REFS)) == []


def test_a_fully_loaded_form_builds_exactly_the_keys_the_form_manages() -> None:
    built = build(FULL_FORM, FULL_REFS)
    task = built["TaskTemplate"]
    container = task["ContainerSpec"]

    assert set(built) == {
        "Name",
        "Labels",
        "TaskTemplate",
        "Mode",
        "UpdateConfig",
        "RollbackConfig",
        "EndpointSpec",
    }
    assert container["Image"].endswith(DIGEST)
    assert container["Args"] == ["sh", "-c", "echo hi"]
    assert container["Env"] == ["A=1", "TOKEN=s3"]
    assert built["Labels"] == {"app": "w"}
    assert built["Mode"] == {"Replicated": {"Replicas": 3}}
    assert task["Networks"] == [{"Target": NETWORK_ID, "Aliases": ["api", "web"]}]


def test_resources_restart_and_placement_are_written_in_the_engines_units() -> None:
    task = build(FULL_FORM, FULL_REFS)["TaskTemplate"]

    assert task["Resources"]["Limits"] == {
        "NanoCPUs": 500_000_000,
        "MemoryBytes": 128 * 1024**2,
        "Pids": 100,
    }
    assert task["Resources"]["Reservations"] == {
        "NanoCPUs": 100_000_000,
        "MemoryBytes": 32 * 1024**2,
    }
    assert task["RestartPolicy"] == {
        "Condition": "on-failure",
        "Delay": 5 * 10**9,
        "MaxAttempts": 3,
        "Window": 10 * 10**9,
    }
    assert task["Placement"] == {
        "Constraints": ["node.role==worker", "node.labels.zone != a"],
        "Preferences": [{"Spread": {"SpreadDescriptor": "node.labels.zone"}}],
        "MaxReplicas": 2,
    }


def test_rollout_ports_and_mounts_are_written_and_zero_values_are_kept() -> None:
    built = build(FULL_FORM, FULL_REFS)
    container = built["TaskTemplate"]["ContainerSpec"]

    assert built["UpdateConfig"] == {
        "Parallelism": 1,
        "Delay": 10**9,
        "FailureAction": "pause",
        "Monitor": 2 * 10**9,
        "MaxFailureRatio": 0.1,
        "Order": "start-first",
    }
    assert built["RollbackConfig"] == {"Parallelism": 0, "Delay": 0, "FailureAction": "pause"}
    assert built["EndpointSpec"] == {
        "Mode": "vip",
        "Ports": [
            {"TargetPort": 80, "Protocol": "tcp", "PublishMode": "ingress", "PublishedPort": 8080},
            {"TargetPort": 53, "Protocol": "udp", "PublishMode": "host"},
        ],
    }
    assert container["Mounts"] == [
        {"Type": "volume", "Target": "/data", "Source": "data", "ReadOnly": True},
        {"Type": "tmpfs", "Target": "/tmp", "TmpfsOptions": {"SizeBytes": 64 * 1024**2}},
        {"Type": "volume", "Target": "/anon"},
    ]


def test_configs_and_secrets_carry_the_resolved_id_and_name_with_their_file_mode() -> None:
    container = build(FULL_FORM, FULL_REFS)["TaskTemplate"]["ContainerSpec"]

    assert container["Configs"] == [
        {
            "File": {"Name": "/cfg", "UID": "0", "GID": "0", "Mode": 0o440},
            "ConfigID": CONFIG_ID,
            "ConfigName": "cfg",
        }
    ]
    assert container["Secrets"] == [
        {
            "File": {"Name": "sec", "UID": "1000", "GID": "1000", "Mode": 0o444},
            "SecretID": SECRET_ID,
            "SecretName": "sec",
        }
    ]


def test_the_smallest_form_builds_the_smallest_spec() -> None:
    assert ss.build_spec(parse({"name": "m", "image": "busybox"}), NO_REFS) == {
        "Name": "m",
        "TaskTemplate": {"ContainerSpec": {"Image": LATEST}},
        "Mode": {"Replicated": {"Replicas": 1}},
    }


def test_global_mode_builds_an_empty_global_object() -> None:
    spec = build({"name": "g", "image": "busybox", "mode": "global"})

    assert spec["Mode"] == {"Global": {}}


def test_a_built_spec_is_json_serialisable_and_holds_no_null() -> None:
    encoded = json.dumps(build(FULL_FORM, FULL_REFS))

    assert "null" not in encoded


def test_networks_are_resolved_by_slot_and_the_resolved_id_is_the_target() -> None:
    refs = ss.ResolvedRefs({0: "a" * 25, 1: "b" * 64}, {}, {}, "x:1")

    spec = build(
        {"name": "w", "image": "x", "network-0-name": "net", "network-1-name": "other"}, refs
    )

    assert [n["Target"] for n in spec["TaskTemplate"]["Networks"]] == ["a" * 25, "b" * 64]


def test_run_on_manager_adds_a_pin_to_the_connected_manager_once() -> None:
    refs = ss.ResolvedRefs({}, {}, {}, LATEST, NODE)
    plain = build({**BARE, "run_on_manager": "on", "constraints": "node.role==manager"}, refs)
    already = build({**BARE, "run_on_manager": "on", "constraints": f"node.id == {NODE}"}, refs)

    assert plain["TaskTemplate"]["Placement"]["Constraints"] == [
        "node.role==manager",
        f"node.id=={NODE}",
    ]
    assert pinned_to(plain, NODE)
    assert len(already["TaskTemplate"]["Placement"]["Constraints"]) == 1


def test_run_on_manager_without_a_known_manager_node_is_a_field_error() -> None:
    with pytest.raises(ValidationFailed) as raised:
        build({**BARE, "run_on_manager": "on"})

    assert "run_on_manager" in raised.value.fields


def test_a_network_the_service_layer_did_not_resolve_is_an_error_on_its_row() -> None:
    with pytest.raises(ValidationFailed) as raised:
        build({**BARE, "network-0-name": "x"})

    assert "network-0-name" in raised.value.fields


def mount_with_labels(labels: dict[str, str]) -> Spec:
    return {
        "Name": "s",
        "TaskTemplate": {
            "ContainerSpec": {
                "Image": "x",
                "Mounts": [
                    {
                        "Type": "volume",
                        "Source": "d",
                        "Target": "/d",
                        "VolumeOptions": {"Labels": labels},
                    }
                ],
            }
        },
    }


def label_fields(spec: Spec, baseline: Spec | None = None) -> dict[str, str] | None:
    try:
        ss.check_labels(spec, baseline)
    except ValidationFailed as error:
        return dict(error.fields)
    return None


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param({"Labels": {NAMESPACE_LABEL: "x"}}, id="service"),
        pytest.param(
            {"TaskTemplate": {"ContainerSpec": {"Labels": {NAMESPACE_LABEL: "x"}}}}, id="container"
        ),
        pytest.param({"Labels": {" COM.Docker.Compose.PROJECT ": "x"}}, id="compose label recased"),
        pytest.param(mount_with_labels({NAMESPACE_LABEL: "x"}), id="mount volume options"),
        pytest.param(mount_with_labels({NAMESPACE_LABEL.upper(): "x"}), id="mount recased"),
    ],
)
def test_a_reserved_label_anywhere_a_label_reaches_docker_is_refused(spec: Spec) -> None:
    assert label_fields(spec) is not None


def test_a_plain_mount_label_is_allowed() -> None:
    assert label_fields(mount_with_labels({"a": "b"})) is None


def test_a_reserved_label_carried_unchanged_from_the_baseline_is_allowed() -> None:
    carried = mount_with_labels({NAMESPACE_LABEL: "stack"})

    assert label_fields(carried, copy.deepcopy(carried)) is None


def test_a_reserved_label_with_a_changed_value_or_a_new_place_is_refused() -> None:
    baseline = mount_with_labels({NAMESPACE_LABEL: "stack"})

    assert label_fields(mount_with_labels({NAMESPACE_LABEL: "other"}), baseline) is not None
    assert label_fields({"Labels": {NAMESPACE_LABEL: "stack"}}, baseline) is not None


def test_labels_are_capped_at_64_kib_across_service_and_mount_together() -> None:
    both = mount_with_labels({"m": "v" * (40 * 1024)})
    both["Labels"] = {"s": "v" * (40 * 1024)}

    assert label_fields({"Labels": {"k": "v" * (64 * 1024)}}) is not None
    assert label_fields(both) is not None
    assert label_fields({"Labels": {"k": "v" * (64 * 1024 - 2)}}) is None


def test_merging_what_the_form_showed_changes_nothing_in_any_engine_returned_spec() -> None:
    for admin in (True, False):
        for _, _, spec in sample_specs():
            values = ss.form_values(spec, administrator=admin)
            merged = ss.merge_spec(spec, parse(values, creating=False, admin=admin), refs_for(spec))

            assert stripped(merged) == stripped(spec)
            assert ss.changed_categories(spec, merged) == ()


def rich_spec() -> Spec:
    rich = sample_spec()
    container = rich["TaskTemplate"]["ContainerSpec"]
    container.update(
        {
            "Healthcheck": {"Test": ["CMD", "x"]},
            "User": "1000",
            "Hostname": "h",
            "CapabilityDrop": ["ALL"],
            "Command": ["sh", "-c"],
            "Hosts": ["1.1.1.1 a"],
            "Privileges": {"CredentialSpec": None, "SELinuxContext": None},
        }
    )
    rich["TaskTemplate"]["Placement"]["Platforms"] = [{"Architecture": "amd64", "OS": "linux"}]
    rich["TaskTemplate"]["Resources"]["Reservations"]["GenericResources"] = [
        {"DiscreteResourceSpec": {"Kind": "gpu", "Value": 1}}
    ]
    container["Mounts"] = [
        {
            "Type": "volume",
            "Source": "data",
            "Target": "/d",
            "VolumeOptions": {"NoCopy": True, "Labels": {"a": "b"}, "DriverConfig": {"Name": "x"}},
        },
        {"Type": "npipe", "Source": "p", "Target": "/p"},
        {
            "Type": "bind",
            "Source": "/etc",
            "Target": "/e",
            "BindOptions": {"Propagation": "rshared"},
        },
    ]
    rich["TaskTemplate"]["Networks"] = [
        {"Target": "n" * 25, "Aliases": ["a"], "DriverOpts": {"o": "v"}}
    ]
    rich["EndpointSpec"]["Ports"][0]["Name"] = "http"
    return rich


def test_fields_the_form_does_not_model_survive_an_update_untouched() -> None:
    rich = rich_spec()
    values = ss.form_values(rich, administrator=True)

    merged = ss.merge_spec(rich, parse(values, creating=False, admin=True), refs_for(rich))

    assert stripped(merged) == stripped(rich)
    assert merged["Name"] == rich["Name"]
    assert not any(v == "npipe" for v in values.values())


def test_a_changed_field_is_written_and_the_rest_is_kept() -> None:
    rich = rich_spec()
    values = {**ss.form_values(rich, administrator=True), "replicas": "5", "image": "other:2"}
    refs = ss.ResolvedRefs(
        refs_for(rich).networks, refs_for(rich).configs, refs_for(rich).secrets, "other:2"
    )

    merged = ss.merge_spec(rich, parse(values, creating=False, admin=True), refs)

    container = merged["TaskTemplate"]["ContainerSpec"]
    assert merged["Mode"]["Replicated"]["Replicas"] == 5
    assert container["Image"] == "other:2"
    assert container["Healthcheck"] == {"Test": ["CMD", "x"]}
    assert container["Mounts"][0]["VolumeOptions"]["DriverConfig"] == {"Name": "x"}


def test_merging_leaves_its_input_alone() -> None:
    rich = rich_spec()
    before = copy.deepcopy(rich)

    ss.merge_spec(
        rich,
        parse(ss.form_values(rich, administrator=True), creating=False, admin=True),
        refs_for(rich),
    )

    assert rich == before


def test_a_job_mode_is_not_on_the_form_and_survives_a_merge() -> None:
    job = sample_spec()
    job["Mode"] = {"ReplicatedJob": {"MaxConcurrent": 1, "TotalCompletions": 3}}
    values = ss.form_values(job, administrator=True)

    merged = ss.merge_spec(
        job, parse({**values, "image": "x"}, creating=False, admin=True), refs_for(job)
    )

    assert "mode" not in values and "replicas" not in values
    assert merged["Mode"] == job["Mode"]


def test_a_reserved_volume_label_already_on_a_mount_survives_an_update() -> None:
    rich = rich_spec()
    rich["TaskTemplate"]["ContainerSpec"]["Mounts"][0]["VolumeOptions"]["Labels"] = {
        NAMESPACE_LABEL: "stack"
    }
    values = ss.form_values(rich, administrator=True)

    merged = ss.merge_spec(rich, parse(values, creating=False, admin=True), refs_for(rich))

    assert merged["TaskTemplate"]["ContainerSpec"]["Mounts"][0]["VolumeOptions"]["Labels"] == {
        NAMESPACE_LABEL: "stack"
    }


def test_a_bare_environment_entry_stays_bare() -> None:
    bare = {
        "TaskTemplate": {"ContainerSpec": {"Image": "x:1", "Env": ["BARE", "K=v"]}},
        "Mode": {"Replicated": {"Replicas": 1}},
    }
    values = ss.form_values(bare, administrator=True)

    merged = ss.merge_spec(
        bare, parse(values, creating=False, admin=True), ss.ResolvedRefs({}, {}, {}, "x:1")
    )

    assert merged["TaskTemplate"]["ContainerSpec"]["Env"] == ["BARE", "K=v"]


def test_merging_into_a_spec_with_null_parts_builds_the_managed_fields() -> None:
    sloppy = {"Name": "x", "Labels": None, "TaskTemplate": None, "Mode": None}

    merged = ss.merge_spec(
        sloppy, parse({"image": "x"}, creating=False), ss.ResolvedRefs({}, {}, {}, "x:1")
    )

    assert merged["TaskTemplate"]["ContainerSpec"]["Image"] == "x:1"
    assert merged["Mode"] == {"Replicated": {"Replicas": 1}}
    assert "Labels" not in merged


def test_viewers_are_shown_environment_values_by_role_never_a_sensitive_one() -> None:
    spec = sample_spec()

    standard = ss.form_values(spec, administrator=False)
    administrator = ss.form_values(spec, administrator=True)

    assert (
        "env-0-key" in standard and "env-0-value" not in standard and "env-1-value" not in standard
    )
    assert administrator["env-0-value"] == "1" and "env-1-value" not in administrator
    assert "abc" not in json.dumps(standard) + json.dumps(administrator)
    assert all("stack.namespace" not in value for value in administrator.values())


def test_a_withheld_environment_value_left_empty_is_kept_on_update() -> None:
    spec = sample_spec()
    shown = ss.form_values(spec, administrator=False)

    parsed = parse(shown, creating=False)
    merged = ss.merge_spec(spec, parsed, refs_for(spec))

    assert [r.value for r in parsed.env] == [None, None]
    assert merged["TaskTemplate"]["ContainerSpec"]["Env"] == ["A=1", "SECRET_TOKEN=abc"]
    assert merged["Labels"][NAMESPACE_LABEL] == "S17Manual"
    assert merged["TaskTemplate"]["ContainerSpec"]["Labels"][NAMESPACE_LABEL] == "S17Manual"


def test_a_typed_replacement_for_a_withheld_value_is_applied_and_a_removed_row_removes_it() -> None:
    spec = sample_spec()
    shown = ss.form_values(spec, administrator=False)
    removed = {k: v for k, v in shown.items() if not k.startswith("env-1-")}

    replaced = ss.merge_spec(
        spec, parse({**shown, "env-1-value": "new"}, creating=False), refs_for(spec)
    )
    dropped = ss.merge_spec(spec, parse(removed, creating=False), refs_for(spec))

    assert replaced["TaskTemplate"]["ContainerSpec"]["Env"] == ["A=1", "SECRET_TOKEN=new"]
    assert dropped["TaskTemplate"]["ContainerSpec"]["Env"] == ["A=1"]


def test_an_administrators_empty_sensitive_value_is_kept_and_an_empty_plain_one_is_empty() -> None:
    spec = sample_spec()
    shown = ss.form_values(spec, administrator=True)
    form = {**shown, "env-1-key": "SECRET_TOKEN", "env-1-value": "", "env-0-value": ""}

    merged = ss.merge_spec(spec, parse(form, creating=False, admin=True), refs_for(spec))

    assert merged["TaskTemplate"]["ContainerSpec"]["Env"] == ["A=", "SECRET_TOKEN=abc"]


def test_a_new_row_with_an_empty_value_is_empty_and_never_borrows_a_stored_one() -> None:
    spec = sample_spec()
    shown = ss.form_values(spec, administrator=False)

    merged = ss.merge_spec(
        spec, parse({**shown, "env-5-key": "FRESH"}, creating=False), refs_for(spec)
    )

    assert merged["TaskTemplate"]["ContainerSpec"]["Env"][-1] == "FRESH="


def test_a_standard_users_sensitive_label_value_is_withheld_and_kept() -> None:
    spec = sample_spec()
    spec["Labels"].update({"db_password": "hunter2", "plain": "ok"})
    shown = ss.form_values(spec, administrator=False)

    merged = ss.merge_spec(spec, parse(shown, creating=False), refs_for(spec))

    assert "hunter2" not in json.dumps(shown) and "ok" in shown.values()
    assert merged["Labels"]["db_password"] == "hunter2"
    assert merged["Labels"]["plain"] == "ok"


def test_a_reserved_label_submitted_on_an_update_is_refused() -> None:
    shown = ss.form_values(sample_spec(), administrator=False)

    assert "label-9-key" in errors(
        {**shown, "label-9-key": NAMESPACE_LABEL, "label-9-value": "other"}, creating=False
    )


def stored_out_of_range() -> Spec:
    spec = sample_spec()
    spec["TaskTemplate"]["Resources"] = {"Limits": {"NanoCPUs": 500_000, "MemoryBytes": 1024}}
    spec["TaskTemplate"]["RestartPolicy"] = {"Condition": "any", "Delay": 1_500_400}
    spec["UpdateConfig"] = {"Delay": 500_000, "Parallelism": 1}
    return spec


def test_stored_values_outside_the_ranges_are_shown_exactly() -> None:
    values = ss.form_values(stored_out_of_range(), administrator=True)

    assert values["restart_delay"] == "1ms500us400ns"
    assert values["update_delay"] == "500us"
    assert values["cpu_limit"] == "0.0005"


def test_posting_a_stored_value_back_exactly_as_shown_carries_it_raw() -> None:
    spec = stored_out_of_range()
    shown = ss.form_values(spec, administrator=True)

    parsed = ss.parse_service_form(shown, creating=False, administrator=True, current=spec)
    merged = ss.merge_spec(spec, parsed, refs_for(spec))

    assert merged["TaskTemplate"]["Resources"]["Limits"] == {
        "NanoCPUs": 500_000,
        "MemoryBytes": 1024,
    }
    assert merged["TaskTemplate"]["RestartPolicy"]["Delay"] == 1_500_400
    assert merged["UpdateConfig"]["Delay"] == 500_000


def test_the_same_values_are_refused_when_they_are_not_the_stored_ones() -> None:
    spec = stored_out_of_range()
    shown = ss.form_values(spec, administrator=True)

    assert "cpu_limit" in errors(shown, creating=False, admin=True)
    with pytest.raises(ValidationFailed) as raised:
        ss.parse_service_form(
            {**shown, "cpu_limit": "0.0001"}, creating=False, administrator=True, current=spec
        )

    assert "cpu_limit" in raised.value.fields


@pytest.mark.parametrize(
    ("nanoseconds", "text"),
    [
        (0, "0s"),
        (-5, "0s"),
        (1, "1ns"),
        (1_500_400, "1ms500us400ns"),
        (3_600_000_000_000, "1h"),
        (3_661_000_000_001, "1h1m1s1ns"),
    ],
)
def test_durations_are_formatted_down_to_the_nanosecond(nanoseconds: int, text: str) -> None:
    assert ss.format_exact_duration(nanoseconds) == text


def test_replicas_of_reports_replicated_services_only() -> None:
    spec = sample_spec()
    job = {**spec, "Mode": {"ReplicatedJob": {"MaxConcurrent": 1}}}

    assert ss.replicas_of(spec) == 2
    assert ss.replicas_of({"Mode": {"Global": {}}}) is None
    assert ss.replicas_of(job) is None
    assert ss.replicas_of({"Mode": {"Replicated": {}}}) == 1


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ({"Replicated": {"Replicas": 1}}, "replicated"),
        ({"Global": {}}, "global"),
        ({"ReplicatedJob": {}}, "job"),
        ({"GlobalJob": {}}, "job"),
        ({}, None),
    ],
)
def test_mode_of_names_the_scheduling_mode(mode: dict[str, Any], expected: str | None) -> None:
    assert ss.mode_of({"Mode": mode}) == expected


def test_scaling_changes_only_the_replica_count_and_leaves_the_original_alone() -> None:
    spec = sample_spec()

    scaled = ss.scaled(spec, 0)

    assert scaled["Mode"] == {"Replicated": {"Replicas": 0}}
    assert stripped({**scaled, "Mode": None}) == stripped({**spec, "Mode": None})
    assert spec["Mode"]["Replicated"]["Replicas"] == 2


@pytest.mark.parametrize(
    ("mode", "replicas"),
    [
        pytest.param({"Global": {}}, 1, id="global"),
        pytest.param({"ReplicatedJob": {}}, 1, id="job"),
        pytest.param({"Replicated": {"Replicas": 1}}, -1, id="negative"),
        pytest.param({"Replicated": {"Replicas": 1}}, 1001, id="over the ceiling"),
    ],
)
def test_scaling_refuses_a_non_replicated_service_and_an_out_of_range_count(
    mode: dict[str, Any], replicas: int
) -> None:
    with pytest.raises(ValueError):
        ss.scaled({"Mode": mode}, replicas)


def test_a_forced_spec_increments_the_counter_and_changes_nothing_else() -> None:
    spec = sample_spec()

    forced = ss.forced(spec)
    twice = ss.forced(forced)

    assert (ss.force_counter(spec), ss.force_counter(forced), ss.force_counter(twice)) == (0, 1, 2)
    assert stripped({**forced, "TaskTemplate": None}) == stripped({**spec, "TaskTemplate": None})


def test_image_of_returns_the_image_or_none() -> None:
    assert ss.image_of(sample_spec()).startswith("sha256:")  # type: ignore[union-attr]
    assert ss.image_of({}) is None


def mutations() -> dict[str, Any]:
    def set_in(path: tuple[str, ...], value: Any) -> Any:
        def mutate(spec: Spec) -> None:
            node = spec
            for step in path[:-1]:
                node = node[step]
            node[path[-1]] = value

        return mutate

    container = ("TaskTemplate", "ContainerSpec")
    return {
        "image": set_in((*container, "Image"), "other"),
        "mode": set_in(("Mode", "Replicated", "Replicas"), 9),
        "command": set_in((*container, "Args"), ["x"]),
        "environment": set_in((*container, "Env"), ["B=2"]),
        "labels": set_in(("Labels", "extra"), "1"),
        "placement": set_in(("TaskTemplate", "Placement", "MaxReplicas"), 7),
        "resources": set_in(("TaskTemplate", "Resources", "Limits", "Pids"), 7),
        "restart_policy": set_in(("TaskTemplate", "RestartPolicy", "MaxAttempts"), 9),
        "update_config": set_in(("UpdateConfig", "Parallelism"), 4),
        "rollback_config": set_in(("RollbackConfig", "Order"), "start-first"),
        "networks": set_in(("TaskTemplate", "Networks"), [{"Target": "z"}]),
        "ports": lambda s: s["EndpointSpec"]["Ports"][0].update(PublishedPort=1),
        "mounts": set_in(
            (*container, "Mounts"), [{"Type": "volume", "Source": "v", "Target": "/v"}]
        ),
        "configs": set_in((*container, "Configs"), []),
        "secrets": lambda s: s["TaskTemplate"]["ContainerSpec"]["Secrets"][0]["File"].update(
            Name="renamed"
        ),
        "healthcheck": set_in((*container, "Healthcheck", "Interval"), 40_000_000_000),
        "process": set_in((*container, "User"), "1000"),
        "host": set_in((*container, "Hostname"), "web"),
        "security": set_in((*container, "ReadOnly"), True),
        "ulimits": set_in((*container, "Ulimits"), [{"Name": "nofile", "Soft": 1, "Hard": 2}]),
        "logging": set_in(("TaskTemplate", "LogDriver", "Options", "max-size"), "2m"),
    }


@pytest.mark.parametrize("category", [c for c in ss.CATEGORIES if c != "other"])
def test_changed_categories_reports_only_the_category_that_changed(category: str) -> None:
    before = sample_spec()
    after = copy.deepcopy(before)
    mutations()[category](after)

    assert ss.changed_categories(before, after) == (category,)


def test_a_key_outside_the_form_is_reported_as_other() -> None:
    before = sample_spec()
    after = copy.deepcopy(before)
    after["TaskTemplate"]["ContainerSpec"]["Isolation"] = "process"
    after["TaskTemplate"]["ContainerSpec"]["OomScoreAdj"] = 5

    assert ss.changed_categories(before, after) == ("other",)


def test_no_difference_and_reordered_environment_are_no_change() -> None:
    before = sample_spec()
    reordered = copy.deepcopy(before)
    reordered["TaskTemplate"]["ContainerSpec"]["Env"] = ["SECRET_TOKEN=abc", "A=1"]

    assert ss.changed_categories(before, copy.deepcopy(before)) == ()
    assert ss.changed_categories(before, reordered) == ()


def test_defaults_the_engine_adds_are_not_changes() -> None:
    before = sample_spec()
    after = copy.deepcopy(before)
    after["TaskTemplate"].update(Runtime="container", ForceUpdate=0)
    after["TaskTemplate"]["ContainerSpec"]["Isolation"] = "default"
    after["TaskTemplate"]["ContainerSpec"]["DNSConfig"] = {}
    after["RollbackConfig"]["MaxFailureRatio"] = 0

    assert ss.changed_categories(before, after) == ()


def test_a_zero_differs_from_an_absent_value_on_a_managed_key() -> None:
    zero = sample_spec()
    zero["RollbackConfig"]["Delay"] = 0
    absent = sample_spec()
    absent["RollbackConfig"].pop("Delay", None)

    assert ss.changed_categories(zero, absent) == ("rollback_config",)


def test_the_category_vocabulary_is_fixed_and_ends_with_other() -> None:
    assert ss.CATEGORIES == (
        "image",
        "mode",
        "command",
        "environment",
        "labels",
        "placement",
        "resources",
        "restart_policy",
        "update_config",
        "rollback_config",
        "networks",
        "ports",
        "mounts",
        "configs",
        "secrets",
        "healthcheck",
        "process",
        "host",
        "security",
        "ulimits",
        "logging",
        "other",
    )


def test_the_current_and_previous_spec_of_a_rolled_back_service_can_be_compared() -> None:
    document = sample_json("10-GET-A-after-rollback.bin")

    assert isinstance(ss.changed_categories(document["Spec"], document["PreviousSpec"]), tuple)


def test_garbage_in_a_spec_does_not_raise_in_the_form_helpers() -> None:
    garbage = {
        "TaskTemplate": {
            "ContainerSpec": {"Env": [1, None, "A=b"], "Mounts": ["x"], "Args": [1]},
            "Networks": [3],
        },
        "EndpointSpec": {"Ports": ["p"]},
    }

    assert ss.form_values({}, administrator=True) == {}
    assert (
        ss.form_values({"TaskTemplate": None, "Mode": None, "Labels": None}, administrator=False)
        == {}
    )
    assert isinstance(ss.form_values(garbage, administrator=True), dict)
    assert isinstance(ss.changed_categories({}, {"TaskTemplate": 3, "Mode": 4}), tuple)
