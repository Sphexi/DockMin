"""Tests for app/services/dockerfile_check.py: refused constructs and extracted image references."""

from collections.abc import Mapping

import pytest

from app.services.dockerfile_check import (
    MAX_DOCKERFILE_BYTES,
    DockerfileReferences,
    DockerfileRefusal,
    check_dockerfile,
)

NO_BREAK_SPACE = chr(0xA0)


def dockerfile(*lines: str) -> bytes:
    return "\n".join(lines).encode("utf-8")


def references(
    content: bytes, args: Mapping[str, str] | None = None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    result = check_dockerfile(content, args or {})
    assert isinstance(result, DockerfileReferences), result
    return result.bases, result.copy_sources


def refusal(content: bytes, args: Mapping[str, str] | None = None) -> DockerfileRefusal:
    result = check_dockerfile(content, args or {})
    assert isinstance(result, DockerfileRefusal), result
    return result


ACCEPTED = {
    "a single base": (("FROM python:3.12-slim",), (("python:3.12-slim",), ())),
    "instructions in any case": (
        ("from alpine:3", "Run echo hi", "cOpY a b"),
        (("alpine:3",), ()),
    ),
    "a platform flag and a stage": (
        (
            "FROM --platform=$BUILDPLATFORM golang:1.22 AS build",
            "FROM alpine",
            "COPY --from=build a b",
        ),
        (("golang:1.22", "alpine"), ()),
    ),
    "scratch is not an image": (("FROM scratch",), ((), ())),
    "the same base twice": (("FROM a", "FROM a AS b", "FROM b"), (("a",), ())),
    "comments and blank lines": (
        ("# note", "", "  # indented note", "FROM a", "# FROM evil", "RUN true"),
        (("a",), ()),
    ),
    "a continued FROM": (("FROM \\", "  alpine"), (("alpine",), ())),
    "a comment inside a continuation": (
        ("FROM a", "RUN echo a \\", "  # --network=host", "  && echo b"),
        (("a",), ()),
    ),
    "an empty line inside a continuation": (
        ("FROM a", "RUN echo a \\", "", "  && echo b"),
        (("a",), ()),
    ),
    "trailing blanks after the continuation character": (
        ("FROM a", "RUN echo a \\ \t", "  && true"),
        (("a",), ()),
    ),
    "a flag-like word in the arguments is not a flag": (
        ("FROM a", "RUN echo \\", " --network=host"),
        (("a",), ()),
    ),
    "JSON-form arguments": (
        ("FROM a", 'RUN ["echo", "--network=host"]', 'COPY ["x", "y"]', 'CMD ["a"]'),
        (("a",), ()),
    ),
    "JSON-form COPY with a flag": (
        ("FROM a", 'COPY --from=tools:1 ["x", "y"]'),
        (("a",), ("tools:1",)),
    ),
    "allowed networks": (
        ("FROM a", "RUN --network=default true", "RUN --network=none true"),
        (("a",), ()),
    ),
    "mounts other than ssh and secret": (
        ("FROM a", "RUN --mount=type=cache,target=/c --mount=type=tmpfs,target=/t true"),
        (("a",), ()),
    ),
    "quotes after the flags are part of the command": (
        ("FROM a", 'RUN --network=none echo "--mount=type=bind,from=evil"'),
        (("a",), ()),
    ),
    "ADD of a local file": (("FROM a", "ADD src.tar.gz $HOME/"), (("a",), ())),
    "ADD of a remote file with a checksum": (
        ("FROM a", "ADD --checksum=sha256:abc https://example.com/f.tgz /opt/"),
        (("a",), ()),
    ),
    "ADD of a Git source with a checksum": (
        ("FROM a", "ADD --checksum=abc123 git@example.com:org/repo.git /src"),
        (("a",), ()),
    ),
    "a CRLF file": (b"FROM a\r\nRUN true \\\r\n && true\r\n", (("a",), ())),
    "a byte order mark": (b"\xef\xbb\xbfFROM a", (("a",), ())),
    "a leading no-break space": ((NO_BREAK_SPACE + "FROM a",), (("a",), ())),
    "the check directive": (("# check=skip=all", "FROM a"), (("a",), ())),
    "an unknown directive": (("# marker=1", "FROM a"), (("a",), ())),
    "a backtick escape": (
        ("# escape=`", "FROM a", "RUN echo a `", "  && echo b \\", "RUN true"),
        (("a",), ()),
    ),
    "an ARG after the first FROM is not global": (("ARG X=1", "FROM a", "ARG Y=2"), (("a",), ())),
}

IMAGE_REFERENCES = {
    "COPY --from an image": (("FROM a", "COPY --from=nginx:1 /x /y"), (("a",), ("nginx:1",))),
    "ADD --from an image": (("FROM a", "ADD --from=nginx:1 /x /y"), (("a",), ("nginx:1",))),
    "a bind mount from an image": (
        ("FROM a", "RUN --mount=type=bind,from=reg.example/tools:2,target=/t true"),
        (("a",), ("reg.example/tools:2",)),
    ),
    "a cache mount from an image": (
        ("FROM a", "RUN --mount=type=cache,from=img,target=/c true"),
        (("a",), ("img",)),
    ),
    "several, once each, in order": (
        (
            "FROM a",
            "COPY --from=one /x /y",
            "COPY --from=two /x /y",
            "COPY --from=one /x /z",
        ),
        (("a",), ("one", "two")),
    ),
    "a name declared later is an image": (
        ("FROM a", "COPY --from=later /x /y", "FROM b AS later"),
        (("a", "b"), ("later",)),
    ),
}

STAGES = {
    "a stage in any case": (
        ("FROM a AS Builder", "FROM b", "COPY --from=builder x y", "COPY --from=BUILDER x y"),
        (("a", "b"), ()),
    ),
    "a stage by index": (("FROM a", "FROM b", "COPY --from=0 x y"), (("a", "b"), ())),
    "a mount from a stage": (
        ("FROM a AS t", "FROM b", "RUN --mount=type=bind,from=t,target=/x true"),
        (("a", "b"), ()),
    ),
    "a FROM naming a stage": (("FROM a AS one", "FROM one AS two"), (("a",), ())),
    "a FROM naming a stage through an argument": (
        ("ARG PARENT=one", "FROM a AS one", "FROM ${PARENT}"),
        (("a",), ()),
    ),
}

HEREDOCS = {
    "the body is opaque": (
        (
            "FROM a",
            "RUN <<EOF",
            "FROM private/evil",
            "RUN --network=host curl x",
            "COPY --from=hidden x y",
            "EOF",
            "COPY --from=tools x y",
        ),
        (("a",), ("tools",)),
    ),
    "a python script": (
        ("FROM a", "RUN python3 <<EOF", "from os import path", "print(path)", "EOF"),
        (("a",), ()),
    ),
    "a chomped terminator": (
        ("FROM a", "RUN <<-EOF", "\tx", "\tEOF", "COPY --from=t x y"),
        (("a",), ("t",)),
    ),
    "a quoted delimiter": (
        ("FROM a", "RUN <<'END'", "$X", "END", "COPY --from=t x y"),
        (("a",), ("t",)),
    ),
    "a file descriptor": (("FROM a", "RUN cat 3<<EOF", "x", "EOF"), (("a",), ())),
    "COPY with a heredoc": (("FROM a", "COPY <<EOF /x", "FROM private/evil", "EOF"), (("a",), ())),
    "two heredocs on one line": (
        ("FROM a", "RUN cat <<A && cat <<B", "one", "A", "two", "B", "COPY --from=t x y"),
        (("a",), ("t",)),
    ),
    "a terminator with trailing blanks does not end it": (
        ("FROM a", "RUN <<EOF", "EOF ", "RUN --network=host x", "EOF", "RUN true"),
        (("a",), ()),
    ),
    "a bit shift is not a heredoc": (
        ("FROM a", "RUN echo $((1<<n))", "COPY --from=t x y"),
        (("a",), ("t",)),
    ),
    "a quoted marker is not a heredoc": (
        ("FROM a", 'RUN echo "<<EOF"', "COPY --from=t x y"),
        (("a",), ("t",)),
    ),
    "a marker in JSON form is not a heredoc": (
        ("FROM a", 'RUN ["sh", "-c", "cat <<EOF"]', "COPY --from=t x y"),
        (("a",), ("t",)),
    ),
    "a here-string is not a heredoc": (
        ("FROM a", "RUN cat <<<word", "COPY --from=t x y"),
        (("a",), ("t",)),
    ),
}


class TestExtractedReferences:
    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            *ACCEPTED.values(),
            *IMAGE_REFERENCES.values(),
            *STAGES.values(),
            *HEREDOCS.values(),
        ],
        ids=[*ACCEPTED, *IMAGE_REFERENCES, *STAGES, *HEREDOCS],
    )
    def test_the_bases_and_from_images_are_extracted(
        self, lines: tuple[str, ...] | bytes, expected: tuple[tuple[str, ...], tuple[str, ...]]
    ) -> None:
        content = lines if isinstance(lines, bytes) else dockerfile(*lines)

        assert references(content) == expected

    def test_images_lists_bases_then_from_images_once_each(self) -> None:
        result = check_dockerfile(
            dockerfile("FROM a", "COPY --from=b x y", "COPY --from=a x y"), {}
        )

        assert isinstance(result, DockerfileReferences)
        assert result.images == ("a", "b")


class TestFromSubstitution:
    @pytest.mark.parametrize("form", ["${BASE}", "$BASE"])
    def test_a_global_arg_default_resolves_the_base(self, form: str) -> None:
        assert references(dockerfile("ARG BASE=debian:12", f"FROM {form}")) == (("debian:12",), ())

    def test_a_build_argument_resolves_an_arg_without_a_default(self) -> None:
        content = dockerfile("ARG BASE", "FROM ${BASE}")

        assert references(content, {"BASE": "debian:11"}) == (("debian:11",), ())

    def test_a_build_argument_overrides_the_default(self) -> None:
        content = dockerfile("ARG BASE=debian:12", "FROM ${BASE}")

        assert references(content, {"BASE": "debian:11"}) == (("debian:11",), ())

    def test_a_reference_can_join_text_and_several_names(self) -> None:
        content = dockerfile("ARG REG=r.example", "ARG TAG=1", "FROM ${REG}/app:$TAG-slim")

        assert references(content) == (("r.example/app:1-slim",), ())

    def test_a_default_can_name_an_earlier_arg(self) -> None:
        content = dockerfile("ARG V=12", "ARG BASE=debian:$V", "FROM ${BASE}")

        assert references(content) == (("debian:12",), ())

    def test_quotes_around_a_default_are_removed(self) -> None:
        assert references(dockerfile('ARG BASE="debian:12"', "FROM ${BASE}")) == (
            ("debian:12",),
            (),
        )

    def test_several_args_on_one_line_are_all_declared(self) -> None:
        content = dockerfile("ARG A=x B=y", "FROM $A/$B")

        assert references(content) == (("x/y",), ())


class TestFromRefusals:
    @pytest.mark.parametrize(
        ("lines", "args", "line"),
        [
            (("ARG BASE=a", "FROM ${BASE:-x}"), {}, 2),
            (("ARG BASE=a", "FROM ${BASE#a}"), {}, 2),
            (("ARG BASE=a", "FROM ${BASE:+x}"), {}, 2),
            (("FROM ${BASE}",), {}, 1),
            (("ARG BASE", "FROM ${BASE}"), {}, 2),
            (("ARG BASE=", "FROM ${BASE}"), {}, 2),
            (("FROM ${BASE}",), {"BASE": "a"}, 1),
            (("FROM a", "ARG BASE=b", "FROM ${BASE}"), {}, 3),
            (("ARG BASE=a", "FROM $1"), {}, 2),
            (("ARG BASE=a", "FROM $$BASE"), {}, 2),
            (("ARG BASE=a", "FROM $(echo a)"), {}, 2),
            (("ARG BASE=$OTHER", "FROM ${BASE}"), {}, 2),
            (("ARG BASE", "FROM ${BASE}"), {"BASE": "a b"}, 2),
            (("ARG BASE", "FROM ${BASE}"), {"BASE": "$X"}, 2),
            (("ARG BASE", "FROM ${BASE}"), {"BASE": "'a'"}, 2),
            (('FROM "a"',), {}, 1),
            (("FROM 'a'",), {}, 1),
            (("FROM a\\b",), {}, 1),
            (("FROM",), {}, 1),
            (("FROM a b",), {}, 1),
            (("FROM a AS",), {}, 1),
            (("FROM a AT b",), {}, 1),
            (("FROM a AS b c",), {}, 1),
            (("FROM --foo=1 a",), {}, 1),
            (("FROM a AS 1x",), {}, 1),
            (("FROM a AS x", "FROM b AS X"), {}, 2),
            (("RUN true", "FROM a"), {}, 1),
            (("ENV A=1", "FROM a"), {}, 1),
        ],
        ids=lambda value: repr(value)[:40] if not isinstance(value, int) else f"line{value}",
    )
    def test_an_unresolvable_or_unsupported_from_is_refused_at_its_line(
        self, lines: tuple[str, ...], args: dict[str, str], line: int
    ) -> None:
        assert refusal(dockerfile(*lines), args).line == line


class TestDirectives:
    @pytest.mark.parametrize(
        ("lines", "line"),
        [
            (("# syntax=docker/dockerfile:1", "FROM a"), 1),
            (("#syntax=docker/dockerfile:1", "FROM a"), 1),
            (("# SYNTAX = x", "FROM a"), 1),
            (("  # syntax=x", "FROM a"), 1),
            (("# check=skip=all", "# syntax=x", "FROM a"), 2),
            (("# note", "# syntax=x", "FROM a"), 2),
            (("", "# syntax=x", "FROM a"), 2),
            (("FROM a", "# syntax=x"), 2),
            (("FROM a", "RUN echo \\", "# syntax=x", " && true"), 3),
        ],
    )
    def test_a_syntax_directive_is_refused_wherever_it_could_be_read(
        self, lines: tuple[str, ...], line: int
    ) -> None:
        result = refusal(dockerfile(*lines))

        assert result.line == line
        assert "syntax" in result.message

    @pytest.mark.parametrize(
        ("lines", "line"),
        [
            (("# escape=x", "FROM a"), 1),
            (("# escape=\\", "# escape=`", "FROM a"), 2),
            (("# note", "# escape=`", "FROM a"), 2),
            (("FROM a", "# escape=`"), 2),
        ],
    )
    def test_an_escape_directive_must_be_first_and_a_backslash_or_backtick(
        self, lines: tuple[str, ...], line: int
    ) -> None:
        assert refusal(dockerfile(*lines)).line == line


class TestRunRefusals:
    @pytest.mark.parametrize(
        "flag",
        [
            "--network=host",
            "--network=HOST",
            "--network=bridge",
            "--network=container:x",
            "--network=$N",
            "--network",
            "--security=insecure",
            "--security=sandbox",
            "--mount=type=ssh",
            "--mount=type=SSH,id=k",
            "--mount=type=secret,id=x",
            "--mount=target=/x,type=secret",
            "--mount='type=ssh'",
            '--mount="type=secret,id=x"',
            "--mount=type=cache,type=ssh",
            "--mount=type=bind,from=$X",
            "--mount=type=$T",
            "--mount=$K=v",
            "--mount=type=other",
            "--mount=cache",
            "--mount",
            "--device=x",
            "--NETWORK=host",
        ],
    )
    def test_a_refused_run_flag_is_refused_at_its_line(self, flag: str) -> None:
        result = refusal(dockerfile("FROM a", "RUN true", f"RUN {flag} true"))

        assert result.line == 3

    def test_the_line_is_where_the_instruction_starts_not_where_it_ends(self) -> None:
        content = dockerfile(
            "FROM a", "RUN echo a \\", " && echo b", "RUN --network=host \\", "  curl x"
        )

        assert refusal(content).line == 4

    def test_a_flag_after_a_comment_inside_a_continuation_is_still_found(self) -> None:
        content = dockerfile("FROM a", "RUN \\", "# note", "  --network=host true")

        assert refusal(content).line == 2

    def test_a_refused_flag_after_a_leading_unicode_space_is_found(self) -> None:
        content = dockerfile("FROM a", NO_BREAK_SPACE + "RUN --network=host true")

        assert refusal(content).line == 2

    def test_a_refused_flag_is_found_in_a_continued_flag_list(self) -> None:
        content = dockerfile(
            "FROM a", "RUN --mount=type=cache,target=/c \\", "  --security=insecure true"
        )

        assert refusal(content).line == 2

    def test_a_refused_flag_after_a_heredoc_is_found(self) -> None:
        content = dockerfile("FROM a", "RUN <<EOF", "x", "EOF", "RUN --network=host true")

        assert refusal(content).line == 5

    def test_a_refused_flag_after_an_unterminated_looking_marker_is_found(self) -> None:
        content = dockerfile("FROM a", "RUN cat<<EOF", "RUN --network=host true", "EOF")

        assert refusal(content).line == 3


class TestCopyRefusals:
    @pytest.mark.parametrize(
        "flag",
        ["--from=$S", "--from=${S}", "--from=", "--from", "--from=5", "--from=1", "--from='$S'"],
    )
    def test_a_from_that_cannot_be_read_statically_is_refused(self, flag: str) -> None:
        result = refusal(dockerfile("FROM a", f"COPY {flag} /x /y"))

        assert result.line == 2


class TestFlagLexing:
    @pytest.mark.parametrize(
        "lines",
        [
            pytest.param(
                (
                    "# escape=`",
                    "FROM a",
                    "RUN --mount=type=cache,target=/a\\ b --mount=type=bind,from=evil/img:1,"
                    "target=/m true",
                ),
                id="backslash-space-under-a-backtick-escape",
            ),
            pytest.param(
                (
                    "FROM a",
                    "RUN --mount=type=cache,target='/a\\' x' --mount=type=bind,"
                    "from=evil/img:1,target=/m true",
                ),
                id="backslash-inside-single-quotes",
            ),
            pytest.param(("FROM a", 'RUN --mount="type=cache,target=/c" true'), id="quoted-mount"),
            pytest.param(("FROM a", 'COPY --from="nginx:1" /x /y'), id="quoted-copy-from"),
            pytest.param(("FROM a", "COPY --chown=a`b x y"), id="backtick-in-copy"),
            pytest.param(("FROM a", "ADD --chown='a b' x y"), id="quote-in-add"),
            pytest.param(('FROM --platform="linux/amd64" a',), id="quote-in-from"),
            pytest.param(("FROM --platform=a\\b c",), id="backslash-in-from"),
        ],
    )
    def test_a_quote_backslash_or_backtick_in_the_flags_is_refused_at_its_line(
        self, lines: tuple[str, ...]
    ) -> None:
        result = refusal(dockerfile(*lines))

        assert result.line == len(lines)
        assert "flags" in result.message


class TestOnbuildRefusals:
    @pytest.mark.parametrize(
        ("lines", "line"),
        [
            (("FROM a", "ONBUILD RUN true"), 2),
            (("FROM alpine AS base", "ONBUILD COPY --from=evil/img:1 /x /y", "FROM base"), 2),
            (("FROM a", "onbuild run --network=host true"), 2),
            (("FROM a", "ONBUILD ONBUILD RUN true"), 2),
            (("ONBUILD RUN true", "FROM a"), 1),
        ],
    )
    def test_an_onbuild_instruction_is_refused_at_its_line(
        self, lines: tuple[str, ...], line: int
    ) -> None:
        result = refusal(dockerfile(*lines))

        assert result.line == line


class TestAddRefusals:
    @pytest.mark.parametrize(
        "instruction",
        [
            "ADD https://example.com/f.tgz /opt/",
            "ADD http://example.com/f.tgz /opt/",
            "ADD HTTPS://example.com/f.tgz /opt/",
            "ADD git://example.com/repo.git /src",
            "ADD https://example.com/repo.git#main /src",
            "ADD git@example.com:org/repo.git /src",
            "ADD ssh://git@example.com/org/repo.git /src",
            'ADD ["https://example.com/f.tgz", "/opt/"]',
            'ADD "https://example.com/f.tgz" /opt/',
            "ADD local.txt https://example.com/f.tgz /opt/",
            "ADD $SOURCE /opt/",
            "ADD --checksum= https://example.com/f.tgz /opt/",
        ],
    )
    def test_a_remote_or_variable_source_without_a_checksum_is_refused_at_its_line(
        self, instruction: str
    ) -> None:
        result = refusal(dockerfile("FROM a", instruction))

        assert result.line == 2
        assert "--checksum" in result.message


class TestFileLevelRefusals:
    def test_an_unknown_instruction_is_refused_at_its_line(self) -> None:
        assert refusal(dockerfile("FROM a", "RUNN true")).line == 2

    def test_a_file_without_from_is_refused_for_the_file(self) -> None:
        assert refusal(dockerfile("# only a comment", "ARG A=1")).line == 0

    def test_an_empty_file_is_refused_for_the_file(self) -> None:
        assert refusal(b"").line == 0

    def test_a_file_over_the_size_limit_is_refused_for_the_file(self) -> None:
        content = b"FROM a\n" + b"#" * MAX_DOCKERFILE_BYTES

        assert refusal(content).line == 0

    def test_a_file_at_the_size_limit_is_read(self) -> None:
        filler = b"#" * (MAX_DOCKERFILE_BYTES - len(b"FROM a\n") - 1)

        assert references(b"FROM a\n" + filler) == (("a",), ())

    def test_invalid_utf8_is_refused_at_the_line_of_the_bad_byte(self) -> None:
        assert refusal(b"FROM a\nRUN echo \xff\n").line == 2

    @pytest.mark.parametrize("char", ["\x00", "\x0b", "\x1b", "\x7f", "\r"])
    def test_a_control_character_is_refused_at_its_line(self, char: str) -> None:
        content = dockerfile("FROM a", f"RUN echo a{char}b")

        assert refusal(content).line == 2

    @pytest.mark.parametrize(
        ("lines", "line"),
        [
            (("FROM a", "RUN <<EOF", "x"), 2),
            (("FROM a", "RUN <<EOF", "x", "EOF2"), 2),
            (("FROM a", "RUN cat <<A <<B", "x", "A"), 2),
            (("FROM a", "RUN <<-EOF", "x", " EOF"), 2),
            (("FROM a", "RUN <<$X", "x", "$X"), 2),
        ],
    )
    def test_an_unterminated_or_unsupported_heredoc_is_refused_at_its_instruction(
        self, lines: tuple[str, ...], line: int
    ) -> None:
        assert refusal(dockerfile(*lines)).line == line

    def test_the_first_refusal_in_the_file_is_the_one_reported(self) -> None:
        content = dockerfile("FROM a", "RUN --security=insecure true", "RUN --network=host true")

        assert refusal(content).line == 2
