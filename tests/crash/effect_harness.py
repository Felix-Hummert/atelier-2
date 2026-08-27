from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import sqlalchemy as sa

from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents
from atelier2.adapters.dbos.workflow_ids import EFFECT_WORKFLOW_ID_PREFIX
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    EffectUnknownOutcome,
    OperatorAuthoritativeAbsence,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.effects import EffectAdapter
from tests.scenarios.runs import (
    start_published_v1_run,
    submit_reconcile_command,
)

CRASHED = 86
ADAPTER_EXECUTE_AFTER_COMMIT = "adapter/execute-after-commit"
AFTER_EXTERNAL_COMMIT = "after-external-commit"


class HarnessEffectAdapter:
    def __init__(
        self,
        delegate: EffectAdapter,
        force_unknown_marker: Path,
        after_execute_crash_marker: Path | None,
    ) -> None:
        self._delegate = delegate
        self._force_unknown_marker = force_unknown_marker
        self._after_execute_crash_marker = after_execute_crash_marker

    def readback(self, intent: EffectIntent) -> EffectReadback:
        if self._force_unknown_marker.exists():
            return EffectUnknownOutcome(intent.reference)
        return self._delegate.readback(intent)

    def execute(self, intent: EffectIntent) -> PerformedEffect | EffectUnknownOutcome:
        performed = self._delegate.execute(intent)
        if self._after_execute_crash_marker is not None:
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
        self,
        database: Path,
        force_unknown_marker: Path,
        after_execute_crash_marker: Path | None = None,
    ) -> None:
        self._delegate = LoopbackEffectAdapterFactory(
            database,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-crash-test"),
        )
        self._force_unknown_marker = force_unknown_marker
        self._after_execute_crash_marker = after_execute_crash_marker

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    @property
    def proves_absence(self) -> bool:
        return self._delegate.proves_absence

    def open(self) -> HarnessEffectAdapter:
        return HarnessEffectAdapter(
            self._delegate.open(),
            self._force_unknown_marker,
            self._after_execute_crash_marker,
        )


def runtime(
    database: Path,
    external: Path,
    application_version: str,
    force_unknown_marker: Path,
    after_execute_crash_marker: Path | None = None,
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(database, application_version),
        HarnessEffectAdapterFactory(
            external, force_unknown_marker, after_execute_crash_marker
        ),
        ExactOutputAgentExecutorFactory(),
    )


def load_intent(runtime_lease: DbosRuntime, run_id: str) -> EffectIntent:
    with runtime_lease.engine.connect() as connection:
        record = (
            connection.execute(
                sa.select(effect_intents).where(effect_intents.c.run_id == run_id)
            )
            .mappings()
            .one()
        )
    return intent_snapshot_from_record(record).intent


def initialize(
    database: Path, external: Path, version: str, unknown_marker: Path
) -> None:
    lease = runtime(database, external, version, unknown_marker)
    try:
        lease.initialize_storage()
    finally:
        lease.close()


def seed(
    database: Path,
    external: Path,
    version: str,
    unknown_marker: Path,
    run_id: str,
    document: bytes,
) -> None:
    lease = runtime(database, external, version, unknown_marker)
    try:
        lease.initialize_storage()
        start_published_v1_run(
            lease.engine, lease.settings, RunId(run_id), WorkflowRevision(document)
        )
    finally:
        lease.close()


def _crash_once(marker: Path, operation_name: str, timing: str) -> None:
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.write(descriptor, f"{operation_name}:{timing}".encode())
    os.close(descriptor)
    os._exit(CRASHED)


class EffectEnqueueRefused(RuntimeError):
    """What an Action node workflow raises when its enqueue never lands."""


def install_enqueue_refusal() -> None:
    """Refuse the enqueue of `durable_effect`, after its intent is committed.

    An Action node workflow commits its intent in one transaction step and
    enqueues the effect workflow in the next. Refusing that enqueue ends the
    node workflow in a terminal error status with the intent already PREPARED
    and no effect-workflow row anywhere -- the exact state a node workflow that
    dies terminally between the two leaves behind (#646).
    """

    from dbos._sys_db import (
        SystemDatabase,
        WorkflowStatuses,
        WorkflowStatusInternal,
    )

    original = SystemDatabase.init_workflow

    def refusing(
        self: SystemDatabase,
        status: WorkflowStatusInternal,
        *,
        max_recovery_attempts: int | None,
        owner_xid: str | None,
        is_recovery_request: bool | None,
        is_dequeued_request: bool | None,
    ) -> tuple[WorkflowStatuses, int | None, bool]:
        if status["workflow_uuid"].startswith(EFFECT_WORKFLOW_ID_PREFIX):
            raise EffectEnqueueRefused("the effect workflow never reached the queue")
        return original(
            self,
            status,
            max_recovery_attempts=max_recovery_attempts,
            owner_xid=owner_xid,
            is_recovery_request=is_recovery_request,
            is_dequeued_request=is_dequeued_request,
        )

    SystemDatabase.init_workflow = refusing


def install_crash(marker: Path, operation_name: str, timing: str) -> None:
    from dbos._sys_db import OperationResultInternal, SystemDatabase

    original = SystemDatabase.record_operation_result

    def injected(
        self: SystemDatabase,
        result: OperationResultInternal,
        *,
        completed_at_epoch_ms: int | None = None,
    ) -> None:
        if result.get("function_name") != operation_name:
            original(self, result, completed_at_epoch_ms=completed_at_epoch_ms)
            return
        if timing == "after-record":
            original(self, result, completed_at_epoch_ms=completed_at_epoch_ms)
            _crash_once(marker, operation_name, timing)
            return
        _crash_once(marker, operation_name, timing)
        original(self, result, completed_at_epoch_ms=completed_at_epoch_ms)

    SystemDatabase.record_operation_result = injected


def execute(
    database: Path,
    external: Path,
    version: str,
    unknown_marker: Path,
    run_id: str,
    crash_marker: Path | None,
    operation_name: str,
    timing: str,
    wait_seconds: float,
) -> None:
    adapter_crash = operation_name == ADAPTER_EXECUTE_AFTER_COMMIT
    if adapter_crash and timing != AFTER_EXTERNAL_COMMIT:
        raise ValueError("adapter execute crash requires after-external-commit timing")
    lease = runtime(
        database,
        external,
        version,
        unknown_marker,
        crash_marker if adapter_crash else None,
    )
    try:
        if crash_marker is not None and not adapter_crash:
            install_crash(crash_marker, operation_name, timing)
        lease.launch()
        deadline = time.monotonic() + wait_seconds
        statuses: tuple[str, ...] = ()
        while time.monotonic() < deadline:
            statuses = workflow_statuses(database, version)
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


def workflow_statuses(database: Path, version: str) -> tuple[str, ...]:
    with sqlite3.connect(database, timeout=30) as connection:
        return tuple(
            row[0]
            for row in connection.execute(
                "SELECT status FROM workflow_status WHERE application_version=?",
                (version,),
            )
        )


def strand(
    database: Path,
    external: Path,
    version: str,
    unknown_marker: Path,
    wait_seconds: float,
) -> None:
    """Leave the #646 window behind: a PREPARED intent whose enqueue never was.

    The Action node workflow ends in a terminal error status, so this waits for
    that ending rather than for the run to finish.
    """

    lease = runtime(database, external, version, unknown_marker)
    try:
        install_enqueue_refusal()
        lease.launch()
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if "ERROR" in workflow_statuses(database, version):
                return
            time.sleep(0.025)
        raise TimeoutError(
            f"the refused enqueue did not end its node workflow within {wait_seconds}s"
        )
    finally:
        lease.close()


def converge(
    database: Path, external: Path, version: str, unknown_marker: Path
) -> None:
    """Start the runtime once; serve-start convergence runs inside the launch."""

    lease = runtime(database, external, version, unknown_marker)
    try:
        lease.launch()
    finally:
        lease.close()


def submit_absence(
    database: Path,
    external: Path,
    version: str,
    unknown_marker: Path,
    run_id: str,
) -> None:
    lease = runtime(database, external, version, unknown_marker)
    try:
        intent = load_intent(lease, run_id)
        with lease.engine.connect() as connection:
            snapshot = intent_snapshot_from_record(
                connection.execute(
                    sa.select(effect_intents).where(effect_intents.c.run_id == run_id)
                )
                .mappings()
                .one()
            )
        command = ReconcileCommand(
            ReconcileCommandId(f"{run_id}/reconcile-1"),
            intent.reference,
            snapshot.state_version,
            ReconcileActor("operator"),
            "inspected the exact external destination",
            OperatorAuthoritativeAbsence(),
        )
        submit_reconcile_command(lease.engine, lease.settings, command)
    finally:
        lease.close()


def main() -> None:
    (
        command,
        raw_database,
        raw_external,
        version,
        raw_unknown_marker,
        *arguments,
    ) = sys.argv[1:]
    database = Path(raw_database)
    external = Path(raw_external)
    unknown_marker = Path(raw_unknown_marker)
    if command == "initialize":
        initialize(database, external, version, unknown_marker)
    elif command == "seed":
        run_id, document_hex = arguments
        seed(
            database,
            external,
            version,
            unknown_marker,
            run_id,
            bytes.fromhex(document_hex),
        )
    elif command == "submit-absence":
        (run_id,) = arguments
        submit_absence(database, external, version, unknown_marker, run_id)
    elif command == "strand":
        (raw_wait,) = arguments
        strand(database, external, version, unknown_marker, float(raw_wait))
    elif command == "converge":
        converge(database, external, version, unknown_marker)
    else:
        run_id, raw_marker, operation_name, timing, raw_wait = arguments
        execute(
            database,
            external,
            version,
            unknown_marker,
            run_id,
            None if raw_marker == "NONE" else Path(raw_marker),
            operation_name,
            timing,
            float(raw_wait),
        )
    print(json.dumps({"command": command, "ok": True}))


if __name__ == "__main__":
    main()
