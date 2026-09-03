"""The artifact door in both directions: bytes in under an address, bytes out."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.openapi import ARTIFACTS_PATH
from atelier2.api.problems import PROBLEM_TYPE_PREFIX
from atelier2.api.routes.artifacts import ARTIFACT_MEDIA_TYPE
from atelier2.contracts.artifacts import Artifact, ArtifactHash
from atelier2.ports.artifacts import ArtifactCreated, ArtifactExisting
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

PUBLISHED_MATERIAL = bytes(range(256)) * 400
"""Material of a size the door exists for, carrying every byte value."""


class KeptArtifacts:
    """The durable artifact store, kept in memory for one composed API."""

    def __init__(self) -> None:
        self._kept: dict[str, Artifact] = {}

    def publish_artifact(
        self, artifact: Artifact
    ) -> ArtifactCreated | ArtifactExisting:
        address = artifact.artifact_hash.value
        if address in self._kept:
            return ArtifactExisting(self._kept[address])
        self._kept[address] = artifact
        return ArtifactCreated(artifact)

    def read_artifact(self, artifact_hash: ArtifactHash) -> Artifact | None:
        return self._kept.get(artifact_hash.value)


def artifact_client() -> TestClient:
    """The composed HTTP boundary in front of one artifact store and nothing else."""

    kept = KeptArtifacts()
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(artifact_publisher=kept, artifact_reader=kept),
            limits=api_limits(maximum_request_body_bytes=len(PUBLISHED_MATERIAL)),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def test_published_material_reads_back_byte_identical_under_its_address() -> None:
    client = artifact_client()

    published = client.post(
        ARTIFACTS_PATH,
        content=PUBLISHED_MATERIAL,
        headers={"content-type": ARTIFACT_MEDIA_TYPE},
    )
    address = published.json()["artifact_hash"]
    read = client.get(f"{ARTIFACTS_PATH}/{address}")

    assert published.status_code == HTTPStatus.CREATED
    assert read.status_code == HTTPStatus.OK
    assert read.headers["content-type"] == ARTIFACT_MEDIA_TYPE
    assert read.content == PUBLISHED_MATERIAL


@pytest.mark.parametrize(
    ("address", "status", "code"),
    (
        (
            ArtifactHash.of(b"never published").value,
            HTTPStatus.NOT_FOUND,
            "artifact-not-found",
        ),
        ("not-an-address", HTTPStatus.BAD_REQUEST, "invalid-artifact-hash"),
    ),
    ids=("unknown", "malformed"),
)
def test_an_address_this_store_cannot_answer_is_refused_by_name(
    address: str, status: HTTPStatus, code: str
) -> None:
    client = artifact_client()

    read = client.get(f"{ARTIFACTS_PATH}/{address}")

    assert read.status_code == status
    assert read.headers["content-type"] == "application/problem+json"
    assert read.json()["type"] == PROBLEM_TYPE_PREFIX + code
