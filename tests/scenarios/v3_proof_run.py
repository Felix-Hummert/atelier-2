"""One proof workflow and the terminal truth a supervised start persists for it.

Cut B's starter takes an already-decided node truth rather than executing one, so
a scenario that wants a real start has to state that truth. It is built here from
the production contracts alone — never by copying what the adapter would derive —
so a test measures the composition rather than its own arithmetic.
"""

from __future__ import annotations

from dataclasses import replace

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.bind_node_execution import bind_node_execution
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    NodeArtifact,
    NodeReceipt,
    PersistedReceiptDisposition,
    ReceiptOutput,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_configuration_v3 import (
    ReferenceSite,
    ResolvedReference,
    RunConfigurationRevision,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import VersionedReference, WorkflowGraphV3
from atelier2.ports.durable_runs import StartV3RunWithReceiptRequest

PROOF_SCHEMA_REVISION = PublishedRevisionHash.of(b"the bound meal schema")

PROOF_WORKFLOW_DOCUMENT = f"""format_version: 3
name: Lasagne
description: Cook one supervised proof.
nodes:
  - id: cook
    type: action
    operation:
      ref: cook-operation
      revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    outputs:
      - name: meal
        schema:
          ref: meal-schema
          revision: {PROOF_SCHEMA_REVISION.value}
""".encode()

PROOF_RUN_ID = RunId("run-lasagne")


def decided_truth_for(
    revision: PublishedRevision, *, break_receipt_binding: bool = False
) -> StartV3RunWithReceiptRequest:
    """The terminal truth for one cooked proof run of this exact revision.

    The frozen resolution matrix is this scenario's own data; the manifest and
    the request that names it come from `bind_node_execution`, so the scenario
    states what the document resolved to and never recomputes what production
    derives from it.

    `break_receipt_binding` points the receipt at another node execution, which is
    how a caller asks for the all-or-nothing case without reaching into the store.
    """
    workflow_hash = WorkflowRevisionHash(revision.revision_hash.value)
    execution_id = NodeExecutionId.for_node(PROOF_RUN_ID, workflow_hash, "cook")
    graph = parse_workflow_document(revision.document)
    assert isinstance(graph, WorkflowGraphV3)
    frozen = RunConfigurationRevision(
        workflow_hash,
        AgentBindingSet(()).binding_set_hash,
        (
            ResolvedReference(
                ReferenceSite("outputs.schema", "cook", "meal"),
                RevisionKind.SCHEMA,
                VersionedReference(
                    ref="meal-schema", revision=PROOF_SCHEMA_REVISION.value
                ),
                PROOF_SCHEMA_REVISION,
            ),
        ),
    )
    bound = bind_node_execution(PROOF_RUN_ID, workflow_hash, graph, "cook", frozen)
    node_request = bound.request
    artifact = NodeArtifact(
        run_id=PROOF_RUN_ID,
        node_id="cook",
        node_execution_id=execution_id,
        output_name="meal",
        schema_revision=PROOF_SCHEMA_REVISION,
        value=b"lasagne",
    )
    receipt = NodeReceipt(
        node_execution_id=execution_id,
        disposition=PersistedReceiptDisposition.SUCCEEDED,
        reason="completed",
        request_hash=node_request.request_hash,
        context_package_hash=node_request.context_package_hash,
        outputs=(ReceiptOutput("meal", PROOF_SCHEMA_REVISION, artifact.value_hash),),
        access_receipt_hashes=(Sha256Hash.of(b"oven access"),),
    )
    if break_receipt_binding:
        receipt = replace(
            receipt,
            node_execution_id=NodeExecutionId.for_node(
                PROOF_RUN_ID, workflow_hash, "another_node"
            ),
        )
    return StartV3RunWithReceiptRequest(
        revision, frozen, node_request, bound.context_package, (), (artifact,), receipt
    )
