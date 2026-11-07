"""A Compose process is never aimed at a host the address policy refuses."""

import socket
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.compose_cli.cli import ComposeCli, ComposeProject
from app.compose_cli.errors import ComposeHostRefused
from app.compose_cli.host_check import UNRESOLVED, ComposeHostUnresolved, check_target_host
from app.compose_cli.target import ComposeTarget, TargetMaterial
from app.logging import Scrubber
from app.netcheck import AddrInfo
from app.netpolicy import REFUSAL_TEXT

MATERIAL = TargetMaterial("CA\n", "CERT\n", "KEY\n")
LOCAL = ComposeTarget("unix:///var/run/docker.sock", None)

type Resolver = Callable[[str, int], Awaitable[Sequence[AddrInfo]]]


def tcp(host: str, port: int = 2376) -> ComposeTarget:
    return ComposeTarget(f"tcp://{host}:{port}", MATERIAL)


def resolving_to(*addresses: str) -> Resolver:
    async def resolve(host: str, port: int) -> Sequence[AddrInfo]:
        return [
            (
                socket.AF_INET6 if ":" in text else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (text, port),
            )
            for text in addresses
        ]

    return resolve


async def never_resolves(host: str, port: int) -> Sequence[AddrInfo]:
    raise AssertionError("a literal address must not be resolved")


async def failing(host: str, port: int) -> Sequence[AddrInfo]:
    raise socket.gaierror(socket.EAI_NONAME, "unknown")


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "[::1]", "169.254.169.254", "0.0.0.0", "[::ffff:127.0.0.1]", "[fe80::1]"],
)
async def test_an_internal_literal_host_is_refused_without_a_lookup(host: str) -> None:
    with pytest.raises(ComposeHostRefused) as refused:
        await check_target_host(tcp(host), never_resolves)

    assert str(refused.value) == REFUSAL_TEXT
    assert not isinstance(refused.value, ComposeHostUnresolved)


async def test_a_name_that_resolves_to_an_internal_address_is_refused() -> None:
    with pytest.raises(ComposeHostRefused) as refused:
        await check_target_host(tcp("docker.example.test"), resolving_to("127.0.0.1"))

    assert not isinstance(refused.value, ComposeHostUnresolved)


async def test_every_address_of_a_name_must_pass_so_one_internal_answer_refuses_it() -> None:
    with pytest.raises(ComposeHostRefused):
        await check_target_host(tcp("docker.example.test"), resolving_to("192.0.2.5", "127.0.0.1"))


async def test_a_name_that_resolves_only_to_ordinary_addresses_passes() -> None:
    await check_target_host(tcp("docker.example.test"), resolving_to("192.0.2.5", "2001:db8::5"))


async def test_a_private_literal_address_passes() -> None:
    await check_target_host(tcp("10.1.2.3"), never_resolves)


@pytest.mark.parametrize("resolver", [failing, resolving_to()], ids=["lookup-error", "no-address"])
async def test_a_name_that_does_not_resolve_is_refused_as_unresolved_and_not_as_internal(
    resolver: Resolver,
) -> None:
    with pytest.raises(ComposeHostUnresolved) as refused:
        await check_target_host(tcp("docker.example.test"), resolver)

    assert str(refused.value) == UNRESOLVED
    assert REFUSAL_TEXT not in str(refused.value)


async def test_a_local_socket_is_not_checked() -> None:
    await check_target_host(LOCAL, never_resolves)


async def test_a_permitted_internal_host_passes(address_policy: Callable[..., None]) -> None:
    address_policy("127.0.0.0/8")

    await check_target_host(tcp("127.0.0.1"), never_resolves)
    await check_target_host(tcp("docker.example.test"), resolving_to("127.0.0.2"))


async def test_a_permit_for_one_address_of_a_name_does_not_cover_its_others(
    address_policy: Callable[..., None],
) -> None:
    address_policy("127.0.0.1/32")

    with pytest.raises(ComposeHostRefused):
        await check_target_host(
            tcp("docker.example.test"), resolving_to("127.0.0.1", "169.254.1.1")
        )


async def test_the_refusal_is_logged_with_the_address(
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    with pytest.raises(ComposeHostRefused):
        await check_target_host(tcp("169.254.169.254"), never_resolves)

    assert [line["address"] for line in logged("outbound_address_refused")] == ["169.254.169.254"]


@pytest.fixture
def project(tmp_path: Path) -> ComposeProject:
    directory = tmp_path / "project"
    directory.mkdir()
    file = directory / "compose.yaml"
    file.write_text("services: {}\n", encoding="utf-8")
    return ComposeProject("stack-a", directory, file)


async def test_a_run_aimed_at_a_refused_host_creates_nothing_and_starts_no_process(
    fake_binary: Any, work_base: Path, project: ComposeProject, record_path: Path
) -> None:
    cli = ComposeCli(fake_binary, work_base=work_base, resolver=resolving_to("169.254.169.254"))
    before = sorted(work_base.iterdir())  # noqa: ASYNC240

    with pytest.raises(ComposeHostRefused):
        async with cli.invocation(project, tcp("docker.example.test"), {}, Scrubber()):
            raise AssertionError("the invocation must not be entered")

    assert sorted(work_base.iterdir()) == before  # noqa: ASYNC240
    assert not record_path.exists()  # noqa: ASYNC240
