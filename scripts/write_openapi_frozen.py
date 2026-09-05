"""Regenerates the frozen OpenAPI document `tests/api/test_openapi.py` pins.

A route change makes the FastAPI-generated document disagree with the frozen
artefact. Before this script existed, closing that gap meant hand-editing a
single-line, quarter-megabyte JSON file, and two lanes touching different
routes collided on every byte. `--check` reports the disagreement instead of
requiring a rewrite to see it.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FROZEN_DOCUMENT_RELATIVE_PATH = Path("tests") / "api" / "openapi_frozen.json"


def frozen_document_path(project_root: Path) -> Path:
    return project_root / FROZEN_DOCUMENT_RELATIVE_PATH


def served_app() -> FastAPI:
    """The application `test_openapi.py` serves, built without a live server.

    `tests` is not an installed package; pytest's rootdir insertion supplies it
    when a test imports this module, but running this file directly needs the
    project root added here first, before the import below can resolve it.
    """

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from atelier2.api.app import create_app
    from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

    return create_app(
        source_commit="commit",
        source_tree="tree",
        ports=api_ports(),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
    )


def rendered_document(document: dict[str, Any]) -> str:
    """The frozen artefact's exact text, the one serialisation both sides read.

    Sorted keys and stable indentation keep a route change's diff to the routes
    it touched instead of a reordering churn across the whole file.
    """

    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def current_document_text() -> str:
    return rendered_document(served_app().openapi())


def regenerate_frozen_document(frozen_path: Path) -> None:
    frozen_path.parent.mkdir(parents=True, exist_ok=True)
    frozen_path.write_text(current_document_text(), encoding="utf-8")


def _first_differing_path(previous: Any, current: Any, path: str = "$") -> str | None:
    if previous == current:
        return None
    if isinstance(previous, dict) and isinstance(current, dict):
        for key in sorted(set(previous) | set(current)):
            if key not in previous:
                return f"{path}.{key} (added)"
            if key not in current:
                return f"{path}.{key} (removed)"
            found = _first_differing_path(previous[key], current[key], f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(previous, list) and isinstance(current, list):
        for index, (previous_item, current_item) in enumerate(zip(previous, current)):
            found = _first_differing_path(
                previous_item, current_item, f"{path}[{index}]"
            )
            if found is not None:
                return found
        if len(previous) != len(current):
            return f"{path} (length {len(previous)} -> {len(current)})"
        return None
    return path


def _line_change_counts(previous_text: str, current_text: str) -> tuple[int, int]:
    added = removed = 0
    for line in difflib.unified_diff(
        previous_text.splitlines(), current_text.splitlines(), lineterm=""
    ):
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def diff_report(frozen_path: Path) -> str | None:
    """`None` when the frozen document matches; a summary of the drift otherwise."""

    if not frozen_path.exists():
        return (
            f"{frozen_path} does not exist; run this script without --check "
            "to create it."
        )
    frozen_text = frozen_path.read_text(encoding="utf-8")
    current_text = current_document_text()
    if current_text == frozen_text:
        return None
    path = _first_differing_path(json.loads(frozen_text), json.loads(current_text))
    added, removed = _line_change_counts(frozen_text, current_text)
    return (
        f"{frozen_path} is stale: first differing path {path or '<the whole document>'} "
        f"({added} line(s) added, {removed} line(s) removed). "
        "Regenerate it with `uv run python scripts/write_openapi_frozen.py`."
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate tests/api/openapi_frozen.json from the served "
            "application's OpenAPI document."
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 instead of writing when the frozen document is stale",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    frozen_path = frozen_document_path(Path.cwd())
    if arguments.check:
        problem = diff_report(frozen_path)
        if problem is not None:
            print(problem, file=sys.stderr)
            return 1
        print(
            f"{frozen_path} matches the served application's OpenAPI document.",
            flush=True,
        )
        return 0
    regenerate_frozen_document(frozen_path)
    print(f"Wrote {frozen_path}.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
