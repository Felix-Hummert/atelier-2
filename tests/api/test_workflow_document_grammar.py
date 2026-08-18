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
from tests.scenarios.workflows import V3_DOCUMENT, declared_output

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
def test_a_rule_no_single_node_can_answer_keeps_its_name_at_the_door(
    openapi_document: Any,
) -> None:
    """The shape is honest about its own boundary rather than silently wrong.

    Whether a declared edge resolves is a statement about the whole document, and
    the published shape admits this one. What must not happen is that it is then
    swallowed: the publication refuses it under the name of the node nothing
    declares.
    """

    document = ONE_AGENT_DOCUMENT.replace(
        AGENT_KIND_LINE, AGENT_KIND_LINE + b"    depends_on: [nowhere]\n"
    )
    grammar = published_workflow_grammar(openapi_document)

    assert grammar.is_valid(yaml.safe_load(document))
    with pytest.raises(InvalidWorkflowDocument) as refused:
        parse_workflow_document(document)
    assert refused.value.refusal is not None
    assert refused.value.refusal.reason is WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE


def _is_a_workflow_document(document: bytes) -> bool:
    try:
        parse_workflow_document(document)
    except InvalidWorkflowDocument:
        return False
    return True
