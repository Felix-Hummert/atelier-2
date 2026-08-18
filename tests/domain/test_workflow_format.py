from __future__ import annotations

import pytest

from atelier2.contracts.workflow_formats import WorkflowFormatVersion


def test_a_workflow_format_is_one_of_the_owned_members() -> None:
    assert WorkflowFormatVersion(1) is WorkflowFormatVersion.V1
    assert WorkflowFormatVersion(WorkflowFormatVersion.V3) is WorkflowFormatVersion.V3
    with pytest.raises(ValueError, match="4 is not a valid WorkflowFormatVersion"):
        WorkflowFormatVersion(4)


@pytest.mark.proves("a-schema-check-cannot-silently-narrow-an-owned-vocabulary")
def test_the_workflow_format_set_is_written_once() -> None:
    assert tuple(member.value for member in WorkflowFormatVersion) == (1, 2, 3)
