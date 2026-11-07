"""Health check, process, host, security, limits and logging fields of the service form."""

import copy
from typing import Any

import pytest

from app.auth.errors import ValidationFailed
from app.services import service_spec as ss
from app.services.service_restrictions import find_restricted_service
from app.services.service_spec_task import (
    SecurityInput,
    canonical_capability,
    log_is_allowed,
)
from app.services.swarm_service_queries import TASK_CATEGORIES
from tests.services.test_service_spec import refs_for, sample_spec, stripped

Spec = dict[str, Any]
BARE = {"name": "web", "image": "nginx"}
IMAGE_REFS = ss.ResolvedRefs(networks={}, configs={}, secrets={}, image="nginx:latest")
HOUR_NS = 3_600_000_000_000
SECOND_NS = 1_000_000_000

SECTIONS: dict[str, dict[str, str]] = {
    "healthcheck": {
        "health_test": "cmd",
        "health_command": "curl -f 'http://localhost/a b'",
        "health_interval": "30s",
        "health_timeout": "5s",
        "health_retries": "3",
        "health_start_period": "1m10s",
        "health_start_interval": "2s",
    },
    "process": {
        "user": "1000:1000",
        "groups": "audio video",
        "working_dir": "/app",
        "init": "true",
        "stop_signal": "SIGINT",
        "stop_grace_period": "30s",
        "tty": "on",
        "open_stdin": "on",
    },
    "host": {
        "hostname": "web.example",
        "hosts": "db:10.0.0.5\ncache:fe80::1",
        "dns_nameservers": "8.8.8.8 2001:4860:4860::8888",
        "dns_search": "example.org lan",
        "dns_options": "ndots:2 timeout:3",
    },
    "security": {
        "read_only": "on",
        "no_new_privileges": "on",
        "cap_drop": "ALL",
        "cap_add": "net_bind_service CAP_SYS_TIME",
        "sysctls": "net.core.somaxconn=1024\nkernel.shmmax=65536",
    },
    "ulimits": {
        "ulimit-0-name": "nofile",
        "ulimit-0-soft": "1024",
        "ulimit-0-hard": "4096",
        "ulimit-1-name": "core",
        "ulimit-1-soft": "-1",
        "ulimit-1-hard": "-1",
    },
    "logging": {
        "log_driver": "json-file",
        "logopt-0-key": "max-size",
        "logopt-0-value": "10m",
        "logopt-1-key": "max-file",
        "logopt-1-value": "3",
    },
}
EVERYTHING = {key: value for fields in SECTIONS.values() for key, value in fields.items()}

EXPECTED_CONTAINER: dict[str, dict[str, Any]] = {
    "healthcheck": {
        "Healthcheck": {
            "Test": ["CMD", "curl", "-f", "http://localhost/a b"],
            "Interval": 30 * SECOND_NS,
            "Timeout": 5 * SECOND_NS,
            "Retries": 3,
            "StartPeriod": 70 * SECOND_NS,
            "StartInterval": 2 * SECOND_NS,
        }
    },
    "process": {
        "User": "1000:1000",
        "Groups": ["audio", "video"],
        "Dir": "/app",
        "Init": True,
        "StopSignal": "SIGINT",
        "StopGracePeriod": 30 * SECOND_NS,
        "TTY": True,
        "OpenStdin": True,
    },
    "host": {
        "Hostname": "web.example",
        "Hosts": ["10.0.0.5 db", "fe80::1 cache"],
        "DNSConfig": {
            "Nameservers": ["8.8.8.8", "2001:4860:4860::8888"],
            "Search": ["example.org", "lan"],
            "Options": ["ndots:2", "timeout:3"],
        },
    },
    "security": {
        "ReadOnly": True,
        "Privileges": {"NoNewPrivileges": True},
        "CapabilityDrop": ["ALL"],
        "CapabilityAdd": ["CAP_NET_BIND_SERVICE", "CAP_SYS_TIME"],
        "Sysctls": {"net.core.somaxconn": "1024", "kernel.shmmax": "65536"},
    },
    "ulimits": {
        "Ulimits": [
            {"Name": "nofile", "Soft": 1024, "Hard": 4096},
            {"Name": "core", "Soft": -1, "Hard": -1},
        ]
    },
}


def parse(
    form: dict[str, Any],
    *,
    creating: bool = True,
    admin: bool = True,
    current: Spec | None = None,
) -> ss.ServiceInput:
    return ss.parse_service_form(form, creating=creating, administrator=admin, current=current)


def errors(form: dict[str, Any], **options: Any) -> dict[str, str]:
    with pytest.raises(ValidationFailed) as raised:
        parse({**BARE, **form}, **options)
    return dict(raised.value.fields)


def created(fields: dict[str, str]) -> Spec:
    return ss.build_spec(parse({**BARE, **fields}), IMAGE_REFS)


def with_engine_defaults(spec: Spec) -> Spec:
    """Return the spec as the Engine stores it: its defaults added, absent values null."""
    stored = copy.deepcopy(spec)
    task = stored["TaskTemplate"]
    task.update(ForceUpdate=0, Runtime="container")
    container = task["ContainerSpec"]
    container.setdefault("Isolation", "default")
    container.setdefault("DNSConfig", {})
    container.setdefault("Init", None)
    privileges = container.setdefault("Privileges", {})
    privileges.setdefault("CredentialSpec", None)
    privileges.setdefault("SELinuxContext", None)
    return stored


def edited(
    spec: Spec, changes: dict[str, str] | None = None, *, admin: bool = True
) -> tuple[Spec, tuple[str, ...]]:
    values = {**ss.form_values(spec, administrator=admin), **(changes or {})}
    merged = ss.merge_spec(
        spec, parse(values, creating=False, admin=admin, current=spec), refs_for(spec)
    )
    return merged, ss.changed_categories(spec, merged)


@pytest.mark.parametrize("section", EXPECTED_CONTAINER)
def test_a_section_builds_exactly_the_engine_keys_it_manages(section: str) -> None:
    spec = created(SECTIONS[section])

    container = spec["TaskTemplate"]["ContainerSpec"]
    assert {key: container[key] for key in EXPECTED_CONTAINER[section]} == EXPECTED_CONTAINER[
        section
    ]
    assert set(container) == {"Image", *EXPECTED_CONTAINER[section]}


def test_the_log_driver_is_written_on_the_task_template() -> None:
    spec = created(SECTIONS["logging"])

    assert spec["TaskTemplate"]["LogDriver"] == {
        "Name": "json-file",
        "Options": {"max-size": "10m", "max-file": "3"},
    }
    assert "LogDriver" not in spec["TaskTemplate"]["ContainerSpec"]


def test_a_form_that_sets_none_of_the_sections_builds_a_spec_without_their_keys() -> None:
    spec = created({})

    assert set(spec["TaskTemplate"]) == {"ContainerSpec"}
    assert set(spec["TaskTemplate"]["ContainerSpec"]) == {"Image"}


@pytest.mark.parametrize("fields", [*SECTIONS.values(), EVERYTHING], ids=[*SECTIONS, "everything"])
def test_a_section_round_trips_through_the_engines_stored_form_without_a_change(
    fields: dict[str, str],
) -> None:
    stored = with_engine_defaults(created(fields))

    merged, categories = edited(stored)

    assert categories == ()
    assert stripped(merged) == stripped(stored)
    assert not set(categories) & TASK_CATEGORIES


EDITS: list[tuple[str, str, dict[str, str]]] = [
    ("health interval", "healthcheck", {"health_interval": "45s"}),
    ("health test", "healthcheck", {"health_test": "shell", "health_command": "true"}),
    ("stop grace period", "process", {"stop_grace_period": "31s"}),
    ("terminal off", "process", {"tty": ""}),
    ("search domains", "host", {"dns_search": "example.org"}),
    ("a host address", "host", {"hosts": "db:10.0.0.6\ncache:fe80::1"}),
    ("dropped capabilities", "security", {"cap_drop": ""}),
    ("added capabilities", "security", {"cap_add": "NET_BIND_SERVICE"}),
    ("no new privileges off", "security", {"no_new_privileges": ""}),
    ("a soft limit", "ulimits", {"ulimit-0-soft": "2048"}),
    ("a log option", "logging", {"logopt-0-value": "20m"}),
]


@pytest.mark.parametrize(
    ("category", "change"), [(c, ch) for _, c, ch in EDITS], ids=[n for n, _, _ in EDITS]
)
def test_editing_one_field_changes_exactly_its_category_and_replaces_tasks(
    category: str, change: dict[str, str]
) -> None:
    stored = with_engine_defaults(created(EVERYTHING))

    merged, categories = edited(stored, change)

    assert categories == (category,)
    assert set(categories) & TASK_CATEGORIES
    assert merged["Name"] == stored["Name"]


def test_clearing_a_section_removes_its_keys_and_names_only_its_category() -> None:
    stored = with_engine_defaults(created(EVERYTHING))
    blank = dict.fromkeys(SECTIONS["healthcheck"], "")

    merged, categories = edited(stored, blank)

    assert categories == ("healthcheck",)
    assert "Healthcheck" not in merged["TaskTemplate"]["ContainerSpec"]


STORED_VARIANTS: list[tuple[str, dict[str, Any]]] = [
    ("capabilities spelled bare", {"CapabilityAdd": ["NET_ADMIN"], "CapabilityDrop": ["ALL"]}),
    ("capabilities in another order", {"CapabilityDrop": ["CAP_SYS_TIME", "CAP_KILL"]}),
    ("a host entry with aliases", {"Hosts": ["10.0.0.5 db db.internal"]}),
    ("a host entry that is not an address", {"Hosts": ["not-an-address db"]}),
    ("a kernel parameter outside net and kernel", {"Sysctls": {"fs.mqueue.msg_max": "20"}}),
    ("a health check with zero values", {"Healthcheck": {"Test": ["NONE"], "Retries": 0}}),
    (
        "a health duration with no millisecond form",
        {"Healthcheck": {"Test": ["CMD-SHELL", "true"], "Interval": 1_500_000}},
    ),
    ("a health duration of 48 hours", {"Healthcheck": {"Interval": 48 * HOUR_NS}}),
    ("a health test with a second shell word", {"Healthcheck": {"Test": ["CMD-SHELL", "a", "b"]}}),
    ("a health test of a lower-case word", {"Healthcheck": {"Test": ["cmd", "x"]}}),
    ("a multi-line shell health test", {"Healthcheck": {"Test": ["CMD-SHELL", "a\nb"]}}),
    ("init switched off", {"Init": False}),
    ("a stop grace period of zero", {"StopGracePeriod": 0}),
    ("a stop signal spelled in lower case", {"StopSignal": "sigterm"}),
    ("a working directory of the root", {"Dir": "/"}),
    ("a user with a group", {"User": "app:app"}),
    ("a name server with upper-case hexadecimal", {"DNSConfig": {"Nameservers": ["2001:DB8::1"]}}),
    ("a resource limit of an unlisted name", {"Ulimits": [{"Name": "x", "Soft": 1, "Hard": 1}]}),
    ("explicit false flags", {"TTY": False, "OpenStdin": False, "ReadOnly": False}),
    ("no new privileges set to false", {"Privileges": {"NoNewPrivileges": False}}),
    (
        "a log driver with options of any name",
        {"LogDriver": {"Name": "fluentd", "Options": {"a": "b"}}},
    ),
]


@pytest.mark.parametrize(
    "extra", [variant for _, variant in STORED_VARIANTS], ids=[name for name, _ in STORED_VARIANTS]
)
def test_values_as_the_engine_holds_them_survive_an_unchanged_edit_byte_for_byte(
    extra: dict[str, Any],
) -> None:
    stored = with_engine_defaults(created({}))
    container = stored["TaskTemplate"]["ContainerSpec"]
    for key, value in extra.items():
        if key == "LogDriver":
            stored["TaskTemplate"]["LogDriver"] = value
        elif isinstance(value, dict) and isinstance(container.get(key), dict):
            container[key].update(value)
        else:
            container[key] = value

    merged, categories = edited(stored)

    assert categories == ()
    assert merged == stored


def test_a_service_keeps_the_fields_the_form_does_not_show_across_an_edit() -> None:
    stored = with_engine_defaults(created({}))
    container = stored["TaskTemplate"]["ContainerSpec"]
    container["Privileges"].update(
        Seccomp={"Mode": "unconfined"},
        AppArmor={"Mode": "disabled"},
        SELinuxContext={"Disable": True},
        CredentialSpec={"File": "spec.json"},
    )
    container.update(OomScoreAdj=100, Isolation="process")

    merged, categories = edited(stored, {"no_new_privileges": "on", "user": "1000"})

    kept = merged["TaskTemplate"]["ContainerSpec"]
    assert kept["Privileges"] == {**container["Privileges"], "NoNewPrivileges": True}
    assert kept["OomScoreAdj"] == 100 and kept["Isolation"] == "process"
    assert set(categories) == {"process", "security"}


def test_a_change_to_an_unshown_privilege_is_other_and_not_security() -> None:
    before = with_engine_defaults(created({}))
    after = copy.deepcopy(before)
    after["TaskTemplate"]["ContainerSpec"]["Privileges"]["Seccomp"] = {"Mode": "unconfined"}

    assert ss.changed_categories(before, after) == ("other",)


def test_editing_the_health_test_keeps_unlisted_health_fields_and_a_kept_test() -> None:
    stored = with_engine_defaults(created({}))
    stored["TaskTemplate"]["ContainerSpec"]["Healthcheck"] = {
        "Test": ["cmd", "x"],
        "Interval": 3 * SECOND_NS,
    }

    values = ss.form_values(stored, administrator=True)
    merged, categories = edited(stored, {"health_timeout": "4s"})

    assert values["health_test"] == "keep"
    assert categories == ("healthcheck",)
    assert merged["TaskTemplate"]["ContainerSpec"]["Healthcheck"] == {
        "Test": ["cmd", "x"],
        "Interval": 3 * SECOND_NS,
        "Timeout": 4 * SECOND_NS,
    }


@pytest.mark.parametrize(
    ("form", "test"),
    [
        ({"health_test": "none"}, ["NONE"]),
        ({"health_test": "cmd", "health_command": "a 'b c'"}, ["CMD", "a", "b c"]),
        ({"health_test": "shell", "health_command": "a && b"}, ["CMD-SHELL", "a && b"]),
        ({"health_interval": "10s"}, None),
    ],
    ids=["disabled", "exec form", "shell form", "inherit with a timing"],
)
def test_the_health_test_is_none_exec_shell_or_inherited(
    form: dict[str, str], test: list[str] | None
) -> None:
    health = created(form)["TaskTemplate"]["ContainerSpec"]["Healthcheck"]

    assert health.get("Test") == test


def test_a_standard_users_form_offers_no_added_capabilities_or_kernel_parameters() -> None:
    stored = with_engine_defaults(created({}))
    stored["TaskTemplate"]["ContainerSpec"].update(
        CapabilityAdd=["CAP_NET_ADMIN"], Sysctls={"net.core.somaxconn": "9"}, CapabilityDrop=["ALL"]
    )

    standard = ss.form_values(stored, administrator=False)
    administrator = ss.form_values(stored, administrator=True)

    assert "cap_add" not in standard and "sysctls" not in standard
    assert standard["locked_cap_add"] == "CAP_NET_ADMIN"
    assert standard["locked_sysctls"] == "net.core.somaxconn=9"
    assert standard["cap_drop"] == "ALL"
    assert administrator["cap_add"] == "CAP_NET_ADMIN" and "locked_cap_add" not in administrator


def test_a_standard_users_stored_added_capabilities_and_sysctls_are_carried_unchanged() -> None:
    stored = with_engine_defaults(created({}))
    stored["TaskTemplate"]["ContainerSpec"].update(
        CapabilityAdd=["NET_ADMIN"], Sysctls={"net.core.somaxconn": "9"}
    )

    merged, categories = edited(stored, {"user": "1000"}, admin=False)

    kept = merged["TaskTemplate"]["ContainerSpec"]
    assert kept["CapabilityAdd"] == ["NET_ADMIN"] and kept["Sysctls"] == {"net.core.somaxconn": "9"}
    assert categories == ("process",)
    parsed = parse(
        ss.form_values(stored, administrator=False), creating=False, admin=False, current=stored
    )
    assert parsed.task.security.cap_add is None and parsed.task.security.sysctls is None


@pytest.mark.parametrize(
    ("posted", "option"),
    [
        ({"cap_add": "NET_ADMIN"}, "cap_add"),
        ({"sysctls": "net.core.somaxconn=1"}, "sysctls"),
        ({"log_driver": "syslog"}, "log_driver"),
        ({"log_driver": "json-file", "logopt-0-key": "tag", "logopt-0-value": "x"}, "log_driver"),
    ],
    ids=["capabilities", "kernel parameters", "log driver", "log option"],
)
def test_a_value_a_standard_user_posts_for_a_refused_field_is_refused_by_the_walker(
    posted: dict[str, str], option: str
) -> None:
    parsed = parse({**BARE, **posted}, admin=False)

    findings = find_restricted_service(ss.build_spec(parsed, IMAGE_REFS))

    assert [finding.option for finding in findings] == [option]


def test_a_standard_users_allowed_values_pass_the_walker() -> None:
    allowed = {
        **SECTIONS["healthcheck"],
        **SECTIONS["process"],
        **SECTIONS["host"],
        **SECTIONS["ulimits"],
        "read_only": "on",
        "no_new_privileges": "on",
        "cap_drop": "ALL",
        "log_driver": "local",
        "logopt-0-key": "max-size",
        "logopt-0-value": "1m",
    }

    spec = ss.build_spec(parse({**BARE, **allowed}, admin=False), IMAGE_REFS)

    assert find_restricted_service(spec) == []


def test_a_stored_log_driver_outside_the_standard_allow_list_is_locked_for_a_standard_user() -> (
    None
):
    stored = with_engine_defaults(created({}))
    stored["TaskTemplate"]["LogDriver"] = {"Name": "fluentd", "Options": {"fluentd-address": "x"}}

    standard = ss.form_values(stored, administrator=False)
    administrator = ss.form_values(stored, administrator=True)
    merged, categories = edited(stored, {"user": "1"}, admin=False)

    assert standard["locked_log"] == "1" and standard["locked_log_driver"] == "fluentd"
    assert "log_driver" not in standard and "logopt-0-key" not in standard
    assert administrator["log_driver"] == "fluentd" and "locked_log" not in administrator
    assert merged["TaskTemplate"]["LogDriver"] == stored["TaskTemplate"]["LogDriver"]
    assert categories == ("process",)


@pytest.mark.parametrize(
    ("log", "allowed"),
    [
        (None, True),
        ({}, True),
        ({"Name": "json-file", "Options": {"max-size": "1m", "mode": "non-blocking"}}, True),
        ({"Name": "none"}, True),
        ({"Name": "syslog"}, False),
        ({"Name": "local", "Options": {"tag": "x"}}, False),
        ({"Name": "local", "Options": "x"}, False),
    ],
)
def test_the_log_driver_allow_list_matches_container_create(log: Any, allowed: bool) -> None:
    assert log_is_allowed(log) is allowed


def test_the_sample_documents_of_a_real_engine_still_round_trip_with_every_section_shown() -> None:
    spec = with_engine_defaults(sample_spec())

    for admin in (True, False):
        merged, categories = edited(spec, admin=admin)

        assert categories == ()
        assert merged == spec


ACCEPTED: list[tuple[str, dict[str, str]]] = [
    ("retries zero", {"health_retries": "0"}),
    ("retries one hundred", {"health_retries": "100"}),
    ("interval of one millisecond", {"health_interval": "1ms"}),
    ("interval of twenty-four hours", {"health_interval": "24h"}),
    ("start period zero", {"health_start_period": "0s"}),
    ("start period bare zero", {"health_start_period": "0"}),
    ("thirty-two groups", {"groups": " ".join(f"g{i}" for i in range(32))}),
    ("signal number one", {"stop_signal": "1"}),
    ("signal number sixty-four", {"stop_signal": "64"}),
    ("signal with a plus", {"stop_signal": "SIGRTMIN+3"}),
    ("thirty-two hosts", {"hosts": "\n".join(f"h{i}:10.0.0.{i}" for i in range(32))}),
    ("eight name servers", {"dns_nameservers": " ".join(f"10.0.0.{i}" for i in range(8))}),
    ("eight search domains", {"dns_search": " ".join(f"d{i}.test" for i in range(8))}),
    ("eight resolver options", {"dns_options": " ".join(f"ndots:{i}" for i in range(8))}),
    ("sixty-four capabilities", {"cap_drop": " ".join(["KILL"] * 64)}),
    ("sixty-four kernel parameters", {"sysctls": "\n".join(f"net.k{i}=1" for i in range(64))}),
    ("a kernel parameter with a space in its value", {"sysctls": "net.ipv4.ping_group_range=0 9"}),
    ("every listed limit name", {"ulimit-0-name": "rttime", "ulimit-0-soft": "1"}),
    ("an unlimited soft and hard limit", {"ulimit-0-name": "core", "ulimit-0-soft": "-1"}),
    (
        "an unlimited hard limit above a soft one",
        {**{"ulimit-0-name": "core"}, "ulimit-0-soft": "5", "ulimit-0-hard": "-1"},
    ),
    (
        "sixteen log options",
        {"log_driver": "local", **{f"logopt-{i}-key": f"k{i}" for i in range(16)}},
    ),
]


@pytest.mark.parametrize(
    "fields", [fields for _, fields in ACCEPTED], ids=[name for name, _ in ACCEPTED]
)
def test_values_at_the_edge_of_the_grammar_and_caps_are_accepted(fields: dict[str, str]) -> None:
    ss.parse_service_form({**BARE, **fields}, creating=True, administrator=True)


REFUSED: list[tuple[str, dict[str, str], str]] = [
    ("an unknown health test", {"health_test": "script"}, "health_test"),
    ("a health test that keeps a stored value on create", {"health_test": "keep"}, "health_test"),
    ("an exec test with no command", {"health_test": "cmd"}, "health_command"),
    ("a shell test with no command", {"health_test": "shell"}, "health_command"),
    ("a command with no test chosen", {"health_command": "true"}, "health_command"),
    ("an interval of zero", {"health_interval": "0s"}, "health_interval"),
    ("an interval under a millisecond", {"health_interval": "0ms"}, "health_interval"),
    ("an interval over twenty-four hours", {"health_interval": "24h1s"}, "health_interval"),
    ("a timeout in microseconds", {"health_timeout": "5us"}, "health_timeout"),
    (
        "a start period over twenty-four hours",
        {"health_start_period": "25h"},
        "health_start_period",
    ),
    ("a start interval of zero", {"health_start_interval": "0s"}, "health_start_interval"),
    ("retries above one hundred", {"health_retries": "101"}, "health_retries"),
    ("negative retries", {"health_retries": "-1"}, "health_retries"),
    ("retries that are not a number", {"health_retries": "x"}, "health_retries"),
    ("a user with a space", {"user": "a b"}, "user"),
    ("thirty-three groups", {"groups": " ".join(f"g{i}" for i in range(33))}, "groups"),
    ("a group with a slash", {"groups": "a/b"}, "groups"),
    ("a relative working directory", {"working_dir": "app"}, "working_dir"),
    ("a working directory with dot dot", {"working_dir": "/a/../b"}, "working_dir"),
    ("an unknown init value", {"init": "maybe"}, "init"),
    ("signal number zero", {"stop_signal": "0"}, "stop_signal"),
    ("signal number sixty-five", {"stop_signal": "65"}, "stop_signal"),
    ("a signal with a space", {"stop_signal": "SIG TERM"}, "stop_signal"),
    ("a stop grace period of twenty-five hours", {"stop_grace_period": "25h"}, "stop_grace_period"),
    ("a host name with an underscore", {"hostname": "a_b"}, "hostname"),
    ("a host entry without an address", {"hosts": "db"}, "hosts"),
    ("a host entry with a name for an address", {"hosts": "db:cache"}, "hosts"),
    (
        "thirty-three host entries",
        {"hosts": "\n".join(f"h{i}:10.0.0.1" for i in range(33))},
        "hosts",
    ),
    ("a name server that is a name", {"dns_nameservers": "dns.example"}, "dns_nameservers"),
    (
        "nine name servers",
        {"dns_nameservers": " ".join(f"10.0.0.{i}" for i in range(9))},
        "dns_nameservers",
    ),
    ("nine search domains", {"dns_search": " ".join(f"d{i}.test" for i in range(9))}, "dns_search"),
    ("a search domain with a slash", {"dns_search": "a/b"}, "dns_search"),
    ("nine resolver options", {"dns_options": " ".join(f"o{i}" for i in range(9))}, "dns_options"),
    ("a resolver option with an equals sign", {"dns_options": "ndots=2"}, "dns_options"),
    ("sixty-five capabilities", {"cap_drop": " ".join(["KILL"] * 65)}, "cap_drop"),
    ("an unknown dropped capability", {"cap_drop": "NOT_A_CAP"}, "cap_drop"),
    ("an unknown added capability", {"cap_add": "NOT_A_CAP"}, "cap_add"),
    ("a capability both added and dropped", {"cap_add": "KILL", "cap_drop": "CAP_KILL"}, "cap_add"),
    ("a kernel parameter outside net and kernel", {"sysctls": "vm.swappiness=1"}, "sysctls"),
    ("a kernel parameter without a value", {"sysctls": "net.core.somaxconn"}, "sysctls"),
    ("a kernel parameter set twice", {"sysctls": "net.a=1\nnet.a=2"}, "sysctls"),
    (
        "sixty-five kernel parameters",
        {"sysctls": "\n".join(f"net.k{i}=1" for i in range(65))},
        "sysctls",
    ),
    (
        "a limit of an unknown name",
        {"ulimit-0-name": "bogus", "ulimit-0-soft": "1"},
        "ulimit-0-name",
    ),
    (
        "a limit with no soft value",
        {"ulimit-0-name": "nofile", "ulimit-0-hard": "1"},
        "ulimit-0-soft",
    ),
    (
        "a soft limit above the hard limit",
        {"ulimit-0-name": "nofile", "ulimit-0-soft": "9", "ulimit-0-hard": "5"},
        "ulimit-0-soft",
    ),
    (
        "an unlimited soft limit under a hard one",
        {"ulimit-0-name": "nofile", "ulimit-0-soft": "-1", "ulimit-0-hard": "5"},
        "ulimit-0-soft",
    ),
    (
        "a hard limit that is not a number",
        {"ulimit-0-name": "nofile", "ulimit-0-soft": "1", "ulimit-0-hard": "x"},
        "ulimit-0-hard",
    ),
    (
        "a limit set twice",
        {
            "ulimit-0-name": "nofile",
            "ulimit-0-soft": "1",
            "ulimit-1-name": "nofile",
            "ulimit-1-soft": "1",
        },
        "ulimit-1-name",
    ),
    (
        "thirty-three limits",
        {f"ulimit-{i}-soft": "1" for i in range(33)}
        | {f"ulimit-{i}-name": "nofile" for i in range(33)},
        "ulimits",
    ),
    ("a log driver in upper case", {"log_driver": "JSON"}, "log_driver"),
    ("log options without a driver", {"log_driver": "", "logopt-0-key": "max-size"}, "log_driver"),
    (
        "a log option with a bad name",
        {"log_driver": "local", "logopt-0-key": "Max Size"},
        "logopt-0-key",
    ),
    (
        "a log option set twice",
        {"log_driver": "local", "logopt-0-key": "a", "logopt-1-key": "a"},
        "logopt-1-key",
    ),
    (
        "seventeen log options",
        {"log_driver": "local", **{f"logopt-{i}-key": f"k{i}" for i in range(17)}},
        "log_options",
    ),
]


@pytest.mark.parametrize(
    ("fields", "field"),
    [(fields, field) for _, fields, field in REFUSED],
    ids=[n for n, _, _ in REFUSED],
)
def test_values_outside_the_grammar_or_over_the_caps_are_refused_by_field(
    fields: dict[str, str], field: str
) -> None:
    assert field in errors(fields)


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("net_admin", "CAP_NET_ADMIN"),
        (" CAP_net_admin ", "CAP_NET_ADMIN"),
        ("all", "ALL"),
        ("CAP_ALL", "ALL"),
    ],
)
def test_capabilities_are_normalised_to_the_form_docker_stores(typed: str, stored: str) -> None:
    assert canonical_capability(typed) == stored


def test_added_capabilities_are_written_in_the_stored_form_without_repeats() -> None:
    parsed = parse({**BARE, "cap_add": "net_admin CAP_NET_ADMIN SYS_TIME"})

    assert parsed.task.security == SecurityInput(
        cap_add=("CAP_NET_ADMIN", "CAP_SYS_TIME"), sysctls=None
    )


def test_a_blank_hard_limit_takes_the_soft_value() -> None:
    parsed = parse({**BARE, "ulimit-0-name": "nofile", "ulimit-0-soft": "7"})

    assert [(r.name, r.soft, r.hard) for r in parsed.task.ulimits] == [("nofile", 7, 7)]


def test_a_blank_template_row_with_only_its_default_name_is_dropped() -> None:
    parsed = parse({**BARE, "ulimit-0-name": "core", "ulimit-0-soft": "", "ulimit-0-hard": ""})

    assert parsed.task.ulimits == ()


def test_every_new_category_leaves_other_and_is_in_the_vocabulary() -> None:
    added = {"healthcheck", "process", "host", "security", "ulimits", "logging"}

    assert added <= set(ss.CATEGORIES) and added <= TASK_CATEGORIES
    assert ss.CATEGORIES[-1] == "other"
