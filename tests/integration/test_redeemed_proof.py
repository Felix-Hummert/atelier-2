"""A check that passed keeps its proof, whatever the attempt did afterwards.

A `tool_redemptions` row is the record of a project's own command having been
satisfied. Until V39 it hung from the agent receipt, which only a succeeded
attempt has, so every ending that came *after* a passing check threw that record
away -- a refused answer, an answer the schema would not admit, work that could
not be kept. All three are facts about the agent, and none of them is a fact
about the project's tests.

So the rule these tests pin is one sentence: the proof is kept whenever the
check passed, and never written when it did not. Both halves matter. Keeping it
too eagerly would let a run whose tests failed leave a record saying they ran
clean, which is the more dangerous of the two mistakes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import RowMapping

from atelier2.adapters.dbos.agent_attempt_store import (
    DbosAgentAttemptStore,
    _keep_tool_redemption,
)
from atelier2.adapters.dbos.run_store import ToolRedemptionConflict
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import agent_attempts, tool_redemptions
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    ToolGrantCapability,
    ToolRedemptionReceipt,
)
from atelier2.ports.agent_attempts import AgentAttemptFailed
from tests.integration.test_v3_agent_refusal import REFUSAL_BYTES, refusing_document
from tests.integration.test_v3_output_enforcement import (
    THE_ANSWER_THE_SCHEMA_REFUSES,
    armed_attempt,
)
from tests.scenarios.agents import (
    agent_scratch_root,
    failing_agent_executor_factory,
)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    """A runtime with no provider that can answer: these tests drive the store.

    Built here rather than borrowed from another file so that the handler
    isolation below wraps the construction. A command-line entry point elsewhere
    in the suite installs a logging handler bound to the standard error pytest
    captured for *that* test, and building a durable runtime flushes every
    handler -- a stream closed long ago then fails a test that never touched it.
    """

    with logging_of_its_own():
        started = DbosRuntime(
            DbosRuntimeSettings(
                tmp_path / "atelier.sqlite",
                "redeemed-proof-test",
                agent_scratch_root=agent_scratch_root(tmp_path),
            ),
            LoopbackEffectAdapterFactory(
                tmp_path / "external.sqlite",
                AdapterRevision("loopback-v1"),
                EffectDestination("loopback-test"),
            ),
            ExactOutputAgentExecutorFactory(),
            (failing_agent_executor_factory("exact", []),),
        )
        started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


@contextmanager
def logging_of_its_own() -> Iterator[None]:
    """Run with the handlers this process installs, not ones left behind.

    Narrow on purpose: it neither silences nor reconfigures anything, it only
    keeps a handler bound to another test's closed capture from being flushed
    by code that legitimately flushes all of them.
    """

    root = logging.getLogger()
    inherited = root.handlers[:]
    root.handlers = []
    try:
        yield
    finally:
        root.handlers = inherited


THE_GRANT = DeclaredToolGrant(
    PublishedRevisionHash("c3" * 32), ToolGrantCapability.RUN_PROJECT_VERIFICATION
)
THE_COMMAND = ("/bin/sh", "-c", "run the project's own tests")
WHAT_THE_CHECK_PRINTED = Sha256Hash.of(b"all green")


def redemption_for(
    execution: AgentAttemptExecution, exit_code: int
) -> ToolRedemptionReceipt:
    """What this attempt's granted check left behind, ending as it is told to."""

    request = execution.request
    return ToolRedemptionReceipt.of(
        request.node_execution_id,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        execution.attempt_id,
        THE_GRANT,
        THE_COMMAND,
        exit_code,
        WHAT_THE_CHECK_PRINTED,
    )


def stored_redemption(
    runtime: DbosRuntime, execution: AgentAttemptExecution
) -> RowMapping | None:
    """The row this attempt's granted check left, or nothing where it left none."""

    with runtime.engine.connect() as connection:
        return (
            connection.execute(
                sa.select(tool_redemptions).where(
                    tool_redemptions.c.attempt_id == execution.attempt_id.value
                )
            )
            .mappings()
            .one_or_none()
        )


@pytest.mark.parametrize(
    ("document", "answer", "failure"),
    [
        pytest.param(
            None,
            THE_ANSWER_THE_SCHEMA_REFUSES,
            AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED,
            id="an-answer-its-own-schema-refuses",
        ),
        pytest.param(
            "refusing",
            REFUSAL_BYTES,
            AgentAttemptFailureCode.AGENT_REFUSED,
            id="an-answer-that-is-a-declared-refusal",
        ),
    ],
)
def test_an_ending_after_a_passing_check_keeps_what_that_check_proved(
    runtime, document: str | None, answer: bytes, failure: AgentAttemptFailureCode
) -> None:
    """The agent's answer decides the ending; it says nothing about the project.

    Both of these endings judge what the *provider* produced, and both happen
    after the project's own command has already run and exited zero. Discarding
    its record because the answer was refused would leave an operator unable to
    tell a project whose tests pass from one whose tests were never satisfied --
    and the two need very different next steps.
    """

    execution = armed_attempt(
        runtime, refusing_document() if document == "refusing" else None
    )
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(
        execution, AgentExecutionResult(answer), redemption_for(execution, 0)
    )

    assert isinstance(outcome, AgentAttemptFailed)
    assert outcome.attempt.failure_code is failure
    kept = stored_redemption(runtime, execution)
    assert kept is not None
    assert kept["exit_code"] == 0
    assert str(kept["capability"]) == ToolGrantCapability.RUN_PROJECT_VERIFICATION.value
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(agent_attempts.c.receipt_hash).where(
                    agent_attempts.c.attempt_id == execution.attempt_id.value
                )
            )
            is None
        )


def test_no_ending_keeps_a_record_of_a_check_that_did_not_pass(runtime) -> None:
    """The other half, and the more dangerous one to get wrong.

    A nonzero check ends the attempt under its own code and redeems nothing, so
    the row must not exist. The writer refuses it by name rather than storing it
    and hoping every reader remembers to look at the exit code.
    """

    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(
        execution,
        AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES),
        redemption_for(execution, 1),
    )

    assert isinstance(outcome, AgentAttemptFailed)
    assert outcome.attempt.failure_code is (
        AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED
    )
    assert stored_redemption(runtime, execution) is None


def test_a_writer_handed_a_failed_check_to_store_refuses_it_by_name(runtime) -> None:
    """The guard under the filter, asked directly.

    Every caller today passes what a passed check left, so this refusal is
    unreachable through them -- which is exactly why it is worth pinning: it is
    what keeps a future caller from turning a failed check into a stored claim
    that it passed.
    """

    execution = armed_attempt(runtime)
    with (
        runtime.engine.begin() as connection,
        pytest.raises(ToolRedemptionConflict, match="a check that passed"),
    ):
        _keep_tool_redemption(connection, execution, redemption_for(execution, 1))
