from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

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
