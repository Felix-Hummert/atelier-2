"""What the one-step addition door refuses, and that a refusal reaches no store.

Every durable port is left unwired on purpose. A refusal that touched the store
would fail here with an attribute error instead of a problem document, which is
exactly the sentence these tests claim: nothing is published before the library
knows what it was handed and what to call it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.api.openapi import LIBRARY_ADDITIONS_PATH
from tests.scenarios.api import described_api_client

ACTOR = "operator"
ACTIVATED_AT = "2026-08-26T00:00:00Z"

NAMED_WORKFLOW = b"""format_version: 3
name: review-bounded-diff
nodes:
  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Review one bounded diff.
"""
PROSE_NAMED_WORKFLOW = NAMED_WORKFLOW.replace(
    b"name: review-bounded-diff", b"name: Review one bounded diff, then say so"
)
UNNAMED_WORKFLOW = b"""format_version: 1
start: calculate
nodes:
  - id: calculate
    type: subworkflow
    operation: add
    operands: [1, 2]
    next:
"""
AGENT_DOCUMENT = (
    b"---\n"
    b"name: stage-name-witness\n"
    b"description: Watches the stage and names what it sees.\n"
    b"---\n"
    b"You watch the stage and name what you see.\n"
)
SKILL_DOCUMENT = AGENT_DOCUMENT.replace(b"---\nYou", b"allowed-tools: Read\n---\nYou")
MCP_DECLARATION = b'{"mcpServers": {"github": {"command": "gh-mcp"}}}'
UNREADABLE_DOCUMENT = b"just some prose nobody meant as a definition\n"


def add(
    api: TestClient,
    document: bytes,
    file_name: str | None = None,
    media_type: str = "application/octet-stream",
    **attribution: str,
) -> Response:
    parameters = {"actor": ACTOR, "activated_at": ACTIVATED_AT, **attribution}
    if file_name is not None:
        parameters["file_name"] = file_name
    return api.post(
        LIBRARY_ADDITIONS_PATH,
        content=document,
        params=parameters,
        headers={"content-type": media_type},
    )


def problem_type(answered: Response) -> str:
    return str(answered.json()["type"]).rsplit(":", maxsplit=1)[-1]


@pytest.mark.proves("a-refused-addition-publishes-nothing")
@pytest.mark.parametrize(
    ("document", "file_name", "expected_type"),
    [
        pytest.param(
            UNREADABLE_DOCUMENT, None, "library-document-unrecognized", id="unreadable"
        ),
        pytest.param(
            AGENT_DOCUMENT, "SKILL.md", "library-document-ambiguous", id="two-markers"
        ),
        pytest.param(SKILL_DOCUMENT, "SKILL.md", "library-kind-not-held", id="skill"),
        pytest.param(
            MCP_DECLARATION, ".mcp.json", "library-kind-not-held", id="mcp-server"
        ),
        pytest.param(
            UNNAMED_WORKFLOW, None, "library-name-unusable", id="format-authors-no-name"
        ),
        pytest.param(
            PROSE_NAMED_WORKFLOW, None, "library-name-unusable", id="name-is-prose"
        ),
    ],
)
def test_a_document_the_library_will_not_take_is_refused_by_its_own_reason(
    document: bytes, file_name: str | None, expected_type: str
) -> None:
    answered = add(described_api_client(), document, file_name)

    assert answered.status_code == 422, answered.text
    assert problem_type(answered) == expected_type
    assert answered.json()["detail"]


@pytest.mark.proves("a-refused-addition-publishes-nothing")
def test_an_unrecognised_document_is_told_what_every_marker_looked_for() -> None:
    answered = add(described_api_client(), UNREADABLE_DOCUMENT)

    detail = str(answered.json()["detail"])
    assert "format_version" in detail
    assert "frontmatter" in detail
    assert "SKILL.md" in detail
    assert "mcpServers" in detail


@pytest.mark.proves("a-refused-addition-publishes-nothing")
@pytest.mark.parametrize(
    "attribution",
    [
        pytest.param({"actor": ""}, id="empty-actor"),
        pytest.param({"activated_at": "yesterday"}, id="not-an-instant"),
    ],
)
def test_an_addition_without_honest_attribution_is_refused(
    attribution: dict[str, str],
) -> None:
    answered = add(described_api_client(), NAMED_WORKFLOW, **attribution)

    assert answered.status_code == 422, answered.text
    assert problem_type(answered) == "invalid-request"


@pytest.mark.proves("a-refused-addition-publishes-nothing")
def test_the_door_reads_opaque_bytes_and_refuses_another_media_type() -> None:
    answered = add(
        described_api_client(), NAMED_WORKFLOW, media_type="application/yaml"
    )

    assert answered.status_code == 415, answered.text
    assert problem_type(answered) == "unsupported-media-type"
