"""The host reads project id → root path from the live-versioned channel."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.host_configuration import (
    DbosHostConfigurationChannel,
    append_project_root,
    latest_project_root_revision,
    project_root_for,
    publish_project_root_revision,
)
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings, create_canonical_engine
from atelier2.adapters.dbos.schema import (
    agent_configuration_revisions,
    auth_profile_revisions,
    host_model_registry_entries,
    host_model_registry_revisions,
    host_project_model_defaults,
    host_project_model_defaults_revisions,
    host_project_root_revisions,
    initialize_schema,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.app import create_app
from atelier2.api.openapi import PROJECT_PATH, PROJECTS_PATH
from atelier2.application.model_configuration import (
    ProjectModelDefaultsInvalid,
    publish_project_model_defaults,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import (
    HOST_CONFIGURATION_UNREADABLE,
    PROJECT_ROOT_MISSING,
    PROJECT_UNKNOWN,
    HostConfigurationUnreadable,
    HostModelConfigurationSnapshot,
    HostProjectRootRevisionHash,
    ModelRegistryEntry,
    ModelRegistryEntrySource,
    ModelRegistryRevision,
    ProjectId,
    ProjectModelDefault,
    ProjectModelDefaultsRevision,
    ProjectRootBytesDisagree,
    ProjectRootMissing,
    ProjectRootRevision,
    ProjectRootRevisionConflict,
    ProjectUnknown,
    ProviderModelCheck,
)
from atelier2.host import main
from atelier2.host.serving import compose_application
from atelier2.ports.host_configuration import (
    ModelRegistryRevisionConflict,
    ModelRegistryRevisionCreated,
    ModelRegistryRevisionExisting,
    ProjectModelDefaultsRevisionCreated,
    ProjectModelDefaultsRevisionExisting,
    PublishProjectModelDefaultsResult,
)
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


def test_concurrent_different_project_roots_append_every_revision(
    tmp_path: Path,
) -> None:
    parallelism = 8
    roots = tuple(tmp_path / f"project-{index}" for index in range(parallelism))
    for root in roots:
        root.mkdir()
    engine = opened_channel(tmp_path)
    start_barrier = Barrier(parallelism)
    nontransactional_read_barrier = Barrier(parallelism)

    def align_nontransactional_latest_reads(
        connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        raw_connection = connection.connection.driver_connection
        if (
            "FROM host_project_root_revisions" in statement
            and not raw_connection.in_transaction
        ):
            nontransactional_read_barrier.wait(timeout=5)

    def append(root: Path) -> ProjectRootRevision:
        start_barrier.wait(timeout=5)
        return append_project_root(engine, ProjectId("studio"), root)

    event.listen(engine, "before_cursor_execute", align_nontransactional_latest_reads)
    try:
        try:
            with ThreadPoolExecutor(max_workers=parallelism) as pool:
                revisions = tuple(pool.map(append, roots))
        finally:
            event.remove(
                engine, "before_cursor_execute", align_nontransactional_latest_reads
            )
        with engine.connect() as connection:
            stored_count = connection.scalar(
                sa.select(sa.func.count()).select_from(host_project_root_revisions)
            )
    finally:
        engine.dispose()

    assert sorted(revision.revision_number for revision in revisions) == list(
        range(1, parallelism + 1)
    )
    assert {revision.root_path for revision in revisions} == set(roots)
    assert stored_count == parallelism


def test_concurrent_identical_project_roots_converge_on_one_revision(
    tmp_path: Path,
) -> None:
    parallelism = 8
    root = tmp_path / "project"
    root.mkdir()
    engine = opened_channel(tmp_path)
    start_barrier = Barrier(parallelism)

    def append(_worker: int) -> ProjectRootRevision:
        start_barrier.wait(timeout=5)
        return append_project_root(engine, ProjectId("studio"), root)

    try:
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            revisions = tuple(pool.map(append, range(parallelism)))
        with engine.connect() as connection:
            stored_count = connection.scalar(
                sa.select(sa.func.count()).select_from(host_project_root_revisions)
            )
    finally:
        engine.dispose()

    assert (
        revisions == (ProjectRootRevision(ProjectId("studio"), 1, root),) * parallelism
    )
    assert stored_count == 1


def test_a_project_root_identity_conflict_is_atomic(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    conflicting_root = tmp_path / "conflicting"
    first_root.mkdir()
    conflicting_root.mkdir()
    engine = opened_channel(tmp_path)
    project_id = ProjectId("studio")
    first = ProjectRootRevision(project_id, 1, first_root)

    try:
        publish_project_root_revision(engine, first)
        with pytest.raises(ProjectRootRevisionConflict):
            publish_project_root_revision(
                engine, ProjectRootRevision(project_id, 1, conflicting_root)
            )

        assert latest_project_root_revision(engine, project_id) == first
    finally:
        engine.dispose()


def test_a_project_root_hash_collision_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collision = HostProjectRootRevisionHash("ab" * 32)

    def collide(
        _hash_type: type[HostProjectRootRevisionHash], _payload: bytes
    ) -> HostProjectRootRevisionHash:
        return collision

    monkeypatch.setattr(HostProjectRootRevisionHash, "of", classmethod(collide))
    first_root = tmp_path / "first"
    colliding_root = tmp_path / "colliding"
    first_root.mkdir()
    colliding_root.mkdir()
    engine = opened_channel(tmp_path)
    project_id = ProjectId("studio")
    first = ProjectRootRevision(project_id, 1, first_root)

    try:
        publish_project_root_revision(engine, first)
        with pytest.raises(
            HostConfigurationUnreadable, match=HOST_CONFIGURATION_UNREADABLE
        ):
            append_project_root(engine, project_id, colliding_root)

        assert latest_project_root_revision(engine, project_id) == first
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


def _store_configuration(engine: Engine, provider: str, model: str) -> str:
    profile = AuthProfileRevision(
        f"profile/{provider}/{model}", 1, ProviderId(provider), AuthMode.API_KEY
    )
    configuration = AgentConfigurationRevision(
        model,
        profile.revision_hash,
        AgentExecutorRevision(f"executor/{provider}"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    with engine.begin() as connection:
        connection.execute(
            auth_profile_revisions.insert().values(
                revision_hash=profile.revision_hash.value,
                profile_id=profile.profile_id,
                revision_number=profile.revision_number,
                provider_id=profile.provider_id.value,
                auth_mode=profile.auth_mode.value,
            )
        )
        connection.execute(
            agent_configuration_revisions.insert().values(
                revision_hash=configuration.revision_hash.value,
                model=configuration.model,
                auth_profile_revision_hash=profile.revision_hash.value,
                executor_revision=configuration.executor_revision.value,
                revision_format_version=configuration.revision_format_version,
                requested_capability=configuration.requested_capability.value,
            )
        )
    return configuration.revision_hash.value


def _registry(
    provider: str, model: str, configuration_hash: str
) -> ModelRegistryRevision:
    return ModelRegistryRevision(
        ProviderId(provider),
        1,
        (
            ModelRegistryEntry(
                model,
                AgentConfigurationRevisionHash(configuration_hash),
                ModelRegistryEntrySource.DISCOVERED,
                ProviderModelCheck.CHECKED,
            ),
        ),
    )


def test_model_registry_and_project_defaults_round_trip_idempotently(
    tmp_path: Path,
) -> None:
    engine = opened_channel(tmp_path)
    configuration_hash = _store_configuration(engine, "openai", "gpt-5.6")
    registry = _registry("openai", "gpt-5.6", configuration_hash)
    defaults = ProjectModelDefaultsRevision(
        ProjectId("studio"),
        1,
        (
            ProjectModelDefault(
                2,
                registry.revision_hash,
                registry.provider_id,
                registry.entries[0].model_id,
                registry.entries[0].agent_configuration_revision_hash,
            ),
        ),
    )
    channel = DbosHostConfigurationChannel(engine)

    try:
        assert channel.publish_model_registry_revision(registry) == (
            ModelRegistryRevisionCreated(registry)
        )
        assert channel.publish_model_registry_revision(registry) == (
            ModelRegistryRevisionExisting(registry)
        )
        assert channel.latest_model_registry_revision(registry.provider_id) == registry
        assert channel.publish_project_model_defaults_revision(defaults) == (
            ProjectModelDefaultsRevisionCreated(defaults)
        )
        assert channel.publish_project_model_defaults_revision(defaults) == (
            ProjectModelDefaultsRevisionExisting(defaults)
        )
        assert channel.latest_project_model_defaults_revision(defaults.project_id) == (
            defaults
        )
    finally:
        engine.dispose()


def test_defaults_retry_and_carry_forward_survive_a_registry_advance(
    tmp_path: Path,
) -> None:
    engine = opened_channel(tmp_path)
    configuration_hash = _store_configuration(engine, "openai", "gpt-5.6")
    registry = _registry("openai", "gpt-5.6", configuration_hash)
    defaults = ProjectModelDefaultsRevision(
        ProjectId("studio"),
        1,
        (
            ProjectModelDefault(
                2,
                registry.revision_hash,
                registry.provider_id,
                registry.entries[0].model_id,
                registry.entries[0].agent_configuration_revision_hash,
            ),
        ),
    )
    advanced = ModelRegistryRevision(ProviderId("openai"), 2, ())
    carried = ProjectModelDefaultsRevision(defaults.project_id, 2, defaults.defaults)
    channel = DbosHostConfigurationChannel(engine)

    try:
        assert channel.publish_model_registry_revision(registry) == (
            ModelRegistryRevisionCreated(registry)
        )
        assert channel.publish_project_model_defaults_revision(defaults) == (
            ProjectModelDefaultsRevisionCreated(defaults)
        )
        assert channel.publish_model_registry_revision(advanced) == (
            ModelRegistryRevisionCreated(advanced)
        )

        assert channel.publish_project_model_defaults_revision(defaults) == (
            ProjectModelDefaultsRevisionExisting(defaults)
        )
        assert channel.publish_project_model_defaults_revision(carried) == (
            ProjectModelDefaultsRevisionCreated(carried)
        )
        assert channel.latest_project_model_defaults_revision(defaults.project_id) == (
            carried
        )
    finally:
        engine.dispose()


def test_defaults_do_not_append_after_a_registry_revision_interleaves(
    tmp_path: Path,
) -> None:
    """The registry check and defaults append share the write transaction."""

    engine = opened_channel(tmp_path)
    project = ProjectId("studio")
    configuration_hash = _store_configuration(engine, "openai", "gpt-5.6")
    registry = _registry("openai", "gpt-5.6", configuration_hash)
    replacement = ModelRegistryRevision(ProviderId("openai"), 2, ())

    class InterleavingChannel(DbosHostConfigurationChannel):
        def publish_project_model_defaults_revision(
            self, revision: ProjectModelDefaultsRevision
        ) -> PublishProjectModelDefaultsResult:
            published = self.publish_model_registry_revision(replacement)
            assert isinstance(published, ModelRegistryRevisionCreated)
            return super().publish_project_model_defaults_revision(revision)

    channel = InterleavingChannel(engine)
    try:
        publish_project_root_revision(
            engine, ProjectRootRevision(project, 1, tmp_path / "project")
        )
        assert isinstance(
            channel.publish_model_registry_revision(registry),
            ModelRegistryRevisionCreated,
        )

        result = publish_project_model_defaults(
            project.value,
            project,
            1,
            (
                (
                    2,
                    registry.revision_hash.value,
                    registry.provider_id.value,
                    registry.entries[0].model_id,
                    registry.entries[0].agent_configuration_revision_hash.value,
                ),
            ),
            channel,
        )

        assert isinstance(result, ProjectModelDefaultsInvalid)
        assert channel.latest_project_model_defaults_revision(project) is None
    finally:
        engine.dispose()


def test_model_configuration_snapshot_never_mixes_interleaved_revisions(
    tmp_path: Path,
) -> None:
    engine = opened_channel(tmp_path)
    project = ProjectId("studio")
    old_configuration_hash = _store_configuration(engine, "openai", "gpt-old")
    new_configuration_hash = _store_configuration(engine, "openai", "gpt-new")
    old_registry = _registry("openai", "gpt-old", old_configuration_hash)
    new_registry = ModelRegistryRevision(
        ProviderId("openai"),
        2,
        (
            ModelRegistryEntry(
                "gpt-new",
                AgentConfigurationRevisionHash(new_configuration_hash),
                ModelRegistryEntrySource.DISCOVERED,
                ProviderModelCheck.CHECKED,
            ),
        ),
    )

    def defaults_for(
        revision_number: int, registry: ModelRegistryRevision
    ) -> ProjectModelDefaultsRevision:
        entry = registry.entries[0]
        return ProjectModelDefaultsRevision(
            project,
            revision_number,
            (
                ProjectModelDefault(
                    2,
                    registry.revision_hash,
                    registry.provider_id,
                    entry.model_id,
                    entry.agent_configuration_revision_hash,
                ),
            ),
        )

    old_defaults = defaults_for(1, old_registry)
    new_defaults = defaults_for(2, new_registry)
    channel = DbosHostConfigurationChannel(engine)
    channel.publish_model_registry_revision(old_registry)
    channel.publish_project_model_defaults_revision(old_defaults)
    first_snapshot_query = Barrier(2)
    writer_finished = Barrier(2)
    paused = False

    def pause_after_snapshot_begins(
        connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal paused
        if paused or "SELECT DISTINCT" not in statement.upper():
            return
        paused = True
        assert connection.connection.driver_connection.in_transaction
        first_snapshot_query.wait(timeout=5)
        writer_finished.wait(timeout=5)

    event.listen(engine, "after_cursor_execute", pause_after_snapshot_begins)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(channel.model_configuration_snapshot, project)
            first_snapshot_query.wait(timeout=5)
            channel.publish_model_registry_revision(new_registry)
            channel.publish_project_model_defaults_revision(new_defaults)
            writer_finished.wait(timeout=5)
            snapshot = reading.result(timeout=5)
    finally:
        event.remove(engine, "after_cursor_execute", pause_after_snapshot_begins)
        engine.dispose()

    assert isinstance(snapshot, HostModelConfigurationSnapshot)
    observed = (
        snapshot.registries[0].revision_hash,
        None
        if snapshot.project_defaults is None
        else snapshot.project_defaults.revision_hash,
    )
    assert observed in {
        (old_registry.revision_hash, old_defaults.revision_hash),
        (new_registry.revision_hash, new_defaults.revision_hash),
    }


def test_a_different_registry_at_the_same_provider_revision_conflicts(
    tmp_path: Path,
) -> None:
    engine = opened_channel(tmp_path)
    first_hash = _store_configuration(engine, "openai", "gpt-5.6")
    second_hash = _store_configuration(engine, "anthropic", "claude-opus-5")
    channel = DbosHostConfigurationChannel(engine)
    first = _registry("openai", "gpt-5.6", first_hash)
    other = _registry("openai", "claude-opus-5", second_hash)

    try:
        channel.publish_model_registry_revision(first)
        assert isinstance(
            channel.publish_model_registry_revision(other),
            ModelRegistryRevisionConflict,
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "rewrite",
    [
        pytest.param(
            host_model_registry_revisions.update().values(provider_id="tampered"),
            id="registry-header-update",
        ),
        pytest.param(host_model_registry_entries.delete(), id="registry-entry-delete"),
        pytest.param(
            host_project_model_defaults_revisions.update().values(
                project_id="tampered"
            ),
            id="defaults-header-update",
        ),
        pytest.param(host_project_model_defaults.delete(), id="default-delete"),
    ],
)
def test_model_configuration_revisions_cannot_be_rewritten(
    tmp_path: Path, rewrite: sa.Executable
) -> None:
    engine = opened_channel(tmp_path)
    configuration_hash = _store_configuration(engine, "openai", "gpt-5.6")
    registry = _registry("openai", "gpt-5.6", configuration_hash)
    default = ProjectModelDefault(
        2,
        registry.revision_hash,
        registry.provider_id,
        registry.entries[0].model_id,
        registry.entries[0].agent_configuration_revision_hash,
    )
    channel = DbosHostConfigurationChannel(engine)
    channel.publish_model_registry_revision(registry)
    channel.publish_project_model_defaults_revision(
        ProjectModelDefaultsRevision(ProjectId("studio"), 1, (default,))
    )

    try:
        with (
            pytest.raises(IntegrityError, match=r"host (project )?model"),
            engine.begin() as connection,
        ):
            connection.execute(rewrite)
    finally:
        engine.dispose()
