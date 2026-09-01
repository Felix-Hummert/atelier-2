from __future__ import annotations

import pytest

from atelier2.contracts.workflow_documents import WORKFLOW_DOCUMENT_FORMATS
from atelier2.contracts.workflow_formats import WorkflowFormatVersion


def test_a_workflow_format_is_one_of_the_owned_members() -> None:
    assert WorkflowFormatVersion(1) is WorkflowFormatVersion.V1
    assert WorkflowFormatVersion(WorkflowFormatVersion.V3) is WorkflowFormatVersion.V3
    with pytest.raises(ValueError, match="4 is not a valid WorkflowFormatVersion"):
        WorkflowFormatVersion(4)


@pytest.mark.proves("a-schema-check-cannot-silently-narrow-an-owned-vocabulary")
def test_the_workflow_format_set_is_written_once() -> None:
    assert tuple(member.value for member in WorkflowFormatVersion) == (1, 2, 3)


def test_only_the_live_format_may_be_published() -> None:
    """A document may declare only the one format a model still reads.

    V1 and V2 stay named `WorkflowFormatVersion` members -- the durable
    layer's `runs.workflow_format_version` column and its frozen schema still
    hold those historical values -- but this table, which decides what a
    *published document* may declare, shrank to V3 alone when the V1/V2
    document grammar fell (#901 slice 5). The parser refuses a retired member
    by name instead of reaching this table as an unhandled key
    (`tests/domain/test_yaml_workflows.py` proves the refusal).
    """
    assert set(WORKFLOW_DOCUMENT_FORMATS) == {WorkflowFormatVersion.V3}
