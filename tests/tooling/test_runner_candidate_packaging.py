from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "atelier2"
LAUNCHER = PROJECT_ROOT / "scripts" / "runner_candidate.sh"
CARRIER = SOURCE_ROOT / "adapters" / "docker_carrier.py"
COMPOSE = PROJECT_ROOT / "compose.yaml"
ENGINE_EXECUTABLE = "/usr/bin/docker"
# Every spelling by which a program reaches the engine: its own name, however it
# would be found on `PATH` or addressed outright, and the socket that is the
# engine's actual authority. A module that names none of these has no way to
# call it -- including through an argument vector, which a substring search over
# whole files would have walked straight past.
ENGINE_NAMES = frozenset(
    {"docker", ENGINE_EXECUTABLE, "/var/run/docker.sock", "docker.sock"}
)
STABLE_FILES = (
    "Dockerfile",
    "compose.yaml",
    "scripts/container_live.sh",
    "scripts/container_up.sh",
)


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py")))


def test_core_and_runner_source_have_no_docker_client() -> None:
    for path in _python_files(SOURCE_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".", 1)[0] != "docker" for alias in node.names
                )
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert node.module.split(".", 1)[0] != "docker"


def _engine_names_in(module: Path) -> set[str]:
    """Every engine spelling this module could hand to anything, at all.

    Read out of the parsed source rather than searched for in the text, because
    the way a program calls the engine is an argument vector -- `["docker",
    "run", ...]` -- in which no substring of a call ever appears. Only a
    constant that *is* one of these names counts, so prose that merely mentions
    Docker -- a comment, a docstring, a refusal sentence -- is not a call and is
    not counted.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in ENGINE_NAMES
    }


def test_the_carrier_is_the_only_source_module_that_can_reach_the_engine() -> None:
    """Docker authority is spent in one owner and nowhere else in the source.

    The host launcher's carrier holds it (ADR 0009 sec. 2, `#540` ruling B);
    every other source module ships inside a Core or Runner image, where the
    ability to reach the engine would be exactly the privilege the whole
    arrangement exists to withhold. The witness drives its launcher operations
    through that same owner rather than through a second, shell-shaped copy.
    """
    assert ENGINE_EXECUTABLE in _engine_names_in(CARRIER)
    for path in _python_files(SOURCE_ROOT):
        if path != CARRIER:
            assert _engine_names_in(path) == set(), path
    for path in (PROJECT_ROOT / "src").rglob("*.sh"):
        assert not {name for name in ENGINE_NAMES if name in path.read_text("utf-8")}
    assert "python -m atelier2.adapters.docker_carrier" in LAUNCHER.read_text(
        encoding="utf-8"
    )


def test_the_stable_console_is_given_no_engine_authority() -> None:
    """The console runs the product; it never gets to run containers.

    This is ADR 0009 sec. 2's actual stop condition, and the reason the host
    launcher exists at all: a Serve compromise must not be host root. A mounted
    engine socket or a privileged service would hand it exactly that, so the
    deployment descriptor is read for both.
    """
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "docker.sock" not in compose
    assert "privileged" not in compose
    assert "cap_add" not in compose


def test_stable_serve_files_do_not_adopt_candidate_packaging() -> None:
    """The disposable candidate ships beside the stable serve stack, never in it."""
    for name in STABLE_FILES:
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert "runner_candidate" not in text
        assert "Dockerfile.runner" not in text


def test_the_witness_copies_public_bootstrap_and_keeps_core_handoff_read_only() -> None:
    """Core reads its handoff and never writes it.

    The public bootstrap leaves Core the same way it always did -- copied out
    of the container -- and the directory it is copied into is bound back into
    Core read-only, so the attestation Core reads from it is one nothing inside
    Core could have written.
    """
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "carrier copy-from --container" in text
    assert "--source /var/lib/atelier2-candidate/bootstrap.json" in text
    assert '"$attempt_root/handoff:/handoff:ro"' in text
    assert '"$attempt_root/handoff:/handoff"' not in text.replace(
        '"$attempt_root/handoff:/handoff:ro"', ""
    )
    assert "unlink-private" in text
    core = (PROJECT_ROOT / "tests/witness/runner_candidate_core.py").read_text(
        encoding="utf-8"
    )
    assert "inspect-attested" in core
    assert 'handoff / "inspect-attested"' in core
    assert "_write_json(inspect_attested" not in core
    assert 'handoff / "inspect-attested").write' not in core


def test_the_witness_records_the_attempt_network_the_launcher_reported() -> None:
    """A concurrent `clean` must never see a recorded network before it exists,
    or it could mistake a still-running witness for a released one. The witness
    does not name the network at all any more: it records the one the launcher
    says it created, which cannot precede the creation."""
    text = LAUNCHER.read_text(encoding="utf-8")
    reported_at = text.index("sed -n 's/^attempt-network=//p'")
    recorded_at = text.index('>"$root/network"')
    assert reported_at < recorded_at


def test_the_live_core_restart_runs_while_the_launcher_owns_the_runner() -> None:
    """The live host leg makes same-child identity an explicit pass condition."""
    text = LAUNCHER.read_text(encoding="utf-8")
    cut_fifos_created = text.index('mkfifo "$root/core-store/core-started-cut.event"')
    cut_fence_started = text.index("freeze-started-child", cut_fifos_created)
    fence_requested = text.index("--enforce-current-process-limit", cut_fence_started)
    launcher_started = text.index('--runner-image "$runner_image" --once >')
    launcher_backgrounded = text.index("launcher_pid=$!", launcher_started)
    first_core_wait = text.index(
        'core_cut_status=$(carrier wait --container "$core")', launcher_backgrounded
    )
    cut_fence_waited = text.index('wait "$cut_fence_pid"', first_core_wait)
    started_child_read = text.index("read-started-child", cut_fence_waited)
    child_observed_after_death = text.index(
        "--phase after-core-death", started_child_read
    )
    first_identity_compared = text.index(
        'after-core-death "$started_child_identity"',
        child_observed_after_death,
    )
    event_wait_started = text.index("marker_wait_pid=$!", child_observed_after_death)
    core_restarted = text.index("restart-private-core", child_observed_after_death)
    base_reattached = text.index(
        'docker network connect "$base_network" "$core"', core_restarted
    )
    policy_reattached = text.index(
        'carrier attach-policed --container "$core"', base_reattached
    )
    reconnected_started = text.index(
        '"$root/core-store/core-reconnected-started.json"', policy_reattached
    )
    child_observed_after_reconnect = text.index(
        "--phase after-core-restart", reconnected_started
    )
    identity_compared = text.index(
        'after-core-restart "$started_child_identity"',
        child_observed_after_reconnect,
    )
    launcher_waited = text.index('wait "$launcher_pid"', identity_compared)

    assert cut_fifos_created < cut_fence_started < fence_requested < launcher_started
    assert launcher_started < launcher_backgrounded < first_core_wait < cut_fence_waited
    assert cut_fence_waited < started_child_read < child_observed_after_death
    assert child_observed_after_death < first_identity_compared < event_wait_started
    assert event_wait_started < core_restarted
    assert core_restarted < base_reattached < policy_reattached
    assert policy_reattached < reconnected_started
    assert reconnected_started < child_observed_after_reconnect < identity_compared
    assert identity_compared < launcher_waited
    assert 'docker start "$core"' not in text
    assert "sleep 0.05" not in text
    for refusal in (
        "runner-core-reconnect-container-changed",
        "runner-core-reconnect-pid-changed",
        "runner-core-reconnect-start-tick-changed",
        "runner-core-reconnect-child-count-changed",
        "runner-core-reconnect-cgroup-changed",
    ):
        assert refusal in text
    assert "runner-core-reconnect-started-deadline" in text
    assert "runner-core-reconnect-started-event-mismatch" in text


def test_the_restart_gate_retains_witnesses_and_requires_released_cleanup() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")

    assert 'scenario="core-restart"' in text
    assert '"$root/core-store/child-survival.json"' in text
    assert '"$root/core-store/live-restart-proof.json"' in text
    assert '"$root/leases/released/${lease_id}.json"' in text
    assert '"$root/labelled-object-inventory.json"' in text
    assert '"$root/store.sha256"' in text
    assert "runner_cgroup_limit_hit_count" in (
        PROJECT_ROOT / "tests" / "crash" / "runner_candidate_core_restart_harness.py"
    ).read_text(encoding="utf-8")


def test_the_docker_restart_witness_is_not_described_as_a_deterministic_test() -> None:
    runtime = (PROJECT_ROOT / "docs" / "product" / "runtime.md").read_text(
        encoding="utf-8"
    )
    decision = (PROJECT_ROOT / "docs" / "decisions" / "0009-runner-trust.md").read_text(
        encoding="utf-8"
    )

    assert "live Docker witness, not a deterministic test" in " ".join(runtime.split())
    assert "live Docker witness, not a deterministic test" in " ".join(decision.split())
