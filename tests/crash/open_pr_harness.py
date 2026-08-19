"""Crash harness for the GitHub open-pr adapter, twin of effect_harness."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    PerformedEffect,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.effects import EffectAdapter
from tests.crash.effect_harness import (
    ADAPTER_EXECUTE_AFTER_COMMIT,
    AFTER_EXTERNAL_COMMIT,
    install_crash,
)
from tests.scenarios.runs import start_published_v1_run

CANARY_TOKEN = "gho_atelier2_canary_token_must_not_appear"


class HarnessEffectAdapter:
    def __init__(
        self,
        delegate: EffectAdapter,
        after_execute_crash_marker: Path | None,
    ) -> None:
        self._delegate = delegate
        self._after_execute_crash_marker = after_execute_crash_marker

    def readback(self, intent: EffectIntent) -> EffectReadback:
        return self._delegate.readback(intent)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        performed = self._delegate.execute(intent)
        if self._after_execute_crash_marker is not None:
            from tests.crash.effect_harness import _crash_once

            _crash_once(
                self._after_execute_crash_marker,
                ADAPTER_EXECUTE_AFTER_COMMIT,
                AFTER_EXTERNAL_COMMIT,
            )
        return performed

    def close(self) -> None:
        self._delegate.close()


class HarnessEffectAdapterFactory:
    def __init__(
        self, database: Path, after_execute_crash_marker: Path | None = None
    ) -> None:
        self._delegate = GitHubEffectAdapterFactory(
            database,
            AdapterRevision("github-open-pr-v1"),
            EffectDestination("platform"),
        )
        self._after_execute_crash_marker = after_execute_crash_marker

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    def open(self) -> HarnessEffectAdapter:
        return HarnessEffectAdapter(
            self._delegate.open(), self._after_execute_crash_marker
        )


def runtime(
    database: Path,
    external: Path,
    application_version: str,
    after_execute_crash_marker: Path | None = None,
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(database, application_version),
        HarnessEffectAdapterFactory(external, after_execute_crash_marker),
        ExactOutputAgentExecutorFactory(),
    )


def initialize(database: Path, external: Path, version: str) -> None:
    lease = runtime(database, external, version)
    try:
        lease.initialize_storage()
    finally:
        lease.close()


def seed(
    database: Path,
    external: Path,
    version: str,
    run_id: str,
    document: bytes,
) -> None:
    lease = runtime(database, external, version)
    try:
        lease.initialize_storage()
        start_published_v1_run(
            lease.engine, lease.settings, RunId(run_id), WorkflowRevision(document)
        )
    finally:
        lease.close()


def execute(
    database: Path,
    external: Path,
    version: str,
    run_id: str,
    crash_marker: Path | None,
    operation_name: str,
    timing: str,
    wait_seconds: float,
) -> None:
    adapter_crash = operation_name == ADAPTER_EXECUTE_AFTER_COMMIT
    lease = runtime(
        database,
        external,
        version,
        crash_marker if adapter_crash else None,
    )
    try:
        if crash_marker is not None and not adapter_crash:
            install_crash(crash_marker, operation_name, timing)
        lease.launch()
        deadline = time.monotonic() + wait_seconds
        statuses: tuple[str, ...] = ()
        while time.monotonic() < deadline:
            with sqlite3.connect(database, timeout=30) as connection:
                statuses = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT status FROM workflow_status WHERE application_version=?",
                        (version,),
                    )
                )
            failed = set(statuses) & {"ERROR", "CANCELLED"}
            if failed:
                raise RuntimeError(f"durable workflow failed with {sorted(failed)!r}")
            if statuses and set(statuses) == {"SUCCESS"}:
                break
            time.sleep(0.025)
        else:
            raise TimeoutError(
                f"durable workflows did not finish within {wait_seconds}s: {statuses!r}"
            )
    finally:
        lease.close()


def main() -> None:
    command, raw_database, raw_external, version, *arguments = sys.argv[1:]
    database = Path(raw_database)
    external = Path(raw_external)
    if command == "initialize":
        initialize(database, external, version)
    elif command == "seed":
        run_id, document_hex = arguments
        seed(database, external, version, run_id, bytes.fromhex(document_hex))
    else:
        run_id, raw_marker, operation_name, timing, raw_wait = arguments
        execute(
            database,
            external,
            version,
            run_id,
            None if raw_marker == "NONE" else Path(raw_marker),
            operation_name,
            timing,
            float(raw_wait),
        )
    print(json.dumps({"command": command, "ok": True}))


if __name__ == "__main__":
    main()
