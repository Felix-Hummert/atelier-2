from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import fields, replace
from pathlib import Path

import pytest

from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentOutputLimitExceeded,
    AgentReceiptV2,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import RunId, WorkflowRevisionHash


def _auth(
    *,
    profile_id: str = "max-primary",
    revision_number: int = 7,
    provider_id: str = "anthropic.claude",
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION,
) -> AuthProfileRevision:
    return AuthProfileRevision(
        profile_id,
        revision_number,
        ProviderId(provider_id),
        auth_mode,
    )


def _configuration(
    auth: AuthProfileRevision | None = None,
    *,
    model: str = "claude-opus-5",
    executor_revision: str = "claude-cli/v1",
) -> AgentConfigurationRevision:
    selected = _auth() if auth is None else auth
    return AgentConfigurationRevision(
        model,
        selected.revision_hash,
        AgentExecutorRevision(executor_revision),
    )


def _resolved() -> ResolvedAgentBinding:
    auth = _auth()
    return ResolvedAgentBinding(AgentRole("builder"), _configuration(auth), auth)


def test_profile_configuration_and_binding_hashes_are_fixed_vectors() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (
            AgentBinding(AgentRole("reviewer"), configuration.revision_hash),
            AgentBinding(AgentRole("builder"), configuration.revision_hash),
        )
    )

    assert (
        auth.revision_hash.value
        == "7ae178b50959a06bb720ebd05d8c98fdea6c18fe0f7952a1b4c886fb3003a670"
    )
    assert (
        configuration.revision_hash.value
        == "2b12dec1d7a461dd08e7c913be274b1d4a22c2a632c12f53c15dc1538b25b8a4"
    )
    assert (
        bindings.binding_set_hash.value
        == "1806a60f3554ea5a492d139c786ad046ae876598e10602b81c585a2b195fb2c5"
    )
    assert tuple(binding.role.value for binding in bindings.bindings) == (
        "builder",
        "reviewer",
    )


def test_the_demanded_capability_is_part_of_the_configuration_identity() -> None:
    auth = _auth()
    headless = _configuration(auth)
    interactive = replace(
        headless, requested_capability=AgentExecutionCapability.INTERACTIVE
    )

    assert headless.requested_capability is AgentExecutionCapability.HEADLESS
    assert interactive.revision_hash != headless.revision_hash


def test_public_auth_and_configuration_contracts_have_only_exact_safe_fields() -> None:
    assert tuple(field.name for field in fields(AuthProfileRevision)) == (
        "profile_id",
        "revision_number",
        "provider_id",
        "auth_mode",
        "revision_hash",
    )
    assert tuple(field.name for field in fields(AgentConfigurationRevision)) == (
        "model",
        "auth_profile_revision_hash",
        "executor_revision",
        "requested_capability",
        "revision_hash",
    )


def test_public_and_durable_symbols_expose_no_private_credential_channel() -> None:
    forbidden_symbols = {
        "secret",
        "secret_value",
        "credential",
        "credentials",
        "credential_handle",
        "credential_ref",
        "credential_path",
        "api_key_value",
        "key_name",
        "lookup_key",
        "private_credential_registry",
    }
    source_root = Path(__file__).parents[2] / "src" / "atelier2"
    channel_owners = (
        source_root / "contracts" / "agents.py",
        source_root / "contracts" / "run_bindings.py",
        source_root / "api" / "models.py",
        source_root / "api" / "app.py",
        source_root / "adapters" / "dbos" / "schema.py",
        source_root / "adapters" / "dbos" / "run_store.py",
        source_root / "adapters" / "dbos" / "workflow.py",
    )
    symbols: set[str] = set()
    for source_path in channel_owners:
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), source_path.as_posix()
        )
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.add(node.name.casefold())
            elif isinstance(node, ast.Name):
                symbols.add(node.id.casefold())
            elif isinstance(node, ast.arg):
                symbols.add(node.arg.casefold())
            elif isinstance(node, ast.Attribute):
                symbols.add(node.attr.casefold())

    assert symbols.isdisjoint(forbidden_symbols)


@pytest.mark.parametrize(
    ("mutation", "different"),
    [
        (lambda value: replace(value, profile_id="max-secondary"), "profile"),
        (lambda value: replace(value, revision_number=8), "number"),
        (lambda value: replace(value, provider_id=ProviderId("openai")), "provider"),
        (lambda value: replace(value, auth_mode=AuthMode.API_KEY), "mode"),
    ],
)
def test_every_auth_field_changes_its_revision_hash(
    mutation: Callable[[AuthProfileRevision], AuthProfileRevision], different: str
) -> None:
    original = _auth()
    changed = mutation(original)

    assert changed.revision_hash != original.revision_hash, different


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: replace(value, model="claude-sonnet-5"),
        lambda value: replace(
            value, auth_profile_revision_hash=_auth(profile_id="other").revision_hash
        ),
        lambda value: replace(
            value, executor_revision=AgentExecutorRevision("claude-cli/v2")
        ),
    ),
)
def test_every_configuration_field_changes_its_revision_hash(
    mutation: Callable[[AgentConfigurationRevision], AgentConfigurationRevision],
) -> None:
    original = _configuration()

    assert mutation(original).revision_hash != original.revision_hash


def test_every_binding_entry_field_changes_the_binding_set_hash() -> None:
    configuration = _configuration()
    original = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    changed_role = AgentBindingSet(
        (AgentBinding(AgentRole("reviewer"), configuration.revision_hash),)
    )
    changed_configuration = AgentBindingSet(
        (
            AgentBinding(
                AgentRole("builder"),
                _configuration(model="claude-sonnet-5").revision_hash,
            ),
        )
    )

    assert changed_role.binding_set_hash != original.binding_set_hash
    assert changed_configuration.binding_set_hash != original.binding_set_hash


@pytest.mark.parametrize(
    "value",
    ("", "A", "-openai", "open ai", "openai/cli", "a" * 65),
)
def test_provider_id_is_an_extensible_lowercase_ascii_slug(value: str) -> None:
    with pytest.raises(ValueError):
        ProviderId(value)


@pytest.mark.parametrize("value", ("", "x" * 1025))
def test_exact_human_fields_share_the_closed_character_bound(value: str) -> None:
    with pytest.raises(ValueError):
        AgentRole(value)
    with pytest.raises(ValueError):
        AuthProfileRevision(value, 1, ProviderId("openai"), AuthMode.API_KEY)


def test_roles_keep_exact_unicode_without_normalization() -> None:
    assert AgentRole("Reviewér") != AgentRole("Reviewér")
    assert AgentRole(" Builder ").value == " Builder "


def test_binding_set_rejects_duplicate_exact_roles() -> None:
    configuration = _configuration()

    with pytest.raises(ValueError, match="unique"):
        AgentBindingSet(
            (
                AgentBinding(AgentRole("builder"), configuration.revision_hash),
                AgentBinding(AgentRole("builder"), configuration.revision_hash),
            )
        )


def test_v2_request_and_receipt_are_fixed_and_tamper_evident() -> None:
    run_id = RunId("run/v2")
    revision_hash = WorkflowRevisionHash("1" * 64)
    execution_id = NodeExecutionId.for_node(run_id, revision_hash, "agent")
    resolved = _resolved()
    operational_identity = AgentExecutorOperationalIdentity("claude-process-17")
    request = AgentExecutionRequestV2(
        execution_id,
        run_id,
        revision_hash,
        "agent",
        resolved,
        operational_identity,
        b"implement the story",
    )
    binding_set = AgentBindingSet(
        (AgentBinding(resolved.role, resolved.configuration.revision_hash),)
    )
    receipt = AgentReceiptV2.for_execution(
        request,
        binding_set.binding_set_hash,
        AgentExecutionResult(b"\xff\x00done"),
    )

    assert (
        request.request_hash.value
        == "52b5804335c67689bdd9edc74a7a1404390893211c5d74e0582292eb238a86ef"
    )
    assert (
        receipt.output_hash.value
        == "34eeebf86b55024d6a7c56aa5ffc14c5da8b425f007aeeaacdee8e8fe6b6e9bc"
    )
    assert (
        receipt.receipt_hash.value
        == "a20f510d292e9a30af3414db23cc5168c5d539bba77847ce6161b00266dc2314"
    )

    with pytest.raises(ValueError, match="binding"):
        replace(receipt, role=AgentRole("reviewer"))
    with pytest.raises(ValueError, match="hash"):
        replace(receipt, output_bytes=b"changed")


def _request_variant(field: str) -> AgentExecutionRequestV2:
    run_id = RunId("run/v2" if field != "run_id" else "run/changed")
    revision_hash = WorkflowRevisionHash("1" * 64 if field != "revision" else "2" * 64)
    node_id = "agent" if field != "node" else "other-agent"
    auth = _auth(
        profile_id="other-profile" if field == "profile_id" else "max-primary",
        revision_number=8 if field == "revision_number" else 7,
        provider_id="openai" if field == "provider_id" else "anthropic.claude",
        auth_mode=AuthMode.API_KEY if field == "auth_mode" else AuthMode.SUBSCRIPTION,
    )
    configuration = _configuration(
        auth,
        model="claude-sonnet-5" if field == "model" else "claude-opus-5",
        executor_revision="claude-cli/v2"
        if field == "executor_revision"
        else "claude-cli/v1",
    )
    resolved = ResolvedAgentBinding(
        AgentRole("reviewer" if field == "role" else "builder"),
        configuration,
        auth,
    )
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, node_id),
        run_id,
        revision_hash,
        node_id,
        resolved,
        AgentExecutorOperationalIdentity(
            "other-operation" if field == "operational_identity" else "operation"
        ),
        b"changed job" if field == "job" else b"job",
    )


@pytest.mark.parametrize(
    "field",
    (
        "run_id",
        "revision",
        "node",
        "role",
        "profile_id",
        "revision_number",
        "provider_id",
        "auth_mode",
        "model",
        "executor_revision",
        "operational_identity",
        "job",
    ),
)
def test_every_v2_request_input_changes_the_request_hash(field: str) -> None:
    assert (
        _request_variant(field).request_hash
        != _request_variant("unchanged").request_hash
    )


def _tamper_receipt(receipt: AgentReceiptV2, field: str) -> AgentReceiptV2:
    changed: dict[str, object] = {
        "request_hash": type(receipt.request_hash)("0" * 64),
        "node_execution_id": NodeExecutionId("0" * 64),
        "run_id": RunId("other-run"),
        "workflow_revision_hash": WorkflowRevisionHash("2" * 64),
        "node_id": "other-node",
        "role": AgentRole("reviewer"),
        "binding_set_hash": type(receipt.binding_set_hash)("0" * 64),
        "agent_configuration_revision_hash": type(
            receipt.agent_configuration_revision_hash
        )("0" * 64),
        "auth_profile_revision_hash": type(receipt.auth_profile_revision_hash)(
            "0" * 64
        ),
        "profile_id": "other-profile",
        "revision_number": 8,
        "provider_id": ProviderId("openai"),
        "auth_mode": AuthMode.API_KEY,
        "model": "other-model",
        "executor_revision": AgentExecutorRevision("other-executor"),
        "executor_operational_identity": AgentExecutorOperationalIdentity(
            "other-operation"
        ),
        "output_bytes": b"other-output",
        "output_hash": type(receipt.output_hash)("0" * 64),
        "receipt_hash": type(receipt.receipt_hash)("0" * 64),
    }
    return replace(receipt, **{field: changed[field]})


@pytest.mark.parametrize(
    "field",
    (
        "request_hash",
        "node_execution_id",
        "run_id",
        "workflow_revision_hash",
        "node_id",
        "role",
        "binding_set_hash",
        "agent_configuration_revision_hash",
        "auth_profile_revision_hash",
        "profile_id",
        "revision_number",
        "provider_id",
        "auth_mode",
        "model",
        "executor_revision",
        "executor_operational_identity",
        "output_bytes",
        "output_hash",
        "receipt_hash",
    ),
)
def test_every_v2_receipt_field_is_tamper_evident(field: str) -> None:
    request = _request_variant("unchanged")
    binding_set = AgentBindingSet(
        (
            AgentBinding(
                request.resolved_binding.role,
                request.resolved_binding.configuration.revision_hash,
            ),
        )
    )
    receipt = AgentReceiptV2.for_execution(
        request, binding_set.binding_set_hash, AgentExecutionResult(b"output")
    )

    with pytest.raises(ValueError):
        _tamper_receipt(receipt, field)


def test_v2_output_bound_accepts_49152_and_rejects_49153_before_receipt() -> None:
    run_id = RunId("run/v2")
    revision_hash = WorkflowRevisionHash("1" * 64)
    resolved = _resolved()
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, "agent"),
        run_id,
        revision_hash,
        "agent",
        resolved,
        AgentExecutorOperationalIdentity("process"),
        b"job",
    )
    binding_set = AgentBindingSet(
        (AgentBinding(resolved.role, resolved.configuration.revision_hash),)
    )

    accepted = AgentReceiptV2.for_execution(
        request,
        binding_set.binding_set_hash,
        AgentExecutionResult(b"x" * MAXIMUM_AGENT_OUTPUT_BYTES_V2),
    )
    assert len(accepted.output_bytes) == MAXIMUM_AGENT_OUTPUT_BYTES_V2
    with pytest.raises(AgentOutputLimitExceeded):
        AgentReceiptV2.for_execution(
            request,
            binding_set.binding_set_hash,
            AgentExecutionResult(b"x" * (MAXIMUM_AGENT_OUTPUT_BYTES_V2 + 1)),
        )
