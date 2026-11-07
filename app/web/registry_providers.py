"""Labels, help text and extra fields the registry pages show for each provider."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

ExtraInput = Literal["text", "url", "number", "checkbox"]


@dataclass(frozen=True, slots=True)
class ExtraField:
    """One provider-specific setting; `name` is both the form field and the stored extras key."""

    name: str
    label: str
    help: str
    input: ExtraInput = "text"
    required: bool = False


@dataclass(frozen=True, slots=True)
class ProviderPresentation:
    """How one provider is described and labelled on the form and the detail page."""

    kind: str
    label: str
    summary: str
    address_label: str
    address_help: str
    username_label: str
    username_help: str
    secret_label: str
    secret_help: str
    extras: tuple[ExtraField, ...] = ()


_ADDRESS_FORM = (
    "A host name with an optional port, such as {example}. An https:// prefix is removed."
)

CATALOGUE: Final[Mapping[str, ProviderPresentation]] = {
    "dockerhub": ProviderPresentation(
        kind="dockerhub",
        label="Docker Hub",
        summary="Images from docker.io. Anonymous pulls are subject to Docker Hub rate limits.",
        address_label="Registry address",
        address_help="Fixed at docker.io.",
        username_label="Docker Hub user name",
        username_help="The account that owns the access token.",
        secret_label="Access token",
        secret_help="A personal access token. Do not use the account password.",
    ),
    "ghcr": ProviderPresentation(
        kind="ghcr",
        label="GitHub Container Registry",
        summary="Images from ghcr.io.",
        address_label="Registry address",
        address_help="Fixed at ghcr.io.",
        username_label="GitHub user name",
        username_help="The account that owns the token.",
        secret_label="Personal access token",
        secret_help="A classic personal access token with the read:packages scope.",
        extras=(
            ExtraField(
                "organisation",
                "GitHub organisation",
                "Optional. The organisation whose packages this set is for.",
            ),
            ExtraField(
                "use_organisation",
                "Pull images under the organisation",
                "Requires an organisation.",
                "checkbox",
            ),
        ),
    ),
    "acr": ProviderPresentation(
        kind="acr",
        label="Azure Container Registry",
        summary="A registry hosted on Azure.",
        address_label="Registry address",
        address_help=_ADDRESS_FORM.format(example="myregistry.azurecr.io"),
        username_label="Token name or service principal ID",
        username_help="The identity that holds pull permission on the registry.",
        secret_label="Token password or service principal secret",
        secret_help="The credential that belongs to the identity above.",
    ),
    "gitlab": ProviderPresentation(
        kind="gitlab",
        label="GitLab Container Registry",
        summary=(
            "The registry of a GitLab instance. "
            "Change the address and instance URL for a self-hosted instance."
        ),
        address_label="Registry address",
        address_help=_ADDRESS_FORM.format(example="registry.gitlab.com"),
        username_label="User name or deploy token name",
        username_help="The account or deploy token that holds the credential.",
        secret_label="Personal access token or deploy token",
        secret_help="A token with the read_registry scope.",
        extras=(
            ExtraField(
                "instance_url",
                "GitLab instance URL",
                "The https URL of the GitLab instance, such as https://gitlab.com.",
                "url",
            ),
            ExtraField(
                "project_id",
                "Project ID",
                "Optional. The numeric ID of the GitLab project.",
                "number",
            ),
            ExtraField(
                "project_path",
                "Project path",
                "Optional. The group and project, such as group/subgroup/project.",
            ),
        ),
    ),
    "ecr": ProviderPresentation(
        kind="ecr",
        label="Amazon ECR",
        summary=(
            "A private registry on AWS. The access key is exchanged for a registry token that "
            "expires after 12 hours and is refreshed when it is next needed."
        ),
        address_label="Registry address",
        address_help=(
            "Of the form 123456789012.dkr.ecr.us-east-1.amazonaws.com. "
            "The region must match the AWS region below."
        ),
        username_label="Access key ID",
        username_help="A static IAM access key. Temporary keys are not supported.",
        secret_label="Secret access key",
        secret_help="The secret that belongs to the access key ID.",
        extras=(
            ExtraField(
                "region",
                "AWS region",
                "The region of the registry, such as us-east-1.",
                required=True,
            ),
        ),
    ),
    "custom": ProviderPresentation(
        kind="custom",
        label="Custom registry",
        summary="Any registry that speaks the Docker registry API over HTTPS.",
        address_label="Registry address",
        address_help=_ADDRESS_FORM.format(example="registry.example.com:5000")
        + " Plain HTTP registries are not supported.",
        username_label="User name",
        username_help="The account on the registry.",
        secret_label="Password or token",
        secret_help="The credential that belongs to the user name.",
    ),
}
