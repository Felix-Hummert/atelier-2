"""The declared-kind intake boundary refuses bad requests before persistence."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.api.openapi import LIBRARY_ADDITIONS_PATH
from tests.scenarios.api import described_api_client

ACTOR = "operator"
ACTIVATED_AT = "2026-08-27T00:00:00Z"
OPAQUE_DOCUMENT = b"bytes this route does not classify"


def add(
    api: TestClient,
    *,
    kind: str = "workflow",
    media_type: str = "application/octet-stream",
    **fields: str,
) -> Response:
    return api.post(
        LIBRARY_ADDITIONS_PATH,
        content=OPAQUE_DOCUMENT,
        params={
            "kind": kind,
            "actor": ACTOR,
            "activated_at": ACTIVATED_AT,
            **fields,
        },
        headers={"content-type": media_type},
    )


def problem_type(answered: Response) -> str:
    return str(answered.json()["type"]).rsplit(":", maxsplit=1)[-1]


@pytest.mark.proves("a-catalog-intake-keeps-the-kind-it-was-handed-in")
def test_an_unknown_declared_kind_is_a_named_invalid_request() -> None:
    refused = add(described_api_client(), kind="unknown")

    assert refused.status_code == 422, refused.text
    assert problem_type(refused) == "invalid-request"
    assert refused.json()["invalid_fields"][0]["path"] == "query/kind"


@pytest.mark.proves("a-catalog-intake-keeps-the-kind-it-was-handed-in")
@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"actor": ""}, id="empty-actor"),
        pytest.param({"activated_at": "yesterday"}, id="not-an-instant"),
    ],
)
def test_an_addition_without_honest_attribution_is_refused(
    fields: dict[str, str],
) -> None:
    refused = add(described_api_client(), **fields)

    assert refused.status_code == 422, refused.text
    assert problem_type(refused) == "invalid-request"


@pytest.mark.proves("a-catalog-intake-keeps-the-kind-it-was-handed-in")
def test_the_door_reads_opaque_bytes_and_refuses_another_media_type() -> None:
    refused = add(described_api_client(), media_type="application/yaml")

    assert refused.status_code == 415, refused.text
    assert problem_type(refused) == "unsupported-media-type"
