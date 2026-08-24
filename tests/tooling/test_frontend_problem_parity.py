from __future__ import annotations

from pathlib import Path

from atelier2.api.problems import PROBLEM_DEFINITIONS

PROJECT_ROOT = Path(__file__).parents[2]
FRONTEND_CLIENT = PROJECT_ROOT / "frontend" / "src" / "api" / "client.ts"
"""The hand-maintained frontend decoder that must mirror every backend problem.

The exact bidirectional match is enforced by the Cockpit vitest against the
frozen OpenAPI document. That job is slow and frontend-only, so a backend
addition that forgets its mirror stays invisible to the fast Python suite until
a full frontend/e2e cycle runs. This guard reads the decoder as text and fails
here instead, naming the missing keys.
"""


def test_every_backend_problem_type_is_mirrored_in_the_frontend_decoder() -> None:
    client_source = FRONTEND_CLIENT.read_text(encoding="utf-8")

    missing = sorted(
        code for code in PROBLEM_DEFINITIONS if f'"{code}"' not in client_source
    )

    assert not missing, (
        "The backend publishes problem types the frontend decoder does not "
        f'mirror. Add each as a "<key>" literal to problemDefinitions and the '
        f"problemSchema union in {FRONTEND_CLIENT.relative_to(PROJECT_ROOT)}: "
        + ", ".join(missing)
    )
