"""Which containers share another's network, and when that share is stale."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.docker_engine.models import ContainerSummary
from app.services.network_joiners import (
    MAX_JOINER_CHAIN,
    MAX_JOINER_DEPTH,
    JoinerLink,
    joined_reference,
    joiner_owners,
    joiners_of,
    joins_by_id,
    stale_reason,
    walk_joiners,
)

OWNER_ID = "a" * 64
NOW = datetime(2030, 1, 1, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)


def row(container_id: str, name: str, mode: str = "bridge", state: str = "running") -> Any:
    return ContainerSummary.model_validate(
        {
            "Id": container_id,
            "Names": [f"/{name}"],
            "Image": "nginx",
            "Created": 1,
            "State": state,
            "HostConfig": {"NetworkMode": mode},
        }
    )


@pytest.mark.parametrize(
    ("mode", "reference"),
    [
        ("container:abc", "abc"),
        ("container:vpn", "vpn"),
        ("container:", None),
        ("bridge", None),
        ("host", None),
        ("", None),
    ],
)
def test_a_network_mode_names_the_container_it_joins_or_none(
    mode: str, reference: str | None
) -> None:
    assert joined_reference(mode) == reference


@pytest.mark.parametrize(
    ("status", "owner_started", "joiner_started", "reason"),
    [
        (None, None, NOW, "owner_missing"),
        ("exited", NOW, LATER, "owner_not_running"),
        ("restarting", NOW, LATER, "owner_not_running"),
        ("paused", NOW, LATER, None),
        ("paused", LATER, NOW, "owner_restarted"),
        ("running", LATER, NOW, "owner_restarted"),
        ("running", NOW, LATER, None),
        ("running", NOW, NOW, None),
        ("running", None, NOW, None),
        ("running", LATER, None, None),
    ],
)
def test_a_network_is_stale_when_its_owner_is_gone_down_or_started_after_the_joiner(
    status: str | None,
    owner_started: datetime | None,
    joiner_started: datetime | None,
    reason: str | None,
) -> None:
    assert stale_reason(status, owner_started, joiner_started) == reason


def test_joiners_are_found_by_the_owners_id_prefix_or_name_and_never_include_the_owner() -> None:
    rows = [
        row(OWNER_ID, "vpn"),
        row("b" * 64, "by-id", f"container:{OWNER_ID}"),
        row("c" * 64, "by-name", "container:vpn"),
        row("d" * 64, "other", "container:other"),
        row("e" * 64, "short", f"container:{OWNER_ID[:12]}"),
        row("f" * 64, "too-short", f"container:{OWNER_ID[:11]}"),
        row("9" * 64, "plain"),
    ]

    found = joiners_of(rows, OWNER_ID, "vpn")

    assert [each.id for each in found] == ["b" * 64, "c" * 64, "e" * 64]


def test_an_id_prefix_that_another_container_shares_names_no_owner() -> None:
    twin = "a" * 12 + "b" * 52
    rows = [
        row(OWNER_ID, "vpn"),
        row(twin, "twin"),
        row("c" * 64, "ambiguous", f"container:{OWNER_ID[:12]}"),
    ]

    assert joiners_of(rows, OWNER_ID, "vpn") == []
    assert joiner_owners(rows) == {}


@pytest.mark.parametrize(
    ("mode", "joins"),
    [
        (f"container:{OWNER_ID}", True),
        (f"container:{OWNER_ID[:12]}", True),
        (f"container:{OWNER_ID[:11]}", False),
        ("container:vpn", False),
        ("bridge", False),
    ],
)
def test_a_mode_joins_by_id_for_a_full_id_or_a_long_enough_prefix(mode: str, joins: bool) -> None:
    assert joins_by_id(mode, OWNER_ID) is joins


async def _always(_: JoinerLink) -> bool:
    return True


def _chain(length: int) -> list[Any]:
    rows = [row(f"{1:064x}", "n1")]
    rows.extend(row(f"{i:064x}", f"n{i}", f"container:{i - 1:064x}") for i in range(2, length + 1))
    return rows


async def test_the_walk_lists_each_owner_before_the_containers_that_join_it() -> None:
    rows = [
        row(OWNER_ID, "vpn"),
        row("c" * 64, "leaf", f"container:{'b' * 64}"),
        row("b" * 64, "mid", "container:vpn"),
    ]

    walk = await walk_joiners(rows, [(OWNER_ID, "vpn")], {OWNER_ID}, _always)

    assert [(link.row.id, link.owner_id) for link in walk.links] == [
        ("b" * 64, OWNER_ID),
        ("c" * 64, "b" * 64),
    ]
    assert walk.omitted == 0


async def test_the_walk_stops_below_a_joiner_it_does_not_descend_into() -> None:
    rows = [
        row(OWNER_ID, "vpn"),
        row("b" * 64, "mid", "container:vpn"),
        row("c" * 64, "leaf", f"container:{'b' * 64}"),
    ]

    async def never(_: JoinerLink) -> bool:
        return False

    walk = await walk_joiners(rows, [(OWNER_ID, "vpn")], {OWNER_ID}, never)

    assert [link.row.id for link in walk.links] == ["b" * 64]


async def test_the_walk_counts_the_joiners_a_full_chain_or_a_deep_one_leaves_out() -> None:
    wide = [row(OWNER_ID, "vpn")] + [
        row(f"{i:064x}", f"w{i}", "container:vpn") for i in range(1, MAX_JOINER_CHAIN + 3)
    ]
    deep = _chain(MAX_JOINER_DEPTH + 3)

    full = await walk_joiners(wide, [(OWNER_ID, "vpn")], {OWNER_ID}, _always)
    shallow = await walk_joiners(deep, [(f"{1:064x}", "n1")], set(), _always)

    assert len(full.links) == MAX_JOINER_CHAIN
    assert full.omitted == 2
    assert len(shallow.links) == MAX_JOINER_DEPTH
    assert shallow.omitted == 1


async def test_the_walk_survives_a_cycle() -> None:
    rows = [row("a" * 64, "a", f"container:{'b' * 64}"), row("b" * 64, "b", "container:a")]

    walk = await walk_joiners(rows, [("a" * 64, "a")], set(), _always)

    assert [link.row.id for link in walk.links] == ["b" * 64]


def test_each_listed_joiner_maps_to_the_listed_owner_it_names() -> None:
    rows = [
        row(OWNER_ID, "vpn"),
        row("b" * 64, "mid", "container:vpn"),
        row("c" * 64, "leaf", f"container:{'b' * 64}"),
        row("e" * 64, "short", f"container:{OWNER_ID[:12]}"),
        row("d" * 64, "orphan", "container:gone"),
    ]

    assert joiner_owners(rows) == {"b" * 64: OWNER_ID, "c" * 64: "b" * 64, "e" * 64: OWNER_ID}
