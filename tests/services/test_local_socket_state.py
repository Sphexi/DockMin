"""Reading the state of the Docker socket the first-run panel offers."""

from pathlib import Path

import pytest

from app.services.environments import local_socket_state
from tests.socket_support import SOCKET_NAME, STATES, socket_in_state


@pytest.mark.parametrize("state", STATES)
def test_a_socket_path_is_read_as_the_state_it_is_in(tmp_path: Path, state: str) -> None:
    with socket_in_state(tmp_path, state) as path:
        found = local_socket_state(path)

    assert found == state


def test_a_directory_in_the_place_of_the_socket_is_not_a_socket(tmp_path: Path) -> None:
    (tmp_path / SOCKET_NAME).mkdir()

    assert local_socket_state(str(tmp_path / SOCKET_NAME)) == "not_socket"


def test_a_path_below_a_file_is_absent(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")

    assert local_socket_state(str(blocker / SOCKET_NAME)) == "absent"


def test_a_symbolic_link_to_a_socket_is_read_through_to_the_socket(tmp_path: Path) -> None:
    link = tmp_path / "link.sock"
    with socket_in_state(tmp_path, "present") as target:
        link.symlink_to(target)

        found = local_socket_state(str(link))

    assert found == "present"
