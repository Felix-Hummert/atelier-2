"""Record one named workflow's supervised start, against this engine's own truth.

**Why this is an application use-case and not a host entry.** It decides one
thing over ports and touches no process, socket or terminal, which is the
application layer's own shape; the host wiring that will call it is #111's, and
until that exists a host module would be a layer boundary with nothing on the
other side.

**Why it is in-process at all.** The existing `atelier2` run command is an HTTP
client: `RunOrder` carries a `service_url` and a document, so the write lands
inside the server's transaction, on the far side of a boundary this caller does
not own. A path built on it could prove that some server wrote something — never
that *this* name reached *these* bytes and that the whole record arrived
together. The atomicity sentence is what forces the seam, so the seam takes its
ports and stays on this side of them.

What this composes, and nothing else:

1. the catalog resolves a name to the exact published bytes of one admitted
   lineage member -- one call, because the membership proof lives inside it,
2. the caller's already-decided terminal truth is checked to be about *those*
   bytes,
3. the node execution request and the context package are **composed here**,
   from the resolved bytes and the run configuration the caller froze, and a
   decided truth that describes them differently is refused. The caller states
   the outcome; what the node was asked to do is not a caller's to assert,
4. Cut B's supervised start records the published revision, its workflow
   backing, every record those hashes name, the run, its artifacts and its
   terminal receipt in one transaction, or records none of them.

**No graph is interpreted here, and none is claimed.** Cut B's starter takes an
already-decided truth rather than producing one — it says so itself, "without
claiming an executable engine" — so this seam does not run a workflow either.
What it guarantees is which bytes a name reached and that the record of them is
whole. V3 execution is open and belongs to #194 H1.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.application.bind_node_execution import bind_node_execution
from atelier2.application.resolve_catalog_name import (
    CatalogNameResolved,
    CatalogNameResult,
    CatalogReferenceNonMember,
    resolve_catalog_name,
)
from atelier2.contracts.catalog_v3 import (
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.node_records_v3 import (
    NodeExecutionRequestHash,
    NodeReceiptHash,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflow_refusals import WorkflowDocumentInvalid
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
from atelier2.ports.durable_runs import (
    DurableV3RunCreated,
    DurableV3RunExisting,
    DurableV3RunStarter,
    DurableV3StartWithReceiptResult,
    StartV3RunWithReceiptRequest,
)
from atelier2.ports.published_revisions import CatalogLineageQuery, CatalogResolver
from atelier2.ports.workflow_revisions import WorkflowDocumentParser


@dataclass(frozen=True)
class NamedRunStarted:
    """The name reached these exact bytes, and their whole record was written."""

    run_id: RunId
    revision_hash: PublishedRevisionHash
    receipt_hash: NodeReceiptHash
    lineage_id: CatalogLineageId
    current_display_name: CatalogLineageDisplayName
    already_existed: bool


@dataclass(frozen=True)
class NamedRunNameUnresolved:
    """The name reached no startable revision, in the catalog's own words."""

    refusal: CatalogNameResult


@dataclass(frozen=True)
class NamedRunRevisionUnbound:
    """The name resolved, but its revision is no admitted member of its lineage."""

    lineage_id: CatalogLineageId
    revision_hash: PublishedRevisionHash


@dataclass(frozen=True)
class NamedRunTruthNotComposed:
    """The decided truth describes a request or package this document does not.

    Refused rather than corrected, for the same reason a truth for another
    revision is: the caller may say what happened, never what was asked.
    """

    composed_request_hash: NodeExecutionRequestHash
    decided_request_hash: NodeExecutionRequestHash


@dataclass(frozen=True)
class NamedRunTruthForAnotherRevision:
    """The decided truth describes bytes other than the ones the name reached.

    Refused rather than corrected: substituting the resolved revision would start
    a run whose result was decided for something else, which is exactly the wrong
    answer this door exists to make unreachable.
    """

    resolved: PublishedRevisionHash
    decided_for: PublishedRevisionHash


type NamedRunResult = (
    NamedRunStarted
    | NamedRunNameUnresolved
    | NamedRunRevisionUnbound
    | NamedRunTruthForAnotherRevision
    | NamedRunTruthNotComposed
    | NamedRunUnexecutableDocument
    | DurableV3StartWithReceiptResult
)


@dataclass(frozen=True)
class NamedRunUnexecutableDocument:
    """The name reached bytes this engine cannot read as a V3 document."""

    reason: str


def start_named_run(
    kind: RevisionKind,
    lineage_id_or_name: CatalogLineageQuery,
    position: object,
    decided: StartV3RunWithReceiptRequest,
    catalog: CatalogResolver,
    starter: DurableV3RunStarter,
    parse: WorkflowDocumentParser,
) -> NamedRunResult:
    """Start the exact revision this name holds, or write nothing and say why."""
    match resolve_catalog_name(kind, lineage_id_or_name, position, catalog):
        case CatalogNameResolved() as resolution:
            revision = resolution.revision
        case CatalogReferenceNonMember(lineage_id, revision_hash):
            return NamedRunRevisionUnbound(lineage_id, revision_hash)
        case _ as unresolved:
            return NamedRunNameUnresolved(unresolved)

    if decided.revision.revision_hash != revision.revision_hash:
        return NamedRunTruthForAnotherRevision(
            revision.revision_hash, decided.revision.revision_hash
        )

    try:
        graph = parse(revision.document)
        if not isinstance(graph, WorkflowGraphV3):
            return NamedRunUnexecutableDocument(
                f"the name reached a format {graph.format_version} document"
            )
        composed = bind_node_execution(
            decided.node_request.run_id,
            WorkflowRevisionHash(revision.revision_hash.value),
            graph,
            decided.node_request.node_id,
            decided.run_configuration,
        )
    except (WorkflowDocumentInvalid, KeyError, ValueError) as refused:
        return NamedRunUnexecutableDocument(str(refused))
    if composed.request.request_hash != decided.node_request.request_hash:
        return NamedRunTruthNotComposed(
            composed.request.request_hash, decided.node_request.request_hash
        )

    started = starter.start_v3_with_receipt(
        StartV3RunWithReceiptRequest(
            revision,
            decided.run_configuration,
            composed.request,
            composed.context_package,
            decided.artifacts,
            decided.receipt,
        )
    )
    match started:
        case DurableV3RunCreated(run_id, revision_hash, receipt_hash):
            existed = False
        case DurableV3RunExisting(run_id, revision_hash, receipt_hash):
            existed = True
        case _:
            return started
    return NamedRunStarted(
        run_id,
        revision_hash,
        receipt_hash,
        resolution.lineage_id,
        resolution.current_display_name,
        existed,
    )
