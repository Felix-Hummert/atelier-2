"""Disposable #301-A Core process: its SQLite volume is the only product truth."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import ssl
import time
from pathlib import Path
from typing import cast

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.run_store import run_from_record_with_bindings
from atelier2.adapters.dbos.runner_session_core import DbosRunnerSessionCore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.free_runner_executor import (
    FreeRunnerAuthorizationResolver,
    FreeRunnerExecutorFactory,
    refuse_unbound_runner_a_request,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.runner_tls import (
    CORE_DNS_NAME,
    SupportedPublicKey,
    core_uri_for_certificate,
    invocation_from_runner_uri,
    pin_tls_13,
    runner_uri_for_invocation,
    validate_peer_certificate,
)
from atelier2.application.run_runner_session import (
    CoreRunnerSession,
    encode_runner_prepare_payload,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.runner_manifests import (
    decode_runner_manifest,
    runner_manifest_id,
)
from atelier2.contracts.runner_session_codec import (
    decode_runner_session_frame,
    encode_runner_session_frame,
    runner_session_body_length,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame, RunnerSessionMessage
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.runner.__main__ import CandidateScenario

_OUTPUT_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b"true")
_DOCUMENT = b"""format_version: 3
name: Disposable free Runner candidate
nodes:
  - id: execute
    type: agent
    role: runner
    mode: headless
    instruction: Return the one candidate result.
    outputs:
      - name: result
        schema:
          ref: result-schema
          revision: %s
""" % _OUTPUT_SCHEMA.revision_hash.value.encode("ascii")


def _read_frame(connection: ssl.SSLSocket) -> RunnerSessionFrame:
    prefix = _read_exact(connection, 4)
    length = runner_session_body_length(prefix)
    return decode_runner_session_frame(prefix + _read_exact(connection, length))


def _read_exact(connection: ssl.SSLSocket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ConnectionError("runner closed its one session")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_frame(connection: ssl.SSLSocket, frame: RunnerSessionFrame) -> None:
    connection.sendall(encode_runner_session_frame(frame))


# One connection for a normal lifetime, one more for a resumed reconnect after
# the declared `CRASH_AFTER_PUBLISH` scenario's real process death restarts
# this exact Runner container (`#15-B5`). A third would mean the resumed
# candidate itself failed to reach RELEASED, which stays a loud failure
# rather than a silent extra retry.
_MAXIMUM_RUNNER_CONNECTIONS = 2


def _drive_until_released_or_dropped(
    connection: ssl.SSLSocket, session: CoreRunnerSession, scenario: CandidateScenario
) -> bool:
    """Drive frames on one connection until RELEASED, or the peer disconnects.

    `session` is the one long-lived, in-memory ordering fence for this
    invocation; it survives a dropped connection unmodified; a resumed
    candidate replays every already-accepted sequence, and `CoreRunnerSession`
    answers each replay from its own idempotent cache (`#15-B3`) instead of
    re-advancing the durable store, so this never processes a duplicate.
    """
    while True:
        try:
            frame = _read_frame(connection)
        except (ConnectionError, ssl.SSLError):
            # A hard `os._exit` never sends a TLS close_notify, so the OS
            # closing that fd out from under the peer can surface here as a
            # plain closed connection or as an abrupt SSL EOF depending on
            # platform and timing -- both mean the same thing: this
            # connection is gone, and a resumed candidate may still reconnect.
            return False
        response = (
            session.accept_terminal_record(frame)
            if frame.message is RunnerSessionMessage.TERMINAL_RECORD
            else session.accept(frame)
        )
        if response is not None:
            _write_frame(connection, response)
        if (
            scenario is CandidateScenario.CANCEL
            and frame.message is RunnerSessionMessage.STARTED
        ):
            _write_frame(connection, session.cancel())
        if frame.message is RunnerSessionMessage.RELEASED:
            return True


def _bootstrap(root: Path, handoff: Path, scenario: CandidateScenario):
    database = root / "core.sqlite3"
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            database, "runner-candidate-core", agent_scratch_root=workspace
        ),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite3",
            AdapterRevision("runner-candidate/v1"),
            EffectDestination("runner-candidate"),
        ),
        ExactOutputAgentExecutorFactory(),
        (),
    )
    runtime.initialize_storage()
    try:
        DbosCatalogStore(runtime.engine).publish_revision(_OUTPUT_SCHEMA)
        runner_registry = AgentExecutorRegistry((FreeRunnerExecutorFactory(),))
        catalog = DbosAgentConfigurationCatalog(runtime.engine, runner_registry)
        auth = AuthProfileRevision(
            "candidate", 1, ProviderId("fake-free"), AuthMode.API_KEY
        )
        catalog.publish_auth_profile_revision(auth)
        configuration = AgentConfigurationRevision(
            "free",
            auth.revision_hash,
            AgentExecutorRevision("fake-free/v1"),
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        catalog.publish_agent_configuration_revision(configuration)
        workflow = WorkflowRevision(_DOCUMENT)
        DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
        run_id = RunId("runner-candidate/one")
        started = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runner_registry
        ).start_published(
            StartPublishedRunRequestV2(
                run_id,
                workflow.revision_hash,
                AgentBindingSet(
                    (AgentBinding(AgentRole("runner"), configuration.revision_hash),)
                ),
            )
        )
        if not isinstance(started, DurableRunCreated):
            raise TypeError(
                f"candidate Core could not start its durable run: {started!r}"
            )
        with runtime.engine.connect() as connection:
            record = (
                connection.execute(runs.select().where(runs.c.run_id == run_id.value))
                .mappings()
                .one()
            )
            run = run_from_record_with_bindings(connection, record)
        if not isinstance(run, RunV3):
            raise TypeError("candidate Core did not resolve its V3 run")
        request = AgentExecutionRequestV2(
            NodeExecutionId.for_node(run_id, workflow.revision_hash, "execute"),
            run_id,
            workflow.revision_hash,
            "execute",
            run.agent_bindings[0],
            AgentExecutorOperationalIdentity("free-runner-candidate"),
            b"Return the one candidate result.",
        )
        refuse_unbound_runner_a_request(request)
        execution = AgentAttemptExecution(
            request,
            AgentAttemptId.for_execution(
                request.node_execution_id, request.request_hash
            ),
            1,
        )
        store = DbosAgentAttemptStore(runtime.engine)
        store.prepare(execution)
        manifest = decode_runner_manifest((handoff / "manifest").read_bytes())
        identity = runner_manifest_id(manifest)
        stated = handoff.joinpath("manifest-id").read_text(encoding="ascii").strip()
        if identity.value != stated:
            raise ValueError("runner-manifest-mismatch")
        binding = RunnerGenerationBinding(
            execution.attempt_id,
            request.request_hash,
            RunnerGenerationId(secrets.token_urlsafe(32)),
            identity,
        )
        store.bind_runner_generation(execution, binding)
        bootstrap = {
            "attempt_id": binding.attempt_id.value,
            "request_hash": binding.request_hash.value,
            "generation_id": binding.generation_id.value,
            "manifest_id": binding.manifest_id.value,
            "scenario": scenario.value,
        }
        _write_json(root / "bootstrap.json", bootstrap)
        reference = FreeRunnerAuthorizationResolver().reference_for(
            request.resolved_binding.auth_profile
        )
        return execution, binding, store, request, manifest, reference
    except BaseException:
        runtime.close()
        raise


def _write_json(path: Path, document: dict[str, str]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(temporary, path)


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not path.is_file():
        raise RuntimeError(f"issuer handoff did not create {path}")


def main(arguments: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=tuple(choice.value for choice in CandidateScenario),
        default=CandidateScenario.SUCCESS.value,
    )
    parsed = parser.parse_args(arguments)
    scenario = CandidateScenario(parsed.scenario)
    root = Path("/var/lib/atelier2-candidate")
    handoff = Path("/handoff")
    identity = Path("/run/atelier2-core-identity")
    execution, binding, store, request, manifest, reference = _bootstrap(
        root, handoff, scenario
    )
    certificate_pem = identity.joinpath("core.crt").read_bytes()
    certificate = x509.load_pem_x509_certificate(certificate_pem)
    core_uri = core_uri_for_certificate(
        cast(SupportedPublicKey, certificate.public_key())
    )
    _write_json(
        root / "core-peer.json",
        {
            "dns_name": CORE_DNS_NAME,
            "uri": core_uri,
            "fingerprint": hashlib.sha256(
                certificate.public_bytes(serialization.Encoding.DER)
            ).hexdigest(),
        },
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    pin_tls_13(context)
    context.load_cert_chain(identity / "core.crt", identity / "core.key")
    context.load_verify_locations(cafile=identity / "ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    peer_directory = Path("/run/atelier2-peer-authorization")
    peer_leaf = peer_directory / "client.crt"
    inspect_attested = handoff / "inspect-attested"
    _wait_for(peer_leaf)
    expected = peer_leaf.read_bytes()
    peer_certificate = x509.load_pem_x509_certificate(expected)
    uris = peer_certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)
    if len(uris) != 1:
        raise RuntimeError("runner-binding-san-mismatch")
    invocation = invocation_from_runner_uri(uris[0], binding)
    expected_runner_uri = runner_uri_for_invocation(binding, invocation)
    ca_pem = identity.joinpath("ca.crt").read_bytes()
    validate_peer_certificate(
        expected,
        ca_pem,
        expected_dns_name=None,
        expected_uri=expected_runner_uri,
        expected_eku=ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    server = socket.create_server(("0.0.0.0", 8443), reuse_port=False)
    released = False
    session: CoreRunnerSession | None = None
    with server:
        for _ in range(_MAXIMUM_RUNNER_CONNECTIONS):
            with context.wrap_socket(
                server.accept()[0], server_side=True
            ) as connection:
                presented = connection.getpeercert(binary_form=True)
                if presented is None:
                    raise RuntimeError(
                        "TLS client did not present an authenticated leaf"
                    )
                presented_pem = x509.load_der_x509_certificate(presented).public_bytes(
                    serialization.Encoding.PEM
                )
                validate_peer_certificate(
                    presented_pem,
                    ca_pem,
                    expected_dns_name=None,
                    expected_uri=expected_runner_uri,
                    expected_eku=ExtendedKeyUsageOID.CLIENT_AUTH,
                )
                if presented != peer_certificate.public_bytes(
                    serialization.Encoding.DER
                ):
                    raise RuntimeError(
                        "Runner peer leaf differs from the issuer handoff"
                    )
                if session is None:
                    # Attested and constructed once, against the first
                    # connection -- neither the launcher's inspect attestation
                    # nor this invocation's identity changes on a resumed
                    # reconnect, and `session` is the one long-lived ordering
                    # fence every later connection keeps driving.
                    _wait_for(inspect_attested)
                    if (
                        inspect_attested.read_text(encoding="ascii").strip()
                        != binding.manifest_id.value
                    ):
                        raise RuntimeError("runner-attestation-mismatch")
                    session = CoreRunnerSession(
                        binding,
                        DbosRunnerSessionCore(
                            execution,
                            store,
                            secrets.token_urlsafe(32),
                        ),
                        encode_runner_prepare_payload(request, reference),
                        manifest,
                        reference,
                        invocation,
                    )
                released = _drive_until_released_or_dropped(
                    connection, session, scenario
                )
            if released:
                break
    if not released:
        raise RuntimeError("runner did not reach RELEASED within the reconnect bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
