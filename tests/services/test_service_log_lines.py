"""Service log details tokens, the cross-task tail merge, resume positions and the deduper."""

import tracemalloc

import pytest

from app.docker_engine.frames import LogFrame, LogFrameDecoder
from app.services.log_lines import LogLine, LogLineSplitter
from app.services.service_log_lines import (
    MAX_DETAILS_BYTES,
    LogDeduper,
    ServiceLogLine,
    TailMerger,
    merge_tail,
    resume_since,
    split_details,
)
from tests.swarm_support import sample_bytes

TASK = "t" * 25
NODE = "n" * 25
OTHER_TASK = "u" * 25
BASE_NS = 1_767_225_600_000_000_000
TOKEN = (
    f"com.docker.swarm.node.id={NODE},com.docker.swarm.service.id={'s' * 25},"
    f"com.docker.swarm.task.id={TASK}"
)
SAMPLE_NODE = "t55prpsoltethywit8ykgjtgi"


def line(text: str, ns: int | None = 1, stream: str = "stdout") -> LogLine:
    stamp = None if ns is None else f"2026-01-01T00:00:00.{ns:09d}Z"
    return LogLine(stream, text, stamp, None if ns is None else BASE_NS + ns)  # type: ignore[arg-type]


def item(text: str, ns: int | None, task: str | None = None) -> ServiceLogLine:
    return ServiceLogLine(line(text, ns), task, None)


def lines_of(sample: str, *, raw: bool = False) -> list[ServiceLogLine]:
    body = sample_bytes(sample)
    if raw:
        frames = [LogFrame("stdout", body)]
    else:
        decoder = LogFrameDecoder()
        frames = decoder.feed(body)
        decoder.finish()
    splitter = LogLineSplitter(timestamps=True)
    return [split_details(entry) for entry in splitter.feed_frames(frames) + splitter.flush()]


def split(text: str) -> ServiceLogLine:
    return split_details(line(text))


def test_a_multiplexed_service_log_yields_the_task_and_node_of_every_line() -> None:
    found = lines_of("07-GET-logs-A-nontty-details.bin")

    assert [x.line.text for x in found] == ["err-line", "out-line", "out-line", "err-line"]
    assert [x.line.stream for x in found] == ["stderr", "stdout", "stdout", "stderr"]
    assert {x.node_id for x in found} == {SAMPLE_NODE}
    assert all(x.task_id is not None and len(x.task_id) == 25 for x in found)
    assert all(x.line.timestamp_ns for x in found)


def test_a_raw_tty_service_log_is_split_on_crlf_and_still_carries_the_task() -> None:
    found = lines_of("07-GET-logs-B-tty-details.bin", raw=True)

    assert [(x.line.text, x.task_id) for x in found] == [
        ("tty-out", "3tvudidwx1dgxyss9cuzjhj8p"),
        ("tty-err", "3tvudidwx1dgxyss9cuzjhj8p"),
    ]


def test_user_log_attributes_with_escaped_separators_do_not_hide_the_ids_or_the_message() -> None:
    found = lines_of("30-GET-logs-attr-details.bin")

    assert [(x.line.text, x.task_id) for x in found] == [("attr-line", "fqo047h2vy7m9mfp8cko7scqo")]


def test_a_tail_of_one_line_per_task_arrives_from_six_tasks() -> None:
    found = lines_of("25-GET-logs-A-tail1.bin")

    assert len(found) == 6
    assert len({x.task_id for x in found}) == 6


def test_merging_the_per_task_tails_keeps_the_newest_overall_oldest_first() -> None:
    found = lines_of("25-GET-logs-A-tail1.bin")
    stamps = sorted(x.line.timestamp_ns for x in found if x.line.timestamp_ns is not None)

    kept, cut = merge_tail(found, 3, max_bytes=10_000)

    assert [x.line.timestamp_ns for x in kept] == stamps[-3:]
    assert cut


def test_split_details_takes_the_ids_and_drops_the_token_from_the_text() -> None:
    result = split(f"{TOKEN} hello world")

    assert (result.task_id, result.node_id, result.line.text) == (TASK, NODE, "hello world")


@pytest.mark.parametrize("text", [f"{TOKEN} ", TOKEN])
def test_a_line_that_is_only_a_token_has_an_empty_message(text: str) -> None:
    assert split(text).line.text == ""


def test_equals_signs_in_the_message_stay_in_the_message() -> None:
    assert split(f"{TOKEN} a=b,c=d x").line.text == "a=b,c=d x"


@pytest.mark.parametrize("text", ["just text here", "a=b hello", "a=b%20c d", "\x1b[31m=x hello"])
def test_a_line_whose_first_word_is_not_a_swarm_token_is_unchanged(text: str) -> None:
    result = split(text)

    assert (result.line.text, result.task_id, result.node_id) == (text, None, None)


def test_a_line_without_a_timestamp_is_unchanged() -> None:
    unstamped = LogLine("stdout", f"{TOKEN} x")

    assert split_details(unstamped).line.text == f"{TOKEN} x"


@pytest.mark.parametrize(
    "token",
    [
        f"com.docker.swarm.task.id={'T' * 25},com.docker.swarm.node.id={NODE}",
        "com.docker.swarm.task.id=abc",
    ],
)
def test_an_id_that_is_not_25_lower_case_base_36_names_no_task_but_the_token_is_dropped(
    token: str,
) -> None:
    result = split(f"{token} m")

    assert (result.task_id, result.line.text) == (None, "m")


def test_a_key_the_engine_escaped_is_read_as_the_key_it_spells() -> None:
    assert split(f"com.docker.swarm%2Etask.id={TASK},z=1 m").task_id == TASK


def test_a_forged_second_task_id_with_another_value_names_no_task() -> None:
    forged = f"com.docker.swarm.task.id={TASK},com.docker.swarm.task.id={OTHER_TASK} m"

    assert split(forged).task_id is None


def test_the_same_task_id_stated_twice_is_accepted() -> None:
    twice = f"com.docker.swarm.task.id={TASK},com.docker.swarm.task.id={TASK} m"

    assert split(twice).task_id == TASK


def test_unrelated_user_attributes_before_the_swarm_keys_are_ignored() -> None:
    assert split(f"A=%25,{TOKEN} m").task_id == TASK


def test_a_token_within_the_cap_that_carries_a_long_user_attribute_is_read() -> None:
    long_user = "z=" + "v" * (MAX_DETAILS_BYTES - 100)
    result = split(f"com.docker.swarm.task.id={TASK},{long_user} msg")

    assert (result.task_id, result.line.text) == (TASK, "msg")


def test_a_token_over_the_cap_is_stripped_and_names_no_task_even_if_a_key_follows() -> None:
    over = "a=" + "v" * (MAX_DETAILS_BYTES + 1000)
    result = split(f"{over},com.docker.swarm.task.id={TASK} msg")

    assert (result.task_id, result.node_id, result.line.text) == (None, None, "msg")


def test_a_prefix_over_the_cap_that_is_forged_does_not_appear_as_log_text() -> None:
    forged = "a=" + "b" * 5000

    result = split(f"{forged} message")

    assert result.line.text == "message"
    assert result.task_id is None


def test_the_other_fields_of_a_line_survive_the_split() -> None:
    original = LogLine("stderr", f"{TOKEN} t", "2026-01-01T00:00:00Z", 5, True)

    result = split_details(original).line

    assert (result.stream, result.timestamp, result.timestamp_ns, result.truncated) == (
        "stderr",
        "2026-01-01T00:00:00Z",
        5,
        True,
    )


def test_the_merge_keeps_the_newest_lines_within_the_count_and_flags_the_cut() -> None:
    merger = TailMerger(3, max_bytes=10_000)
    for ns, text in ((5, "e"), (1, "a"), (4, "d"), (2, "b"), (3, "c")):
        merger.add(item(text, ns))

    assert [x.line.text for x in merger.result()] == ["c", "d", "e"]
    assert merger.cut


def test_the_merge_keeps_the_newest_lines_that_fit_the_byte_budget() -> None:
    merger = TailMerger(10, max_bytes=8)
    for ns, text in ((5, "e"), (1, "a"), (4, "dddd"), (2, "bb"), (3, "ccc")):
        merger.add(item(text, ns))

    assert [x.line.text for x in merger.result()] == ["ccc", "dddd", "e"]
    assert merger.cut


def test_fewer_lines_than_the_count_are_all_kept_sorted_and_not_cut() -> None:
    kept, cut = merge_tail([item("b", 2), item("a", 1)], 10, max_bytes=1000)

    assert ([x.line.text for x in kept], cut) == (["a", "b"], False)


def test_a_count_of_zero_keeps_nothing_and_reports_the_cut() -> None:
    assert merge_tail([item("a", 1)], 0, max_bytes=100) == ([], True)


def test_an_empty_input_is_empty_and_not_cut() -> None:
    assert merge_tail([], 5, max_bytes=100) == ([], False)


def test_lines_with_equal_timestamps_keep_their_arrival_order() -> None:
    kept, _ = merge_tail([item(t, 10) for t in "abc"], 3, max_bytes=100)

    assert [x.line.text for x in kept] == ["a", "b", "c"]


def test_a_line_without_a_timestamp_sorts_as_the_line_before_it() -> None:
    kept, _ = merge_tail([item("a", 5), item("b", None), item("c", 7)], 3, max_bytes=100)

    assert [x.line.text for x in kept] == ["a", "b", "c"]


def test_merging_a_large_stream_holds_memory_to_the_budget_not_the_input() -> None:
    megabyte_lines = (item("x" * 1024, ns) for ns in range(8_000))

    tracemalloc.start()
    kept, cut = merge_tail(megabyte_lines, 50, max_bytes=16 * 1024)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert cut
    assert len(kept) <= 16
    assert peak < 1024 * 1024


@pytest.mark.parametrize(
    ("last_ns", "since"),
    [
        (1792494478825000000, "1792494478.825000001"),
        (1792494478999999999, "1792494479.000000000"),
        (0, "0.000000001"),
        (-5, "0.000000001"),
    ],
)
def test_resume_since_is_the_first_nanosecond_after_the_last_line_as_seconds_and_nanos(
    last_ns: int, since: str
) -> None:
    assert resume_since(last_ns) == since


def test_the_deduper_passes_a_line_once() -> None:
    deduper = LogDeduper()
    first = item("x", 100, TASK)
    repeat = item("x", 100, TASK)

    assert deduper.accept(first)
    assert not deduper.accept(repeat)


@pytest.mark.parametrize(
    "other",
    [
        pytest.param(item("x", 100, OTHER_TASK), id="other task"),
        pytest.param(ServiceLogLine(line("x", 100, "stderr"), TASK, None), id="other stream"),
        pytest.param(item("y", 100, TASK), id="other text"),
    ],
)
def test_a_line_differing_in_task_stream_or_text_is_not_a_repeat(other: ServiceLogLine) -> None:
    deduper = LogDeduper()
    deduper.accept(item("x", 100, TASK))

    assert deduper.accept(other)


def test_the_deduper_forgets_the_oldest_key_past_its_window() -> None:
    deduper = LogDeduper(window=3)
    oldest = item("a", 1, TASK)
    for position in range(1, 5):
        deduper.accept(item(str(position), position, TASK))
    deduper.accept(oldest)

    assert deduper.accept(item("a", 1, TASK)) is False
    assert deduper.accept(item("4", 4, TASK)) is False


def test_a_line_without_a_timestamp_always_passes_the_deduper() -> None:
    deduper = LogDeduper()

    assert deduper.accept(item("x", None))
    assert deduper.accept(item("x", None))


def test_filter_returns_the_new_lines_of_a_batch_in_order() -> None:
    first, repeat, other = item("x", 1, TASK), item("x", 1, TASK), item("y", 1, TASK)

    assert LogDeduper().filter([first, repeat, other]) == [first, other]


def test_the_floor_drops_lines_at_or_before_the_resume_point() -> None:
    deduper = LogDeduper(floor=BASE_NS + 10)

    assert not deduper.accept(item("x", 10, TASK))
    assert not deduper.accept(item("x", 9, TASK))
    assert deduper.accept(item("x", 11, TASK))
