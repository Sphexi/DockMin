"""Tests for app/compose_cli/target.py."""

import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.compose_cli.target import (
    ComposeTarget,
    InvalidTarget,
    TargetMaterial,
    materialise,
    target_for,
)

PRIVATE_DIRECTORY = 0o700
PRIVATE_FILE = 0o600
NARROWING_UMASK = 0o277
KEY_PEM = "-----BEGIN PRIVATE KEY-----\nTOP-SECRET-KEY-BODY\n-----END PRIVATE KEY-----"
CERT_PEM = "-----BEGIN CERTIFICATE-----\nCLIENT-CERT-BODY\n-----END CERTIFICATE-----\n"
CA_PEM = "-----BEGIN CERTIFICATE-----\nCA-BODY\n-----END CERTIFICATE-----\n"
MATERIAL = TargetMaterial(CA_PEM, CERT_PEM, KEY_PEM)
SOCKET = "/var/run/docker.sock"
REMOTE = "tcp://docker.example:2376"


@dataclass(frozen=True)
class Row:
    connection_kind: str
    socket_path: str | None = None
    url: str | None = None


def local(path: str | None = SOCKET) -> Row:
    return Row("unix_socket", socket_path=path)


def remote(url: str | None = REMOTE) -> Row:
    return Row("tcp_tls", url=url)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "run"
    directory.mkdir()
    directory.chmod(PRIVATE_DIRECTORY)
    return directory


@pytest.fixture
def restrictive_umask() -> Iterator[None]:
    previous = os.umask(NARROWING_UMASK)
    yield
    os.umask(previous)


def test_a_local_environment_targets_its_socket_without_tls() -> None:
    target = target_for(local(), None)

    assert target == ComposeTarget(f"unix://{SOCKET}", None)


@pytest.mark.parametrize(
    ("url", "docker_host"),
    [
        (REMOTE, REMOTE),
        ("tcp://Docker.Example", "tcp://docker.example:2376"),
        ("tcp://10.0.0.5:2375", "tcp://10.0.0.5:2375"),
        ("tcp://[2001:DB8::1]", "tcp://[2001:db8::1]:2376"),
        ("tcp://host_1:2376", "tcp://host_1:2376"),
    ],
)
def test_a_remote_environment_targets_a_canonical_tcp_endpoint_with_its_material(
    url: str, docker_host: str
) -> None:
    target = target_for(remote(url), MATERIAL)

    assert target.docker_host == docker_host
    assert target.tls == MATERIAL


@pytest.mark.parametrize(
    "path",
    [
        None,
        "",
        "relative.sock",
        "//run/docker.sock",
        "/a/../b",
        "/a?b",
        "/a#b",
        "/a\x00b",
        "/a\u202eb",
        "/" + "a" * 257,
    ],
)
def test_a_socket_path_that_is_not_a_plain_absolute_path_is_refused(path: str | None) -> None:
    with pytest.raises(InvalidTarget):
        target_for(local(path), None)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "tcp://",
        "http://docker.example:2376",
        "tcp://user:pass@docker.example:2376",
        "tcp://docker.example:2376/path",
        "tcp://docker.example:2376/",
        "tcp://docker.example:2376?x=1",
        "tcp://docker.example:2376#frag",
        "tcp://docker.example:0",
        "tcp://docker.example:70000",
        "tcp://docker.example:port",
        "tcp://-bad.example:2376",
        "tcp://a b:2376",
        "tcp://example.123:2376",
        "tcp://0x7f.1:2376",
        "tcp://127.1:2376",
        "tcp://[fe80::1%25eth0]:2376",
    ],
)
def test_a_remote_endpoint_that_is_not_a_plain_tcp_address_is_refused(url: str | None) -> None:
    with pytest.raises(InvalidTarget):
        target_for(remote(url), MATERIAL)


def test_a_local_environment_with_tls_material_is_refused() -> None:
    with pytest.raises(InvalidTarget):
        target_for(local(), MATERIAL)


def test_a_remote_environment_without_material_is_refused() -> None:
    with pytest.raises(InvalidTarget):
        target_for(remote(), None)


@pytest.mark.parametrize(
    "material",
    [
        TargetMaterial("", CERT_PEM, KEY_PEM),
        TargetMaterial(CA_PEM, " \n", KEY_PEM),
        TargetMaterial(CA_PEM, CERT_PEM, ""),
    ],
)
def test_incomplete_tls_material_is_refused(material: TargetMaterial) -> None:
    with pytest.raises(InvalidTarget):
        target_for(remote(), material)


def test_neither_the_material_nor_the_target_repr_discloses_key_material() -> None:
    target = target_for(remote(), MATERIAL)

    rendered = [repr(MATERIAL), str(MATERIAL), f"{MATERIAL}", repr(target), str(target)]

    assert repr(MATERIAL) == "TargetMaterial(<redacted>)"
    assert not any("TOP-SECRET-KEY-BODY" in text or "CLIENT-CERT-BODY" in text for text in rendered)


def test_a_local_target_writes_nothing_and_returns_only_the_host(run_dir: Path) -> None:
    variables = materialise(target_for(local(), None), run_dir)

    assert variables == {"DOCKER_HOST": f"unix://{SOCKET}"}
    assert list(run_dir.iterdir()) == []


def test_a_tls_target_writes_private_files_and_returns_the_connection_variables(
    run_dir: Path, restrictive_umask: None
) -> None:
    variables = materialise(target_for(remote(), MATERIAL), run_dir)

    tls_dir = run_dir / "tls"
    assert variables == {
        "DOCKER_HOST": REMOTE,
        "DOCKER_TLS_VERIFY": "1",
        "DOCKER_CERT_PATH": str(tls_dir),
    }
    assert mode_of(tls_dir) == PRIVATE_DIRECTORY
    assert sorted(p.name for p in tls_dir.iterdir()) == ["ca.pem", "cert.pem", "key.pem"]
    assert [mode_of(tls_dir / name) for name in ("ca.pem", "cert.pem", "key.pem")] == [
        PRIVATE_FILE
    ] * 3
    assert (tls_dir / "ca.pem").read_text(encoding="utf-8") == CA_PEM
    assert (tls_dir / "key.pem").read_text(encoding="utf-8") == KEY_PEM + "\n"


def test_a_tls_target_refuses_a_work_directory_that_others_can_enter(run_dir: Path) -> None:
    run_dir.chmod(0o755)

    with pytest.raises(InvalidTarget):
        materialise(target_for(remote(), MATERIAL), run_dir)

    assert list(run_dir.iterdir()) == []


def test_a_tls_target_refuses_a_work_directory_that_is_a_symlink_or_a_file(
    tmp_path: Path, run_dir: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(run_dir)
    plain = tmp_path / "plain"
    plain.write_text("", encoding="utf-8")
    target = target_for(remote(), MATERIAL)

    with pytest.raises(InvalidTarget):
        materialise(target, link)
    with pytest.raises(InvalidTarget):
        materialise(target, plain)
    assert list(run_dir.iterdir()) == []


def test_a_planted_file_at_the_tls_entry_is_never_written_through(run_dir: Path) -> None:
    planted = run_dir / "tls"
    planted.write_text("planted", encoding="utf-8")

    with pytest.raises(FileExistsError):
        materialise(target_for(remote(), MATERIAL), run_dir)

    assert planted.read_text(encoding="utf-8") == "planted"


def test_a_symlink_at_the_tls_entry_is_not_followed(tmp_path: Path, run_dir: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (run_dir / "tls").symlink_to(victim)

    with pytest.raises(FileExistsError):
        materialise(target_for(remote(), MATERIAL), run_dir)

    assert list(victim.iterdir()) == []


def test_a_failure_part_way_through_leaves_no_tls_files_behind(run_dir: Path) -> None:
    unwritable_key = TargetMaterial(CA_PEM, CERT_PEM, "\ud800")

    with pytest.raises(UnicodeEncodeError):
        materialise(target_for(remote(), unwritable_key), run_dir)

    assert list(run_dir.iterdir()) == []
