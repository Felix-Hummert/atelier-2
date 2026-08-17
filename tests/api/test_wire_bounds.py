"""Every length bound the wire enforces belongs to the contract that owns it.

The wire is the outermost edge of the same fields the store already bounds. When
it types its own number, the two agree only until one of them moves: the contract
could widen `role` and the API would still refuse at the old width, refusing
input the durable side would have accepted, and no test would notice. That is not
hypothetical shape -- the same drift is what the schema-side bound-ownership
tests exist to catch, and this is the wire's half of it.

Two tests, because there are two ways to drift:

  - a bound whose value stopped matching its owner, or a newly bounded field
    nobody declared an owner for, and
  - a bound typed as a literal, which cannot follow its owner at all.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType

from annotated_types import MaxLen
from pydantic import BaseModel

from atelier2.api.references import MAXIMUM_RUN_AGENT_BINDINGS
from atelier2.api.wire import events, requests, resources
from atelier2.contracts.agent_attempts import REPLACEMENT_AGENT_ATTEMPT_ORDINAL
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_PROVIDER_ID_CHARACTERS,
)
from atelier2.contracts.catalog_v3 import (
    MAXIMUM_CATALOG_ACTOR_CHARACTERS,
    MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS,
)

WIRE_MODULES: tuple[ModuleType, ...] = (requests, resources, events)

# Which owner each bounded wire field answers to. Three of them are contracts the
# durable side already obeys; the fourth is the wire's own, because no durable
# owner caps how many roles one run binds.
OWNED_WIRE_BOUNDS: Mapping[str, int] = {
    "AgentAttemptCancellationResourceV2.command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentBindingResourceV2.executor_revision": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentBindingResourceV2.model": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentBindingResourceV2.profile_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentBindingResourceV2.provider_id": MAXIMUM_PROVIDER_ID_CHARACTERS,
    "AgentBindingResourceV2.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AdmitCatalogMemberRequestResource.actor": MAXIMUM_CATALOG_ACTOR_CHARACTERS,
    "AgentCancelRequestedEventResourceV2.command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentCancelledEventResourceV2.command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "CatalogAdmissionResource.display_name": (MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS),
    "FoundCatalogLineageRequestResource.actor": MAXIMUM_CATALOG_ACTOR_CHARACTERS,
    "FoundCatalogLineageRequestResource.display_name": (
        MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS
    ),
    "AgentConfigurationRevisionResource.executor_revision": (
        MAXIMUM_AGENT_FIELD_CHARACTERS
    ),
    "AgentConfigurationRevisionResource.model": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AgentConfigurationRevisionResource.provider_id": MAXIMUM_PROVIDER_ID_CHARACTERS,
    "AgentInterruptedEventResourceV2.command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AuthProfileRevisionResource.profile_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "AuthProfileRevisionResource.provider_id": MAXIMUM_PROVIDER_ID_CHARACTERS,
    "CancelAgentAttemptRequestResource.command_id": MAXIMUM_AGENT_FIELD_CHARACTERS,
    "CatalogNameResolutionResource.display_name": (
        MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS
    ),
    "PublishAgentConfigurationRevisionRequestResource.executor_revision": (
        MAXIMUM_AGENT_FIELD_CHARACTERS
    ),
    "PublishAgentConfigurationRevisionRequestResource.model": (
        MAXIMUM_AGENT_FIELD_CHARACTERS
    ),
    "PublishAuthProfileRevisionRequestResource.profile_id": (
        MAXIMUM_AGENT_FIELD_CHARACTERS
    ),
    "PublishAuthProfileRevisionRequestResource.provider_id": (
        MAXIMUM_PROVIDER_ID_CHARACTERS
    ),
    "RunResourceV2.agent_attempts": REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    "RunResourceV2.agent_bindings": MAXIMUM_RUN_AGENT_BINDINGS,
    "RunResourceV3.agent_bindings": MAXIMUM_RUN_AGENT_BINDINGS,
    # A document declares no more roles than a run can bind: one role is one
    # binding, so the two carry the same limit for the same reason.
    "WorkflowGraphResourceV3.agent_roles": MAXIMUM_RUN_AGENT_BINDINGS,
    "StartRunRequestResourceV2.agent_bindings": MAXIMUM_RUN_AGENT_BINDINGS,
    "StartRunAgentBindingResourceV2.role": MAXIMUM_AGENT_FIELD_CHARACTERS,
}


def _wire_models(module: ModuleType) -> Iterator[type[BaseModel]]:
    for name, member in vars(module).items():
        if (
            isinstance(member, type)
            and issubclass(member, BaseModel)
            and member.__module__ == module.__name__
            and member.__name__ == name
        ):
            yield member


def _declared_wire_bounds() -> Mapping[str, int]:
    """Every maximum length the wire actually enforces, read off the models."""
    declared: dict[str, int] = {}
    for module in WIRE_MODULES:
        for model in _wire_models(module):
            for field_name, field in model.model_fields.items():
                for constraint in field.metadata:
                    if isinstance(constraint, MaxLen):
                        declared[f"{model.__name__}.{field_name}"] = (
                            constraint.max_length
                        )
    return declared


def _typed_bound_literals(module: ModuleType) -> tuple[str, ...]:
    """Where a wire module writes a maximum length as a number of its own."""
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    typed: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "max_length" and isinstance(keyword.value, ast.Constant):
                typed.append(f"{module.__name__}:{keyword.value.lineno}")
    return tuple(typed)


def test_every_bounded_wire_field_is_bounded_by_the_contract_that_owns_it() -> None:
    """Drift in both directions is red: a moved value, or an unowned new field."""
    assert _declared_wire_bounds() == OWNED_WIRE_BOUNDS


def test_no_wire_field_types_a_bound_it_does_not_own() -> None:
    """A literal cannot follow its owner, so the wire never writes one."""
    typed = tuple(
        location
        for module in WIRE_MODULES
        for location in _typed_bound_literals(module)
    )

    assert typed == ()
