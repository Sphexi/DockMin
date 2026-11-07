"""Normalisation of SQL text so two spellings of the same statement compare equal."""

import re

_WHITESPACE = " \t\n\f\r"
_QUOTES = "'\"`"
_LINE_COMMENT_END = re.compile(r"[\n\x00]")


def _quoted_end(sql: str, start: int, quote: str) -> int:
    position = start + 1
    while True:
        found = sql.find(quote, position)
        if found < 0:
            return len(sql)
        if sql.startswith(quote, found + 1):
            position = found + 2
            continue
        return found + 1


def normalise_sql(sql: str) -> str:
    """Return sql without comments, with whitespace runs outside quoting collapsed to one space."""
    parts: list[str] = []
    length = len(sql)
    position = 0
    pending_space = False
    while position < length:
        char = sql[position]
        if sql.startswith("--", position):
            end_of_comment = _LINE_COMMENT_END.search(sql, position)
            position = length if end_of_comment is None else end_of_comment.start()
            pending_space = True
        elif sql.startswith("/*", position):
            close = sql.find("*/", position + 2)
            position = length if close < 0 else close + 2
            pending_space = True
        elif char in _WHITESPACE:
            position += 1
            pending_space = True
        else:
            if pending_space and parts:
                parts.append(" ")
            pending_space = False
            if char in _QUOTES:
                end = _quoted_end(sql, position, char)
            elif char == "[":
                close = sql.find("]", position + 1)
                end = length if close < 0 else close + 1
            else:
                end = position + 1
            parts.append(sql[position:end])
            position = end
    return "".join(parts)
