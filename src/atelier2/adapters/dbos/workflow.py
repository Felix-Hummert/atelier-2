from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, NotRequired, TypedDict, cast

from dbos import DBOS, SetWorkflowID, SQLAlchemyDatasource

from atelier2.adapters.agent_processes import AgentProcessSupervisor
from atelier2.adapters.dbos.agent_attempt_store import (
    CANCELLATION_WORKFLOW_NAME,
    REPLACEMENT_WORKFLOW_NAME,
)
from atelier2.adapters.dbos.continuation import (
    ACTION_CONTINUATION_WORKFLOW_NAME,
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
from atelier2.adapters.dbos.run_store import (
    RunTransitionConflict,
    commit_agent_completed,
    commit_subworkflow_completed,
    commit_wait_answered,
    commit_waiting_input,
    load_graph,
    load_run,
    load_wait_answer,
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
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutionRequest,
    AgentExecutionRequestV2,
    AgentExecutorBinding,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    AuthProfileRevisionHash,
    ExactOutputContract,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.effects import (
    EffectAdapterBinding,
    LogicalEffectKey,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    NodeExecutionId,
    node_workflow_id_for,
    subworkflow_workflow_id_for,
)
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.workflows import (
    ActionNode,
    AgentNode,
    AgentNodeV2,
    SubworkflowNode,
    WaitNode,
)
from atelier2.ports.agent_attempts import AgentAttemptStore, AgentAttemptSucceeded
from atelier2.ports.agent_executions import (
    AgentExecutor,
    AgentExecutorKey,
    AgentExecutorV2,
)
from atelier2.ports.effects import EffectAdapter

WORKFLOW_NAME = "atelier2_durable_run"
NODE_WORKFLOW_NAME = "atelier2_graph_node"
EFFECT_WORKFLOW_NAME = "atelier2_effect"
RECONCILE_WORKFLOW_NAME = "atelier2_reconcile_effect"
ANSWER_WORKFLOW_NAME = "atelier2_wait_answer"
SUBWORKFLOW_WORKFLOW_NAME = "atelier2_add_subworkflow"
QUEUE_NAME = "atelier2-durable-runs"

BOOTSTRAP_STEP_NAME = "bootstrap-run-binding"
NODE_BINDING_STEP_NAME = "node-binding/0"
AGENT_COMMIT_STEP_NAME = "agent-commit/1"
ACTION_PREPARE_STEP_NAME = "action-prepare/1"
WAIT_COMMIT_STEP_NAME = "wait-commit/1"
SUBWORKFLOW_COMMIT_STEP_NAME = "subworkflow-commit/1"
ANSWER_COMMIT_STEP_NAME = "answer-commit/0"
OBSERVE_STEP_NAME = "observe/0"
RESOLVE_STEP_NAME = "resolve/1"
COMMIT_STEP_NAME = "commit/2"
CANCELLATION_REDRIVE_SECONDS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0)


class RunBindingConflict(RuntimeError):
    pass


class EncodedAgentBinding(TypedDict):
    type: Literal["agent"]
    job: str
    output: str


class EncodedAgentBindingV2(TypedDict):
    type: Literal["agent-v2"]
    role: str
    job: str
    configuration_hash: str
    auth_hash: str
    profile_id: str
    revision_number: int
    provider_id: str
    auth_mode: str
    model: str
    executor_revision: str
    revision_format_version: NotRequired[int]
    requested_capability: NotRequired[str]


class EncodedActionBinding(TypedDict):
    type: Literal["action"]


class EncodedWaitBinding(TypedDict):
    type: Literal["wait"]


class EncodedSubworkflowBinding(TypedDict):
    type: Literal["subworkflow"]
    left: int
    right: int


type EncodedNodeBinding = (
    EncodedAgentBinding
    | EncodedAgentBindingV2
    | EncodedActionBinding
    | EncodedWaitBinding
    | EncodedSubworkflowBinding
)


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
        if (
            run.state is not RunState.STARTED
            or run.current_node_id != graph.start
            or run.state_version != 0
            or run.last_event_sequence != 0
        ):
            raise RunBindingConflict("bootstrap requires its exact new durable run")
        return graph.start

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
) -> EncodedNodeBinding:
    def load() -> EncodedNodeBinding:
        session = datasource.sql_session()
        run = load_run(session, run_id)
        if (
            run.revision_hash != revision_hash
            or run.current_node_id != node_id
            or run.state is not RunState.STARTED
        ):
            raise RunTransitionConflict(
                "node workflow does not own current STARTED node"
            )
        node = load_graph(session, revision_hash).node(node_id)
        if isinstance(node, AgentNode):
            return {"type": "agent", "job": node.job, "output": node.output}
        if isinstance(node, AgentNodeV2):
            if not isinstance(run, RunV2):
                raise RunTransitionConflict("V2 Agent node belongs to a V1 run")
            resolved = next(
                (
                    binding
                    for binding in run.agent_bindings
                    if binding.role.value == node.role
                ),
                None,
            )
            if resolved is None:
                raise RunTransitionConflict("V2 Agent role has no durable binding")
            configuration = resolved.configuration
            auth = resolved.auth_profile
            return {
                "type": "agent-v2",
                "role": resolved.role.value,
                "job": node.job,
                "configuration_hash": configuration.revision_hash.value,
                "auth_hash": auth.revision_hash.value,
                "profile_id": auth.profile_id,
                "revision_number": auth.revision_number,
                "provider_id": auth.provider_id.value,
                "auth_mode": auth.auth_mode.value,
                "model": configuration.model,
                "executor_revision": configuration.executor_revision.value,
                "revision_format_version": int(configuration.revision_format_version),
                "requested_capability": configuration.requested_capability.value,
            }
        if isinstance(node, ActionNode):
            return {"type": "action"}
        if isinstance(node, WaitNode):
            return {"type": "wait"}
        if isinstance(node, SubworkflowNode):
            return {
                "type": "subworkflow",
                "left": node.operands[0],
                "right": node.operands[1],
            }
        raise AssertionError("closed WorkflowNode union was not exhaustive")

    return cast(
        EncodedNodeBinding,
        datasource.run_tx_step({"name": NODE_BINDING_STEP_NAME}, load),
    )


def _agent_request_v2(
    binding: EncodedAgentBindingV2,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    operational_identity: AgentExecutorOperationalIdentity,
) -> AgentExecutionRequestV2:
    auth = AuthProfileRevision(
        binding["profile_id"],
        binding["revision_number"],
        ProviderId(binding["provider_id"]),
        AuthMode(binding["auth_mode"]),
    )
    if auth.revision_hash != AuthProfileRevisionHash(binding["auth_hash"]):
        raise RunBindingConflict("V2 auth fields differ from their durable hash")
    has_encoded_version = "revision_format_version" in binding
    has_encoded_capability = "requested_capability" in binding
    if not has_encoded_version and not has_encoded_capability:
        revision_format_version = AgentConfigurationRevisionFormatVersion.V1
        requested_capability = AgentExecutionCapability.HEADLESS
    elif not has_encoded_version or not has_encoded_capability:
        raise RunBindingConflict("V2 configuration contract is only partly encoded")
    else:
        encoded_version = binding["revision_format_version"]
        encoded_capability = binding["requested_capability"]
        if type(encoded_version) is not int or type(encoded_capability) is not str:
            raise RunBindingConflict(
                "V2 configuration contract carries a value of the wrong type"
            )
        try:
            revision_format_version = AgentConfigurationRevisionFormatVersion(
                encoded_version
            )
            requested_capability = AgentExecutionCapability(encoded_capability)
        except ValueError as error:
            raise RunBindingConflict(
                "V2 configuration contract carries an unknown value"
            ) from error
    try:
        configuration = AgentConfigurationRevision(
            binding["model"],
            auth.revision_hash,
            AgentExecutorRevision(binding["executor_revision"]),
            requested_capability,
            revision_format_version,
        )
    except (TypeError, ValueError) as error:
        raise RunBindingConflict(
            "V2 configuration contract carries an invalid combination"
        ) from error
    if configuration.revision_hash != AgentConfigurationRevisionHash(
        binding["configuration_hash"]
    ):
        raise RunBindingConflict(
            "V2 configuration fields differ from their durable hash"
        )
    resolved = ResolvedAgentBinding(AgentRole(binding["role"]), configuration, auth)
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, node_id),
        run_id,
        revision_hash,
        node_id,
        resolved,
        operational_identity,
        binding["job"].encode("utf-8"),
    )


def register_durable_run_workflow(
    datasource: SQLAlchemyDatasource,
    agent_executor: AgentExecutor,
    agent_binding: AgentExecutorBinding,
    agent_executors_v2: Mapping[
        AgentExecutorKey, tuple[AgentExecutorV2, AgentExecutorOperationalIdentity]
    ],
    agent_attempt_store: AgentAttemptStore,
    agent_process_supervisor: AgentProcessSupervisor,
    adapter: EffectAdapter,
    effect_binding: EffectAdapterBinding,
) -> None:
    def start_node(
        run_id: RunId, revision_hash: WorkflowRevisionHash, node_id: str
    ) -> None:
        execution_id = NodeExecutionId.for_node(run_id, revision_hash, node_id)
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
                request, agent_attempt_store, agent_process_supervisor
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
        binding = _node_binding(
            datasource,
            replacement.run_id,
            replacement.workflow_revision_hash,
            replacement.node_id,
        )
        if binding["type"] != "agent-v2":
            raise RunTransitionConflict("replacement attempt is not a V2 agent node")
        executor, operational_identity = agent_executors_v2[
            AgentExecutorKey(
                ProviderId(binding["provider_id"]),
                AgentExecutorRevision(binding["executor_revision"]),
            )
        ]
        request = _agent_request_v2(
            binding,
            replacement.run_id,
            replacement.workflow_revision_hash,
            replacement.node_id,
            operational_identity,
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
        )
        if isinstance(outcome, AgentAttemptSucceeded):
            start_node(
                replacement.run_id,
                replacement.workflow_revision_hash,
                outcome.successor_node_id,
            )
        return outcome.attempt.state.value

    @DBOS.workflow(name=NODE_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_node(run_id: str, revision_hash: str, node_id: str) -> str:
        typed_run_id = RunId(run_id)
        typed_revision = WorkflowRevisionHash(revision_hash)
        binding = _node_binding(datasource, typed_run_id, typed_revision, node_id)
        if binding["type"] == "agent":
            execution_request = AgentExecutionRequest(
                NodeExecutionId.for_node(typed_run_id, typed_revision, node_id),
                typed_run_id,
                typed_revision,
                node_id,
                binding["job"].encode("utf-8"),
                ExactOutputContract(binding["output"].encode("utf-8")),
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
        if binding["type"] == "agent-v2":
            executor, operational_identity = agent_executors_v2[
                AgentExecutorKey(
                    ProviderId(binding["provider_id"]),
                    AgentExecutorRevision(binding["executor_revision"]),
                )
            ]
            execution_request_v2 = _agent_request_v2(
                binding,
                typed_run_id,
                typed_revision,
                node_id,
                operational_identity,
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
            )
            if not isinstance(outcome, AgentAttemptSucceeded):
                return RunState.STARTED.value
            start_node(typed_run_id, typed_revision, outcome.successor_node_id)
            return RunState.STARTED.value
        if binding["type"] == "action":
            from atelier2.adapters.dbos.advancer import (
                effect_workflow_id_for,
                prepare_graph_action,
            )

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
        if binding["type"] == "wait":
            datasource.run_tx_step(
                {"name": WAIT_COMMIT_STEP_NAME},
                lambda: (
                    commit_waiting_input(
                        datasource.sql_session(), typed_run_id, typed_revision, node_id
                    ).state.value
                ),
            )
            return RunState.WAITING_INPUT.value
        if binding["type"] == "subworkflow":
            execution_id = NodeExecutionId.for_node(
                typed_run_id, typed_revision, node_id
            )
            with SetWorkflowID(subworkflow_workflow_id_for(execution_id)):
                handle = DBOS.start_workflow(
                    durable_add, int(binding["left"]), int(binding["right"])
                )
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
        raise AssertionError("closed node binding union was not exhaustive")

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
        run_id, successor = checkpoint_confirmed_action(
            datasource, typed_key, typed_revision
        )
        start_node(RunId(run_id), typed_revision, successor)
        return RunState.STARTED.value

    @DBOS.workflow(name=ANSWER_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_answer(run_id: str, revision_hash: str, node_id: str) -> str:
        typed_run_id = RunId(run_id)
        typed_revision = WorkflowRevisionHash(revision_hash)

        def apply() -> str:
            answer = load_wait_answer(
                datasource.sql_session(), typed_run_id, typed_revision, node_id
            ).answer
            return commit_wait_answered(
                datasource.sql_session(), answer
            ).current_node_id

        successor = str(
            datasource.run_tx_step({"name": ANSWER_COMMIT_STEP_NAME}, apply)
        )
        start_node(typed_run_id, typed_revision, successor)
        return RunState.STARTED.value
