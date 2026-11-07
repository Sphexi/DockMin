"""The presentation catalogue behind the one registry form."""

import pytest

from app.registry_client.providers import PROVIDERS, ProviderKind
from app.web.registry_providers import CATALOGUE

BOOLEAN_FIELDS = {"use_organisation"}
NUMERIC_FIELDS = {"project_id"}
URL_FIELDS = {"instance_url"}


def test_the_catalogue_describes_exactly_the_providers_the_product_has() -> None:
    assert set(CATALOGUE) == set(PROVIDERS)
    assert {presentation.kind for presentation in CATALOGUE.values()} == set(PROVIDERS)


@pytest.mark.parametrize("kind", sorted(PROVIDERS))
def test_a_providers_extra_fields_are_exactly_the_fields_of_its_model(kind: ProviderKind) -> None:
    shown = [extra.name for extra in CATALOGUE[kind].extras]

    assert shown == list(PROVIDERS[kind].extras_model.model_fields)


@pytest.mark.parametrize("kind", sorted(PROVIDERS))
def test_every_label_and_help_text_is_written(kind: ProviderKind) -> None:
    presentation = CATALOGUE[kind]
    texts = [
        presentation.label,
        presentation.summary,
        presentation.address_label,
        presentation.address_help,
        presentation.username_label,
        presentation.username_help,
        presentation.secret_label,
        presentation.secret_help,
        *(extra.label for extra in presentation.extras),
        *(extra.help for extra in presentation.extras),
    ]

    assert [text for text in texts if not text.strip()] == []


def test_each_extra_uses_the_input_kind_its_type_needs() -> None:
    inputs = {
        extra.name: extra.input
        for presentation in CATALOGUE.values()
        for extra in presentation.extras
    }

    assert {name for name, kind in inputs.items() if kind == "checkbox"} == BOOLEAN_FIELDS
    assert {name for name, kind in inputs.items() if kind == "number"} == NUMERIC_FIELDS
    assert {name for name, kind in inputs.items() if kind == "url"} == URL_FIELDS


def test_the_only_required_extra_is_the_ecr_region() -> None:
    required = {
        (kind, extra.name)
        for kind, presentation in CATALOGUE.items()
        for extra in presentation.extras
        if extra.required
    }

    assert required == {("ecr", "region")}


def test_ecr_is_described_with_the_key_pair_it_takes() -> None:
    ecr = CATALOGUE["ecr"]

    assert (ecr.username_label, ecr.secret_label) == ("Access key ID", "Secret access key")
