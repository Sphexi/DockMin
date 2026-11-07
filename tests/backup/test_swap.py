"""Replacing the live data directory contents with staged data, and undoing it."""

import stat
from pathlib import Path

import pytest

from app.backup import paths

RESTORE_ID = "1" * 32
OTHER_RESTORE_ID = "2" * 32
FAILING_MOVE = 3
OLD, NEW = b"OLD", b"NEW"


class Layout:
    """A data directory holding live data, and a restore directory holding staged data."""

    def __init__(self, root: Path) -> None:
        self.data = root / "data"
        self.data.mkdir()
        for name, content in (
            ("dockmin.db", OLD),
            ("dockmin.db-wal", b"OLDWAL"),
            ("dockmin.db-shm", b"OLDSHM"),
            ("dockmin.lock", b""),
        ):
            (self.data / name).write_bytes(content)
        (self.data / "stacks" / "aa").mkdir(parents=True)
        (self.data / "stacks" / "aa" / "compose.yaml").write_bytes(b"old-stack")
        for name in ("tls-work", "compose-work"):
            (self.data / name).mkdir()
        self.restore = paths.new_restore_directory(self.data, RESTORE_ID)
        self.staged = self.restore / "staging"
        (self.staged / "stacks" / "bb").mkdir(parents=True)
        (self.staged / "dockmin.db").write_bytes(NEW)
        (self.staged / "stacks" / "bb" / "compose.yaml").write_bytes(b"new-stack")

    def close_database(self) -> None:
        (self.data / "dockmin.db-wal").unlink()
        (self.data / "dockmin.db-shm").unlink()

    def entries(self) -> list[str]:
        return sorted(path.name for path in self.data.iterdir())


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path)


def test_the_plan_names_every_live_entry_present_and_where_each_goes(layout: Layout) -> None:
    plan = paths.plan_swap(layout.data, layout.restore)

    assert sorted(path.name for path in plan.live) == [
        "dockmin.db",
        "dockmin.db-shm",
        "dockmin.db-wal",
        "stacks",
    ]
    assert (plan.previous, plan.staged) == (layout.restore / "previous", layout.staged)


def test_a_swap_puts_the_staged_data_in_place_keeps_the_previous_and_leaves_the_rest(
    layout: Layout,
) -> None:
    plan = paths.plan_swap(layout.data, layout.restore)
    layout.close_database()

    paths.apply_swap(plan)

    assert (layout.data / "dockmin.db").read_bytes() == NEW
    assert (layout.data / "stacks" / "bb" / "compose.yaml").exists()
    assert (plan.previous / "dockmin.db").read_bytes() == OLD
    assert (plan.previous / "stacks" / "aa" / "compose.yaml").exists()
    assert {"dockmin.lock", "tls-work", "compose-work"} <= set(layout.entries())


def test_a_journal_file_that_appears_after_the_plan_is_moved_aside_with_the_rest(
    layout: Layout,
) -> None:
    plan = paths.plan_swap(layout.data, layout.restore)
    layout.close_database()
    (layout.data / "dockmin.db-wal").write_bytes(b"LATE")

    paths.apply_swap(plan)

    assert (plan.previous / "dockmin.db-wal").read_bytes() == b"LATE"
    assert not (layout.data / "dockmin.db-wal").exists()


def test_reverting_a_swap_restores_the_original_and_returns_the_staged_data(layout: Layout) -> None:
    plan = paths.plan_swap(layout.data, layout.restore)
    layout.close_database()
    journal = paths.apply_swap(plan)

    paths.revert_swap(journal)

    assert (layout.data / "dockmin.db").read_bytes() == OLD
    assert (layout.data / "stacks" / "aa").exists()
    assert (layout.staged / "dockmin.db").read_bytes() == NEW
    assert not (plan.previous / "dockmin.db").exists()


def test_a_move_that_fails_midway_journals_only_the_moves_made_and_reverts_cleanly(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout.close_database()
    real_move = paths._move
    calls: list[Path] = []

    def flaky(source: Path, target: Path, journal: paths.SwapJournal) -> None:
        calls.append(source)
        if len(calls) == FAILING_MOVE:
            raise OSError("injected")
        real_move(source, target, journal)

    monkeypatch.setattr(paths, "_move", flaky)

    with pytest.raises(paths.SwapFailed) as raised:
        paths.apply_swap(paths.plan_swap(layout.data, layout.restore))
    monkeypatch.setattr(paths, "_move", real_move)
    moves_made = len(raised.value.journal.moves)
    paths.revert_swap(raised.value.journal)

    assert moves_made == FAILING_MOVE - 1
    assert (layout.data / "dockmin.db").read_bytes() == OLD
    assert (layout.data / "stacks" / "aa").exists()
    assert (layout.staged / "dockmin.db").exists() and (layout.staged / "stacks" / "bb").exists()


def test_rejecting_restored_data_puts_the_previous_data_back_and_keeps_the_rejected_files(
    layout: Layout,
) -> None:
    plan = paths.plan_swap(layout.data, layout.restore)
    layout.close_database()
    journal = paths.apply_swap(plan)
    (layout.data / "dockmin.db-wal").write_bytes(b"NEWWAL")
    (layout.data / "dockmin.db-shm").write_bytes(b"NEWSHM")
    rejected = layout.restore / "rejected"

    paths.reject_restored(plan, journal, rejected)

    assert (layout.data / "dockmin.db").read_bytes() == OLD
    assert (layout.data / "stacks" / "aa").exists()
    assert not (layout.data / "dockmin.db-wal").exists()
    assert (rejected / "dockmin.db").read_bytes() == NEW
    assert (rejected / "dockmin.db-wal").read_bytes() == b"NEWWAL"
    assert (rejected / "stacks" / "bb").exists()


def test_a_swap_with_no_staged_database_is_refused_before_any_move(layout: Layout) -> None:
    empty = paths.new_restore_directory(layout.data, OTHER_RESTORE_ID)

    with pytest.raises(paths.SwapFailed) as raised:
        paths.apply_swap(paths.plan_swap(layout.data, empty))

    assert raised.value.journal.moves == []
    assert (layout.data / "dockmin.db").read_bytes() == OLD


def test_a_staged_database_with_unmerged_journal_files_is_refused_before_any_move(
    layout: Layout,
) -> None:
    (layout.staged / "dockmin.db-wal").write_bytes(b"x")

    with pytest.raises(paths.SwapFailed) as raised:
        paths.apply_swap(paths.plan_swap(layout.data, layout.restore))

    assert raised.value.journal.moves == []
    assert (layout.data / "dockmin.db").read_bytes() == OLD


def test_a_restore_id_must_be_32_lowercase_hexadecimal_characters(layout: Layout) -> None:
    with pytest.raises(ValueError, match="restore_id"):
        paths.new_restore_directory(layout.data, "../x")


def test_a_work_directory_that_is_a_symbolic_link_is_refused(tmp_path: Path) -> None:
    link = tmp_path / "work"
    link.symlink_to(tmp_path)

    with pytest.raises(paths.UnsafePath):
        paths.ensure_private_directory(link)


def test_a_new_work_directory_is_private_and_each_one_is_distinct(tmp_path: Path) -> None:
    first = paths.new_work_directory(tmp_path)
    second = paths.new_work_directory(tmp_path)

    assert first != second
    assert stat.S_IMODE(first.stat().st_mode) == paths.DIRECTORY_MODE
    assert first.parent == tmp_path / paths.WORK_DIRECTORY


def test_removing_a_directory_follows_no_link_and_tolerates_a_missing_path(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "kept.txt").write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(target)

    paths.remove_directory(link)
    paths.remove_directory(tmp_path / "absent")

    assert not link.exists() and (target / "kept.txt").exists()
