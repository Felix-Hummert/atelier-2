"""The canonical durable state a scenario runtime is given under one root.

A V3 scenario keeps two stores under its temporary root: the runtime's own
SQLite file next to the agent scratch directory, and the loopback effect
adapter's external SQLite file. Both spellings were written out per file until
this owner existed.

Keep these four imports. This module is deliberately a leaf: importing
`tests.scenarios.runtime`, the owner of `recording_exact_runtime`, costs a
spawned child process 372 ms more than the `atelier2.adapters.dbos.runtime`
import it already pays (1148 ms against 776 ms, measured for #989), because
that module reaches through `tests.scenarios.runs` and `tests.scenarios.api`
into the whole served FastAPI application for three constants. In-process under
pytest that is free after the first import; a self-spawning crash harness pays
it per child. Nothing here needs it, so nothing here imports it.

The agent scratch root is a required argument rather than something these
helpers derive, because creating it writes to the filesystem and that belongs
at the call site. `application_version` has no honest shared default either:
DBOS scopes workflow recovery to the exact version that enqueued a workflow
(`adapters/dbos/uncontinuable_runs.py:6-8`, `adapters/dbos/effect_store.py:994`),
so a scenario that reopens its own durable root more than once -- a restart, a
self-spawning crash-harness child -- must pass back the identical literal each
time or DBOS finds nothing live to recover under the new one
(`tests/crash/test_durable_run_restart.py`, where a run crashed under
`"executor-A"` and resumed under `"executor-B"` leaves the bootstrap workflow
stuck PENDING). Whether and what to repeat is each scenario's own restart
shape to decide, not something this owner could pick for it
(`tests/crash/test_durable_run_restart.py:196`,
`test_executor_version_is_explicit_test_configuration`).
"""

from __future__ import annotations

from pathlib import Path

from atelier2.adapters.dbos.runtime import DbosRuntimeSettings
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.effects import AdapterRevision, EffectDestination


def canonical_loopback_effects(root: Path) -> LoopbackEffectAdapterFactory:
    """The loopback effects a scenario performs into its own external store.

    A scenario that needs its effects to land somewhere distinguishable names
    its own revision and destination instead of calling this.
    """
    return LoopbackEffectAdapterFactory(
        root / "external.sqlite",
        AdapterRevision("loopback-v1"),
        EffectDestination("loopback-test"),
    )


def canonical_runtime_settings(
    root: Path, application_version: str, scratch_root: Path
) -> DbosRuntimeSettings:
    """Settings for a runtime whose durable state lives under ``root``.

    A scenario whose subject is a setting itself -- a differing lock timeout, a
    Runner lease, a store path it opens directly -- states that setting rather
    than hiding it behind this owner.
    """
    return DbosRuntimeSettings(
        root / "atelier.sqlite",
        application_version,
        agent_scratch_root=scratch_root,
    )
