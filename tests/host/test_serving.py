"""`HostSettings`'s Runner-lease deployment (`#540` C-3.6, C-4).

`tests/host/test_local_host.py` owns every other `HostSettings`/
`compose_application` behavior; this module owns only the Runner-lease
deployment -- the group of fields, its all-or-nothing refusal, and the fake-
free candidate's `RUNNER_LEASE` registration once the group is declared
(C-3.6), and the `atelier serve` flag group that reaches that composition
from the packaged command line (C-4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from atelier2.adapters.free_runner_executor import FreeRunnerExecutorFactory
from atelier2.host import main
from atelier2.host.serving import HostSettings, compose_application
from atelier2.ports.agent_executions import AgentExecutorCarrier

_ACCEPT_TIMEOUT_SECONDS = 5.0

_RUNNER_LEASE_FLAGS = {
    "runner_lease_root": "--runner-lease-root",
    "runner_image": "--runner-image",
    "runner_image_digest": "--runner-image-digest",
    "runner_console_container": "--runner-console-container",
    "runner_core_identity_directory": "--runner-core-identity-directory",
    "runner_accept_timeout_seconds": "--runner-accept-timeout-seconds",
}


def _self_signed_identity(directory: Path) -> None:
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


def _frontend(tmp_path: Path) -> Path:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("index")
    return frontend


def _settings(tmp_path: Path, **runner_lease: Any) -> HostSettings:
    return HostSettings(
        database_path=tmp_path / "durable.sqlite",
        effect_store_path=tmp_path / "effects.sqlite",
        effect_adapter_revision="loopback-v1",
        effect_destination="local",
        application_version="composition-test",
        source_commit="c" * 40,
        source_tree="tree",
        frontend_dist=_frontend(tmp_path),
        **runner_lease,
    )


def _declared_runner_lease(tmp_path: Path) -> dict[str, Any]:
    identity = tmp_path / "identity"
    _self_signed_identity(identity)
    return {
        "runner_lease_root": tmp_path / "leases",
        "runner_image": "atelier2-runner-candidate:test",
        "runner_image_digest": "sha256:" + "a" * 64,
        "runner_console_container": "serve-test",
        "runner_core_identity_directory": identity,
        "runner_accept_timeout_seconds": _ACCEPT_TIMEOUT_SECONDS,
    }


def test_no_runner_lease_declaration_offers_no_runner_lease_executor(
    tmp_path: Path,
) -> None:
    _app, runtime = compose_application(_settings(tmp_path))
    try:
        assert runtime.agent_executor_registry.keys == frozenset()
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "omit",
    (
        "runner_lease_root",
        "runner_image",
        "runner_image_digest",
        "runner_console_container",
        "runner_core_identity_directory",
        "runner_accept_timeout_seconds",
    ),
)
def test_a_partial_runner_lease_declaration_is_refused_at_start(
    tmp_path: Path, omit: str
) -> None:
    declared = _declared_runner_lease(tmp_path)
    del declared[omit]
    with pytest.raises(ValueError, match="declared together"):
        _settings(tmp_path, **declared)


def test_a_full_runner_lease_declaration_serves_the_fake_free_candidate_as_a_lease(
    tmp_path: Path,
) -> None:
    _app, runtime = compose_application(
        _settings(tmp_path, **_declared_runner_lease(tmp_path))
    )
    try:
        key = FreeRunnerExecutorFactory().key
        assert key in runtime.agent_executor_registry.keys
        assert (
            runtime.agent_executor_registry.carrier(key)
            is AgentExecutorCarrier.RUNNER_LEASE
        )
        assert runtime.agent_workspace_owner is None
    finally:
        runtime.close()


def _serve_command(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "serve",
        "--database",
        str(tmp_path / "durable.sqlite"),
        "--effect-store",
        str(tmp_path / "effects.sqlite"),
        "--effect-adapter-revision",
        "loopback-v1",
        "--effect-destination",
        "local",
        "--application-version",
        "serve-cli-test",
        "--source-commit",
        "c" * 40,
        "--source-tree",
        "tree",
        "--frontend-dist",
        str(_frontend(tmp_path)),
        *extra,
    ]


def _runner_lease_flags(lease: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    for field, flag in _RUNNER_LEASE_FLAGS.items():
        if field in lease:
            flags += [flag, str(lease[field])]
    return flags


def _captured_serve_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, HostSettings]:
    captured: dict[str, HostSettings] = {}

    def fake_serve(settings: HostSettings) -> None:
        captured["settings"] = settings

    monkeypatch.setattr("atelier2.host.serve", fake_serve)
    return captured


def test_the_serve_flags_compose_the_fake_free_runner_lease_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The packaged command reaches the C-3.6 composition it used to skip.

    The whole flag group builds the same `HostSettings` a direct construction
    would, so the composition registers the fake-free candidate as this
    deployment's one `RUNNER_LEASE` offer -- the wiring `atelier serve` was
    missing until C-4.
    """

    captured = _captured_serve_settings(monkeypatch)
    lease = _declared_runner_lease(tmp_path)

    assert main(_serve_command(tmp_path, *_runner_lease_flags(lease))) == 0

    _app, runtime = compose_application(captured["settings"])
    try:
        key = FreeRunnerExecutorFactory().key
        assert key in runtime.agent_executor_registry.keys
        assert (
            runtime.agent_executor_registry.carrier(key)
            is AgentExecutorCarrier.RUNNER_LEASE
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("omit", tuple(_RUNNER_LEASE_FLAGS))
def test_a_partial_runner_lease_flag_group_is_refused_at_the_command_line(
    tmp_path: Path, omit: str
) -> None:
    """Any missing member fails loud at the parser, never a half-served lease.

    `DbosRuntimeSettings` owns the all-or-nothing rule; `_serve` surfaces its
    refusal as a command-line error rather than a traceback or a silent start.
    """

    lease = _declared_runner_lease(tmp_path)
    del lease[omit]

    with pytest.raises(SystemExit) as refusal:
        main(_serve_command(tmp_path, *_runner_lease_flags(lease)))

    assert refusal.value.code == 2


@pytest.mark.parametrize(
    ("flag", "malformed"),
    (
        ("--runner-image-digest", "not-a-digest"),
        ("--source-commit", "dev"),
    ),
)
def test_a_malformed_runner_lease_format_is_refused_at_the_command_line(
    tmp_path: Path, flag: str, malformed: str
) -> None:
    """A nonempty-but-malformed digest or source commit fails at the parser.

    Both values pass the nonempty guard, so only the runner manifest's format
    contract rejects them. `DbosRuntimeSettings` enforces that format at the
    serve boundary and `_serve` surfaces the refusal as exit 2, rather than
    starting green and failing deep in the first lease attempt. The well-formed
    group still composes -- `test_the_serve_flags_compose_the_fake_free_runner_
    lease_executor` above is that positive guard.
    """

    lease = _declared_runner_lease(tmp_path)
    command = _serve_command(tmp_path, *_runner_lease_flags(lease), flag, malformed)

    with pytest.raises(SystemExit) as refusal:
        main(command)

    assert refusal.value.code == 2


def test_serve_without_runner_lease_flags_keeps_todays_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _captured_serve_settings(monkeypatch)

    assert main(_serve_command(tmp_path)) == 0

    assert captured["settings"].runner_lease_root is None
