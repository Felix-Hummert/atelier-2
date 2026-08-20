"""The host reads project id → root path from the live-versioned channel."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.host_configuration import (
    DbosHostConfigurationChannel,
    append_project_root,
    latest_occupancy_revision,
    latest_project_root_revision,
    project_root_for,
    publish_occupancy_revision,
    publish_project_root_revision,
)
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings, create_canonical_engine
from atelier2.adapters.dbos.schema import (
    host_occupancy_bindings,
    host_occupancy_revisions,
    host_project_root_revisions,
    initialize_schema,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.app import create_app
from atelier2.api.openapi import PROJECT_PATH, PROJECTS_PATH
from atelier2.contracts.agents import AgentConfigurationRevisionHash, AgentRole
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import (
    HOST_CONFIGURATION_UNREADABLE,
    OCCUPANCY_REVISION_CONFLICT,
    PROJECT_ROOT_MISSING,
    PROJECT_UNKNOWN,
    HostConfigurationUnreadable,
    OccupancyBinding,
    OccupancyRevision,
    OccupancyRevisionConflict,
    ProjectId,
    ProjectRootBytesDisagree,
    ProjectRootMissing,
    ProjectRootRevision,
    ProjectUnknown,
)
from atelier2.host import main
from atelier2.host.serving import compose_application
from tests.host.test_local_host import serve_arguments, served_settings
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff
from tests.scenarios.projects import declaring_verification, git_project
from tests.scenarios.runtime import exact_output_runtime


def opened_channel(tmp_path: Path) -> Engine:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    return engine


def project_http_client(engine: Engine, project_id: ProjectId) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                host_configuration_channel=DbosHostConfigurationChannel(engine)
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
            served_project_id=project_id,
        )
    )


def test_a_written_project_root_is_read_back(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    engine = opened_channel(tmp_path)
    revision = ProjectRootRevision(ProjectId("studio"), 1, root)

    try:
        stored = publish_project_root_revision(engine, revision)

        assert stored == revision
        assert project_root_for(engine, ProjectId("studio")) == root.resolve()
    finally:
        engine.dispose()


def test_the_latest_revision_is_the_mapping_the_runtime_reads(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    engine = opened_channel(tmp_path)
    project = ProjectId("studio")

    try:
        publish_project_root_revision(engine, ProjectRootRevision(project, 1, first))
        publish_project_root_revision(engine, ProjectRootRevision(project, 2, second))

        assert project_root_for(engine, project) == second.resolve()
        assert latest_project_root_revision(engine, project) == ProjectRootRevision(
            project, 2, second
        )
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(host_project_root_revisions)
                )
                == 2
            )
    finally:
        engine.dispose()


def test_a_missing_root_is_refused_by_name(tmp_path: Path) -> None:
    engine = opened_channel(tmp_path)

    try:
        with pytest.raises(ProjectRootMissing, match=PROJECT_ROOT_MISSING):
            project_root_for(engine, ProjectId("unknown"))
    finally:
        engine.dispose()


def test_a_bad_project_id_is_refused_before_the_channel_is_asked() -> None:
    with pytest.raises(ProjectUnknown, match=PROJECT_UNKNOWN):
        ProjectId("")


def test_an_unreadable_channel_is_refused_by_name(tmp_path: Path) -> None:
    engine = create_canonical_engine(tmp_path / "not-a-store.sqlite")

    try:
        with pytest.raises(
            HostConfigurationUnreadable, match=HOST_CONFIGURATION_UNREADABLE
        ):
            project_root_for(engine, ProjectId("studio"))
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "rewrite",
    [
        pytest.param(
            host_project_root_revisions.update().values(root_path="/tampered"),
            id="update",
        ),
        pytest.param(host_project_root_revisions.delete(), id="delete"),
    ],
)
def test_a_project_root_revision_can_no_longer_be_rewritten(
    tmp_path: Path, rewrite: sa.Executable
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    engine = opened_channel(tmp_path)
    publish_project_root_revision(
        engine, ProjectRootRevision(ProjectId("studio"), 1, root)
    )

    try:
        with (
            pytest.raises(
                IntegrityError, match="host project-root revisions are immutable"
            ),
            engine.begin() as connection,
        ):
            connection.execute(rewrite)
    finally:
        engine.dispose()


def test_the_runtime_reads_the_mapping_from_the_channel(tmp_path: Path) -> None:
    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    append_project_root(engine, ProjectId("studio"), root)
    engine.dispose()

    runtime = exact_output_runtime(
        DbosRuntimeSettings(
            database, "host-config-read", project_id=ProjectId("studio")
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("host-config-read"),
        ),
    )
    try:
        assert project_root_for(runtime.engine, ProjectId("studio")) == root.resolve()
        assert runtime.declared_project is not None
        assert runtime.declared_project.source.head() == pin
    finally:
        runtime.close()


def test_the_runtime_refuses_a_project_whose_root_is_not_in_the_channel(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProjectUnknown, match=PROJECT_UNKNOWN):
        exact_output_runtime(
            DbosRuntimeSettings(
                tmp_path / "atelier.sqlite",
                "host-config-missing",
                project_id=ProjectId("studio"),
            ),
            LoopbackEffectAdapterFactory(
                tmp_path / "effects.sqlite",
                AdapterRevision("loopback-v1"),
                EffectDestination("host-config-missing"),
            ),
        )


def test_bootstrap_flags_write_the_channel_and_the_runtime_reads_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    settings = served_settings(
        tmp_path, project_id=ProjectId("studio"), project_root=root
    )

    _app, runtime = compose_application(settings)
    try:
        assert project_root_for(runtime.engine, ProjectId("studio")) == root.resolve()
        assert runtime.declared_project is not None
        assert runtime.declared_project.source.head() == pin
    finally:
        runtime.close()


def test_compose_serves_the_configured_project_through_its_delivered_reference(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    git_project(root, declaring_verification(["/bin/true"]))
    app, runtime = compose_application(
        served_settings(tmp_path, project_id=ProjectId("studio"), project_root=root)
    )

    try:
        client = TestClient(app)
        listed = client.get(PROJECTS_PATH)
        (project,) = listed.json()["items"]
        detailed = client.get(
            PROJECT_PATH.format(
                public_project_reference=project["public_project_reference"]
            )
        )

        assert listed.status_code == 200
        assert detailed.status_code == 200
        assert detailed.json() == project
    finally:
        runtime.close()


def test_tampered_project_root_bytes_are_durable_state_corrupt_over_http(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    engine = opened_channel(tmp_path)
    project_id = ProjectId("studio")
    publish_project_root_revision(engine, ProjectRootRevision(project_id, 1, root))
    with engine.begin() as connection:
        connection.execute(
            sa.text("DROP TRIGGER host_project_root_revisions_no_update")
        )
        connection.execute(
            sa.text("UPDATE host_project_root_revisions SET root_path = '/tampered'")
        )

    try:
        with pytest.raises(ProjectRootBytesDisagree):
            latest_project_root_revision(engine, project_id)
        response = project_http_client(engine, project_id).get(PROJECTS_PATH)

        assert response.status_code == 500
        assert response.json()["type"].endswith("durable-state-corrupt")
    finally:
        engine.dispose()


def test_a_real_unreadable_project_channel_stays_temporarily_unavailable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    engine = opened_channel(tmp_path)
    engine.dispose()
    database.rename(tmp_path / "unavailable.sqlite")
    database.mkdir()

    try:
        response = project_http_client(engine, ProjectId("studio")).get(PROJECTS_PATH)

        assert response.status_code == 503
        assert response.json()["type"].endswith("temporarily-unavailable")
    finally:
        engine.dispose()


def test_compose_reads_the_pinned_project_from_the_channel_without_a_second_flag(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    pin = git_project(root, declaring_verification(["/bin/true"]))
    engine = create_canonical_engine(tmp_path / "durable.sqlite")
    initialize_schema(engine)
    append_project_root(engine, ProjectId("studio"), root)
    engine.dispose()

    _app, runtime = compose_application(
        served_settings(tmp_path, project_id=ProjectId("studio"))
    )
    try:
        assert runtime.declared_project is not None
        assert runtime.declared_project.source.head() == pin
    finally:
        runtime.close()


def test_compose_refuses_an_unknown_project_by_name(tmp_path: Path) -> None:
    with pytest.raises(ProjectUnknown, match=PROJECT_UNKNOWN):
        compose_application(served_settings(tmp_path, project_id=ProjectId("studio")))


def test_a_project_root_flag_without_a_project_id_refuses_to_serve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    git_project(root, declaring_verification(["/bin/true"]))

    with pytest.raises(SystemExit) as refusal:
        main(serve_arguments(tmp_path, "--project-root", str(root)))

    assert refusal.value.code == 2
    assert "--project-id" in capsys.readouterr().err


def _occupancy(
    *,
    project: str = "studio",
    lineage: str = "ab" * 32,
    revision_number: int = 1,
    bindings: tuple[OccupancyBinding, ...] | None = None,
) -> OccupancyRevision:
    if bindings is None:
        bindings = (
            OccupancyBinding(
                AgentRole("chef"), AgentConfigurationRevisionHash("cd" * 32)
            ),
        )
    return OccupancyRevision(
        ProjectId(project),
        CatalogLineageId(lineage),
        revision_number,
        bindings,
    )


def test_a_written_occupancy_revision_is_read_back(tmp_path: Path) -> None:
    engine = opened_channel(tmp_path)
    revision = _occupancy()

    try:
        stored = publish_occupancy_revision(engine, revision)

        assert stored == revision
        assert (
            latest_occupancy_revision(engine, revision.project_id, revision.lineage_id)
            == revision
        )
    finally:
        engine.dispose()


def test_the_latest_occupancy_revision_is_what_a_read_returns(tmp_path: Path) -> None:
    engine = opened_channel(tmp_path)
    first = _occupancy()
    later = _occupancy(
        revision_number=2,
        bindings=(
            OccupancyBinding(
                AgentRole("chef"), AgentConfigurationRevisionHash("ee" * 32)
            ),
        ),
    )

    try:
        publish_occupancy_revision(engine, first)
        publish_occupancy_revision(engine, later)

        assert (
            latest_occupancy_revision(engine, first.project_id, first.lineage_id)
            == later
        )
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(host_occupancy_revisions)
                )
                == 2
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(host_occupancy_bindings)
                )
                == 2
            )
    finally:
        engine.dispose()


def test_the_same_occupancy_bytes_at_the_same_key_are_idempotent(
    tmp_path: Path,
) -> None:
    engine = opened_channel(tmp_path)
    revision = _occupancy()

    try:
        first = publish_occupancy_revision(engine, revision)
        second = publish_occupancy_revision(engine, revision)

        assert first == second == revision
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(host_occupancy_revisions)
                )
                == 1
            )
    finally:
        engine.dispose()


def test_a_different_occupancy_at_the_same_key_conflicts(tmp_path: Path) -> None:
    engine = opened_channel(tmp_path)
    first = _occupancy()
    other = _occupancy(
        bindings=(
            OccupancyBinding(
                AgentRole("chef"), AgentConfigurationRevisionHash("ee" * 32)
            ),
        ),
    )

    try:
        publish_occupancy_revision(engine, first)
        with pytest.raises(
            OccupancyRevisionConflict, match=OCCUPANCY_REVISION_CONFLICT
        ):
            publish_occupancy_revision(engine, other)
    finally:
        engine.dispose()


def test_a_missing_occupancy_is_none(tmp_path: Path) -> None:
    engine = opened_channel(tmp_path)

    try:
        assert (
            latest_occupancy_revision(
                engine, ProjectId("studio"), CatalogLineageId("ab" * 32)
            )
            is None
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "rewrite",
    [
        pytest.param(
            host_occupancy_revisions.update().values(project_id="tampered"),
            id="header-update",
        ),
        pytest.param(host_occupancy_revisions.delete(), id="header-delete"),
        pytest.param(
            host_occupancy_bindings.update().values(role="tampered"),
            id="binding-update",
        ),
        pytest.param(host_occupancy_bindings.delete(), id="binding-delete"),
    ],
)
def test_an_occupancy_revision_can_no_longer_be_rewritten(
    tmp_path: Path, rewrite: sa.Executable
) -> None:
    engine = opened_channel(tmp_path)
    publish_occupancy_revision(engine, _occupancy())

    try:
        with (
            pytest.raises(IntegrityError, match="host occupancy"),
            engine.begin() as connection,
        ):
            connection.execute(rewrite)
    finally:
        engine.dispose()
