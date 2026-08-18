"""What a run standing in one state answers when a transition is written against it.

One table, read as state + event -> state, or state + event -> the refusal by the
name its rule owner gives it. It is written against the transitions where they
live today so that moving them to their own owner can change the import line and
nothing else: a vector whose expected value had to be edited would say the move
was not a move.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    commit_reconciliation_required,
    commit_reconciliation_resolved,
    commit_waiting_input,
)
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    initialize_schema,
    run_events,
    runs,
    workflow_revisions,
)
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)

DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: waiting, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: waiting}
  - {id: agent, type: agent, job: job-17, output: draft-17, next: action}
"""
RUN_ID = RunId("run-transition-vectors")
RESULT = b"7"

WriteTransition = Callable[[Session, WorkflowRevisionHash, str], object]


def begin_waiting(
    session: Session, revision: WorkflowRevisionHash, node: str
) -> object:
    return commit_waiting_input(session, RUN_ID, revision, node)


def require_reconciliation(
    session: Session, revision: WorkflowRevisionHash, node: str
) -> object:
    return commit_reconciliation_required(session, RUN_ID, revision, node, b"request")


def resolve_reconciliation(
    session: Session, revision: WorkflowRevisionHash, node: str
) -> object:
    return commit_reconciliation_resolved(
        session,
        RUN_ID,
        revision,
        node,
        LogicalEffectKey("logical-key"),
        RESULT,
        Sha256Hash.of(RESULT),
    )


VECTORS: tuple[
    tuple[str, str, RunState, WriteTransition, tuple[RunState, str] | None, str | None],
    ...,
] = (
    (
        "a started wait node begins waiting for its answer",
        "waiting",
        RunState.STARTED,
        begin_waiting,
        (RunState.WAITING_INPUT, "waiting"),
        None,
    ),
    (
        "no node but a wait node begins waiting",
        "action",
        RunState.STARTED,
        begin_waiting,
        None,
        "WAITING_INPUT target is not a Wait node",
    ),
    (
        "a started action begins reconciling",
        "action",
        RunState.STARTED,
        require_reconciliation,
        (RunState.WAITING_RECONCILIATION, "action"),
        None,
    ),
    (
        "no node but an action begins reconciling",
        "agent",
        RunState.STARTED,
        require_reconciliation,
        None,
        "WAITING_RECONCILIATION target is not an Action",
    ),
    (
        "a run that is not reconciling has no resolution to take",
        "action",
        RunState.STARTED,
        resolve_reconciliation,
        None,
        "run is not at the transition's exact source",
    ),
    (
        "a waiting run does not begin waiting twice",
        "waiting",
        RunState.WAITING_INPUT,
        begin_waiting,
        None,
        "run is not at the transition's exact source",
    ),
    (
        "a waiting state standing on no wait node is a run that disagrees with itself",
        "action",
        RunState.WAITING_INPUT,
        begin_waiting,
        None,
        "WAITING_INPUT must name a Wait node",
    ),
    (
        "a reconciling state standing on no action is a run that disagrees with itself",
        "waiting",
        RunState.WAITING_RECONCILIATION,
        resolve_reconciliation,
        None,
        "WAITING_RECONCILIATION must name an Action node",
    ),
    (
        "a run standing on a node its graph does not carry transitions nowhere",
        "ghost",
        RunState.STARTED,
        begin_waiting,
        None,
        "run current node is absent from its workflow graph",
    ),
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def standing(engine: Engine, node: str, state: RunState) -> WorkflowRevisionHash:
    revision = WorkflowRevision(DOCUMENT)
    with Session(engine) as session:
        session.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value, document=DOCUMENT
            )
        )
        session.execute(
            runs.insert().values(
                run_id=RUN_ID.value,
                bootstrap_workflow_id=f"bootstrap-{RUN_ID.value}",
                revision_hash=revision.revision_hash.value,
                workflow_format_version=1,
                current_node_id=node,
                state=state.value,
                state_version=0,
                last_event_sequence=0,
            )
        )
        session.commit()
    return revision.revision_hash


def durable_head(engine: Engine) -> tuple[RunState, str]:
    with Session(engine) as session:
        record = (
            session.execute(sa.select(runs).where(runs.c.run_id == RUN_ID.value))
            .mappings()
            .one()
        )
    return RunState(str(record["state"])), str(record["current_node_id"])


def written_events(engine: Engine) -> int:
    with Session(engine) as session:
        return len(
            tuple(
                session.execute(
                    sa.select(run_events.c.event_sequence).where(
                        run_events.c.run_id == RUN_ID.value
                    )
                ).scalars()
            )
        )


@pytest.mark.proves("a-run-transition-answers-one-state-or-one-named-refusal")
@pytest.mark.parametrize(
    ("node", "state", "write", "admits", "refuses"),
    [vector[1:] for vector in VECTORS],
    ids=[vector[0] for vector in VECTORS],
)
def test_a_run_transition_answers_its_vector(
    engine: Engine,
    node: str,
    state: RunState,
    write: WriteTransition,
    admits: tuple[RunState, str] | None,
    refuses: str | None,
) -> None:
    revision = standing(engine, node, state)

    with Session(engine) as session:
        if refuses is None:
            write(session, revision, node)
            session.commit()
        else:
            with pytest.raises(RunTransitionConflict, match=refuses):
                write(session, revision, node)

    assert durable_head(engine) == (admits or (state, node))
    assert written_events(engine) == (0 if refuses else 1)


def test_writing_the_same_transition_twice_leaves_one_event(engine: Engine) -> None:
    """A replayed step re-answers from the event it already wrote, and writes none."""

    revision = standing(engine, "waiting", RunState.STARTED)

    with Session(engine) as session:
        first = begin_waiting(session, revision, "waiting")
        session.commit()
    with Session(engine) as session:
        replayed = begin_waiting(session, revision, "waiting")
        session.commit()

    assert replayed == first
    assert durable_head(engine) == (RunState.WAITING_INPUT, "waiting")
    assert written_events(engine) == 1
