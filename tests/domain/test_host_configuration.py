"""The host configuration channel's own records and named refusals."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier2.contracts.agents import AgentConfigurationRevisionHash, AgentRole
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import (
    MAXIMUM_PROJECT_ID_CHARACTERS,
    PROJECT_UNKNOWN,
    OccupancyBinding,
    OccupancyRevision,
    ProjectId,
    ProjectRootRevision,
    ProjectUnknown,
)


def test_a_project_id_is_the_exact_characters_it_was_given() -> None:
    assert ProjectId("studio").value == "studio"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("x" * (MAXIMUM_PROJECT_ID_CHARACTERS + 1), id="too long"),
    ],
)
def test_a_bad_project_id_is_refused_as_project_unknown(value: str) -> None:
    with pytest.raises(ProjectUnknown, match=PROJECT_UNKNOWN):
        ProjectId(value)


def test_the_same_project_root_revision_is_the_same_hash(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    first = ProjectRootRevision(ProjectId("studio"), 1, root)
    second = ProjectRootRevision(ProjectId("studio"), 1, root)

    assert first.revision_hash == second.revision_hash
    assert first.root_path == root.resolve()


def test_a_later_revision_or_another_project_is_a_different_hash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    first = ProjectRootRevision(ProjectId("studio"), 1, root)
    later = ProjectRootRevision(ProjectId("studio"), 2, root)
    other = ProjectRootRevision(ProjectId("other"), 1, root)

    assert first.revision_hash != later.revision_hash
    assert first.revision_hash != other.revision_hash


def _occupancy(
    *,
    project: str = "studio",
    lineage: str = "ab" * 32,
    revision_number: int = 1,
    bindings: tuple[OccupancyBinding, ...] = (),
) -> OccupancyRevision:
    return OccupancyRevision(
        ProjectId(project),
        CatalogLineageId(lineage),
        revision_number,
        bindings,
    )


def test_the_same_occupancy_revision_is_the_same_hash() -> None:
    binding = OccupancyBinding(
        AgentRole("chef"), AgentConfigurationRevisionHash("cd" * 32)
    )
    first = _occupancy(bindings=(binding,))
    second = _occupancy(bindings=(binding,))

    assert first.revision_hash == second.revision_hash
    assert first.bindings == (binding,)


def test_occupancy_bindings_are_canonical_by_role() -> None:
    chef = OccupancyBinding(
        AgentRole("chef"), AgentConfigurationRevisionHash("cd" * 32)
    )
    baker = OccupancyBinding(
        AgentRole("baker"), AgentConfigurationRevisionHash("ef" * 32)
    )
    first = _occupancy(bindings=(chef, baker))
    second = _occupancy(bindings=(baker, chef))

    assert first.revision_hash == second.revision_hash
    assert first.bindings == (baker, chef)


def test_a_later_occupancy_revision_or_another_key_is_a_different_hash() -> None:
    binding = OccupancyBinding(
        AgentRole("chef"), AgentConfigurationRevisionHash("cd" * 32)
    )
    first = _occupancy(bindings=(binding,))
    later = _occupancy(revision_number=2, bindings=(binding,))
    other_project = _occupancy(project="other", bindings=(binding,))
    other_lineage = _occupancy(lineage="11" * 32, bindings=(binding,))
    other_binding = _occupancy(
        bindings=(
            OccupancyBinding(
                AgentRole("chef"), AgentConfigurationRevisionHash("ee" * 32)
            ),
        )
    )

    assert first.revision_hash != later.revision_hash
    assert first.revision_hash != other_project.revision_hash
    assert first.revision_hash != other_lineage.revision_hash
    assert first.revision_hash != other_binding.revision_hash


def test_duplicate_occupancy_roles_are_refused() -> None:
    first = OccupancyBinding(
        AgentRole("chef"), AgentConfigurationRevisionHash("cd" * 32)
    )
    second = OccupancyBinding(
        AgentRole("chef"), AgentConfigurationRevisionHash("ef" * 32)
    )

    with pytest.raises(ValueError, match="unique"):
        _occupancy(bindings=(first, second))
