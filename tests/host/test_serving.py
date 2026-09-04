"""The `atelier serve` deployments beyond one provider's basics.

`tests/host/test_local_host.py` owns every other `HostSettings`/
`compose_application` behavior; this module owns two deployments' arming. The
Runner-lease deployment (`#540` C-3.6, C-4): the group of fields, its
all-or-nothing refusal, the fake-free candidate's `RUNNER_LEASE` registration
once the group is declared, and the serve flag group that reaches that
composition. And the Claude atelier-doors arming (`#7`): the serve flag that
arms and always attests the doors executor, its refusal without the Claude
deployment, and its absence leaving the doors unserved --
`tests/host/test_conductor_workflow.py` owns the composition those settings
reach. It also owns `_discover_grok_models`/`_discover_codex_models`'
credential isolation (`#1009`): no test elsewhere exercises those two
host-level probes, so their own private-directory discipline is proven here
rather than left unproven."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from atelier2.adapters.claude_subscription import (
    CLAUDE_ATELIER_DOORS_EXECUTOR_KEY,
    ClaudeSubscriptionSettings,
)
from atelier2.adapters.codex_subscription import (
    CodexSandboxMode,
    CodexSubscriptionSettings,
)
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.run_store import load_run_orders
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.free_runner_executor import FreeRunnerExecutorFactory
from atelier2.adapters.grok_subscription import GrokSubscriptionSettings
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.queue_projection import (
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueAdmissionRationale,
    QueueAutomationDisposition,
    QueueItemAdmitted,
    QueueItemProposed,
    QueueItemTrackerObservation,
    QueuePriorityRank,
    QueueProjectionRevision,
    QueueProjectPolicyRevision,
    QueueProposal,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunState, WorkflowRevision
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_DOCUMENT,
    WORK_ITEM_ORDER_SCHEMA_REVISION,
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
    read_work_item_order_document,
)
from atelier2.host import main
from atelier2.host.serving import (
    HostSettings,
    _discover_codex_models,
    _discover_grok_models,
    compose_application,
)
from atelier2.ports.agent_executions import AgentExecutorCarrier
from atelier2.ports.issue_observation import WorkItemRevisionObserved
from atelier2.ports.published_revisions import CatalogLineageFounded
from atelier2.ports.queue_projection import QueueItemsPage, QueueItemsReconciled
from tests.integration.test_claude_atelier_doors import doors_deployment, doors_flags
from tests.integration.test_claude_subscription import (
    INTROSPECTING_CLAUDE,
    parsing_claude,
)
from tests.scenarios.agents import agent_scratch_root, claude_subscription_deployment
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.runs import publish_revision
from tests.scenarios.workflows import graph_input_wait_line

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
        # Isolated per test, never the operator's real XDG state directory:
        # a stray real receipt must never make an unrelated test's gate
        # answer depend on what happens to sit on the machine running it.
        provider_probe_receipt_directory=tmp_path / "provider-probes",
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


def _doors_capable_claude(tmp_path: Path) -> ClaudeSubscriptionSettings:
    """A fake Claude whose parser reads the whole doors vector.

    The reference deployment exists only to ask the production composition
    which flags that vector carries, so the fake refuses exactly the flags a
    real release would not know -- the shape `attest_atelier_doors_invocation`
    insists on.
    """

    reference = doors_deployment(tmp_path, "doors-reference", INTROSPECTING_CLAUDE)
    directory = tmp_path / "claude-deployment"
    directory.mkdir()
    return claude_subscription_deployment(
        directory, parsing_claude(doors_flags(reference))
    )


def _claude_serve_flags(
    tmp_path: Path, claude: ClaudeSubscriptionSettings, *extra: str
) -> list[str]:
    return [
        "--agent-scratch-root",
        str(agent_scratch_root(tmp_path)),
        "--claude-executable",
        str(claude.executable),
        "--claude-credential-directory",
        str(claude.credential_directory),
        *extra,
    ]


def test_the_serve_flag_arms_and_attests_the_claude_atelier_doors_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One named flag beside the Claude deployment serves the doors executor.

    Arming always attests: the command launches the exact door-bearing vector
    once, so the composed settings carry no start refusal and the composition
    registers the doors executor beside its siblings.
    """

    claude = _doors_capable_claude(tmp_path)
    monkeypatch.setenv("PATH", claude.search_path)
    captured = _captured_serve_settings(monkeypatch)

    command = _serve_command(
        tmp_path, *_claude_serve_flags(tmp_path, claude, "--claude-atelier-doors")
    )
    assert main(command) == 0

    served = captured["settings"]
    assert served.claude_atelier_doors
    assert served.claude_atelier_doors_start_refusal is None
    _app, runtime = compose_application(served)
    try:
        assert CLAUDE_ATELIER_DOORS_EXECUTOR_KEY in runtime.agent_executor_registry.keys
    finally:
        runtime.close()


def test_an_unattestable_doors_vector_names_its_refusal_and_still_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An executable that cannot start the doors vector does not kill serve."""

    directory = tmp_path / "claude-deployment"
    directory.mkdir()
    claude = claude_subscription_deployment(directory, parsing_claude(()))
    monkeypatch.setenv("PATH", claude.search_path)
    captured = _captured_serve_settings(monkeypatch)

    command = _serve_command(
        tmp_path, *_claude_serve_flags(tmp_path, claude, "--claude-atelier-doors")
    )
    assert main(command) == 0

    served = captured["settings"]
    assert served.claude_atelier_doors
    assert served.claude_atelier_doors_start_refusal is not None
    assert "atelier-doors" in served.claude_atelier_doors_start_refusal


def test_arming_the_doors_without_a_claude_deployment_is_refused_at_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refusal:
        main(_serve_command(tmp_path, "--claude-atelier-doors"))

    assert refusal.value.code == 2
    assert "--claude-atelier-doors" in capsys.readouterr().err


def test_serve_without_the_doors_flag_leaves_the_doors_unarmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _doors_capable_claude(tmp_path)
    monkeypatch.setenv("PATH", claude.search_path)
    captured = _captured_serve_settings(monkeypatch)

    assert main(_serve_command(tmp_path, *_claude_serve_flags(tmp_path, claude))) == 0

    served = captured["settings"]
    assert not served.claude_atelier_doors
    assert served.claude_atelier_doors_start_refusal is None


def _write_executable(path: Path, source: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# Stands in for grok's `models` subcommand: rather than listing real models,
# it reports the private home this probe was launched with and the auth.json
# bytes it can read there -- exactly what `_discover_grok_models`'s own
# isolation depends on, without a billed CLI.
DISCOVERING_GROK = """
import os, sys
from pathlib import Path

home = os.environ.get("GROK_HOME", "")
auth = (Path(home) / "auth.json").read_text() if home else ""
print("Available models:")
print(f"* HOME={home}")
print(f"* AUTH={auth}")
"""


def _grok_discovery_deployment(
    tmp_path: Path, source: str = DISCOVERING_GROK
) -> GrokSubscriptionSettings:
    executable = _write_executable(tmp_path / "grok", source)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = tmp_path / "grok-home"
    credentials.mkdir()
    authentication = credentials / "auth.json"
    authentication.write_bytes(b"{}")
    authentication.chmod(0o600)
    return GrokSubscriptionSettings(
        executable, workspace, credentials, os.environ.get("PATH", "/usr/bin")
    )


def test_grok_model_discovery_never_runs_inside_the_operators_credential_directory(
    tmp_path: Path,
) -> None:
    """The live deployment served `--grok-credential-directory` here directly.

    `_discover_grok_models` used to name that operator directory as `HOME` and
    `GROK_HOME` for a spawned process; this proves it now spawns into a
    private, disposable copy instead, that the copy carries the material the
    probe needs, and that the copy is already gone once the probe returns.
    """

    settings = _grok_discovery_deployment(tmp_path)

    models = _discover_grok_models(settings, timeout_seconds=5.0)

    reported = dict(entry.split("=", 1) for entry in models)
    assert reported["AUTH"] == "{}"
    assert reported["HOME"] != str(settings.credential_directory)
    assert not Path(reported["HOME"]).exists()


# A `models` subcommand that refuses outright, after recording the private
# home it was launched with beside itself -- the one channel left once
# `_discover_grok_models` never returns a value to read the home back from.
DISCOVERING_GROK_FAILING = """
import os, sys
from pathlib import Path

home = os.environ.get("GROK_HOME", "")
(Path(sys.argv[0]).resolve().parent / "observed-home.txt").write_text(home)
sys.stderr.write("synthetic Grok refusal\\n")
raise SystemExit(1)
"""


def test_grok_model_discovery_removes_its_private_home_after_a_failing_run(
    tmp_path: Path,
) -> None:
    settings = _grok_discovery_deployment(tmp_path, DISCOVERING_GROK_FAILING)

    with pytest.raises(ValueError, match="Grok model discovery failed"):
        _discover_grok_models(settings, timeout_seconds=5.0)

    reported_home = (tmp_path / "observed-home.txt").read_text()
    assert reported_home != str(settings.credential_directory)
    assert not Path(reported_home).exists()


# Stands in for `codex app-server`'s JSON-RPC handshake: rather than listing
# real models, it reports the private home this probe was launched with and
# the auth.json bytes it can read there -- exactly what
# `_discover_codex_models`'s own isolation depends on, without a billed CLI.
DISCOVERING_CODEX = """
import json, sys, os
from pathlib import Path


def send(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


home = os.environ.get("CODEX_HOME", "")
auth = (Path(home) / "auth.json").read_text() if home else ""

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if message.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
    elif message.get("method") == "model/list":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "data": [
                        {"model": f"HOME={home}"},
                        {"model": f"AUTH={auth}"},
                    ]
                },
            }
        )
"""


def _codex_discovery_deployment(
    tmp_path: Path, source: str = DISCOVERING_CODEX
) -> CodexSubscriptionSettings:
    executable = _write_executable(tmp_path / "codex", source)
    credentials = tmp_path / "codex-home"
    credentials.mkdir()
    authentication = credentials / "auth.json"
    authentication.write_bytes(b"{}")
    authentication.chmod(0o600)
    return CodexSubscriptionSettings(
        executable,
        credentials,
        os.environ.get("PATH", "/usr/bin"),
        CodexSandboxMode.READ_ONLY,
    )


def test_codex_model_discovery_never_runs_inside_the_operators_credential_directory(
    tmp_path: Path,
) -> None:
    """`_discover_codex_models` used to name the operator's own `CODEX_HOME`.

    Unreachable from the live serve command line today, but the same proof as
    Grok's above: the spawned `app-server` process gets a private, disposable
    copy instead, that copy carries the material the probe needs, and it is
    already gone once the probe returns.
    """

    settings = _codex_discovery_deployment(tmp_path)

    models = _discover_codex_models(
        settings, timeout_seconds=5.0, termination_grace_seconds=5.0
    )

    reported = dict(entry.split("=", 1) for entry in models)
    assert reported["AUTH"] == "{}"
    assert reported["HOME"] != str(settings.credential_directory)
    assert not Path(reported["HOME"]).exists()


# An `app-server` that answers the `initialize` handshake with garbage
# JSON-RPC (no `result` field), after recording the private home it was
# launched with beside itself -- the one channel left once
# `_discover_codex_models` never returns a value to read the home back from.
DISCOVERING_CODEX_FAILING = """
import json, sys, os
from pathlib import Path

home = os.environ.get("CODEX_HOME", "")
(Path(sys.argv[0]).resolve().parent / "observed-home.txt").write_text(home)

message = json.loads(sys.stdin.readline())
sys.stdout.write(
    json.dumps({"jsonrpc": "2.0", "id": message["id"], "malformed": True}) + "\\n"
)
sys.stdout.flush()
"""


def test_codex_model_discovery_removes_its_private_home_after_a_failing_run(
    tmp_path: Path,
) -> None:
    settings = _codex_discovery_deployment(tmp_path, DISCOVERING_CODEX_FAILING)

    with pytest.raises(TypeError, match="Codex model discovery returned no result"):
        _discover_codex_models(
            settings, timeout_seconds=5.0, termination_grace_seconds=5.0
        )

    reported_home = (tmp_path / "observed-home.txt").read_text()
    assert reported_home != str(settings.credential_directory)
    assert not Path(reported_home).exists()


_QUEUE_STARTED_PROJECT = ProjectId("studio")


def test_a_queue_started_run_carries_the_admitted_items_tracker_reference(
    tmp_path: Path,
) -> None:
    """A2 (`#1145`): a run the queue sweep starts is pinned to the item it is about.

    Through `DbosRuntime` composed exactly as `compose_application` composes
    it -- `tracker_item_source` injected the same way `_effect_adapters` is --
    because a genuine `compose_application` connected to a live GitHub project
    would reach the real network for the sweep's own tracker read, and this
    slice's subject is the queue's order-filling decision, not the GitHub
    adapter's HTTP contract (`tests/integration/test_github_observation.py`
    owns that). The bound document declares one `graph_input` pinned to the
    work-item schema, the same shape `workflows/issue-to-pr.yaml` declares,
    without that file's agent role -- proving `advance_queue`'s own decision
    needs no agent-configuration or model-registry setup beside it.
    """

    tracker_reference = TrackerItemReference("gh:9001")
    revision = ObservedWorkItemRevision(
        tracker_reference,
        WorkItemKind.ISSUE,
        b"push the candidate before the pull request opens",
        WorkItemChangeMarker('W/"9001"'),
        RecordedAt("2026-09-04T09:00:00Z"),
    )
    tracker = FakeTrackerItemSource(snapshot_answer=WorkItemRevisionObserved(revision))
    project_root = tmp_path / "operator-project"
    project_root.mkdir()
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "durable.sqlite",
            "queue-carries-item-test",
            project_id=_QUEUE_STARTED_PROJECT,
            bootstrap_project_root=project_root,
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        tracker_item_source=tracker,
    )
    try:
        engine = runtime.engine
        catalog = DbosCatalogStore(engine)
        work_item_schema = PublishedRevision(
            RevisionKind.SCHEMA, WORK_ITEM_ORDER_SCHEMA_DOCUMENT
        )
        approval_schema = PublishedRevision(RevisionKind.SCHEMA, b"true")
        document = graph_input_wait_line(WORK_ITEM_ORDER_SCHEMA_REVISION.value)
        published = PublishedRevision(RevisionKind.WORKFLOW, document)
        for revision_to_publish in (work_item_schema, approval_schema, published):
            catalog.publish_revision(revision_to_publish)
        # The catalog registry (lineage, name resolution) and the durable
        # workflow-revision store the starter reads by hash are two owners of
        # the same bytes (ADR 0007); a start needs both published.
        publish_revision(engine, WorkflowRevision(document))
        founded = catalog.found_lineage(
            published,
            CatalogLineageDisplayName("queue-carries-item"),
            CatalogActor("operator"),
            CatalogActivatedAt("2026-09-04T09:00:00Z"),
        )
        assert isinstance(founded, CatalogLineageFounded)

        queue = DbosQueueProjectionStore(engine)
        item_reference = WorkItemReference(_QUEUE_STARTED_PROJECT, tracker_reference)
        observed_at = RecordedAt("2026-09-04T09:00:00Z")
        queue.put_policy(
            QueueProjectPolicyRevision(_QUEUE_STARTED_PROJECT, 1, 1, None), 0
        )
        reconciled = queue.reconcile_open_items(
            _QUEUE_STARTED_PROJECT,
            (
                (
                    item_reference,
                    QueueItemTrackerObservation("push before the pr", observed_at),
                ),
            ),
            observed_at,
        )
        assert isinstance(reconciled, QueueItemsReconciled)
        proposed = queue.plan(
            PlanQueueItem(
                item_reference,
                QueueProposal(
                    QueuePriorityRank(1),
                    founded.lineage.lineage_id,
                    (),
                    QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
                    1,
                ),
                QueueProjectionRevision(0),
            )
        )
        assert isinstance(proposed, QueueItemProposed)
        admitted = queue.confirm(
            ConfirmQueueProposal(
                item_reference,
                proposed.revision,
                QueueAdmissionRationale("operator approved the inspected proposal"),
            )
        )
        assert isinstance(admitted, QueueItemAdmitted)

        runtime.launch()

        page = queue.list_items(None, MAXIMUM_PAGE_ITEMS)
        assert isinstance(page, QueueItemsPage)
        (snapshot,) = [
            item for item in page.items if item.item_reference == item_reference
        ]
        assert snapshot.launch_binding is not None
        run_id = snapshot.launch_binding.run_id

        with engine.connect() as connection:
            orders_by_run = load_run_orders(connection, [run_id.value])
        (order,) = orders_by_run[run_id.value]
        assert order.schema_revision == WORK_ITEM_ORDER_SCHEMA_REVISION
        decoded = read_work_item_order_document(order.value)
        assert decoded is not None
        assert decoded.reference == tracker_reference

        with engine.connect() as connection:
            run_record = connection.execute(
                sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
            ).scalar_one()
        assert run_record == RunState.STARTED.value
    finally:
        runtime.close()
