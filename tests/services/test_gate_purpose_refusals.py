"""What restart, update and backup say while a re-encryption holds the gate instead of a restore."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.backups import BUSY_MESSAGE, REKEY_BUSY_MESSAGE, BackupProduction
from app.services.instance import RestartRefused, restart_refusal, update_refusal
from app.services.restore_gate import RestoreGate
from app.services.self_update import UPDATE_REFUSALS
from tests.access_support import ENV_B
from tests.self_support import home_identity

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OWNER_ID = "r" * 32
RE_ENCRYPTING = "re-encrypted"


def engaged(purpose: str) -> RestoreGate:
    gate = RestoreGate()
    gate.engage(OWNER_ID, NOW, purpose=purpose)  # type: ignore[arg-type]
    return gate


@pytest.mark.parametrize(
    ("purpose", "reason"),
    [("rekey", "rekey_in_progress"), ("restore", "restore_in_progress")],
)
def test_a_restart_and_an_update_are_refused_for_the_reason_that_engaged_the_gate(
    purpose: str, reason: str
) -> None:
    identity = home_identity(ENV_B, kind="unix_socket")
    gate = engaged(purpose)

    assert restart_refusal(identity, gate) == reason
    assert update_refusal(identity, gate) == reason


def test_a_restart_and_an_update_are_not_refused_by_a_gate_that_reopened_after_a_rekey() -> None:
    identity = home_identity(ENV_B, kind="unix_socket")
    gate = engaged("rekey")
    gate.reopen()

    assert restart_refusal(identity, gate) is None
    assert update_refusal(identity, gate) is None


def test_the_restart_and_update_texts_for_a_rekey_name_the_re_encryption_and_not_a_restore() -> (
    None
):
    restart = str(RestartRefused("rekey_in_progress"))
    update = UPDATE_REFUSALS["rekey_in_progress"]

    assert RE_ENCRYPTING in restart and RE_ENCRYPTING in update
    assert "restore" not in restart.lower() and "restore" not in update.lower()


@pytest.mark.parametrize(
    ("for_rekey", "message"), [(True, REKEY_BUSY_MESSAGE), (False, BUSY_MESSAGE)]
)
def test_the_backup_lock_names_a_rekey_only_while_a_rekey_holds_it(
    tmp_path: Path, for_rekey: bool, message: str
) -> None:
    production = BackupProduction(tmp_path)

    assert production.try_hold(for_rekey=for_rekey)

    assert production.busy_message == message


def test_the_backup_lock_forgets_the_rekey_once_it_is_freed(tmp_path: Path) -> None:
    production = BackupProduction(tmp_path)
    production.try_hold(for_rekey=True)

    production.free()

    assert production.busy_message == BUSY_MESSAGE
