from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from atelier2.adapters.systemd_agent_collector import (
    collect_direct_systemd_agent_process,
    encode_direct_systemd_launch_envelope,
)
from atelier2.adapters.systemd_generation_records import (
    DirectSystemdGenerationRecords,
    DirectSystemdIntent,
    DirectSystemdInvocationId,
    DirectSystemdRecoveryState,
)
from atelier2.contracts.agent_attempts import AgentAttemptId, WatchdogGenerationId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.ports.agent_executions import AgentProcessInvocation
from tests.crash.systemd_collector_harness import CRASHED

HARNESS = Path(__file__).with_name("systemd_collector_harness.py")
INVOCATION_ID = DirectSystemdInvocationId("0123456789abcdef0123456789abcdef")


def test_process_death_between_started_file_and_directory_fsync_never_runs_provider(
    tmp_path: Path,
) -> None:
    provider_marker = tmp_path / "provider-ran"
    invocation = AgentProcessInvocation(
        (
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
            str(provider_marker),
        ),
        Path.cwd(),
        standard_output_frame_bytes=17,
    )
    records = DirectSystemdGenerationRecords(tmp_path)
    launch_envelope_path = tmp_path / "launch-envelope"
    envelope = encode_direct_systemd_launch_envelope(invocation)
    records.publish_intent(
        DirectSystemdIntent(
            AgentAttemptId.of(b"crash-attempt"),
            WatchdogGenerationId("crash-generation"),
            "atelier2-crash-attempt.service",
            Sha256Hash.of(envelope),
            invocation.standard_output_frame_bytes,
        )
    )
    launch_envelope_path.write_bytes(envelope)
    os.chmod(launch_envelope_path, 0o600)

    crashed = subprocess.run(
        [
            sys.executable,
            str(HARNESS),
            str(tmp_path),
            str(launch_envelope_path),
            INVOCATION_ID.value,
        ],
        check=False,
        env={"PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        timeout=5,
    )

    assert crashed.returncode == CRASHED
    assert records.started_path.exists()
    assert records.inspect().state is DirectSystemdRecoveryState.POSSIBLY_RAN
    assert not provider_marker.exists()

    with pytest.raises(FileExistsError):
        collect_direct_systemd_agent_process(
            records,
            INVOCATION_ID,
            launch_envelope_path=launch_envelope_path,
        )
    assert not provider_marker.exists()
