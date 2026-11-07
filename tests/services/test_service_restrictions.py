"""The standard-user restriction walker over service specs, and the references a spec holds."""

import copy
from collections.abc import Callable
from typing import Any

import pytest

from app.services.container_restrictions import OPTION_IDS, Finding, option_label
from app.services.service_restrictions import (
    added_references,
    constraints_of,
    find_restricted_service,
    parse_constraint,
    pinned_to,
    references,
    volume_pin_exempt,
    volume_placement_findings,
)
from tests.swarm_support import NAMESPACE_LABEL, NODE, sample_json, sample_specs

Spec = dict[str, Any]


def base() -> Spec:
    return {
        "Name": "s",
        "TaskTemplate": {"ContainerSpec": {"Image": "nginx:latest"}},
        "Mode": {"Replicated": {"Replicas": 1}},
    }


def container(**fields: Any) -> Spec:
    spec = base()
    spec["TaskTemplate"]["ContainerSpec"].update(copy.deepcopy(fields))
    return spec


def task(**fields: Any) -> Spec:
    spec = base()
    spec["TaskTemplate"].update(copy.deepcopy(fields))
    return spec


def top(**fields: Any) -> Spec:
    spec = base()
    spec.update(copy.deepcopy(fields))
    return spec


def volume_mount(**fields: Any) -> Spec:
    return container(Mounts=[{"Type": "volume", "Source": "data", "Target": "/d", **fields}])


def options(spec: Spec) -> list[str]:
    return sorted({f.option for f in find_restricted_service(spec)})


def paths(spec: Spec) -> list[str]:
    return sorted(f.path for f in find_restricted_service(spec))


def only(mount: Spec) -> Spec:
    return container(Mounts=[mount])


SELINUX = {"Type": "spc_t"}
FULLY_LOADED = container(
    Init=True,
    ReadOnly=True,
    Hosts=["1.2.3.4 a"],
    DNSConfig={"Nameservers": ["1.1.1.1"], "Search": [], "Options": []},
    Groups=["g"],
    User="1000",
    Hostname="h",
    Dir="/",
    Env=["A=1"],
    Command=["sh"],
    Args=["-c", "x"],
    StopSignal="SIGINT",
    StopGracePeriod=1,
    Healthcheck={"Test": ["CMD", "true"], "Interval": 1, "StartInterval": 1},
    TTY=True,
    OpenStdin=True,
)

ALLOWED_AND_REFUSED = [
    ("no-new-privileges allowed", container(Privileges={"NoNewPrivileges": True}), []),
    ("selinux refused", container(Privileges={"SELinuxContext": SELINUX}), ["security_opt"]),
    (
        "selinux disable refused",
        container(Privileges={"SELinuxContext": {"Disable": True}}),
        ["security_opt"],
    ),
    ("seccomp default allowed", container(Privileges={"Seccomp": {"Mode": "default"}}), []),
    (
        "seccomp unconfined refused",
        container(Privileges={"Seccomp": {"Mode": "unconfined"}}),
        ["security_opt"],
    ),
    (
        "seccomp custom refused",
        container(Privileges={"Seccomp": {"Mode": "custom", "Profile": "e30="}}),
        ["security_opt"],
    ),
    (
        "seccomp default with a profile refused",
        container(Privileges={"Seccomp": {"Mode": "default", "Profile": "e30="}}),
        ["security_opt"],
    ),
    (
        "seccomp mode in another case refused",
        container(Privileges={"Seccomp": {"Mode": "Default"}}),
        ["security_opt"],
    ),
    ("apparmor default allowed", container(Privileges={"AppArmor": {"Mode": "default"}}), []),
    (
        "apparmor disabled refused",
        container(Privileges={"AppArmor": {"Mode": "disabled"}}),
        ["security_opt"],
    ),
    (
        "credential spec config refused",
        container(Privileges={"CredentialSpec": {"Config": "x"}}),
        ["unknown_option"],
    ),
    (
        "credential spec file refused",
        container(Privileges={"CredentialSpec": {"File": "f.json"}}),
        ["unknown_option"],
    ),
    ("cap add refused", container(CapabilityAdd=["NET_ADMIN"]), ["cap_add"]),
    ("cap add empty allowed", container(CapabilityAdd=[]), []),
    ("cap drop allowed", container(CapabilityDrop=["ALL"]), []),
    ("ulimits allowed", container(Ulimits=[{"Name": "nofile", "Soft": 1, "Hard": 2}]), []),
    ("fields the form carries are allowed", FULLY_LOADED, []),
    ("container labels allowed", container(Labels={"a": "b"}), []),
    ("negative oom score refused", container(OomScoreAdj=-100), ["oom_score_adj"]),
    ("zero oom score allowed", container(OomScoreAdj=0), []),
    ("positive oom score allowed", container(OomScoreAdj=100), []),
    ("oom score as text refused", container(OomScoreAdj="-1"), ["oom_score_adj"]),
    ("sysctls refused", container(Sysctls={"net.core.somaxconn": "1"}), ["sysctls"]),
    ("sysctls empty allowed", container(Sysctls={}), []),
    ("isolation default allowed", container(Isolation="default"), []),
    ("isolation hyperv refused", container(Isolation="hyperv"), ["isolation"]),
    ("named volume mount allowed", volume_mount(), []),
    (
        "tmpfs mount allowed",
        only({"Type": "tmpfs", "Target": "/t", "TmpfsOptions": {"SizeBytes": 1, "Mode": 1}}),
        [],
    ),
    (
        "bind mount refused",
        only({"Type": "bind", "Source": "/etc", "Target": "/d"}),
        ["bind_mount"],
    ),
    (
        "bind mount in another case refused as a bind",
        only({"Type": " Bind", "Source": "/etc", "Target": "/d"}),
        ["bind_mount"],
    ),
    ("npipe mount refused", only({"Type": "npipe", "Source": "x", "Target": "/d"}), ["mount_type"]),
    (
        "cluster mount refused",
        only({"Type": "cluster", "Source": "x", "Target": "/d", "ClusterOptions": {}}),
        ["mount_type"],
    ),
    (
        "image mount refused",
        only({"Type": "image", "Source": "x", "Target": "/d", "ImageOptions": {"Subpath": "a"}}),
        ["mount_type"],
    ),
    ("mount without a type refused", only({"Source": "data", "Target": "/d"}), ["mount_type"]),
    ("volume mount with a path source refused", volume_mount(Source="/etc"), ["bind_mount"]),
    (
        "volume mount with bind options refused",
        volume_mount(BindOptions={"Propagation": "rshared"}),
        ["bind_mount"],
    ),
    (
        "volume driver refused",
        volume_mount(VolumeOptions={"DriverConfig": {"Name": "nfs", "Options": {"o": "x"}}}),
        ["volume_driver"],
    ),
    (
        "volume mount options the container create allows are allowed",
        volume_mount(
            ReadOnly=True,
            Consistency="cached",
            VolumeOptions={"NoCopy": True, "Labels": {"a": "b"}, "Subpath": "s"},
        ),
        [],
    ),
    (
        "config runtime reference refused",
        container(Configs=[{"ConfigID": "a", "ConfigName": "b", "Runtime": {}}]),
        ["unknown_option"],
    ),
    (
        "secret runtime reference refused",
        container(Secrets=[{"SecretID": "a", "SecretName": "b", "Runtime": {}}]),
        ["unknown_option"],
    ),
    (
        "config file reference allowed",
        container(
            Configs=[
                {
                    "ConfigID": "a",
                    "ConfigName": "b",
                    "File": {"Name": "/x", "UID": "0", "GID": "0", "Mode": 292},
                }
            ]
        ),
        [],
    ),
    (
        "generic resources refused",
        task(
            Resources={
                "Reservations": {
                    "GenericResources": [{"DiscreteResourceSpec": {"Kind": "gpu", "Value": 1}}]
                }
            }
        ),
        ["devices"],
    ),
    (
        "limits and reservations allowed",
        task(
            Resources={
                "Limits": {"NanoCPUs": 1, "MemoryBytes": 1, "Pids": 1},
                "Reservations": {"NanoCPUs": 1, "MemoryBytes": 1},
            }
        ),
        [],
    ),
    (
        "json-file with max-size allowed",
        task(LogDriver={"Name": "json-file", "Options": {"max-size": "1m"}}),
        [],
    ),
    ("syslog driver refused", task(LogDriver={"Name": "syslog"}), ["log_driver"]),
    (
        "log option that reads the environment refused",
        task(LogDriver={"Name": "json-file", "Options": {"env": "A"}}),
        ["log_driver"],
    ),
    ("container runtime allowed", task(Runtime="container"), []),
    ("plugin runtime refused", task(Runtime="plugin"), ["runtime"]),
    ("attachment runtime refused", task(Runtime="attachment"), ["runtime"]),
    ("plugin spec refused", task(PluginSpec={"Name": "p"}), ["runtime"]),
    ("empty plugin spec refused", task(PluginSpec={}), ["runtime"]),
    (
        "network attachment spec refused",
        task(NetworkAttachmentSpec={"ContainerID": "x"}),
        ["runtime"],
    ),
    (
        "network driver options refused",
        task(Networks=[{"Target": "n", "DriverOpts": {"a": "b"}}]),
        ["endpoint_driver_opts"],
    ),
    ("network with aliases allowed", task(Networks=[{"Target": "n", "Aliases": ["a"]}]), []),
    ("host network refused", task(Networks=[{"Target": "host"}]), ["network_host"]),
    (
        "host network padded and upper-case refused",
        task(Networks=[{"Target": " HOST "}]),
        ["network_host"],
    ),
    (
        "placement, restart policy and force update allowed",
        task(
            Placement={
                "Constraints": ["node.role==manager"],
                "Preferences": [{"Spread": {"SpreadDescriptor": "x"}}],
                "MaxReplicas": 1,
                "Platforms": [{"Architecture": "amd64", "OS": "linux"}],
            },
            RestartPolicy={"Condition": "any", "Delay": 1, "MaxAttempts": 1, "Window": 1},
            ForceUpdate=3,
        ),
        [],
    ),
    (
        "replicated job mode allowed",
        top(Mode={"ReplicatedJob": {"MaxConcurrent": 1, "TotalCompletions": 2}}),
        [],
    ),
    ("global job mode allowed", top(Mode={"GlobalJob": {}}), []),
    (
        "update and rollback configuration allowed",
        top(
            UpdateConfig={
                "Parallelism": 1,
                "Delay": 1,
                "FailureAction": "pause",
                "Monitor": 1,
                "MaxFailureRatio": 0.1,
                "Order": "stop-first",
            },
            RollbackConfig={"Parallelism": 1},
        ),
        [],
    ),
    (
        "host-mode sctp port on a dnsrr endpoint allowed",
        top(
            EndpointSpec={
                "Mode": "dnsrr",
                "Ports": [
                    {
                        "Name": "a",
                        "Protocol": "sctp",
                        "TargetPort": 1,
                        "PublishedPort": 2,
                        "PublishMode": "host",
                    }
                ],
            }
        ),
        [],
    ),
    ("legacy top-level networks refused", top(Networks=[{"Target": "n"}]), ["unknown_option"]),
    ("empty legacy top-level networks allowed", top(Networks=[]), []),
    (
        "several findings are all collected",
        container(
            CapabilityAdd=["ALL"],
            Sysctls={"a": "b"},
            Privileges={"Seccomp": {"Mode": "unconfined"}},
            Mounts=[{"Type": "bind", "Source": "/", "Target": "/h"}],
        ),
        ["bind_mount", "cap_add", "security_opt", "sysctls"],
    ),
]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [pytest.param(spec, expected, id=label) for label, spec, expected in ALLOWED_AND_REFUSED],
)
def test_a_service_spec_yields_exactly_the_option_ids_its_table_row_names(
    spec: Spec, expected: list[str]
) -> None:
    assert options(spec) == sorted(expected)


NULLABLE = [
    ("privileges", container(Privileges=None)),
    (
        "every privileges member",
        container(
            Privileges={
                "CredentialSpec": None,
                "SELinuxContext": None,
                "Seccomp": None,
                "AppArmor": None,
                "NoNewPrivileges": None,
            }
        ),
    ),
    ("seccomp members", container(Privileges={"Seccomp": {"Mode": None, "Profile": None}})),
    ("apparmor mode", container(Privileges={"AppArmor": {"Mode": None}})),
    (
        "credential spec members",
        container(Privileges={"CredentialSpec": {"Config": None, "File": None, "Registry": None}}),
    ),
    ("empty selinux context", container(Privileges={"SELinuxContext": {}})),
    ("cap add", container(CapabilityAdd=None)),
    ("oom score", container(OomScoreAdj=None)),
    ("sysctls", container(Sysctls=None)),
    ("isolation", container(Isolation=None)),
    ("mounts", container(Mounts=None)),
    ("bind options", volume_mount(BindOptions=None)),
    ("volume options", volume_mount(VolumeOptions=None)),
    ("driver config", volume_mount(VolumeOptions={"DriverConfig": None})),
    (
        "driver config members",
        volume_mount(VolumeOptions={"DriverConfig": {"Name": None, "Options": None}}),
    ),
    (
        "tmpfs, image and cluster options",
        volume_mount(TmpfsOptions=None, ImageOptions=None, ClusterOptions=None),
    ),
    (
        "anonymous volume source",
        container(Mounts=[{"Type": "volume", "Source": None, "Target": "/d"}]),
    ),
    ("config and secret lists", container(Configs=None, Secrets=None)),
    (
        "config runtime and file",
        container(Configs=[{"ConfigID": "a", "ConfigName": "b", "Runtime": None, "File": None}]),
    ),
    ("generic resources", task(Resources={"Reservations": {"GenericResources": None}})),
    ("resource limits", task(Resources={"Limits": None, "Reservations": None})),
    ("log driver", task(LogDriver=None)),
    ("log driver members", task(LogDriver={"Name": None, "Options": None})),
    ("runtime", task(Runtime=None)),
    ("plugin and attachment", task(PluginSpec=None, NetworkAttachmentSpec=None)),
    ("task networks", task(Networks=None)),
    (
        "network members",
        task(Networks=[{"Target": "n", "DriverOpts": None, "Aliases": None}]),
    ),
    ("top-level networks", top(Networks=None)),
    (
        "mode, rollout and endpoint",
        top(Mode=None, UpdateConfig=None, RollbackConfig=None, EndpointSpec=None),
    ),
    ("placement and restart policy", task(Placement=None, RestartPolicy=None)),
]


@pytest.mark.parametrize("spec", [pytest.param(s, id=label) for label, s in NULLABLE])
def test_a_json_null_counts_as_absent_wherever_a_value_is_judged(spec: Spec) -> None:
    assert find_restricted_service(spec) == []


LEVELS: list[tuple[str, Callable[[Any], Spec]]] = [
    ("service", lambda v: top(Zzz=v)),
    ("previous spec as a key", lambda v: top(PreviousSpec=v)),
    ("task template", lambda v: task(Zzz=v)),
    ("container spec", lambda v: container(Zzz=v)),
    ("health check in the swagger spelling", lambda v: container(HealthCheck=v)),
    ("privileges", lambda v: container(Privileges={"Zzz": v})),
    ("credential spec", lambda v: container(Privileges={"CredentialSpec": {"Zzz": v}})),
    ("selinux", lambda v: container(Privileges={"SELinuxContext": {"Zzz": v}})),
    ("seccomp", lambda v: container(Privileges={"Seccomp": {"Zzz": v}})),
    ("apparmor", lambda v: container(Privileges={"AppArmor": {"Zzz": v}})),
    ("healthcheck", lambda v: container(Healthcheck={"Zzz": v})),
    ("dns config", lambda v: container(DNSConfig={"Zzz": v})),
    ("ulimit", lambda v: container(Ulimits=[{"Zzz": v}])),
    ("secret reference", lambda v: container(Secrets=[{"Zzz": v}])),
    ("secret file", lambda v: container(Secrets=[{"File": {"Zzz": v}}])),
    ("config reference", lambda v: container(Configs=[{"Zzz": v}])),
    ("config file", lambda v: container(Configs=[{"File": {"Zzz": v}}])),
    ("mount", lambda v: container(Mounts=[{"Type": "volume", "Target": "/d", "Zzz": v}])),
    ("bind options", lambda v: volume_mount(BindOptions={"Zzz": v})),
    ("volume options", lambda v: volume_mount(VolumeOptions={"Zzz": v})),
    ("driver config", lambda v: volume_mount(VolumeOptions={"DriverConfig": {"Zzz": v}})),
    ("image options", lambda v: volume_mount(ImageOptions={"Zzz": v})),
    ("tmpfs options", lambda v: volume_mount(TmpfsOptions={"Zzz": v})),
    ("cluster options", lambda v: volume_mount(ClusterOptions={"Zzz": v})),
    ("resources", lambda v: task(Resources={"Zzz": v})),
    ("limits", lambda v: task(Resources={"Limits": {"Zzz": v}})),
    ("a key only API 1.56 knows", lambda v: task(Resources={"Limits": {"SwapBytes": v}})),
    ("reservations", lambda v: task(Resources={"Reservations": {"Zzz": v}})),
    ("restart policy", lambda v: task(RestartPolicy={"Zzz": v})),
    ("placement", lambda v: task(Placement={"Zzz": v})),
    ("preference", lambda v: task(Placement={"Preferences": [{"Zzz": v}]})),
    ("spread", lambda v: task(Placement={"Preferences": [{"Spread": {"Zzz": v}}]})),
    ("platform", lambda v: task(Placement={"Platforms": [{"Zzz": v}]})),
    ("task network", lambda v: task(Networks=[{"Target": "n", "Zzz": v}])),
    ("log driver", lambda v: task(LogDriver={"Name": "local", "Zzz": v})),
    ("mode", lambda v: top(Mode={"Zzz": v})),
    ("replicated mode", lambda v: top(Mode={"Replicated": {"Zzz": v}})),
    ("global mode", lambda v: top(Mode={"Global": {"Zzz": v}})),
    ("replicated job", lambda v: top(Mode={"ReplicatedJob": {"Zzz": v}})),
    ("global job", lambda v: top(Mode={"GlobalJob": {"Zzz": v}})),
    ("update config", lambda v: top(UpdateConfig={"Zzz": v})),
    ("rollback config", lambda v: top(RollbackConfig={"Zzz": v})),
    ("endpoint spec", lambda v: top(EndpointSpec={"Zzz": v})),
    ("endpoint port", lambda v: top(EndpointSpec={"Ports": [{"Zzz": v}]})),
]
KEY_VALUES: list[Any] = ["x", None, {}]


@pytest.mark.parametrize("value", KEY_VALUES, ids=["text", "null", "empty-object"])
@pytest.mark.parametrize("build", [pytest.param(b, id=label) for label, b in LEVELS])
def test_a_key_outside_its_levels_known_set_is_refused_whatever_its_value(
    build: Callable[[Any], Spec], value: Any
) -> None:
    assert "unknown_option" in options(build(value))


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(container(image="x"), id="lower-case image"),
        pytest.param(container(capabilityadd=["ALL"]), id="lower-case capability add"),
        pytest.param(top(taskTemplate={}), id="lower camel task template"),
        pytest.param(top(NAME="x"), id="upper-case name"),
        pytest.param(container(Privileges={"Seccomp": {"mode": "unconfined"}}), id="lower mode"),
    ],
)
def test_only_the_exact_spelling_of_a_key_is_known_although_the_engine_reads_any_case(
    spec: Spec,
) -> None:
    assert "unknown_option" in options(spec)


def test_the_healthcheck_spelling_the_engine_returns_is_the_known_one() -> None:
    assert options(container(Healthcheck={"Test": ["NONE"]})) == []


def test_a_non_string_key_is_reported_without_echoing_it_and_a_hostile_key_is_masked() -> None:
    odd: Spec = base()
    odd[1] = "x"  # type: ignore[index]
    hostile = base()
    hostile["a b\nc"] = 1

    assert any("<key>" in path for path in paths(odd))
    assert paths(hostile) == ["<key>"]


def test_values_of_the_wrong_container_type_are_unknown_options_not_errors() -> None:
    assert options(container(Env="x", Mounts="y", Privileges=3)) == ["unknown_option"]
    assert options(container(Mounts=["x"])) == ["mount_type"]
    assert options({"TaskTemplate": "x"}) == ["unknown_option"]
    assert find_restricted_service({}) == []


def test_findings_name_the_form_row_the_user_typed_when_slots_are_given() -> None:
    mounts = container(
        Mounts=[
            {"Type": "volume", "Source": "dd", "Target": "/a"},
            {"Type": "bind", "Source": "/x", "Target": "/b"},
        ]
    )
    networks = task(Networks=[{"Target": "a"}, {"Target": "host"}])

    mount_findings = find_restricted_service(mounts, slots={"mounts": [3, 7]})
    network_findings = find_restricted_service(networks, slots={"networks": [0, 5]})

    assert [f.field for f in mount_findings if f.option == "bind_mount"] == ["mount-7-type"]
    assert [f.field for f in network_findings] == ["network-5-name"]


def test_the_placement_option_id_is_registered_and_worded() -> None:
    finding = Finding("swarm_volume_placement", "p")

    assert "swarm_volume_placement" in OPTION_IDS
    assert option_label("swarm_volume_placement") == (
        "named volumes on a service that is not pinned to this manager"
    )
    assert finding.message.startswith("Only administrators may use named volumes")


LOG_OPTION_SAMPLES = {("10-GET-A-after-rollback.bin", "PreviousSpec"): ["log_driver"]}


@pytest.mark.parametrize(
    ("sample", "key"),
    [pytest.param(name, key, id=f"{name}-{key}") for name, key, _ in sample_specs()],
)
def test_a_spec_as_a_real_engine_returned_it_passes_the_exact_key_check(
    sample: str, key: str
) -> None:
    spec = next(s for name, k, s in sample_specs() if (name, k) == (sample, key))

    assert options(spec) == LOG_OPTION_SAMPLES.get((sample, key), [])


def test_a_stack_deployed_privileges_object_with_null_members_has_no_findings() -> None:
    specs = [item["Spec"] for item in sample_json("18-GET-services-label-key-filter.bin")]

    assert all(find_restricted_service(spec) == [] for spec in specs)


def test_a_previous_spec_is_walked_as_a_service_spec_and_is_not_a_key_of_one() -> None:
    document = sample_json("10-GET-A-after-rollback.bin")

    assert options(document["PreviousSpec"]) == ["log_driver"]
    assert "unknown_option" in options(document)


def test_references_list_networks_with_their_spec_path() -> None:
    found = references(task(Networks=[{"Target": "n1"}, {"Target": "n2", "Aliases": ["a"]}]))

    assert found.networks == (
        ("n1", "TaskTemplate.Networks[0]"),
        ("n2", "TaskTemplate.Networks[1]"),
    )


def test_references_list_named_volumes_only_with_their_read_only_flag() -> None:
    spec = container(
        Mounts=[
            {"Type": "volume", "Source": "data", "Target": "/d", "ReadOnly": True},
            {"Type": "volume", "Target": "/anon"},
            {"Type": "tmpfs", "Target": "/t"},
            {"Type": "bind", "Source": "/x", "Target": "/b"},
            {"Type": "volume", "Source": "w", "Target": "/w"},
        ]
    )

    assert references(spec).volumes == (("data", True, "/d"), ("w", False, "/w"))


CONFIGS = [
    {"ConfigID": "c1", "ConfigName": "cfg", "File": {"Name": "/cfg"}},
    {"ConfigID": "c2", "ConfigName": "rt", "Runtime": {}},
]
SECRETS = [{"SecretID": "s1", "SecretName": "sec", "File": {"Name": "sec"}}]


def test_references_list_configs_and_secrets_with_their_file_names() -> None:
    found = references(container(Configs=CONFIGS, Secrets=SECRETS))

    assert found.configs == (("c1", "cfg", "/cfg"), ("c2", "rt", ""))
    assert found.secrets == (("s1", "sec", "sec"),)


def test_references_tolerate_malformed_documents() -> None:
    garbage = {
        "TaskTemplate": {
            "Networks": ["x", None, {"Target": 3}, {"Target": "ok"}],
            "ContainerSpec": {
                "Mounts": [1, {"Type": "volume", "Source": 5}],
                "Configs": "x",
                "Secrets": [None],
            },
        }
    }

    assert references({"TaskTemplate": "x"}) == references({})
    assert references(garbage).networks == (("ok", "TaskTemplate.Networks[3]"),)


def test_added_references_without_a_before_is_everything() -> None:
    spec = container(Configs=CONFIGS, Secrets=SECRETS)

    assert added_references(None, spec) == references(spec)


def test_added_references_return_only_what_the_before_does_not_cover() -> None:
    before = container(Configs=CONFIGS, Secrets=SECRETS)
    after = copy.deepcopy(before)
    after["TaskTemplate"]["ContainerSpec"]["Secrets"].append(
        {"SecretID": "s2", "SecretName": "two", "File": {"Name": "two"}}
    )

    added = added_references(before, after)

    assert added.secrets == (("s2", "two", "two"),)
    assert not (added.configs or added.networks or added.volumes)


def test_added_networks_ignore_order_and_aliases() -> None:
    before = task(Networks=[{"Target": "n1"}, {"Target": "n2"}])
    after = task(Networks=[{"Target": "n2", "Aliases": ["x"]}, {"Target": "n1"}, {"Target": "n3"}])

    assert added_references(before, after).networks == (("n3", "TaskTemplate.Networks[2]"),)


def test_a_mount_that_becomes_writable_is_added_and_one_that_becomes_read_only_is_not() -> None:
    read_only = container(
        Mounts=[{"Type": "volume", "Source": "data", "Target": "/d", "ReadOnly": True}]
    )
    writable = container(Mounts=[{"Type": "volume", "Source": "data", "Target": "/d"}])

    assert added_references(read_only, writable).volumes == (("data", False, "/d"),)
    assert added_references(writable, read_only).volumes == ()


def with_constraints(*constraints: str) -> Spec:
    return task(Placement={"Constraints": list(constraints)})


@pytest.mark.parametrize(
    ("constraint", "pins"),
    [
        (f"node.id=={NODE}", True),
        (f"node.id == {NODE}", True),
        (f"NODE.ID==  {NODE.upper()}", True),
        (f"node.id!={NODE}", False),
        ("node.id==other", False),
        ("node.hostname==x", False),
        (f"node.id=={NODE}x", False),
        (f"node.id==={NODE}", False),
        ("x==y==node.id", False),
        ("node.id==*", False),
        (f"node.labels.node.id=={NODE}", False),
        ("", False),
        (f"node.id{NODE}", False),
    ],
)
def test_a_constraint_pins_a_service_to_the_node_only_as_an_equality_on_its_id(
    constraint: str, pins: bool
) -> None:
    assert pinned_to(with_constraints(constraint), NODE) is pins


def test_one_pinning_constraint_among_others_is_enough_and_a_missing_placement_pins_nothing() -> (
    None
):
    assert pinned_to(with_constraints("node.role==worker", f"node.id=={NODE}"), NODE)
    assert not pinned_to(base(), NODE)
    assert not pinned_to(task(Placement={"Constraints": None}), NODE)
    assert not pinned_to({"TaskTemplate": 3}, NODE)
    assert pinned_to(
        {"TaskTemplate": {"Placement": {"Constraints": [1, None, f"node.id=={NODE}"]}}}, NODE
    )


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("node.role==worker", ("node.role", "==", "worker")),
        ("node.labels.zone != a", ("node.labels.zone", "!=", "a")),
        ("node.role", None),
        ("node.role==a=b", None),
        ("==x", None),
    ],
)
def test_constraints_parse_to_an_attribute_an_operator_and_a_value(
    text: str, parsed: tuple[str, str, str] | None
) -> None:
    assert parse_constraint(text) == parsed


def test_constraints_of_reads_the_strings_as_written() -> None:
    assert constraints_of(with_constraints("a==b", "c!=d")) == ("a==b", "c!=d")
    assert constraints_of(base()) == ()


def pinned_and_unpinned() -> tuple[Spec, Spec]:
    pinned = container(Mounts=[{"Type": "volume", "Source": "data", "Target": "/d"}])
    pinned["TaskTemplate"]["Placement"] = {"Constraints": [f"node.id=={NODE}"]}
    unpinned = copy.deepcopy(pinned)
    unpinned["TaskTemplate"]["Placement"] = {"Constraints": ["node.role==worker"]}
    return pinned, unpinned


def test_a_service_pinned_to_the_manager_has_no_placement_findings() -> None:
    pinned, _ = pinned_and_unpinned()

    assert volume_placement_findings(None, pinned, NODE) == []


def test_an_unpinned_named_mount_is_one_finding_naming_its_form_row() -> None:
    _, unpinned = pinned_and_unpinned()

    found = volume_placement_findings(None, unpinned, NODE, slots={"mounts": [9]})

    assert [(f.option, f.path, f.field) for f in found] == [
        ("swarm_volume_placement", "TaskTemplate.ContainerSpec.Mounts[0].Source", "mount-9-source")
    ]


def test_an_unknown_manager_node_refuses_even_a_pinned_service() -> None:
    pinned, _ = pinned_and_unpinned()

    assert len(volume_placement_findings(None, pinned, None)) == 1


def test_anonymous_volumes_and_tmpfs_need_no_pin() -> None:
    spec = container(Mounts=[{"Type": "volume", "Target": "/a"}, {"Type": "tmpfs", "Target": "/t"}])

    assert volume_placement_findings(None, spec, NODE) == []


def test_an_unchanged_named_mount_with_unchanged_constraints_is_exempt_from_the_pin() -> None:
    _, unpinned = pinned_and_unpinned()

    assert volume_placement_findings(unpinned, copy.deepcopy(unpinned), NODE) == []
    assert volume_pin_exempt(unpinned, copy.deepcopy(unpinned))
    assert not volume_pin_exempt(None, unpinned)


def test_the_exemption_is_lost_when_constraints_change() -> None:
    _, unpinned = pinned_and_unpinned()
    changed = copy.deepcopy(unpinned)
    changed["TaskTemplate"]["Placement"]["Constraints"].append("node.role!=manager")

    assert len(volume_placement_findings(unpinned, changed, NODE)) == 1


def test_the_exemption_is_lost_when_a_mount_changes_or_is_added() -> None:
    _, unpinned = pinned_and_unpinned()
    renamed = copy.deepcopy(unpinned)
    renamed["TaskTemplate"]["ContainerSpec"]["Mounts"][0]["Source"] = "other"
    extra = copy.deepcopy(unpinned)
    extra["TaskTemplate"]["ContainerSpec"]["Mounts"].append(
        {"Type": "volume", "Source": "two", "Target": "/e"}
    )

    assert len(volume_placement_findings(unpinned, renamed, NODE)) == 1
    assert len(volume_placement_findings(unpinned, extra, NODE)) == 2


def test_the_namespace_label_is_a_known_label_not_a_restricted_option() -> None:
    assert find_restricted_service(top(Labels={NAMESPACE_LABEL: "x"})) == []
