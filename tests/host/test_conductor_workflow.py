"""The conductor slice (#658 P3): its loop document contract and its doors arming.

`tests/host/test_local_host.py` owns the general `HostSettings` behaviors;
this module owns only the conductor's own facts -- the `loop{wait, agent}`
conversation document the builder validates, its publishability through the
production publish path, and the atelier-doors executor appearing in a
composition exactly where an operator armed it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atelier2.adapters.claude_subscription import (
    CLAUDE_ATELIER_DOORS_EXECUTOR_KEY,
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    ClaudeSubscriptionSettings,
)
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.starter import DbosWorkflowRevisionPublisher
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.publish_workflow_revision import (
    PublicationCreated,
    PublicationExisting,
    publish_workflow_revision,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.provider_probe_receipts import (
    ProviderProbeReceipt,
    ProviderProbeResult,
    ProviderProbeVectorId,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import (
    InstanceAccepted,
    InstanceRefused,
    SchemaAccepted,
    read_instance_document,
    read_schema_document,
)
from atelier2.contracts.verdicts import VERDICT_ANSWER_SCHEMA
from atelier2.contracts.when import recorded_instant
from atelier2.contracts.workflows_v3 import AgentNodeV3, WaitNodeV3, WorkflowGraphV3
from atelier2.host.conductor_workflow import (
    CONDUCTOR_AGENT_NODE_ID,
    CONDUCTOR_CARRIED_CONTEXT_MAXIMUM_LENGTH,
    CONDUCTOR_DOOR_TOOLS,
    CONDUCTOR_LOOP_ID,
    CONDUCTOR_LOOP_MAXIMUM_ROUNDS,
    CONDUCTOR_MESSAGE_SCHEMA,
    CONDUCTOR_REPORT_SCHEMA,
    CONDUCTOR_ROLE,
    CONDUCTOR_WAIT_NODE_ID,
    CONDUCTOR_WORKFLOW_NAME,
    ConductorDocumentDefect,
    conductor_workflow_document,
    require_conductor_document,
)
from atelier2.host.mcp_tools import McpToolName
from atelier2.host.serving import HostSettings, compose_application
from atelier2.ports.agent_configurations import AgentConfigurationRevisionPage
from atelier2.ports.agent_executions import AgentExecutorCarrier
from tests.scenarios.agents import (
    agent_scratch_root,
    claude_subscription_deployment,
)
from tests.scenarios.api import permissive_projection_limit

INERT_CLAUDE = "raise SystemExit(0)\n"

_ANY_JSON_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b"true")


def _conductor_document() -> bytes:
    revision = _ANY_JSON_SCHEMA.revision_hash.value
    return conductor_workflow_document(revision, revision)


def _claude_deployment(tmp_path: Path) -> ClaudeSubscriptionSettings:
    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    return claude_subscription_deployment(deployment, INERT_CLAUDE)


def _doors_armed_settings(
    tmp_path: Path, claude_atelier_doors: bool = True
) -> HostSettings:
    frontend = tmp_path / "frontend"
    if not frontend.is_dir():
        (frontend / "assets").mkdir(parents=True)
        (frontend / "index.html").write_text("index")
    return HostSettings(
        database_path=tmp_path / "durable.sqlite",
        effect_store_path=tmp_path / "effects.sqlite",
        effect_adapter_revision="loopback-v1",
        effect_destination="local",
        application_version="composition-test",
        source_commit="c" * 40,
        source_tree="tree",
        frontend_dist=frontend,
        # Isolated per test, never the operator's real XDG state directory:
        # a stray real receipt must never make an unrelated test's gate
        # answer depend on what happens to sit on the machine running it.
        provider_probe_receipt_directory=tmp_path / "provider-probes",
        agent_scratch_root=agent_scratch_root(tmp_path),
        claude_subscription=_claude_deployment(tmp_path),
        claude_atelier_doors=claude_atelier_doors,
    )


@pytest.mark.proves("the-conductor-document-carries-its-own-fence")
def test_the_conductor_document_carries_its_own_fence() -> None:
    """A wait-then-agent loop, the granted doors only, and the fence in its orders.

    Beyond the gate's link: the document parses under the production V3
    grammar, its description names the run-starting power in the operator's
    own words (the run view shows it, #654), its one loop enters at the wait
    node and closes at the agent node capped at the named round ceiling, the
    agent's instruction names exactly the three granted doors and neither
    write-shaped one, grounds every offer in the listed catalog, and the
    builder's own validation door accepts it.
    """

    document = _conductor_document()
    parsed = parse_workflow_document(document)

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.name == CONDUCTOR_WORKFLOW_NAME
    assert parsed.description is not None
    assert "starts the real run you ask for" in parsed.description
    assert len(parsed.nodes) == 2
    wait_node = parsed.node(CONDUCTOR_WAIT_NODE_ID)
    node = parsed.node(CONDUCTOR_AGENT_NODE_ID)
    assert isinstance(wait_node, WaitNodeV3)
    assert isinstance(node, AgentNodeV3)
    assert node.role == CONDUCTOR_ROLE
    assert node.depends_on == (CONDUCTOR_WAIT_NODE_ID,)
    assert len(parsed.loops) == 1
    loop = parsed.loops[0]
    assert loop.id == CONDUCTOR_LOOP_ID
    assert loop.body == (CONDUCTOR_WAIT_NODE_ID, CONDUCTOR_AGENT_NODE_ID)
    assert loop.maximum_rounds == CONDUCTOR_LOOP_MAXIMUM_ROUNDS
    assert loop.repeat_while is None
    for granted in CONDUCTOR_DOOR_TOOLS:
        assert granted.value in node.instruction
    for withheld in (McpToolName.ANSWER_WAIT, McpToolName.PUBLISH_ARTIFACT):
        assert withheld.value not in node.instruction
    assert f"Never start the workflow named '{CONDUCTOR_WORKFLOW_NAME}'" in (
        node.instruction
    )
    assert "never promise help with a workflow the catalog does not hold" in (
        node.instruction
    )
    assert require_conductor_document(document) is None


def _accepted(schema_document: bytes) -> SchemaAccepted:
    verdict = read_schema_document(schema_document)
    assert isinstance(verdict, SchemaAccepted)
    return verdict


def test_the_canonical_message_and_report_schemas_are_enforceable() -> None:
    """Both published schema documents pass the production schema profile."""

    _accepted(CONDUCTOR_MESSAGE_SCHEMA)
    _accepted(CONDUCTOR_REPORT_SCHEMA)


def _report(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "answer": "Started run build-1; it is STARTED.",
        "started_run_ids": ["build-1"],
        "carried_context": "the operator asked to build canary; build-1 started",
        "carried_context_truncated": False,
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_the_report_schema_admits_the_instructed_json_and_refuses_prose() -> None:
    """Schema and instruction agree on one JSON report shape.

    The billed probe (#7, 25.08.) proved the drift this pins: a prose report
    under the JSON output schema refused every real conductor run.
    """

    report_schema = _accepted(CONDUCTOR_REPORT_SCHEMA)

    assert isinstance(
        read_instance_document(_report(), report_schema), InstanceAccepted
    )
    assert isinstance(
        read_instance_document(b"I started run build-1.", report_schema),
        InstanceRefused,
    )


def test_the_report_schema_requires_the_honesty_marker_and_bounds_carried_context() -> (
    None
):
    """`carried_context_truncated` cannot be omitted, and its context is bounded.

    A report that silently dropped context would poison every round that
    trusts it; the schema refuses one that keeps quiet about it, and refuses
    a `carried_context` past its own named ceiling.
    """

    report_schema = _accepted(CONDUCTOR_REPORT_SCHEMA)
    missing_marker = json.dumps(
        {
            "answer": "done",
            "started_run_ids": [],
            "carried_context": "",
        }
    ).encode()
    oversized_context = _report(
        carried_context="x" * (CONDUCTOR_CARRIED_CONTEXT_MAXIMUM_LENGTH + 1)
    )

    assert isinstance(
        read_instance_document(missing_marker, report_schema), InstanceRefused
    )
    assert isinstance(
        read_instance_document(oversized_context, report_schema), InstanceRefused
    )
    assert isinstance(
        read_instance_document(
            _report(
                carried_context="x" * CONDUCTOR_CARRIED_CONTEXT_MAXIMUM_LENGTH,
                carried_context_truncated=True,
            ),
            report_schema,
        ),
        InstanceAccepted,
    )


def test_the_message_schema_admits_the_operators_words_and_refuses_empty() -> None:
    """A round's Wait answer is the operator's raw message, nothing structured."""

    message_schema = _accepted(CONDUCTOR_MESSAGE_SCHEMA)

    assert isinstance(
        read_instance_document(
            json.dumps("Start the canary workflow.").encode(), message_schema
        ),
        InstanceAccepted,
    )
    assert isinstance(
        read_instance_document(json.dumps("").encode(), message_schema), InstanceRefused
    )


def test_a_headless_conductor_document_is_refused() -> None:
    """A doors grant is a tool-bearing call; plain headless could never bind it."""

    headless = _conductor_document().replace(
        b"mode: headless_with_tools", b"mode: headless"
    )

    with pytest.raises(ConductorDocumentDefect, match="headless_with_tools"):
        require_conductor_document(headless)


def test_an_instruction_that_lost_the_json_report_shape_is_refused() -> None:
    """An instruction drifting back to prose cannot silently ship again."""

    prose = _conductor_document().replace(b'"started_run_ids"', b"started runs")

    with pytest.raises(ConductorDocumentDefect, match="started_run_ids"):
        require_conductor_document(prose)


def test_a_conductor_document_that_lost_its_fence_is_refused() -> None:
    """An edit that drops the fence sentence cannot silently ship."""

    document = _conductor_document()
    fenceless = document.replace(b"Never start the workflow named", b"Start")

    with pytest.raises(ConductorDocumentDefect, match="recursion fence"):
        require_conductor_document(fenceless)


def test_a_conductor_document_authoring_an_order_naming_itself_is_refused() -> None:
    """The slice-1 fence: no authored order may point the conductor at itself."""

    document = _conductor_document()
    self_starting = document.replace(
        b"      - name: previous_report\n"
        b"        from: {node: conduct, output: report}\n",
        b"      - name: previous_report\n"
        b"        from: {node: conduct, output: report}\n"
        b"      - name: target\n"
        b'        value: "start the conductor workflow"\n',
    )
    assert self_starting != document

    with pytest.raises(ConductorDocumentDefect, match="unbounded billed tree"):
        require_conductor_document(self_starting)


def test_a_document_with_a_third_node_is_not_a_conductor() -> None:
    """A conductor's round is exactly one wait node and one agent node."""

    document = _conductor_document()
    three_nodes = document.replace(
        b"loops:\n",
        b"  - id: extra\n"
        b"    type: agent\n"
        b"    role: extra\n"
        b"    mode: headless\n"
        b"    instruction: Do more.\n"
        b"    outputs:\n"
        b"      - name: more\n"
        b'        schema: {ref: conductor-report, revision: "'
        + _ANY_JSON_SCHEMA.revision_hash.value.encode("ascii")
        + b'"}\n'
        b"loops:\n",
    )
    assert three_nodes != document

    with pytest.raises(ConductorDocumentDefect, match="exactly one wait node"):
        require_conductor_document(three_nodes)


def _document_with_cap(cap: int) -> bytes:
    return _conductor_document().replace(
        f"maximum_rounds: {CONDUCTOR_LOOP_MAXIMUM_ROUNDS}".encode(),
        f"maximum_rounds: {cap}".encode(),
    )


def _document_with_repeat_while() -> bytes:
    """A verdict exit needs its deciding node's output pinned to the verdict
    contract to pass grammar at all -- only that lets the mutated document
    reach the conductor's own semantic refusal instead of a generic grammar
    one."""

    revision = _ANY_JSON_SCHEMA.revision_hash.value
    pinned = conductor_workflow_document(
        revision, VERDICT_ANSWER_SCHEMA.revision_hash.value
    )
    return pinned.replace(
        f"    maximum_rounds: {CONDUCTOR_LOOP_MAXIMUM_ROUNDS}\n".encode(),
        f"    maximum_rounds: {CONDUCTOR_LOOP_MAXIMUM_ROUNDS}\n"
        f"    repeat_while: {{node: {CONDUCTOR_AGENT_NODE_ID}, verdict: revise}}\n".encode(),
    )


def _document_without_a_loop() -> bytes:
    """Dropping the loop also drops the previous-round self-edge it licenses:
    without a shared loop the self-edge input is an unordered read and the
    document is refused at grammar level before this check is ever reached --
    so the self-edge input is dropped along with the loop."""

    document = _conductor_document().replace(
        f"""loops:
  - id: {CONDUCTOR_LOOP_ID}
    body: [{CONDUCTOR_WAIT_NODE_ID}, {CONDUCTOR_AGENT_NODE_ID}]
    maximum_rounds: {CONDUCTOR_LOOP_MAXIMUM_ROUNDS}
""".encode(),
        b"",
    )
    return document.replace(
        f"""      - name: previous_report
        from: {{node: {CONDUCTOR_AGENT_NODE_ID}, output: report}}
""".encode(),
        b"",
    )


def _document_with_reversed_body() -> bytes:
    """Reversing `body` while `conduct` still depends on the wait would be a
    literal graph cycle once the wait is also made to depend on it; swapping
    which node the `depends_on` edge names is what keeps this document a
    valid, still one-line, merely reversed loop."""

    document = _conductor_document().replace(
        f"depends_on: [{CONDUCTOR_WAIT_NODE_ID}]\n    inputs:".encode(),
        b"inputs:",
    )
    document = document.replace(
        f"""  - id: {CONDUCTOR_WAIT_NODE_ID}
    type: wait
    prompt: What would you like the conductor to do?
""".encode(),
        f"""  - id: {CONDUCTOR_WAIT_NODE_ID}
    type: wait
    prompt: What would you like the conductor to do?
    depends_on: [{CONDUCTOR_AGENT_NODE_ID}]
""".encode(),
    )
    return document.replace(
        f"body: [{CONDUCTOR_WAIT_NODE_ID}, {CONDUCTOR_AGENT_NODE_ID}]".encode(),
        f"body: [{CONDUCTOR_AGENT_NODE_ID}, {CONDUCTOR_WAIT_NODE_ID}]".encode(),
    )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        pytest.param(
            lambda: _document_with_cap(CONDUCTOR_LOOP_MAXIMUM_ROUNDS - 1),
            "own named ceiling",
            id="cap-changed",
        ),
        pytest.param(
            _document_with_repeat_while,
            "never on a verdict",
            id="repeat_while-added",
        ),
        pytest.param(
            _document_without_a_loop,
            "exactly one loop",
            id="loop-removed",
        ),
        pytest.param(
            _document_with_reversed_body,
            "enter at the wait node and close at the agent node",
            id="body-reversed",
        ),
    ],
)
def test_a_loop_shape_the_document_does_not_carry_is_refused(
    mutate: Callable[[], bytes], match: str
) -> None:
    """Each of the loop's own structural facts is guarded, not just parsed.

    `require_conductor_document` checks the round count against the named
    ceiling, that the loop never exits on a verdict, that exactly one loop
    repeats the round, and that the loop's own body order is wait-then-agent
    -- four independent refusals a document mutated to lose just one of them
    must still hit by name.
    """

    document = mutate()
    with pytest.raises(ConductorDocumentDefect, match=match):
        require_conductor_document(document)


def test_the_conductor_document_publishes_once_through_the_production_path(
    tmp_path: Path,
) -> None:
    """Publish, then publish again: one revision, the same durable hash."""

    _app, runtime = compose_application(_doors_armed_settings(tmp_path))
    runtime.initialize_storage()
    try:
        catalog = DbosCatalogStore(runtime.engine)
        catalog.publish_revision(_ANY_JSON_SCHEMA)
        publisher = DbosWorkflowRevisionPublisher(runtime.engine)
        document = _conductor_document()

        created = publish_workflow_revision(
            document,
            publisher,
            parse_workflow_document,
            permissive_projection_limit(),
            catalog,
        )
        republished = publish_workflow_revision(
            document,
            publisher,
            parse_workflow_document,
            permissive_projection_limit(),
            catalog,
        )

        assert isinstance(created, PublicationCreated)
        assert isinstance(republished, PublicationExisting)
        assert (
            republished.read.projection.revision.revision_hash
            == created.read.projection.revision.revision_hash
        )
        assert isinstance(created.read.projection.graph, WorkflowGraphV3)
        assert created.read.projection.graph.name == CONDUCTOR_WORKFLOW_NAME
    finally:
        runtime.close()


def test_the_doors_executor_is_served_only_where_it_was_armed(
    tmp_path: Path,
) -> None:
    """Naming a Claude executable grants a tool-free call and nothing more.

    The doors executor lets a node's own process start real billed catalog
    runs, so it is a grant of its own: it appears in the registry only where
    the operator armed it, with the tool capability and the local carrier.
    """

    _app, runtime = compose_application(_doors_armed_settings(tmp_path))
    try:
        registry = runtime.agent_executor_registry
        assert CLAUDE_ATELIER_DOORS_EXECUTOR_KEY in registry.keys
        assert registry.declared_capabilities(
            CLAUDE_ATELIER_DOORS_EXECUTOR_KEY
        ) == frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS})
        assert (
            registry.carrier(CLAUDE_ATELIER_DOORS_EXECUTOR_KEY)
            is AgentExecutorCarrier.LOCAL_PROCESS
        )
    finally:
        runtime.close()


def test_an_unarmed_claude_deployment_offers_no_doors_executor(
    tmp_path: Path,
) -> None:
    _app, runtime = compose_application(
        _doors_armed_settings(tmp_path, claude_atelier_doors=False)
    )
    try:
        assert runtime.agent_executor_registry.keys == frozenset(
            {CLAUDE_SUBSCRIPTION_EXECUTOR_KEY}
        )
    finally:
        runtime.close()


def test_arming_the_doors_without_a_claude_deployment_is_refused(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("index")

    with pytest.raises(ValueError, match="third executor"):
        HostSettings(
            database_path=tmp_path / "durable.sqlite",
            effect_store_path=tmp_path / "effects.sqlite",
            effect_adapter_revision="loopback-v1",
            effect_destination="local",
            application_version="composition-test",
            source_commit="commit",
            source_tree="tree",
            frontend_dist=frontend,
            claude_atelier_doors=True,
        )


def test_the_published_conductor_configuration_is_startable_where_doors_are_armed(
    tmp_path: Path,
) -> None:
    """The binding half of phase B: a config naming the doors revision starts.

    The catalog judges startability against the composed registry, so this is
    the production answer to "can a conductor node be bound": yes where the
    doors executor is armed, and the same configuration would be unstartable in
    a composition without it.
    """

    settings = _doors_armed_settings(tmp_path)
    _app, runtime = compose_application(settings)
    runtime.initialize_storage()
    try:
        catalog = DbosAgentConfigurationCatalog(
            runtime.engine, runtime.agent_executor_registry
        )
        auth = AuthProfileRevision(
            "max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
        )
        catalog.publish_auth_profile_revision(auth)
        configuration = AgentConfigurationRevision(
            "claude-opus-4-6",
            auth.revision_hash,
            CLAUDE_ATELIER_DOORS_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.HEADLESS_WITH_TOOLS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        catalog.publish_agent_configuration_revision(configuration)

        assert settings.provider_probe_receipt_directory is not None
        settings.provider_probe_receipt_directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        receipt = ProviderProbeReceipt(
            ProviderProbeVectorId("atelier-doors-claude-opus-4-6"),
            configuration.revision_hash,
            WorkflowRevisionHash("b" * 64),
            settings.source_commit,
            recorded_instant(now - timedelta(minutes=1)),
            recorded_instant(now + timedelta(hours=1)),
            ProviderProbeResult.SUCCEEDED,
            RunId("provider-canary/atelier-doors-fixture"),
            terminal_hash=Sha256Hash("d" * 64),
        )
        (
            settings.provider_probe_receipt_directory / "claude-opus-4-6.json"
        ).write_bytes(receipt.canonical_bytes())

        page = catalog.list_agent_configuration_revisions(None, 10)

        assert isinstance(page, AgentConfigurationRevisionPage)
        listed = {item.revision.revision_hash: item.startable for item in page.items}
        assert listed[configuration.revision_hash] is True
    finally:
        runtime.close()
