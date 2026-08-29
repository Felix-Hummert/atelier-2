"""Wait for the durable workflow event a scenario needs before reading its state."""

from __future__ import annotations

import time
from typing import Any

from dbos import DBOS

_WORKFLOW_CREATION_TIMEOUT_SECONDS = 16.0
_WORKFLOW_CREATION_POLL_SECONDS = 0.025


def wait_for_workflow_completion(workflow_id: str, awaited: str) -> Any:
    """Return a workflow's result after DBOS records its completion.

    The workflow may be enqueued by preceding durable work, so its status is
    observed until DBOS creates it. Once present, ``get_result`` waits for its
    completion event and propagates any workflow failure unchanged.
    """
    deadline = time.monotonic() + _WORKFLOW_CREATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if DBOS.get_workflow_status(workflow_id) is not None:
            return DBOS.retrieve_workflow(workflow_id).get_result()
        time.sleep(_WORKFLOW_CREATION_POLL_SECONDS)
    raise AssertionError(f"never observed {awaited}")
