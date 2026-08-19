"""The host reads project id → root path from the live-versioned channel."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.host_configuration import (
    append_project_root,
    latest_project_root_revision,
    project_root_for,
    publish_project_root_revision,
)
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings, create_canonical_engine
from atelier2.adapters.dbos.schema import (
    host_project_root_revisions,
    initialize_schema,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import (
    HOST_CONFIGURATION_UNREADABLE,
    PROJECT_ROOT_MISSING,
    PROJECT_UNKNOWN,
    HostConfigurationUnreadable,
    ProjectId,
    ProjectRootMissing,
    ProjectRootRevision,
    ProjectUnknown,
)
from atelier2.host import main
from atelier2.host.serving import compose_application
from tests.host.test_local_host import serve_arguments, served_settings
from tests.scenarios.projects import declaring_verification, git_project
from tests.scenarios.runtime import exact_output_runtime


def opened_channel(tmp_path: Path):
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    return engine


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
    git_project(root, declaring_verification(["/bin/true"]))
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
    finally:
        runtime.close()


def test_the_runtime_refuses_a_project_whose_root_is_not_in_the_channel(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProjectRootMissing, match=PROJECT_ROOT_MISSING):
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
    git_project(root, declaring_verification(["/bin/true"]))
    settings = served_settings(
        tmp_path, project_id=ProjectId("studio"), project_root=root
    )

    _app, runtime = compose_application(settings)
    try:
        assert project_root_for(runtime.engine, ProjectId("studio")) == root.resolve()
    finally:
        runtime.close()


def test_a_project_root_flag_without_a_project_id_refuses_to_serve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    git_project(root, declaring_verification(["/bin/true"]))

    with pytest.raises(SystemExit) as refusal:
        main(serve_arguments(tmp_path, "--project-root", str(root)))

    assert refusal.value.code == 2
    assert "--project-id" in capsys.readouterr().err
