"""Every authored workflow document in `workflows/` is a real definition source.

A file under `workflows/` is authored the way an operator authors one for the
Catalog's Git-source intake (#660): the same bytes a `git pull` would hand the
publish door. This suite is the fixture-side half of that promise -- it proves
each committed document parses through the production V3 parser and clears
the same executability door `POST /workflow-revisions` enforces, so a document
that only looks executable never lands here unnoticed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.contracts.workflows_v3 import (
    WorkflowGraphV3,
    what_a_v3_document_still_waits_for,
)

WORKFLOWS_DIRECTORY = Path(__file__).parents[2] / "workflows"
AUTHORED_WORKFLOW_PATHS = sorted(WORKFLOWS_DIRECTORY.glob("*.yaml"))


@pytest.mark.parametrize(
    "workflow_path", AUTHORED_WORKFLOW_PATHS, ids=lambda path: path.name
)
def test_an_authored_workflow_document_is_executable(workflow_path: Path) -> None:
    graph = parse_executable_workflow_document(workflow_path.read_bytes())

    assert isinstance(graph, WorkflowGraphV3)
    assert what_a_v3_document_still_waits_for(graph) is None


def test_the_workflows_directory_carries_at_least_one_authored_document() -> None:
    assert AUTHORED_WORKFLOW_PATHS, "workflows/ must not be an empty fixture door"
