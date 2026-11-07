"""The product-written override that replaces a built service's `build` with its image."""

import pytest

from app.compose_cli.sources import ComposeSource, number_sources, override_document

BUILT = (("web", "shop-web"), ("api.v2", "shop-api.v2"))


def test_each_built_service_has_its_build_reset_and_its_image_set() -> None:
    document = override_document((), BUILT)

    assert document == (
        "services:\n"
        '  "web":\n'
        "    build: !reset null\n"
        '    image: "shop-web"\n'
        '  "api.v2":\n'
        "    build: !reset null\n"
        '    image: "shop-api.v2"\n'
    )


def test_built_services_and_file_sources_share_one_document() -> None:
    numbered = number_sources([ComposeSource("configs", "app", "x")])

    document = override_document(numbered, BUILT[:1])

    assert document.index("services:") < document.index("configs:")
    assert "    file: !reset null" in document


@pytest.mark.parametrize(
    "built",
    [
        pytest.param((("web\nbuild: x", "shop-web"),), id="newline-in-service"),
        pytest.param((("web", "Shop-Web"),), id="upper-case-image"),
        pytest.param((("web", "shop-web:latest"),), id="image-with-a-tag"),
        pytest.param((("web", "a b"),), id="space-in-image"),
        pytest.param((("web", "a"), ("web", "b")), id="service-twice"),
    ],
)
def test_a_name_outside_the_grammar_is_refused_before_it_reaches_the_file(
    built: tuple[tuple[str, str], ...],
) -> None:
    with pytest.raises(ValueError):
        override_document((), built)
