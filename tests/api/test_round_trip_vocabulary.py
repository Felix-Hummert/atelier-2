"""The published description says the same thing the running API does.

`tests/integration/test_api_round_trip.py` proves a consumer can drive the four
round-trips without renaming a value. These two tests hold the *description* to
the same promise, so a consumer reading the OpenAPI document before it sends
anything is told the truth: no served body hides which revision or which format
it means, and each request the description defines is writable out of the fields
the answers before it define.
"""

from __future__ import annotations

from typing import Any

import pytest

from atelier2.api.openapi import WORKFLOW_DOCUMENT_COMPONENT
from tests.api.test_openapi import served_app

AMBIGUOUS_PROPERTY_NAMES = ("revision_hash", "format_version")
"""The two words this API stopped using on its own bodies.

Both were true of several different things at once -- a workflow revision, a
schema revision, a budget revision; a workflow document's format, a run's -- so
a consumer reading one could not know what to write it back into. Each is now
spelled with the thing it names. The authored workflow document keeps them,
which is why the published grammar of that document is not judged here: inside a
document there is only one revision and one format they can mean.
"""


class RoundTrip:
    """One request, the answers it is written out of, and what the caller brings.

    `own` is the material no answer can supply -- an identity the caller chooses,
    bytes it authored, an attribution it makes. Everything else in the request
    has to be a field one of the named answers already published under that name.
    """

    def __init__(self, request: str, answers: tuple[str, ...], own: frozenset[str]):
        self.request = request
        self.answers = answers
        self.own = own

    def __repr__(self) -> str:
        return self.request


ROUND_TRIPS = (
    RoundTrip(
        "StartRunRequestResourceV3",
        ("WorkflowRevisionSummaryResourceV2",),
        frozenset({"run_id", "agent_bindings", "orders"}),
    ),
    RoundTrip(
        "AnswerWaitRequestResource",
        ("RunResourceV3", "NodeRailResource"),
        frozenset({"answer_base64"}),
    ),
    RoundTrip(
        "ArtifactOrderResource",
        ("ArtifactResource", "WorkflowDeclaredOrderResourceV3"),
        frozenset(),
    ),
    RoundTrip(
        "FoundCatalogLineageRequestResource",
        ("WorkflowRevisionDetailResource",),
        frozenset({"actor", "activated_at"}),
    ),
)


def served_components() -> dict[str, Any]:
    return served_app().openapi()["components"]["schemas"]


COMPONENT_REFERENCE_PREFIX = "#/components/schemas/"


def referenced_components(schema: Any) -> set[str]:
    """The components one published schema names directly."""

    found: set[str] = set()
    if isinstance(schema, dict):
        reference = schema.get("$ref")
        if isinstance(reference, str) and reference.startswith(
            COMPONENT_REFERENCE_PREFIX
        ):
            found.add(reference.removeprefix(COMPONENT_REFERENCE_PREFIX))
        for value in schema.values():
            found |= referenced_components(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= referenced_components(item)
    return found


def document_grammar_components(components: dict[str, Any]) -> set[str]:
    """Everything the published workflow-document grammar is made of.

    Derived by following the grammar's own references rather than listed, so a
    grammar that grows a component does not quietly start being judged as if it
    were a wire body. Inside a document, `revision_hash` and `format_version`
    mean exactly one thing each -- that is the document's namespace, not this
    API's.
    """

    reached = {WORKFLOW_DOCUMENT_COMPONENT}
    while True:
        found = reached | {
            name
            for reachable in reached
            for name in referenced_components(components[reachable])
        }
        if found == reached:
            return reached
        reached = found


def property_names(schema: Any) -> set[str]:
    """Every property name reachable inside one published component."""

    found: set[str] = set()
    if isinstance(schema, dict):
        declared = schema.get("properties")
        if isinstance(declared, dict):
            found.update(declared)
        for value in schema.values():
            found |= property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= property_names(item)
    return found


@pytest.mark.proves("no-served-body-names-a-revision-or-a-format-without-saying-which")
def test_no_served_body_names_a_revision_or_a_format_without_saying_which() -> None:
    components = served_components()
    authored = document_grammar_components(components)

    offenders = {
        name: sorted(property_names(schema) & set(AMBIGUOUS_PROPERTY_NAMES))
        for name, schema in components.items()
        if name not in authored
        and property_names(schema) & set(AMBIGUOUS_PROPERTY_NAMES)
    }

    assert offenders == {}


@pytest.mark.parametrize("round_trip", ROUND_TRIPS, ids=repr)
@pytest.mark.proves("a-consumer-writes-every-request-out-of-the-answers-it-read")
def test_each_request_is_writable_out_of_the_answers_that_precede_it(
    round_trip: RoundTrip,
) -> None:
    """Every required request field is a field some answer already publishes."""

    components = served_components()
    required = set(components[round_trip.request].get("required", ()))
    published = set[str]().union(
        *(property_names(components[answer]) for answer in round_trip.answers)
    )

    assert required - round_trip.own <= published
