"""The host configuration channel's own records and named refusals."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier2.contracts.host_configuration import (
    MAXIMUM_PROJECT_ID_CHARACTERS,
    PROJECT_UNKNOWN,
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
