"""Every destructive confirmation names its targets, keeps force apart, and works as a link."""

import re
from functools import cache
from typing import Any, cast

import pytest

from app.web.templating import TEMPLATES_DIR
from tests.web.helpers import TOKEN, render_macro, strip_jinja_comments
from tests.web.html_tree import Node, Page, accessible_name
from tests.web.page_world import IDLE_VOLUME, Key, world_pages  # noqa: F401

pytestmark = pytest.mark.xdist_group("page-world")

TARGETS_PARTIAL = "partials/confirm_targets.html"
COMPONENT_GALLERY = "ui_kit.html"
EXTRA_CONFIRMATIONS = frozenset({"containers/_batch.html"})
REFERENCE = re.compile(r"""\{%-?\s*(?:from|import|include|extends)\s+["']([^"']+)["']""")
TARGETS_CALL = re.compile(r"(?<![\w.])(?<!macro )confirm_targets\(")
DIALOG_CALL = re.compile(r"(?<![\w.])(?<!macro )confirmation_dialog\(")
FORCE_NAMES = frozenset({"force", "confirm_force", "volumes", "remove_volumes", "remove_orphans"})
YES = "yes"

ORDINARY_PAGES = [
    "container-stop-confirm",
    "container-remove-confirm",
    "container-batch-confirm",
    "image-remove-confirm",
    "image-untag-confirm",
    "volume-remove-confirm",
    "network-remove-confirm",
]
FORCE_PAGES = [
    pytest.param("container-remove-force-confirm", "confirm_force", id="container"),
    pytest.param("container-batch-force-confirm", "confirm_force", id="container-batch"),
    pytest.param("image-remove-force-confirm", "force", id="image"),
    pytest.param("volume-remove-force-confirm", "force", id="volume"),
]
CONFIRMATION_PAGES = ORDINARY_PAGES + [cast(str, param.values[0]) for param in FORCE_PAGES]


@cache
def _source(name: str) -> str:
    return strip_jinja_comments((TEMPLATES_DIR / name).read_text(encoding="utf-8"))


def _all_templates() -> list[str]:
    return sorted(
        path.relative_to(TEMPLATES_DIR).as_posix() for path in TEMPLATES_DIR.rglob("*.html")
    )


def _closure(name: str) -> set[str]:
    reached: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(REFERENCE.findall(_source(current)))
    return reached


def _renders_targets(name: str) -> bool:
    return any(
        TARGETS_CALL.search(_source(member))
        for member in _closure(name)
        if member != TARGETS_PARTIAL
    )


def _is_confirmation(name: str) -> bool:
    if name.startswith("partials/"):
        return False
    source = _source(name)
    return (
        "confirm" in name.rsplit("/", 1)[-1]
        or "<dialog" in source
        or bool(DIALOG_CALL.search(source))
        or name in EXTRA_CONFIRMATIONS
    )


CONFIRMATIONS = [name for name in _all_templates() if _is_confirmation(name)]
FORCE_TEMPLATES = [
    name
    for name in _all_templates()
    if re.search(r"_(?:force|orphans|volumes)_confirm\.html$", name)
]


def _ordinary_partner(force_template: str) -> str:
    directory, _, leaf = force_template.rpartition("/")
    leaf = leaf.replace("_force", "").replace("_orphans", "").replace("_volumes", "")
    return f"{directory}/{leaf}"


def test_the_scanners_find_confirmations_of_every_kind() -> None:
    assert len(CONFIRMATIONS) > 40
    assert {
        "containers/action_confirm.html",
        "networks/_attach.html",
        "containers/_batch.html",
    } <= set(CONFIRMATIONS)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("auth/login.html", False),
        ("partials/confirmation_dialog.html", True),
        ("containers/action_confirm.html", True),
    ],
)
def test_target_scanner_follows_imports_to_the_call(name: str, expected: bool) -> None:
    assert _renders_targets(name) is expected


@pytest.mark.parametrize("name", CONFIRMATIONS)
def test_confirmation_template_renders_its_targets_through_confirm_targets(name: str) -> None:
    assert _renders_targets(name)


@pytest.mark.parametrize("name", [n for n in _all_templates() if DIALOG_CALL.search(_source(n))])
def test_every_confirmation_dialog_call_passes_the_targets_it_names(name: str) -> None:
    calls = re.findall(r"confirmation_dialog\((?:[^()]|\([^()]*\))*\)", _source(name))

    unnamed = [call for call in calls if "targets=" not in call]

    assert name == COMPONENT_GALLERY or unnamed == []


@pytest.mark.parametrize("name", FORCE_TEMPLATES)
def test_force_variant_is_a_template_apart_from_its_ordinary_confirmation(name: str) -> None:
    partner = _ordinary_partner(name)

    assert (TEMPLATES_DIR / partner).is_file()
    assert partner != name


@pytest.mark.parametrize("name", sorted({_ordinary_partner(n) for n in FORCE_TEMPLATES}))
def test_ordinary_confirmation_template_names_no_force_input(name: str) -> None:
    posted = re.findall(r"""name=["']([\w-]+)["']""", _source(name))

    assert FORCE_NAMES.isdisjoint(posted)


def _forms(page: Page) -> list[Node]:
    return [n for n in page.tags("form") if n.attrs.get("method", "get").lower() == "post"]


def _input_names(page: Page) -> set[str]:
    return {
        field.attrs["name"]
        for form in _forms(page)
        for field in form.walk()
        if field.tag == "input" and "name" in field.attrs
    }


def test_the_world_renders_every_confirmation_page(pages: dict[Key, Page]) -> None:
    statuses = {label: pages[("admin", label)].status for label in CONFIRMATION_PAGES}

    assert set(statuses.values()) == {200}


@pytest.mark.parametrize("label", ORDINARY_PAGES)
def test_ordinary_confirmation_page_posts_no_force_input(
    pages: dict[Key, Page], label: str
) -> None:
    page = pages[("admin", label)]

    assert FORCE_NAMES.isdisjoint(_input_names(page))


@pytest.mark.parametrize(("label", "flag"), FORCE_PAGES)
def test_force_confirmation_page_posts_its_flag_as_yes_under_a_force_heading(
    pages: dict[Key, Page], label: str, flag: str
) -> None:
    page = pages[("admin", label)]
    flags = [
        field.attrs.get("value")
        for form in _forms(page)
        for field in form.walk()
        if field.tag == "input" and field.attrs.get("name") == flag
    ]
    submits = [
        accessible_name(n, page.ids)
        for n in page.tags("button")
        if n.attrs.get("type") == "submit" and "btn--danger" in n.classes()
    ]
    headings = [n.text() for n in page.tags("h1", "h2")]

    assert flags == [YES]
    assert any(text.startswith("Force remove") for text in headings)
    assert submits
    assert all(text.startswith("Force remove") for text in submits)


@pytest.mark.parametrize("label", CONFIRMATION_PAGES)
def test_confirmation_page_lists_its_targets_through_the_shared_component(
    pages: dict[Key, Page], label: str
) -> None:
    page = pages[("admin", label)]

    components = [n for n in page.tags("div") if "confirm-targets" in n.classes()]

    assert components
    assert all(n.text() for n in components)
    assert all(any(d.tag == "li" for d in n.walk()) for n in components)


@pytest.mark.parametrize(
    ("label", "target"),
    [
        ("container-stop-confirm", "web"),
        ("container-remove-confirm", "job"),
        ("container-remove-force-confirm", "web"),
        ("container-batch-confirm", "job"),
        ("container-batch-force-confirm", "job"),
        ("network-remove-confirm", "shop_default"),
        ("volume-remove-force-confirm", IDLE_VOLUME),
    ],
)
def test_confirmation_page_names_the_exact_object_it_will_act_on(
    pages: dict[Key, Page], label: str, target: str
) -> None:
    page = pages[("admin", label)]

    names = [
        n.text()
        for component in page.tags("div")
        if "confirm-targets" in component.classes()
        for n in component.walk()
        if "mono" in n.classes()
    ]

    assert target in names


@pytest.mark.parametrize(
    ("targets", "noun", "more", "plural", "expected"),
    [
        (["web"], "container", 0, None, ["1 container", "web"]),
        (["web", "db"], "container", 0, None, ["2 containers", "web", "db"]),
        (["web"], "container", 3, None, ["4 containers", "and 3 more not shown"]),
        (["a"], "registry", 1, None, ["2 registries"]),
        (["a", "b"], "person", 0, "people", ["2 people"]),
        ([{"name": "web", "detail": "abc123"}], "container", 0, None, ["web", "abc123"]),
        (["<script>x</script>"], "item", 0, None, ["&lt;script&gt;"]),
    ],
)
def test_confirm_targets_renders_the_count_the_names_and_the_remainder(
    targets: list[object], noun: str, more: int, plural: str | None, expected: list[str]
) -> None:
    html = render_macro(
        "confirm_targets", "confirm_targets", targets=targets, noun=noun, more=more, plural=plural
    )

    assert all(fragment in html for fragment in expected)
    assert "<script>" not in html


OPENER = "remove-opener"
DIALOG = "remove-dialog"


def _dialog_markup(**extra: Any) -> Page:
    dialog = render_macro(
        "confirmation_dialog",
        "confirmation_dialog",
        dialog_id=DIALOG,
        title="Remove?",
        message="Removes",
        action="/remove",
        confirm_label="Remove",
        targets=["web"],
        noun="container",
        **extra,
    )
    return Page(200, dialog)


def test_confirm_button_is_a_link_to_the_dialog_that_script_upgrades() -> None:
    html = render_macro("confirmation_dialog", "confirm_button", dialog_id=DIALOG, label="Remove")
    link = next(Page(200, html).tags("a"))

    assert link.attrs["href"] == f"#{DIALOG}"
    assert link.attrs["id"] == f"{DIALOG}-opener"
    assert link.attrs["data-dialog-open"] == DIALOG
    assert link.attrs["aria-haspopup"] == "dialog"
    assert link.text() == "Remove"


def test_confirm_button_takes_the_opener_id_it_is_given() -> None:
    html = render_macro(
        "confirmation_dialog", "confirm_button", dialog_id=DIALOG, label="Remove", opener_id=OPENER
    )

    assert next(Page(200, html).tags("a")).attrs["id"] == OPENER


@pytest.mark.parametrize(
    ("opener_id", "expected"), [(None, f"#{DIALOG}-opener"), (OPENER, f"#{OPENER}")]
)
def test_dialog_cancel_is_a_link_back_to_the_opener(opener_id: str | None, expected: str) -> None:
    page = _dialog_markup(opener_id=opener_id)

    dialog = next(page.tags("dialog"))
    cancel = next(n for n in page.tags("a") if "data-dialog-close" in n.attrs)

    assert dialog.attrs["id"] == DIALOG
    assert cancel.attrs["href"] == expected
    assert cancel.text() == "Cancel"


def test_dialog_is_named_and_described_by_elements_inside_it() -> None:
    page = _dialog_markup()
    dialog = next(page.tags("dialog"))

    assert dialog.attrs["aria-labelledby"] in page.ids
    assert dialog.attrs["aria-describedby"] in page.ids
    assert page.ids[dialog.attrs["aria-labelledby"]].text() == "Remove?"


def test_dialog_form_posts_the_token_the_extra_fields_and_the_submit_label() -> None:
    page = _dialog_markup(hidden={"expected_digest": "abc"})

    fields = {n.attrs["name"]: n.attrs["value"] for n in page.tags("input")}
    submit = next(n for n in page.tags("button") if n.attrs.get("type") == "submit")

    assert fields == {"csrf_token": TOKEN, "expected_digest": "abc"}
    assert submit.text() == "Remove"


def _modal_dialogs(page: Page) -> list[Node]:
    return [
        n for n in page.tags("dialog") if "data-confirm-dialog" in n.attrs and "open" not in n.attrs
    ]


@pytest.mark.parametrize(("role", "label"), [("admin", "user-detail"), ("admin", "team-detail")])
def test_each_modal_dialog_on_a_rendered_page_has_an_opener_and_a_return_link(
    pages: dict[Key, Page], role: str, label: str
) -> None:
    page = pages[(role, label)]
    dialogs = _modal_dialogs(page)
    openers = {
        n.attrs["data-dialog-open"]: n for n in page.tags("a") if "data-dialog-open" in n.attrs
    }

    returns = [
        next(a for a in d.walk() if a.tag == "a" and "data-dialog-close" in a.attrs).attrs["href"]
        for d in dialogs
    ]

    assert dialogs
    assert all(d.attrs["id"] in openers for d in dialogs)
    assert {n.tag for n in openers.values()} == {"a"}
    assert all(openers[d.attrs["id"]].attrs["href"] == f"#{d.attrs['id']}" for d in dialogs)
    assert all(
        href.removeprefix("#") == openers[d.attrs["id"]].attrs["id"]
        for href, d in zip(returns, dialogs, strict=True)
    )
