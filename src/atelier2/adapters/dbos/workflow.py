from __future__ import annotations

from collections.abc import Mapping
from typing import Any, assert_never, cast

import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID, SQLAlchemyDatasource

from atelier2.adapters.agent_processes import AgentProcessSupervisor
from atelier2.adapters.dbos.advancer import prepare_graph_action
from atelier2.adapters.dbos.continuation import (
    checkpoint_confirmed_action,
    schedule_confirmed_action_continuation,
)
from atelier2.adapters.dbos.effect_store import (
    EncodedEffectResolution,
    commit_resolution,
    observe_adapter,
    observe_reconcile_command,
    resolve_observation,
)
from atelier2.adapters.dbos.names import (
    ACTION_CONTINUATION_WORKFLOW_NAME,
    ACTION_PREPARE_STEP_NAME,
    AGENT_COMMIT_STEP_NAME,
    ANSWER_COMMIT_STEP_NAME,
    ANSWER_WORKFLOW_NAME,
    BOOTSTRAP_STEP_NAME,
    CANCELLATION_WORKFLOW_NAME,
    COMMIT_STEP_NAME,
    EFFECT_WORKFLOW_NAME,
    NODE_BINDING_STEP_NAME,
    NODE_WORKFLOW_NAME,
    OBSERVE_STEP_NAME,
    RECONCILE_WORKFLOW_NAME,
    REPLACEMENT_WORKFLOW_NAME,
    RESOLVE_STEP_NAME,
    SUBWORKFLOW_COMMIT_STEP_NAME,
    SUBWORKFLOW_WORKFLOW_NAME,
    WAIT_COMMIT_STEP_NAME,
    WORKFLOW_NAME,
)
from atelier2.adapters.dbos.node_binding_codec import (
    EncodedNodeBinding,
    decode_node_binding,
    encode_node_binding,
)
from atelier2.adapters.dbos.run_store import (
    commit_agent_completed,
    commit_subworkflow_completed,
    commit_wait_answered,
    entry_node_of,
    load_node_outputs,
    load_published_schema_document,
    load_run_inputs,
    load_wait_answer,
)
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    commit_waiting_input,
    load_graph,
    load_run,
)
from atelier2.adapters.dbos.schema import published_revisions
from atelier2.adapters.dbos.workflow_ids import (
    effect_workflow_id_for,
    node_workflow_id_for,
    subworkflow_workflow_id_for,
)
from atelier2.application.bind_node import (
    agent_execution_request,
    agent_execution_request_v2,
    bind_node,
    pinned_project,
    require_the_run_stands_on,
)
from atelier2.application.cancel_agent_attempt import (
    continue_agent_attempt_cancellation,
)
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agents import (
    AgentExecutionCapability,
    AgentExecutorBinding,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.budgets_v3 import (
    BudgetRevisionRefused,
    read_budget_revision_document,
)
from atelier2.contracts.effects import (
    EffectAdapterBinding,
    LogicalEffectKey,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    NodeExecutionId,
)
from atelier2.contracts.node_bindings import (
    ActionNodeBinding,
    AgentNodeBinding,
    AgentNodeBindingV2,
    SubworkflowNodeBinding,
    WaitNodeBinding,
)
from atelier2.contracts.node_records_v3 import DeliveredOutput, RunInput
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_bindings import RunBindingConflict
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    ToolGrantRefused,
    read_tool_grant_document,
)
from atelier2.contracts.workflows import (
    AgentNodeV2,
    RunCompletes,
    RunContinues,
)
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    AnyWorkflowDocument,
    AnyWorkflowDocumentNode,
)
from atelier2.ports.agent_attempts import AgentAttemptStore, AgentAttemptSucceeded
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceOwner,
    AgentExecutor,
    AgentExecutorKey,
    AgentExecutorV2,
)
from atelier2.ports.effects import EffectAdapter
from atelier2.ports.project_verification import (
    DeclaredProject,
)

CANCELLATION_REDRIVE_SECONDS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0)


def _declared_workspace_owner(
    owner: AgentAttemptWorkspaceOwner | None,
) -> AgentAttemptWorkspaceOwner:
    if owner is None:
        raise RunBindingConflict(
            "a V2 agent node requires the declared agent scratch root"
        )
    return owner


def bootstrap_run_binding(
    datasource: SQLAlchemyDatasource,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
) -> str:
    def load_binding() -> str:
        session = datasource.sql_session()
        run = load_run(session, run_id)
        if run.revision_hash != revision_hash:
            raise RunBindingConflict("bootstrap requires its exact durable run binding")
        graph = load_graph(session, revision_hash)
        entry = entry_node_of(graph)
        if (
            run.state is not RunState.STARTED
            or run.current_node_id != entry
            or run.state_version != 0
            or run.last_event_sequence != 0
        ):
            raise RunBindingConflict("bootstrap requires its exact new durable run")
        return entry

    return str(datasource.run_tx_step({"name": BOOTSTRAP_STEP_NAME}, load_binding))


def _run_effect_step(
    datasource: SQLAlchemyDatasource,
    name: str,
    operation: Any,
    *arguments: Any,
) -> EncodedEffectResolution:
    def execute() -> EncodedEffectResolution:
        return operation(datasource.sql_session(), *arguments)

    return datasource.run_tx_step({"name": name}, execute)


def _node_binding(
    datasource: SQLAlchemyDatasource,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    project: DeclaredProject | None,
) -> EncodedNodeBinding:
    """Record what this node durably binds, including the source it is pinned to.

    The pin is taken here and nowhere else. Composing the binding is the one
    moment a node's material is decided, so resolving the project's head again at
    launch time would let a commit landing between the two silently change what a
    started run works on -- and a recovered node replays the binding this step
    recorded rather than resolving anything a second time.
    """

    def load() -> EncodedNodeBinding:
        session = datasource.sql_session()
        run = load_run(session, run_id)
        require_the_run_stands_on(run, revision_hash, node_id)
        graph = load_graph(session, revision_hash)
        node = graph.node(node_id)
        orders, results = _agent_material(
            session, run_id, revision_hash, graph, node, run.current_round_ordinal
        )
        return encode_node_binding(
            bind_node(
                run,
                node,
                orders=orders,
                results=results,
                tool_grant=_pinned_tool_grant(session, node),
                declared_output_schema_document=_declared_output_schema_document(
                    session, node
                ),
                maximum_assistant_turns=_pinned_maximum_assistant_turns(session, node),
                project_source=_pinned_source(node, project),
            )
        )

    return cast(
        EncodedNodeBinding,
        datasource.run_tx_step({"name": NODE_BINDING_STEP_NAME}, load),
    )


def _agent_material(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    graph: AnyWorkflowDocument,
    node: AnyWorkflowDocumentNode,
    round_ordinal: int,
) -> tuple[tuple[RunInput, ...], tuple[DeliveredOutput, ...]]:
    """The orders and earlier results a V3 Agent node reads, and nothing for the rest.

    Read here rather than inside the decision because reading them is a store
    call, and read only for the one node kind whose document can declare them.

    The round travels with the read because a looped producer wrote one value per
    round: without it the store would be asked for a node's output and hold
    several.
    """
    if not isinstance(node, AgentNodeV3):
        return ((), ())
    return (
        load_run_inputs(session, run_id, node),
        load_node_outputs(session, run_id, revision_hash, graph, node, round_ordinal),
    )


def _pinned_source(
    node: AnyWorkflowDocumentNode, project: DeclaredProject | None
) -> ProjectSourcePin | None:
    """The source this runtime pins for one Agent node, resolved once and here.

    Only an Agent node works in a tree, so only an Agent node's binding takes the
    head -- a Wait or Subworkflow node that resolved it would make a run depend
    on a repository it never reads.
    """
    if project is None or not isinstance(node, (AgentNodeV2, AgentNodeV3)):
        return None
    return project.source.head()


def _executor_key(binding: AgentNodeBindingV2) -> AgentExecutorKey:
    """Which registered executor this binding names: its provider and its revision."""
    return AgentExecutorKey(
        binding.resolved.auth_profile.provider_id,
        binding.resolved.configuration.executor_revision,
    )


def _pinned_tool_grant(
    session: Any, node: AnyWorkflowDocumentNode
) -> DeclaredToolGrant | None:
    """The grant this node pinned, read from the revision the document pins by hash.

    A V3 `tools` entry pins its published revision by that revision's own hash, so
    reading the registry under it is reading exactly what the run configuration
    froze rather than resolving a second time. The bytes are immutable and were
    already read as a grant when the run was bound; a registry that cannot answer
    for them now contradicts a run that has already started.
    """
    if not isinstance(node, AgentNodeV3) or not node.tools:
        return None
    pinned = node.tools[0]
    document = session.scalar(
        sa.select(published_revisions.c.document).where(
            published_revisions.c.kind == RevisionKind.TOOL.value,
            published_revisions.c.revision_hash == pinned.revision,
        )
    )
    if document is None:
        raise RunBindingConflict("the pinned tool revision left the registry")
    grant = read_tool_grant_document(bytes(document))
    if isinstance(grant, ToolGrantRefused):
        raise RunBindingConflict(f"the pinned tool revision is no grant: {grant}")
    return DeclaredToolGrant(PublishedRevisionHash(pinned.revision), grant.capability)


def _pinned_maximum_assistant_turns(
    session: Any, node: AnyWorkflowDocumentNode
) -> int | None:
    """The turn bound this node pinned, read from the revision the document pins.

    Same door as the tool grant: the run already resolved these bytes as a budget
    before any process existed, so a registry that cannot answer for them now,
    or answers with bytes that are no budget, contradicts a run that started.
    A budget that names no turn bound is still a budget; the executor then keeps
    the default it already declares.
    """
    if not isinstance(node, AgentNodeV3) or node.budget is None:
        return None
    document = session.scalar(
        sa.select(published_revisions.c.document).where(
            published_revisions.c.kind == RevisionKind.BUDGET_POLICY.value,
            published_revisions.c.revision_hash == node.budget.revision,
        )
    )
    if document is None:
        raise RunBindingConflict("the pinned budget revision left the registry")
    verdict = read_budget_revision_document(bytes(document))
    if isinstance(verdict, BudgetRevisionRefused):
        raise RunBindingConflict(f"the pinned budget revision is no budget: {verdict}")
    return verdict.content.maximum_assistant_turns


def _declared_output_schema_document(
    session: Any, node: AnyWorkflowDocumentNode
) -> str | None:
    """The exact published document this node's one output pinned, as its own text.

    Same bytes the schema seam later judges. The binding carries them so the
    provider flag cannot invent a second serialization, and the text is decoded
    here because this is the only place that knows which node pinned it.
    """
    if not isinstance(node, AgentNodeV3):
        return None
    if not node.outputs:
        raise RunBindingConflict("a V3 agent node has no declared output schema")
    declared = node.outputs[0]
    document = load_published_schema_document(
        session, declared.schema_reference.revision
    )
    if document is None:
        raise RunBindingConflict(
            f"the schema node {node.id!r} pinned for output "
            f"{declared.name!r} left the registry"
        )
    try:
        return document.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunBindingConflict(
            f"the schema node {node.id!r} pinned for output "
            f"{declared.name!r} is not UTF-8"
        ) from error


def register_durable_run_workflow(
    datasource: SQLAlchemyDatasource,
    agent_executor: AgentExecutor,
    agent_binding: AgentExecutorBinding,
    agent_executors_v2: Mapping[
        AgentExecutorKey,
        tuple[
            AgentExecutorV2,
            AgentExecutorOperationalIdentity,
            frozenset[AgentExecutionCapability],
        ],
    ],
    agent_attempt_store: AgentAttemptStore,
    agent_process_supervisor: AgentProcessSupervisor,
    agent_workspace_owner: AgentAttemptWorkspaceOwner | None,
    project: DeclaredProject | None,
    adapter: EffectAdapter,
    effect_binding: EffectAdapterBinding,
) -> None:
    def start_node(
        run_id: RunId,
        revision_hash: WorkflowRevisionHash,
        node_id: str,
        round_ordinal: int = FIRST_ROUND_ORDINAL,
    ) -> None:
        """Start one round of one node under the identity that round has.

        The durable workflow id is derived from the execution, so a second round
        of the same node is a second durable workflow rather than a repeat of
        the first that the idempotency key would swallow.
        """
        execution_id = NodeExecutionId.for_node(
            run_id, revision_hash, node_id, round_ordinal
        )
        with SetWorkflowID(node_workflow_id_for(execution_id)):
            DBOS.start_workflow(
                durable_node, run_id.value, revision_hash.value, node_id
            )

    @DBOS.workflow(name=WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_run(run_id: str, revision_hash: str) -> str:
        typed_run_id = RunId(run_id)
        typed_revision = WorkflowRevisionHash(revision_hash)
        start = bootstrap_run_binding(datasource, typed_run_id, typed_revision)
        start_node(typed_run_id, typed_revision, start)
        return RunState.STARTED.value

    @DBOS.workflow(name=SUBWORKFLOW_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_add(left: int, right: int) -> int:
        return left + right

    @DBOS.workflow(name=CANCELLATION_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_agent_attempt_cancellation(
        run_id: str, attempt_id: str, command_id: str
    ) -> str:
        attempt = agent_attempt_store.load(AgentAttemptId(attempt_id))
        cancellation = attempt.cancellation
        if cancellation is None or attempt.run_id != RunId(run_id):
            raise RunTransitionConflict(
                "cancellation workflow differs from its durable attempt"
            )
        request = CancelAgentAttemptRequest(
            attempt.run_id,
            attempt.attempt_id,
            command_id,
            cancellation.expected_attempt_state_version,
            cancellation.replacement,
        )
        redrive_index = 0
        while (
            continue_agent_attempt_cancellation(
                request,
                agent_attempt_store,
                agent_process_supervisor,
                _declared_workspace_owner(agent_workspace_owner),
            )
            is None
        ):
            DBOS.sleep(
                CANCELLATION_REDRIVE_SECONDS[
                    min(redrive_index, len(CANCELLATION_REDRIVE_SECONDS) - 1)
                ]
            )
            redrive_index += 1
        return agent_attempt_store.load(attempt.attempt_id).state.value

    @DBOS.workflow(name=REPLACEMENT_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_agent_attempt_replacement(attempt_id: str) -> str:
        replacement = agent_attempt_store.load(AgentAttemptId(attempt_id))
        binding = decode_node_binding(
            _node_binding(
                datasource,
                replacement.run_id,
                replacement.workflow_revision_hash,
                replacement.node_id,
                project,
            )
        )
        if not isinstance(binding, AgentNodeBindingV2):
            raise RunTransitionConflict("replacement attempt is not a V2 agent node")
        executor, operational_identity, declared_capabilities = agent_executors_v2[
            _executor_key(binding)
        ]
        request = agent_execution_request_v2(
            binding,
            replacement.run_id,
            replacement.workflow_revision_hash,
            replacement.node_id,
            operational_identity,
            declared_capabilities,
        )
        if (
            request.node_execution_id != replacement.node_execution_id
            or request.request_hash != replacement.request_hash
            or request.executor_operational_identity
            != replacement.executor_operational_identity
        ):
            raise RunTransitionConflict(
                "replacement workflow differs from its durable attempt binding"
            )
        outcome = execute_agent_attempt(
            AgentAttemptExecution(request, replacement.attempt_id, 2),
            executor,
            agent_attempt_store,
            agent_process_supervisor,
            _declared_workspace_owner(agent_workspace_owner),
            pinned_project(binding, project),
        )
        if isinstance(outcome, AgentAttemptSucceeded):
            match outcome.completion:
                case RunContinues(node_id, round_ordinal):
                    start_node(
                        replacement.run_id,
                        replacement.workflow_revision_hash,
                        node_id,
                        round_ordinal,
                    )
                case RunCompletes():
                    pass
                case _ as unreachable:
                    assert_never(unreachable)
        return outcome.attempt.state.value

    @DBOS.workflow(name=NODE_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_node(run_id: str, revision_hash: str, node_id: str) -> str:
        typed_run_id = RunId(run_id)
        typed_revision = WorkflowRevisionHash(revision_hash)
        binding = decode_node_binding(
            _node_binding(datasource, typed_run_id, typed_revision, node_id, project)
        )
        if isinstance(binding, AgentNodeBinding):
            execution_request = agent_execution_request(
                binding, typed_run_id, typed_revision, node_id
            )
            result = agent_executor.execute(execution_request)
            successor = datasource.run_tx_step(
                {"name": AGENT_COMMIT_STEP_NAME},
                lambda: (
                    commit_agent_completed(
                        datasource.sql_session(),
                        execution_request,
                        agent_binding,
                        result,
                    ).current_node_id
                ),
            )
            start_node(typed_run_id, typed_revision, str(successor))
            return RunState.STARTED.value
        if isinstance(binding, AgentNodeBindingV2):
            executor, operational_identity, declared_capabilities = agent_executors_v2[
                _executor_key(binding)
            ]
            execution_request_v2 = agent_execution_request_v2(
                binding,
                typed_run_id,
                typed_revision,
                node_id,
                operational_identity,
                declared_capabilities,
            )
            attempt_execution = AgentAttemptExecution(
                execution_request_v2,
                AgentAttemptId.for_execution(
                    execution_request_v2.node_execution_id,
                    execution_request_v2.request_hash,
                    1,
                ),
                1,
            )
            outcome = execute_agent_attempt(
                attempt_execution,
                executor,
                agent_attempt_store,
                agent_process_supervisor,
                _declared_workspace_owner(agent_workspace_owner),
                pinned_project(binding, project),
            )
            if not isinstance(outcome, AgentAttemptSucceeded):
                return RunState.STARTED.value
            match outcome.completion:
                case RunContinues(node_id, round_ordinal):
                    start_node(typed_run_id, typed_revision, node_id, round_ordinal)
                    return RunState.STARTED.value
                case RunCompletes():
                    return RunState.COMPLETED.value
                case _ as unreachable:
                    assert_never(unreachable)
        if isinstance(binding, ActionNodeBinding):
            logical_key = str(
                datasource.run_tx_step(
                    {"name": ACTION_PREPARE_STEP_NAME},
                    lambda: (
                        prepare_graph_action(
                            datasource.sql_session(),
                            typed_run_id,
                            typed_revision,
                            effect_binding,
                        ).intent.binding.logical_key.value
                    ),
                )
            )
            with SetWorkflowID(effect_workflow_id_for(LogicalEffectKey(logical_key))):
                DBOS.start_workflow(durable_effect, logical_key, revision_hash)
            return RunState.STARTED.value
        if isinstance(binding, WaitNodeBinding):
            datasource.run_tx_step(
                {"name": WAIT_COMMIT_STEP_NAME},
                lambda: (
                    commit_waiting_input(
                        datasource.sql_session(), typed_run_id, typed_revision, node_id
                    ).state.value
                ),
            )
            return RunState.WAITING_INPUT.value
        if isinstance(binding, SubworkflowNodeBinding):
            execution_id = NodeExecutionId.for_node(
                typed_run_id, typed_revision, node_id
            )
            with SetWorkflowID(subworkflow_workflow_id_for(execution_id)):
                handle = DBOS.start_workflow(durable_add, *binding.operands)
            result = handle.get_result()
            datasource.run_tx_step(
                {"name": SUBWORKFLOW_COMMIT_STEP_NAME},
                lambda: (
                    commit_subworkflow_completed(
                        datasource.sql_session(),
                        typed_run_id,
                        typed_revision,
                        node_id,
                        result,
                    ).state.value
                ),
            )
            return RunState.COMPLETED.value
        assert_never(binding)

    @DBOS.workflow(name=EFFECT_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_effect(logical_key: str, revision_hash: str) -> str:
        observed = _run_effect_step(
            datasource,
            OBSERVE_STEP_NAME,
            observe_adapter,
            adapter,
            logical_key,
            revision_hash,
        )
        resolved = _run_effect_step(
            datasource,
            RESOLVE_STEP_NAME,
            resolve_observation,
            adapter,
            logical_key,
            revision_hash,
            observed,
        )
        state = RunState(
            datasource.run_tx_step(
                {"name": COMMIT_STEP_NAME},
                lambda: (
                    commit_resolution(
                        datasource.sql_session(), logical_key, revision_hash, resolved
                    ).value
                ),
            )
        )
        if state is RunState.STARTED:
            schedule_confirmed_action_continuation(
                durable_action_continuation,
                LogicalEffectKey(logical_key),
                WorkflowRevisionHash(revision_hash),
            )
        return state.value

    @DBOS.workflow(name=RECONCILE_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_reconciliation(command_id: str, revision_hash: str) -> str:
        observed = _run_effect_step(
            datasource,
            OBSERVE_STEP_NAME,
            observe_reconcile_command,
            adapter,
            command_id,
            revision_hash,
        )
        command = ReconcileCommandId(command_id)
        logical_key = str(observed["logical_key"])
        resolved = _run_effect_step(
            datasource,
            RESOLVE_STEP_NAME,
            resolve_observation,
            adapter,
            logical_key,
            revision_hash,
            observed,
            command if observed.get("operator_authorized") == command_id else None,
        )
        state = RunState(
            datasource.run_tx_step(
                {"name": COMMIT_STEP_NAME},
                lambda: (
                    commit_resolution(
                        datasource.sql_session(),
                        logical_key,
                        revision_hash,
                        resolved,
                        command,
                    ).value
                ),
            )
        )
        if state is RunState.STARTED:
            schedule_confirmed_action_continuation(
                durable_action_continuation,
                LogicalEffectKey(logical_key),
                WorkflowRevisionHash(revision_hash),
            )
        return state.value

    @DBOS.workflow(name=ACTION_CONTINUATION_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_action_continuation(logical_key: str, revision_hash: str) -> str:
        typed_key = LogicalEffectKey(logical_key)
        typed_revision = WorkflowRevisionHash(revision_hash)
        run_id, head, state = checkpoint_confirmed_action(
            datasource, typed_key, typed_revision
        )
        if RunState(state) is RunState.STARTED:
            start_node(RunId(run_id), typed_revision, head)
        return state

    @DBOS.workflow(name=ANSWER_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_answer(run_id: str, revision_hash: str, node_id: str) -> str:
        typed_run_id = RunId(run_id)
        typed_revision = WorkflowRevisionHash(revision_hash)

        def apply() -> list[str]:
            answer = load_wait_answer(
                datasource.sql_session(), typed_run_id, typed_revision, node_id
            ).answer
            transition = commit_wait_answered(datasource.sql_session(), answer)
            return [transition.current_node_id, transition.state.value]

        # The step reports the state as well as the head, because an answered Wait
        # node that is its run's sink ends the run, and after a terminal transition
        # correctness must not rest on DBOS deduplicating a workflow id: starting
        # the sink's own node again is harmless only for as long as that id happens
        # to collide. The state is what says "there is nothing left to start", so
        # the decision is read from the run rather than borrowed from the runtime.
        # Recording two values is why the step name carries a version -- a
        # recovered step of the earlier shape would be read as a head alone.
        head, state = cast(
            tuple[str, str],
            tuple(datasource.run_tx_step({"name": ANSWER_COMMIT_STEP_NAME}, apply)),
        )
        if RunState(state) is RunState.STARTED:
            start_node(typed_run_id, typed_revision, head)
        return state
