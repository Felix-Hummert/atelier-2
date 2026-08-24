"""`DbosRuntime`'s carrier-dependent composition (`#540` C-3.6).

Only a `LOCAL_PROCESS`-carried executor key needs Serve's own process
supervisor, cgroup root and scratch root; a `RUNNER_LEASE`-carried key needs
none of them, and only the declared Runner-lease deployment. This module
proves that split at the `_open_binding` seam `tests/integration/
test_dbos_runtime_lifecycle.py` already exercises for the `LOCAL_PROCESS`
side -- this file owns the `RUNNER_LEASE` half rather than growing that one
further.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import atelier2.adapters.dbos.runtime as dbos_runtime
from atelier2.adapters.dbos.runtime import (
    AgentProcessSupervisorUnavailable,
    DbosRuntime,
    DbosRuntimeBindingConflict,
    DbosRuntimeSettings,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.free_runner_executor import FreeRunnerExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.ports.agent_executions import (
    AgentExecutorCarrier,
    AgentExecutorRegistration,
)
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2, agent_scratch_root

_ACCEPT_TIMEOUT_SECONDS = 5.0


def _self_signed_identity(directory: Path) -> None:
    """A syntactically real key/certificate pair, good enough to load into an
    `ssl.SSLContext` -- these tests never complete a handshake, so a real CA
    chain buys nothing a self-signed pair does not already prove."""

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-core")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "core.key").write_bytes(key_pem)
    (directory / "core.crt").write_bytes(certificate_pem)
    (directory / "ca.crt").write_bytes(certificate_pem)


def _runner_lease_settings(
    root: Path, agent_scratch_root: Path | None = None
) -> DbosRuntimeSettings:
    identity = root / "identity"
    _self_signed_identity(identity)
    return DbosRuntimeSettings(
        root / "atelier.sqlite",
        "runner-lease-test",
        agent_scratch_root=agent_scratch_root,
        runner_lease_root=root / "leases",
        runner_image="atelier2-runner-candidate:test",
        runner_image_digest="sha256:" + "a" * 64,
        runner_console_container="serve-test",
        runner_core_identity_directory=identity,
        runner_accept_timeout_seconds=_ACCEPT_TIMEOUT_SECONDS,
        runner_lease_source_commit="b" * 40,
    )


def _effect_factory(root: Path) -> LoopbackEffectAdapterFactory:
    return LoopbackEffectAdapterFactory(
        root / "effects.sqlite",
        AdapterRevision("loopback-v1"),
        EffectDestination("runner-lease-test"),
    )


def _forbidden(*_args: object, **_kwargs: object) -> Never:
    raise AssertionError("a runner-lease-only binding resolved local process authority")


def test_a_pure_runner_lease_registry_opens_without_process_supervision_or_scratch_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dbos_runtime, "delegated_cgroup_root", _forbidden)
    monkeypatch.setattr(dbos_runtime, "AgentProcessSupervisor", _forbidden)
    monkeypatch.setattr(dbos_runtime, "LocalAgentAttemptWorkspaceOwner", _forbidden)
    runtime = DbosRuntime(
        _runner_lease_settings(tmp_path),
        _effect_factory(tmp_path),
        ExactOutputAgentExecutorFactory(),
        (
            AgentExecutorRegistration.startable(
                FreeRunnerExecutorFactory(), AgentExecutorCarrier.RUNNER_LEASE
            ),
        ),
    )
    try:
        assert runtime.agent_workspace_owner is None
        with pytest.raises(
            AgentProcessSupervisorUnavailable,
            match="no LOCAL_PROCESS-carried executor key",
        ):
            _ = runtime.agent_process_supervisor
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "declared",
    (
        ("runner_lease_root",),
        ("runner_image", "runner_image_digest"),
        (
            "runner_console_container",
            "runner_core_identity_directory",
            "runner_accept_timeout_seconds",
            "runner_lease_source_commit",
        ),
    ),
    ids=("one-field", "two-fields", "five-fields"),
)
def test_a_partial_runner_lease_deployment_is_refused_by_name(
    tmp_path: Path, declared: tuple[str, ...]
) -> None:
    complete = _runner_lease_settings(tmp_path)
    partial = {name: getattr(complete, name) for name in declared}
    with pytest.raises(ValueError, match="declared together"):
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "runner-lease-test", **partial)


def test_serving_a_runner_lease_key_without_the_deployment_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(DbosRuntimeBindingConflict, match="runner-lease deployment"):
        DbosRuntime(
            DbosRuntimeSettings(tmp_path / "atelier.sqlite", "runner-lease-test"),
            _effect_factory(tmp_path),
            ExactOutputAgentExecutorFactory(),
            (
                AgentExecutorRegistration.startable(
                    FreeRunnerExecutorFactory(), AgentExecutorCarrier.RUNNER_LEASE
                ),
            ),
        )


def test_a_mixed_registry_still_requires_a_scratch_root_for_its_local_process_key(
    tmp_path: Path,
) -> None:
    with pytest.raises(DbosRuntimeBindingConflict, match="agent scratch root"):
        DbosRuntime(
            _runner_lease_settings(tmp_path),
            _effect_factory(tmp_path),
            ExactOutputAgentExecutorFactory(),
            (
                AgentExecutorRegistration.startable(
                    FreeRunnerExecutorFactory(), AgentExecutorCarrier.RUNNER_LEASE
                ),
                RecordingAgentExecutorFactoryV2(
                    "recording", "recording/v1", "recording-operation", b""
                ),
            ),
        )


def test_a_mixed_registry_opens_both_carriers_together(tmp_path: Path) -> None:
    runtime = DbosRuntime(
        _runner_lease_settings(
            tmp_path, agent_scratch_root=agent_scratch_root(tmp_path)
        ),
        _effect_factory(tmp_path),
        ExactOutputAgentExecutorFactory(),
        (
            AgentExecutorRegistration.startable(
                FreeRunnerExecutorFactory(), AgentExecutorCarrier.RUNNER_LEASE
            ),
            RecordingAgentExecutorFactoryV2(
                "recording", "recording/v1", "recording-operation", b""
            ),
        ),
    )
    try:
        assert runtime.agent_workspace_owner is not None
        assert runtime.agent_process_supervisor is not None
    finally:
        runtime.close()
