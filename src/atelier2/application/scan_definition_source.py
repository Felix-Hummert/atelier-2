"""What a registered source holds right now, compared against what came in.

A scan writes nothing. It resolves the configured ref to one commit, reads
every selected file of that commit, and says per path whether the catalog
already holds those bytes. That is the whole of it: the operator sees a newer
version exists and decides, and no commit ever enters the catalog because
somebody looked (`#660` ruled lines 2 and 12).

Every selected file is put through `read_publishable_workflow`, the same door
an intake would use, so a scan already says what an intake would refuse instead
of discovering it halfway through the batch. The refusal is repeated in the
publication door's own words rather than renamed here.

That it writes nothing is the shape of what it is handed: the durable side it
takes is `DefinitionSourceRegistry`, which has no door that writes.

What it validated is part of what it answers, because an intake of the scanned
commit needs exactly those bytes. Reaching for them again would read a source
that may have moved between the two reads, and would publish a commit nobody
was shown.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from atelier2.application.publish_workflow_revision import (
    PublicationInvalid,
    PublishableWorkflow,
    WorkflowPublicationLimits,
    read_publishable_workflow,
)
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.definition_sources import (
    DefinitionSourceId,
    DefinitionSourceRefusal,
    DefinitionSourceRevision,
    RepositoryPath,
    SourceCommit,
    SourceIntake,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.workflow_refusals import WorkflowRefusal
from atelier2.ports.definition_sources import (
    DefinitionSourceFound,
    DefinitionSourceMissing,
    DefinitionSourceReader,
    DefinitionSourceRegistry,
    DefinitionSourceUnreadable,
    SelectedFile,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.workflow_revisions import WorkflowDocumentParser


class PathFreshness(StrEnum):
    """How one selected path stands between the source and the catalog."""

    IN_SYNC = "in_sync"
    SOURCE_AHEAD = "source_ahead"
    SOURCE_ABSENT = "source_absent"


@dataclass(frozen=True)
class ScannedPath:
    """One path of the resolved commit, and where the catalog stands on it.

    `revision_hash` is the identity the bytes would publish under and is absent
    exactly when the source no longer carries the path: `source_absent` says
    the catalog holds a history the source stopped serving, and inventing a
    hash for a file that is not there would be a lie about what was read.
    """

    path: RepositoryPath
    kind: RevisionKind
    freshness: PathFreshness
    revision_hash: PublishedRevisionHash | None


@dataclass(frozen=True)
class DefinitionSourceScanned:
    """One write-free reading of a registered source.

    `carried` holds every path the commit serves, validated once, in the order
    the source served them. A path the source stopped carrying is in `paths`
    and not here: there are no bytes to hold for it.
    """

    revision: DefinitionSourceRevision
    commit: SourceCommit
    paths: tuple[ScannedPath, ...]
    carried: Mapping[RepositoryPath, PublishableWorkflow]


@dataclass(frozen=True)
class ScanRefused:
    """The source could not be read, in its own closed vocabulary."""

    refusal: DefinitionSourceRefusal
    detail: str


@dataclass(frozen=True)
class ScannedDocumentInvalid:
    """A selected file would not pass the door an intake puts it through."""

    path: RepositoryPath
    detail: str
    refusal: WorkflowRefusal | None


@dataclass(frozen=True)
class DefinitionSourceUnknown:
    """No source is registered under this id."""

    source_id: DefinitionSourceId


type ScanDefinitionSourceResult = (
    DefinitionSourceScanned
    | ScanRefused
    | ScannedDocumentInvalid
    | DefinitionSourceUnknown
    | ReadUnavailable
    | DurableStateCorrupt
)


def scan_definition_source(
    source_id: DefinitionSourceId,
    sources: DefinitionSourceRegistry,
    reader: DefinitionSourceReader,
    parser: WorkflowDocumentParser,
    limits: WorkflowPublicationLimits,
) -> ScanDefinitionSourceResult:
    """Say where the source stands, having written nothing to reach the answer."""

    registered = sources.read_source(source_id)
    match registered:
        case DefinitionSourceMissing(missing):
            return DefinitionSourceUnknown(missing)
        case DurableWriteUnavailable():
            return ReadUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case DefinitionSourceFound(revision):
            pass
        case _ as unreachable:
            assert_never(unreachable)
    try:
        scanned = reader.scan(revision.configuration)
    except DefinitionSourceUnreadable as refused:
        return ScanRefused(refused.refusal, refused.detail)
    carried = _validated(scanned.files, parser, limits)
    if isinstance(carried, ScannedDocumentInvalid):
        return carried
    intaken = sources.latest_intakes(source_id)
    if isinstance(intaken, DurableWriteUnavailable):
        return ReadUnavailable()
    if isinstance(intaken, PortDurableStateCorrupt):
        return DurableStateCorrupt()
    return DefinitionSourceScanned(
        revision,
        scanned.commit,
        _compared(scanned.files, carried, intaken),
        carried,
    )


def _validated(
    files: tuple[SelectedFile, ...],
    parser: WorkflowDocumentParser,
    limits: WorkflowPublicationLimits,
) -> Mapping[RepositoryPath, PublishableWorkflow] | ScannedDocumentInvalid:
    """Every selected file put through the intake door, or the first refusal.

    The whole scan stops at one refused file rather than reporting the rest:
    an intake of this commit would refuse the batch whole, and a scan that
    listed the other paths as ready would promise something the intake door
    will not do.
    """

    read: dict[RepositoryPath, PublishableWorkflow] = {}
    for selected in files:
        publishable = read_publishable_workflow(selected.document, parser, limits)
        if isinstance(publishable, PublicationInvalid):
            return ScannedDocumentInvalid(
                selected.path, publishable.detail, publishable.refusal
            )
        read[selected.path] = publishable
    return read


def published_hash(publishable: PublishableWorkflow) -> PublishedRevisionHash:
    """The catalog identity of bytes the workflow door already accepted.

    One derivation for the scan that compares it and the intake that admits
    under it, so the two can never name one file by two hashes.
    """

    return PublishedRevisionHash(publishable.revision.revision_hash.value)


def _compared(
    files: tuple[SelectedFile, ...],
    carried: Mapping[RepositoryPath, PublishableWorkflow],
    intaken: Mapping[RepositoryPath, SourceIntake],
) -> tuple[ScannedPath, ...]:
    """Every path the source carries, then every path only the catalog holds.

    A path the source stopped serving is reported, never retired: what came in
    stays what it was, and only the operator decides what that means
    (`#660` ruled lines 7 and 17).
    """

    served = tuple(
        ScannedPath(
            selected.path,
            selected.selection.kind,
            _freshness(
                published_hash(carried[selected.path]), intaken.get(selected.path)
            ),
            published_hash(carried[selected.path]),
        )
        for selected in files
    )
    absent = tuple(
        ScannedPath(path, intake.revision_kind, PathFreshness.SOURCE_ABSENT, None)
        for path, intake in sorted(intaken.items(), key=lambda item: item[0].value)
        if path not in carried
    )
    return served + absent


def _freshness(
    published: PublishedRevisionHash, intake: SourceIntake | None
) -> PathFreshness:
    if intake is None or intake.revision_hash != published:
        return PathFreshness.SOURCE_AHEAD
    return PathFreshness.IN_SYNC
