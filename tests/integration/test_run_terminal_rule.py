from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.run_store import (
    RunTransitionConflict,
    commit_subworkflow_completed,
)
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    initialize_schema,
    run_agent_bindings,
    run_configuration_revisions,
    runs,
    workflow_revisions,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevisionHash,
    AgentRole,
)
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.ports.agent_executions import AgentExecutorRegistry
from tests.scenarios.agents import failing_agent_executor_factory
from tests.scenarios.runs import publish_v3_agent_bindings

_DOCUMENT = b"""format_version: 3
name: Terminal rule fixture
nodes:
  - id: agent
    type: agent
    role: builder
    mode: headless
    instruction: Produce the text the sink stands on.
    outputs:
      - name: text
        schema: {ref: text-schema, revision: schema-text}
  - id: final
    type: agent
    role: builder
    mode: headless
    instruction: Stand as this run's sink.
    depends_on: [agent]
    outputs:
      - name: text
        schema: {ref: text-schema, revision: schema-text}
"""

_RUN_ID = RunId("run-terminal-rule")


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _seed_run_standing_on(engine: Engine, node_id: str) -> WorkflowRevision:
    revision = WorkflowRevision(_DOCUMENT)
    registry = AgentExecutorRegistry((failing_agent_executor_factory("exact", []),))
    bindings = publish_v3_agent_bindings(engine, registry)
    binding_set = AgentBindingSet(
        tuple(
            AgentBinding(
                AgentRole(binding.role),
                AgentConfigurationRevisionHash(
                    binding.agent_configuration_revision_hash
                ),
            )
            for binding in bindings
        )
    )
    configuration_hash = _digest(f"configuration-{_RUN_ID.value}")
    with Session(engine) as session:
        session.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value, document=_DOCUMENT
            )
        )
        session.execute(
            run_configuration_revisions.insert().values(
                revision_hash=configuration_hash,
                preimage=b"seeded terminal-rule run configuration",
            )
        )
        session.execute(
            runs.insert().values(
                run_id=_RUN_ID.value,
                bootstrap_workflow_id=f"bootstrap-{_RUN_ID.value}",
                revision_hash=revision.revision_hash.value,
                workflow_format_version=3,
                run_configuration_revision_hash=configuration_hash,
                agent_binding_set_hash=binding_set.binding_set_hash.value,
                current_node_id=node_id,
                current_round_ordinal=FIRST_ROUND_ORDINAL,
                state=RunState.STARTED.value,
                state_version=0,
                last_event_sequence=0,
            )
        )
        session.execute(
            run_agent_bindings.insert(),
            [
                {
                    "run_id": _RUN_ID.value,
                    "revision_hash": revision.revision_hash.value,
                    "binding_set_hash": binding_set.binding_set_hash.value,
                    "role": binding.role,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash
                    ),
                }
                for binding in bindings
            ],
        )
        session.commit()
    return revision


def _durable_head(engine: Engine) -> tuple[str, str]:
    with Session(engine) as session:
        record = (
            session.execute(sa.select(runs).where(runs.c.run_id == _RUN_ID.value))
            .mappings()
            .one()
        )
    return str(record["state"]), str(record["current_node_id"])


def _durable_terminal_hash(engine: Engine) -> str | None:
    with Session(engine) as session:
        return session.scalar(
            sa.select(runs.c.terminal_hash).where(runs.c.run_id == _RUN_ID.value)
        )


def test_finishing_the_sink_completes_the_run(engine: Engine) -> None:
    revision = _seed_run_standing_on(engine, "final")

    with Session(engine) as session:
        snapshot = commit_subworkflow_completed(
            session, _RUN_ID, revision.revision_hash, "final", 5
        )
        session.commit()

    assert snapshot.state is RunState.COMPLETED
    assert snapshot.current_node_id == "final"
    assert _durable_head(engine) == (RunState.COMPLETED.value, "final")
    assert _durable_terminal_hash(engine) is not None


def test_a_terminal_transition_aimed_at_a_node_with_a_successor_is_refused(
    engine: Engine,
) -> None:
    # The run stands on a node that still has a configured successor, so
    # completing there would strand the rest of the graph behind a COMPLETED
    # head. The refusal is the run-level terminal condition, not a node kind.
    revision = _seed_run_standing_on(engine, "agent")

    with Session(engine) as session:
        with pytest.raises(RunTransitionConflict):
            commit_subworkflow_completed(
                session, _RUN_ID, revision.revision_hash, "agent", 5
            )
        session.rollback()

    assert _durable_head(engine) == (RunState.STARTED.value, "agent")
