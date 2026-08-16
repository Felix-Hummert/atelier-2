"""Start one named workflow in this process, against this engine's own truth.

**Why a host function and not a CLI subcommand.** The existing `atelier2` run
command is an HTTP client: `RunOrder` carries a `service_url` and a document, and
the run write happens inside the server's transaction, on the far side of a
boundary this caller does not own. A subcommand built on it would prove that some
server wrote something — never that *this* name resolved to *these* bytes and that
the revision, the run and the receipt arrived together. The atomicity sentence is
what forces the entry point, so the entry is in-process and takes its ports.

The CLI is deliberately deferred, not forgotten: once this path is trusted, a
subcommand is a thin caller of it in the #114 pattern, and it belongs to #111.

What this composes, and nothing else:

1. the catalog resolves a name to one admitted lineage member,
2. that member resolves to its exact published bytes,
3. the caller's already-decided terminal truth is checked to be about *those*
   bytes,
4. Cut B's supervised start persists revision, run, artifacts and receipt in one
   transaction, or persists nothing.

It executes no graph. Cut B's starter takes a decided truth rather than producing
one, so this door does not claim an engine either — what it guarantees is which
bytes were started and that the record of them is whole.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.resolve_catalog_name import (
    CatalogNameResolved,
    CatalogNameResult,
    CatalogReferenceNonMember,
    CatalogReferenceResolved,
    resolve_catalog_name,
    resolve_catalog_reference,
)
from atelier2.contracts.catalog_v3 import (
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.node_records_v3 import NodeReceiptHash
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import RunId
from atelier2.ports.durable_runs import (
    DurableV3RunCreated,
    DurableV3RunExisting,
    DurableV3RunStarter,
    DurableV3StartWithReceiptResult,
    StartV3RunWithReceiptRequest,
)
from atelier2.ports.published_revisions import CatalogLineageQuery, CatalogResolver


@dataclass(frozen=True)
class NamedRunStarted:
    """One named workflow's exact bytes are started and their record is whole."""

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
    | DurableV3StartWithReceiptResult
)


def start_named_run(
    kind: RevisionKind,
    lineage_id_or_name: CatalogLineageQuery,
    position: object,
    decided: StartV3RunWithReceiptRequest,
    catalog: CatalogResolver,
    starter: DurableV3RunStarter,
) -> NamedRunResult:
    """Start the exact revision this name holds, or write nothing and say why."""
    resolution = resolve_catalog_name(kind, lineage_id_or_name, position, catalog)
    if not isinstance(resolution, CatalogNameResolved):
        return NamedRunNameUnresolved(resolution)

    match resolve_catalog_reference(
        kind, resolution.lineage_id, resolution.revision_hash, catalog
    ):
        case CatalogReferenceNonMember(lineage_id, revision_hash):
            return NamedRunRevisionUnbound(lineage_id, revision_hash)
        case CatalogReferenceResolved(revision):
            pass
        case _ as unreachable:
            assert_never(unreachable)

    if decided.revision.revision_hash != revision.revision_hash:
        return NamedRunTruthForAnotherRevision(
            revision.revision_hash, decided.revision.revision_hash
        )

    started = starter.start_v3_with_receipt(
        StartV3RunWithReceiptRequest(
            revision, decided.node_request, decided.artifacts, decided.receipt
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
