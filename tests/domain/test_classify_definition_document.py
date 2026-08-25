"""What the library says a loose document is, from its bytes and its name alone."""

from __future__ import annotations

import pytest

from atelier2.adapters.markdown_agent_definitions import parse_agent_definition
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.classify_definition_document import (
    ClassifyDefinitionDocumentResult,
    classify_definition_document,
)
from atelier2.contracts.agents import ProviderId
from atelier2.contracts.library_recognition import (
    DocumentAmbiguous,
    DocumentNotHeld,
    DocumentUnrecognized,
    LibraryDocumentKind,
    RecognizedAgentDefinition,
    RecognizedWorkflow,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from tests.scenarios.workflows import V3_DOCUMENT

AGENT_DOCUMENT = (
    b"---\n"
    b"name: stage-name-witness\n"
    b"description: Watches the stage and names what it sees.\n"
    b"---\n"
    b"You watch the stage and name what you see.\n"
)
V1_DOCUMENT = (
    b"format_version: 1\n"
    b"start: final\n"
    b"nodes:\n"
    b"  - {id: final, type: subworkflow, operation: add, operands: [1, 2], next: null}\n"
)
MCP_DECLARATION = b'{"mcpServers": {"github": {"command": "gh-mcp"}}}'


def recognise(
    document: bytes, file_name: str | None = None
) -> ClassifyDefinitionDocumentResult:
    return classify_definition_document(
        document, file_name, parse_workflow_document, parse_agent_definition
    )


@pytest.mark.parametrize(
    ("document", "file_name", "expected"),
    [
        pytest.param(
            V3_DOCUMENT,
            "workflows/review.yaml",
            RecognizedWorkflow(
                WorkflowFormatVersion.V3,
                "Implement a candidate, then review it for defects",
                None,
            ),
            id="v3-workflow-carries-its-authored-name",
        ),
        pytest.param(
            V1_DOCUMENT,
            None,
            RecognizedWorkflow(WorkflowFormatVersion.V1, None, None),
            id="older-workflow-format-authors-no-name",
        ),
        pytest.param(
            AGENT_DOCUMENT,
            "agents/witness.md",
            RecognizedAgentDefinition(
                "stage-name-witness",
                "Watches the stage and names what it sees.",
                ProviderId("anthropic"),
            ),
            id="agent-definition-carries-its-provider-mark",
        ),
        pytest.param(
            AGENT_DOCUMENT,
            None,
            RecognizedAgentDefinition(
                "stage-name-witness",
                "Watches the stage and names what it sees.",
                ProviderId("anthropic"),
            ),
            id="agent-definition-needs-no-file-name",
        ),
    ],
)
def test_a_held_kind_is_recognised_by_its_own_parser(
    document: bytes, file_name: str | None, expected: ClassifyDefinitionDocumentResult
) -> None:
    assert recognise(document, file_name) == expected


@pytest.mark.parametrize(
    ("document", "file_name", "kind"),
    [
        pytest.param(
            AGENT_DOCUMENT,
            "skills/witness/SKILL.md",
            LibraryDocumentKind.SKILL,
            id="skill-frontmatter-is-claimed-by-its-file-name",
        ),
        pytest.param(
            MCP_DECLARATION,
            ".mcp.json",
            LibraryDocumentKind.MCP_SERVER,
            id="mcp-declaration-by-its-servers-key",
        ),
        pytest.param(
            MCP_DECLARATION,
            None,
            LibraryDocumentKind.MCP_SERVER,
            id="mcp-declaration-needs-no-file-name",
        ),
    ],
)
def test_a_kind_the_library_does_not_hold_is_named_with_its_reason(
    document: bytes, file_name: str | None, kind: LibraryDocumentKind
) -> None:
    recognised = recognise(document, file_name)

    assert isinstance(recognised, DocumentNotHeld)
    assert recognised.kind is kind
    assert recognised.reason


def test_an_unrecognised_document_is_told_what_every_marker_expected() -> None:
    recognised = recognise(b"just a note to self\n", "notes.txt")

    assert isinstance(recognised, DocumentUnrecognized)
    by_kind = {refusal.kind: refusal for refusal in recognised.refusals}
    assert set(by_kind) == set(LibraryDocumentKind)
    assert "format_version" in by_kind[LibraryDocumentKind.WORKFLOW].expected
    assert "frontmatter-missing" in (
        by_kind[LibraryDocumentKind.AGENT_DEFINITION].refused_because
    )
    assert "SKILL.md" in by_kind[LibraryDocumentKind.SKILL].refused_because
    assert "JSON" in by_kind[LibraryDocumentKind.MCP_SERVER].refused_because


def test_a_workflow_missing_its_format_version_is_refused_by_that_field() -> None:
    recognised = recognise(b"name: no version\nnodes: []\n", "workflow.yaml")

    assert isinstance(recognised, DocumentUnrecognized)
    workflow = next(
        refusal
        for refusal in recognised.refusals
        if refusal.kind is LibraryDocumentKind.WORKFLOW
    )
    assert "format_version" in workflow.refused_because


def test_a_skill_file_holding_workflow_bytes_is_refused_naming_both() -> None:
    recognised = recognise(b"---\n" + V3_DOCUMENT, "SKILL.md")

    assert recognised == DocumentAmbiguous(
        (LibraryDocumentKind.WORKFLOW, LibraryDocumentKind.SKILL)
    )
