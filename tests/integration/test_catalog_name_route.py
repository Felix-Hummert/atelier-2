from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import published_revisions
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageId,
    CatalogRetirementState,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.ports.published_revisions import (
    CatalogLineageFounded,
    CatalogLineageQuery,
    CatalogNameFound,
    CatalogRevisionPosition,
    PublishedRevisionCreated,
    PublishedRevisionsUnavailable,
    ResolveCatalogNameResult,
    ResolvePublishedRevisionResult,
)
from tests.scenarios.api import (
    api_limits,
    durable_api_client,
    durable_ports,
    event_poll_backoff,
)

NAME = "review-bounded-diff"
DOCUMENT = b"""format_version: 3
name: review-bounded-diff
nodes:
  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Review one bounded diff.
"""
SECOND_DOCUMENT = DOCUMENT.replace(b"Review one bounded diff.", b"Review it again.")
BY_NAME = f"{API_PREFIX}/catalog-revisions/by-name/{RevisionKind.WORKFLOW.value}"


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "catalog-route-test"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def found(runtime: DbosRuntime, document: bytes = DOCUMENT) -> PublishedRevision:
    """One named lineage in the catalog, published the way the product does."""
    store = DbosCatalogStore(runtime.engine)
    revision = PublishedRevision(RevisionKind.WORKFLOW, document)
    assert isinstance(store.publish_revision(revision), PublishedRevisionCreated)
    founded = store.found_lineage(
        revision,
        CatalogLineageDisplayName(NAME),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-17T00:00:00Z"),
    )
    assert isinstance(founded, CatalogLineageFounded)
    return revision


def client(runtime: DbosRuntime) -> TestClient:
    return durable_api_client(runtime)


@pytest.mark.proves("a-name-is-answerable-over-the-api")
def test_a_name_answers_with_the_revision_it_resolves_to(
    runtime: DbosRuntime,
) -> None:
    revision = found(runtime)

    response = client(runtime).get(f"{BY_NAME}/{NAME}")

    assert response.status_code == 200
    body = response.json()
    assert body["catalog_revision_hash"] == revision.revision_hash.value
    assert body["display_name"] == NAME
    assert body["revision_number"] == 1


@pytest.mark.proves("the-name-path-is-never-read-as-a-revision-hash")
def test_the_name_path_is_not_read_as_a_revision_hash(runtime: DbosRuntime) -> None:
    # The name now answers under its own prefix rather than beside
    # `/workflow-revisions/{hash}`, so no hash route can claim it. This asks a
    # name and reads a name back, which is what that separation is for.
    found(runtime)

    response = client(runtime).get(f"{BY_NAME}/{NAME}")

    assert response.status_code == 200


@pytest.mark.proves("a-name-the-catalog-cannot-honour-is-refused-by-its-own-reason")
def test_a_name_nobody_admitted_is_a_named_problem(runtime: DbosRuntime) -> None:
    found(runtime)

    response = client(runtime).get(f"{BY_NAME}/no-such-workflow")

    assert response.status_code == 404
    assert response.json()["type"].endswith("catalog-name-not-found")


@pytest.mark.proves("a-name-the-catalog-cannot-honour-is-refused-by-its-own-reason")
def test_a_retired_lineage_refuses_by_name_instead_of_answering(
    runtime: DbosRuntime,
) -> None:
    revision = found(runtime)
    store = DbosCatalogStore(runtime.engine)
    found_name = store.resolve_name(
        RevisionKind.WORKFLOW, CatalogLineageDisplayName(NAME), "head"
    )
    assert isinstance(found_name, CatalogNameFound)
    lineage_id = found_name.lineage_id
    store.retire_lineage(
        lineage_id,
        CatalogRetirementState.RETIRED,
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-17T00:01:00Z"),
    )

    response = client(runtime).get(f"{BY_NAME}/{NAME}")

    assert response.status_code == 410
    assert response.json()["type"].endswith("catalog-lineage-retired")
    # A retirement discloses neither the bytes it once held nor which lineage
    # held them: the refusal is the whole answer.
    assert revision.revision_hash.value not in response.text
    assert lineage_id.value not in response.text


@pytest.mark.proves("a-name-is-answerable-over-the-api")
def test_a_position_answers_the_member_the_caller_asked_for(
    runtime: DbosRuntime,
) -> None:
    first = found(runtime)
    store = DbosCatalogStore(runtime.engine)
    second = PublishedRevision(RevisionKind.WORKFLOW, SECOND_DOCUMENT)
    store.publish_revision(second)
    found_name = store.resolve_name(
        RevisionKind.WORKFLOW, CatalogLineageDisplayName(NAME), "head"
    )
    assert isinstance(found_name, CatalogNameFound)
    lineage_id = found_name.lineage_id
    store.admit_member(
        lineage_id,
        second,
        CatalogLineageDisplayName(NAME),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-17T00:02:00Z"),
    )
    api = client(runtime)

    head = api.get(f"{BY_NAME}/{NAME}")
    first_member = api.get(f"{BY_NAME}/{NAME}?position=1")

    assert head.json()["catalog_revision_hash"] == second.revision_hash.value
    assert first_member.json()["catalog_revision_hash"] == first.revision_hash.value


@pytest.mark.proves("a-name-the-catalog-cannot-honour-is-refused-by-its-own-reason")
def test_a_position_that_is_not_a_member_is_a_named_problem(
    runtime: DbosRuntime,
) -> None:
    found(runtime)

    response = client(runtime).get(f"{BY_NAME}/{NAME}?position=7")

    assert response.status_code == 404
    assert response.json()["type"].endswith("catalog-name-not-found")


@pytest.mark.proves("a-name-the-catalog-cannot-honour-is-refused-by-its-own-reason")
def test_a_position_that_is_neither_a_number_nor_head_is_refused(
    runtime: DbosRuntime,
) -> None:
    found(runtime)

    response = client(runtime).get(f"{BY_NAME}/{NAME}?position=later")

    assert response.status_code == 400
    assert response.json()["type"].endswith("invalid-catalog-position")


@dataclass
class _ReferenceLookupUnavailable:
    """A real store for name lookup, but a scripted outage resolving the member.

    `resolve_reference` runs only after `resolve_name` already found the head,
    so the route's own DB cannot isolate one failure from the other; this
    double proves the route maps `resolve_reference`'s own refusal (#735)
    rather than exercising the adapter's SQL failure paths again.
    """

    store: DbosCatalogStore

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        return self.store.resolve(kind, revision_hash)

    def resolve_reference(
        self,
        kind: RevisionKind,
        lineage_id: CatalogLineageId,
        revision_hash: PublishedRevisionHash,
    ) -> ResolvePublishedRevisionResult:
        del kind, lineage_id, revision_hash
        return PublishedRevisionsUnavailable()

    def resolve_name(
        self,
        kind: RevisionKind,
        lineage_id_or_name: CatalogLineageQuery,
        position: CatalogRevisionPosition,
    ) -> ResolveCatalogNameResult:
        return self.store.resolve_name(kind, lineage_id_or_name, position)


def test_a_reference_lookup_outage_answers_unavailable_instead_of_a_500(
    runtime: DbosRuntime,
) -> None:
    """`CatalogResolver.resolve_reference` (#735) is a named refusal, not a 500."""
    found(runtime)
    failing_client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=durable_ports(
                runtime.engine,
                runtime.settings,
                runtime.agent_executor_registry,
                catalog_resolver=_ReferenceLookupUnavailable(
                    DbosCatalogStore(runtime.engine)
                ),
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )

    response = failing_client.get(f"{BY_NAME}/{NAME}")

    assert response.status_code == 503
    assert response.json()["type"].endswith("temporarily-unavailable")


def test_a_database_corruption_answers_durable_state_corrupt_instead_of_a_500(
    runtime: DbosRuntime,
) -> None:
    """A member the name resolves to, whose published bytes vanished, is
    corruption (#735) -- real end to end, since `resolve_name` is guarded now too."""
    found(runtime)
    store = DbosCatalogStore(runtime.engine)
    second = PublishedRevision(RevisionKind.WORKFLOW, SECOND_DOCUMENT)
    store.publish_revision(second)
    found_name = store.resolve_name(
        RevisionKind.WORKFLOW, CatalogLineageDisplayName(NAME), "head"
    )
    assert isinstance(found_name, CatalogNameFound)
    store.admit_member(
        found_name.lineage_id,
        second,
        CatalogLineageDisplayName(NAME),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-17T00:02:00Z"),
    )
    with runtime.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER published_revisions_no_delete")
        connection.execute(
            published_revisions.delete().where(
                published_revisions.c.kind == second.kind.value,
                published_revisions.c.revision_hash == second.revision_hash.value,
            )
        )

    response = client(runtime).get(f"{BY_NAME}/{NAME}")

    assert response.status_code == 500
    assert response.json()["type"].endswith("durable-state-corrupt")
