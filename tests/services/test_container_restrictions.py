"""Tests for app/services/container_restrictions.py: options a standard user may not set."""

from typing import Any

import pytest

from app.services.container_restrictions import (
    OPTION_IDS,
    Finding,
    find_restricted,
    needs_default_runtime,
    option_engine_paths,
    option_form_fields,
    option_label,
    reach_finding,
    refused_options,
)

DEFAULT_RUNTIME = "runc"


def host(**config: Any) -> dict[str, Any]:
    return {"HostConfig": config}


def endpoint(name: str, **config: Any) -> dict[str, Any]:
    return {"NetworkingConfig": {"EndpointsConfig": {name: config}}}


def mount(kind: str, **fields: Any) -> dict[str, Any]:
    return host(Mounts=[{"Type": kind, "Target": "/x", **fields}])


def found(body: dict[str, Any], **options: Any) -> list[tuple[str, str, str | None]]:
    return [(f.option, f.path, f.field) for f in find_restricted(body, **options)]


def options_of(body: dict[str, Any], **options: Any) -> list[str]:
    return [option for option, _, _ in found(body, **options)]


OPTION_CASES: list[tuple[str, dict[str, Any], str, str, str | None]] = [
    ("privileged", host(Privileged=True), "privileged", "HostConfig.Privileged", "privileged"),
    ("cap-add", host(CapAdd=["NET_ADMIN"]), "cap_add", "HostConfig.CapAdd", "cap_add"),
    ("bind-in-binds", host(Binds=["/etc:/x"]), "bind_mount", "HostConfig.Binds[0]", None),
    (
        "bind-mount-type",
        mount("bind", Source="/etc"),
        "bind_mount",
        "HostConfig.Mounts[0].Type",
        "mount-0-type",
    ),
    (
        "volume-with-a-path-source",
        mount("volume", Source="/etc"),
        "bind_mount",
        "HostConfig.Mounts[0].Source",
        "mount-0-source",
    ),
    (
        "volume-driver",
        host(VolumeDriver="rexray"),
        "volume_driver",
        "HostConfig.VolumeDriver",
        None,
    ),
    (
        "volume-driver-config",
        mount(
            "volume",
            Source="v1",
            VolumeOptions={"DriverConfig": {"Name": "local", "Options": {"o": "bind"}}},
        ),
        "volume_driver",
        "HostConfig.Mounts[0].VolumeOptions.DriverConfig",
        "mount-0-source",
    ),
    (
        "mount-type-image",
        mount("image", Source="s"),
        "mount_type",
        "HostConfig.Mounts[0].Type",
        "mount-0-type",
    ),
    (
        "mount-type-npipe",
        mount("npipe", Source="s"),
        "mount_type",
        "HostConfig.Mounts[0].Type",
        "mount-0-type",
    ),
    (
        "mount-type-cluster",
        mount("cluster", Source="s"),
        "mount_type",
        "HostConfig.Mounts[0].Type",
        "mount-0-type",
    ),
    ("volumes-from", host(VolumesFrom=["other"]), "volumes_from", "HostConfig.VolumesFrom", None),
    (
        "network-host",
        host(NetworkMode="host"),
        "network_host",
        "HostConfig.NetworkMode",
        "network_mode",
    ),
    (
        "network-host-endpoint",
        endpoint("host"),
        "network_host",
        "NetworkingConfig.EndpointsConfig[host]",
        "network-0-name",
    ),
    (
        "network-container",
        host(NetworkMode="container:abc"),
        "network_container",
        "HostConfig.NetworkMode",
        None,
    ),
    (
        "network-container-endpoint",
        endpoint("container:abc"),
        "network_container",
        "NetworkingConfig.EndpointsConfig[<network>]",
        None,
    ),
    ("pid-host", host(PidMode="host"), "pid_namespace", "HostConfig.PidMode", "pid_host"),
    (
        "pid-container",
        host(PidMode="container:x"),
        "pid_namespace",
        "HostConfig.PidMode",
        "pid_host",
    ),
    ("ipc-host", host(IpcMode="host"), "ipc_namespace", "HostConfig.IpcMode", "ipc_host"),
    (
        "ipc-container",
        host(IpcMode="container:x"),
        "ipc_namespace",
        "HostConfig.IpcMode",
        "ipc_host",
    ),
    ("uts-host", host(UTSMode="host"), "uts_namespace", "HostConfig.UTSMode", None),
    ("userns-host", host(UsernsMode="host"), "userns_mode", "HostConfig.UsernsMode", None),
    ("cgroupns-host", host(CgroupnsMode="host"), "cgroupns_mode", "HostConfig.CgroupnsMode", None),
    ("cgroup", host(Cgroup="x"), "cgroup", "HostConfig.Cgroup", None),
    ("cgroup-parent", host(CgroupParent="/foo"), "cgroup_parent", "HostConfig.CgroupParent", None),
    (
        "device",
        host(Devices=[{"PathOnHost": "/dev/x"}]),
        "devices",
        "HostConfig.Devices[0]",
        "device-0-host_path",
    ),
    (
        "device-requests",
        host(DeviceRequests=[{"Driver": "nvidia"}]),
        "devices",
        "HostConfig.DeviceRequests",
        None,
    ),
    (
        "device-rules",
        host(DeviceCgroupRules=["c 1:3 mr"]),
        "devices",
        "HostConfig.DeviceCgroupRules",
        None,
    ),
    (
        "seccomp-unconfined",
        host(SecurityOpt=["seccomp=unconfined"]),
        "security_opt",
        "HostConfig.SecurityOpt[0]",
        None,
    ),
    ("empty-masked-paths", host(MaskedPaths=[]), "masked_paths", "HostConfig.MaskedPaths", None),
    (
        "empty-readonly-paths",
        host(ReadonlyPaths=[]),
        "masked_paths",
        "HostConfig.ReadonlyPaths",
        None,
    ),
    ("sysctls", host(Sysctls={"net.ipv4.ip_forward": "1"}), "sysctls", "HostConfig.Sysctls", None),
    ("runtime", host(Runtime="nvidia"), "runtime", "HostConfig.Runtime", "runtime"),
    (
        "log-driver",
        host(LogConfig={"Type": "syslog", "Config": {}}),
        "log_driver",
        "HostConfig.LogConfig.Type",
        "log_driver",
    ),
    (
        "log-option",
        host(LogConfig={"Type": "json-file", "Config": {"syslog-address": "tcp://x"}}),
        "log_driver",
        "HostConfig.LogConfig.Config[syslog-address]",
        "logopt-0-key",
    ),
    ("links", host(Links=["a:b"]), "links", "HostConfig.Links", None),
    (
        "endpoint-links",
        endpoint("net", Links=["a"]),
        "links",
        "NetworkingConfig.EndpointsConfig[net].Links",
        None,
    ),
    (
        "oom-kill-disable",
        host(OomKillDisable=True),
        "oom_kill_disable",
        "HostConfig.OomKillDisable",
        None,
    ),
    ("negative-oom-score", host(OomScoreAdj=-500), "oom_score_adj", "HostConfig.OomScoreAdj", None),
    (
        "realtime-cpu",
        host(CpuRealtimeRuntime=1000),
        "realtime_cpu",
        "HostConfig.CpuRealtimeRuntime",
        None,
    ),
    (
        "blkio-device",
        host(BlkioDeviceReadBps=[{"Path": "/dev/x", "Rate": 1}]),
        "blkio_devices",
        "HostConfig.BlkioDeviceReadBps",
        None,
    ),
    ("storage-opt", host(StorageOpt={"size": "1G"}), "storage_opt", "HostConfig.StorageOpt", None),
    ("annotations", host(Annotations={"a": "b"}), "annotations", "HostConfig.Annotations", None),
    ("isolation", host(Isolation="hyperv"), "isolation", "HostConfig.Isolation", None),
    (
        "endpoint-driver-options",
        endpoint("net", DriverOpts={"x": "y"}),
        "endpoint_driver_opts",
        "NetworkingConfig.EndpointsConfig[net].DriverOpts",
        None,
    ),
    (
        "container-id-file",
        host(ContainerIDFile="/tmp/x"),
        "container_id_file",
        "HostConfig.ContainerIDFile",
        None,
    ),
    ("unknown-body-key", {"Surprise": 1}, "unknown_option", "Surprise", None),
    ("unknown-host-key", host(NewThing=1), "unknown_option", "HostConfig.NewThing", None),
]

HOST_BACKED_ID = "host_backed_volume"
SWARM_PLACEMENT_ID = "swarm_volume_placement"


@pytest.mark.parametrize(
    ("body", "option", "path", "field"),
    [
        pytest.param(body, option, path, field, id=name)
        for name, body, option, path, field in OPTION_CASES
    ],
)
def test_each_restricted_option_is_found_at_its_engine_path_and_form_field(
    body: dict[str, Any], option: str, path: str, field: str | None
) -> None:
    assert found(body) == [(option, path, field)]


def test_every_option_id_that_a_body_can_reveal_has_a_case_above() -> None:
    covered = {option for _, _, option, _, _ in OPTION_CASES}

    assert set(OPTION_IDS) - covered == {HOST_BACKED_ID, SWARM_PLACEMENT_ID}


@pytest.mark.parametrize("mode", ["host", "HOST", " Host ", "host\t", "\thost\n"])
def test_host_networking_is_found_in_whatever_case_and_whitespace_it_is_spelled(mode: str) -> None:
    assert options_of(host(NetworkMode=mode)) == ["network_host"]


@pytest.mark.parametrize("mode", ["container:abc", "CONTAINER:abc", " Container:abc"])
def test_a_shared_network_namespace_is_found_in_whatever_case_it_is_spelled(mode: str) -> None:
    assert options_of(host(NetworkMode=mode)) == ["network_container"]


@pytest.mark.parametrize(
    ("name", "option"),
    [
        ("HOST", "network_host"),
        (" host", "network_host"),
        ("Container:x", "network_container"),
        ("CONTAINER:x", "network_container"),
    ],
)
def test_an_endpoint_named_for_a_special_network_is_found_in_any_case(
    name: str, option: str
) -> None:
    assert options_of(endpoint(name)) == [option]


def test_a_network_mode_that_is_not_a_string_is_unknown() -> None:
    assert options_of(host(NetworkMode=123)) == ["unknown_option"]


@pytest.mark.parametrize("mode", ["bridge", "none", "default", "my-network", "a" * 64])
def test_a_named_network_mode_is_not_restricted_here(mode: str) -> None:
    assert find_restricted(host(NetworkMode=mode)) == []


@pytest.mark.parametrize(
    "source",
    ["/etc", "./rel", "../up", "~/x", "/", "relative/dir", "", " /etc", "-flag", "a" * 256],
)
def test_a_bind_in_binds_is_found_for_every_form_of_host_path(source: str) -> None:
    assert options_of(host(Binds=[f"{source}:/x"])) == ["bind_mount"]


@pytest.mark.parametrize("entry", ["nocolon", ":/x", "", 42])
def test_a_binds_entry_with_no_volume_name_is_found(entry: Any) -> None:
    assert options_of(host(Binds=[entry])) == ["bind_mount"]


@pytest.mark.parametrize(
    "entry", ["data:/x", "data:/x:ro", "my-vol_1.x:/y:rw,nocopy", "v" * 255 + ":/x"]
)
def test_a_binds_entry_naming_a_volume_is_not_restricted(entry: str) -> None:
    assert find_restricted(host(Binds=[entry])) == []


def test_only_the_binds_entries_with_a_host_path_are_named_by_position() -> None:
    assert found(host(Binds=["data:/a", "/etc:/b", "other:/c", "../x:/d"])) == [
        ("bind_mount", "HostConfig.Binds[1]", None),
        ("bind_mount", "HostConfig.Binds[3]", None),
    ]


def test_binds_that_is_not_a_list_is_found() -> None:
    assert options_of(host(Binds="/etc:/x")) == ["bind_mount"]


@pytest.mark.parametrize("kind", [" bind ", "BIND", "Bind\n"])
def test_a_bind_mount_type_is_found_in_whatever_case_and_whitespace_it_is_spelled(
    kind: str,
) -> None:
    assert options_of(mount(kind, Source="/etc")) == ["bind_mount"]


@pytest.mark.parametrize(
    "kind", ["Volume", "TMPFS", "image", "npipe", "cluster", "", "other", None]
)
def test_a_mount_type_other_than_exactly_volume_or_tmpfs_is_found(kind: Any) -> None:
    assert options_of(mount(kind, Source="s")) == ["mount_type"]


def test_a_mount_that_is_not_a_mapping_is_found_as_a_mount_type() -> None:
    assert options_of(host(Mounts=["/etc:/x"])) == ["mount_type"]


@pytest.mark.parametrize(
    "body",
    [
        mount("volume", Source="data"),
        mount("volume"),
        mount("volume", Source="data", ReadOnly=True),
        mount("volume", Source="data", VolumeOptions={"NoCopy": True, "Labels": {"a": "b"}}),
        mount("volume", Source="data", VolumeOptions={"Subpath": "sub"}),
        mount("tmpfs", TmpfsOptions={"SizeBytes": 1024, "Mode": 1777}),
        mount("tmpfs"),
    ],
    ids=["named", "anonymous", "read-only", "volume-options", "subpath", "tmpfs-options", "tmpfs"],
)
def test_a_volume_or_tmpfs_mount_is_not_restricted(body: dict[str, Any]) -> None:
    assert find_restricted(body) == []


@pytest.mark.parametrize(
    "config",
    [
        {"Type": "local", "Config": {"max-size": "1m", "max-file": "3", "compress": "true"}},
        {"Type": "json-file", "Config": {"mode": "non-blocking", "max-buffer-size": "4m"}},
        {"Type": "none"},
        {"Type": "", "Config": {}},
        {"Type": "json-file", "Config": None},
        {},
    ],
    ids=["local", "json-file-buffer", "none", "blank-type", "null-config", "empty"],
)
def test_the_local_log_drivers_and_their_options_are_not_restricted(config: dict[str, Any]) -> None:
    assert find_restricted(host(LogConfig=config)) == []


@pytest.mark.parametrize(
    "driver",
    ["syslog", "gelf", "fluentd", "splunk", "awslogs", "journald", "JSON-FILE", "json-file "],
)
def test_any_other_log_driver_is_found_since_it_sends_from_the_daemon_host(driver: str) -> None:
    assert options_of(host(LogConfig={"Type": driver})) == ["log_driver"]


def test_a_log_option_outside_the_allowed_keys_is_found_by_its_position() -> None:
    config = {"Type": "json-file", "Config": {"max-size": "1m", "labels": "x", "env": "y"}}

    assert found(host(LogConfig=config), slots={"log_options": (5, 6, 7)}) == [
        ("log_driver", "HostConfig.LogConfig.Config[labels]", "logopt-6-key"),
        ("log_driver", "HostConfig.LogConfig.Config[env]", "logopt-7-key"),
    ]


@pytest.mark.parametrize(
    "options",
    [
        ["seccomp=unconfined"],
        ["apparmor=unconfined"],
        ["label=disable"],
        ["label=type:spc_t"],
        ["systempaths=unconfined"],
        ["no-new-privileges:false"],
        ["No-New-Privileges"],
        ["no-new-privileges", "seccomp=unconfined"],
        [7],
        "no-new-privileges",
    ],
)
def test_any_security_option_other_than_no_new_privileges_is_found(options: Any) -> None:
    assert set(options_of(host(SecurityOpt=options))) == {"security_opt"}


@pytest.mark.parametrize("options", [["no-new-privileges"], ["no-new-privileges:true"], []])
def test_no_new_privileges_is_not_restricted(options: list[str]) -> None:
    assert find_restricted(host(SecurityOpt=options)) == []


@pytest.mark.parametrize("key", ["MaskedPaths", "ReadonlyPaths"])
def test_an_empty_masked_or_readonly_path_list_is_found_though_empty(key: str) -> None:
    assert options_of(host(**{key: []})) == ["masked_paths"]


def test_a_respelled_masked_paths_key_is_unknown_and_still_treated_as_masked_paths() -> None:
    assert options_of(host(maskedpaths=[])) == ["unknown_option", "masked_paths"]


def test_a_null_masked_paths_is_treated_as_absent() -> None:
    assert find_restricted(host(MaskedPaths=None)) == []


@pytest.mark.parametrize("value", ["", "private"])
def test_a_private_or_unset_pid_and_cgroup_namespace_is_not_restricted(value: str) -> None:
    assert find_restricted(host(PidMode=value, CgroupnsMode=value)) == []


@pytest.mark.parametrize("value", ["", "private", "shareable", "none"])
def test_the_non_sharing_ipc_modes_are_not_restricted(value: str) -> None:
    assert find_restricted(host(IpcMode=value)) == []


@pytest.mark.parametrize("value", ["HOST", "Private", "shared", 3])
def test_a_namespace_mode_outside_the_allow_list_is_found(value: Any) -> None:
    assert options_of(host(PidMode=value, IpcMode=value)) == ["pid_namespace", "ipc_namespace"]


@pytest.mark.parametrize(
    ("body", "default", "expected"),
    [
        (host(Runtime="runc"), DEFAULT_RUNTIME, []),
        (host(Runtime="nvidia"), "nvidia", []),
        (host(Runtime="runc"), "nvidia", ["runtime"]),
        (host(Runtime="runc"), None, ["runtime"]),
        (host(Runtime="RUNC"), DEFAULT_RUNTIME, ["runtime"]),
        (host(Runtime=""), None, []),
    ],
    ids=["default", "default-custom", "not-default", "no-default-known", "case", "unset"],
)
def test_a_runtime_is_allowed_only_when_it_is_the_daemons_default(
    body: dict[str, Any], default: str | None, expected: list[str]
) -> None:
    assert options_of(body, default_runtime=default) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [(host(Runtime="x"), True), (host(Runtime=""), False), (host(), False), ({}, False)],
)
def test_the_default_runtime_is_needed_only_when_a_runtime_is_set(
    body: dict[str, Any], expected: bool
) -> None:
    assert needs_default_runtime(body) is expected


@pytest.mark.parametrize("score", [0, 1, 500])
def test_a_zero_or_positive_oom_score_is_not_restricted(score: int) -> None:
    assert find_restricted(host(OomScoreAdj=score)) == []


@pytest.mark.parametrize("value", [-1, "-1", True, 1.5, "x"])
def test_an_oom_score_that_is_not_a_positive_integer_is_found(value: Any) -> None:
    assert options_of(host(OomScoreAdj=value)) == ["oom_score_adj"]


@pytest.mark.parametrize("value", ["", "default"])
def test_the_default_isolation_is_not_restricted(value: str) -> None:
    assert find_restricted(host(Isolation=value)) == []


UNKNOWN_KEYS = [
    pytest.param({"Surprise": None}, "Surprise", id="body-null"),
    pytest.param({"Surprise": 0}, "Surprise", id="body-zero"),
    pytest.param({"Surprise": False}, "Surprise", id="body-false"),
    pytest.param({"image": "x"}, "image", id="body-lowercase-key"),
    pytest.param({"IMAGE": "x"}, "IMAGE", id="body-uppercase-key"),
    pytest.param({"hostconfig": {"Privileged": True}}, "hostconfig", id="host-config-lowercase"),
    pytest.param({"networkingconfig": {}}, "networkingconfig", id="networking-config-lowercase"),
    pytest.param(host(NewThing=None), "HostConfig.NewThing", id="host-null"),
    pytest.param(host(privileged=True), "HostConfig.privileged", id="host-lowercase-privileged"),
    pytest.param(host(CAPADD=["X"]), "HostConfig.CAPADD", id="host-uppercase-capadd"),
    pytest.param(
        host(Mounts=[{"Type": "volume", "Source": "v1", "Target": "/x", "Extra": 1}]),
        "HostConfig.Mounts[0].Extra",
        id="mount",
    ),
    pytest.param(
        mount("volume", Source="v1", VolumeOptions={"Extra": 1}),
        "HostConfig.Mounts[0].VolumeOptions.Extra",
        id="volume-options",
    ),
    pytest.param(
        mount("tmpfs", TmpfsOptions={"Extra": 1}),
        "HostConfig.Mounts[0].TmpfsOptions.Extra",
        id="tmpfs-options",
    ),
    pytest.param(
        host(LogConfig={"Type": "json-file", "Extra": 1}),
        "HostConfig.LogConfig.Extra",
        id="log-config",
    ),
    pytest.param(
        endpoint("net", Extra=1), "NetworkingConfig.EndpointsConfig[net].Extra", id="endpoint"
    ),
    pytest.param({"NetworkingConfig": {"Extra": {}}}, "NetworkingConfig.Extra", id="networking"),
]


@pytest.mark.parametrize(("body", "path"), UNKNOWN_KEYS)
def test_a_key_outside_the_known_sets_is_refused_whatever_its_value_or_case(
    body: dict[str, Any], path: str
) -> None:
    assert ("unknown_option", path) in [(option, p) for option, p, _ in found(body)]


@pytest.mark.parametrize("value", [1, "x", ["x"], True])
def test_a_host_config_that_is_not_a_mapping_is_found(value: Any) -> None:
    assert options_of({"HostConfig": value}) == ["unknown_option"]


@pytest.mark.parametrize("value", [None, [], {}, ""])
def test_an_empty_host_config_is_not_restricted(value: Any) -> None:
    assert find_restricted({"HostConfig": value}) == []


def test_a_hostile_key_name_is_replaced_by_a_placeholder_in_the_reported_path() -> None:
    hostile = "x" * 80 + "\n<script>"

    assert found(host(**{hostile: 1})) == [("unknown_option", "HostConfig.<key>", None)]


ALLOWED = [
    pytest.param(host(ReadonlyRootfs=True), id="read-only-root"),
    pytest.param(host(Tmpfs={"/run": "rw,exec,size=64m"}), id="tmpfs"),
    pytest.param(host(Init=True), id="init"),
    pytest.param(host(ExtraHosts=["db:10.0.0.5", "gw:host-gateway"]), id="extra-hosts"),
    pytest.param(
        host(Dns=["1.1.1.1"], DnsSearch=["example.test"], DnsOptions=["ndots:2"]), id="dns"
    ),
    pytest.param(
        host(PortBindings={"80/tcp": [{"HostIp": "", "HostPort": "8080"}]}, PublishAllPorts=True),
        id="ports",
    ),
    pytest.param(host(CapDrop=["ALL"], CapAdd=[]), id="dropped-capabilities"),
    pytest.param(host(Ulimits=[{"Name": "nofile", "Soft": 1024, "Hard": 2048}]), id="ulimits"),
    pytest.param(host(GroupAdd=["audio"]), id="group-add"),
    pytest.param(host(ShmSize=67108864), id="shm"),
    pytest.param(
        host(
            CpuShares=512,
            Memory=1,
            MemorySwap=2,
            MemoryReservation=1,
            NanoCpus=1,
            PidsLimit=10,
            CpusetCpus="0",
            CpusetMems="0",
            BlkioWeight=500,
            CpuPeriod=100000,
            CpuQuota=50000,
        ),
        id="resource-limits",
    ),
    pytest.param(host(AutoRemove=True), id="auto-remove"),
    pytest.param(host(RestartPolicy={"Name": "on-failure", "MaximumRetryCount": 3}), id="restart"),
    pytest.param(host(ConsoleSize=[24, 80]), id="console-size"),
    pytest.param(host(OomKillDisable=False, Privileged=False, CapAdd=[]), id="false-and-empty"),
    pytest.param(
        {
            "Image": "x",
            "Hostname": "h",
            "Domainname": "d",
            "User": "1000",
            "Env": ["A=1"],
            "Cmd": ["run"],
            "Entrypoint": ["tini"],
            "Labels": {"a": "b"},
            "Healthcheck": {"Test": ["CMD-SHELL", "true"]},
            "StopSignal": "SIGINT",
            "StopTimeout": 5,
            "ExposedPorts": {"80/tcp": {}},
            "Volumes": {"/data": {}},
            "WorkingDir": "/srv",
            "MacAddress": "02:42:ac:11:00:02",
        },
        id="config",
    ),
    pytest.param(
        endpoint(
            "net",
            MacAddress="02:42:ac:11:00:02",
            IPAMConfig={"IPv4Address": "10.0.0.5"},
            Aliases=["web"],
            GwPriority=1,
        ),
        id="endpoint",
    ),
    pytest.param(host(NetworkMode="bridge"), id="bridge"),
    pytest.param({"HostConfig": {}, "NetworkingConfig": {}}, id="empty-sections"),
    pytest.param({}, id="empty-body"),
]


@pytest.mark.parametrize("body", ALLOWED)
def test_an_option_that_is_harmless_to_the_host_yields_no_finding(body: dict[str, Any]) -> None:
    assert find_restricted(body) == []


def test_a_reported_row_position_comes_from_the_slots_for_every_row_group() -> None:
    body = {
        "HostConfig": {
            "Mounts": [{"Type": "bind", "Source": "/a", "Target": "/x"}],
            "Devices": [{"PathOnHost": "/dev/x"}],
        },
        "NetworkingConfig": {"EndpointsConfig": {"host": {}}},
    }
    slots = {"mounts": (4,), "devices": (6,), "networks": (2,)}

    assert [f.field for f in find_restricted(body, slots=slots)] == [
        "mount-4-type",
        "device-6-host_path",
        "network-2-name",
    ]


def test_without_slots_the_position_in_the_body_stands_in_for_the_row() -> None:
    body = host(
        Mounts=[
            {"Type": "volume", "Source": "v1", "Target": "/a"},
            {"Type": "bind", "Source": "/b", "Target": "/b"},
        ]
    )

    assert found(body) == [("bind_mount", "HostConfig.Mounts[1].Type", "mount-1-type")]


def test_several_findings_come_back_together_so_every_field_can_carry_its_error() -> None:
    body = host(Privileged=True, CapAdd=["SYS_ADMIN"], NetworkMode="host", PidMode="host")

    assert set(options_of(body)) == {"privileged", "cap_add", "network_host", "pid_namespace"}


def test_refused_options_lists_each_id_once_in_vocabulary_order() -> None:
    findings = [Finding("sysctls", "a"), Finding("privileged", "b"), Finding("sysctls", "c")]

    assert refused_options(findings) == ("privileged", "sysctls")


def test_a_finding_keys_on_its_form_field_and_falls_back_to_the_engine_path() -> None:
    assert Finding("privileged", "HostConfig.Privileged", "privileged").key == "privileged"
    assert Finding("sysctls", "HostConfig.Sysctls").key == "HostConfig.Sysctls"


def test_a_finding_message_names_the_option_and_an_unknown_one_says_so() -> None:
    assert Finding("bind_mount", "p").message == "Only administrators may use host bind mounts."
    assert "not recognised" in Finding("unknown_option", "p").message


def test_a_reach_finding_carries_the_option_path_and_field_it_was_given() -> None:
    finding = reach_finding(HOST_BACKED_ID, "HostConfig.Mounts[0].Source", "mount-0-source")

    assert (finding.option, finding.path, finding.field) == (
        HOST_BACKED_ID,
        "HostConfig.Mounts[0].Source",
        "mount-0-source",
    )


def test_a_reach_finding_for_an_option_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        reach_finding("not_an_option", "path")


@pytest.mark.parametrize("option", OPTION_IDS)
def test_every_option_id_has_a_label(option: str) -> None:
    assert option_label(option)


@pytest.mark.parametrize("option", [o for o in OPTION_IDS if o != "unknown_option"])
def test_every_option_id_but_the_catch_all_names_the_engine_paths_it_covers(option: str) -> None:
    assert option_engine_paths(option)
    assert isinstance(option_form_fields(option), tuple)


def test_the_option_vocabulary_has_thirty_four_ids_without_repeats() -> None:
    assert len(OPTION_IDS) == len(set(OPTION_IDS)) == 34
