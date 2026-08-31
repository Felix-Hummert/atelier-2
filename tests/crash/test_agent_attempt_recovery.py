from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_attempt_receipts_v3,
    agent_attempts,
    agent_receipts_v2,
    runs,
)
from atelier2.adapters.dbos.workflow import AgentExecutorMap, reconstruct_agent_attempt
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.hashing import Sha256Hash
from atelier2.ports.agent_executions import AgentExecutionResult
from tests.integration.test_v3_output_enforcement import (
    THE_ANSWER_THE_SCHEMA_ADMITS,
    THE_ANSWER_THE_SCHEMA_REFUSES,
    armed_attempt,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    failing_agent_executor_factory,
)

CRASHED = 86
HARNESS = Path(__file__).with_name("agent_attempt_harness.py")


def child(root: Path, mode: str, expected: int = 0) -> None:
    result = subprocess.run(
        [sys.executable, str(HARNESS), str(root), mode],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (
                    str(Path(__file__).parents[2]),
                    str(Path(__file__).parents[2] / "src"),
                )
            ),
        },
        text=True,
        timeout=20,
    )
    assert result.returncode == expected, result.stderr


def rows(root: Path, statement: str) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        return tuple(tuple(record) for record in connection.execute(statement))


def output_schema_runtime(
    root: Path, executor_factory: RecordingAgentExecutorFactoryV2 | None = None
) -> DbosRuntime:
    started = DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "v3-output-contract-test",
            agent_scratch_root=agent_scratch_root(root),
        ),
        LoopbackEffectAdapterFactory(
            root / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (
            failing_agent_executor_factory("exact", [])
            if executor_factory is None
            else executor_factory,
        ),
    )
    started.initialize_storage()
    return started


def wait_for_run_state(runtime: DbosRuntime, state: str) -> None:
    deadline = time.monotonic() + 10
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(connection.scalar(sa.select(runs.c.state)))
        if observed == state:
            return
        time.sleep(0.01)
    raise AssertionError(f"run stayed {observed!r}, expected {state!r}")


def _executor_map_of(runtime: DbosRuntime) -> AgentExecutorMap:
    return {
        entry.key: (
            None,
            entry.manifest_entry.operational_identity,
            entry.manifest_entry.declared_capabilities,
            entry.manifest_entry.carrier,
        )
        for entry in runtime.agent_executor_registry.entries
    }


def test_restart_after_a_schema_refusal_runs_its_repair_once(
    tmp_path: Path,
) -> None:
    """The boundary after round one survives a process loss with one repair id."""
    first_executor = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        THE_ANSWER_THE_SCHEMA_ADMITS,
    )
    first = output_schema_runtime(tmp_path, first_executor)
    try:
        execution = armed_attempt(first)
        DbosAgentAttemptStore(
            first.engine, first.settings.application_version
        ).complete_success(
            execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
        )
        with first.engine.connect() as connection:
            durable_receipt = (
                connection.execute(sa.select(agent_attempt_receipts_v3))
                .mappings()
                .one()
            )
            repair_before = (
                connection.execute(
                    sa.select(agent_attempts).where(
                        agent_attempts.c.attempt_ordinal == 2
                    )
                )
                .mappings()
                .one()
            )
        durable_repair = DbosAgentAttemptStore(
            first.engine, first.settings.application_version
        ).load(AgentAttemptId(str(repair_before["attempt_id"])))
        reconstructed_before = reconstruct_agent_attempt(
            first.datasource,
            _executor_map_of(first),
            first.declared_project,
            durable_repair,
        ).execution
        assert durable_receipt["attempt_id"] == execution.attempt_id.value
        assert (
            durable_receipt["value_hash"]
            == Sha256Hash.of(THE_ANSWER_THE_SCHEMA_REFUSES).value
        )
        assert reconstructed_before.ordinal == 2
        assert (
            reconstructed_before.request.round_ordinal
            == execution.request.round_ordinal
        )
        assert reconstructed_before.request.job_bytes != execution.request.job_bytes
        assert reconstructed_before.request.request_hash == durable_repair.request_hash
        assert first_executor.opened is not None
        assert first_executor.opened.requests == []
    finally:
        first.close()

    recovered_executor = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        THE_ANSWER_THE_SCHEMA_ADMITS,
    )
    recovered = output_schema_runtime(tmp_path, recovered_executor)
    try:
        recovered.launch()
        wait_for_run_state(recovered, "COMPLETED")
        with recovered.engine.connect() as connection:
            attempts = tuple(
                connection.execute(
                    sa.select(agent_attempts.c.attempt_ordinal).order_by(
                        agent_attempts.c.attempt_ordinal
                    )
                )
            )
            receipts = connection.scalar(
                sa.select(sa.func.count()).select_from(agent_receipts_v2)
            )
            repair_state = connection.scalar(
                sa.select(agent_attempts.c.state).where(
                    agent_attempts.c.attempt_ordinal == 2
                )
            )
        assert attempts == ((1,), (2,))
        assert receipts == 1
        assert repair_state == "SUCCEEDED"
        assert recovered_executor.opened is not None
        assert len(recovered_executor.opened.requests) == 1
        recovered_request = recovered_executor.opened.requests[0]
        assert (
            recovered_request.round_ordinal
            == reconstructed_before.request.round_ordinal
        )
        assert recovered_request.job_bytes == reconstructed_before.request.job_bytes
        assert (
            repair_before["attempt_id"],
            repair_before["request_hash"],
        ) == (
            AgentAttemptId.for_execution(
                recovered_request.node_execution_id,
                recovered_request.request_hash,
                2,
            ).value,
            recovered_request.request_hash.value,
        )
    finally:
        recovered.close()


def test_restart_during_round_two_reconstructs_one_exact_adapter_request(
    tmp_path: Path,
) -> None:
    """A real process death after repair reconstruction does not duplicate it."""
    child(tmp_path, "crash-output-schema-repair-before-adapter", CRASHED)
    precrash = json.loads(
        (tmp_path / "precrash-repair.json").read_text(encoding="utf-8")
    )
    assert precrash["adapter_requests"] == 0

    inspector_factory = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        THE_ANSWER_THE_SCHEMA_ADMITS,
    )
    inspector = output_schema_runtime(tmp_path, inspector_factory)
    try:
        with inspector.engine.connect() as connection:
            refusal_receipt = (
                connection.execute(sa.select(agent_attempt_receipts_v3))
                .mappings()
                .one()
            )
            repair_id = AgentAttemptId(
                str(
                    connection.scalar(
                        sa.select(agent_attempts.c.attempt_id).where(
                            agent_attempts.c.attempt_ordinal == 2
                        )
                    )
                )
            )
        store = DbosAgentAttemptStore(
            inspector.engine, inspector.settings.application_version
        )
        durable = store.load(repair_id)
        reconstructed = reconstruct_agent_attempt(
            inspector.datasource,
            _executor_map_of(inspector),
            inspector.declared_project,
            durable,
        ).execution
        durable_evidence = {
            "attempt_id": durable.attempt_id.value,
            "attempt_ordinal": durable.attempt_ordinal,
            "round_ordinal": reconstructed.request.round_ordinal,
            "job_bytes_base64": base64.b64encode(
                reconstructed.request.job_bytes
            ).decode("ascii"),
            "request_hash": durable.request_hash.value,
        }
        assert refusal_receipt["attempt_id"] != durable.attempt_id.value
        assert inspector_factory.opened is not None
        assert inspector_factory.opened.requests == []
    finally:
        inspector.close()

    child(tmp_path, "recover-output-schema-repair")
    recovered = json.loads(
        (tmp_path / "recovered-repair.json").read_text(encoding="utf-8")
    )
    assert recovered["adapter_requests"] == 1
    evidence_keys = (
        "attempt_id",
        "attempt_ordinal",
        "round_ordinal",
        "job_bytes_base64",
        "request_hash",
    )
    assert (
        tuple(durable_evidence[key] for key in evidence_keys)
        == tuple(precrash[key] for key in evidence_keys)
        == tuple(recovered[key] for key in evidence_keys)
    )


def test_restart_reclaims_prepared_but_only_projects_launch_armed_as_possibly_ran(
    tmp_path: Path,
) -> None:
    prepared = tmp_path / "prepared"
    child(prepared, "crash-prepared", CRASHED)
    assert rows(prepared, "SELECT state,state_version FROM agent_attempts") == (
        ("PREPARED", 0),
    )
    child(prepared, "recover")
    assert (prepared / "counter").read_bytes() == b"x"
    assert rows(prepared, "SELECT state,state_version FROM agent_attempts") == (
        ("SUCCEEDED", 4),
    )

    armed = tmp_path / "armed"
    child(armed, "crash-armed", CRASHED)
    assert (armed / "counter").read_bytes() == b"x"
    assert rows(armed, "SELECT state,state_version FROM agent_attempts") == (
        ("LAUNCH_ARMED", 1),
    )
    child(armed, "recover")
    assert (armed / "counter").read_bytes() == b"x"
    assert (armed / "projected-attempt-state").read_text(encoding="utf-8") == (
        "POSSIBLY_RAN"
    )
    assert rows(armed, "SELECT state,state_version FROM agent_attempts") == (
        ("LAUNCH_ARMED", 1),
    )


def test_controlled_process_counter_proves_the_launch_armed_boundary(
    tmp_path: Path,
) -> None:
    child(tmp_path, "crash-armed", CRASHED)
    child(tmp_path, "recover")

    assert (tmp_path / "counter").read_bytes() == b"x"
    assert rows(
        tmp_path,
        "SELECT state,current_node_id,state_version,last_event_sequence FROM runs",
    ) == (("STARTED", "build", 0, 0),)
    assert rows(tmp_path, "SELECT COUNT(*) FROM agent_receipts_v2") == ((0,),)
    assert rows(tmp_path, "SELECT COUNT(*) FROM run_events") == ((0,),)


def test_restart_finishes_cancellation_after_the_process_owner_dies(
    tmp_path: Path,
) -> None:
    child(tmp_path, "crash-running", CRASHED)
    provider_pid = int((tmp_path / "running-pid").read_text(encoding="ascii"))

    child(tmp_path, "recover-cancellation")

    assert (tmp_path / "recovered-cancellation-state").read_text(
        encoding="ascii"
    ) == "INTERRUPTED"
    assert not Path(f"/proc/{provider_pid}").exists()
    assert rows(
        tmp_path,
        "SELECT state,process_phase,redrive_state,cancellation_disposition FROM agent_attempts",
    ) == (
        (
            "INTERRUPTED",
            "CLEANUP_ATTESTED",
            "CLEANUP_ATTESTED",
            "OWNER_LOST_AFTER_PARENT_DEATH",
        ),
    )


def test_restart_lifts_an_operator_cancelled_run_and_keeps_its_hash_across_a_second_restart(
    tmp_path: Path,
) -> None:
    """#439 P3, Fenster (i): the operator's own command survives the crash.

    `test_restart_finishes_cancellation_after_the_process_owner_dies` above
    proves the attempt's own recovery under a foreign command; this is the
    same crash, but under the operator's `is_operator_run_cancel` command --
    so this restart must also lift the run to `CANCELLED`, not leave it
    `STARTED`. A second restart against the exact same durable state proves
    the terminal hash, the run's word, and the event log are byte-stable: a
    retry against an already-terminal run writes nothing new.
    """
    child(tmp_path, "crash-running", CRASHED)
    provider_pid = int((tmp_path / "running-pid").read_text(encoding="ascii"))

    child(tmp_path, "recover-operator-cancellation")

    assert not Path(f"/proc/{provider_pid}").exists()
    assert rows(
        tmp_path,
        "SELECT state,process_phase,redrive_state,cancellation_disposition FROM agent_attempts",
    ) == (
        (
            "INTERRUPTED",
            "CLEANUP_ATTESTED",
            "CLEANUP_ATTESTED",
            "OWNER_LOST_AFTER_PARENT_DEATH",
        ),
    )
    run_after_first_restart = rows(tmp_path, "SELECT state,terminal_hash FROM runs")
    assert len(run_after_first_restart) == 1
    run_state, terminal_hash = run_after_first_restart[0]
    assert run_state == "CANCELLED"
    assert terminal_hash is not None
    events_after_first_restart = rows(
        tmp_path, "SELECT event_kind FROM run_events ORDER BY event_sequence"
    )
    assert events_after_first_restart == (
        ("AGENT_CANCEL_REQUESTED",),
        ("AGENT_INTERRUPTED",),
    )
    # The harness's own workflow document is format 2: #439 P3's receipt is a
    # V3-only `node-receipt/v3` (proved directly, with a V3 run, in
    # `tests/integration/test_run_cancellation.py`); a format-2 run stays
    # honestly receipt-less here, the same as every other leftover family
    # `tests/integration/test_interrupted_uncontinuable_inventory.py` proves.
    assert rows(tmp_path, "SELECT COUNT(*) FROM node_receipts_v3") == ((0,),)

    child(tmp_path, "recover-operator-cancellation")

    assert (
        rows(tmp_path, "SELECT state,terminal_hash FROM runs")
        == run_after_first_restart
    )
    assert (
        rows(tmp_path, "SELECT event_kind FROM run_events ORDER BY event_sequence")
        == events_after_first_restart
    )


def test_clean_runtime_close_retains_the_witness_until_recovery_attests(
    tmp_path: Path,
) -> None:
    child(tmp_path, "close-running")
    provider_pid = int((tmp_path / "closed-running-pid").read_text(encoding="ascii"))
    cgroup = Path((tmp_path / "closed-cgroup").read_text(encoding="utf-8"))
    endpoint = Path((tmp_path / "closed-endpoint").read_text(encoding="utf-8"))

    assert not Path(f"/proc/{provider_pid}").exists()
    assert cgroup.is_dir()
    assert endpoint.is_socket()
    assert rows(tmp_path, "SELECT state,process_phase FROM agent_attempts") == (
        ("LAUNCH_ARMED", "PROCESS_OBSERVED"),
    )

    child(tmp_path, "recover-cancellation")

    assert not cgroup.exists()
    assert not endpoint.exists()
    assert rows(
        tmp_path,
        "SELECT state,process_phase,redrive_state,cancellation_disposition FROM agent_attempts",
    ) == (
        (
            "INTERRUPTED",
            "CLEANUP_ATTESTED",
            "CLEANUP_ATTESTED",
            "OWNER_LOST_AFTER_PARENT_DEATH",
        ),
    )


def test_restart_kills_a_session_escaped_descendant_from_only_the_cgroup(
    tmp_path: Path,
) -> None:
    child(tmp_path, "crash-watchdog-and-running-descendant", CRASHED)
    descendant_pid = int((tmp_path / "descendant-pid").read_text(encoding="ascii"))
    try:
        assert Path(f"/proc/{descendant_pid}").exists()

        child(tmp_path, "recover-cancellation")

        assert (tmp_path / "recovered-cancellation-state").read_text(
            encoding="ascii"
        ) == "INTERRUPTED"
        assert not Path(f"/proc/{descendant_pid}").exists()
        assert rows(
            tmp_path,
            "SELECT state,process_phase,redrive_state,cancellation_disposition FROM agent_attempts",
        ) == (
            (
                "INTERRUPTED",
                "CLEANUP_ATTESTED",
                "CLEANUP_ATTESTED",
                "OWNER_LOST_AFTER_PARENT_DEATH",
            ),
        )
    finally:
        if Path(f"/proc/{descendant_pid}").exists():
            os.kill(descendant_pid, 9)


def test_restart_attests_cleanup_when_the_host_already_removed_the_witness(
    tmp_path: Path,
) -> None:
    child(tmp_path, "crash-running", CRASHED)

    child(tmp_path, "recover-without-witness")

    assert (tmp_path / "witnessless-recovery-state").read_text(
        encoding="ascii"
    ) == "INTERRUPTED"


def test_a_serve_start_converges_a_run_whose_driver_workflow_died(
    tmp_path: Path,
) -> None:
    child(tmp_path, "crash-running", CRASHED)
    provider_pid = int((tmp_path / "running-pid").read_text(encoding="ascii"))
    assert rows(
        tmp_path, "SELECT state,cancellation_command_id FROM agent_attempts"
    ) == (("LAUNCH_ARMED", None),)

    child(tmp_path, "converge-driverless")

    cgroup = Path((tmp_path / "driverless-cgroup").read_text(encoding="utf-8"))
    endpoint = Path((tmp_path / "driverless-endpoint").read_text(encoding="utf-8"))
    assert (tmp_path / "converged-state").read_text(encoding="ascii") == "INTERRUPTED"
    assert not Path(f"/proc/{provider_pid}").exists()
    assert not cgroup.exists()
    assert not endpoint.exists()
    assert rows(
        tmp_path,
        "SELECT state,cancellation_command_id,cancellation_disposition FROM agent_attempts",
    ) == (("INTERRUPTED", "atelier2-driver-lost", "OWNER_LOST_AFTER_PARENT_DEATH"),)
    assert rows(
        tmp_path, "SELECT event_kind,cancellation_command_id FROM run_events"
    ) == (
        ("AGENT_CANCEL_REQUESTED", "atelier2-driver-lost"),
        ("AGENT_INTERRUPTED", "atelier2-driver-lost"),
    )


def test_a_serve_start_converges_even_where_the_unit_took_the_witness_with_it(
    tmp_path: Path,
) -> None:
    child(tmp_path, "crash-running", CRASHED)

    child(tmp_path, "converge-driverless-without-witness")

    endpoint = Path((tmp_path / "driverless-endpoint").read_text(encoding="utf-8"))
    assert (tmp_path / "converged-state").read_text(encoding="ascii") == "INTERRUPTED"
    assert not endpoint.exists()
    assert rows(
        tmp_path, "SELECT state,cancellation_disposition FROM agent_attempts"
    ) == (("INTERRUPTED", "OWNER_LOST_AFTER_PARENT_DEATH"),)


def test_restart_converges_after_cleanup_precedes_durable_attestation(
    tmp_path: Path,
) -> None:
    child(tmp_path, "crash-after-cleanup-before-attestation", CRASHED)
    cgroup = Path((tmp_path / "cleanup-cgroup").read_text(encoding="utf-8"))
    endpoint = Path((tmp_path / "cleanup-endpoint").read_text(encoding="utf-8"))

    assert (tmp_path / "cleanup-disposition").read_text(
        encoding="ascii"
    ) == "REAPED_AFTER_TERM"
    assert cgroup.is_dir()
    assert endpoint.is_socket()
    assert rows(tmp_path, "SELECT state FROM agent_attempts") == (
        ("CANCEL_REQUESTED",),
    )

    child(tmp_path, "recover-cancellation")
    child(tmp_path, "recover-cancellation")

    assert not cgroup.exists()
    assert not endpoint.exists()
    assert rows(
        tmp_path,
        "SELECT state,process_phase,redrive_state,cancellation_disposition FROM agent_attempts",
    ) == (
        (
            "INTERRUPTED",
            "CLEANUP_ATTESTED",
            "CLEANUP_ATTESTED",
            "OWNER_LOST_AFTER_PARENT_DEATH",
        ),
    )


def test_restart_only_releases_after_attestation_precedes_witness_gc(
    tmp_path: Path,
) -> None:
    child(tmp_path, "crash-after-attestation-before-release", CRASHED)
    cgroup = Path((tmp_path / "attested-cgroup").read_text(encoding="utf-8"))
    endpoint = Path((tmp_path / "attested-endpoint").read_text(encoding="utf-8"))
    state_version = int(
        (tmp_path / "attested-state-version").read_text(encoding="ascii")
    )

    assert cgroup.is_dir()
    assert endpoint.is_socket()
    before = rows(
        tmp_path,
        "SELECT state,state_version,process_phase,redrive_state,cancellation_disposition FROM agent_attempts",
    )
    assert before == (
        (
            "CANCELLED",
            state_version,
            "CLEANUP_ATTESTED",
            "CLEANUP_ATTESTED",
            "REAPED_AFTER_TERM",
        ),
    )

    child(tmp_path, "recover-cancellation")
    child(tmp_path, "recover-cancellation")

    assert not cgroup.exists()
    assert not endpoint.exists()
    assert (
        rows(
            tmp_path,
            "SELECT state,state_version,process_phase,redrive_state,cancellation_disposition FROM agent_attempts",
        )
        == before
    )


def test_a_candidate_kept_before_the_crash_outlives_the_attempt_that_never_ended(
    tmp_path: Path,
) -> None:
    """The order is the invariant, so the crash between the two writes is the proof.

    The candidate ref and the terminal attempt cannot be written together -- one
    is a git object, the other a durable row -- so what stops a run from
    claiming work it cannot show is that the ref goes first. A process dying in
    exactly that gap must therefore leave the work readable and the attempt not
    succeeded, which is the safe half of the two.
    """

    root = tmp_path / "kept"
    child(root, "crash-once-the-candidate-is-kept", CRASHED)

    assert rows(root, "SELECT state FROM agent_attempts") == (("LAUNCH_ARMED",),)
    child(root, "read-candidate")
    assert (root / "kept-tree").read_text(encoding="utf-8") == (
        root / "pinned-tree"
    ).read_text(encoding="utf-8")


def test_the_attempt_whose_candidate_survived_still_reports_it_possibly_ran(
    tmp_path: Path,
) -> None:
    """Kept work does not settle an armed attempt; only its own store can.

    A candidate standing in the store says the work exists, never that the
    attempt finished. Reading it as an ending would turn every crash after a
    capture into a silent success, so the recovery answers exactly what it did
    before this slice: this attempt possibly ran, and a human decides.
    """

    root = tmp_path / "kept-then-recovered"
    child(root, "crash-once-the-candidate-is-kept", CRASHED)

    child(root, "recover")

    assert (root / "projected-attempt-state").read_text(encoding="utf-8") == (
        "POSSIBLY_RAN"
    )
