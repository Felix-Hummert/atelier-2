"""Publishing an adapter-operation revision: exact bytes in, the catalog's write, hash out.

The catalog store already publishes any kind. This use-case is the door that
kind `adapter_operation` was missing: it reads the bytes against the one owner
that knows what an operation this runtime performs is, then asks the store that
already owns the write. It does not invent a second publication.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.adapter_operations_v3 import (
    AdapterOperationRefused,
    read_adapter_operation_document,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.published_revisions import (
    PublishedRevisionCollision,
    PublishedRevisionCreated,
    PublishedRevisionExisting,
    PublishedRevisionRegistry,
)


@dataclass(frozen=True)
class AdapterOperationPublicationCreated:
    revision: PublishedRevision


@dataclass(frozen=True)
class AdapterOperationPublicationExisting:
    revision: PublishedRevision


@dataclass(frozen=True)
class AdapterOperationPublicationInvalid:
    verdict: AdapterOperationRefused


@dataclass(frozen=True)
class AdapterOperationPublicationCollision:
    pass


type PublishAdapterOperationRevisionResult = (
    AdapterOperationPublicationCreated
    | AdapterOperationPublicationExisting
    | AdapterOperationPublicationInvalid
    | AdapterOperationPublicationCollision
    | WriteUnavailable
    | DurableStateCorrupt
)


def publish_adapter_operation_revision(
    document: bytes, registry: PublishedRevisionRegistry
) -> PublishAdapterOperationRevisionResult:
    verdict = read_adapter_operation_document(document)
    if isinstance(verdict, AdapterOperationRefused):
        return AdapterOperationPublicationInvalid(verdict)
    revision = PublishedRevision(RevisionKind.ADAPTER_OPERATION, document)
    result = registry.publish_revision(revision)
    match result:
        case PublishedRevisionCreated(stored):
            return AdapterOperationPublicationCreated(stored)
        case PublishedRevisionExisting(stored):
            return AdapterOperationPublicationExisting(stored)
        case PublishedRevisionCollision():
            return AdapterOperationPublicationCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
