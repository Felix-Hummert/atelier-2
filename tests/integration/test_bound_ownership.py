"""Where a persisted or edge bound is written, and where it may not be.

The suite next door already proves the schema's bounds *equal* the bounds their
contracts own. Equality is not ownership: two literals that happen to agree pass
it, and go on agreeing until one of them is edited. These tests are about the
second half — that the number is written once and derived everywhere else, so a
change at the owner cannot leave a copy behind.

They read source text on purpose. A value that is spelled twice is a property of
the source, not of the running object, and no assertion over imported constants
can see it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from atelier2.api.limits import base64_characters_for
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
)
from atelier2.host.serving import (
    MAXIMUM_FIELD_CHARACTERS,
    MAXIMUM_REQUEST_BODY_BYTES,
)

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "atelier2"

FIELD_BOUND_IN_A_CHECK = re.compile(r"BETWEEN 1 AND (\d+)")

BOUNDS_THE_SCHEMA_MAY_STILL_SPELL: dict[str, str] = {
    "64": (
        "two different 64s share this text: the provider-id bound, whose owner is "
        "a private pattern in contracts.agents rather than a named number, and the "
        "catalog kind-token bound, which has no owner at all. Deriving them needs "
        "an owner minted first, which is a contract decision and not this head's."
    ),
}
"""Literal bounds a CHECK may still carry, each with the reason it has no owner.

Naming them is what keeps the gap visible: a bound that quietly joins this map
is a review question, and a bound that leaves it can never come back silently.
"""


def source_of(relative: str) -> str:
    return (SOURCE_ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.proves("a-persisted-bound-is-written-once-and-derived-everywhere")
def test_no_schema_check_spells_the_field_bound_as_its_own_literal() -> None:
    """The schema may enforce the contract's bound; it may not restate it.

    A CHECK carrying its own number is the drift this item exists to remove: the
    contract can be raised and every one of these constraints keeps the old
    bound, with nothing red until a value between the two is stored.
    """
    spelled_out = set(
        FIELD_BOUND_IN_A_CHECK.findall(source_of("adapters/dbos/schema.py"))
    )

    assert spelled_out == set(BOUNDS_THE_SCHEMA_MAY_STILL_SPELL)


@pytest.mark.proves("a-persisted-bound-is-written-once-and-derived-everywhere")
def test_the_edge_field_bound_is_the_contracts_own_bound() -> None:
    assert MAXIMUM_FIELD_CHARACTERS == MAXIMUM_AGENT_FIELD_CHARACTERS
    assert "MAXIMUM_FIELD_CHARACTERS = 1_024" not in source_of("host/serving.py")


PAGE_LIMIT_RESTATED = re.compile(r"<= 100|> 100|from 1 to 100")
WORKFLOW_FORMAT_RESTATED = re.compile(
    r"workflow_format_version IN \(1, 2, 3\)"
    r"|not in \(1, 2, 3\)"
    r"|_V3_WORKFLOW_FORMAT_VERSION = 3"
    r"|if version == [123]:"
)


@pytest.mark.proves("a-persisted-bound-is-written-once-and-derived-everywhere")
def test_a_page_limit_is_written_once_and_derived_everywhere() -> None:
    """The 1-to-100 page bound has one owner; adapters name it, they do not retype it."""

    assert "MAXIMUM_PAGE_ITEMS = 100" in source_of("contracts/pages.py")
    for relative in (
        "adapters/dbos/queries.py",
        "adapters/dbos/agent_catalog.py",
        "api/limits.py",
        "api/_support.py",
        "api/stream.py",
        "host/serving.py",
    ):
        assert PAGE_LIMIT_RESTATED.search(source_of(relative)) is None, relative


@pytest.mark.proves("a-schema-check-cannot-silently-narrow-an-owned-vocabulary")
def test_a_workflow_format_is_written_once_and_derived_everywhere() -> None:
    """The 1-2-3 format set has one owner; adapters name it, they do not retype it."""

    owner = source_of("contracts/workflow_formats.py")
    assert "V1 = 1" in owner
    assert "V2 = 2" in owner
    assert "V3 = 3" in owner
    for relative in (
        "adapters/dbos/queries.py",
        "adapters/dbos/run_store.py",
        "adapters/dbos/run_transitions.py",
        "adapters/dbos/runtime.py",
        "adapters/dbos/schema.py",
        "adapters/dbos/starter.py",
        "adapters/yaml_workflows.py",
        "api/limits.py",
        "api/projection/events.py",
        "application/project_node_rail.py",
        "contracts/run_events.py",
    ):
        assert WORKFLOW_FORMAT_RESTATED.search(source_of(relative)) is None, relative


@pytest.mark.proves("a-persisted-bound-is-written-once-and-derived-everywhere")
def test_the_edge_body_bound_owns_the_envelope_around_an_encoded_payload() -> None:
    assert MAXIMUM_REQUEST_BODY_BYTES > base64_characters_for(
        MAXIMUM_AGENT_OUTPUT_BYTES_V2
    )
    assert "MAXIMUM_REQUEST_BODY_BYTES = 65_536" not in source_of("host/serving.py")
    assert "MAXIMUM_REQUEST_BODY_BYTES = MAXIMUM_BASE64_CHARACTERS" not in source_of(
        "host/serving.py"
    )
