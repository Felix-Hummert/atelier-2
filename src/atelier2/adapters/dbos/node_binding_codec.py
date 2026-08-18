"""How a node binding is written into a durable step output, and read back from one.

**Why the encoded form is not the contract.** What `node-binding/0` returns is
recorded in `operation_outputs` and replayed to whatever code recovers the run,
so the shape is a storage format with rows already written in it. DBOS reads a
recorded output by its ordinal and pickles it, which means renaming the step
migrates nothing and returning a class from it would pickle class paths into
durable rows. The step therefore keeps returning a plain dictionary, and the
typed `NodeBinding` is made on this side of the step boundary.

**One legacy form, named.** A binding written before the configuration contract
existed carries neither `revision_format_version` nor `requested_capability`, and
means exactly `V1` and `HEADLESS`. That is the only shape missing them that this
codec accepts; a form carrying one of the two, an unknown key, a missing key, an
unknown value or a hash its own fields do not produce is refused by name rather
than read as the legacy one. The legacy arm may be deleted once no
`operation_outputs` row without both keys can exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, NotRequired, TypedDict, assert_never

from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    AuthProfileRevisionHash,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.node_bindings import (
    ActionNodeBinding,
    AgentNodeBinding,
    AgentNodeBindingV2,
    NodeBinding,
    SubworkflowNodeBinding,
    WaitNodeBinding,
)
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_bindings import RunBindingConflict
from atelier2.contracts.tool_grants_v3 import DeclaredToolGrant, ToolGrantCapability


class EncodedAgentBinding(TypedDict):
    type: Literal["agent"]
    job: str
    output: str


class EncodedAgentBindingV2(TypedDict):
    type: Literal["agent-v2"]
    role: str
    job: str
    tool_revision_hash: NotRequired[str]
    tool_capability: NotRequired[str]
    project_commit: NotRequired[str]
    project_tree: NotRequired[str]
    configuration_hash: str
    auth_hash: str
    profile_id: str
    revision_number: int
    provider_id: str
    auth_mode: str
    model: str
    executor_revision: str
    revision_format_version: NotRequired[int]
    requested_capability: NotRequired[str]
    output_schema_document: NotRequired[str]


class EncodedActionBinding(TypedDict):
    type: Literal["action"]


class EncodedWaitBinding(TypedDict):
    type: Literal["wait"]


class EncodedSubworkflowBinding(TypedDict):
    type: Literal["subworkflow"]
    left: int
    right: int


type EncodedNodeBinding = (
    EncodedAgentBinding
    | EncodedAgentBindingV2
    | EncodedActionBinding
    | EncodedWaitBinding
    | EncodedSubworkflowBinding
)

_FORM_ONLY_KEYS = frozenset({"type"})
_AGENT_KEYS = frozenset({"type", "job", "output"})
_SUBWORKFLOW_KEYS = frozenset({"type", "left", "right"})
_AGENT_V2_KEYS = frozenset(
    [
        "type",
        "role",
        "job",
        "configuration_hash",
        "auth_hash",
        "profile_id",
        "revision_number",
        "provider_id",
        "auth_mode",
        "model",
        "executor_revision",
    ]
)
_AGENT_V2_OPTIONAL_KEYS = frozenset(
    [
        "tool_revision_hash",
        "tool_capability",
        "project_commit",
        "project_tree",
        "revision_format_version",
        "requested_capability",
        "output_schema_document",
    ]
)


def encode_node_binding(binding: NodeBinding) -> EncodedNodeBinding:
    """The durable form of this binding, in the shape every recorded row carries."""
    match binding:
        case AgentNodeBinding(job=job, output=output):
            return {"type": "agent", "job": job, "output": output}
        case AgentNodeBindingV2():
            return _encode_agent_v2(binding)
        case ActionNodeBinding():
            return {"type": "action"}
        case WaitNodeBinding():
            return {"type": "wait"}
        case SubworkflowNodeBinding(operands=(left, right)):
            return {"type": "subworkflow", "left": left, "right": right}
        case _ as unreachable:
            assert_never(unreachable)


def decode_node_binding(encoded: Mapping[str, object]) -> NodeBinding:
    """What a recorded step output binds, or the named refusal that it binds nothing."""
    match encoded.get("type"):
        case "agent":
            _refuse_foreign_keys(encoded, _AGENT_KEYS)
            return AgentNodeBinding(_text(encoded, "job"), _text(encoded, "output"))
        case "agent-v2":
            return _decode_agent_v2(encoded)
        case "action":
            _refuse_foreign_keys(encoded, _FORM_ONLY_KEYS)
            return ActionNodeBinding()
        case "wait":
            _refuse_foreign_keys(encoded, _FORM_ONLY_KEYS)
            return WaitNodeBinding()
        case "subworkflow":
            _refuse_foreign_keys(encoded, _SUBWORKFLOW_KEYS)
            return SubworkflowNodeBinding(
                (_whole_number(encoded, "left"), _whole_number(encoded, "right"))
            )
        case _:
            raise RunBindingConflict(
                "a durable node binding names no form this adapter writes"
            )


def _encode_agent_v2(binding: AgentNodeBindingV2) -> EncodedAgentBindingV2:
    configuration = binding.resolved.configuration
    auth = binding.resolved.auth_profile
    encoded: EncodedAgentBindingV2 = {
        "type": "agent-v2",
        "role": binding.resolved.role.value,
        "job": binding.job,
        "configuration_hash": configuration.revision_hash.value,
        "auth_hash": auth.revision_hash.value,
        "profile_id": auth.profile_id,
        "revision_number": auth.revision_number,
        "provider_id": auth.provider_id.value,
        "auth_mode": auth.auth_mode.value,
        "model": configuration.model,
        "executor_revision": configuration.executor_revision.value,
        "revision_format_version": int(configuration.revision_format_version),
        "requested_capability": configuration.requested_capability.value,
    }
    if binding.tool_grant is not None:
        encoded["tool_revision_hash"] = binding.tool_grant.revision_hash.value
        encoded["tool_capability"] = binding.tool_grant.capability.value
    if binding.declared_output_schema_document is not None:
        encoded["output_schema_document"] = binding.declared_output_schema_document
    if binding.project_source is not None:
        encoded["project_commit"] = binding.project_source.commit
        encoded["project_tree"] = binding.project_source.tree
    return encoded


def _decode_agent_v2(encoded: Mapping[str, object]) -> AgentNodeBindingV2:
    _refuse_foreign_keys(encoded, _AGENT_V2_KEYS, _AGENT_V2_OPTIONAL_KEYS)
    auth = _auth_profile(encoded)
    configuration = _configuration(encoded, auth)
    try:
        resolved = ResolvedAgentBinding(
            AgentRole(_text(encoded, "role")), configuration, auth
        )
    except (TypeError, ValueError) as error:
        raise RunBindingConflict(
            "a durable agent binding carries an invalid combination"
        ) from error
    return AgentNodeBindingV2(
        resolved,
        _text(encoded, "job"),
        _declared_tool_grant(encoded),
        _declared_source_pin(encoded),
        _declared_output_schema_document(encoded),
    )


def _auth_profile(encoded: Mapping[str, object]) -> AuthProfileRevision:
    try:
        auth = AuthProfileRevision(
            _text(encoded, "profile_id"),
            _whole_number(encoded, "revision_number"),
            ProviderId(_text(encoded, "provider_id")),
            AuthMode(_text(encoded, "auth_mode")),
        )
    except (TypeError, ValueError) as error:
        raise RunBindingConflict(
            "a durable auth profile carries an unknown value"
        ) from error
    if auth.revision_hash != AuthProfileRevisionHash(_text(encoded, "auth_hash")):
        raise RunBindingConflict("V2 auth fields differ from their durable hash")
    return auth


def _configuration(
    encoded: Mapping[str, object], auth: AuthProfileRevision
) -> AgentConfigurationRevision:
    revision_format_version, requested_capability = _declared_contract(encoded)
    try:
        configuration = AgentConfigurationRevision(
            _text(encoded, "model"),
            auth.revision_hash,
            AgentExecutorRevision(_text(encoded, "executor_revision")),
            requested_capability,
            revision_format_version,
        )
    except (TypeError, ValueError) as error:
        raise RunBindingConflict(
            "V2 configuration contract carries an invalid combination"
        ) from error
    if configuration.revision_hash != AgentConfigurationRevisionHash(
        _text(encoded, "configuration_hash")
    ):
        raise RunBindingConflict(
            "V2 configuration fields differ from their durable hash"
        )
    return configuration


def _declared_contract(
    encoded: Mapping[str, object],
) -> tuple[AgentConfigurationRevisionFormatVersion, AgentExecutionCapability]:
    """The configuration contract this row carries, or the one legacy row's meaning."""
    has_version = "revision_format_version" in encoded
    has_capability = "requested_capability" in encoded
    if not has_version and not has_capability:
        return (
            AgentConfigurationRevisionFormatVersion.V1,
            AgentExecutionCapability.HEADLESS,
        )
    if not has_version or not has_capability:
        raise RunBindingConflict("V2 configuration contract is only partly encoded")
    version = encoded["revision_format_version"]
    capability = encoded["requested_capability"]
    if type(version) is not int or type(capability) is not str:
        raise RunBindingConflict(
            "V2 configuration contract carries a value of the wrong type"
        )
    try:
        return (
            AgentConfigurationRevisionFormatVersion(version),
            AgentExecutionCapability(capability),
        )
    except ValueError as error:
        raise RunBindingConflict(
            "V2 configuration contract carries an unknown value"
        ) from error


def _declared_tool_grant(encoded: Mapping[str, object]) -> DeclaredToolGrant | None:
    """The grant a durable node binding carries, or nothing where none was pinned."""
    has_revision = "tool_revision_hash" in encoded
    has_capability = "tool_capability" in encoded
    if not has_revision and not has_capability:
        return None
    if not has_revision or not has_capability:
        raise RunBindingConflict("a durable tool grant is only partly encoded")
    try:
        return DeclaredToolGrant(
            PublishedRevisionHash(_text(encoded, "tool_revision_hash")),
            ToolGrantCapability(_text(encoded, "tool_capability")),
        )
    except (TypeError, ValueError) as error:
        raise RunBindingConflict(
            "a durable tool grant carries an unknown value"
        ) from error


def _declared_output_schema_document(encoded: Mapping[str, object]) -> str | None:
    """The schema document a durable binding carries, or nothing where none was."""
    if "output_schema_document" not in encoded:
        return None
    document = encoded["output_schema_document"]
    if type(document) is not str:
        raise RunBindingConflict(
            "a durable output schema document carries a value of the wrong type"
        )
    return document


def _declared_source_pin(encoded: Mapping[str, object]) -> ProjectSourcePin | None:
    """The project source a durable node binding pinned, or nothing where none was."""
    has_commit = "project_commit" in encoded
    has_tree = "project_tree" in encoded
    if not has_commit and not has_tree:
        return None
    if not has_commit or not has_tree:
        raise RunBindingConflict("a durable project source pin is only partly encoded")
    try:
        return ProjectSourcePin(
            _text(encoded, "project_commit"), _text(encoded, "project_tree")
        )
    except (TypeError, ValueError) as error:
        raise RunBindingConflict(
            "a durable project source pin carries an unknown value"
        ) from error


def _refuse_foreign_keys(
    encoded: Mapping[str, object],
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    present = frozenset(encoded)
    if present - required - optional:
        raise RunBindingConflict(
            "a durable node binding carries a key its form does not declare"
        )
    if required - present:
        raise RunBindingConflict(
            "a durable node binding is missing a key its form declares"
        )


def _text(encoded: Mapping[str, object], key: str) -> str:
    value = encoded[key]
    if type(value) is not str:
        raise RunBindingConflict(
            f"a durable node binding carries {key} as a value of the wrong type"
        )
    return value


def _whole_number(encoded: Mapping[str, object], key: str) -> int:
    value = encoded[key]
    if type(value) is not int:
        raise RunBindingConflict(
            f"a durable node binding carries {key} as a value of the wrong type"
        )
    return value
