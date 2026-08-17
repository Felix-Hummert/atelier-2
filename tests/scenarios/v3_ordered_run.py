"""One workflow that takes an order, and the supervised start that carries it.

The document declares a graph input and one node reads it, which is the shape
#38's first sentence is about: the same published revision serves every order,
because the order arrives as material rather than inside the bytes.

Everything derived -- the manifest, the request, the envelopes -- comes from the
production author, so a test measures the composition and never its own
arithmetic. What the scenario states is only what an operator states: which
revision, which name, which bytes.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.bind_node_execution import bind_node_execution
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.node_records_v3 import (
    NodeArtifact,
    NodeReceipt,
    PersistedReceiptDisposition,
    ReceiptOutput,
    RunInput,
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
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)

ORDER_SCHEMA_DOCUMENT = (
    b'{"type": "object", "properties": {"portions": {"type": "integer", '
    b'"minimum": 1}}, "required": ["portions"], "additionalProperties": false}'
)
MEAL_SCHEMA_DOCUMENT = b'{"type": "string"}'
ORDER_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, ORDER_SCHEMA_DOCUMENT)
MEAL_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, MEAL_SCHEMA_DOCUMENT)

ORDER_NAME = "order"
ORDER_VALUE = b'{"portions": 4}'
ORDERED_RUN_ID = RunId("run-to-order")

ORDERED_DOCUMENT = f"""format_version: 3
name: Cook to order
description: One revision, every order.
graph_inputs:
  - name: {ORDER_NAME}
    schema:
      ref: order-schema
      revision: {ORDER_SCHEMA.revision_hash.value}
nodes:
  - id: cook
    type: action
    operation:
      ref: cook-operation
      revision: {"a" * 64}
    inputs:
      - name: {ORDER_NAME}
        from:
          graph_input: {ORDER_NAME}
    outputs:
      - name: meal
        schema:
          ref: meal-schema
          revision: {MEAL_SCHEMA.revision_hash.value}
""".encode()


def ordered_revision() -> PublishedRevision:
    return PublishedRevision(RevisionKind.WORKFLOW, ORDERED_DOCUMENT)


def publish_order_schemas(engine: Engine) -> None:
    """Publish the schemas the document pins, through the real publisher.

    The start validates an order against the schema its document named, so those
    bytes have to be published first -- exactly as they would be for a workflow
    an operator authored.
    """
    store = DbosCatalogStore(engine)
    for revision in (ORDER_SCHEMA, MEAL_SCHEMA):
        published = store.publish_revision(revision)
        assert isinstance(
            published, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), published


def _resolved(
    field: str, entry: str | None, revision: PublishedRevisionHash
) -> ResolvedReference:
    return ResolvedReference(
        ReferenceSite(
            field, None if field.startswith("graph_inputs") else "cook", entry
        ),
        RevisionKind.SCHEMA,
        VersionedReference(ref=f"{entry}-schema", revision=revision.value),
        revision,
    )


def ordered_configuration(revision: PublishedRevision) -> RunConfigurationRevision:
    """What the document's references resolved to, as the caller froze them."""
    workflow_hash = WorkflowRevisionHash(revision.revision_hash.value)
    return RunConfigurationRevision(
        workflow_hash,
        AgentBindingSet(()).binding_set_hash,
        (
            _resolved("graph_inputs.schema", ORDER_NAME, ORDER_SCHEMA.revision_hash),
            _resolved("outputs.schema", "meal", MEAL_SCHEMA.revision_hash),
        ),
    )


def ordered_run_input() -> RunInput:
    return RunInput(ORDER_NAME, ORDER_SCHEMA.revision_hash, ORDER_VALUE)


def ordered_truth_for(revision: PublishedRevision) -> StartV3RunWithReceiptRequest:
    """The decided truth for one cooked order of this exact revision."""
    workflow_hash = WorkflowRevisionHash(revision.revision_hash.value)
    execution_id = NodeExecutionId.for_node(ORDERED_RUN_ID, workflow_hash, "cook")
    graph = parse_workflow_document(revision.document)
    assert isinstance(graph, WorkflowGraphV3)
    frozen = ordered_configuration(revision)
    supplied = (ordered_run_input(),)
    bound = bind_node_execution(
        ORDERED_RUN_ID, workflow_hash, graph, "cook", frozen, supplied
    )
    artifact = NodeArtifact(
        run_id=ORDERED_RUN_ID,
        node_id="cook",
        node_execution_id=execution_id,
        output_name="meal",
        schema_revision=MEAL_SCHEMA.revision_hash,
        value=b'"lasagne for four"',
    )
    receipt = NodeReceipt(
        node_execution_id=execution_id,
        disposition=PersistedReceiptDisposition.SUCCEEDED,
        reason="completed",
        request_hash=bound.request.request_hash,
        context_package_hash=bound.request.context_package_hash,
        outputs=(
            ReceiptOutput("meal", MEAL_SCHEMA.revision_hash, artifact.value_hash),
        ),
    )
    return StartV3RunWithReceiptRequest(
        revision,
        frozen,
        bound.request,
        bound.context_package,
        supplied,
        (artifact,),
        receipt,
    )
