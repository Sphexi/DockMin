"""Tests for app/services/container_derivation.py: recreate configuration and duplicate prefill."""

import copy
from typing import Any

import pytest

from app.services.container_derivation import (
    BLOCKER_MESSAGES,
    DaemonDefaults,
    Duplicate,
    derive_recreate,
    image_is_id,
    recreate_blockers,
    spec_from_inspect,
)
from app.services.container_restrictions import find_restricted
from app.services.container_spec import FormState, parse_form
from tests.container_support import OLD_ID, OLD_IMAGE_ID
from tests.container_support import inspect_document as inspected

OLD = OLD_ID
SHORT = OLD[:12]
IMAGE_ID = OLD_IMAGE_ID
ANONYMOUS = "a" * 64
FRONT, BACK = "f" * 64, "e" * 64
SHARED_NAMESPACE = "container:" + "9" * 64
DAEMON = DaemonDefaults(runtime="runc", logging_driver="json-file", cgroup_version="1")
KIB, MIB = 1024, 1024 * 1024
MASKED = [
    "/proc/acpi",
    "/proc/kcore",
    "/proc/keys",
    "/proc/latency_stats",
    "/proc/timer_list",
    "/proc/sched_debug",
    "/proc/scsi",
    "/sys/firmware",
]
READONLY = ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"]
STACK_LABEL = "com.docker.compose.project"


def image(**config: Any) -> dict[str, Any]:
    return {"Config": config}


def notices_of(duplicate: Duplicate) -> dict[str, tuple[str, ...]]:
    return {notice.kind: notice.items for notice in duplicate.notices}


@pytest.mark.parametrize(
    ("document", "pull", "expected"),
    [
        pytest.param(inspected(LabelsTruncated=True), False, ("labels_truncated",), id="labels"),
        pytest.param(
            inspected(config={"Labels": {"com.docker.swarm.service.id": "x"}}),
            False,
            ("swarm_managed",),
            id="swarm",
        ),
        pytest.param(
            inspected(config={"Labels": {" COM.DOCKER.SWARM.SERVICE.ID ": "x"}}),
            False,
            ("swarm_managed",),
            id="swarm-in-any-case",
        ),
        pytest.param(
            inspected(host={"AutoRemove": True}), False, ("auto_remove",), id="auto-remove"
        ),
        pytest.param(
            inspected(config={"Image": "sha256:" + "a" * 64}),
            True,
            ("image_id_reference",),
            id="image-id-with-pull",
        ),
        pytest.param(
            inspected(config={"Image": "abcdef123456"}),
            True,
            ("image_id_reference",),
            id="short-id",
        ),
        pytest.param(inspected(config={"Image": "abcdef123456"}), False, (), id="image-id-no-pull"),
        pytest.param(inspected(name="ab" * 32), False, ("hex_name",), id="64-hex-name"),
        pytest.param(inspected(name="ab" * 31 + "a"), False, (), id="63-hex-name"),
        pytest.param(
            inspected(LabelsTruncated=True, host={"AutoRemove": True}, name="ab" * 32),
            False,
            ("labels_truncated", "auto_remove", "hex_name"),
            id="several",
        ),
        pytest.param(inspected(), True, (), id="none"),
    ],
)
def test_recreate_blockers_name_every_reason_a_container_cannot_be_recreated(
    document: dict[str, Any], pull: bool, expected: tuple[str, ...]
) -> None:
    assert recreate_blockers(document, pull=pull) == expected


def test_every_blocker_has_a_message_that_does_not_name_the_container() -> None:
    assert set(BLOCKER_MESSAGES) == {
        "labels_truncated",
        "swarm_managed",
        "auto_remove",
        "image_id_reference",
        "hex_name",
    }


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("nginx", False),
        ("nginx:1", False),
        ("sha256:" + "a" * 64, True),
        ("a" * 64, True),
        ("abcdef012345", True),
        ("abc", False),
        ("deadbeefdeadbeef", True),
    ],
)
def test_a_bare_image_id_is_told_from_a_name(reference: str, expected: bool) -> None:
    assert image_is_id(reference) is expected


def test_a_container_with_nothing_set_derives_its_image_and_network_mode_only() -> None:
    derived = derive_recreate(inspected())

    assert derived.body == {"Image": "nginx:1", "HostConfig": {"NetworkMode": "bridge"}}
    assert (derived.name, derived.old_short_id, derived.image) == ("web", SHORT, "nginx:1")
    assert derived.own_volumes == derived.own_network_ids == frozenset()


def test_the_image_reference_falls_back_to_the_image_id_when_the_config_has_none() -> None:
    derived = derive_recreate(inspected(config={"Image": ""}))

    assert derived.body["Image"] == derived.image == IMAGE_ID


def test_the_hostname_is_dropped_only_when_it_is_the_old_short_id() -> None:
    generated = derive_recreate(inspected())
    chosen = derive_recreate(inspected(config={"Hostname": "custom", "Domainname": "corp.test"}))

    assert "Hostname" not in generated.body
    assert (chosen.body["Hostname"], chosen.body["Domainname"]) == ("custom", "corp.test")


def test_the_deprecated_mac_address_and_the_container_id_file_are_dropped() -> None:
    derived = derive_recreate(
        inspected(config={"MacAddress": "02:42"}, host={"ContainerIDFile": "/run/x"})
    )

    assert "MacAddress" not in derived.body
    assert "ContainerIDFile" not in derived.body["HostConfig"]


@pytest.mark.parametrize("mode", [SHARED_NAMESPACE, "container:web"])
def test_a_container_that_shares_another_network_namespace_carries_no_hostname_or_domain(
    mode: str,
) -> None:
    document = inspected(
        config={"Hostname": "inherited", "Domainname": "corp.test"}, host={"NetworkMode": mode}
    )

    derived = derive_recreate(document)

    assert "Hostname" not in derived.body
    assert "Domainname" not in derived.body


@pytest.mark.parametrize("mode", ["host", "none"])
def test_host_and_none_modes_keep_the_hostname_and_carry_no_endpoints(mode: str) -> None:
    document = inspected(
        config={"Hostname": "kept"},
        host={"NetworkMode": mode},
        networks={mode: {"NetworkID": "c" * 64}},
    )

    derived = derive_recreate(document)

    assert derived.body["Hostname"] == "kept"
    assert "NetworkingConfig" not in derived.body
    assert derived.own_network_ids == frozenset()


def test_the_container_derived_from_is_not_modified() -> None:
    document = inspected(
        config={"Env": ["A=1"], "Labels": {"k": "v"}},
        host={"MaskedPaths": MASKED, "ReadonlyPaths": READONLY, "ContainerIDFile": "/x"},
        mounts=[{"Type": "volume", "Name": ANONYMOUS, "Destination": "/c", "RW": True}],
        networks={"front": {"NetworkID": FRONT, "Aliases": [SHORT, "web"]}},
    )
    before = copy.deepcopy(document)

    derive_recreate(document, image_inspect=image(Env=["A=1"], Labels={"k": "v"}))

    assert document == before


IMAGE_DEFAULTS: list[tuple[str, Any]] = [
    ("Cmd", ["nginx", "-g", "daemon off;"]),
    ("Entrypoint", ["/docker-entrypoint.sh"]),
    ("WorkingDir", "/srv"),
    ("User", "nginx"),
    ("Healthcheck", {"Test": ["CMD-SHELL", "true"], "Interval": 30_000_000_000}),
    ("StopSignal", "SIGQUIT"),
    ("Shell", ["/bin/bash", "-c"]),
]


@pytest.mark.parametrize(("key", "value"), IMAGE_DEFAULTS, ids=[k for k, _ in IMAGE_DEFAULTS])
def test_a_setting_equal_to_the_images_own_is_dropped_so_a_pull_can_replace_it(
    key: str, value: Any
) -> None:
    derived = derive_recreate(inspected(config={key: value}), image_inspect=image(**{key: value}))

    assert key not in derived.body


@pytest.mark.parametrize(("key", "value"), IMAGE_DEFAULTS, ids=[k for k, _ in IMAGE_DEFAULTS])
def test_a_setting_that_differs_from_the_images_is_kept(key: str, value: Any) -> None:
    derived = derive_recreate(
        inspected(config={key: value}), image_inspect=image(**{key: "something else"})
    )

    assert derived.body[key] == value


def test_environment_labels_ports_and_volumes_the_image_supplies_are_subtracted_pairwise() -> None:
    document = inspected(
        config={
            "Env": ["PATH=/bin", "A=2", "B=3"],
            "Labels": {"v": "1", "mine": "x", "changed": "mine"},
            "ExposedPorts": {"80/tcp": {}, "81/tcp": {}},
            "Volumes": {"/data": {}, "/mine": {}},
        }
    )
    base = image(
        Env=["PATH=/bin", "A=1"],
        Labels={"v": "1", "changed": "theirs"},
        ExposedPorts={"80/tcp": {}},
        Volumes={"/data": {}},
    )

    derived = derive_recreate(document, image_inspect=base)

    assert derived.body["Env"] == ["A=2", "B=3"]
    assert derived.body["Labels"] == {"mine": "x", "changed": "mine"}
    assert derived.body["ExposedPorts"] == {"81/tcp": {}}
    assert derived.body["Volumes"] == {"/mine": {}}
    assert derived.image_defaults_kept is False


def test_a_container_that_adds_nothing_to_its_images_environment_derives_none() -> None:
    document = inspected(config={"Env": ["PATH=/bin"], "Labels": {"v": "1"}})

    derived = derive_recreate(document, image_inspect=image(Env=["PATH=/bin"], Labels={"v": "1"}))

    assert "Env" not in derived.body
    assert "Labels" not in derived.body


def test_without_the_image_nothing_is_subtracted_and_the_loss_is_reported() -> None:
    document = inspected(config={"Env": ["PATH=/bin"], "Cmd": ["nginx"]})

    derived = derive_recreate(document, image_inspect=None)

    assert derived.body["Env"] == ["PATH=/bin"]
    assert derived.body["Cmd"] == ["nginx"]
    assert derived.image_defaults_kept is True


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ({"MaskedPaths": MASKED, "ReadonlyPaths": READONLY}, {}),
        ({"MaskedPaths": [*MASKED, "/proc/extra"], "ReadonlyPaths": READONLY}, {}),
        ({"MaskedPaths": [*MASKED, "/srv/keys"]}, {"MaskedPaths": [*MASKED, "/srv/keys"]}),
        ({"MaskedPaths": MASKED[:3]}, {"MaskedPaths": MASKED[:3]}),
        ({"MaskedPaths": []}, {"MaskedPaths": []}),
        ({"ReadonlyPaths": []}, {"ReadonlyPaths": []}),
    ],
    ids=[
        "daemon-defaults",
        "daemon-defaults-plus",
        "defaults-plus-a-path-of-its-own",
        "partial-masked",
        "empty-masked",
        "empty-readonly",
    ],
)
def test_the_daemons_default_path_lists_are_dropped_and_an_override_is_kept(
    host: dict[str, Any], expected: dict[str, Any]
) -> None:
    derived = derive_recreate(inspected(host=host))

    paths = {k: v for k, v in derived.body["HostConfig"].items() if k.endswith("Paths")}
    assert paths == expected


@pytest.mark.parametrize(
    ("host", "defaults", "expected"),
    [
        ({"LogConfig": {"Type": "json-file", "Config": {}}}, DAEMON, {}),
        ({"LogConfig": {"Type": "json-file", "Config": None}}, DAEMON, {}),
        (
            {"LogConfig": {"Type": "json-file", "Config": {"max-size": "1m"}}},
            DAEMON,
            {"LogConfig": {"Type": "json-file", "Config": {"max-size": "1m"}}},
        ),
        (
            {"LogConfig": {"Type": "syslog", "Config": {}}},
            DAEMON,
            {"LogConfig": {"Type": "syslog", "Config": {}}},
        ),
        (
            {"LogConfig": {"Type": "json-file", "Config": {}}},
            DaemonDefaults(logging_driver="local"),
            {"LogConfig": {"Type": "json-file", "Config": {}}},
        ),
        ({"CgroupnsMode": "host"}, DAEMON, {}),
        ({"CgroupnsMode": "host"}, DaemonDefaults(cgroup_version="2"), {"CgroupnsMode": "host"}),
        ({"CgroupnsMode": "private"}, DAEMON, {"CgroupnsMode": "private"}),
    ],
    ids=[
        "log-default",
        "log-default-null-options",
        "log-with-options",
        "log-other-driver",
        "log-differs-from-daemon",
        "cgroupns-host-on-v1",
        "cgroupns-host-on-v2",
        "cgroupns-private",
    ],
)
def test_settings_equal_to_the_daemons_own_defaults_are_left_for_the_daemon_to_apply(
    host: dict[str, Any], defaults: DaemonDefaults, expected: dict[str, Any]
) -> None:
    derived = derive_recreate(inspected(host=host), defaults=defaults)

    kept = {k: v for k, v in derived.body["HostConfig"].items() if k != "NetworkMode"}
    assert kept == expected


def test_without_daemon_defaults_the_logging_and_cgroup_settings_are_passed_through() -> None:
    host = {"LogConfig": {"Type": "json-file", "Config": {}}, "CgroupnsMode": "host"}

    derived = derive_recreate(inspected(host=host))

    assert derived.body["HostConfig"]["LogConfig"] == {"Type": "json-file", "Config": {}}
    assert derived.body["HostConfig"]["CgroupnsMode"] == "host"


def test_an_anonymous_volume_is_carried_by_name_so_its_data_survives() -> None:
    document = inspected(
        mounts=[
            {"Type": "volume", "Name": ANONYMOUS, "Destination": "/cache", "RW": True},
            {"Type": "volume", "Name": "b" * 64, "Destination": "/logs", "RW": False},
        ]
    )

    derived = derive_recreate(document)

    assert derived.body["HostConfig"]["Mounts"] == [
        {"Type": "volume", "Source": ANONYMOUS, "Target": "/cache"},
        {"Type": "volume", "Source": "b" * 64, "Target": "/logs", "ReadOnly": True},
    ]
    assert derived.anonymous_volumes == (ANONYMOUS, "b" * 64)


def test_a_volume_declared_in_binds_or_mounts_is_not_carried_a_second_time() -> None:
    document = inspected(
        host={
            "Binds": ["data:/data:ro"],
            "Mounts": [{"Type": "volume", "Source": "m1", "Target": "/m1"}],
        },
        mounts=[
            {"Type": "volume", "Name": "data", "Destination": "/data", "RW": False},
            {"Type": "volume", "Name": "m1", "Destination": "/m1", "RW": True},
            {"Type": "bind", "Source": "/etc", "Destination": "/etc"},
        ],
    )

    derived = derive_recreate(document)

    assert derived.body["HostConfig"]["Binds"] == ["data:/data:ro"]
    assert derived.body["HostConfig"]["Mounts"] == [
        {"Type": "volume", "Source": "m1", "Target": "/m1"}
    ]
    assert derived.anonymous_volumes == ()


def test_the_volumes_the_old_container_already_uses_are_reported_by_name() -> None:
    document = inspected(
        host={"Binds": ["data:/data", "/etc:/etc"], "Mounts": [{"Type": "volume", "Source": "m1"}]},
        mounts=[
            {"Type": "volume", "Name": "m1", "Destination": "/m1", "RW": True},
            {"Type": "volume", "Name": ANONYMOUS, "Destination": "/c", "RW": True},
        ],
    )

    assert derive_recreate(document).own_volumes == frozenset({"data", "m1", ANONYMOUS})


ENDPOINTS = {
    "front": {
        "NetworkID": FRONT,
        "Aliases": ["web", SHORT, OLD],
        "IPAMConfig": {"IPv4Address": "10.0.0.2"},
        "Links": ["db:db"],
        "DriverOpts": {"opt": "value"},
        "GwPriority": 5,
        "MacAddress": "02:42:ac:11:00:02",
    },
    "back": {
        "NetworkID": BACK,
        "Aliases": [],
        "IPAMConfig": None,
        "Links": [],
        "DriverOpts": {},
        "GwPriority": 0,
    },
}


def test_each_attached_network_becomes_an_endpoint_keyed_by_its_id() -> None:
    derived = derive_recreate(inspected(host={"NetworkMode": "front"}, networks=ENDPOINTS))

    assert derived.body["NetworkingConfig"]["EndpointsConfig"] == {
        FRONT: {
            "IPAMConfig": {"IPv4Address": "10.0.0.2"},
            "Links": ["db:db"],
            "Aliases": ["web"],
            "DriverOpts": {"opt": "value"},
            "GwPriority": 5,
        },
        BACK: {},
    }
    assert derived.own_network_ids == frozenset({FRONT, BACK})


def test_the_old_ids_are_stripped_from_aliases_and_the_endpoint_mac_is_not_carried() -> None:
    derived = derive_recreate(inspected(networks=ENDPOINTS))

    front = derived.body["NetworkingConfig"]["EndpointsConfig"][FRONT]
    assert front["Aliases"] == ["web"]
    assert "MacAddress" not in front


def test_a_network_without_an_id_is_keyed_by_its_name() -> None:
    derived = derive_recreate(inspected(networks={"legacy": {"Aliases": []}}))

    assert list(derived.body["NetworkingConfig"]["EndpointsConfig"]) == ["legacy"]


def test_a_standard_users_ordinary_container_derives_a_body_with_no_restricted_option() -> None:
    document = inspected(
        config={"Env": ["A=1"], "Labels": {"tier": "front"}},
        host={
            "LogConfig": {"Type": "json-file", "Config": {}},
            "CgroupnsMode": "host",
            "MaskedPaths": MASKED,
            "ReadonlyPaths": READONLY,
            "Binds": ["data:/data"],
            "RestartPolicy": {"Name": "always", "MaximumRetryCount": 0},
        },
        mounts=[{"Type": "volume", "Name": ANONYMOUS, "Destination": "/c", "RW": True}],
        networks={"front": {"NetworkID": FRONT, "Aliases": [SHORT], "MacAddress": "02:42"}},
    )

    body = derive_recreate(document, defaults=DAEMON).body

    assert find_restricted(body, default_runtime="runc") == []


@pytest.mark.parametrize(
    ("host", "option"),
    [
        ({"Privileged": True}, "privileged"),
        ({"CapAdd": ["SYS_ADMIN"]}, "cap_add"),
        ({"Binds": ["/etc:/etc"]}, "bind_mount"),
        ({"NetworkMode": "host"}, "network_host"),
        ({"MaskedPaths": []}, "masked_paths"),
        ({"SecurityOpt": ["seccomp=unconfined"]}, "security_opt"),
        ({"Devices": [{"PathOnHost": "/dev/mem"}]}, "devices"),
    ],
)
def test_a_restricted_option_on_the_old_container_survives_into_the_derived_body(
    host: dict[str, Any], option: str
) -> None:
    body = derive_recreate(inspected(host=host), defaults=DAEMON).body

    assert option in [f.option for f in find_restricted(body, default_runtime="runc")]


def test_an_unknown_engine_field_on_the_old_container_survives_into_the_derived_body() -> None:
    body = derive_recreate(inspected(host={"FutureOption": "on"})).body

    assert [f.option for f in find_restricted(body)] == ["unknown_option"]


RICH_HOST: dict[str, Any] = {
    "Privileged": True,
    "CapAdd": ["NET_ADMIN"],
    "CapDrop": ["ALL"],
    "PortBindings": {
        "80/tcp": [{"HostIp": "", "HostPort": "8080"}],
        "53/udp": [{"HostIp": "127.0.0.1", "HostPort": "5353"}],
    },
    "PublishAllPorts": True,
    "Binds": ["data:/data", "/etc/secret:/s:ro", f"{ANONYMOUS}:/anon"],
    "Mounts": [
        {"Type": "tmpfs", "Target": "/tmp", "TmpfsOptions": {"SizeBytes": MIB}},
        {"Type": "bind", "Source": "/srv/host", "Target": "/h"},
        {"Type": "volume", "Source": "v2", "Target": "/v2", "ReadOnly": True},
    ],
    "RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 4},
    "NanoCpus": 1_500_000_000,
    "CpuShares": 512,
    "Memory": 512 * MIB,
    "MemorySwap": 1024 * MIB,
    "PidsLimit": 100,
    "ShmSize": 128 * MIB,
    "Devices": [
        {"PathOnHost": "/dev/fuse", "PathInContainer": "/dev/fuse", "CgroupPermissions": "rwm"}
    ],
    "Dns": ["1.1.1.1"],
    "DnsSearch": ["x.test"],
    "DnsOptions": ["ndots:2"],
    "ExtraHosts": ["db:10.0.0.5"],
    "LogConfig": {"Type": "splunk", "Config": {"splunk-token": "TOKEN", "max-size": "1m"}},
    "Runtime": "nvidia",
    "Init": True,
    "PidMode": "host",
    "IpcMode": "host",
    "Ulimits": [{"Name": "nofile", "Soft": 1, "Hard": 2}],
    "Sysctls": {"net.core.somaxconn": "1024"},
}
RICH_CONFIG: dict[str, Any] = {
    "Env": ["DB_PASSWORD=hunter2", "MODE=fast"],
    "Cmd": ["serve", "--token=abc"],
    "Entrypoint": ["/entry"],
    "WorkingDir": "/srv",
    "User": "1000",
    "Labels": {
        "tier": "front",
        "db_password": "s3cret",
        "url": "https://user:pw@host/x",
        STACK_LABEL: "shop",
        "COM.DOCKER.STACK.NAMESPACE": "stack",
        "com.docker.swarm.task": "t",
        "plain": "ok",
    },
    "Healthcheck": {
        "Test": ["CMD-SHELL", "curl -u admin:hunter2 localhost"],
        "Interval": 30_000_000_000,
        "Retries": 3,
    },
    "ExposedPorts": {"80/tcp": {}, "9000/tcp": {}},
}


def rich() -> dict[str, Any]:
    return inspected(
        config=RICH_CONFIG,
        host=RICH_HOST,
        mounts=[
            {"Type": "volume", "Name": "data", "Destination": "/data", "RW": True},
            {"Type": "volume", "Name": ANONYMOUS, "Destination": "/anon", "RW": True},
            {"Type": "volume", "Name": "c" * 64, "Destination": "/cache", "RW": True},
            {"Type": "bind", "Source": "/etc/secret", "Destination": "/s"},
        ],
        networks={
            "front": {
                "NetworkID": FRONT,
                "Aliases": ["web", SHORT],
                "IPAMConfig": {"IPv4Address": "10.0.0.2"},
            },
            "bridge": {"NetworkID": BACK, "Aliases": []},
        },
    )


def duplicate(*, administrator: bool, **options: Any) -> Duplicate:
    cached = image(Volumes={"/cache": {}})
    return spec_from_inspect(
        rich(),
        is_administrator=administrator,
        image_inspect=options.pop("image_inspect", cached),
        defaults=DaemonDefaults(runtime="runc"),
        **options,
    )


def test_an_administrators_duplicate_carries_everything_the_form_can_represent() -> None:
    spec = duplicate(administrator=True).spec

    assert spec.image == "nginx:1"
    assert [(row.key, row.value) for row in spec.env] == [
        ("DB_PASSWORD", "hunter2"),
        ("MODE", "fast"),
    ]
    assert (spec.command, spec.entrypoint) == (("serve", "--token=abc"), ("/entry",))
    assert (spec.working_dir, spec.user) == ("/srv", "1000")
    assert [(p.host_ip, p.host_port, p.container_port, p.protocol) for p in spec.ports] == [
        ("127.0.0.1", "5353", "53", "udp"),
        ("", "8080", "80", "tcp"),
    ]
    assert spec.publish_all is True
    assert (spec.restart_policy, spec.restart_max_retries) == ("on-failure", 4)
    assert (spec.cpus, spec.cpu_shares, spec.memory) == (  # type: ignore[comparison-overlap]
        1.5,
        512,
        512 * MIB,
    )
    assert (spec.memory_swap, spec.pids_limit, spec.shm_size) == (1024 * MIB, 100, 128 * MIB)
    assert (spec.cap_add, spec.cap_drop) == (("NET_ADMIN",), ("ALL",))
    assert [(d.host_path, d.permissions) for d in spec.devices] == [("/dev/fuse", "rwm")]
    assert spec.dns.servers == ("1.1.1.1",) and spec.dns.search == ("x.test",)
    assert [(h.name, h.address) for h in spec.extra_hosts] == [("db", "10.0.0.5")]
    assert (spec.healthcheck.mode, spec.healthcheck.retries) == ("command", 3)
    assert spec.healthcheck.test == "curl -u admin:hunter2 localhost"
    assert spec.log.driver == "splunk"
    assert [(o.key, o.value) for o in spec.log.options] == [
        ("splunk-token", "TOKEN"),
        ("max-size", "1m"),
    ]
    assert (spec.runtime, spec.init, spec.privileged) == ("nvidia", True, True)
    assert (spec.pid_host, spec.ipc_host) == (True, True)


def test_a_duplicate_is_a_fresh_create_that_pulls_if_missing_and_starts() -> None:
    result = duplicate(administrator=True)

    assert (result.spec.pull, result.spec.start) == ("missing", True)
    assert result.suggested_name == "web-copy"
    assert result.spec.name is None


def test_an_administrators_mounts_keep_binds_and_turn_an_anonymous_source_into_a_blank_one() -> (
    None
):
    mounts = duplicate(administrator=True).spec.mounts

    assert [(m.type, m.source, m.target, m.read_only) for m in mounts] == [
        ("tmpfs", "", "/tmp", False),
        ("bind", "/srv/host", "/h", False),
        ("volume", "v2", "/v2", True),
        ("volume", "data", "/data", False),
        ("bind", "/etc/secret", "/s", True),
        ("volume", "", "/anon", False),
    ]


def test_an_administrators_duplicate_lists_the_engine_fields_the_form_cannot_set() -> None:
    notices = notices_of(duplicate(administrator=True))

    assert notices["not_copied"] == (
        "Config.ExposedPorts",
        "HostConfig.Sysctls",
        "HostConfig.Ulimits",
    )


def test_a_duplicate_states_which_anonymous_volumes_will_start_empty() -> None:
    notices = notices_of(duplicate(administrator=True))

    assert notices["anonymous_volumes"] == ("/anon", "/cache")


def test_a_volume_the_image_declares_is_not_turned_into_a_mount_row() -> None:
    mounts = duplicate(administrator=True).spec.mounts

    assert "/cache" not in [m.target for m in mounts]


@pytest.mark.parametrize("administrator", [True, False])
def test_stack_compose_and_swarm_labels_are_dropped_for_everyone_in_any_case(
    administrator: bool,
) -> None:
    result = duplicate(administrator=administrator)

    assert [row.key for row in result.spec.labels] == ["tier", "db_password", "url", "plain"]
    assert notices_of(result)["labels_dropped"] == (
        "COM.DOCKER.STACK.NAMESPACE",
        "com.docker.compose.project",
        "com.docker.swarm.task",
    )


def test_a_standard_users_duplicate_withholds_the_environment_command_and_entrypoint() -> None:
    result = duplicate(administrator=False)

    assert (result.spec.env, result.spec.command, result.spec.entrypoint) == ((), None, None)
    assert notices_of(result)["withheld"][:3] == (
        "command",
        "entrypoint",
        "environment variables",
    )


def test_a_standard_users_duplicate_blanks_label_values_that_may_be_secrets() -> None:
    labels = {row.key: row.value for row in duplicate(administrator=False).spec.labels}

    assert labels == {"tier": "front", "db_password": "", "url": "", "plain": "ok"}


def test_a_standard_users_duplicate_withholds_the_health_check_command_and_keeps_its_timings() -> (
    None
):
    check = duplicate(administrator=False).spec.healthcheck

    assert (check.mode, check.test, check.interval, check.retries) == (
        "command",
        "",
        30_000_000_000,
        3,
    )


def test_a_standard_users_duplicate_asks_for_the_health_check_command_again() -> None:
    state = FormState.from_spec(duplicate(administrator=False).spec)

    reparsed = parse_form(dict(state.values))

    assert set(reparsed.state.errors) == {"healthcheck_test"}


def test_a_standard_users_duplicate_drops_a_log_driver_that_sends_off_the_host() -> None:
    log = duplicate(administrator=False).spec.log

    assert (log.driver, log.options) == (None, ())


def test_a_standard_user_keeps_an_allowed_log_driver_and_only_its_allowed_options() -> None:
    document = inspected(
        host={
            "LogConfig": {
                "Type": "local",
                "Config": {"max-size": "1m", "labels": "x", "tag": "{{.Name}}"},
            }
        }
    )

    log = spec_from_inspect(document, is_administrator=False).spec.log

    assert log.driver == "local"
    assert [(o.key, o.value) for o in log.options] == [("max-size", "1m")]


def test_a_standard_users_duplicate_leaves_out_every_option_only_administrators_may_set() -> None:
    spec = duplicate(administrator=False).spec

    assert (spec.privileged, spec.cap_add, spec.devices, spec.runtime) == (False, (), (), None)
    assert (spec.pid_host, spec.ipc_host) == (False, False)
    assert [m.type for m in spec.mounts] == ["tmpfs", "volume", "volume", "volume"]
    assert spec.cap_drop == ("ALL",)


def test_a_standard_users_duplicate_names_the_options_it_did_not_copy_by_id_and_label() -> None:
    notice = next(n for n in duplicate(administrator=False).notices if n.kind == "restricted")

    assert notice.options == (
        "privileged",
        "cap_add",
        "bind_mount",
        "pid_namespace",
        "ipc_namespace",
        "devices",
        "sysctls",
        "runtime",
        "log_driver",
    )
    assert "host bind mounts" in notice.items
    assert notice.text.startswith("Not copied, only administrators may set them: ")


def test_a_standard_users_duplicate_does_not_report_a_restricted_field_as_merely_uncopyable() -> (
    None
):
    not_copied = notices_of(duplicate(administrator=False))["not_copied"]

    assert "HostConfig.Sysctls" not in not_copied


@pytest.mark.parametrize("administrator", [True, False])
def test_an_anonymous_volume_with_a_64_hex_name_never_prefills_as_a_source(
    administrator: bool,
) -> None:
    sources = [m.source for m in duplicate(administrator=administrator).spec.mounts]

    assert ANONYMOUS not in sources


def test_a_64_hex_source_in_a_mounts_entry_is_blanked_too() -> None:
    document = inspected(
        host={"Mounts": [{"Type": "volume", "Source": ANONYMOUS, "Target": "/c"}]},
        mounts=[{"Type": "volume", "Name": ANONYMOUS, "Destination": "/c", "RW": True}],
    )

    spec = spec_from_inspect(document, is_administrator=True).spec

    assert [(m.source, m.target) for m in spec.mounts] == [("", "/c")]


def test_an_image_id_reference_is_replaced_by_the_reference_given() -> None:
    document = inspected(config={"Image": "sha256:" + "c" * 64})

    spec = spec_from_inspect(document, is_administrator=False, image_reference="nginx:1.2").spec

    assert spec.image == "nginx:1.2"


def test_the_suggested_name_is_cut_to_the_name_limit() -> None:
    result = spec_from_inspect(inspected(name="n" * 130), is_administrator=True)

    assert result.suggested_name == ("n" * 130 + "-copy")[:128]


def test_a_value_the_form_would_refuse_is_dropped_with_a_notice_instead_of_failing() -> None:
    result = spec_from_inspect(inspected(host={"Dns": ["not-an-ip"]}), is_administrator=True)

    assert result.spec.dns.servers == ()
    assert notices_of(result)["dropped_invalid"] == ("dns_servers",)


def test_a_shared_network_namespace_is_not_copied_and_is_reported_to_an_administrator() -> None:
    document = inspected(host={"NetworkMode": SHARED_NAMESPACE})

    result = spec_from_inspect(document, is_administrator=True)

    assert result.spec.network_mode == "bridge"
    assert notices_of(result)["not_copied"] == ("HostConfig.NetworkMode",)


def test_host_networking_is_copied_for_an_administrator_and_refused_for_a_standard_user() -> None:
    document = inspected(host={"NetworkMode": "host"})

    administrator = spec_from_inspect(document, is_administrator=True)
    standard = spec_from_inspect(document, is_administrator=False)

    assert (administrator.spec.network_mode, standard.spec.network_mode) == ("host", "bridge")
    assert notices_of(standard)["restricted"] == ("host networking",)


def test_every_user_defined_network_becomes_a_row_with_the_primary_first() -> None:
    result = duplicate(administrator=False)

    assert result.spec.network_mode == "networks"
    assert [(n.name, n.aliases, n.ipv4) for n in result.spec.networks] == [
        ("bridge", (), ""),
        ("front", ("web",), "10.0.0.2"),
    ]


def test_a_duplicate_with_nothing_to_report_has_no_notices() -> None:
    assert spec_from_inspect(inspected(), is_administrator=False).notices == ()


def test_a_notice_reads_as_one_sentence_naming_its_items() -> None:
    notice = next(n for n in duplicate(administrator=True).notices if n.kind == "labels_dropped")

    assert notice.text == (
        "Not copied, a duplicate is not a member of a stack: "
        "COM.DOCKER.STACK.NAMESPACE, com.docker.compose.project, com.docker.swarm.task."
    )


def test_an_anonymous_volume_declared_by_a_mount_entry_is_carried_by_name() -> None:
    document = inspected(
        host={"Mounts": [{"Type": "volume", "Target": "/scratch"}]},
        mounts=[{"Type": "volume", "Name": ANONYMOUS, "Destination": "/scratch", "RW": True}],
    )

    derived = derive_recreate(document)

    assert derived.body["HostConfig"]["Mounts"] == [
        {"Type": "volume", "Source": ANONYMOUS, "Target": "/scratch"}
    ]


def test_a_container_run_with_the_socket_a_volume_and_a_group_keeps_all_of_it_but_its_image() -> (
    None
):
    old = inspected(
        name="dockmin",
        config={
            "Image": "ghcr.io/example/dockmin:1.0.0",
            "Env": ["PATH=/usr/bin", "DOCKMIN_PORT=8080", "DOCKMIN_SECRET_KEY_FILE=/run/key"],
            "Labels": {"app": "dockmin", "tier": "control"},
            "StopTimeout": 30,
            "User": "10001:10001",
        },
        host={
            "Binds": ["/var/run/docker.sock:/var/run/docker.sock", "dockmin-data:/data"],
            "GroupAdd": ["999"],
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "PortBindings": {"8080/tcp": [{"HostIp": "", "HostPort": "8080"}]},
            "SecurityOpt": ["no-new-privileges:true"],
        },
        mounts=[
            {
                "Type": "bind",
                "Source": "/var/run/docker.sock",
                "Destination": "/var/run/docker.sock",
                "RW": True,
            },
            {"Type": "volume", "Name": "dockmin-data", "Destination": "/data", "RW": True},
        ],
        networks={"front": {"NetworkID": FRONT, "Aliases": [SHORT, "dockmin"], "MacAddress": "02"}},
    )

    body = derive_recreate(old, image_inspect=image(Env=["PATH=/usr/bin"]), defaults=DAEMON).body
    body["Image"] = "ghcr.io/example/dockmin:1.2.0"

    assert body == {
        "Image": "ghcr.io/example/dockmin:1.2.0",
        "Env": ["DOCKMIN_PORT=8080", "DOCKMIN_SECRET_KEY_FILE=/run/key"],
        "Labels": {"app": "dockmin", "tier": "control"},
        "StopTimeout": 30,
        "User": "10001:10001",
        "HostConfig": {
            "NetworkMode": "bridge",
            "Binds": ["/var/run/docker.sock:/var/run/docker.sock", "dockmin-data:/data"],
            "GroupAdd": ["999"],
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "PortBindings": {"8080/tcp": [{"HostIp": "", "HostPort": "8080"}]},
            "SecurityOpt": ["no-new-privileges:true"],
        },
        "NetworkingConfig": {"EndpointsConfig": {FRONT: {"Aliases": ["dockmin"]}}},
    }
