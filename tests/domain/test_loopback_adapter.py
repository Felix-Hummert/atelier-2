from __future__ import annotations

import os
from pathlib import Path

import pytest

from atelier2.adapters.loopback import (
    LoopbackEffectAdapter,
    LoopbackEffectAdapterFactory,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectIntentMismatch,
    EffectReceipt,
    LogicalEffectKey,
)
from atelier2.contracts.runs import RunId, WorkflowRevision

ADAPTER_REVISION = AdapterRevision("loopback-adapter-1")
DESTINATION = EffectDestination("loopback://effects")
LOGICAL_KEY = LogicalEffectKey("run-1/publish-pull-request")


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "loopback.sqlite"


@pytest.fixture
def adapter(database_path: Path) -> LoopbackEffectAdapter:
    return LoopbackEffectAdapterFactory(
        database_path, ADAPTER_REVISION, DESTINATION
    ).open()


def effect_intent(database_path: Path, payload: bytes) -> EffectIntent:
    return EffectIntent(
        EffectBinding(
            logical_key=LOGICAL_KEY,
            run_id=RunId("run-1"),
            workflow_revision_hash=WorkflowRevision(b"workflow-v1").revision_hash,
            adapter_revision=ADAPTER_REVISION,
            destination=DESTINATION,
            adapter_operational_identity=AdapterOperationalIdentity(
                str(database_path.resolve())
            ),
        ),
        CanonicalRequest(payload),
    )


def open_database_descriptors(database_path: Path) -> int:
    """Count this process's still-open handles on the loopback database.

    Reads /proc because SQLite exposes no handle inventory; a descriptor that
    disappears while the directory is walked is by definition no longer open.
    """

    prefix = str(database_path.resolve())
    open_descriptors = 0
    for entry in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{entry}")
        except OSError:
            continue
        if target.startswith(prefix):
            open_descriptors += 1
    return open_descriptors


def test_a_refused_execute_leaves_no_open_database_handle(
    adapter: LoopbackEffectAdapter, database_path: Path
) -> None:
    adapter.execute(effect_intent(database_path, b'{"title":"the first request"}'))

    with pytest.raises(EffectIntentMismatch) as refusal:
        adapter.execute(effect_intent(database_path, b'{"title":"another request"}'))

    assert refusal.traceback
    assert open_database_descriptors(database_path) == 0


def test_a_served_readback_leaves_no_open_database_handle(
    adapter: LoopbackEffectAdapter, database_path: Path
) -> None:
    intent = effect_intent(database_path, b'{"title":"the only request"}')
    performed = adapter.execute(intent)

    receipt = adapter.readback(intent)

    assert isinstance(receipt, EffectReceipt)
    assert receipt.effect_id == performed.effect_id
    assert open_database_descriptors(database_path) == 0
