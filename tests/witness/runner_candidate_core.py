"""Disposable #301-A Core process: its SQLite volume is the only product truth.

The transport this process drives its one Runner connection over -- the TLS
listener, the accept/reconnect bound, frame I/O, peer-leaf pinning, and the
drive loop to RELEASED -- is not reimplemented here: it is the production
owner `atelier2.adapters.runner_core_transport`, first called by this exact
witness. Nothing in this module imports `atelier2.runner` -- the scenario this
process declares (`--scenario`) is Witness-only vocabulary, carried as a plain
bootstrap string a real launcher's bootstrap need never write at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
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
    FreeRunnerExecutorFactory,
    FreeRunnerHoldJob,
    FreeRunnerPrintJob,
    encode_free_runner_job,
    free_runner_auth_reference,
    refuse_unbound_runner_a_request,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.runner_cli_pins import runner_executor_cli_pin
from atelier2.adapters.runner_core_transport import (
    RunnerPeerPin,
    accept_and_drive_session,
    bind_session_listener,
)
from atelier2.adapters.runner_tls import (
    CORE_DNS_NAME,
    CORE_SESSION_PORT,
    CorePeerDocument,
    SupportedPublicKey,
    core_uri_for_certificate,
    encode_core_peer_document,
    invocation_from_runner_uri,
    pin_tls_13,
    runner_uri_for_invocation,
    sole_peer_uri,
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
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2

_OUTPUT_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b"true")

# The Witness's own declared scenario vocabulary (`scripts/runner_candidate.sh`
# passes exactly one of these to `--scenario`) -- production carries scenario
# as an optional plain string in `bootstrap.json`, and never imports this set.
_SCENARIO_SUCCESS = "success"
_SCENARIO_CANCEL = "cancel"
_SCENARIO_CRASH_AFTER_PUBLISH = "crash-after-publish"
_SCENARIOS = (_SCENARIO_SUCCESS, _SCENARIO_CANCEL, _SCENARIO_CRASH_AFTER_PUBLISH)


def _document_for(job: FreeRunnerPrintJob | FreeRunnerHoldJob) -> bytes:
    """The one-node workflow whose authored instruction *is* the job document.

    The durable store recomputes `job_bytes` from this exact node's authored
    instruction (`_validate_request`), so the fixed candidate program's job
    document cannot be handed to the request separately -- it has to be what
    the workflow's author wrote. `json.dumps` doubles as a YAML-safe quoted
    scalar here (a JSON string literal is valid YAML flow-scalar syntax),
    which keeps the encoded document's own JSON braces and quotes from being
    read as YAML flow-mapping syntax.
    """
    instruction_literal = json.dumps(encode_free_runner_job(job).decode("utf-8"))
    return (
        b"format_version: 3\n"
        b"name: Disposable free Runner candidate\n"
        b"nodes:\n"
        b"  - id: execute\n"
        b"    type: agent\n"
        b"    role: runner\n"
        b"    mode: headless\n"
        b"    instruction: " + instruction_literal.encode("utf-8") + b"\n"
        b"    outputs:\n"
        b"      - name: result\n"
        b"        schema:\n"
        b"          ref: result-schema\n"
        b"          revision: "
        + _OUTPUT_SCHEMA.revision_hash.value.encode("ascii")
        + b"\n"
    )


# Bounds every accept() so an unreachable or crashed-and-never-restarted
# Runner fails this witness loudly instead of hanging Core -- and the
# launcher's `docker wait` on it -- forever.
_ACCEPT_TIMEOUT_SECONDS = 30.0


def _maximum_runner_connections(scenario: str) -> int:
    """One connection for a normal lifetime; only the declared
    `crash-after-publish` scenario's real process death ever legitimately
    reconnects, restarting this exact Runner container (`#15-B5`). Any other
    scenario reaching a second `accept()` means its one connection dropped
    unexpectedly -- a loud failure from the reconnect bound, not a silent
    extra retry that could hang forever on a Runner that will never come back.
    """
    if scenario == _SCENARIO_CRASH_AFTER_PUBLISH:
        return 2
    return 1


def _bootstrap(root: Path, handoff: Path, scenario: str):
    manifest = decode_runner_manifest((handoff / "manifest").read_bytes())
    job = (
        FreeRunnerHoldJob(manifest.total_attempt_milliseconds / 1000)
        if scenario == _SCENARIO_CANCEL
        else FreeRunnerPrintJob("runner candidate")
    )
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
        workflow = WorkflowRevision(_document_for(job))
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
            encode_free_runner_job(job),
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
            "scenario": scenario,
        }
        _write_json(root / "bootstrap.json", bootstrap)
        reference = free_runner_auth_reference(
            request.resolved_binding.auth_profile
        ).value
        return execution, binding, store, request, manifest, reference
    except BaseException:
        runtime.close()
        raise


def _write_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _write_json(path: Path, document: dict[str, str]) -> None:
    _write_bytes(
        path,
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not path.is_file():
        raise RuntimeError(f"issuer handoff did not create {path}")


def main(arguments: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=_SCENARIOS, default=_SCENARIO_SUCCESS)
    parsed = parser.parse_args(arguments)
    scenario = parsed.scenario
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
    peer_document = CorePeerDocument(
        CORE_DNS_NAME,
        core_uri,
        hashlib.sha256(
            certificate.public_bytes(serialization.Encoding.DER)
        ).hexdigest(),
        CORE_SESSION_PORT,
    )
    _write_bytes(root / "core-peer.json", encode_core_peer_document(peer_document))
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    pin_tls_13(context)
    context.load_cert_chain(identity / "core.crt", identity / "core.key")
    context.load_verify_locations(cafile=identity / "ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    peer_directory = Path("/run/atelier2-peer-authorization")
    peer_leaf = peer_directory / "client.crt"
    inspect_attested = handoff / "inspect-attested"
    _wait_for(peer_leaf)
    expected_leaf_pem = peer_leaf.read_bytes()
    peer_certificate = x509.load_pem_x509_certificate(expected_leaf_pem)
    invocation = invocation_from_runner_uri(sole_peer_uri(peer_certificate), binding)
    expected_runner_uri = runner_uri_for_invocation(binding, invocation)
    ca_pem = identity.joinpath("ca.crt").read_bytes()
    validate_peer_certificate(
        expected_leaf_pem,
        ca_pem,
        expected_dns_name=None,
        expected_uri=expected_runner_uri,
        expected_eku=ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    pin = RunnerPeerPin(
        ca_pem,
        expected_runner_uri,
        ExtendedKeyUsageOID.CLIENT_AUTH,
        peer_certificate.public_bytes(serialization.Encoding.DER),
    )

    def _session_for_first_connection() -> CoreRunnerSession:
        # Attested and constructed once, against the first connection --
        # neither the launcher's inspect attestation nor this invocation's
        # identity changes on a resumed reconnect, and the session this
        # returns is the one long-lived ordering fence every later connection
        # keeps driving.
        _wait_for(inspect_attested)
        if (
            inspect_attested.read_text(encoding="ascii").strip()
            != binding.manifest_id.value
        ):
            raise RuntimeError("runner-attestation-mismatch")
        return CoreRunnerSession(
            binding,
            DbosRunnerSessionCore(execution, store, secrets.token_urlsafe(32)),
            encode_runner_prepare_payload(request, reference),
            manifest,
            reference,
            invocation,
            # A composition root's job, not Core's: the serving deployment
            # binds the conformance set its executor adapters own. This
            # disposable witness reuses the Runner-side registry because it
            # composes both ends.
            runner_executor_cli_pin(manifest),
        )

    on_started = (
        (lambda session: session.cancel()) if scenario == _SCENARIO_CANCEL else None
    )
    with bind_session_listener(CORE_SESSION_PORT, _ACCEPT_TIMEOUT_SECONDS) as server:
        accept_and_drive_session(
            server,
            context,
            pin,
            _session_for_first_connection,
            _maximum_runner_connections(scenario),
            on_started,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
