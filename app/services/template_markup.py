"""Sanitiser that turns untrusted catalogue text into safe HTML or plain text."""

import re
import unicodedata
from abc import ABC, abstractmethod
from html.parser import HTMLParser
from typing import Final

from markupsafe import Markup, escape

MAX_INPUT: Final = 10_000
MAX_OUTPUT: Final = 40_000
MAX_DEPTH: Final = 16
MAX_HREF: Final = 2048

_KEPT: Final = frozenset({"b", "strong", "i", "em", "p", "ul", "ol", "li", "code", "pre"})
_HEADINGS: Final = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_DROPPED_WITH_CONTENT: Final = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "template",
        "noscript",
        "svg",
        "math",
        "form",
        "select",
        "textarea",
    }
)
_WORD_BREAKING: Final = frozenset(
    {"br", "p", "ul", "ol", "li", "pre", "div", "table", "tr", "td", "th", "blockquote", "hr"}
    | _HEADINGS
)

_HIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_KEPT_CONTROLS: Final = frozenset("\n\r\t")

_TAG_HINT: Final = re.compile(r"</?[A-Za-z!?]")
_URL_SPACE: Final = re.compile(r"[\x00-\x20\x7f]")
_WEB_URL: Final = re.compile(r"https?://[^/?#]", re.IGNORECASE)
_LINK_ATTRIBUTES: Final = 'target="_blank" rel="noopener noreferrer nofollow"'


def _visible(text: str) -> str:
    """Remove control, format, surrogate and line-separator characters, keeping newline and tab."""
    return "".join(
        character
        for character in text
        if character in _KEPT_CONTROLS or unicodedata.category(character) not in _HIDDEN_CATEGORIES
    )


def _bounded(text: str) -> str:
    """Cut text to MAX_INPUT without leaving a tag half-written at the end."""
    text = _visible(text).strip()
    if len(text) <= MAX_INPUT:
        return text
    cut = text[:MAX_INPUT]
    if cut.rfind("<") > cut.rfind(">"):
        cut = cut[: cut.rfind("<")]
    return cut


def _web_href(attrs: list[tuple[str, str | None]]) -> str | None:
    """Return the first href if it is an http or https URL, stripped of whitespace and controls."""
    for name, value in attrs:
        if name == "href":
            href = _URL_SPACE.sub("", _visible(value or ""))
            if len(href) <= MAX_HREF and _WEB_URL.match(href):
                return href
            return None
    return None


class _Reader(HTMLParser, ABC):
    """Walks the markup, skipping dropped elements with their content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skipping: str | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skipping is not None:
            if tag == self._skipping:
                self._skip_depth += 1
            return
        if tag in _DROPPED_WITH_CONTENT:
            self._skipping = tag
            self._skip_depth = 1
            return
        self.start(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._skipping is not None:
            if tag == self._skipping:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skipping = None
            return
        self.end(tag)

    def handle_data(self, data: str) -> None:
        if self._skipping is None:
            self.text(_visible(data))

    @abstractmethod
    def start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None: ...

    @abstractmethod
    def end(self, tag: str) -> None: ...

    @abstractmethod
    def text(self, data: str) -> None: ...


class _Sanitiser(_Reader):
    def __init__(self, *, line_breaks: bool) -> None:
        super().__init__()
        self._line_breaks = line_breaks
        self._parts: list[str] = []
        self._size = 0
        self._full = False
        self._open: list[tuple[str, str]] = []

    def _emit(self, markup: str) -> bool:
        if self._full or self._size + len(markup) > MAX_OUTPUT:
            self._full = True
            return False
        self._parts.append(markup)
        self._size += len(markup)
        return True

    def _emit_text(self, text: str) -> None:
        if self._full:
            return
        escaped = str(escape(text))
        room = MAX_OUTPUT - self._size
        if len(escaped) > room:
            escaped = escaped[:room]
            ampersand = escaped.rfind("&")
            if ampersand > escaped.rfind(";"):
                escaped = escaped[:ampersand]
            self._full = True
        self._parts.append(escaped)
        self._size += len(escaped)

    def start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._emit("<br>")
            return
        if len(self._open) >= MAX_DEPTH:
            return
        if tag == "a":
            href = _web_href(attrs)
            if href is None or any(name == "a" for name, _ in self._open):
                return
            if self._emit(f'<a href="{escape(href)}" {_LINK_ATTRIBUTES}>'):
                self._open.append((tag, "</a>"))
        elif tag in _HEADINGS:
            if self._emit("<strong>"):
                self._open.append((tag, "</strong><br>"))
        elif tag in _KEPT and self._emit(f"<{tag}>"):
            self._open.append((tag, f"</{tag}>"))

    def end(self, tag: str) -> None:
        names = [name for name, _ in self._open]
        if tag not in names:
            return
        keep = len(names) - 1 - names[::-1].index(tag)
        closers = [closer for _, closer in reversed(self._open[keep:])]
        del self._open[keep:]
        self._parts.extend(closers)

    def text(self, data: str) -> None:
        if self._line_breaks:
            lines = data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            for index, line in enumerate(lines):
                if index:
                    self._emit("<br>")
                self._emit_text(line)
        else:
            self._emit_text(data)

    def result(self) -> Markup:
        self.close()
        closers = [closer for _, closer in reversed(self._open)]
        self._open.clear()
        return Markup("".join([*self._parts, *closers]))


class _PlainText(_Reader):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _WORD_BREAKING:
            self._parts.append(" ")

    def end(self, tag: str) -> None:
        if tag in _WORD_BREAKING:
            self._parts.append(" ")

    def text(self, data: str) -> None:
        self._parts.append(data)

    def result(self) -> str:
        self.close()
        return " ".join("".join(self._parts).split())


def safe_markup(text: str | None) -> Markup:
    """Return the allowed formatting of untrusted text as HTML that is safe to render as is.

    Input with no tag keeps its line breaks; input with any tag treats newlines as whitespace.
    """
    bounded = _bounded(text or "")
    sanitiser = _Sanitiser(line_breaks=_TAG_HINT.search(bounded) is None)
    sanitiser.feed(bounded)
    return sanitiser.result()


def plain_text(text: str | None) -> str:
    """Return untrusted text with its tags removed, entities decoded and whitespace collapsed."""
    reader = _PlainText()
    reader.feed(_bounded(text or ""))
    return reader.result()
