"""Tests for what a terminal request is made of: the form, the ticket and the failure reports."""

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.services.activity import UserActor
from app.services.container_actions import ContainerGone
from app.services.container_stats import ContainerNotRunning
from app.services.object_inspector import EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectBusy, TooManyStreams
from app.services.terminals import (
    CLOSE_ACCESS_ENDED,
    CLOSE_ERROR,
    CLOSE_NO_SLOT,
    CLOSE_NORMAL,
    CLOSE_POLICY,
    MAX_ARGUMENTS,
    MAX_COMMAND_CHARS,
    AttachBusy,
    AttachNotAvailable,
    InvalidTicket,
    IssuedTicket,
    RestrictedCache,
    RestrictedContainer,
    TerminalSpec,
    TerminalTickets,
    build_spec,
    open_failure,
    read_ticket_message,
)

LIFETIME = 30.0
PER_USER = 2
USER = 1
OTHER_USER = 2
SESSION = "s" * 64
OTHER_SESSION = "t" * 64
ENVIRONMENT = 7
CONTAINER = "a" * 64
OTHER_CONTAINER = "b" * 64
SPEC = TerminalSpec("exec", ("/bin/sh",), None, None, 24, 80)
HOLDER = LockHolder(UserActor(USER), "operator", "stop")


class Ticks:
    """A clock that moves only when told to."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def exec_spec(**form: str) -> TerminalSpec:
    return build_spec(form, mode="exec", is_administrator=False)


def spec_errors(*, administrator: bool = False, mode: str = "exec", **form: str) -> set[str]:
    with pytest.raises(ValidationFailed) as refused:
        build_spec(form, mode=mode, is_administrator=administrator)  # type: ignore[arg-type]
    return set(refused.value.fields)


def test_a_blank_form_asks_for_the_default_shell_at_the_default_size() -> None:
    spec = exec_spec()

    assert spec == TerminalSpec("exec", ("/bin/sh",), None, None, 24, 80)


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        ("/bin/bash", ("/bin/bash",)),
        ("  /bin/ash  ", ("/bin/ash",)),
        ("sh -c 'echo \"a b\"'", ("sh", "-c", 'echo "a b"')),
        ('python -c "print(1)"', ("python", "-c", "print(1)")),
        ("ls; rm -rf /", ("ls;", "rm", "-rf", "/")),
        ("echo $HOME `id` | cat > /x", ("echo", "$HOME", "`id`", "|", "cat", ">", "/x")),
        ("a\\ b c", ("a b", "c")),
        ("   ", ("/bin/sh",)),
    ],
    ids=[
        "shell",
        "padded",
        "single-quotes",
        "double-quotes",
        "no-shell-operators",
        "no-expansion",
        "escaped-space",
        "blank-is-default",
    ],
)
def test_the_command_is_split_like_a_shell_would_quote_it_and_never_run_through_one(
    command: str, argv: tuple[str, ...]
) -> None:
    assert exec_spec(command=command).command == argv


def test_a_command_at_exactly_the_character_and_word_limits_is_accepted() -> None:
    words = " ".join(["w"] * MAX_ARGUMENTS)
    long_word = "x" * MAX_COMMAND_CHARS

    assert len(exec_spec(command=words).command) == MAX_ARGUMENTS
    assert exec_spec(command=long_word).command == (long_word,)


@pytest.mark.parametrize(
    "command",
    [
        "ls 'unclosed",
        'ls "unclosed',
        "ls \\",
        "x" * (MAX_COMMAND_CHARS + 1),
        " ".join(["w"] * (MAX_ARGUMENTS + 1)),
        "a\x00b",
        "''",
    ],
    ids=[
        "single-quote",
        "double-quote",
        "trailing-backslash",
        "too-long",
        "too-many-words",
        "nul",
        "empty-program",
    ],
)
def test_a_command_that_cannot_be_split_or_is_over_a_limit_is_refused_by_field(
    command: str,
) -> None:
    assert spec_errors(command=command) == {"command"}


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        ("", None),
        ("   ", None),
        ("/srv/app", "/srv/app"),
        ("//a/./b/../c", "/a/c"),
        ("/", "/"),
        ("/../..", "/"),
        ("/srv/app/", "/srv/app"),
    ],
)
def test_a_working_directory_is_normalised_to_an_absolute_path(
    directory: str, expected: str | None
) -> None:
    assert exec_spec(working_dir=directory).working_dir == expected


@pytest.mark.parametrize("directory", ["relative/path", "~", "/a\x00b", "/" + "d" * 1024])
def test_a_working_directory_that_is_not_absolute_or_is_too_long_is_refused(
    directory: str,
) -> None:
    assert spec_errors(working_dir=directory) == {"working_dir"}


@pytest.mark.parametrize("user", ["root", "1000", "1000:1000", "www-data:www", "app.user_1"])
def test_an_administrator_may_choose_the_user(user: str) -> None:
    spec = build_spec({"user": user}, mode="exec", is_administrator=True)

    assert spec.user == user


@pytest.mark.parametrize("user", ["bad user", "a:b:c", "ro/ot", "x" * 257, "root;id", ":group"])
def test_a_malformed_user_is_refused_for_an_administrator(user: str) -> None:
    assert spec_errors(administrator=True, user=user) == {"user"}


def test_a_standard_user_may_not_choose_the_user() -> None:
    assert spec_errors(user="root") == {"user"}


@pytest.mark.parametrize(
    ("rows", "cols", "expected"),
    [
        ("30", "100", (30, 100)),
        (" 40 ", " 90 ", (40, 90)),
        ("", "", (24, 80)),
        ("abc", "12.5", (24, 80)),
        ("-5", "-5", (24, 80)),
        ("٣", "٣", (24, 80)),
        ("0", "0", (1, 1)),
        ("501", "1001", (500, 1000)),
        ("5000", "50000", (24, 80)),
    ],
    ids=[
        "given",
        "padded",
        "blank",
        "not-numbers",
        "negative",
        "non-ascii",
        "zero-clamped",
        "just-over-clamped",
        "too-many-digits-ignored",
    ],
)
def test_terminal_size_falls_back_when_unreadable_and_is_clamped_to_the_engines_range(
    rows: str, cols: str, expected: tuple[int, int]
) -> None:
    spec = exec_spec(rows=rows, cols=cols)

    assert (spec.rows, spec.cols) == expected


def test_an_attach_request_takes_only_the_size_whatever_else_was_posted() -> None:
    spec = build_spec(
        {
            "command": "ls 'bad",
            "working_dir": "relative",
            "user": "root",
            "rows": "30",
            "cols": "100",
        },
        mode="attach",
        is_administrator=False,
    )

    assert spec == TerminalSpec("attach", (), None, None, 30, 100)


def test_every_bad_field_is_reported_in_one_refusal() -> None:
    assert spec_errors(command="x'", working_dir="rel", user="root") == {
        "command",
        "working_dir",
        "user",
    }


def issued(tickets: TerminalTickets, user: int = USER) -> str:
    return tickets.issue(user, SESSION, ENVIRONMENT, CONTAINER, SPEC)


def redeem(tickets: TerminalTickets, value: str, **overrides: object) -> TerminalSpec:
    arguments: dict[str, object] = {
        "user_id": USER,
        "session_hash": SESSION,
        "environment_id": ENVIRONMENT,
        "container_id": CONTAINER,
    } | overrides
    return tickets.redeem(
        value,
        arguments["user_id"],  # type: ignore[arg-type]
        arguments["session_hash"],  # type: ignore[arg-type]
        arguments["environment_id"],  # type: ignore[arg-type]
        arguments["container_id"],  # type: ignore[arg-type]
    )


@pytest.fixture
def clock() -> Ticks:
    return Ticks()


@pytest.fixture
def tickets(clock: Ticks) -> TerminalTickets:
    return TerminalTickets(clock=clock, lifetime=LIFETIME, per_user=PER_USER)


def test_a_ticket_returns_the_request_it_was_issued_for(tickets: TerminalTickets) -> None:
    value = issued(tickets)

    assert redeem(tickets, value) == SPEC


def test_a_ticket_is_single_use(tickets: TerminalTickets) -> None:
    value = issued(tickets)
    redeem(tickets, value)

    with pytest.raises(InvalidTicket):
        redeem(tickets, value)


@pytest.mark.parametrize(
    "mismatch",
    [
        {"user_id": OTHER_USER},
        {"session_hash": OTHER_SESSION},
        {"environment_id": ENVIRONMENT + 1},
        {"container_id": OTHER_CONTAINER},
    ],
    ids=["user", "session", "environment", "container"],
)
def test_a_ticket_presented_for_anything_else_is_refused_and_spent(
    tickets: TerminalTickets, mismatch: dict[str, object]
) -> None:
    value = issued(tickets)
    with pytest.raises(InvalidTicket):
        redeem(tickets, value, **mismatch)

    with pytest.raises(InvalidTicket):
        redeem(tickets, value)


def test_a_ticket_is_valid_until_its_lifetime_has_passed(
    tickets: TerminalTickets, clock: Ticks
) -> None:
    value = issued(tickets)
    clock.now += LIFETIME - 0.001

    assert redeem(tickets, value) == SPEC


def test_a_ticket_expires_at_its_lifetime(tickets: TerminalTickets, clock: Ticks) -> None:
    value = issued(tickets)
    clock.now += LIFETIME

    with pytest.raises(InvalidTicket):
        redeem(tickets, value)


def test_expired_tickets_are_dropped_when_the_next_one_is_issued(
    tickets: TerminalTickets, clock: Ticks
) -> None:
    issued(tickets)
    clock.now += LIFETIME

    issued(tickets, OTHER_USER)

    assert len(tickets) == 1


def test_a_user_beyond_the_ticket_cap_loses_their_oldest_and_others_are_untouched(
    tickets: TerminalTickets,
) -> None:
    oldest, newer, newest = issued(tickets), issued(tickets), issued(tickets)
    other = issued(tickets, OTHER_USER)

    with pytest.raises(InvalidTicket):
        redeem(tickets, oldest)
    assert redeem(tickets, newer) == SPEC
    assert redeem(tickets, newest) == SPEC
    assert redeem(tickets, other, user_id=OTHER_USER) == SPEC


@pytest.mark.parametrize(
    "value",
    ["", "short", "x" * 44, "x" * 42, "!" * 43, "a b" + "c" * 40, "A" * 43],
    ids=["empty", "short", "long", "one-short", "bad-characters", "space", "well-formed-unknown"],
)
def test_a_value_that_was_never_issued_is_refused(tickets: TerminalTickets, value: str) -> None:
    issued(tickets)

    with pytest.raises(InvalidTicket):
        redeem(tickets, value)


def test_every_ticket_is_different_and_long_enough_to_be_unguessable(
    tickets: TerminalTickets,
) -> None:
    values = {issued(tickets, user) for user in range(50)}

    assert len(values) == 50
    assert {len(value) for value in values} == {43}


def test_the_issued_ticket_value_is_kept_out_of_its_representation() -> None:
    ticket = IssuedTicket("SECRET-TICKET-VALUE", "/path", "exec", "web", "abc", 24, 80)

    assert "SECRET-TICKET-VALUE" not in repr(ticket)


def test_the_first_message_names_the_ticket() -> None:
    assert read_ticket_message('{"t":"ticket","v":"abc"}') == "abc"


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "null",
        '"ticket"',
        '{"t":"resize","v":"abc"}',
        '{"t":"ticket"}',
        '{"t":"ticket","v":5}',
        '{"t":"ticket","v":null}',
        '{"t":"ticket","v":"' + "a" * 600 + '"}',
        '{"t":"ticket","v":"' + "é" * 300 + '"}',
        "[" * 100_000,
        "",
    ],
    ids=[
        "text",
        "list",
        "null",
        "string",
        "wrong-kind",
        "no-value",
        "number-value",
        "null-value",
        "over-512-characters",
        "over-512-bytes",
        "deep-nesting",
        "empty",
    ],
)
def test_anything_but_a_well_formed_ticket_message_is_an_invalid_ticket(text: str) -> None:
    with pytest.raises(InvalidTicket):
        read_ticket_message(text)


QUEUE_REFUSAL = TooManyStreams("user", "terminal")
PROCESS_REFUSAL = TooManyStreams("process", "view")
RESTRICTED = RestrictedContainer(("host bind mounts", "privileged mode"))
NOT_RUNNING = ContainerNotRunning()
GONE = ContainerGone()
BUSY_ATTACH = AttachBusy(HOLDER)
BUSY_OBJECT = ObjectBusy(HOLDER)
NO_ATTACH = AttachNotAvailable("Open a terminal with exec instead.")
UNREACHABLE = EnvironmentUnavailable("unreachable")
UNKNOWN = EnvironmentUnavailable("outcome_unknown")


@pytest.mark.parametrize(
    ("error", "reason", "close_code", "message"),
    [
        (InvalidTicket(), "bad_ticket", CLOSE_POLICY, None),
        (QUEUE_REFUSAL, "slots_user", CLOSE_NO_SLOT, str(QUEUE_REFUSAL)),
        (PROCESS_REFUSAL, "slots_process", CLOSE_NO_SLOT, str(PROCESS_REFUSAL)),
        (RESTRICTED, "restricted_container", CLOSE_ACCESS_ENDED, str(RESTRICTED)),
        (ResourceNotFound("x"), "access_denied", CLOSE_ACCESS_ENDED, None),
        (PermissionDenied("x"), "access_denied", CLOSE_ACCESS_ENDED, None),
        (ValidationFailed({"command": "bad"}), "invalid", CLOSE_ACCESS_ENDED, None),
        (NOT_RUNNING, "not_running", CLOSE_NORMAL, str(NOT_RUNNING)),
        (GONE, "not_running", CLOSE_NORMAL, str(GONE)),
        (BUSY_ATTACH, "attach_busy", CLOSE_NORMAL, str(BUSY_ATTACH)),
        (
            BUSY_OBJECT,
            "object_busy",
            CLOSE_NORMAL,
            "Another action on this container is in progress. Try again shortly.",
        ),
        (NO_ATTACH, "attach_unavailable", CLOSE_NORMAL, "Open a terminal with exec instead."),
        (UNREACHABLE, "unreachable", CLOSE_ERROR, str(UNREACHABLE)),
        (UNKNOWN, "outcome_unknown", CLOSE_ERROR, str(UNKNOWN)),
        (RuntimeError("engine said SENTINEL"), "engine_error", CLOSE_ERROR, None),
    ],
    ids=[
        "bad-ticket",
        "slots-user",
        "slots-process",
        "restricted",
        "not-found",
        "denied",
        "invalid",
        "not-running",
        "gone",
        "attach-busy",
        "object-busy",
        "attach-unavailable",
        "unreachable",
        "outcome-unknown",
        "other",
    ],
)
def test_a_failed_open_is_reported_with_a_fixed_reason_close_code_and_product_sentence(
    error: Exception, reason: str, close_code: int, message: str | None
) -> None:
    failure = open_failure(error)

    assert (failure.reason, failure.close_code, failure.message) == (reason, close_code, message)


def test_the_busy_report_names_nobody() -> None:
    assert "operator" not in open_failure(BUSY_ATTACH).message  # type: ignore[operator]
    assert "operator" not in str(BUSY_ATTACH)


def test_the_restricted_refusal_names_the_options_that_caused_it() -> None:
    assert str(RESTRICTED) == (
        "Only administrators may open a terminal in a container that uses "
        "host bind mounts, privileged mode."
    )


def test_the_restricted_cache_remembers_by_environment_and_container() -> None:
    cache = RestrictedCache()
    cache.remember(ENVIRONMENT, CONTAINER, ("privileged",))

    assert cache.recall(ENVIRONMENT, CONTAINER) == ("privileged",)
    assert cache.recall(ENVIRONMENT + 1, CONTAINER) is None
    assert cache.recall(ENVIRONMENT, OTHER_CONTAINER) is None


def test_the_restricted_cache_drops_the_least_recently_used_entry_at_its_size() -> None:
    cache = RestrictedCache(size=2)
    cache.remember(ENVIRONMENT, "1", ())
    cache.remember(ENVIRONMENT, "2", ())
    cache.recall(ENVIRONMENT, "1")

    cache.remember(ENVIRONMENT, "3", ())

    assert (cache.recall(ENVIRONMENT, "1"), cache.recall(ENVIRONMENT, "2")) == ((), None)
    assert len(cache) == 2
