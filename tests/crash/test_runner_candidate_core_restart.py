from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.crash.runner_candidate_core_restart_harness import (
    CORE_STARTED_CUT_EXIT_CODE,
    ChildObservation,
    _runner_cgroup_process_evidence,
    observe_provider_child,
    record_child_observation,
    require_claimed_lease,
)
from tests.witness.runner_candidate_core import (
    _CORE_WITNESS_BINDING_FIELDS,
    _read_witness_document,
    _validated_child_observations,
)

HARNESS = Path(__file__).with_name("runner_candidate_core_restart_harness.py")
PROJECT_ROOT = Path(__file__).parents[2]


def _environment() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(PROJECT_ROOT), str(PROJECT_ROOT / "src"))),
    }


def _run(
    root: Path,
    mode: str,
    expected: int = 0,
    connection: socket.socket | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(HARNESS), mode, "--root", str(root)]
    pass_fds: tuple[int, ...] = ()
    if connection is not None:
        command.extend(("--connection", str(connection.fileno())))
        pass_fds = (connection.fileno(),)
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=_environment(),
        pass_fds=pass_fds,
        text=True,
        timeout=30,
    )
    assert completed.returncode == expected, completed.stderr
    return completed


def _rows(root: Path, statement: str) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(root / "core-store" / "core.sqlite3") as connection:
        return tuple(tuple(row) for row in connection.execute(statement))


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.is_file()


def _witness_binding(root: Path) -> dict[str, str]:
    document = json.loads(
        (root / "core-store" / "core-started-cut.json").read_text(encoding="utf-8")
    )
    return {field: document[field] for field in _CORE_WITNESS_BINDING_FIELDS}


def test_restart_reuses_one_generation_while_the_same_provider_child_lives(
    tmp_path: Path,
) -> None:
    first_core, first_runner = socket.socketpair()
    restarted_core, restarted_runner = socket.socketpair()
    runner = subprocess.Popen(
        [
            sys.executable,
            str(HARNESS),
            "runner",
            "--first-connection",
            str(first_runner.fileno()),
            "--restarted-connection",
            str(restarted_runner.fileno()),
        ],
        env=_environment(),
        pass_fds=(first_runner.fileno(), restarted_runner.fileno()),
        start_new_session=True,
    )
    first_runner.close()
    restarted_runner.close()
    restarted: subprocess.Popen[str] | None = None
    try:
        _run(
            tmp_path,
            "seed-and-crash",
            CORE_STARTED_CUT_EXIT_CODE,
            first_core,
        )
        first_core.close()
        binding = _witness_binding(tmp_path)
        first = observe_provider_child(
            "runner-container-one", runner.pid, "2", "2", "0"
        )
        record_child_observation(
            tmp_path / "core-store" / "child-survival.json",
            "after-core-death",
            first,
            binding,
        )

        restarted = subprocess.Popen(
            [
                sys.executable,
                str(HARNESS),
                "restart",
                "--root",
                str(tmp_path),
                "--connection",
                str(restarted_core.fileno()),
            ],
            env=_environment(),
            pass_fds=(restarted_core.fileno(),),
            start_new_session=True,
            text=True,
        )
        restarted_core.close()
        reconnected_started = tmp_path / "core-store" / "core-reconnected-started.json"
        session_finished = (
            tmp_path / "core-store" / "core-reconnect-session-finished.json"
        )
        _wait_for_file(reconnected_started)
        assert restarted.poll() is None
        assert not session_finished.exists()

        second = observe_provider_child(
            "runner-container-one", runner.pid, "2", "2", "0"
        )
        record_child_observation(
            tmp_path / "core-store" / "child-survival.json",
            "after-core-restart",
            second,
            binding,
        )
        assert restarted.wait(timeout=10) == 0
        _wait_for_file(session_finished)
        assert runner.wait(timeout=10) == 0

        assert first.runner_process_id == second.runner_process_id
        assert first.provider_child_pid == second.provider_child_pid
        assert (
            first.provider_child_start_time_ticks
            == second.provider_child_start_time_ticks
        )
        assert first.provider_child_count == "1"
        assert _rows(
            tmp_path,
            "SELECT COUNT(*), COUNT(DISTINCT runner_generation_id), "
            "COUNT(DISTINCT runner_invocation_id) FROM agent_attempts",
        ) == ((1, 1, 1),)
        cut = json.loads(
            (tmp_path / "core-store" / "core-started-cut.json").read_text("utf-8")
        )
        reconnected = json.loads(
            (tmp_path / "core-store" / "core-reconnected-started.json").read_text(
                "utf-8"
            )
        )
        assert reconnected["attempt_id"] == cut["attempt_id"]
        assert reconnected["generation_id"] == cut["generation_id"]
        assert reconnected["invocation_id"] == cut["invocation_id"]
        assert cut["runner_container_id"] == first.runner_container_id
        assert cut["runner_process_id"] == first.runner_process_id
        assert cut["provider_child_pid"] == first.provider_child_pid
        assert (
            cut["provider_child_start_time_ticks"]
            == first.provider_child_start_time_ticks
        )
    finally:
        first_core.close()
        restarted_core.close()
        if restarted is not None and restarted.poll() is None:
            os.killpg(restarted.pid, signal.SIGKILL)
            restarted.wait(timeout=10)
        if runner.poll() is None:
            os.killpg(runner.pid, signal.SIGKILL)
            runner.wait(timeout=10)


@pytest.mark.parametrize(
    ("damage", "refusal"),
    (
        ("missing", "runner-core-reconnect-witness-missing"),
        ("malformed", "runner-core-reconnect-witness-malformed"),
        ("mismatched", "runner-core-reconnect-witness-binding-mismatch"),
    ),
)
def test_restart_refuses_an_unusable_witness(
    tmp_path: Path, damage: str, refusal: str
) -> None:
    _run(tmp_path, "seed-and-crash", CORE_STARTED_CUT_EXIT_CODE)
    bootstrap = tmp_path / "core-store" / "bootstrap.json"
    if damage == "missing":
        bootstrap.unlink()
    elif damage == "malformed":
        bootstrap.write_text("{", encoding="utf-8")
    else:
        document = json.loads(bootstrap.read_text(encoding="utf-8"))
        document["generation_id"] = "different-generation"
        bootstrap.write_text(json.dumps(document), encoding="utf-8")

    completed = _run(tmp_path, "restart", 1)

    assert refusal in completed.stderr
    assert _rows(tmp_path, "SELECT COUNT(*) FROM agent_attempts") == ((1,),)


def test_child_gone_and_released_lease_refuse_before_restart(tmp_path: Path) -> None:
    runner_pid = 123
    children = tmp_path / "proc" / str(runner_pid) / "task" / str(runner_pid)
    children.mkdir(parents=True)
    (children / "children").write_text("", encoding="ascii")

    with pytest.raises(RuntimeError, match="runner-core-reconnect-child-gone"):
        observe_provider_child(
            "runner-container-one", runner_pid, "2", "2", "0", tmp_path / "proc"
        )

    lease_id = "a" * 64
    released = tmp_path / "leases" / "released"
    released.mkdir(parents=True)
    (released / f"{lease_id}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="runner-core-reconnect-lease-not-claimed"):
        require_claimed_lease(tmp_path / "leases", lease_id)


def test_changed_or_duplicate_provider_process_refuses_the_witness(
    tmp_path: Path,
) -> None:
    output = tmp_path / "child-survival.json"
    binding = {field: field for field in _CORE_WITNESS_BINDING_FIELDS}
    first = ChildObservation("container", "10", "11", "100", "S", "1", "2", "2", "0")
    changed = ChildObservation("container", "10", "12", "101", "S", "1", "2", "2", "1")
    record_child_observation(output, "after-core-death", first, binding)

    record_child_observation(output, "after-core-restart", changed, binding)
    with pytest.raises(RuntimeError, match="runner-core-reconnect-pid-changed"):
        _validated_child_observations(tmp_path, binding)

    runner_pid = 20
    children = tmp_path / "proc" / str(runner_pid) / "task" / str(runner_pid)
    children.mkdir(parents=True)
    (children / "children").write_text("21 22", encoding="ascii")
    with pytest.raises(
        RuntimeError, match="runner-core-reconnect-duplicate-provider-processes"
    ):
        observe_provider_child(
            "container", runner_pid, "3", "3", "0", tmp_path / "proc"
        )


def test_child_witness_requires_every_identity_field_and_exact_binding(
    tmp_path: Path,
) -> None:
    binding = {field: field for field in _CORE_WITNESS_BINDING_FIELDS}
    observation = ChildObservation(
        "container", "10", "11", "100", "S", "1", "2", "2", "0"
    )
    output = tmp_path / "child-survival.json"
    record_child_observation(output, "after-core-death", observation, binding)
    record_child_observation(output, "after-core-restart", observation, binding)
    document = json.loads(output.read_text(encoding="utf-8"))
    del document["observations"]["after-core-restart"]["provider_child_pid"]
    output.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="runner-core-reconnect-child-witness-malformed"
    ):
        _validated_child_observations(tmp_path, binding)

    document["observations"]["after-core-restart"] = document["observations"][
        "after-core-death"
    ]
    document["attempt_id"] = "another-attempt"
    output.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(
        RuntimeError, match="runner-core-reconnect-child-witness-malformed"
    ):
        _validated_child_observations(tmp_path, binding)


def test_cgroup_fence_records_the_process_limit_and_limit_hits(
    tmp_path: Path,
) -> None:
    runner_pid = 10
    proc = tmp_path / "proc" / str(runner_pid)
    proc.mkdir(parents=True)
    proc.joinpath("cgroup").write_text("0::/runner-one\n", encoding="ascii")
    cgroup = tmp_path / "cgroup" / "runner-one"
    cgroup.mkdir(parents=True)
    cgroup.joinpath("pids.current").write_text("2\n", encoding="ascii")
    cgroup.joinpath("pids.max").write_text("2\n", encoding="ascii")
    cgroup.joinpath("pids.events").write_text("max 1\n", encoding="ascii")

    assert _runner_cgroup_process_evidence(
        runner_pid, tmp_path / "proc", tmp_path / "cgroup"
    ) == ("2", "2", "1")


@pytest.mark.parametrize("contents", (None, "{", "[]"))
def test_witness_artifact_failures_are_token_typed(
    tmp_path: Path, contents: str | None
) -> None:
    path = tmp_path / "witness.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    refusal = "missing-token" if contents is None else "malformed-token"
    with pytest.raises((RuntimeError, TypeError), match=refusal):
        _read_witness_document(path, "missing-token", "malformed-token")
