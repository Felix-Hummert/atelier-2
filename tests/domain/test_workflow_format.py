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


def test_every_owned_format_version_names_the_model_that_reads_it() -> None:
    """A version a document may declare is a version something can read.

    The publication looks its format up in this table, so a member without an
    entry would reach a caller as an unhandled key rather than as the named
    refusal an author can act on.
    """
    assert set(WORKFLOW_DOCUMENT_FORMATS) == set(WorkflowFormatVersion)
