from __future__ import annotations

import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from atelier2.adapters.runner_child import (
    LandlockUnavailable,
    install_landlock_guard,
    reap_cancelled_runner_child,
    start_runner_child,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerCancellationObservation,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.runner_manifests import (
    RunnerManifestV1,
    candidate_runner_manifest,
)
from atelier2.runner.session import (
    _control_or_child_exit,
    _CoreFrameFence,
    _reap_child,
)


@pytest.mark.proves("runner-child-landlock")
def test_landlock_guard_denies_a_child_direct_read_of_runner_identity(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "workspace"
    identity = tmp_path / "identity"
    allowed.mkdir()
    identity.mkdir()
    (allowed / "job.txt").write_text("permitted", encoding="utf-8")
    key = identity / "client.key"
    key.write_text("not-for-child", encoding="utf-8")
    code = (
        "from pathlib import Path\n"
        "from atelier2.adapters.runner_child import install_landlock_guard\n"
        f"install_landlock_guard((Path({str(allowed)!r}),))\n"
        f"assert Path({str(allowed / 'job.txt')!r}).read_text() == 'permitted'\n"
        "try:\n"
        f"    Path({str(key)!r}).read_bytes()\n"
        "except PermissionError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(23)\n"
    )

    result = subprocess.run(
        (sys.executable, "-c", code), capture_output=True, check=False, text=True
    )

    assert result.returncode == 0, result.stderr


def test_landlock_refusal_is_loud_when_the_kernel_cannot_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atelier2.adapters.runner_child._landlock_abi", lambda: 0)

    with pytest.raises(LandlockUnavailable):
        install_landlock_guard((Path("/tmp"),))


@pytest.mark.proves("runner-cancel-none")
def test_cancel_reap_observes_exit_before_signal() -> None:
    child = start_runner_child((sys.executable, "-c", "pass"))
    child.wait(timeout=2)

    assert (
        reap_cancelled_runner_child(child, 1, 5)
        is RunnerCancellationObservation.EXITED_BEFORE_SIGNAL
    )


def test_cancel_reap_observes_term() -> None:
    child = start_runner_child((sys.executable, "-c", "import time; time.sleep(60)"))
    try:
        assert (
            reap_cancelled_runner_child(child, 1, 5)
            is RunnerCancellationObservation.REAPED_AFTER_TERM
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_cancel_reap_kills_a_child_that_ignores_term(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    child = start_runner_child(
        (
            sys.executable,
            "-c",
            "import signal,sys,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(sys.argv[1]).touch(); time.sleep(60)",
            str(ready),
        )
    )
    deadline = time.monotonic() + 2
    while not ready.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.is_file()
    try:
        assert (
            reap_cancelled_runner_child(child, 1, 5)
            is RunnerCancellationObservation.REAPED_AFTER_KILL
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


@pytest.mark.proves("runner-child-landlock")
def test_started_child_landlock_denies_identity(tmp_path: Path) -> None:
    identity = tmp_path / "identity"
    identity.mkdir()
    key = identity / "client.key"
    key.write_text("not-for-child", encoding="utf-8")
    allowed = tuple(
        path
        for path in (
            Path("/usr"),
            Path("/lib"),
            Path("/lib64"),
            Path("/proc"),
            Path("/dev"),
            Path(sys.prefix),
            Path(sys.base_prefix),
        )
        if path.exists()
    )
    child = start_runner_child(
        (
            sys.executable,
            "-c",
            "import sys\nfrom pathlib import Path\ntry:\n    Path(sys.argv[1]).read_bytes()\nexcept PermissionError:\n    raise SystemExit(0)\nraise SystemExit(23)",
            str(key),
        ),
        allowed,
    )
    assert child.wait(timeout=5) == 0, child.stderr.read() if child.stderr else b""


def _candidate_manifest(**timings: int) -> RunnerManifestV1:
    return replace(
        candidate_runner_manifest(
            source_commit="a" * 40,
            image_digest="sha256:" + "b" * 64,
            required_landlock_abi=1,
            executor_revision="fake-free/v1",
            executor_operational_identity="free-runner-candidate",
            provider_id="fake-free",
            auth_mode="api_key",
            requested_capability="headless",
        ),
        **timings,
    )


def _reap_after_test(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.kill()
        child.wait(timeout=2)


def test_post_start_control_wait_ends_at_the_manifest_attempt_span() -> None:
    manifest = _candidate_manifest(total_attempt_milliseconds=200)
    child = start_runner_child((sys.executable, "-c", "import time; time.sleep(60)"))
    core_side, runner_side = socket.socketpair()
    try:
        with core_side, runner_side:
            fence = _CoreFrameFence(
                runner_side,
                RunnerGenerationBinding(
                    AgentAttemptId("a" * 64),
                    AgentExecutionRequestHash("b" * 64),
                    RunnerGenerationId("A" * 43),
                    RunnerManifestId("c" * 64),
                ),
                RunnerInvocationId("B" * 43),
            )
            waited_from = time.monotonic()

            assert _control_or_child_exit(fence, child, manifest) is None
            # The hardcoded span this replaces waited 60 seconds.
            assert time.monotonic() - waited_from < 30
    finally:
        _reap_after_test(child)


def test_child_reap_grace_comes_from_the_attested_manifest(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    child = start_runner_child(
        (
            sys.executable,
            "-c",
            "import signal,sys,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(sys.argv[1]).touch(); time.sleep(60)",
            str(ready),
        )
    )
    deadline = time.monotonic() + 2
    while not ready.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.is_file()
    try:
        reaped_from = time.monotonic()

        observation = _reap_child(
            child, _candidate_manifest(terminate_grace_milliseconds=100)
        )

        assert observation is RunnerCancellationObservation.REAPED_AFTER_KILL
        # The hardcoded grace this replaces waited a full second before KILL.
        assert time.monotonic() - reaped_from < 0.9
    finally:
        _reap_after_test(child)


def test_landlocked_child_reaps_after_term() -> None:
    allowed = tuple(
        path
        for path in (
            Path("/usr"),
            Path("/lib"),
            Path("/lib64"),
            Path("/proc"),
            Path("/dev"),
            Path(sys.prefix),
            Path(sys.base_prefix),
        )
        if path.exists()
    )
    child = start_runner_child(
        (sys.executable, "-c", "import time; time.sleep(60)"), allowed
    )
    try:
        assert (
            reap_cancelled_runner_child(child, 1, 5)
            is RunnerCancellationObservation.REAPED_AFTER_TERM
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
