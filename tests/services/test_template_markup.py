"""Reducing untrusted catalogue text to safe HTML and to plain text."""

from collections.abc import Callable

import pytest
from markupsafe import Markup

from app.services.template_markup import (
    MAX_DEPTH,
    MAX_INPUT,
    MAX_OUTPUT,
    plain_text,
    safe_markup,
)

ESCAPED_QUOTE = "&#34;"
LINK_ATTRIBUTES = 'target="_blank" rel="noopener noreferrer nofollow"'
DROPPED_WITH_CONTENT = (
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
)
REJECTED_HREFS = (
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "  javascript:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "&#106;avascript:alert(1)",
    "java&colon;script:alert(1)",
    "javascript&colon;alert(1)",
    "java&Tab;script:alert(1)",
    'javascript:alert(1)" href="https://x.test',
    "data:text/html,x",
    "vbscript:x",
    "//evil.test/x",
    "/relative",
    "ftp://x.test/",
    "https:evil.test",
    "http:/x.test",
    "",
)


def link(href: str, text: str = "go") -> str:
    return f'<a href="{href}" {LINK_ATTRIBUTES}>{text}</a>'


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            '<b>Template created by…</b><br><a href="https://x.test/a?b=1&amp;c=2" '
            'target="_blank">…</a>',
            f"<b>Template created by…</b><br>{link('https://x.test/a?b=1&amp;c=2', '…')}",
            id="catalogue_example",
        ),
        pytest.param(
            "<strong>s</strong><i>i</i><em>e</em><code>c</code><pre>p</pre>",
            "<strong>s</strong><i>i</i><em>e</em><code>c</code><pre>p</pre>",
            id="inline_and_preformatted_kept",
        ),
        pytest.param(
            "<p>a</p><ul><li>b</li></ul><ol><li>c</li></ol>",
            "<p>a</p><ul><li>b</li></ul><ol><li>c</li></ol>",
            id="paragraphs_and_lists_kept",
        ),
        pytest.param("<h1>Title</h1>text", "<strong>Title</strong><br>text", id="heading_1"),
        pytest.param("a<h6>Small</h6>b", "a<strong>Small</strong><br>b", id="heading_6"),
        pytest.param("<h2>Open", "<strong>Open</strong><br>", id="unclosed_heading"),
        pytest.param(
            '<div><span>a</span><font color="red">b</font><center>c</center>'
            "<table><tr><td>d</td></tr></table></div>",
            "abcd",
            id="other_tags_lose_the_tag_and_keep_the_text",
        ),
        pytest.param(
            'a<img src="https://evil.test/x.png" onerror="alert(1)">b', "ab", id="image_removed"
        ),
        pytest.param("a<!-- <script> -->b", "ab", id="comment_removed"),
        pytest.param(
            "a<!-- x --!><script>alert(1)</script>b", "ab", id="comment_closed_by_bang_variant"
        ),
        pytest.param(
            "<a/href=https://x.test>go</a>", link("https://x.test"), id="slash_before_attribute"
        ),
        pytest.param(
            "<xmp><b>x</b><script>y</script></xmp>z",
            "&lt;b&gt;x&lt;/b&gt;&lt;script&gt;y&lt;/script&gt;z",
            id="xmp_content_stays_text",
        ),
        pytest.param(
            "<noembed><b>x</b></noembed>z", "&lt;b&gt;x&lt;/b&gt;z", id="noembed_content_stays_text"
        ),
        pytest.param("&#x202E;gpj.exe", "gpj.exe", id="encoded_bidi_override_removed"),
        pytest.param("a&#x200B;b&#x2028;c", "abc", id="encoded_zero_width_and_separator_removed"),
        pytest.param("a\u202eb", "ab", id="raw_bidi_override_removed"),
        pytest.param(
            '<a href="https://x.test/&#x202E;a">go</a>',
            link("https://x.test/a"),
            id="hidden_character_removed_from_href",
        ),
        pytest.param("<svg><svg>a</svg>b</svg>c", "c", id="nested_dropped_element"),
        pytest.param(
            "<script><script>a</script>b</script>c", "bc", id="script_ends_at_first_close"
        ),
        pytest.param("a<script>alert(1)", "a", id="unclosed_dropped_element_eats_the_rest"),
    ],
)
def test_safe_markup_keeps_only_the_allowed_formatting(source: str, expected: str) -> None:
    result = safe_markup(source)

    assert result == expected


@pytest.mark.parametrize("tag", DROPPED_WITH_CONTENT)
def test_safe_markup_drops_these_elements_with_their_content(tag: str) -> None:
    source = f"before<{tag}>hidden <b>bold</b></{tag}>after"

    result = safe_markup(source)

    assert result == "beforeafter"


@pytest.mark.parametrize("href", REJECTED_HREFS)
def test_safe_markup_removes_a_link_whose_target_is_not_http_or_https(href: str) -> None:
    source = f'<a href="{href}">text</a>'

    result = safe_markup(source)

    assert result == "text"


def test_safe_markup_removes_a_link_with_no_href() -> None:
    result = safe_markup("<a>text</a>")

    assert result == "text"


@pytest.mark.parametrize(
    ("href", "expected_href"),
    [
        pytest.param("https://x.test/", "https://x.test/", id="https"),
        pytest.param("http://x.test", "http://x.test", id="http"),
        pytest.param("HTTPS://Example.test/x", "HTTPS://Example.test/x", id="upper_case_scheme"),
        pytest.param("  https://x.test/\n", "https://x.test/", id="surrounding_whitespace"),
        pytest.param(
            'https://x.test" onmouseover="alert(1)',
            "https://x.test&#34;onmouseover=&#34;alert(1)",
            id="quotes_stay_inside_the_attribute",
        ),
    ],
)
def test_safe_markup_keeps_an_http_or_https_link_with_fixed_attributes(
    href: str, expected_href: str
) -> None:
    source = f"<a href='{href}'>go</a>"

    result = safe_markup(source)

    assert result == link(expected_href)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            '<a href="https://x.test" onclick="alert(1)" style="x" id="y" target="_self" '
            'rel="opener">go</a>',
            link("https://x.test"),
            id="link_attributes",
        ),
        pytest.param("<b onmouseover=alert(1)>t</b>", "<b>t</b>", id="inline_attributes"),
        pytest.param(
            '<p class="x" style="y" onclick="z">t</p>', "<p>t</p>", id="paragraph_attributes"
        ),
    ],
)
def test_safe_markup_discards_every_attribute_but_a_link_target(source: str, expected: str) -> None:
    result = safe_markup(source)

    assert result == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("<b><i>x", "<b><i>x</i></b>", id="unclosed_tags_closed_in_order"),
        pytest.param("<b>x</i>y</b>", "<b>xy</b>", id="stray_closing_tag_ignored"),
        pytest.param("</b>stray", "stray", id="closing_tag_with_no_opener"),
        pytest.param("<b><i>x</b>y", "<b><i>x</i></b>y", id="closing_an_outer_tag_closes_inner"),
        pytest.param("<br/>a<br>b", "<br>a<br>b", id="line_breaks"),
        pytest.param("</br>a", "a", id="closing_line_break_ignored"),
        pytest.param(
            '<a href="https://x.test">a<a href="https://y.test">b</a>c</a>',
            f"{link('https://x.test', 'ab')}c",
            id="nested_link_dropped",
        ),
    ],
)
def test_safe_markup_balances_nesting(source: str, expected: str) -> None:
    result = safe_markup(source)

    assert result == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "&lt;script&gt;alert(1)&lt;/script&gt;",
            "&lt;script&gt;alert(1)&lt;/script&gt;",
            id="escaped_script_stays_text",
        ),
        pytest.param("a &amp;lt; b", "a &amp;lt; b", id="decoded_once_and_escaped_once"),
        pytest.param("Tom &amp; Jerry", "Tom &amp; Jerry", id="ampersand"),
        pytest.param("a </script> b", "a  b", id="stray_script_close_in_text"),
        pytest.param("1 < 2 && 3 > 2", "1 &lt; 2 &amp;&amp; 3 &gt; 2", id="bare_angle_brackets"),
        pytest.param('say "hi"', "say &#34;hi&#34;", id="quotes"),
    ],
)
def test_safe_markup_escapes_text_exactly_once(source: str, expected: str) -> None:
    result = safe_markup(source)

    assert result == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("one\ntwo", "one<br>two", id="newline"),
        pytest.param("one\r\ntwo\rthree", "one<br>two<br>three", id="carriage_returns"),
        pytest.param("a < b\nc", "a &lt; b<br>c", id="angle_bracket_that_is_not_a_tag"),
        pytest.param("<b>one</b>\ntwo", "<b>one</b>\ntwo", id="newline_kept_as_is_with_markup"),
        pytest.param("", "", id="empty"),
        pytest.param("  \n ", "", id="blank"),
    ],
)
def test_safe_markup_keeps_line_breaks_only_for_input_without_tags(
    source: str, expected: str
) -> None:
    result = safe_markup(source)

    assert result == expected


@pytest.mark.parametrize("reduce", [safe_markup, plain_text])
def test_a_lone_surrogate_never_reaches_the_output(reduce: Callable[[str], str]) -> None:
    source = "\ud800<b>x</b>\udfff"

    result = reduce(source)

    assert result.encode("utf-8")


def test_safe_markup_treats_none_as_empty() -> None:
    result = safe_markup(None)

    assert result == ""


def test_safe_markup_returns_markup_so_templates_do_not_escape_it_again() -> None:
    result = safe_markup("<b>x</b>")

    assert isinstance(result, Markup)


def test_safe_markup_limits_nesting_depth_and_still_balances_the_tags() -> None:
    source = "<b>" * (MAX_DEPTH * 3) + "x"

    result = safe_markup(source)

    assert result.count("<b>") == MAX_DEPTH
    assert result.count("</b>") == MAX_DEPTH


def test_safe_markup_truncates_long_input_to_the_input_limit() -> None:
    source = "a" * (MAX_INPUT * 3)

    result = safe_markup(source)

    assert result == "a" * MAX_INPUT


def test_safe_markup_drops_a_tag_cut_by_the_input_limit() -> None:
    source = "x" * (MAX_INPUT - 3) + '<a href="https://x.test">link</a>'

    result = safe_markup(source)

    assert result == "x" * (MAX_INPUT - 3)


def test_safe_markup_bounds_the_output_when_escaping_expands_the_text() -> None:
    source = '"' * MAX_INPUT

    result = safe_markup(source)

    assert result.startswith(ESCAPED_QUOTE)
    assert len(result) <= MAX_OUTPUT


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            '<b>Template</b> created<br>by <a href="https://x.test">me</a>',
            "Template created by me",
            id="tags_stripped",
        ),
        pytest.param("<b>Bo</b>ld", "Bold", id="inline_tag_does_not_split_a_word"),
        pytest.param("<p>a</p><p>b</p>", "a b", id="paragraphs_separated"),
        pytest.param("<ul><li>a</li><li>b</li></ul>", "a b", id="list_items_separated"),
        pytest.param("<script>x</script>y", "y", id="dropped_element_content_gone"),
        pytest.param("<img src=x onerror=alert(1)>", "", id="image_gone"),
        pytest.param("&lt;b&gt; &amp; x", "<b> & x", id="entities_decoded"),
        pytest.param("  a \n\t b  ", "a b", id="whitespace_collapsed"),
        pytest.param("&#x202E;gpj&#x200B;.exe", "gpj.exe", id="hidden_characters_removed"),
        pytest.param("", "", id="empty"),
    ],
)
def test_plain_text_strips_tags_decodes_entities_and_collapses_whitespace(
    source: str, expected: str
) -> None:
    result = plain_text(source)

    assert result == expected


def test_plain_text_treats_none_as_empty() -> None:
    result = plain_text(None)

    assert result == ""
