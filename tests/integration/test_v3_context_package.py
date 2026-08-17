"""The manifest a V3 receipt names is a record, not a number nobody can read.

ADR 0006 binds `context-package/v3` to material written **once, immutably, before
START**, and a `node-receipt/v3` carries that manifest's hash. Until this head the
hash was all there was: the supervised start stored `context_package_hash` on the
receipt and the manifest bytes went nowhere, so nothing could answer what the
node was actually given. A receipt whose package cannot be read is a promise
about material that may never have existed.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.runtime import DbosRuntimeSettings, create_canonical_engine
from atelier2.adapters.dbos.schema import (
    context_packages_v3,
    initialize_schema,
    node_execution_requests_v3,
    node_receipts_v3,
    run_configuration_revisions,
    runs,
)
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.contracts.node_records_v3 import (
    ContextPackage,
    NodeExecutionRequestHash,
)
from atelier2.contracts.run_configuration_v3 import (
    RunConfigurationRevision,
    RunConfigurationRevisionHash,
)
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.durable_runs import (
    DurableV3RunCreated,
    DurableV3StartBindingInvalid,
)
from tests.integration.test_v3_atomic_start import request


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[tuple[Engine, DbosDurableRunStarter]]:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    try:
        yield (
            engine,
            DbosDurableRunStarter(
                engine,
                DbosRuntimeSettings(database, "context-package-test"),
                AgentExecutorRegistry(),
            ),
        )
    finally:
        engine.dispose()


@pytest.mark.proves("a-supervised-v3-start-leaves-the-manifest-its-receipt-names")
def test_a_supervised_v3_start_leaves_the_manifest_its_receipt_names(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    """The receipt's package hash reaches the exact bytes that produced it."""
    engine, starter = storage

    started = starter.start_v3_with_receipt(request())

    assert isinstance(started, DurableV3RunCreated)
    with engine.connect() as connection:
        stored_receipt = (
            connection.execute(sa.select(node_receipts_v3)).mappings().one()
        )
        manifest = connection.scalar(
            sa.select(context_packages_v3.c.manifest).where(
                context_packages_v3.c.package_hash
                == stored_receipt["context_package_hash"]
            )
        )

    assert manifest is not None
    assert (
        ContextPackage(bytes(manifest)).package_hash.value
        == stored_receipt["context_package_hash"]
    )


@pytest.mark.proves("a-supervised-v3-start-leaves-the-manifest-its-receipt-names")
def test_the_stored_manifest_can_never_be_changed_or_removed(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    """ADR 0006's "written once, immutably" is a trigger, not a habit."""
    engine, starter = storage
    started = starter.start_v3_with_receipt(request())
    assert isinstance(started, DurableV3RunCreated)

    for forbidden in (
        context_packages_v3.update().values(manifest=b"another package"),
        context_packages_v3.delete(),
    ):
        with (
            engine.begin() as connection,
            pytest.raises(IntegrityError, match="context packages are immutable"),
        ):
            connection.execute(forbidden)


@pytest.mark.proves(
    "a-supervised-v3-run-records-the-configuration-it-was-started-under"
)
def test_a_supervised_v3_run_reads_back_carrying_its_run_configuration(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    """`RunV3` stops at the format number no longer: the snapshot id is durable."""
    engine, starter = storage
    started = request()
    assert isinstance(starter.start_v3_with_receipt(started), DurableV3RunCreated)

    with engine.connect() as connection:
        stored = (
            connection.execute(
                sa.select(runs).where(
                    runs.c.run_id == started.node_request.run_id.value
                )
            )
            .mappings()
            .one()
        )

    assert (
        stored["run_configuration_revision_hash"]
        == started.node_request.run_configuration_revision_hash.value
    )


@pytest.mark.proves("a-supervised-v3-start-leaves-the-manifest-its-receipt-names")
def test_a_truth_naming_a_package_it_does_not_carry_is_refused_without_a_write(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    """The manifest travels with the truth that names it, or nothing is written."""
    engine, starter = storage
    decided = request()

    refused = starter.start_v3_with_receipt(
        replace(decided, context_package=ContextPackage(b"a package nobody assembled"))
    )

    assert isinstance(refused, DurableV3StartBindingInvalid)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(context_packages_v3)
            )
            == 0
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


@pytest.mark.proves("a-v3-node-execution-request-is-composed-not-supplied")
def test_the_stored_records_recompute_the_hashes_the_receipt_carries(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    """A reader can walk from the receipt to the exact bytes, trusting nothing.

    The receipt names a request hash and a package hash; the request names the
    run configuration. Each of those is stored as the preimage its identity was
    taken over, so recomputing it from the store must give the same name back --
    that is the difference between a record and a number.
    """
    engine, starter = storage
    decided = request()
    assert isinstance(starter.start_v3_with_receipt(decided), DurableV3RunCreated)

    with engine.connect() as connection:
        receipt = connection.execute(sa.select(node_receipts_v3)).mappings().one()
        stored_request = connection.scalar(
            sa.select(node_execution_requests_v3.c.preimage).where(
                node_execution_requests_v3.c.request_hash == receipt["request_hash"]
            )
        )
        stored_configuration = connection.scalar(
            sa.select(run_configuration_revisions.c.preimage).where(
                run_configuration_revisions.c.revision_hash
                == decided.run_configuration.revision_hash.value
            )
        )

    assert stored_request is not None and stored_configuration is not None
    assert (
        NodeExecutionRequestHash.of(bytes(stored_request)).value
        == receipt["request_hash"]
    )
    assert (
        RunConfigurationRevisionHash.of(bytes(stored_configuration))
        == decided.run_configuration.revision_hash
    )


@pytest.mark.proves(
    "a-supervised-v3-run-records-the-configuration-it-was-started-under"
)
def test_a_truth_naming_a_configuration_it_does_not_carry_is_refused(
    storage: tuple[Engine, DbosDurableRunStarter],
) -> None:
    """Every record the truth names travels with it, or nothing is written."""
    engine, starter = storage
    decided = request()
    other = RunConfigurationRevision(
        decided.run_configuration.workflow_revision_hash,
        decided.run_configuration.binding_set_hash,
        (),
    )

    refused = starter.start_v3_with_receipt(replace(decided, run_configuration=other))

    assert isinstance(refused, DurableV3StartBindingInvalid)
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(run_configuration_revisions)
            )
            == 0
        )
