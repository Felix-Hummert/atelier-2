"""Recognise one loose document without writing anything.

Each library kind has one marker. A marker either recognises the bytes or says,
in its own words, why it does not; the recognition is what exactly one marker
claimed. Two claims are refused naming both, because picking one would publish a
guess under Phase C's one-step add.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from atelier2.application.reconstruct_agent_definition import AgentDefinitionParser
from atelier2.contracts.agent_definitions import AgentDefinitionRefused
from atelier2.contracts.library_recognition import (
    AGENT_DEFINITION_PROVIDER,
    EXPECTED_DOCUMENT_FORMS,
    MCP_SERVERS_KEY,
    NOT_HELD_REASONS,
    SKILL_DOCUMENT_FILE_NAME,
    DocumentAmbiguous,
    DocumentNotHeld,
    DocumentUnrecognized,
    KindRefusal,
    LibraryDocumentKind,
    RecognizedAgentDefinition,
    RecognizedWorkflow,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflow_refusals import WorkflowDocumentInvalid
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
from atelier2.ports.workflow_revisions import WorkflowDocumentParser

FRONTMATTER_OPENING = b"---\n"

type MarkerClaim = RecognizedWorkflow | RecognizedAgentDefinition | DocumentNotHeld
type ClassifyDefinitionDocumentResult = (
    MarkerClaim | DocumentUnrecognized | DocumentAmbiguous
)


def classify_definition_document(
    document: bytes,
    file_name: str | None,
    parse_workflow: WorkflowDocumentParser,
    parse_agent_definition: AgentDefinitionParser,
) -> ClassifyDefinitionDocumentResult:
    base_name = None if file_name is None else PurePosixPath(file_name).name
    answers = (
        _workflow_marker(document, parse_workflow),
        _agent_definition_marker(document, base_name, parse_agent_definition),
        _skill_marker(document, base_name),
        _mcp_server_marker(document),
    )
    claims = tuple(answer for answer in answers if not isinstance(answer, KindRefusal))
    if len(claims) == 1:
        return claims[0]
    if not claims:
        return DocumentUnrecognized(
            tuple(answer for answer in answers if isinstance(answer, KindRefusal))
        )
    return DocumentAmbiguous(tuple(_kind_of(claim) for claim in claims))


def _kind_of(claim: MarkerClaim) -> LibraryDocumentKind:
    match claim:
        case RecognizedWorkflow():
            return LibraryDocumentKind.WORKFLOW
        case RecognizedAgentDefinition():
            return LibraryDocumentKind.AGENT_DEFINITION
        case DocumentNotHeld(kind):
            return kind


def _refused(kind: LibraryDocumentKind, because: str) -> KindRefusal:
    return KindRefusal(kind, EXPECTED_DOCUMENT_FORMS[kind], because)


def _workflow_marker(
    document: bytes, parse_workflow: WorkflowDocumentParser
) -> RecognizedWorkflow | KindRefusal:
    try:
        graph = parse_workflow(document)
    except WorkflowDocumentInvalid as refused:
        return _refused(LibraryDocumentKind.WORKFLOW, str(refused))
    if isinstance(graph, WorkflowGraphV3):
        return RecognizedWorkflow(
            WorkflowFormatVersion.V3, graph.name, graph.description
        )
    return RecognizedWorkflow(WorkflowFormatVersion(graph.format_version), None, None)


def _agent_definition_marker(
    document: bytes,
    base_name: str | None,
    parse_agent_definition: AgentDefinitionParser,
) -> RecognizedAgentDefinition | KindRefusal:
    # A skill file carries the same frontmatter an agent does; only its name
    # tells the two apart (ADR 0018 selection grammar), so the name decides.
    if base_name == SKILL_DOCUMENT_FILE_NAME:
        return _refused(
            LibraryDocumentKind.AGENT_DEFINITION,
            f"a file named {SKILL_DOCUMENT_FILE_NAME} is a skill, not an agent",
        )
    try:
        definition = parse_agent_definition(document)
    except AgentDefinitionRefused as refused:
        return _refused(LibraryDocumentKind.AGENT_DEFINITION, str(refused))
    return RecognizedAgentDefinition(
        definition.name, definition.description, AGENT_DEFINITION_PROVIDER
    )


def _skill_marker(
    document: bytes, base_name: str | None
) -> DocumentNotHeld | KindRefusal:
    if base_name != SKILL_DOCUMENT_FILE_NAME:
        return _refused(
            LibraryDocumentKind.SKILL,
            f"the file is not named {SKILL_DOCUMENT_FILE_NAME}",
        )
    if not document.startswith(FRONTMATTER_OPENING):
        return _refused(
            LibraryDocumentKind.SKILL,
            f"{SKILL_DOCUMENT_FILE_NAME} does not open with frontmatter",
        )
    return DocumentNotHeld(
        LibraryDocumentKind.SKILL, NOT_HELD_REASONS[LibraryDocumentKind.SKILL]
    )


def _mcp_server_marker(document: bytes) -> DocumentNotHeld | KindRefusal:
    try:
        declaration = json.loads(document)
    except ValueError:
        return _refused(LibraryDocumentKind.MCP_SERVER, "the bytes are not JSON")
    if not isinstance(declaration, dict):
        return _refused(LibraryDocumentKind.MCP_SERVER, "the JSON is not an object")
    if MCP_SERVERS_KEY not in declaration:
        return _refused(
            LibraryDocumentKind.MCP_SERVER,
            f"the JSON object has no `{MCP_SERVERS_KEY}` key",
        )
    return DocumentNotHeld(
        LibraryDocumentKind.MCP_SERVER, NOT_HELD_REASONS[LibraryDocumentKind.MCP_SERVER]
    )
