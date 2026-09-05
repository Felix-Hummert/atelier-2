"""A V3 agent node opens its own pull request through a declared `open-pr` grant.

`#431` Phase 2: the pull request the Action node lands in `test_v3_open_pr_action`
is here opened by the agent node itself, as a declared tool grant rather than a
downstream Action. The same locked adapter, the same receipt, no `project_source`
the grant has no use for, and no token in anything durable. Without the grant the
tool does not exist: a plain agent node opens nothing.

The proof is the whole vertical, driven from the public start seam and read back
from the store, because each half alone is a promise -- an admitted `open-pr`
grant nothing redeems, or a pull request no grant could have asked for.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.advancer import prepared_effect_intent
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.effect_store import (
    commit_resolution,
    intent_snapshot_from_record,
)
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    load_graph,
    load_run,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    run_events,
    runs,
    tool_redemptions,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_run_reference
from atelier2.application.compose_node_job import node_job
from atelier2.contracts.agents import (
    AgentBindingSet,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.effect_markers import body_carries_request_hash
from atelier2.contracts.effect_requests import OpenPullRequest
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    EffectAdapterBinding,
    EffectBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentStateVersion,
    EffectOutcome,
    EffectReadback,
    EffectResult,
    EffectUnknownOutcome,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    PerformedEffect,
    ReadbackPhase,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    logical_effect_key_for,
    logical_effect_key_for_node,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_forks import RunForkCommandId, successor_run_id_for
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.workflows_v3 import AgentNodeV3
from atelier2.ports.durable_run_forks import DurableRunForkCreated, ForkRunRequest
from atelier2.ports.durable_runs import (
    DurableRunFormatNotExecutable,
    StartPublishedRunRequestV2,
)
from atelier2.ports.effects import EffectAdapter
from atelier2.ports.run_queries import NodeDetailFound
from tests.integration.test_v3_open_pr_action import CountingGitHubEffectAdapterFactory
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_attempt_execution,
    agent_scratch_root,
)
from tests.scenarios.api import durable_api_client, durable_queries
from tests.scenarios.open_pr_agent import (
    OPEN_PR_GRANT,
    OPERATIONAL_IDENTITY,
    PR_SPEC,
    create_open_pr_agent_run,
    open_pr_agent_executor_factory,
    publish_open_pr_agent_run,
)
from tests.scenarios.run_waiting import wait_for_run_state
from tests.scenarios.runs import submit_reconcile_command
from tests.scenarios.workflows import ANY_JSON_SCHEMA

RUN = RunId("v3/agent-open-pr")
UNGRANTED_RUN = RunId("v3/agent-no-grant")
LOOPED_RUN = RunId("v3/agent-open-pr-loop")
CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"


class _UnknownOpenPrAdapter:
    """An adapter shaped like the live one: it never proves an absence itself."""

    def __init__(self) -> None:
        self.execute_calls = 0

    def readback(self, intent: EffectIntent, phase: ReadbackPhase) -> EffectReadback:
        del phase
        return EffectUnknownOutcome(intent.reference)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        del intent
        self.execute_calls += 1
        return PerformedEffect(EffectId("opened-after-absence"), EffectResult(PR_SPEC))

    def close(self) -> None:
        return None


class _UnknownOpenPrAdapterFactory:
    def __init__(self) -> None:
        self.adapter = _UnknownOpenPrAdapter()
        self.binding = EffectAdapterBinding(
            AdapterRevision("live-shaped-open-pr-v1"),
            EffectDestination("platform"),
            AdapterOperationalIdentity("test-live-github"),
        )

    @property
    def proves_absence(self) -> bool:
        return False

    def open(self) -> EffectAdapter:
        return self.adapter


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path]]:
    github = CountingGitHubEffectAdapterFactory(tmp_path / "github.sqlite")
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-agent-open-pr-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        github,
        (open_pr_agent_executor_factory(PR_SPEC),),
    )
    started.initialize_storage()
    try:
        yield started, github, tmp_path / "atelier.sqlite"
    finally:
        started.close()


def _start(runtime: DbosRuntime, run: RunId, *, granted: bool) -> None:
    workflow, bindings = publish_open_pr_agent_run(runtime, granted=granted)
    create_open_pr_agent_run(runtime, run, workflow, bindings)
    runtime.launch()


def _publish_two_agent_open_pr_run(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    _, bindings = publish_open_pr_agent_run(runtime, granted=True)
    tool_revision = PublishedRevision(
        RevisionKind.TOOL, OPEN_PR_GRANT
    ).revision_hash.value
    schema_revision = ANY_JSON_SCHEMA.revision_hash.value
    workflow = WorkflowRevision(
        f"""format_version: 3
name: Reuse one agent effect before another agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Draft the pull request this chain opens.
    tools:
      - {{ref: open-pr, revision: {tool_revision}}}
    outputs:
      - name: result
        schema: {{ref: any-json, revision: {schema_revision}}}
  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Review the already-opened pull request.
    depends_on: [implement]
    outputs:
      - name: result
        schema: {{ref: any-json, revision: {schema_revision}}}
""".encode()
    )
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, bindings


def _wait_for_receipt(runtime: DbosRuntime) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            if connection.scalar(
                sa.select(sa.func.count()).select_from(effect_receipts)
            ):
                return
        time.sleep(0.025)
    raise AssertionError("no effect receipt was written")


def _submit_reconciliation(
    runtime: DbosRuntime,
    intent: EffectIntent,
    determination: OperatorFoundEffect | OperatorAuthoritativeAbsence,
) -> None:
    submit_reconcile_command(
        runtime.engine,
        runtime.settings,
        ReconcileCommand(
            ReconcileCommandId("agent-open-pr-reconcile"),
            intent.reference,
            EffectIntentStateVersion(1),
            ReconcileActor("operator"),
            "checked the declared pull-request destination",
            determination,
        ),
    )


def _persist_round_two_reconciliation(
    runtime: DbosRuntime,
    run: RunId,
    workflow_revision_hash: WorkflowRevisionHash,
    adapter_binding: EffectAdapterBinding,
) -> EffectIntent:
    """Seed the persisted legacy state a looped grant may leave behind.

    New looped effect grants are refused before a run exists, but an older
    database can already carry this exact round-two reconciliation door. The
    reconciliation path must keep that execution's round rather than silently
    returning it to the first.
    """

    logical_key = logical_effect_key_for_node(
        run, workflow_revision_hash, "implement", 2
    )
    intent = EffectIntent(
        EffectBinding(
            logical_key,
            run,
            workflow_revision_hash,
            adapter_binding.adapter_revision,
            adapter_binding.destination,
            adapter_binding.operational_identity,
        ),
        CanonicalRequest(PR_SPEC),
    )
    with canonical_write_transaction(runtime.engine) as connection:
        connection.execute(
            runs.update()
            .where(runs.c.run_id == run.value)
            .values(current_round_ordinal=2)
        )
        prepared_effect_intent(connection, intent)
        commit_resolution(
            connection,
            logical_key.value,
            workflow_revision_hash.value,
            {"outcome": EffectOutcome.UNKNOWN.value},
        )
    return intent


def _completed_agent_execution(runtime: DbosRuntime, run: RunId):
    with runtime.engine.connect() as connection:
        durable_run = load_run(connection, run)
        graph = load_graph(connection, durable_run.revision_hash)
    assert isinstance(durable_run, RunV3)
    node = graph.node("implement")
    assert isinstance(node, AgentNodeV3)
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run, durable_run.revision_hash, node.id),
        run,
        durable_run.revision_hash,
        node.id,
        durable_run.agent_bindings[0],
        AgentExecutorOperationalIdentity(OPERATIONAL_IDENTITY),
        node_job(node.instruction).encode("utf-8"),
    )
    return agent_attempt_execution(request)


def _replace_receipt_adapter_revision(runtime: DbosRuntime) -> None:
    with runtime.engine.begin() as connection:
        trigger = connection.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND name='effect_receipts_no_update'"
            )
        ).scalar_one()
        connection.execute(sa.text("DROP TRIGGER effect_receipts_no_update"))
        connection.execute(
            effect_receipts.update().values(adapter_revision="mismatched-receipt")
        )
        connection.execute(sa.text(str(trigger)))


def _durable_bytes_contain(database: Path, token: str) -> bool:
    needle = token.encode("utf-8")
    for candidate in (
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ):
        if candidate.is_file() and needle in candidate.read_bytes():
            return True
    return False


@pytest.mark.proves("a-v3-agent-node-opens-one-pr-through-its-own-open-pr-grant")
def test_a_granted_agent_node_opens_one_pull_request_and_leaves_one_receipt(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path],
) -> None:
    started_runtime, github, atelier_sqlite = runtime

    _start(started_runtime, RUN, granted=True)
    wait_for_run_state(started_runtime.engine, RUN, RunState.COMPLETED)
    _wait_for_receipt(started_runtime)

    recorded = github.recorded_pull_requests()
    assert len(recorded) == 1
    pull_request = recorded[0]

    with started_runtime.engine.connect() as connection:
        agent_output = bytes(
            connection.execute(
                sa.select(run_events.c.payload).where(
                    run_events.c.run_id == RUN.value,
                    run_events.c.event_kind == RunEventKind.AGENT_COMPLETED.value,
                )
            ).scalar_one()
        )
        intent = intent_snapshot_from_record(
            connection.execute(sa.select(effect_intents)).mappings().one()
        ).intent
        receipt_payload = bytes(
            connection.execute(sa.select(effect_receipts.c.result)).scalar_one()
        )
        redemption_count = connection.scalar(
            sa.select(sa.func.count()).select_from(tool_redemptions)
        )

    # The pull request is the agent's own kept output, marked by this exact
    # prepared request -- the same shape an Action's confirmation leaves.
    assert agent_output == PR_SPEC
    typed_request = OpenPullRequest.from_canonical_bytes(intent.request.payload)
    assert typed_request.body.encode("utf-8") == PR_SPEC
    # #706: `graph_agent_open_pr_intent` now mints this key through the one
    # owner it shares with the Action preparer and the #646 sweep
    # (`logical_effect_key_for_node`) instead of composing `NodeExecutionId.
    # for_node` and `logical_effect_key_for` by hand -- a pure refactor, so
    # round one's key stays the exact bytes the un-rounded call already gave.
    assert intent.binding.logical_key == logical_effect_key_for(
        NodeExecutionId.for_node(
            RUN, intent.binding.workflow_revision_hash, "implement"
        )
    )
    assert body_carries_request_hash(
        pull_request.body, intent.request.request_hash.value
    )
    assert json.loads(receipt_payload.decode("utf-8")) == {
        "branch": pull_request.branch,
        "pr_number": pull_request.pr_number,
    }
    # An open-pr grant is redeemed as a platform effect, never into the
    # exec-shaped tool_redemptions row a verification leaves.
    assert redemption_count == 0

    # Idempotency: the derived logical key readback-matches the first pull
    # request, so redeeming again opens no twin.
    adapter = github.open()
    try:
        replayed = adapter.execute(intent)
    finally:
        adapter.close()
    assert isinstance(replayed, PerformedEffect)
    assert json.loads(replayed.result.payload.decode("utf-8")) == {
        "branch": pull_request.branch,
        "pr_number": pull_request.pr_number,
    }
    assert len(github.recorded_pull_requests()) == 1

    # No credential-shaped value reaches anything durable the redemption touched.
    # The fake carries no real credential, so this canary is only a floor here --
    # the live credential-by-reference proof is the live GitHub test's (#430).
    assert CANARY_TOKEN not in pull_request.body
    assert not _durable_bytes_contain(atelier_sqlite, CANARY_TOKEN)
    assert not _durable_bytes_contain(github.database_path, CANARY_TOKEN)


def test_forked_agent_open_pr_references_the_confirmed_effect_without_replay(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path],
) -> None:
    started_runtime, github, _atelier_sqlite = runtime
    workflow, bindings = publish_open_pr_agent_run(started_runtime, granted=True)
    create_open_pr_agent_run(started_runtime, RUN, workflow, bindings)
    started_runtime.launch()
    wait_for_run_state(started_runtime.engine, RUN, RunState.COMPLETED)
    _wait_for_receipt(started_runtime)

    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    request = ForkRunRequest(RUN, "retry-agent-open-pr", "implement")
    forked = starter.fork_run(request)
    assert isinstance(forked, DurableRunForkCreated)
    successor = successor_run_id_for(
        RunForkCommandId.for_request(RUN, "retry-agent-open-pr")
    )
    wait_for_run_state(started_runtime.engine, successor, RunState.COMPLETED)

    assert len(github.recorded_pull_requests()) == 1
    with started_runtime.engine.connect() as connection:
        successor_receipt = (
            connection.execute(
                sa.select(effect_receipts).where(
                    effect_receipts.c.run_id == successor.value
                )
            )
            .mappings()
            .one()
        )
        events = tuple(
            connection.execute(
                sa.select(run_events.c.event_kind).where(
                    run_events.c.run_id == successor.value
                )
            ).scalars()
        )
    assert events == (
        RunEventKind.AGENT_COMPLETED.value,
        RunEventKind.ACTION_COMPLETED.value,
    )
    assert successor_receipt["confirmation_source"] == "FORK_REFERENCE"
    assert successor_receipt["fork_source_run_id"] == RUN.value
    assert successor_receipt["fork_source_logical_key"] is not None
    assert successor_receipt["fork_source_result_hash"] is not None

    # #1234: the successor's own "implement" execution carries both its
    # AGENT_COMPLETED completion and this ACTION_COMPLETED confirmation under
    # the same node-execution id -- `get_node_detail` must read the node's own
    # kind rather than any answer-bearing kind, or it finds both rows and
    # refuses with `MultipleResultsFound`.
    queries = durable_queries(started_runtime.engine)
    detail = queries.get_node_detail(successor, "implement")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.answer is not None
    assert detail.detail.answer.value == PR_SPEC

    api = durable_api_client(started_runtime)
    node_response = api.get(
        f"{API_PREFIX}/runs/{encode_public_run_reference(successor)}/nodes/implement"
    )
    assert node_response.status_code == 200, node_response.text


def test_fork_of_fork_fences_an_inherited_agent_effect_before_adapter_invocation(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path],
) -> None:
    started_runtime, github, _atelier_sqlite = runtime
    workflow, bindings = _publish_two_agent_open_pr_run(started_runtime)
    create_open_pr_agent_run(started_runtime, RUN, workflow, bindings)
    started_runtime.launch()
    wait_for_run_state(started_runtime.engine, RUN, RunState.COMPLETED)

    starter = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    )
    inherited = starter.fork_run(ForkRunRequest(RUN, "reuse-agent", "review"))
    assert isinstance(inherited, DurableRunForkCreated)
    wait_for_run_state(started_runtime.engine, inherited.run.run_id, RunState.COMPLETED)
    assert inherited.fork.reused_nodes[0].source_run_id == RUN
    calls_before_exact = (github.readback_calls, github.execute_calls)

    exact = starter.fork_run(
        ForkRunRequest(inherited.run.run_id, "exact-inherited-agent", "implement")
    )
    assert isinstance(exact, DurableRunForkCreated)
    wait_for_run_state(started_runtime.engine, exact.run.run_id, RunState.COMPLETED)
    assert (
        github.readback_calls - calls_before_exact[0],
        github.execute_calls - calls_before_exact[1],
    ) == (0, 0)
    assert len(github.recorded_pull_requests()) == 1
    with started_runtime.engine.connect() as connection:
        exact_receipt = (
            connection.execute(
                sa.select(effect_receipts).where(
                    effect_receipts.c.run_id == exact.run.run_id.value
                )
            )
            .mappings()
            .one()
        )
    assert exact_receipt["confirmation_source"] == "FORK_REFERENCE"
    assert exact_receipt["fork_source_run_id"] == RUN.value

    factory = next(
        entry.factory
        for entry in started_runtime.agent_executor_registry.entries
        if isinstance(entry.factory, RecordingAgentExecutorFactoryV2)
    )
    assert isinstance(factory, RecordingAgentExecutorFactoryV2)
    assert factory.opened is not None
    factory.opened.output = json.dumps(
        {"title": "Changed request", "opened_by": "the nested fork"}
    ).encode()
    calls_before_mismatch = (github.readback_calls, github.execute_calls)
    mismatched = starter.fork_run(
        ForkRunRequest(inherited.run.run_id, "changed-inherited-agent", "implement")
    )
    assert isinstance(mismatched, DurableRunForkCreated)
    wait_for_run_state(
        started_runtime.engine,
        mismatched.run.run_id,
        RunState.WAITING_RECONCILIATION,
    )
    assert (
        github.readback_calls - calls_before_mismatch[0],
        github.execute_calls - calls_before_mismatch[1],
    ) == (0, 0)
    assert len(github.recorded_pull_requests()) == 1
    with started_runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(effect_receipts)
                .where(effect_receipts.c.run_id == mismatched.run.run_id.value)
            )
            == 0
        )


@pytest.mark.proves("without-the-grant-the-open-pr-tool-does-not-exist")
def test_an_agent_node_without_the_grant_opens_no_pull_request(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path],
) -> None:
    """A plain agent node has no open-pr tool: it completes and opens nothing."""
    started_runtime, github, _atelier_sqlite = runtime

    _start(started_runtime, UNGRANTED_RUN, granted=False)
    wait_for_run_state(started_runtime.engine, UNGRANTED_RUN, RunState.COMPLETED)

    assert github.recorded_pull_requests() == ()
    with started_runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_intents))
            == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_receipts))
            == 0
        )


def test_a_completed_agent_replay_refuses_a_mismatched_receipt_intent(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path],
) -> None:
    started_runtime, _github, _atelier_sqlite = runtime

    _start(started_runtime, RUN, granted=True)
    wait_for_run_state(started_runtime.engine, RUN, RunState.COMPLETED)
    _wait_for_receipt(started_runtime)
    _replace_receipt_adapter_revision(started_runtime)

    with pytest.raises(
        RunTransitionConflict,
        match="successful effect-grant attempt has no exact confirmed effect receipt",
    ):
        DbosAgentAttemptStore(started_runtime.engine).claim(
            _completed_agent_execution(started_runtime, RUN)
        )


def test_a_looped_agent_effect_grant_is_refused_before_it_creates_a_run(
    runtime: tuple[DbosRuntime, CountingGitHubEffectAdapterFactory, Path],
) -> None:
    started_runtime, github, _atelier_sqlite = runtime
    workflow, bindings = publish_open_pr_agent_run(
        started_runtime, granted=True, loop_maximum_rounds=2
    )
    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(LOOPED_RUN, workflow.revision_hash, bindings)
    )

    assert isinstance(started, DurableRunFormatNotExecutable)
    with started_runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_intents))
            == 0
        )
    assert github.recorded_pull_requests() == ()


@pytest.mark.parametrize("operator_found", (True, False))
def test_a_persisted_round_two_agent_reconciliation_preserves_its_round(
    tmp_path: Path,
    operator_found: bool,
) -> None:
    adapter_factory = _UnknownOpenPrAdapterFactory()
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "unknown-agent-open-pr",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        adapter_factory,
        (open_pr_agent_executor_factory(PR_SPEC),),
    )
    runtime.initialize_storage()
    try:
        workflow, bindings = publish_open_pr_agent_run(
            runtime, granted=False, loop_maximum_rounds=2
        )
        create_open_pr_agent_run(runtime, RUN, workflow, bindings)
        intent = _persist_round_two_reconciliation(
            runtime, RUN, workflow.revision_hash, adapter_factory.binding
        )
        with runtime.engine.connect() as connection:
            state, node, round_ordinal = connection.execute(
                sa.select(
                    runs.c.state, runs.c.current_node_id, runs.c.current_round_ordinal
                ).where(runs.c.run_id == RUN.value)
            ).one()
            receipts = connection.scalar(
                sa.select(sa.func.count()).select_from(effect_receipts)
            )
        assert (state, node, round_ordinal) == (
            RunState.WAITING_RECONCILIATION.value,
            "implement",
            2,
        )
        assert receipts == 0
        assert adapter_factory.adapter.execute_calls == 0
        assert not _durable_bytes_contain(tmp_path / "atelier.sqlite", CANARY_TOKEN)

        if operator_found:
            determination: OperatorFoundEffect | OperatorAuthoritativeAbsence = (
                OperatorFoundEffect(
                    EffectId("found-by-operator"), EffectResult(PR_SPEC)
                )
            )
        else:
            determination = OperatorAuthoritativeAbsence()
        runtime.launch()
        _submit_reconciliation(runtime, intent, determination)
        wait_for_run_state(runtime.engine, RUN, RunState.COMPLETED)
        _wait_for_receipt(runtime)

        with runtime.engine.connect() as connection:
            receipt_count = connection.scalar(
                sa.select(sa.func.count()).select_from(effect_receipts)
            )
            events = connection.execute(
                sa.select(run_events.c.event_kind, run_events.c.round_ordinal)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).all()
            current_round = connection.scalar(
                sa.select(runs.c.current_round_ordinal).where(
                    runs.c.run_id == RUN.value
                )
            )
        assert receipt_count == 1
        assert adapter_factory.adapter.execute_calls == (0 if operator_found else 1)
        assert events == [
            (RunEventKind.ACTION_RECONCILIATION_REQUIRED.value, 2),
            (RunEventKind.ACTION_RECONCILIATION_RESOLVED.value, 2),
            (RunEventKind.ACTION_COMPLETED.value, 2),
        ]
        assert current_round == 2
    finally:
        runtime.close()
