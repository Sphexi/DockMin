"""A small element tree over rendered HTML, with the queries the accessibility tests need."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
NON_CONTROL_INPUTS = frozenset({"hidden", "submit", "button", "image", "reset"})
NOT_RENDERED = frozenset({"script", "style", "template"})


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str]
    parent: "Node | None" = None
    children: list["Node | str"] = field(default_factory=list)

    def walk(self) -> Iterator["Node"]:
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def ancestors(self) -> Iterator["Node"]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def classes(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())

    def text(self, *, skip_hidden: bool = True) -> str:
        """Return the text a screen reader would read: aria-hidden subtrees are left out."""
        if skip_hidden and (self.attrs.get("aria-hidden") == "true" or self.tag in NOT_RENDERED):
            return ""
        parts = [
            child if isinstance(child, str) else child.text(skip_hidden=skip_hidden)
            for child in self.children
        ]
        return " ".join("".join(parts).split())


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {})
        self._open = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {name: value or "" for name, value in attrs}, self._open)
        self._open.children.append(node)
        if tag not in VOID:
            self._open = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open.children.append(
            Node(tag, {name: value or "" for name, value in attrs}, self._open)
        )

    def handle_endtag(self, tag: str) -> None:
        node = self._open
        while node is not self.root and node.tag != tag:
            node = node.parent or self.root
        if node is not self.root:
            self._open = node.parent or self.root

    def handle_data(self, data: str) -> None:
        self._open.children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    return builder.root


def rendered_nodes(root: Node) -> Iterator[Node]:
    """Yield every element that is on the page, leaving out template content and scripts."""
    return (
        node
        for node in root.walk()
        if not any(a.tag in NOT_RENDERED for a in (node, *node.ancestors()))
    )


def by_id(root: Node) -> dict[str, Node]:
    return {node.attrs["id"]: node for node in root.walk() if "id" in node.attrs}


def accessible_name(node: Node, ids: dict[str, Node]) -> str:
    """Return the name from aria-labelledby, aria-label, a value attribute or content."""
    referenced = node.attrs.get("aria-labelledby", "").split()
    if referenced:
        return " ".join(ids[i].text(skip_hidden=False) for i in referenced if i in ids).strip()
    if node.attrs.get("aria-label", "").strip():
        return node.attrs["aria-label"].strip()
    if node.tag == "input" and node.attrs.get("type") in ("submit", "button", "reset"):
        return node.attrs.get("value", "").strip()
    return node.text()


def heading_levels(root: Node) -> list[int]:
    return [int(n.tag[1]) for n in rendered_nodes(root) if n.tag in HEADINGS]


def is_labelled_control(node: Node, root: Node, ids: dict[str, Node]) -> bool:
    if node.attrs.get("aria-label", "").strip() or node.attrs.get("aria-labelledby", "").strip():
        return True
    if any(a.tag == "label" for a in node.ancestors()):
        return True
    own = node.attrs.get("id")
    return bool(own) and any(
        n.tag == "label" and n.attrs.get("for") == own and n.text(skip_hidden=False)
        for n in root.walk()
    )


def labelled_controls(root: Node) -> Iterator[Node]:
    """Yield every input, select and textarea that takes user input."""
    for node in rendered_nodes(root):
        if node.tag in ("select", "textarea") or (
            node.tag == "input" and node.attrs.get("type", "text") not in NON_CONTROL_INPUTS
        ):
            yield node


TIMESTAMP_TEXT = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})? UTC")


def text_nodes(node: Node) -> Iterator[tuple[str, Node]]:
    for child in node.children:
        if isinstance(child, str):
            yield child, node
        else:
            yield from text_nodes(child)


def is_aria_hidden(node: Node) -> bool:
    return any(n.attrs.get("aria-hidden") == "true" for n in (node, *node.ancestors()))


class Page:
    """A rendered response parsed once, with the queries the page checks share."""

    def __init__(self, status: int, html: str) -> None:
        self.status = status
        self.html = html
        self.root = parse(html)
        self.ids = by_id(self.root)
        self.nodes = list(rendered_nodes(self.root))

    def tags(self, *names: str) -> Iterator[Node]:
        return (node for node in self.nodes if node.tag in names)
