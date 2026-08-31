"""Recognise one loose document without writing anything.

Each library kind has one marker. A marker either recognises the bytes or says,
in its own words, why it does not; the recognition is what exactly one marker
claimed. Two claims are refused naming both, because picking one would publish a
guess under Phase C's one-step add.

A file name is a marker, never a tie-breaker: `SKILL.md` claims a document with
a closed frontmatter block, and the agent marker claims any frontmatter that
parses as an agent definition regardless of the name. A `SKILL.md` whose
frontmatter is a valid agent definition is therefore ambiguous, and only a
`SKILL.md` that is not a valid agent is a skill (head decision on #698, until a
configured kind from #660 P1 can say which one was meant).
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from atelier2.application.reconstruct_agent_definition import AgentDefinitionParser
from atelier2.contracts.agent_definitions import (
    AgentDefinition,
    AgentDefinitionRefusal,
    AgentDefinitionRefused,
)
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
from atelier2.ports.workflow_revisions import WorkflowDocumentParser

_NO_CLOSED_FRONTMATTER = frozenset(
    {
        AgentDefinitionRefusal.DOCUMENT_NOT_UTF8,
        AgentDefinitionRefusal.FRONTMATTER_MISSING,
        AgentDefinitionRefusal.FRONTMATTER_UNTERMINATED,
    }
)
"""The refusals the agent parser raises before it has found a closed block.

Every other refusal, and a successful parse, means both delimiters were found:
the skill marker reads that from the one parser that already knows, instead of
splitting frontmatter a second time.
"""

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
    agent_reading = _read_agent_definition(document, parse_agent_definition)
    answers = (
        _workflow_marker(document, parse_workflow),
        _agent_definition_marker(agent_reading),
        _skill_marker(agent_reading, base_name),
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
    return RecognizedWorkflow(WorkflowFormatVersion.V3, graph.name, graph.description)


def _read_agent_definition(
    document: bytes, parse_agent_definition: AgentDefinitionParser
) -> AgentDefinition | AgentDefinitionRefused:
    try:
        return parse_agent_definition(document)
    except AgentDefinitionRefused as refused:
        return refused


def _agent_definition_marker(
    reading: AgentDefinition | AgentDefinitionRefused,
) -> RecognizedAgentDefinition | KindRefusal:
    if isinstance(reading, AgentDefinitionRefused):
        return _refused(LibraryDocumentKind.AGENT_DEFINITION, str(reading))
    return RecognizedAgentDefinition(
        reading.name, reading.description, AGENT_DEFINITION_PROVIDER
    )


def _skill_marker(
    reading: AgentDefinition | AgentDefinitionRefused, base_name: str | None
) -> DocumentNotHeld | KindRefusal:
    if base_name != SKILL_DOCUMENT_FILE_NAME:
        return _refused(
            LibraryDocumentKind.SKILL,
            f"the file is not named {SKILL_DOCUMENT_FILE_NAME}",
        )
    if (
        isinstance(reading, AgentDefinitionRefused)
        and reading.refusal in _NO_CLOSED_FRONTMATTER
    ):
        return _refused(
            LibraryDocumentKind.SKILL,
            f"{SKILL_DOCUMENT_FILE_NAME} does not carry a closed frontmatter block",
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
