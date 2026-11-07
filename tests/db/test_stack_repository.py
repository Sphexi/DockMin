"""Tests for app/db/repositories/stack.py: stacks, encrypted variables and the paged list."""

import hashlib
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.crypto.envelope import SecretLocation
from app.crypto.keys import Keyring
from app.crypto.vault import Vault
from app.db.engine import Database, Session, connect
from app.db.migrations import migrate
from app.db.repositories.environment import EnvironmentRepository, StatusRecord
from app.db.repositories.errors import DuplicateError, MissingReferenceError
from app.db.repositories.registry_credential import SecretUnreadableError
from app.db.repositories.stack import (
    MAX_VARIABLES,
    TOKEN_LENGTH,
    DeployedState,
    StackKind,
    StackListRepository,
    StackNameError,
    StackRepository,
    StackVariableError,
    StoredVariable,
    VariableChanges,
    VariableWrite,
    inputs_digest_of,
)
from app.db.repositories.stack_file import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_FILES_TOTAL_BYTES,
    StackFileError,
    StackFileLimitError,
)
from app.db.repositories.visibility_filter import SqlFragment
from tests.crypto.helpers import FIRST_KEY, FIRST_VERSION, INSTANCE_ONE, SECOND_KEY, vault_with
from tests.db.backup_support import migrations_up_to
from tests.db.helpers import CREATED_AT, CREATED_STAMP, LATER_AT, LATER_STAMP, UNKNOWN_ID, at

SECRET_VALUE = "hunter2-secret-sentinel"
NEW_SECRET_VALUE = "rotated-secret-sentinel"
PLAIN_VALUE = "visible-plain-value"
DIGEST = "d" * 64
OTHER_DIGEST = "e" * 64
FILE = "services:\n  web:\n    image: x\n"
OTHER_FILE = "services: {}\n"
STACK_NAME = "shop"
SWARM_NAME = "Shop"
EMPTY_INPUTS_DIGEST = inputs_digest_of((), ())
TOKEN_PATTERN = re.compile(rf"[0-9a-z]{{{TOKEN_LENGTH}}}")


class SealFailure(Exception):
    """Raised by a vault that cannot seal, to fail a write between its two statements."""


class FailingVault(Vault):
    def seal(self, plaintext: str, location: SecretLocation) -> str:
        raise SealFailure


@pytest.fixture
def vault() -> Vault:
    return vault_with(FIRST_KEY)


@pytest.fixture
def stacks(session: Session, vault: Vault) -> StackRepository:
    return StackRepository(session, vault, at(CREATED_AT))


@pytest.fixture
async def environment_id(session: Session) -> int:
    return await add_environment(session, "one")


async def add_environment(session: Session, name: str) -> int:
    result = await session.execute(
        "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
        "VALUES (?, 'unix_socket', ?, ?, ?)",
        (name, f"/run/{name}.sock", CREATED_STAMP, CREATED_STAMP),
    )
    assert result.last_row_id is not None
    return result.last_row_id


async def add_stack(
    repository: StackRepository,
    environment_id: int,
    name: str = STACK_NAME,
    number: int = 1,
    kind: StackKind = "compose",
) -> int:
    return await repository.insert(
        environment_id=environment_id,
        name=name,
        kind=kind,
        origin="created",
        project_directory=f"{number:032x}",
        compose_file=FILE,
        compose_file_digest=DIGEST,
        created_by_user_id=None,
    )


@pytest.fixture
async def stack_id(stacks: StackRepository, environment_id: int) -> int:
    return await add_stack(stacks, environment_id)


async def raw_variables(session: Session, stack_id: int) -> list[dict[str, Any]]:
    rows = await session.fetch_limited(
        "SELECT name, is_secret, value, value_ciphertext FROM stack_variable "
        "WHERE stack_id = ? ORDER BY name",
        (stack_id,),
        limit=100,
    )
    return [dict(row) for row in rows]


async def stored_inputs_digest(session: Session, stack_id: int) -> str | None:
    row = await session.fetch_one("SELECT inputs_digest FROM stack WHERE id = ?", (stack_id,))
    assert row is not None
    digest: str | None = row["inputs_digest"]
    return digest


def secret(name: str, value: str | None = SECRET_VALUE) -> VariableWrite:
    return VariableWrite(name, True, value)


def plain(name: str, value: str | None = PLAIN_VALUE) -> VariableWrite:
    return VariableWrite(name, False, value)


class TestStackRows:
    async def test_an_inserted_stack_is_read_back_by_name_and_by_id(
        self, stacks: StackRepository, environment_id: int
    ) -> None:
        stack_id = await add_stack(stacks, environment_id)

        by_name = await stacks.get(environment_id, STACK_NAME)
        by_id = await stacks.get_by_id(stack_id)

        assert by_name == by_id
        assert by_name is not None
        assert (by_name.id, by_name.environment_id, by_name.name) == (
            stack_id,
            environment_id,
            STACK_NAME,
        )
        assert (by_name.kind, by_name.origin, by_name.status) == ("compose", "created", "unknown")
        assert by_name.compose_file == FILE
        assert by_name.compose_file_digest == DIGEST
        assert by_name.project_directory == f"{1:032x}"
        assert by_name.created_at == by_name.updated_at == CREATED_STAMP
        assert by_name.last_operation is None
        assert by_name.last_outcome is None
        assert by_name.deployed_file_digest is None
        assert by_name.inputs_digest == EMPTY_INPUTS_DIGEST
        assert by_name.deployed_inputs_digest is None

    async def test_the_file_text_is_left_out_of_the_stack_repr(
        self, stacks: StackRepository, stack_id: int
    ) -> None:
        stored = await stacks.get_by_id(stack_id)

        assert FILE not in repr(stored)

    async def test_lookups_miss_on_another_name_environment_or_id(
        self, stacks: StackRepository, environment_id: int, stack_id: int, session: Session
    ) -> None:
        other = await add_environment(session, "two")

        assert await stacks.get(environment_id, "Shop") is None
        assert await stacks.get(other, STACK_NAME) is None
        assert await stacks.get_by_id(UNKNOWN_ID) is None

    async def test_a_taken_name_or_directory_is_a_duplicate_and_a_missing_environment_is_refused(
        self, stacks: StackRepository, environment_id: int, stack_id: int
    ) -> None:
        with pytest.raises(DuplicateError):
            await add_stack(stacks, environment_id, STACK_NAME, number=2)
        with pytest.raises(DuplicateError):
            await add_stack(stacks, environment_id, "other", number=1)
        with pytest.raises(MissingReferenceError):
            await add_stack(stacks, UNKNOWN_ID, "other", number=3)

    async def test_names_lists_one_environment_in_order(
        self, stacks: StackRepository, environment_id: int, session: Session
    ) -> None:
        other = await add_environment(session, "two")
        await add_stack(stacks, environment_id, "zeta", 1)
        await add_stack(stacks, environment_id, "alpha", 2)
        await add_stack(stacks, other, "elsewhere", 3)

        assert await stacks.names(environment_id) == ["alpha", "zeta"]
        assert await stacks.names(UNKNOWN_ID) == []

    async def test_update_file_replaces_the_text_and_digest_and_touches_the_time(
        self, session: Session, vault: Vault, stacks: StackRepository, stack_id: int
    ) -> None:
        later = StackRepository(session, vault, at(LATER_AT))

        updated = await later.update_file(
            stack_id, compose_file=OTHER_FILE, compose_file_digest=OTHER_DIGEST
        )
        missing = await later.update_file(
            UNKNOWN_ID, compose_file=OTHER_FILE, compose_file_digest=OTHER_DIGEST
        )

        stored = await stacks.get_by_id(stack_id)
        assert (updated, missing) == (True, False)
        assert stored is not None
        assert (stored.compose_file, stored.compose_file_digest) == (OTHER_FILE, OTHER_DIGEST)
        assert (stored.created_at, stored.updated_at) == (CREATED_STAMP, LATER_STAMP)

    async def test_record_operation_stores_the_outcome_and_the_deployed_state(
        self, session: Session, vault: Vault, stacks: StackRepository, stack_id: int
    ) -> None:
        later = StackRepository(session, vault, at(LATER_AT))

        recorded = await later.record_operation(
            stack_id,
            operation="deploy",
            outcome="success",
            status="active",
            deployed=DeployedState(DIGEST, OTHER_DIGEST),
        )

        stored = await stacks.get_by_id(stack_id)
        assert recorded is True
        assert stored is not None
        assert (stored.last_operation, stored.last_outcome) == ("deploy", "success")
        assert stored.status == "active"
        assert (stored.deployed_file_digest, stored.resolved_digest) == (DIGEST, OTHER_DIGEST)
        assert stored.last_operation_at == stored.last_deployed_at == LATER_STAMP

    async def test_record_operation_without_status_or_state_keeps_what_is_stored(
        self, session: Session, vault: Vault, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.record_operation(
            stack_id,
            operation="deploy",
            outcome="success",
            status="active",
            deployed=DeployedState(DIGEST, OTHER_DIGEST),
        )
        later = StackRepository(session, vault, at(LATER_AT))

        await later.record_operation(stack_id, operation="remove", outcome="failure")

        stored = await stacks.get_by_id(stack_id)
        assert stored is not None
        assert (stored.last_operation, stored.last_outcome) == ("remove", "failure")
        assert stored.status == "active"
        assert (stored.deployed_file_digest, stored.resolved_digest) == (DIGEST, OTHER_DIGEST)
        assert stored.last_deployed_at == CREATED_STAMP
        assert stored.last_operation_at == LATER_STAMP

    async def test_record_operation_on_a_missing_stack_reports_false(
        self, stacks: StackRepository
    ) -> None:
        recorded = await stacks.record_operation(UNKNOWN_ID, operation="stop", outcome="success")

        assert recorded is False

    async def test_delete_removes_the_stack_and_its_variables(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [plain("A"), secret("B")])

        deleted = await stacks.delete(stack_id)
        again = await stacks.delete(stack_id)

        assert (deleted, again) == (True, False)
        assert await stacks.get_by_id(stack_id) is None
        assert await raw_variables(session, stack_id) == []

    async def test_deleting_an_environment_removes_its_stacks_and_variables(
        self, session: Session, stacks: StackRepository, environment_id: int, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [secret("B")])

        await session.execute("DELETE FROM environment WHERE id = ?", (environment_id,))

        assert await stacks.get_by_id(stack_id) is None
        assert await raw_variables(session, stack_id) == []


class TestSecretVariables:
    async def test_a_secret_is_stored_encrypted_and_never_returned_by_the_listing(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        changes = await stacks.replace_variables(stack_id, [secret("TOKEN"), plain("MODE")])

        raw = await raw_variables(session, stack_id)
        listed = await stacks.variables(stack_id)
        revealed = await stacks.reveal_secrets(stack_id)

        assert changes == VariableChanges(changed=2, removed=0)
        mode, token = raw
        assert (mode["value"], mode["value_ciphertext"]) == (PLAIN_VALUE, None)
        assert token["value"] is None
        assert token["value_ciphertext"].startswith(f"v{FIRST_VERSION}:")
        assert SECRET_VALUE not in token["value_ciphertext"]
        assert [replace(variable, value_token=None) for variable in listed] == [
            StoredVariable(id_of(listed, "MODE"), "MODE", False, PLAIN_VALUE),
            StoredVariable(id_of(listed, "TOKEN"), "TOKEN", True, None),
        ]
        assert all(TOKEN_PATTERN.fullmatch(variable.value_token or "") for variable in listed)
        assert SECRET_VALUE not in repr(listed)
        assert revealed == {"TOKEN": SECRET_VALUE}

    async def test_the_secret_plaintext_is_not_in_the_database_file(
        self, database: Database, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [secret("TOKEN"), plain("MODE")])
        await stacks.replace_variables(stack_id, [secret("TOKEN", NEW_SECRET_VALUE), plain("MODE")])

        content = database_bytes(database.path)

        assert PLAIN_VALUE.encode() in content
        assert SECRET_VALUE.encode() not in content
        assert NEW_SECRET_VALUE.encode() not in content

    async def test_the_insert_placeholder_is_never_committed_when_sealing_fails(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        failing = StackRepository(
            session,
            FailingVault(Keyring(FIRST_VERSION, {FIRST_VERSION: FIRST_KEY}), INSTANCE_ONE),
            at(CREATED_AT),
        )

        async with session.transaction():
            with pytest.raises(SealFailure):
                await failing.replace_variables(stack_id, [plain("A"), secret("TOKEN")])

        assert await raw_variables(session, stack_id) == []
        assert await stored_inputs_digest(session, stack_id) == EMPTY_INPUTS_DIGEST

    async def test_secret_values_stay_out_of_the_reprs_and_errors(
        self, stacks: StackRepository, stack_id: int
    ) -> None:
        write = secret("TOKEN")

        with pytest.raises(StackVariableError) as raised:
            await stacks.replace_variables(stack_id, [secret("NOVALUE", None)])

        assert SECRET_VALUE not in repr(write)
        assert SECRET_VALUE not in repr(plain("MODE", SECRET_VALUE))
        assert "NOVALUE" in str(raised.value)
        assert SECRET_VALUE not in repr(raised.value)

    async def test_a_secret_sealed_under_another_key_or_tampered_is_unreadable(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [secret("TOKEN")])
        other_key = StackRepository(session, vault_with(SECOND_KEY), at(CREATED_AT))

        with pytest.raises(SecretUnreadableError):
            await other_key.reveal_secrets(stack_id)
        await session.execute(
            "UPDATE stack_variable SET value_ciphertext = 'v1:AAAA:BBBB' WHERE name = 'TOKEN'", ()
        )
        with pytest.raises(SecretUnreadableError):
            await stacks.reveal_secrets(stack_id)

    async def test_a_ciphertext_moved_to_another_variable_does_not_open(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [secret("ONE"), secret("TWO", NEW_SECRET_VALUE)])
        await session.execute(
            "UPDATE stack_variable SET value_ciphertext = "
            "(SELECT value_ciphertext FROM stack_variable WHERE name = 'ONE') WHERE name = 'TWO'",
            (),
        )

        with pytest.raises(SecretUnreadableError):
            await stacks.reveal_secrets(stack_id)


def id_of(listed: list[StoredVariable], name: str) -> int:
    return next(item.id for item in listed if item.name == name)


def database_bytes(path: Path) -> bytes:
    wal = path.with_name(path.name + "-wal")
    return path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


class TestReplaceVariables:
    async def test_a_secret_given_no_value_keeps_the_stored_ciphertext(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [secret("TOKEN")])
        before = await raw_variables(session, stack_id)

        changes = await stacks.replace_variables(stack_id, [secret("TOKEN", None), plain("NEW")])

        after = await raw_variables(session, stack_id)
        assert changes == VariableChanges(changed=1, removed=0)
        assert [row["name"] for row in after] == ["NEW", "TOKEN"]
        assert after[1] == before[0]
        assert await stacks.reveal_secrets(stack_id) == {"TOKEN": SECRET_VALUE}

    async def test_a_secret_given_a_new_value_is_replaced_and_counted_as_changed(
        self, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [secret("TOKEN")])

        changes = await stacks.replace_variables(stack_id, [secret("TOKEN", NEW_SECRET_VALUE)])

        assert changes == VariableChanges(changed=1, removed=0)
        assert await stacks.reveal_secrets(stack_id) == {"TOKEN": NEW_SECRET_VALUE}

    async def test_names_left_out_are_removed(self, stacks: StackRepository, stack_id: int) -> None:
        await stacks.replace_variables(stack_id, [plain("A"), secret("B"), plain("C")])

        changes = await stacks.replace_variables(stack_id, [plain("A")])

        assert changes == VariableChanges(changed=0, removed=2)
        assert [item.name for item in await stacks.variables(stack_id)] == ["A"]
        assert await stacks.reveal_secrets(stack_id) == {}

    async def test_a_variable_can_change_between_plain_and_secret(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [plain("A", "clear-before"), secret("B")])

        await stacks.replace_variables(stack_id, [secret("A", NEW_SECRET_VALUE), plain("B", "z")])

        first, second = await raw_variables(session, stack_id)
        assert (first["is_secret"], first["value"]) == (1, None)
        assert first["value_ciphertext"] is not None
        assert (second["is_secret"], second["value"], second["value_ciphertext"]) == (0, "z", None)
        assert await stacks.reveal_secrets(stack_id) == {"A": NEW_SECRET_VALUE}

    async def test_the_inputs_digest_moves_only_when_a_value_changed(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [plain("A"), secret("B")])
        first = await stored_inputs_digest(session, stack_id)

        unchanged = await stacks.replace_variables(stack_id, [plain("A"), secret("B", None)])
        after_no_change = await stored_inputs_digest(session, stack_id)
        await stacks.replace_variables(stack_id, [plain("A", "2"), secret("B", None)], LATER_AT)
        after_change = await stored_inputs_digest(session, stack_id)
        await stacks.replace_variables(stack_id, [plain("A"), secret("B", None)], LATER_AT)
        after_set_back = await stored_inputs_digest(session, stack_id)

        assert unchanged == VariableChanges(changed=0, removed=0)
        assert after_no_change == first
        assert after_change not in (None, first)
        assert after_set_back == first

    async def test_a_secret_given_the_same_value_again_changes_the_inputs_digest(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [secret("B")])
        first = await stored_inputs_digest(session, stack_id)

        await stacks.replace_variables(stack_id, [secret("B")])

        assert await stored_inputs_digest(session, stack_id) != first

    async def test_the_deployed_inputs_digest_takes_the_current_digest_on_a_deploy_only(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [plain("A")])
        current = await stored_inputs_digest(session, stack_id)

        await stacks.record_operation(stack_id, operation="stop", outcome="success")
        after_stop = await stacks.get_by_id(stack_id)
        await stacks.record_operation(
            stack_id,
            operation="deploy",
            outcome="success",
            deployed=DeployedState(DIGEST, OTHER_DIGEST),
        )
        after_deploy = await stacks.get_by_id(stack_id)

        assert after_stop is not None and after_deploy is not None
        assert after_stop.deployed_inputs_digest is None
        assert after_deploy.deployed_inputs_digest == current

    async def test_stored_files_take_part_in_the_inputs_digest_without_their_secret_content(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await session.execute(
            "INSERT INTO stack_file (stack_id, name, is_secret, content, content_digest, "
            "size_bytes, value_token, created_at, updated_at) "
            "VALUES (?, 'app.env', 0, 'A=1', ?, 3, 'abcdefghij12', ?, ?)",
            (stack_id, DIGEST, CREATED_STAMP, CREATED_STAMP),
        )

        digest = await stacks.refresh_inputs_digest(stack_id)

        assert digest == inputs_digest_of((), [("app.env", False, DIGEST)])
        assert digest != EMPTY_INPUTS_DIGEST

    async def test_a_secret_with_no_stored_value_to_keep_is_refused_and_nothing_is_written(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [plain("KEPT")])

        with pytest.raises(StackVariableError, match="BROKEN"):
            await stacks.replace_variables(
                stack_id, [plain("KEPT", "changed"), plain("EXTRA"), secret("BROKEN", None)]
            )

        raw = await raw_variables(session, stack_id)
        assert [(row["name"], row["value"]) for row in raw] == [("KEPT", PLAIN_VALUE)]

    @pytest.mark.parametrize(
        ("existing", "write"),
        [
            pytest.param([plain("A")], secret("A", None), id="secret-over-a-plain-variable"),
            pytest.param([], plain("A", None), id="plain-without-a-value"),
        ],
    )
    async def test_a_write_that_has_nothing_to_store_is_refused(
        self, stacks: StackRepository, stack_id: int, existing: list[Any], write: VariableWrite
    ) -> None:
        await stacks.replace_variables(stack_id, existing)

        with pytest.raises(StackVariableError):
            await stacks.replace_variables(stack_id, [write])

    async def test_duplicate_names_and_too_many_variables_are_refused(
        self, stacks: StackRepository, stack_id: int
    ) -> None:
        too_many = [plain(f"V{i}") for i in range(MAX_VARIABLES + 1)]

        with pytest.raises(ValueError, match="unique"):
            await stacks.replace_variables(stack_id, [plain("A"), plain("A")])
        with pytest.raises(ValueError, match="at most"):
            await stacks.replace_variables(stack_id, too_many)

    async def test_a_missing_stack_is_a_missing_reference(self, stacks: StackRepository) -> None:
        with pytest.raises(MissingReferenceError):
            await stacks.replace_variables(UNKNOWN_ID, [plain("A")])

    async def test_a_change_moves_the_stack_updated_time_too(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [plain("A")], LATER_AT)

        stored = await stacks.get_by_id(stack_id)

        assert stored is not None
        assert stored.updated_at == LATER_STAMP
        assert stored.inputs_digest != EMPTY_INPUTS_DIGEST


NAMES = ["alpha", "a_b", "axb", "5000off", "beta", "gamma", "ninth"]


class TestStackListRepository:
    @pytest.fixture
    async def listed(
        self, session: Session, stacks: StackRepository, environment_id: int
    ) -> StackListRepository:
        other = await add_environment(session, "two")
        for number, name in enumerate(NAMES, start=1):
            await add_stack(stacks, environment_id, name, number)
        await add_stack(stacks, other, "elsewhere", 100)
        return StackListRepository(session)

    async def test_a_page_lists_one_environment_ordered_by_name_without_the_files(
        self, listed: StackListRepository, environment_id: int
    ) -> None:
        page = await listed.page(environment_id, limit=50)

        assert [item.name for item in page.items] == sorted(NAMES)
        assert page.next_cursor is None
        assert not hasattr(page.items[0], "compose_file")
        assert page.items[0].compose_file_digest == DIGEST

    async def test_pages_follow_the_cursor_without_gaps_or_repeats(
        self, listed: StackListRepository, environment_id: int
    ) -> None:
        first = await listed.page(environment_id, limit=3)
        second = await listed.page(environment_id, limit=3, cursor=first.next_cursor)
        third = await listed.page(environment_id, limit=3, cursor=second.next_cursor)

        assert [item.name for item in first.items] == sorted(NAMES)[:3]
        assert [item.name for item in second.items] == sorted(NAMES)[3:6]
        assert [item.name for item in third.items] == sorted(NAMES)[6:]
        assert (first.next_cursor is None, second.next_cursor is None) == (False, False)
        assert third.next_cursor is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("a", ["a_b", "alpha", "axb", "beta", "gamma"]),
            ("_", ["a_b"]),
            ("%", []),
            ("50", ["5000off"]),
            ("a_b", ["a_b"]),
            ("\\", []),
            ("missing", []),
        ],
    )
    async def test_the_name_filter_matches_text_and_treats_wildcards_literally(
        self, listed: StackListRepository, environment_id: int, text: str, expected: list[Any]
    ) -> None:
        page = await listed.page(environment_id, limit=50, name_contains=text)

        assert [item.name for item in page.items] == expected

    async def test_the_visible_clause_narrows_the_page_within_the_environment(
        self, listed: StackListRepository, environment_id: int
    ) -> None:
        visible = SqlFragment("s.name IN (?, ?, ?)", ("alpha", "gamma", "elsewhere"))

        page = await listed.page(environment_id, limit=50, visible=visible)

        assert [item.name for item in page.items] == ["alpha", "gamma"]

    async def test_the_filter_and_the_visible_clause_combine(
        self, listed: StackListRepository, environment_id: int
    ) -> None:
        visible = SqlFragment("s.name <> ?", ("axb",))

        page = await listed.page(environment_id, limit=50, name_contains="a", visible=visible)

        assert [item.name for item in page.items] == ["a_b", "alpha", "beta", "gamma"]

    async def test_an_empty_environment_has_an_empty_page(
        self, listed: StackListRepository
    ) -> None:
        page = await listed.page(UNKNOWN_ID, limit=10)

        assert (page.items, page.next_cursor) == ([], None)


INSTALLED_COMPOSE = "v2.31.0"
UPGRADED_COMPOSE = "v2.32.1"


def observation(compose_cli_version: str | None) -> StatusRecord:
    return StatusRecord(
        status="reachable",
        failed_stage=None,
        message=None,
        checked_at=LATER_STAMP,
        contacted_at=LATER_STAMP,
        compose_cli_version=compose_cli_version,
    )


class TestEnvironmentComposeVersion:
    @pytest.fixture
    def environments(self, session: Session) -> EnvironmentRepository:
        return EnvironmentRepository(session, at(CREATED_AT))

    async def test_a_status_record_stores_the_version_and_a_record_without_one_keeps_it(
        self, environments: EnvironmentRepository, environment_id: int
    ) -> None:
        await environments.record_status(environment_id, observation(INSTALLED_COMPOSE))
        stored = await environments.get(environment_id)
        await environments.record_status(environment_id, observation(None))
        kept = await environments.get(environment_id)
        await environments.record_status(environment_id, observation(UPGRADED_COMPOSE))
        replaced = await environments.get(environment_id)

        assert stored is not None and kept is not None and replaced is not None
        assert stored.compose_cli_version == INSTALLED_COMPOSE
        assert kept.compose_cli_version == INSTALLED_COMPOSE
        assert replaced.compose_cli_version == UPGRADED_COMPOSE

    async def test_a_new_environment_has_no_version(
        self, environments: EnvironmentRepository, environment_id: int
    ) -> None:
        created = await environments.get(environment_id)

        assert created is not None
        assert created.compose_cli_version is None

    async def test_record_compose_version_sets_it_without_touching_the_updated_time(
        self, session: Session, environment_id: int
    ) -> None:
        later = EnvironmentRepository(session, at(LATER_AT))

        recorded = await later.record_compose_version(environment_id, UPGRADED_COMPOSE)
        missing = await later.record_compose_version(UNKNOWN_ID, UPGRADED_COMPOSE)

        stored = await later.get(environment_id)
        assert (recorded, missing) == (True, False)
        assert stored is not None
        assert stored.compose_cli_version == UPGRADED_COMPOSE
        assert stored.updated_at == CREATED_STAMP


class TestSwarmStackRows:
    async def test_get_folded_finds_a_stack_of_either_kind_ignoring_case_within_the_environment(
        self, stacks: StackRepository, environment_id: int, session: Session
    ) -> None:
        other = await add_environment(session, "two")
        compose_id = await add_stack(stacks, environment_id, "shop", 1)
        swarm_id = await add_stack(stacks, environment_id, "WebApp", 2, "swarm")
        await add_stack(stacks, other, "elsewhere", 3)

        by_compose = await stacks.get_folded(environment_id, "SHOP")
        by_swarm = await stacks.get_folded(environment_id, "webapp")
        absent = await stacks.get_folded(environment_id, "missing")
        wrong_environment = await stacks.get_folded(environment_id, "elsewhere")

        assert by_compose is not None and by_compose.id == compose_id
        assert by_swarm is not None and (by_swarm.id, by_swarm.name) == (swarm_id, "WebApp")
        assert absent is None
        assert wrong_environment is None

    async def test_a_swarm_name_keeps_its_case_and_exact_lookup_does_not_fold(
        self, stacks: StackRepository, environment_id: int
    ) -> None:
        await add_stack(stacks, environment_id, SWARM_NAME, kind="swarm")

        exact = await stacks.get(environment_id, SWARM_NAME)
        folded_spelling = await stacks.get(environment_id, SWARM_NAME.lower())

        assert exact is not None and exact.name == SWARM_NAME and exact.kind == "swarm"
        assert folded_spelling is None

    async def test_a_name_folding_onto_a_stack_of_the_other_kind_is_a_duplicate(
        self, stacks: StackRepository, environment_id: int
    ) -> None:
        await add_stack(stacks, environment_id, STACK_NAME, 1)

        with pytest.raises(DuplicateError):
            await add_stack(stacks, environment_id, SWARM_NAME, 2, "swarm")

    @pytest.mark.parametrize("name", ["my.stack", "-shop", "_shop", "", "a" * 64, "a b"])
    async def test_a_swarm_name_outside_the_grammar_is_refused_before_a_row_is_written(
        self, stacks: StackRepository, environment_id: int, session: Session, name: str
    ) -> None:
        with pytest.raises(StackNameError):
            await add_stack(stacks, environment_id, name, kind="swarm")

        count = await session.fetch_one("SELECT count(*) AS n FROM stack")
        assert count is not None and count["n"] == 0

    async def test_names_can_be_limited_to_one_kind(
        self, stacks: StackRepository, environment_id: int
    ) -> None:
        await add_stack(stacks, environment_id, "alpha", 1)
        await add_stack(stacks, environment_id, "Beta", 2, "swarm")

        assert await stacks.names(environment_id) == ["Beta", "alpha"]
        assert await stacks.names(environment_id, kind="compose") == ["alpha"]
        assert await stacks.names(environment_id, kind="swarm") == ["Beta"]

    async def test_a_page_lists_only_the_requested_kind_and_both_when_none_is_given(
        self, session: Session, stacks: StackRepository, environment_id: int
    ) -> None:
        await add_stack(stacks, environment_id, "alpha", 1)
        await add_stack(stacks, environment_id, "Beta", 2, "swarm")
        listed = StackListRepository(session)

        compose = await listed.page(environment_id, limit=50, kind="compose")
        swarm = await listed.page(environment_id, limit=50, kind="swarm")
        both = await listed.page(environment_id, limit=50)

        assert [item.name for item in compose.items] == ["alpha"]
        assert [item.name for item in swarm.items] == ["Beta"]
        assert [item.name for item in both.items] == ["Beta", "alpha"]


class TestValueTokens:
    async def test_every_written_value_gets_a_fresh_twelve_character_token(
        self, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [secret("TOKEN"), plain("MODE")])

        listed = await stacks.variables(stack_id)

        tokens = [variable.value_token or "" for variable in listed]
        assert all(TOKEN_PATTERN.fullmatch(token) for token in tokens)
        assert len(set(tokens)) == len(tokens)

    async def test_a_rewritten_secret_gets_a_new_token_even_when_the_value_is_identical(
        self, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [secret("TOKEN")])
        before = await stacks.variable_tokens(stack_id)

        await stacks.replace_variables(stack_id, [secret("TOKEN")])

        after = await stacks.variable_tokens(stack_id)
        assert set(before) == set(after) == {"TOKEN"}
        assert before["TOKEN"] != after["TOKEN"]

    async def test_a_kept_secret_and_an_unchanged_plain_value_keep_their_tokens(
        self, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [secret("TOKEN"), plain("MODE")])
        before = {
            variable.name: variable.value_token for variable in await stacks.variables(stack_id)
        }

        await stacks.replace_variables(stack_id, [secret("TOKEN", None), plain("MODE")])

        after = {
            variable.name: variable.value_token for variable in await stacks.variables(stack_id)
        }
        assert after == before

    async def test_a_changed_plain_value_gets_a_new_token(
        self, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [plain("MODE")])
        [before] = await stacks.variables(stack_id)

        await stacks.replace_variables(stack_id, [plain("MODE", "changed")])

        [after] = await stacks.variables(stack_id)
        assert after.value_token != before.value_token

    async def test_variable_tokens_lists_secrets_only_and_leaves_out_an_untokened_row(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.replace_variables(stack_id, [secret("TOKEN"), secret("LEGACY"), plain("MODE")])
        await session.execute("UPDATE stack_variable SET value_token = NULL WHERE name = 'LEGACY'")

        tokens = await stacks.variable_tokens(stack_id)

        assert set(tokens) == {"TOKEN"}
        assert TOKEN_PATTERN.fullmatch(tokens["TOKEN"])


FILE_CONTENT = "A=1\n"
SECRET_FILE_CONTENT = "SECRET=file-secret-sentinel\n"


async def raw_files(session: Session, stack_id: int) -> list[dict[str, Any]]:
    rows = await session.fetch_limited(
        "SELECT name, is_secret, content, content_ciphertext, content_digest, size_bytes, "
        "value_token FROM stack_file WHERE stack_id = ? ORDER BY name",
        (stack_id,),
        limit=100,
    )
    return [dict(row) for row in rows]


class TestStackFiles:
    async def test_a_plain_file_is_stored_with_its_digest_and_size(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        stored = await stacks.files.put_file(stack_id, "app.env", content="é=1", secret=False)

        [raw] = await raw_files(session, stack_id)
        assert (raw["content"], raw["content_ciphertext"]) == ("é=1", None)
        assert raw["size_bytes"] == len("é=1".encode())
        assert raw["content_digest"] == hashlib.sha256("é=1".encode()).hexdigest()
        assert TOKEN_PATTERN.fullmatch(raw["value_token"])
        assert (stored.name, stored.is_secret, stored.content) == ("app.env", False, "é=1")

    async def test_a_secret_file_is_encrypted_and_neither_listed_nor_in_the_database_file(
        self, database: Database, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        stored = await stacks.files.put_file(
            stack_id, "s.env", content=SECRET_FILE_CONTENT, secret=True
        )

        [raw] = await raw_files(session, stack_id)
        listed = await stacks.files.files(stack_id)
        revealed = await stacks.files.reveal_files(stack_id)

        assert raw["content"] is None
        assert raw["content_digest"] is None
        assert raw["content_ciphertext"].startswith(f"v{FIRST_VERSION}:")
        assert [(item.name, item.content, item.content_digest) for item in listed] == [
            ("s.env", None, None)
        ]
        assert stored.content is None
        assert SECRET_FILE_CONTENT not in repr(listed)
        assert revealed == {"s.env": SECRET_FILE_CONTENT}
        assert SECRET_FILE_CONTENT.encode() not in database_bytes(database.path)

    async def test_every_content_write_issues_a_new_token_and_a_blank_secret_keeps_its_value(
        self, stacks: StackRepository, stack_id: int
    ) -> None:
        first = await stacks.files.put_file(stack_id, "a", content=FILE_CONTENT, secret=False)
        same = await stacks.files.put_file(stack_id, "a", content=FILE_CONTENT, secret=False)
        await stacks.files.put_file(stack_id, "s", content=SECRET_FILE_CONTENT, secret=True)
        kept = await stacks.files.put_file(stack_id, "s", content=None, secret=True)
        rewritten = await stacks.files.put_file(stack_id, "s", content="NEW\n", secret=True)

        assert same.value_token != first.value_token
        assert same.content_digest == first.content_digest
        assert (await stacks.files.reveal_files(stack_id))["s"] == "NEW\n"
        assert rewritten.value_token != kept.value_token

    async def test_a_file_can_change_between_plain_and_secret(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.files.put_file(stack_id, "a", content=FILE_CONTENT, secret=False)
        await stacks.files.put_file(stack_id, "a", content=SECRET_FILE_CONTENT, secret=True)
        [as_secret] = await raw_files(session, stack_id)
        await stacks.files.put_file(stack_id, "a", content=FILE_CONTENT, secret=False)
        [as_plain] = await raw_files(session, stack_id)

        assert (as_secret["content"], as_secret["content_digest"]) == (None, None)
        assert as_secret["content_ciphertext"] is not None
        assert (as_plain["content"], as_plain["content_ciphertext"]) == (FILE_CONTENT, None)

    @pytest.mark.parametrize(
        ("existing", "secret"),
        [pytest.param(False, True, id="plain-to-secret"), pytest.param(None, False, id="new")],
    )
    async def test_a_write_with_no_content_to_keep_is_refused(
        self, stacks: StackRepository, stack_id: int, existing: bool | None, secret: bool
    ) -> None:
        if existing is not None:
            await stacks.files.put_file(stack_id, "a", content=FILE_CONTENT, secret=existing)

        with pytest.raises(StackFileError, match="needs content"):
            await stacks.files.put_file(stack_id, "a", content=None, secret=secret)

    async def test_writes_and_deletes_recompute_the_inputs_digest_in_the_same_transaction(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.files.put_file(stack_id, "a", content=FILE_CONTENT, secret=False)
        plain_digest = await stored_inputs_digest(session, stack_id)
        secret_file = await stacks.files.put_file(stack_id, "s", content="x", secret=True)
        secret_digest = await stored_inputs_digest(session, stack_id)
        removed = await stacks.files.delete_file(stack_id, "s")
        missing = await stacks.files.delete_file(stack_id, "s")

        assert plain_digest == inputs_digest_of(
            (), [("a", False, hashlib.sha256(FILE_CONTENT.encode()).hexdigest())]
        )
        assert secret_digest != plain_digest
        assert await stored_inputs_digest(session, stack_id) == plain_digest
        assert (removed, missing) == (True, False)
        assert secret_file.value_token not in repr(plain_digest)

    async def test_names_are_unique_per_stack_ignoring_case(
        self, session: Session, stacks: StackRepository, stack_id: int, environment_id: int
    ) -> None:
        other = await add_stack(stacks, environment_id, "other", 2)
        await stacks.files.put_file(stack_id, "App.env", content="x", secret=False)

        with pytest.raises(DuplicateError):
            await stacks.files.put_file(stack_id, "app.ENV", content="x", secret=False)
        await stacks.files.put_file(other, "App.env", content="x", secret=False)

    async def test_the_count_and_total_limits_hold_and_a_refusal_changes_nothing(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        for number in range(MAX_FILES):
            await stacks.files.put_file(stack_id, f"f{number}", content="x", secret=False)
        digest = await stored_inputs_digest(session, stack_id)

        with pytest.raises(StackFileLimitError):
            await stacks.files.put_file(stack_id, "extra", content="x", secret=False)

        assert len(await raw_files(session, stack_id)) == MAX_FILES
        assert await stored_inputs_digest(session, stack_id) == digest

    async def test_the_total_size_limit_holds_exactly(
        self, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.files.put_file(stack_id, "a", content="x" * MAX_FILE_BYTES, secret=False)
        await stacks.files.put_file(
            stack_id, "b", content="x" * (MAX_FILES_TOTAL_BYTES - MAX_FILE_BYTES), secret=True
        )

        with pytest.raises(StackFileLimitError):
            await stacks.files.put_file(stack_id, "c", content="x", secret=False)

    async def test_a_missing_stack_is_a_missing_reference(self, stacks: StackRepository) -> None:
        with pytest.raises(MissingReferenceError):
            await stacks.files.put_file(UNKNOWN_ID, "a", content="x", secret=False)

    async def test_a_file_sealed_under_another_key_or_moved_to_another_row_is_unreadable(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.files.put_file(stack_id, "one", content="1", secret=True)
        await stacks.files.put_file(stack_id, "two", content="2", secret=True)
        other_key = StackRepository(session, vault_with(SECOND_KEY), at(CREATED_AT))

        with pytest.raises(SecretUnreadableError):
            await other_key.files.reveal_files(stack_id)
        await session.execute(
            "UPDATE stack_file SET content_ciphertext = "
            "(SELECT content_ciphertext FROM stack_file WHERE name = 'one') WHERE name = 'two'",
            (),
        )
        with pytest.raises(SecretUnreadableError):
            await stacks.files.reveal_files(stack_id)

    async def test_the_insert_placeholder_is_never_committed_when_sealing_fails(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        failing = StackRepository(
            session,
            FailingVault(Keyring(FIRST_VERSION, {FIRST_VERSION: FIRST_KEY}), INSTANCE_ONE),
            at(CREATED_AT),
        )

        async with session.transaction():
            with pytest.raises(SealFailure):
                await failing.files.put_file(stack_id, "s", content="x", secret=True)

        assert await raw_files(session, stack_id) == []

    async def test_deleting_a_stack_deletes_its_files(
        self, session: Session, stacks: StackRepository, stack_id: int
    ) -> None:
        await stacks.files.put_file(stack_id, "a", content="x", secret=False)

        await stacks.delete(stack_id)

        assert await raw_files(session, stack_id) == []


class TestStackFilesMigration:
    PREVIOUS_VERSION = 21

    def test_secret_variables_gain_tokens_and_no_row_is_lost(self, tmp_path: Path) -> None:
        previous = migrations_up_to(self.PREVIOUS_VERSION, tmp_path / "previous")
        connection = connect(tmp_path / "state.db")
        try:
            migrate(connection, previous)
            self.seed(connection)

            migrate(connection)

            counts = (
                connection.execute("SELECT count(*) FROM stack").fetchone()[0],
                connection.execute("SELECT count(*) FROM stack_variable").fetchone()[0],
            )
            tokens = connection.execute(
                "SELECT is_secret, value_token FROM stack_variable ORDER BY id"
            ).fetchall()
            columns = [row[1] for row in connection.execute("PRAGMA table_info(stack)")]
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()

        assert counts == (2, 3)
        assert tokens[0]["value_token"] is not None
        assert TOKEN_PATTERN.fullmatch(tokens[0]["value_token"])
        assert tokens[1]["value_token"] == "abcdefghij12"
        assert tokens[2]["value_token"] is None
        assert "variables_changed_at" not in columns
        assert {"inputs_digest", "deployed_inputs_digest"} <= set(columns)
        assert violations == []

    @staticmethod
    def seed(connection: sqlite3.Connection) -> None:
        run = connection.execute
        run(
            "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
            "VALUES ('one', 'unix_socket', '/run/one.sock', ?, ?)",
            (CREATED_STAMP, CREATED_STAMP),
        )
        for number, name in ((1, "alpha"), (2, "beta")):
            run(
                "INSERT INTO stack (environment_id, name, kind, origin, project_directory, "
                "compose_file, compose_file_digest, variables_changed_at, created_at, updated_at) "
                "VALUES (1, ?, 'compose', 'created', ?, ?, ?, ?, ?, ?)",
                (name, f"{number:032x}", FILE, DIGEST, CREATED_STAMP, CREATED_STAMP, CREATED_STAMP),
            )
        run(
            "INSERT INTO stack_variable (stack_id, name, is_secret, value_ciphertext, "
            "created_at, updated_at) VALUES (1, 'OLD', 1, 'sealed', ?, ?)",
            (CREATED_STAMP, CREATED_STAMP),
        )
        run(
            "INSERT INTO stack_variable (stack_id, name, is_secret, value_ciphertext, "
            "value_token, created_at, updated_at) "
            "VALUES (1, 'NEW', 1, 'sealed', 'abcdefghij12', ?, ?)",
            (CREATED_STAMP, CREATED_STAMP),
        )
        run(
            "INSERT INTO stack_variable (stack_id, name, is_secret, value, created_at, updated_at) "
            "VALUES (2, 'PLAIN', 0, 'x', ?, ?)",
            (CREATED_STAMP, CREATED_STAMP),
        )
