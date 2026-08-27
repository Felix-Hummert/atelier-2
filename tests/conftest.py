from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

PROOF_MARKER = "proves"


@pytest.fixture
def dbos_logging_isolation() -> Iterator[None]:
    """Keep DBOS from flushing capture handlers another test has closed."""
    root = logging.getLogger()
    inherited = root.handlers[:]
    root.handlers = []
    try:
        yield
    finally:
        root.handlers = inherited


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Carry every ``proves`` claim into the run report pytest writes.

    Collection is the earliest place a claim can be attached, so it reaches the
    report even for a test that errors in setup. The ``record_property`` fixture
    would do the same at call time, but it warns under every junit family except
    the legacy ones.
    """
    for item in items:
        for marker in item.iter_markers(name=PROOF_MARKER):
            for identifier in marker.args:
                item.user_properties.append((PROOF_MARKER, identifier))
