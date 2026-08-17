"""What a V3 node was asked to do, composed from the document and one snapshot.

Before this owner existed, every `node-execution-request/v3` that reached a
durable store was assembled by its caller, so two callers could describe the same
node differently and both be written. These tests pin the composition itself: the
manifest names exactly the declared members in their declared order, the request
binds exactly the revisions the frozen configuration resolved, and a reference the
configuration does not carry is refused by its site rather than dropped.
"""

from __future__ import annotations

import pytest

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.bind_node_execution import (
    NodeExecutionBindingUnsupported,
    NodeExecutionUnresolved,
    bind_node_execution,
)
from atelier2.contracts.agents import AgentBindingSet, AgentExecutionCapability
from atelier2.contracts.node_records_v3 import (
    ContextPackageMember,
    NodeKindV3,
    declared_context_package_of,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_configuration_v3 import (
    ReferenceSite,
    ResolvedReference,
    RunConfigurationRevision,
)
from atelier2.contracts.runs import RunId, WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import VersionedReference, WorkflowGraphV3

RUN = RunId("run/bind")
HOUSE_STYLE = PublishedRevisionHash.of(b"the house style")
KITCHEN_NOTES = PublishedRevisionHash.of(b"the kitchen notes")
RETRY_POLICY = PublishedRevisionHash.of(b"the retry policy")

DOCUMENT = f"""format_version: 3
name: One agent with context
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Cook the one thing this package is for.
    required_context:
      - name: style
        source:
          ref: house-style
          revision: {HOUSE_STYLE.value}
          selector: sections/plating
      - name: notes
        source:
          ref: kitchen-notes
          revision: {KITCHEN_NOTES.value}
          selector: sections/timing
    retry:
      ref: retry-policy
      revision: {RETRY_POLICY.value}
""".encode()


def graph() -> WorkflowGraphV3:
    parsed = parse_workflow_document(DOCUMENT)
    assert isinstance(parsed, WorkflowGraphV3)
    return parsed


def revision_hash() -> WorkflowRevisionHash:
    return WorkflowRevisionHash(WorkflowRevision(DOCUMENT).revision_hash.value)


def _context(entry: str, revision: PublishedRevisionHash) -> ResolvedReference:
    return ResolvedReference(
        ReferenceSite("required_context.source", "implement", entry),
        RevisionKind.CONTEXT_SOURCE,
        VersionedReference(ref=f"{entry}-source", revision=revision.value),
        revision,
    )


def configuration(
    *resolutions: ResolvedReference,
) -> RunConfigurationRevision:
    return RunConfigurationRevision(
        revision_hash(), AgentBindingSet(()).binding_set_hash, resolutions
    )


def fully_resolved() -> RunConfigurationRevision:
    return configuration(
        _context("style", HOUSE_STYLE),
        _context("notes", KITCHEN_NOTES),
        ResolvedReference(
            ReferenceSite("retry", "implement"),
            RevisionKind.RETRY_POLICY,
            VersionedReference(ref="retry-policy", revision=RETRY_POLICY.value),
            RETRY_POLICY,
        ),
    )


@pytest.mark.proves("a-v3-node-execution-request-is-composed-not-supplied")
def test_the_manifest_carries_the_declared_members_in_their_declared_order() -> None:
    """The container is what the hash covers, so order and selector are in it."""
    bound = bind_node_execution(
        RUN, revision_hash(), graph(), "implement", fully_resolved()
    )

    assert bound.context_package == declared_context_package_of(
        revision_hash(),
        RUN,
        "implement",
        (
            ContextPackageMember("style", HOUSE_STYLE, "sections/plating"),
            ContextPackageMember("notes", KITCHEN_NOTES, "sections/timing"),
        ),
    )
    assert bound.request.context_package_hash == bound.context_package.package_hash


@pytest.mark.proves("a-v3-node-execution-request-is-composed-not-supplied")
def test_a_swapped_member_order_is_another_package() -> None:
    """A re-ordered member is visible in the package hash, as ADR 0006 requires."""
    ordered = declared_context_package_of(
        revision_hash(),
        RUN,
        "implement",
        (
            ContextPackageMember("style", HOUSE_STYLE, "sections/plating"),
            ContextPackageMember("notes", KITCHEN_NOTES, "sections/timing"),
        ),
    )
    swapped = declared_context_package_of(
        revision_hash(),
        RUN,
        "implement",
        (
            ContextPackageMember("notes", KITCHEN_NOTES, "sections/timing"),
            ContextPackageMember("style", HOUSE_STYLE, "sections/plating"),
        ),
    )

    assert ordered.package_hash != swapped.package_hash


@pytest.mark.proves("a-v3-node-execution-request-is-composed-not-supplied")
def test_the_request_binds_what_the_frozen_configuration_resolved() -> None:
    """The request is the document read through one snapshot, not a second answer."""
    bound = bind_node_execution(
        RUN, revision_hash(), graph(), "implement", fully_resolved()
    )

    assert bound.request.kind is NodeKindV3.AGENT
    assert bound.request.mode is AgentExecutionCapability.HEADLESS
    assert bound.request.node_id == "implement"
    assert bound.request.run_id == RUN
    assert bound.request.bound_revisions.retry == RETRY_POLICY
    assert bound.request.bound_revisions.profile is None
    assert (
        bound.request.run_configuration_revision_hash == fully_resolved().revision_hash
    )


@pytest.mark.proves("a-v3-node-execution-request-is-composed-not-supplied")
def test_a_reference_the_configuration_did_not_resolve_is_refused_by_its_site() -> None:
    """Dropping it would describe a smaller task than the author wrote."""
    incomplete = configuration(_context("style", HOUSE_STYLE))

    with pytest.raises(NodeExecutionUnresolved) as refused:
        bind_node_execution(RUN, revision_hash(), graph(), "implement", incomplete)

    assert refused.value.site.entry == "notes"
    assert refused.value.kind is RevisionKind.CONTEXT_SOURCE


@pytest.mark.proves("a-v3-node-execution-request-is-composed-not-supplied")
def test_a_configuration_frozen_for_another_revision_is_refused() -> None:
    """A snapshot of another document describes another run's resolutions."""
    other = RunConfigurationRevision(
        WorkflowRevisionHash(PublishedRevisionHash.of(b"another document").value),
        AgentBindingSet(()).binding_set_hash,
        (),
    )

    with pytest.raises(ValueError, match="another workflow revision"):
        bind_node_execution(RUN, revision_hash(), graph(), "implement", other)


TWO_SKILL_DOCUMENT = f"""format_version: 3
name: One agent with two skills
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Cook the one thing this package is for.
    skills:
      - ref: plating
        revision: {HOUSE_STYLE.value}
      - ref: timing
        revision: {KITCHEN_NOTES.value}
""".encode()


@pytest.mark.proves("a-v3-node-execution-request-is-composed-not-supplied")
def test_more_bound_revisions_of_one_kind_than_the_record_holds_is_refused() -> None:
    """A record that cannot carry them must say so, never bind fewer in silence.

    ADR 0006 binds one resolved skill id and one tool id per request, while the
    document may declare a sequence of each. Two skills therefore cannot be
    written down, and the answer is the node's name and the count -- dropping
    them would run an agent under capabilities its author declared and the
    receipt would never show it.
    """
    parsed = parse_workflow_document(TWO_SKILL_DOCUMENT)
    assert isinstance(parsed, WorkflowGraphV3)
    revision = WorkflowRevisionHash(
        WorkflowRevision(TWO_SKILL_DOCUMENT).revision_hash.value
    )
    frozen = RunConfigurationRevision(
        revision,
        AgentBindingSet(()).binding_set_hash,
        (
            ResolvedReference(
                ReferenceSite("skills", "implement"),
                RevisionKind.SKILL,
                VersionedReference(ref="plating", revision=HOUSE_STYLE.value),
                HOUSE_STYLE,
            ),
            ResolvedReference(
                ReferenceSite("skills", "implement"),
                RevisionKind.SKILL,
                VersionedReference(ref="timing", revision=KITCHEN_NOTES.value),
                KITCHEN_NOTES,
            ),
        ),
    )

    with pytest.raises(NodeExecutionBindingUnsupported) as refused:
        bind_node_execution(RUN, revision, parsed, "implement", frozen)

    assert refused.value.kind is RevisionKind.SKILL
    assert refused.value.declared == 2
