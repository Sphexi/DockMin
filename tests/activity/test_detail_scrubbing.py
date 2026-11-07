"""Detail is scrubbed, then bounded, before it is stored; credentials never reach a row."""

import time
from typing import Any

import pytest

from app.db.repositories.activity_event import (
    MAX_DETAIL_KEYS,
    MAX_DETAIL_SCRUB_INPUT,
    MAX_DETAIL_TEXT,
    ActivityEventRepository,
)
from app.logging import DEFAULT_SCRUBBER, REDACTED
from tests.activity.support import RecordingSession

SECRET = "hunter2"
LONG_WORD_LENGTH = 65_536
PAIR_COUNT = 10_000
TIME_BUDGET_SECONDS = 0.5
PEM_BLOCK = f"-----BEGIN PRIVATE KEY-----\n{SECRET}\n-----END PRIVATE KEY-----"

REDACTED_DETAIL = [
    pytest.param({"client_key_pem": SECRET}, id="pem-field-name"),
    pytest.param({"password": SECRET}, id="password-field-name"),
    pytest.param({"message": f"failed to load {PEM_BLOCK} from disk"}, id="private-key-block"),
    pytest.param({"message": f"connect failed: secret={SECRET}"}, id="pair-in-exception-text"),
    pytest.param({"message": f'{{\\"password\\":\\"{SECRET}\\"}}'}, id="escaped-json"),
    pytest.param({"message": f'{{"auth":"{SECRET}"}}'}, id="docker-auth"),
    pytest.param({"message": f'{{"identitytoken":"{SECRET}"}}'}, id="docker-identity-token"),
    pytest.param({"message": f"registrytoken={SECRET}"}, id="docker-registry-token"),
    pytest.param(
        {"message": f"pull https://bob:{SECRET}@registry.example/ failed"}, id="url-credentials"
    ),
    pytest.param(
        {"message": f"rejected Bearer eyJhbGciOiJIUzI1NiJ9.{SECRET}.signature"}, id="bearer-token"
    ),
    pytest.param({"message": f"pwd={SECRET}"}, id="pwd-pair"),
    pytest.param({"message": f"pass={SECRET}"}, id="pass-pair"),
    pytest.param({"message": f"apikey={SECRET}"}, id="apikey-pair"),
    pytest.param({"message": f'password="{SECRET}'}, id="unclosed-quote"),
    pytest.param({"message": f'{{"password":"a\\"{SECRET}"}}'}, id="escaped-quote"),
]
UNCHANGED_PROSE = [
    "informational",
    "password reset requested",
    "pass the test",
    "Bearer authentication failed",
    "https://host:5000/v2/",
]

NEAR_STORED_LIMIT = "." * (MAX_DETAIL_TEXT - len("password=hun")) + f"password={SECRET}"
REDACTED_RUN = "password=" + "x" * MAX_DETAIL_SCRUB_INPUT
PAIR_ACROSS_SCRUB_LIMIT = (
    REDACTED_RUN[: MAX_DETAIL_SCRUB_INPUT - len(" token=hun")] + f" token={SECRET}"
)
KEY_ACROSS_SCRUB_LIMIT = (
    REDACTED_RUN[: MAX_DETAIL_SCRUB_INPUT - len(" -----BEGIN PRIVATE KEY-----\nhun")]
    + f" {PEM_BLOCK}"
)
BOUNDARY_VALUES = [
    pytest.param(NEAR_STORED_LIMIT, id="across-the-stored-limit"),
    pytest.param(PAIR_ACROSS_SCRUB_LIMIT, id="pair-across-the-scrub-input-limit"),
    pytest.param(KEY_ACROSS_SCRUB_LIMIT, id="key-block-across-the-scrub-input-limit"),
]

NOT_FLAT = [
    pytest.param({"nested": {"a": 1}}, id="nested-mapping"),
    pytest.param({"listed": [1, 2]}, id="list"),
    pytest.param({"fraction": 1.5}, id="float"),
    pytest.param({3: "x"}, id="non-string-key"),
    pytest.param({f"key{n}": n for n in range(MAX_DETAIL_KEYS + 1)}, id="too-many-keys"),
]


async def store(
    recording: RecordingSession, detail: dict[Any, Any] | None
) -> dict[str, Any] | None:
    await ActivityEventRepository(recording).insert(  # type: ignore[arg-type]
        category="user_action",
        actor_type="user",
        actor_user_id=1,
        actor_subsystem=None,
        rule_id=None,
        job_run_id=None,
        request_id=None,
        environment_id=None,
        target_type="probe",
        target_id=None,
        action="probe",
        result="failure",
        detail=detail,
    )
    return recording.stored_detail() if detail else None


@pytest.mark.parametrize("detail", REDACTED_DETAIL)
async def test_a_credential_in_detail_is_redacted_in_the_stored_row(
    recording: RecordingSession, detail: dict[str, str]
) -> None:
    stored = await store(recording, detail)

    assert SECRET not in str(stored)
    assert REDACTED in str(stored)


@pytest.mark.parametrize("prose", UNCHANGED_PROSE)
async def test_ordinary_prose_that_resembles_a_credential_is_stored_unchanged(
    recording: RecordingSession, prose: str
) -> None:
    stored = await store(recording, {"message": prose})

    assert stored == {"message": prose}


@pytest.mark.parametrize("value", BOUNDARY_VALUES)
async def test_a_secret_across_a_truncation_boundary_is_not_exposed(
    recording: RecordingSession, value: str
) -> None:
    stored = await store(recording, {"message": value})

    assert stored is not None
    assert "hun" not in stored["message"]


async def test_a_long_value_is_stored_cut_to_the_text_limit(recording: RecordingSession) -> None:
    stored = await store(recording, {"message": "a" * (MAX_DETAIL_TEXT * 20)})

    assert stored == {"message": "a" * MAX_DETAIL_TEXT}


async def test_detail_is_stored_as_compact_key_sorted_ascii_json(
    recording: RecordingSession,
) -> None:
    await store(recording, {"username": "zoë", "address": "a"})

    [written] = recording.writes
    assert written["detail"] == '{"address":"a","username":"zo\\u00eb"}'


LONE_SURROGATE = "\ud800"


@pytest.mark.parametrize(
    ("detail", "stored"),
    [
        pytest.param({"note": f"a{LONE_SURROGATE}b"}, {"note": "a?b"}, id="in-a-value"),
        pytest.param({f"k{LONE_SURROGATE}": "v"}, {"k?": "v"}, id="in-a-key"),
    ],
)
async def test_a_lone_surrogate_in_detail_is_replaced_so_the_row_can_be_read_back(
    recording: RecordingSession, detail: dict[str, str], stored: dict[str, str]
) -> None:
    assert await store(recording, detail) == stored


async def test_an_empty_detail_is_stored_as_null(recording: RecordingSession) -> None:
    await store(recording, {})

    [written] = recording.writes
    assert written["detail"] is None


@pytest.mark.parametrize("detail", NOT_FLAT)
async def test_detail_that_is_not_a_flat_bounded_mapping_is_refused(
    recording: RecordingSession, detail: dict[Any, Any]
) -> None:
    with pytest.raises(ValueError, match="detail holds"):
        await store(recording, detail)

    assert recording.writes == []


async def test_storing_detail_never_registers_a_value_as_a_secret(
    recording: RecordingSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered: list[str] = []
    monkeypatch.setattr(DEFAULT_SCRUBBER, "register_secret", registered.append)

    await store(recording, {"password": SECRET, "message": f"token={SECRET}"})

    assert registered == []


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a" * LONG_WORD_LENGTH, id="one-long-word"),
        pytest.param("a=b=" * PAIR_COUNT, id="chain-of-pairs"),
    ],
)
def test_scrubbing_a_hostile_string_takes_linear_time(text: str) -> None:
    started = time.perf_counter()
    DEFAULT_SCRUBBER.scrub_text(text)
    elapsed = time.perf_counter() - started

    assert elapsed < TIME_BUDGET_SECONDS
