"""SigV4 signing against the published test suite and a botocore-generated ECR oracle."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from app.registry_client import sigv4
from tests.registry_client.signing_data import ORACLE, SUITE

ECR_TARGET = "AmazonEC2ContainerRegistry_V20150921.GetAuthorizationToken"
ECR_HEADERS = {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": ECR_TARGET}
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
MOMENT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def parse_stamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, sigv4.AMZ_DATE_FORMAT).replace(tzinfo=UTC)


def sign_vector(vector: dict[str, Any]) -> dict[str, str]:
    return sigv4.sign_request(
        method=vector["method"],
        host=vector["host"],
        path=vector["path"],
        query=vector["query"],
        headers=vector["headers"],
        body=vector["body"].encode(),
        access_key_id=vector["access_key_id"],
        secret_access_key=SecretStr(vector["secret_access_key"]),
        region=vector["region"],
        service=vector["service"],
        moment=parse_stamp(vector["timestamp"].replace("-", "").replace(":", "")),
        content_sha256_header=vector["sign_body"],
    )


def sign_ecr(
    region: str,
    *,
    host: str | None = None,
    secret: str = SECRET,
    moment: datetime = MOMENT,
    **overrides: Any,
) -> dict[str, str]:
    arguments: dict[str, Any] = {
        "method": "POST",
        "host": host or f"api.ecr.{region}.amazonaws.com",
        "path": "/",
        "headers": ECR_HEADERS,
        "body": b"{}",
        "access_key_id": ACCESS_KEY,
        "secret_access_key": SecretStr(secret),
        "region": region,
        "service": "ecr",
        "moment": moment,
    }
    return sigv4.sign_request(**{**arguments, **overrides})


def test_the_published_suite_is_loaded_in_full() -> None:
    assert len(SUITE) == 26


@pytest.mark.parametrize("vector", SUITE, ids=[v["name"] for v in SUITE])
def test_the_authorization_header_matches_the_published_suite(vector: dict[str, Any]) -> None:
    signed = sign_vector(vector)

    assert signed["Authorization"] == vector["authorization"].strip()


@pytest.mark.parametrize("vector", SUITE, ids=[v["name"] for v in SUITE])
def test_the_canonical_request_and_string_to_sign_match_the_published_suite(
    vector: dict[str, Any],
) -> None:
    signed = sign_vector(vector)
    headers = {k: v for k, v in signed.items() if k != "Authorization"}

    canonical = sigv4.canonical_request(
        method=vector["method"],
        path=vector["path"],
        query=vector["query"],
        headers={**headers, "Host": vector["host"]},
        payload_hash=sigv4.sha256_hex(vector["body"].encode()),
    )
    scope = f"{signed['X-Amz-Date'][:8]}/{vector['region']}/{vector['service']}/aws4_request"
    to_sign = sigv4.string_to_sign(canonical.text, signed["X-Amz-Date"], scope)

    assert canonical.text == vector["canonical_request"]
    assert to_sign == vector["string_to_sign"]


def test_the_signing_key_matches_botocore_for_the_documented_derivation_inputs() -> None:
    derivation = ORACLE["derivation"]

    key = sigv4.signing_key(
        derivation["secret_access_key"],
        derivation["date"],
        derivation["region"],
        derivation["service"],
    )

    assert key.hex() == derivation["signing_key_hex"]


@pytest.mark.parametrize("case", ORACLE["cases"], ids=[c["region"] for c in ORACLE["cases"]])
def test_an_ecr_request_is_signed_exactly_as_botocore_signs_it(case: dict[str, Any]) -> None:
    signed = sign_ecr(case["region"], host=case["host"], moment=parse_stamp(case["timestamp"]))

    assert signed["Authorization"] == case["authorization"]
    assert signed["X-Amz-Date"] == case["timestamp"]


@pytest.mark.parametrize("case", ORACLE["cases"], ids=[c["region"] for c in ORACLE["cases"]])
def test_an_ecr_canonical_request_and_string_to_sign_match_botocore(case: dict[str, Any]) -> None:
    canonical = sigv4.canonical_request(
        method="POST",
        path="/",
        query="",
        headers={**ECR_HEADERS, "Host": case["host"], "X-Amz-Date": case["timestamp"]},
        payload_hash=sigv4.sha256_hex(b"{}"),
    )
    scope = f"{case['timestamp'][:8]}/{case['region']}/ecr/aws4_request"

    assert canonical.text == case["canonical_request"]
    assert sigv4.string_to_sign(canonical.text, case["timestamp"], scope) == case["string_to_sign"]
    assert (
        sigv4.signing_key(SECRET, case["timestamp"][:8], case["region"], "ecr").hex()
        == (case["signing_key_hex"])
    )


def test_the_signed_headers_cover_content_type_host_date_and_target_only() -> None:
    signed = sign_ecr("eu-west-2")

    assert "SignedHeaders=content-type;host;x-amz-date;x-amz-target," in signed["Authorization"]
    assert set(signed) == {"Content-Type", "X-Amz-Target", "X-Amz-Date", "Authorization"}


def test_the_secret_access_key_appears_nowhere_in_what_is_sent() -> None:
    signed = sign_ecr("eu-west-2")

    assert SECRET not in "".join(f"{name}{value}" for name, value in signed.items())


def test_a_moment_in_another_time_zone_signs_as_the_same_utc_instant() -> None:
    offset = timezone(timedelta(hours=5, minutes=30))

    local = sign_ecr("eu-west-2", moment=MOMENT.astimezone(offset))

    assert local == sign_ecr("eu-west-2")


def test_the_region_changes_the_signature() -> None:
    assert sign_ecr("eu-west-1")["Authorization"] != sign_ecr("eu-west-2")["Authorization"]


def test_the_service_changes_the_signature() -> None:
    assert sign_ecr("eu-west-2", service="s3") != sign_ecr("eu-west-2")


def test_a_different_secret_changes_the_signature() -> None:
    assert sign_ecr("eu-west-2", secret=SECRET + "x") != sign_ecr("eu-west-2")


def test_the_payload_hash_header_is_added_only_when_asked_for() -> None:
    without = sign_ecr("eu-west-2")
    with_hash = sign_ecr("eu-west-2", content_sha256_header=True)

    assert "X-Amz-Content-Sha256" not in without
    assert with_hash["X-Amz-Content-Sha256"] == sigv4.sha256_hex(b"{}")


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({"Bad Name": "v"}, "tokens"),
        ({"Bad:Name": "v"}, "tokens"),
        ({"Host": "other.example"}, "set by the signer"),
        ({"x-amz-date": "20260102T030405Z"}, "set by the signer"),
        ({"Authorization": "x"}, "set by the signer"),
        ({"X-Test": "line\r\nInjected: 1"}, "single-line"),
        ({"X-Test": "nul\x00"}, "single-line"),
    ],
)
def test_a_header_that_could_split_or_override_the_request_is_refused(
    headers: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        sign_ecr("eu-west-2", headers=headers)


PAYLOAD = b"archive-bytes"


def sign_put(**overrides: Any) -> dict[str, str]:
    arguments: dict[str, Any] = {
        "method": "PUT",
        "host": "bucket.example.test",
        "path": "/backups/a.tar.gz",
        "headers": {"Content-Length": str(len(PAYLOAD))},
        "body": PAYLOAD,
        "access_key_id": ACCESS_KEY,
        "secret_access_key": SecretStr(SECRET),
        "region": "us-east-1",
        "service": "s3",
        "moment": MOMENT,
        "content_sha256_header": True,
    }
    return sigv4.sign_request(**{**arguments, **overrides})


def test_a_precomputed_payload_hash_signs_the_same_as_hashing_the_body() -> None:
    digest = sigv4.sha256_hex(PAYLOAD)

    hashed = sign_put()
    precomputed = sign_put(body=b"", payload_sha256=digest)

    assert precomputed == hashed
    assert precomputed["X-Amz-Content-Sha256"] == digest


@pytest.mark.parametrize(
    ("body", "payload_sha256"),
    [(PAYLOAD, "0" * 64), (b"", "NOTHEX"), (b"", "A" * 64), (b"", "0" * 63)],
    ids=["body-present", "not-hex", "upper-case", "too-short"],
)
def test_a_payload_hash_is_refused_with_a_body_or_when_it_is_not_lower_case_hex(
    body: bytes, payload_sha256: str
) -> None:
    with pytest.raises(ValueError, match="payload_sha256"):
        sign_put(body=body, payload_sha256=payload_sha256)
