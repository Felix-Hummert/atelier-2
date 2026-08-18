"""The store side of ADR 0006's V3 node record family.

**Why this module exists.** The six tables landed with #63 as tables, triggers
and typed contracts, and then stood empty: the writer that filled them was a
callerless foundation door, removed with the seam it hung on. This is the family's
production writer, and it has exactly two moments -- the serialized start, where
one bound execution per node becomes a durable request and the package it names,
and the terminal write, where a node execution gets the one receipt ADR 0006 gives
it. Nothing else writes these tables.

**Why the two moments are separate.** A request says what a node was asked to do
and is decided before anything runs; a receipt says how that ended and cannot be
written until it has. Writing them together would mean either a request nobody
could point a receipt at until the node ran, or a receipt whose request was
invented at the same instant -- and the composite key that binds them exists
precisely so a receipt cannot name a request nobody wrote.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from atelier2.adapters.dbos.schema import (
    context_packages_v3,
    node_artifacts_v3,
    node_execution_requests_v3,
    node_receipt_outputs_v3,
    node_receipts_v3,
)
from atelier2.application.bind_node_execution import bind_node_execution
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.node_records_v3 import (
    DeclaredContextPackageHash,
    NodeArtifact,
    NodeExecutionRequestHash,
    NodeReceipt,
    PersistedReceiptDisposition,
    ReceiptOutput,
    RunInput,
)
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevision
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import WorkflowGraphV3


class NodeReceiptConflict(RuntimeError):
    """The store already holds a different receipt for this node execution.

    One node execution gets one terminal receipt, and a second write that means
    something else is the store contradicting itself -- surfaced, never absorbed
    by the insert's identity rule.
    """


def persist_bound_node_executions(
    connection: Any,
    run_id: RunId,
    workflow_revision_hash: WorkflowRevisionHash,
    graph: WorkflowGraphV3,
    run_configuration: RunConfigurationRevision,
    orders: tuple[RunInput, ...],
) -> None:
    """Write what every node of this run was asked to do, before any of them runs.

    Every node, not only the entry one: ADR 0006 writes the package before START,
    and a receipt can only bind a request that already exists -- so a chain whose
    later nodes had no request would be a chain whose later nodes could not be
    receipted.

    `OR IGNORE` is the identity rule, not a swallow. Both records are addressed by
    a hash over their own bytes, so a second start of the same run under the same
    revision, configuration and orders presents byte-identical records; a differing
    one is a differing hash and a new row. What refuses a repeat that means
    something else is the run identity check this write sits behind.
    """
    for node in graph.nodes:
        bound = bind_node_execution(
            run_id,
            workflow_revision_hash,
            graph,
            node.id,
            run_configuration,
            orders,
        )
        connection.execute(
            context_packages_v3.insert()
            .prefix_with("OR IGNORE")
            .values(
                package_hash=bound.context_package.package_hash.value,
                manifest=bound.context_package.manifest,
            )
        )
        connection.execute(
            node_execution_requests_v3.insert()
            .prefix_with("OR IGNORE")
            .values(
                request_hash=bound.request.request_hash.value,
                node_execution_id=NodeExecutionId.for_node(
                    run_id, workflow_revision_hash, node.id
                ).value,
                run_configuration_revision_hash=(run_configuration.revision_hash.value),
                context_package_hash=bound.context_package.package_hash.value,
                preimage=bound.request.preimage,
            )
        )


def keep_node_receipt(
    connection: Any,
    node_execution_id: NodeExecutionId,
    disposition: PersistedReceiptDisposition,
    reason: str,
    artifact: NodeArtifact | None = None,
) -> NodeReceipt | None:
    """The one terminal receipt of one node execution, with its value where it has one.

    The request and package hashes are read from the durable request rather than
    recomputed: recomputing would put a second author on the record the receipt
    binds, and two authors of one hash is exactly what the composite key refuses.

    A run started before this writer existed carries no durable request, and its
    execution stays honestly receipt-less -- returning ``None`` -- rather than
    receipting a request nobody wrote. A run started by this writer cannot lack
    the request: both rows leave the same start transaction as the run itself.

    A succeeded execution keeps its produced value as `node-artifact/v3` first,
    because the receipt's output row is bound to that artifact by key -- the value
    a receipt names is the value the store holds, or neither exists.
    """
    bound = _bound_request(connection, node_execution_id)
    if bound is None:
        return None
    request_hash, package_hash = bound
    outputs: tuple[ReceiptOutput, ...] = ()
    if artifact is not None:
        if artifact.node_execution_id != node_execution_id:
            raise ValueError("the artifact belongs to another node execution")
        connection.execute(
            node_artifacts_v3.insert()
            .prefix_with("OR IGNORE")
            .values(
                run_id=artifact.run_id.value,
                node_id=artifact.node_id,
                node_execution_id=artifact.node_execution_id.value,
                output_name=artifact.output_name,
                schema_revision_hash=artifact.schema_revision.value,
                value=artifact.value,
                value_hash=artifact.value_hash.value,
                artifact_hash=artifact.artifact_hash.value,
            )
        )
        outputs = (
            ReceiptOutput(
                artifact.output_name, artifact.schema_revision, artifact.value_hash
            ),
        )
    receipt = NodeReceipt(
        node_execution_id,
        disposition,
        reason,
        request_hash,
        package_hash,
        outputs,
    )
    connection.execute(
        node_receipts_v3.insert()
        .prefix_with("OR IGNORE")
        .values(
            node_execution_id=receipt.node_execution_id.value,
            disposition=receipt.disposition.value,
            reason=receipt.reason,
            request_hash=receipt.request_hash.value,
            context_package_hash=receipt.context_package_hash.value,
            receipt_hash=receipt.receipt_hash.value,
        )
    )
    stored_hash = connection.scalar(
        sa.select(node_receipts_v3.c.receipt_hash).where(
            node_receipts_v3.c.node_execution_id == receipt.node_execution_id.value
        )
    )
    if str(stored_hash) != receipt.receipt_hash.value:
        raise NodeReceiptConflict(
            f"node execution {receipt.node_execution_id.value} already carries "
            "a different terminal node-receipt/v3"
        )
    for position, output in enumerate(receipt.outputs):
        connection.execute(
            node_receipt_outputs_v3.insert()
            .prefix_with("OR IGNORE")
            .values(
                node_execution_id=receipt.node_execution_id.value,
                position=position,
                output_name=output.name,
                schema_revision_hash=output.schema_revision.value,
                value_hash=output.value_hash.value,
            )
        )
    return receipt


def _bound_request(
    connection: Any, node_execution_id: NodeExecutionId
) -> tuple[NodeExecutionRequestHash, DeclaredContextPackageHash] | None:
    record = (
        connection.execute(
            sa.select(
                node_execution_requests_v3.c.request_hash,
                node_execution_requests_v3.c.context_package_hash,
            ).where(
                node_execution_requests_v3.c.node_execution_id
                == node_execution_id.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    return (
        NodeExecutionRequestHash(str(record["request_hash"])),
        DeclaredContextPackageHash(str(record["context_package_hash"])),
    )
