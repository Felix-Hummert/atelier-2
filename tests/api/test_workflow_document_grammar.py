"""What the API says a workflow document is, judged against what it accepts.

A consumer authors the one thing this API otherwise takes as opaque bytes. These
tests hold the published description to the parser it claims to describe: what
the shape refuses, the publication refuses; and where the shape is deliberately
silent, the refusal still has a name.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from atelier2.adapters.yaml_workflows import (
    InvalidWorkflowDocument,
    parse_workflow_document,
)
from atelier2.contracts.verdicts import VERDICT_ANSWER_SCHEMA, Verdict
from atelier2.contracts.workflow_documents import WORKFLOW_DOCUMENT_FORMATS
from atelier2.contracts.workflow_refusals import WorkflowRefusalReason
from tests.scenarios.api import (
    described_api_client,
    discovered_openapi_document,
    named_document_path,
    openapi_component,
    published_workflow_grammar,
    published_workflow_grammar_reference,
)
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    LOOPED_LINE_DOCUMENT,
    V3_DOCUMENT,
    VERDICT_LOOP_DOCUMENT,
    declared_output,
)

GUESSED_PATH = "/where-a-consumer-holding-only-a-base-url-knocks-first"

V1_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: agent, type: agent, job: test, output: payload, next: final}
"""
V2_DOCUMENT = b"""format_version: 2
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: agent, type: agent, role: builder, job: test, next: final}
"""
ONE_AGENT_DOCUMENT = b"""format_version: 3
name: One agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
""" + declared_output()
AGENT_KIND_LINE = b"    type: agent\n"


@pytest.fixture(scope="module")
def api() -> TestClient:
    return described_api_client()


@pytest.fixture(scope="module")
def openapi_document(api: TestClient) -> Any:
    """The description reached the way a consumer has to reach it."""

    return discovered_openapi_document(api, GUESSED_PATH)


@dataclass(frozen=True)
class DocumentCase:
    """One authored document, and whether this API takes it at all."""

    name: str
    document: bytes
    accepted: bool


DOCUMENT_CASES = (
    DocumentCase("a v1 document", V1_DOCUMENT, True),
    DocumentCase("a v2 document", V2_DOCUMENT, True),
    DocumentCase("a v3 chain", V3_DOCUMENT, True),
    DocumentCase("a single v3 agent", ONE_AGENT_DOCUMENT, True),
    DocumentCase(
        "an unsupported format version",
        ONE_AGENT_DOCUMENT.replace(b"format_version: 3", b"format_version: 9"),
        False,
    ),
    DocumentCase(
        "a node kind nothing declares",
        ONE_AGENT_DOCUMENT.replace(AGENT_KIND_LINE, b"    type: oracle\n"),
        False,
    ),
    DocumentCase(
        "a key an earlier format retired",
        ONE_AGENT_DOCUMENT.replace(AGENT_KIND_LINE, AGENT_KIND_LINE + b"    job: go\n"),
        False,
    ),
    DocumentCase(
        "a node kind without the field it needs",
        ONE_AGENT_DOCUMENT.replace(b"    mode: headless\n", b""),
        False,
    ),
    DocumentCase(
        "a handover naming no output",
        V3_DOCUMENT.replace(
            b"from: {node: implement, output: candidate}", b"from: {node: implement}"
        ),
        False,
    ),
    DocumentCase(
        "a role pinned to one exact model",
        ONE_AGENT_DOCUMENT.replace(
            AGENT_KIND_LINE, AGENT_KIND_LINE + b"    model: claude-opus-5\n"
        ),
        True,
    ),
    DocumentCase(
        "a role pinned to an alias instead of a model id",
        ONE_AGENT_DOCUMENT.replace(
            AGENT_KIND_LINE, AGENT_KIND_LINE + b"    model: newest opus\n"
        ),
        False,
    ),
    DocumentCase("a declared loop", LOOPED_LINE_DOCUMENT, True),
    DocumentCase(
        "a loop without the bound it must declare",
        LOOPED_LINE_DOCUMENT.replace(b"    maximum_rounds: 3\n", b""),
        False,
    ),
    DocumentCase("a loop a verdict steers", VERDICT_LOOP_DOCUMENT, True),
    DocumentCase(
        "a verdict outside the closed vocabulary",
        VERDICT_LOOP_DOCUMENT.replace(
            f"verdict: {Verdict.REVISE.value}".encode(), b"verdict: whenever"
        ),
        False,
    ),
)


@dataclass(frozen=True)
class WholeDocumentCase:
    """One rule the shape admits because no single node can answer it."""

    name: str
    document: bytes
    reason: WorkflowRefusalReason


WHOLE_DOCUMENT_CASES = (
    WholeDocumentCase(
        "a control edge naming no node",
        ONE_AGENT_DOCUMENT.replace(
            AGENT_KIND_LINE, AGENT_KIND_LINE + b"    depends_on: [nowhere]\n"
        ),
        WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
    ),
    WholeDocumentCase(
        "a loop repeating a node the document does not declare",
        LOOPED_LINE_DOCUMENT.replace(b"body: [implement, review]", b"body: [nowhere]"),
        WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
    ),
    WholeDocumentCase(
        "a loop entered where the round would already have run",
        LOOPED_LINE_DOCUMENT.replace(
            b"body: [implement, review]", b"body: [review, implement]"
        ),
        WorkflowRefusalReason.LOOP_BODY_NOT_ONE_LINE,
    ),
    WholeDocumentCase(
        "a verdict read from a node that does not close the round",
        VERDICT_LOOP_DOCUMENT.replace(b"{node: review,", b"{node: implement,"),
        WorkflowRefusalReason.LOOP_VERDICT_NODE_NOT_THE_ROUND_END,
    ),
    WholeDocumentCase(
        "a verdict read from an answer under another contract",
        VERDICT_LOOP_DOCUMENT.replace(
            VERDICT_ANSWER_SCHEMA.revision_hash.value.encode("ascii"),
            ANY_JSON_SCHEMA.revision_hash.value.encode("ascii"),
        ),
        WorkflowRefusalReason.LOOP_VERDICT_UNREADABLE,
    ),
)


@pytest.mark.proves("the-published-shape-is-the-grammar-the-publication-reads")
@pytest.mark.parametrize(
    "case", DOCUMENT_CASES, ids=[case.name for case in DOCUMENT_CASES]
)
def test_the_published_shape_answers_exactly_what_the_publication_answers(
    case: DocumentCase, openapi_document: Any
) -> None:
    """One grammar, seen twice: the consumer's check and the door's check agree.

    A description written beside the models instead of derived from them drifts
    the moment a field, a node kind or a format version moves, and this is where
    that drift dies.
    """

    grammar = published_workflow_grammar(openapi_document)

    assert grammar.is_valid(yaml.safe_load(case.document)) is case.accepted
    assert _is_a_workflow_document(case.document) is case.accepted


@pytest.mark.proves("the-graph-grammar-is-reachable-from-the-base-url")
def test_a_guessed_path_is_refused_by_naming_the_document_that_lists_them(
    api: TestClient,
) -> None:
    """The one answer a consumer holding nothing but a base URL is sure to get."""

    refusal = api.get(GUESSED_PATH)

    assert refusal.status_code == HTTPStatus.NOT_FOUND
    named = api.get(named_document_path(refusal.json()["detail"]))
    assert named.status_code == HTTPStatus.OK
    assert named.json()["openapi"] == "3.1.0"


@pytest.mark.proves("the-graph-grammar-is-reachable-from-the-base-url")
def test_the_named_document_describes_every_format_a_document_may_declare(
    openapi_document: Any,
) -> None:
    """Following the publication body leads to the grammar, not to opaque bytes."""

    grammar = openapi_component(
        openapi_document, published_workflow_grammar_reference(openapi_document)
    )

    described = {
        openapi_component(openapi_document, variant)["properties"]["format_version"][
            "const"
        ]
        for variant in grammar["oneOf"]
    }

    assert described == {version.value for version in WORKFLOW_DOCUMENT_FORMATS}


@pytest.mark.proves("the-published-shape-is-the-grammar-the-publication-reads")
@pytest.mark.parametrize(
    "case", WHOLE_DOCUMENT_CASES, ids=[case.name for case in WHOLE_DOCUMENT_CASES]
)
def test_a_rule_no_single_node_can_answer_keeps_its_name_at_the_door(
    case: WholeDocumentCase, openapi_document: Any
) -> None:
    """The shape is honest about its own boundary rather than silently wrong.

    Whether a declared edge resolves, and whether a declared loop repeats one
    uninterrupted stretch of the order, are statements about the whole document,
    and the published shape admits both. What must not happen is that they are
    then swallowed: the publication refuses each under its own name.
    """

    grammar = published_workflow_grammar(openapi_document)

    assert grammar.is_valid(yaml.safe_load(case.document))
    with pytest.raises(InvalidWorkflowDocument) as refused:
        parse_workflow_document(case.document)
    assert refused.value.refusal is not None
    assert refused.value.refusal.reason is case.reason


def _is_a_workflow_document(document: bytes) -> bool:
    try:
        parse_workflow_document(document)
    except InvalidWorkflowDocument:
        return False
    return True
