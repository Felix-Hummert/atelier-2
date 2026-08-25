"""The conductor slice (#7): its document contract and its doors arming.

`tests/host/test_local_host.py` owns the general `HostSettings` behaviors;
this module owns only the conductor's own facts -- the workflow document the
builder validates, its publishability through the production publish path, and
the atelier-doors executor appearing in a composition exactly where an operator
armed it.
"""

from __future__ import annotations

import json
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
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.schemas_v3 import (
    InstanceAccepted,
    InstanceRefused,
    SchemaAccepted,
    read_instance_document,
    read_schema_document,
)
from atelier2.contracts.workflows_v3 import AgentNodeV3, WorkflowGraphV3
from atelier2.host.conductor_workflow import (
    CONDUCTOR_BRIEF_SCHEMA,
    CONDUCTOR_DOOR_TOOLS,
    CONDUCTOR_REPORT_SCHEMA,
    CONDUCTOR_ROLE,
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
        source_commit="commit",
        source_tree="tree",
        frontend_dist=frontend,
        agent_scratch_root=agent_scratch_root(tmp_path),
        claude_subscription=_claude_deployment(tmp_path),
        claude_atelier_doors=claude_atelier_doors,
    )


@pytest.mark.proves("the-conductor-document-carries-its-own-fence")
def test_the_conductor_document_carries_its_own_fence() -> None:
    """One agent node, the granted doors only, and the fence in its own orders.

    Beyond the gate's link: the document parses under the production V3
    grammar, its description names the run-starting power in the operator's
    own words (the run view shows it, #654), its instruction names exactly the
    three granted doors and neither write-shaped one, grounds every offer in
    the listed catalog, and the builder's own validation door accepts it.
    """

    document = _conductor_document()
    parsed = parse_workflow_document(document)

    assert isinstance(parsed, WorkflowGraphV3)
    assert parsed.name == CONDUCTOR_WORKFLOW_NAME
    assert parsed.description is not None
    assert "starts the real run you ask for" in parsed.description
    assert len(parsed.nodes) == 1
    node = parsed.nodes[0]
    assert isinstance(node, AgentNodeV3)
    assert node.role == CONDUCTOR_ROLE
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


def test_the_canonical_brief_and_report_schemas_are_enforceable() -> None:
    """Both published schema documents pass the production schema profile."""

    _accepted(CONDUCTOR_BRIEF_SCHEMA)
    _accepted(CONDUCTOR_REPORT_SCHEMA)


def test_the_report_schema_admits_the_instructed_json_and_refuses_prose() -> None:
    """Schema and instruction agree on one JSON report shape.

    The billed probe (#7, 25.08.) proved the drift this pins: a prose report
    under the JSON output schema refused every real conductor run.
    """

    report_schema = _accepted(CONDUCTOR_REPORT_SCHEMA)
    report = json.dumps(
        {
            "answer": "Started run build-1; it is STARTED.",
            "started_run_ids": ["build-1"],
        }
    ).encode()

    assert isinstance(read_instance_document(report, report_schema), InstanceAccepted)
    assert isinstance(
        read_instance_document(b"I started run build-1.", report_schema),
        InstanceRefused,
    )


def test_the_brief_schema_admits_a_bounded_transcript_and_names_truncation() -> None:
    """A workbench brief carries message, prior transcript and the drop count."""

    brief_schema = _accepted(CONDUCTOR_BRIEF_SCHEMA)
    brief = json.dumps(
        {
            "message": "Start the canary workflow.",
            "prior_transcript": [
                {"speaker": "operator", "text": "hello"},
                {"speaker": "conductor", "text": "hello back"},
            ],
            "dropped_oldest_messages": 2,
        }
    ).encode()
    without_drop_count = json.dumps({"message": "hi", "prior_transcript": []}).encode()

    assert isinstance(read_instance_document(brief, brief_schema), InstanceAccepted)
    assert isinstance(
        read_instance_document(without_drop_count, brief_schema), InstanceRefused
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
        b"      - name: brief\n        from: {graph_input: brief}\n",
        b"      - name: brief\n        from: {graph_input: brief}\n"
        b"      - name: target\n"
        b'        value: "start the conductor workflow"\n',
    )
    assert self_starting != document

    with pytest.raises(ConductorDocumentDefect, match="unbounded billed tree"):
        require_conductor_document(self_starting)


def test_a_document_with_a_second_node_is_not_a_conductor() -> None:
    document = _conductor_document()
    two_nodes = document + (
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
    )

    with pytest.raises(ConductorDocumentDefect, match="exactly one agent node"):
        require_conductor_document(two_nodes)


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

    _app, runtime = compose_application(_doors_armed_settings(tmp_path))
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

        page = catalog.list_agent_configuration_revisions(None, 10)

        assert isinstance(page, AgentConfigurationRevisionPage)
        listed = {item.revision.revision_hash: item.startable for item in page.items}
        assert listed[configuration.revision_hash] is True
    finally:
        runtime.close()
