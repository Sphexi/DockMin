"""The helper's Engine client over a Unix socket."""

from pathlib import Path

import pytest

from tests.updater.conftest import Rig
from tests.updater.fake_engine import API_VERSION, OLD_ID, Failure
from updater.engine import MAX_ERROR_CHARS, Engine, EngineError

CONFLICT = 409
NOT_FOUND = 404
NEW_NAME = "dockmin-next"
NO_SUCH_SOCKET = "no-engine.sock"


def test_an_api_version_that_is_not_a_version_number_is_refused() -> None:
    with pytest.raises(ValueError, match="API version"):
        Engine("/run/docker.sock", "1.47/../../x")


def test_an_inspect_that_answers_with_another_container_is_refused(rig: Rig) -> None:
    rig.daemon.wrong_id_for = OLD_ID

    with pytest.raises(EngineError, match="different container"):
        rig.engine().inspect_container(OLD_ID)


def test_a_missing_container_raises_with_the_engines_status(rig: Rig) -> None:
    with pytest.raises(EngineError) as raised:
        rig.engine().inspect_container("no-such-container")

    assert raised.value.status == NOT_FOUND


def test_stopping_a_stopped_container_and_starting_a_running_one_are_not_errors(rig: Rig) -> None:
    engine = rig.engine()
    engine.stop_container(OLD_ID, 1)

    engine.stop_container(OLD_ID, 1)
    engine.start_container(OLD_ID)
    engine.start_container(OLD_ID)

    assert rig.daemon.containers[OLD_ID].running


def test_an_error_carries_the_engines_message_cut_to_its_limit(rig: Rig) -> None:
    rig.daemon.failures["rename"] = Failure(CONFLICT, "name in use " + "x" * 1000)

    with pytest.raises(EngineError) as raised:
        rig.engine().rename_container(OLD_ID, NEW_NAME)

    assert raised.value.status == CONFLICT
    assert "name in use" in str(raised.value)
    assert len(str(raised.value)) < MAX_ERROR_CHARS + 100


def test_removing_a_running_container_without_force_is_refused_and_never_takes_volumes(
    rig: Rig,
) -> None:
    with pytest.raises(EngineError) as raised:
        rig.engine().remove_container(OLD_ID, force=False)

    [call] = [c for c in rig.daemon.calls if c.method == "DELETE"]
    assert raised.value.status == CONFLICT
    assert call.query == {"force": "0", "v": "0"}


def test_a_create_answered_with_a_malformed_id_is_refused(rig: Rig) -> None:
    rig.daemon.create_reply = b'{"Id": "not-an-id"}'

    with pytest.raises(EngineError, match="malformed id"):
        rig.engine().create_container(NEW_NAME, {"Image": "x"})


def test_an_engine_that_is_not_listening_raises_instead_of_hanging(tmp_path: Path) -> None:
    engine = Engine(str(tmp_path / NO_SUCH_SOCKET), API_VERSION)

    with pytest.raises(EngineError):
        engine.inspect_image("anything")
