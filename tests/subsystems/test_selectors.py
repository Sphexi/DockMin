"""Target selectors: parsing, canonical form, matching and the access check."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.stack import StackKind
from app.docker_engine.errors import EnvironmentUnreachable
from app.services.access import ResourceKey
from app.services.object_inspector import EnvironmentUnavailable
from app.subsystems.selectors import (
    ALL_KINDS,
    COMPOSE_ONLY,
    CONTAINER_KINDS,
    MAX_LABEL_KEY,
    MAX_LABEL_TERMS,
    MAX_LABEL_VALUE,
    MAX_SELECTOR_BYTES,
    MAX_TARGET_NAMES,
    NOT_OFFERED,
    NOT_SWARM,
    SELECTOR_FIELD,
    UNREADABLE_TARGET,
    UNVERIFIED,
    ContainerTarget,
    EnvironmentTarget,
    LabelsTarget,
    ServiceLabelsTarget,
    ServiceTarget,
    StackTarget,
    TargetUnverified,
    authorise_selector,
    canonical,
    carries,
    compose_managed,
    describe,
    describe_stored,
    find_container,
    find_containers,
    find_service,
    matches,
    only_name,
    parse_selector_text,
)

ENVIRONMENT_ID = 7
CONTAINER_ID = "a" * 64
SERVICE_ID = "s" + "0" * 24
SHORT_ID = "s" * 12
BOTH_KINDS: frozenset[StackKind] = frozenset({"compose", "swarm"})
PROJECT_LABEL = "com.docker.compose.project"
LONGEST_CONTAINER_NAME = 128
OWNER = Principal(user_id=3, username="owner", role="standard")


def selector(**data: Any) -> str:
    return json.dumps(data)


def labels_selector(*terms: tuple[str, str | None]) -> str:
    return selector(kind="labels", labels=[list(term) for term in terms])


def service_labels_selector(*terms: tuple[str, str | None]) -> str:
    return selector(kind="service_labels", labels=[list(term) for term in terms])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            selector(kind="container", names=["web"]),
            '{"kind":"container","names":["web"]}',
        ),
        (
            selector(kind="container", names=["web", "db", "web"]),
            '{"kind":"container","names":["db","web"]}',
        ),
        (
            selector(kind="container", names=["web", "Web"]),
            '{"kind":"container","names":["Web","web"]}',
        ),
        (selector(kind="stack", names=["shop"]), '{"kind":"stack","names":["shop"]}'),
        (selector(kind="stack", names=["Shop"]), '{"kind":"stack","names":["Shop"]}'),
        (
            selector(kind="stack", names=["shop", "blog"]),
            '{"kind":"stack","names":["blog","shop"]}',
        ),
        (
            selector(kind="service", names=["web-1", "api"]),
            '{"kind":"service","names":["api","web-1"]}',
        ),
        (selector(kind="environment"), '{"kind":"environment"}'),
        (
            labels_selector(("tier", None), ("app", "web")),
            '{"kind":"labels","labels":[["app","web"],["tier",null]]}',
        ),
        (
            service_labels_selector(("tier", None), ("app", "web")),
            '{"kind":"service_labels","labels":[["app","web"],["tier",null]]}',
        ),
        (
            ' {"names": ["web"], "kind": "container"} ',
            '{"kind":"container","names":["web"]}',
        ),
    ],
)
def test_a_valid_selector_parses_to_one_canonical_text(text: str, expected: str) -> None:
    parsed = parse_selector_text(text, ALL_KINDS)

    assert canonical(parsed) == expected
    assert parse_selector_text(expected, ALL_KINDS) == parsed


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "null",
        selector(kind="nonsense"),
        selector(kind="container"),
        selector(kind="container", names=["web"], extra=1),
        selector(kind="environment", names=["web"]),
        selector(kind="container", name="web"),
        selector(kind="stack", name="shop"),
        selector(kind="service", name="web"),
        selector(kind="container", names=[]),
        selector(kind="container", names="web"),
        selector(kind="container", names=[""]),
        selector(kind="container", names=["web", ""]),
        selector(kind="container", names=["-web"]),
        selector(kind="container", names=["web", "a b"]),
        selector(kind="container", names=["../etc"]),
        selector(kind="container", names=["x" * (LONGEST_CONTAINER_NAME + 1)]),
        selector(kind="container", names=[1]),
        selector(kind="stack", names=["-shop"]),
        selector(kind="stack", names=["a b"]),
        selector(kind="stack", names=["x" * 64]),
        selector(kind="service", names=[]),
        selector(kind="service", names=["-web"]),
        selector(kind="service", names=["web", "web-"]),
        selector(kind="service", names=["a b"]),
        selector(kind="service", names=["x" * 64]),
        selector(kind="labels", labels=[]),
        labels_selector(*[(f"k{n}", None) for n in range(MAX_LABEL_TERMS + 1)]),
        labels_selector(("app", "a"), ("app", "b")),
        labels_selector(("a=b", None)),
        labels_selector(("", None)),
        labels_selector(("k" * (MAX_LABEL_KEY + 1), None)),
        labels_selector(("app", "v" * (MAX_LABEL_VALUE + 1))),
        labels_selector(("app", "line\nbreak")),
        selector(kind="labels", labels="app=web"),
        selector(kind="service_labels", labels=[]),
        service_labels_selector(*[(f"k{n}", None) for n in range(MAX_LABEL_TERMS + 1)]),
        service_labels_selector(("app", "a"), ("app", "b")),
        service_labels_selector(("a=b", None)),
        service_labels_selector(("", None)),
        service_labels_selector(("k" * (MAX_LABEL_KEY + 1), None)),
        service_labels_selector(("app", "v" * (MAX_LABEL_VALUE + 1))),
        service_labels_selector(("app", "line\nbreak")),
        selector(kind="service_labels", names=["web"]),
    ],
)
def test_an_invalid_selector_is_refused_on_the_target_field(text: str) -> None:
    with pytest.raises(ValidationFailed) as refused:
        parse_selector_text(text, ALL_KINDS)

    assert set(refused.value.fields) == {SELECTOR_FIELD}


@pytest.mark.parametrize("kind", ["container", "stack", "service"])
def test_a_list_of_the_most_names_is_accepted_and_one_more_is_refused(kind: str) -> None:
    fits = selector(kind=kind, names=[f"n{n:02}" for n in range(MAX_TARGET_NAMES)])
    too_many = selector(kind=kind, names=[f"n{n:02}" for n in range(MAX_TARGET_NAMES + 1)])

    parsed = parse_selector_text(fits, ALL_KINDS)
    with pytest.raises(ValidationFailed) as refused:
        parse_selector_text(too_many, ALL_KINDS)

    assert len(parsed.names) == MAX_TARGET_NAMES  # type: ignore[union-attr]
    assert refused.value.fields == {SELECTOR_FIELD: f"Name 1 to {MAX_TARGET_NAMES} targets."}


def test_names_that_repeat_collapse_before_the_count_is_taken() -> None:
    names = [f"n{n:02}" for n in range(MAX_TARGET_NAMES)] * 2

    parsed = parse_selector_text(selector(kind="container", names=names), ALL_KINDS)

    assert len(parsed.names) == MAX_TARGET_NAMES  # type: ignore[union-attr]


@pytest.mark.parametrize("names", [["shop", "Shop"], ["SHOP", "shop", "blog"]])
def test_stack_names_that_differ_only_by_case_are_refused(names: list[str]) -> None:
    with pytest.raises(ValidationFailed) as refused:
        parse_selector_text(selector(kind="stack", names=names), ALL_KINDS)

    assert refused.value.fields == {SELECTOR_FIELD: "Name each stack once."}


def test_a_selector_with_a_single_name_field_instead_of_names_is_refused() -> None:
    with pytest.raises(ValidationFailed) as refused:
        parse_selector_text('{"kind":"container","name":"web"}', ALL_KINDS)

    assert set(refused.value.fields) == {SELECTOR_FIELD}


def test_a_selector_over_four_kibibytes_is_refused_before_it_is_decoded() -> None:
    text = selector(kind="container", names=["x" * MAX_SELECTOR_BYTES])

    with pytest.raises(ValidationFailed) as refused:
        parse_selector_text(text, ALL_KINDS)

    assert MAX_SELECTOR_BYTES == 4096
    assert refused.value.fields[SELECTOR_FIELD] == "Use at most 4 KiB."


def test_twenty_container_names_of_the_longest_length_fit_in_the_limit() -> None:
    names = [f"{n:02}".ljust(LONGEST_CONTAINER_NAME, "x") for n in range(MAX_TARGET_NAMES)]

    parsed = parse_selector_text(selector(kind="container", names=names), ALL_KINDS)

    assert len(parsed.names) == MAX_TARGET_NAMES  # type: ignore[union-attr]
    assert len(canonical(parsed).encode()) <= MAX_SELECTOR_BYTES


def test_the_service_kind_is_offered_to_all_kinds_and_not_to_the_container_kinds() -> None:
    text = selector(kind="service", names=["web"])

    assert CONTAINER_KINDS | {"service", "service_labels"} == ALL_KINDS
    assert "service" not in CONTAINER_KINDS
    assert parse_selector_text(text, ALL_KINDS) == ServiceTarget(names=("web",))
    with pytest.raises(ValidationFailed) as refused:
        parse_selector_text(text, CONTAINER_KINDS)
    assert refused.value.fields[SELECTOR_FIELD] == NOT_OFFERED


def test_a_kind_the_subsystem_does_not_offer_is_refused() -> None:
    with pytest.raises(ValidationFailed) as refused:
        parse_selector_text(selector(kind="environment"), frozenset({"container", "stack"}))

    assert refused.value.fields[SELECTOR_FIELD] == NOT_OFFERED


def test_the_widest_label_selector_still_fits_in_the_limit() -> None:
    terms = [(f"{n}" * 5, "v" * 20) for n in range(MAX_LABEL_TERMS)]

    parsed = parse_selector_text(labels_selector(*terms), ALL_KINDS)

    assert len(canonical(parsed).encode()) <= MAX_SELECTOR_BYTES


@pytest.mark.parametrize(
    ("target", "words"),
    [
        (ContainerTarget(names=("web",)), "Container web"),
        (ContainerTarget(names=("db", "web")), "Containers db, web"),
        (ContainerTarget(names=("a", "b", "c")), "Containers a, b, c"),
        (ContainerTarget(names=("a", "b", "c", "d")), "Containers a, b, c and 1 more"),
        (ContainerTarget(names=tuple("abcdefg")), "Containers a, b, c and 4 more"),
        (StackTarget(names=("shop",)), "Stack shop"),
        (StackTarget(names=("blog", "shop")), "Stacks blog, shop"),
        (ServiceTarget(names=("web",)), "Swarm service web"),
        (ServiceTarget(names=("api", "db", "q", "web")), "Swarm services api, db, q and 1 more"),
        (EnvironmentTarget(), "Every container in the environment"),
        (
            LabelsTarget(labels=(("app", "web"), ("tier", None))),
            "Containers labelled app=web, tier",
        ),
        (
            ServiceLabelsTarget(labels=(("app", "web"), ("tier", None))),
            "Swarm services labelled app=web, tier",
        ),
    ],
)
def test_a_selector_is_described_in_words(target: Any, words: str) -> None:
    assert describe(target) == words
    assert describe_stored(canonical(target)) == words


def test_an_unreadable_stored_selector_is_described_as_such() -> None:
    assert describe_stored("{broken") == UNREADABLE_TARGET


@pytest.mark.parametrize(
    ("target", "name", "labels", "expected"),
    [
        (ContainerTarget(names=("web",)), "web", {}, True),
        (ContainerTarget(names=("web",)), "web-2", {}, False),
        (ContainerTarget(names=("web",)), "my-web", {}, False),
        (ContainerTarget(names=("db", "web")), "web", {}, True),
        (ContainerTarget(names=("db", "web")), "db", {}, True),
        (ContainerTarget(names=("db", "web")), "cache", {}, False),
        (StackTarget(names=("shop",)), "any", {PROJECT_LABEL: "shop"}, True),
        (StackTarget(names=("shop",)), "any", {PROJECT_LABEL: "Shop"}, True),
        (StackTarget(names=("shop",)), "any", {PROJECT_LABEL: "shop2"}, False),
        (StackTarget(names=("shop",)), "any", {"Com.Docker.Compose.Project": "shop"}, False),
        (StackTarget(names=("shop",)), "any", {}, False),
        (StackTarget(names=("blog", "shop")), "any", {PROJECT_LABEL: "shop"}, True),
        (StackTarget(names=("blog", "shop")), "any", {PROJECT_LABEL: "blog"}, True),
        (StackTarget(names=("blog", "shop")), "any", {PROJECT_LABEL: "wiki"}, False),
        (LabelsTarget(labels=(("app", "web"),)), "n", {"app": "web", "x": "y"}, True),
        (LabelsTarget(labels=(("app", "web"),)), "n", {"app": "api"}, False),
        (LabelsTarget(labels=(("app", None),)), "n", {"app": "anything"}, True),
        (LabelsTarget(labels=(("app", None),)), "n", {"other": "x"}, False),
        (LabelsTarget(labels=(("app", ""),)), "n", {"app": "x"}, False),
        (LabelsTarget(labels=(("a", "1"), ("b", "2"))), "n", {"a": "1"}, False),
        (LabelsTarget(labels=(("a", "1"), ("b", "2"))), "n", {"a": "1", "b": "2"}, True),
        (EnvironmentTarget(), "anything", {}, True),
        (ServiceTarget(names=("web",)), "web", {}, False),
        (ServiceTarget(names=("web",)), "web", {PROJECT_LABEL: "web"}, False),
        (ServiceLabelsTarget(labels=(("app", None),)), "n", {"app": "web"}, False),
    ],
)
def test_a_selector_matches_a_container_by_name_stack_or_labels(
    target: Any, name: str, labels: dict[str, str], expected: bool
) -> None:
    assert matches(target, name=name, labels=labels) is expected


@pytest.mark.parametrize(
    ("terms", "labels", "expected"),
    [
        ((("app", "web"),), {"app": "web", "x": "y"}, True),
        ((("app", "web"),), {"app": "api"}, False),
        ((("app", None),), {"app": "anything"}, True),
        ((("app", None),), {"other": "x"}, False),
        ((("app", ""),), {"app": "x"}, False),
        ((("app", ""),), {"app": ""}, True),
        ((("a", "1"), ("b", None)), {"a": "1", "b": "x"}, True),
        ((("a", "1"), ("b", None)), {"a": "1"}, False),
        ((("App", None),), {"app": "x"}, False),
    ],
)
def test_label_terms_match_by_key_alone_or_by_key_and_exact_value(
    terms: Any, labels: dict[str, str], expected: bool
) -> None:
    assert carries(terms, labels) is expected


class RecordingAuthoriser:
    """Records each access check and refuses the levels it was told to refuse."""

    def __init__(
        self,
        refuse: frozenset[str] = frozenset(),
        error: type[Exception] = PermissionDenied,
        unwritable: frozenset[str] = frozenset(),
    ) -> None:
        self.calls: list[tuple[Principal, ResourceKey, str]] = []
        self._refuse = refuse
        self._error = error
        self._unwritable = unwritable

    async def require_access(self, principal: Principal, key: ResourceKey, level: str) -> None:
        self.calls.append((principal, key, level))
        if level in self._refuse or (level == "write" and key.resource_id in self._unwritable):
            raise self._error("insufficient access")


EVERY_LEVEL = frozenset({"read", "write"})
WRITE_ONLY = frozenset({"write"})


def container_key(identifier: str = CONTAINER_ID) -> ResourceKey:
    return ResourceKey(ENVIRONMENT_ID, "container", identifier)


class FakeHosts:
    """Hands out a client whose container list is the rows it was given, or an unreachable host."""

    def __init__(
        self,
        *names: str,
        labels: dict[str, dict[str, str]] | None = None,
        unreachable: bool = False,
    ) -> None:
        self.filters: list[Any] = []
        self._rows = [
            SimpleNamespace(
                id=format(10 + index, "x") * 64,
                names=[f"/{name}"],
                labels=(labels or {}).get(name, {}),
            )
            for index, name in enumerate(names)
        ]
        self._unreachable = unreachable

    async def client(self, environment_id: int) -> "FakeHosts":
        return self

    @property
    def containers(self) -> "FakeHosts":
        return self

    async def list(self, *, all: bool, filters: Any) -> list[Any]:
        self.filters.append(filters)
        if self._unreachable:
            raise EnvironmentUnreachable(ENVIRONMENT_ID, "down")
        return self._rows


class FakeStacks:
    def __init__(self, kind: str | None = None, name: str = "shop") -> None:
        self._row = None if kind is None else SimpleNamespace(kind=kind, name=name)
        self.asked: list[str] = []

    async def get_folded(self, environment_id: int, name: str) -> Any:
        self.asked.append(name)
        return self._row


async def authorise(
    target: Any,
    *,
    hosts: FakeHosts | None = None,
    stacks: FakeStacks | None = None,
    authoriser: RecordingAuthoriser | None = None,
    refuse_compose_managed: bool = False,
) -> RecordingAuthoriser:
    used = authoriser or RecordingAuthoriser()
    await authorise_selector(
        OWNER,
        ENVIRONMENT_ID,
        target,
        used,  # type: ignore[arg-type]
        hosts or FakeHosts(),  # type: ignore[arg-type]
        stacks or FakeStacks(),
        refuse_compose_managed=refuse_compose_managed,
    )
    return used


async def test_an_existing_container_is_checked_for_read_then_write_access_by_its_id() -> None:
    used = await authorise(ContainerTarget(names=("web",)), hosts=FakeHosts("web"))

    assert used.calls == [
        (OWNER, container_key(), "read"),
        (OWNER, container_key(), "write"),
    ]


async def test_a_container_that_does_not_exist_yet_passes_without_a_check() -> None:
    used = await authorise(ContainerTarget(names=("web",)), hosts=FakeHosts("other"))

    assert used.calls == []


async def test_each_named_container_that_exists_is_checked_and_the_missing_one_is_skipped() -> None:
    hosts = FakeHosts("db", "web")

    used = await authorise(ContainerTarget(names=("cache", "db", "web")), hosts=hosts)

    assert [(call[1].resource_id, call[2]) for call in used.calls] == [
        ("a" * 64, "read"),
        ("a" * 64, "write"),
        ("b" * 64, "read"),
        ("b" * 64, "write"),
    ]
    assert hosts.filters == [{"name": [f"^/{name}$"]} for name in ("cache", "db", "web")]


@pytest.mark.parametrize("error", [PermissionDenied, ResourceNotFound])
async def test_a_container_the_user_cannot_see_is_saved_as_if_it_did_not_exist(
    error: type[Exception],
) -> None:
    refusing = RecordingAuthoriser(EVERY_LEVEL, error)

    used = await authorise(
        ContainerTarget(names=("web",)),
        hosts=FakeHosts("web", labels={"web": {PROJECT_LABEL: "shop"}}),
        authoriser=refusing,
        refuse_compose_managed=True,
    )

    assert [call[2] for call in used.calls] == ["read"]


async def test_a_container_the_user_can_see_but_not_write_to_is_refused() -> None:
    with pytest.raises(PermissionDenied):
        await authorise(
            ContainerTarget(names=("web",)),
            hosts=FakeHosts("web"),
            authoriser=RecordingAuthoriser(WRITE_ONLY),
        )


async def test_one_unwritable_name_among_several_refuses_the_whole_selector() -> None:
    with pytest.raises(PermissionDenied):
        await authorise(
            ContainerTarget(names=("db", "web")),
            hosts=FakeHosts("db", "web"),
            authoriser=RecordingAuthoriser(unwritable=frozenset({"b" * 64})),
        )


async def test_a_compose_managed_container_is_refused_with_its_stack_offered_when_asked() -> None:
    hosts = FakeHosts("web", labels={"web": {PROJECT_LABEL: "shop"}})

    with pytest.raises(ValidationFailed) as refused:
        await authorise(ContainerTarget(names=("web",)), hosts=hosts, refuse_compose_managed=True)

    assert refused.value.fields == {
        SELECTOR_FIELD: (
            "web is managed by the Compose project shop. "
            "Choose the stack shop as the target instead."
        )
    }


async def test_a_compose_managed_container_passes_unless_the_refusal_is_asked_for() -> None:
    hosts = FakeHosts("web", labels={"web": {PROJECT_LABEL: "shop"}})

    used = await authorise(ContainerTarget(names=("web",)), hosts=hosts)

    assert [call[2] for call in used.calls] == ["read", "write"]


async def test_a_compose_managed_container_is_not_mentioned_to_a_user_who_may_not_write_to_it() -> (
    None
):
    hosts = FakeHosts("web", labels={"web": {PROJECT_LABEL: "shop"}})

    with pytest.raises(PermissionDenied):
        await authorise(
            ContainerTarget(names=("web",)),
            hosts=hosts,
            authoriser=RecordingAuthoriser(WRITE_ONLY),
            refuse_compose_managed=True,
        )


async def test_the_refusal_names_the_unnamed_project_when_the_label_is_not_a_stack_name() -> None:
    hosts = FakeHosts("web", labels={"web": {PROJECT_LABEL: "not a stack"}})

    with pytest.raises(ValidationFailed) as refused:
        await authorise(ContainerTarget(names=("web",)), hosts=hosts, refuse_compose_managed=True)

    assert refused.value.fields[SELECTOR_FIELD] == (
        "web is managed by a Compose project. Choose its stack as the target instead."
    )


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ({}, None),
        ({"other": "x"}, None),
        ({PROJECT_LABEL: "shop"}, "web is managed by the Compose project shop"),
        ({PROJECT_LABEL: "not a stack"}, "web is managed by a Compose project"),
    ],
)
def test_a_compose_managed_container_is_described_by_its_project(
    labels: dict[str, str], expected: str | None
) -> None:
    assert compose_managed("web", labels) == expected


async def test_a_container_lookup_matches_the_exact_name_only() -> None:
    hosts = FakeHosts("web-2")

    row = await find_container(hosts, "web")  # type: ignore[arg-type]

    assert row is None
    assert hosts.filters == [{"name": ["^/web$"]}]


async def test_a_container_name_is_escaped_in_the_engine_name_filter() -> None:
    hosts = FakeHosts("a.b")

    await find_container(hosts, "a.b")  # type: ignore[arg-type]

    assert hosts.filters == [{"name": [r"^/a\.b$"]}]


async def test_several_container_lookups_answer_in_the_order_given_with_none_for_a_miss() -> None:
    hosts = FakeHosts("web")

    found = await find_containers(hosts, ["gone", "web"])  # type: ignore[arg-type]

    assert [(name, row and row.id) for name, row in found] == [("gone", None), ("web", "a" * 64)]
    assert hosts.filters == [{"name": ["^/gone$"]}, {"name": ["^/web$"]}]


def test_narrowing_a_selector_to_one_name_keeps_its_kind() -> None:
    narrowed = only_name(StackTarget(names=("blog", "shop")), "shop")

    assert narrowed == StackTarget(names=("shop",))


async def test_an_unreachable_host_leaves_a_container_target_unverified() -> None:
    with pytest.raises(TargetUnverified) as raised:
        await authorise(ContainerTarget(names=("web",)), hosts=FakeHosts(unreachable=True))

    assert str(raised.value) == UNVERIFIED
    assert isinstance(raised.value, EnvironmentUnavailable)


async def test_a_compose_stack_row_is_checked_for_read_then_write_access_by_name() -> None:
    used = await authorise(StackTarget(names=("shop",)), stacks=FakeStacks("compose", "shop"))

    key = ResourceKey(ENVIRONMENT_ID, "stack", "shop")
    assert used.calls == [(OWNER, key, "read"), (OWNER, key, "write")]


async def test_a_stack_that_does_not_exist_yet_passes_without_a_check() -> None:
    stacks = FakeStacks(None)

    used = await authorise(StackTarget(names=("blog", "shop")), stacks=stacks)

    assert used.calls == []
    assert stacks.asked == ["blog", "shop"]


async def test_a_swarm_stack_is_refused_to_a_user_who_may_write_to_it() -> None:
    with pytest.raises(ValidationFailed) as refused:
        await authorise(StackTarget(names=("shop",)), stacks=FakeStacks("swarm"))

    assert refused.value.fields[SELECTOR_FIELD] == COMPOSE_ONLY


async def test_a_swarm_stack_the_user_may_not_write_to_reveals_nothing_about_its_kind() -> None:
    with pytest.raises(PermissionDenied):
        await authorise(
            StackTarget(names=("shop",)),
            stacks=FakeStacks("swarm"),
            authoriser=RecordingAuthoriser(WRITE_ONLY),
        )


async def test_a_stack_the_user_cannot_see_is_saved_as_if_it_did_not_exist() -> None:
    used = await authorise(
        StackTarget(names=("shop",)),
        stacks=FakeStacks("swarm"),
        authoriser=RecordingAuthoriser(EVERY_LEVEL),
    )

    assert [call[2] for call in used.calls] == ["read"]


@pytest.mark.parametrize("target", [EnvironmentTarget(), LabelsTarget(labels=(("app", None),))])
async def test_dynamic_selectors_need_no_target_check(target: Any) -> None:
    hosts = FakeHosts("web")

    used = await authorise(target, hosts=hosts, stacks=FakeStacks("compose"))

    assert used.calls == []
    assert hosts.filters == []


async def authorise_service_labels(hosts: "FakeSwarmHosts") -> RecordingAuthoriser:
    used = RecordingAuthoriser()
    await authorise_selector(
        OWNER,
        ENVIRONMENT_ID,
        ServiceLabelsTarget(labels=(("app", None),)),
        used,  # type: ignore[arg-type]
        hosts,  # type: ignore[arg-type]
        FakeStacks(),
    )
    return used


async def test_services_selected_by_labels_on_a_manager_need_no_host_read_or_check() -> None:
    hosts = FakeSwarmHosts("swarm")

    used = await authorise_service_labels(hosts)

    assert (used.calls, hosts.clients, hosts.filters) == ([], 0, [])


async def test_services_selected_by_labels_outside_a_manager_are_refused() -> None:
    hosts = FakeSwarmHosts("standalone")

    with pytest.raises(ValidationFailed) as refused:
        await authorise_service_labels(hosts)

    assert refused.value.fields == {SELECTOR_FIELD: NOT_SWARM}
    assert hosts.clients == 0


def test_the_label_limit_the_message_states_is_the_limit_that_is_enforced() -> None:
    too_many = labels_selector(*[(f"k{n}", None) for n in range(MAX_LABEL_TERMS + 1)])
    fits = labels_selector(*[(f"k{n}", None) for n in range(MAX_LABEL_TERMS)])

    with pytest.raises(ValidationFailed) as refused:
        parse_selector_text(too_many, ALL_KINDS)

    assert f"1 to {MAX_LABEL_TERMS}" in refused.value.fields[SELECTOR_FIELD]
    assert len(parse_selector_text(fits, ALL_KINDS).labels) == MAX_LABEL_TERMS  # type: ignore[union-attr]


class FakeSwarmHosts:
    """Hands out a manager whose service list is the rows it was given."""

    def __init__(
        self,
        environment_type: str = "swarm",
        *rows: tuple[str, str],
        unreachable: bool = False,
    ) -> None:
        self.type = environment_type
        self.filters: list[Any] = []
        self.clients = 0
        self._rows = [SimpleNamespace(id=found, spec=SimpleNamespace(name=n)) for found, n in rows]
        self._unreachable = unreachable

    async def environment(self, environment_id: int) -> Any:
        return SimpleNamespace(type=self.type)

    async def client(self, environment_id: int) -> "FakeSwarmHosts":
        self.clients += 1
        return self

    @property
    def swarm_services(self) -> "FakeSwarmHosts":
        return self

    async def list(self, *, filters: Any) -> list[Any]:
        self.filters.append(filters)
        if self._unreachable:
            raise EnvironmentUnreachable(ENVIRONMENT_ID, "down")
        return self._rows


async def authorise_service(
    hosts: FakeSwarmHosts,
    *names: str,
    authoriser: RecordingAuthoriser | None = None,
) -> RecordingAuthoriser:
    used = authoriser or RecordingAuthoriser()
    await authorise_selector(
        OWNER,
        ENVIRONMENT_ID,
        ServiceTarget(names=names or ("web",)),
        used,  # type: ignore[arg-type]
        hosts,  # type: ignore[arg-type]
        FakeStacks(),
    )
    return used


async def test_a_service_target_outside_a_manager_is_refused_without_asking_the_host() -> None:
    hosts = FakeSwarmHosts("standalone", (SERVICE_ID, "web"))

    with pytest.raises(ValidationFailed) as refused:
        await authorise_service(hosts)

    assert refused.value.fields == {SELECTOR_FIELD: NOT_SWARM}
    assert hosts.clients == 0


async def test_an_existing_service_is_checked_for_read_then_write_access_by_its_id() -> None:
    used = await authorise_service(FakeSwarmHosts("swarm", (SERVICE_ID, "web")))

    key = ResourceKey(ENVIRONMENT_ID, "service", SERVICE_ID)
    assert used.calls == [(OWNER, key, "read"), (OWNER, key, "write")]


async def test_a_service_that_does_not_exist_yet_passes_without_a_check() -> None:
    used = await authorise_service(FakeSwarmHosts("swarm", (SERVICE_ID, "other")))

    assert used.calls == []


async def test_each_named_service_is_looked_up_and_checked_on_its_own() -> None:
    other = "t" + "0" * 24
    hosts = FakeSwarmHosts("swarm", (SERVICE_ID, "api"), (other, "web"))

    used = await authorise_service(hosts, "api", "gone", "web")

    assert [(call[1].resource_id, call[2]) for call in used.calls] == [
        (SERVICE_ID, "read"),
        (SERVICE_ID, "write"),
        (other, "read"),
        (other, "write"),
    ]
    assert hosts.filters == [{"name": ["api"]}, {"name": ["gone"]}, {"name": ["web"]}]


async def test_a_service_the_user_may_not_write_to_is_refused() -> None:
    with pytest.raises(PermissionDenied):
        await authorise_service(
            FakeSwarmHosts("swarm", (SERVICE_ID, "web")),
            authoriser=RecordingAuthoriser(WRITE_ONLY),
        )


async def test_a_service_the_user_cannot_see_is_saved_as_if_it_did_not_exist() -> None:
    used = await authorise_service(
        FakeSwarmHosts("swarm", (SERVICE_ID, "web")),
        authoriser=RecordingAuthoriser(EVERY_LEVEL),
    )

    assert [call[2] for call in used.calls] == ["read"]


async def test_an_unreachable_manager_leaves_a_service_target_unverified() -> None:
    with pytest.raises(TargetUnverified):
        await authorise_service(FakeSwarmHosts("swarm", unreachable=True))


async def test_a_service_lookup_matches_the_exact_name_with_a_canonical_id_only() -> None:
    hosts = FakeSwarmHosts(
        "swarm", (SERVICE_ID, "web-2"), (SHORT_ID, "web"), ("t" + "0" * 24, "web")
    )

    found = await find_service(hosts, "web", ENVIRONMENT_ID)  # type: ignore[arg-type]

    assert found is not None and found.id == "t" + "0" * 24
    assert hosts.filters == [{"name": ["web"]}]


async def test_a_service_lookup_finds_nothing_when_only_a_prefix_matches() -> None:
    hosts = FakeSwarmHosts("swarm", (SERVICE_ID, "web-2"))

    assert await find_service(hosts, "web", ENVIRONMENT_ID) is None  # type: ignore[arg-type]


async def test_a_swarm_stack_is_accepted_when_the_subsystem_works_on_both_kinds() -> None:
    used = RecordingAuthoriser()

    await authorise_selector(
        OWNER,
        ENVIRONMENT_ID,
        StackTarget(names=("Shop",)),
        used,  # type: ignore[arg-type]
        FakeHosts(),  # type: ignore[arg-type]
        FakeStacks("swarm", "Shop"),
        stack_kinds=BOTH_KINDS,
    )

    key = ResourceKey(ENVIRONMENT_ID, "stack", "Shop")
    assert used.calls == [(OWNER, key, "read"), (OWNER, key, "write")]
