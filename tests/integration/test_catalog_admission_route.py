"""The write door beside the read one: authored names enter through the API.

One door family names every kind: `POST /catalog-lineages`, its members and
retirements, and `GET /catalog-revisions/by-name/{kind}/{name}`. These tests
drive the real routes against the real store, and every sentence that is about
admission rather than about one format is asked of both formats that author
their own name -- a V3 workflow and an agent definition.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    catalog_lineage_aliases,
    catalog_lineage_members,
    catalog_lineage_retirements,
    catalog_lineages,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.app import create_app
from atelier2.api.openapi import (
    API_PREFIX,
    CATALOG_LINEAGES_PATH,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
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
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.published_revisions import (
    CatalogLineageQuery,
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
SECOND_NAME = "review-bounded-diff-again"
LINEAGES = CATALOG_LINEAGES_PATH


def workflow_document(name: str, body: str = "Review one bounded diff.") -> bytes:
    return f"""format_version: 3
name: {name}
nodes:
  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: {body}
""".encode()


def agent_document(name: str, body: str = "Review one bounded diff.") -> bytes:
    return f"""---
name: {name}
description: Reviews one bounded diff.
---
{body}
""".encode()


@dataclass(frozen=True)
class AuthoringFormat:
    """One published format whose own bytes author the catalog name it takes."""

    kind: RevisionKind
    publish_path: str
    media_type: str
    published_hash_field: str
    document: Callable[[str, str], bytes]

    def __str__(self) -> str:
        return self.kind.value


FORMATS = (
    AuthoringFormat(
        RevisionKind.WORKFLOW,
        f"{API_PREFIX}/workflow-revisions",
        "application/yaml",
        "workflow_revision_hash",
        workflow_document,
    ),
    AuthoringFormat(
        RevisionKind.AGENT_DEFINITION,
        f"{API_PREFIX}/agent-definition-revisions",
        "text/markdown",
        "agent_definition_revision_hash",
        agent_document,
    ),
)
authoring_formats = pytest.mark.parametrize(
    "authoring", FORMATS, ids=[str(authoring) for authoring in FORMATS]
)
WORKFLOW, AGENT = FORMATS


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "admission-route-test"),
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


def client(runtime: DbosRuntime) -> TestClient:
    return durable_api_client(runtime)


def published_over_http(
    api: TestClient,
    authoring: AuthoringFormat,
    name: str = NAME,
    body: str = "Review one bounded diff.",
) -> str:
    """The operator door of this kind: bytes in, hash out."""

    response = api.post(
        authoring.publish_path,
        content=authoring.document(name, body),
        headers={"content-type": authoring.media_type},
    )
    assert response.status_code == 201, response.text
    return str(response.json()[authoring.published_hash_field])


def founding(
    authoring: AuthoringFormat, revision_hash: str, name: str | None = None
) -> dict[str, str]:
    request = {
        "kind": authoring.kind.value,
        "catalog_revision_hash": revision_hash,
        "actor": "operator",
        "activated_at": "2026-08-17T00:00:00Z",
    }
    if name is not None:
        request["display_name"] = name
    return request


def membership(
    authoring: AuthoringFormat,
    revision_hash: str,
    activated_at: str = "2026-08-17T00:01:00Z",
) -> dict[str, str]:
    return {
        "kind": authoring.kind.value,
        "catalog_revision_hash": revision_hash,
        "actor": "operator",
        "activated_at": activated_at,
    }


def retirement() -> dict[str, str]:
    return {"actor": "operator", "activated_at": "2026-08-17T00:02:00Z"}


def by_name_path(authoring: AuthoringFormat, name: str) -> str:
    return f"{API_PREFIX}/catalog-revisions/by-name/{authoring.kind.value}/{name}"


def catalog_snapshot(runtime: DbosRuntime) -> dict[str, tuple[tuple[object, ...], ...]]:
    tables = (
        catalog_lineages,
        catalog_lineage_members,
        catalog_lineage_aliases,
        catalog_lineage_retirements,
    )
    with runtime.engine.connect() as connection:
        return {
            table.name: tuple(
                sorted(tuple(row) for row in connection.execute(sa.select(table)))
            )
            for table in tables
        }


@pytest.mark.proves("a-workflow-published-over-the-api-is-named-over-the-api")
@authoring_formats
def test_a_document_published_over_the_api_is_named_over_the_api(
    runtime: DbosRuntime, authoring: AuthoringFormat
) -> None:
    """The live hole: publish through the kind's door, then POST /catalog-lineages."""

    api = client(runtime)
    revision_hash = published_over_http(api, authoring)
    request = founding(authoring, revision_hash)

    response = api.post(LINEAGES, json=request)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["display_name"] == NAME
    assert body["catalog_revision_hash"] == revision_hash
    assert body["revision_number"] == 1
    assert (
        body["lineage_id"]
        == CatalogLineage(
            authoring.kind, PublishedRevisionHash(revision_hash)
        ).lineage_id.value
    )

    repeated = api.post(LINEAGES, json=request)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json() == body


@pytest.mark.proves("a-published-revision-becomes-a-named-lineage-over-the-api")
@authoring_formats
def test_the_name_the_api_founded_answers_the_read_door(
    runtime: DbosRuntime, authoring: AuthoringFormat
) -> None:
    """The whole point: the catalog fills, and the read door answers per kind."""

    api = client(runtime)
    revision_hash = published_over_http(api, authoring)

    api.post(LINEAGES, json=founding(authoring, revision_hash))
    answered = api.get(by_name_path(authoring, NAME))

    assert answered.status_code == 200, answered.text
    assert answered.json()["catalog_revision_hash"] == revision_hash


@pytest.mark.proves("a-later-revision-joins-the-lineage-that-already-holds-its-name")
@authoring_formats
def test_a_later_revision_joins_the_named_lineage(
    runtime: DbosRuntime, authoring: AuthoringFormat
) -> None:
    api = client(runtime)
    first_hash = published_over_http(api, authoring)
    founded = api.post(LINEAGES, json=founding(authoring, first_hash))
    assert founded.status_code == 201, founded.text
    second_hash = published_over_http(api, authoring, SECOND_NAME)

    response = api.post(
        f"{LINEAGES}/{founded.json()['lineage_id']}/members",
        json=membership(authoring, second_hash),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["revision_number"] == 2
    assert body["display_name"] == SECOND_NAME
    assert body["catalog_revision_hash"] == second_hash


@pytest.mark.proves("a-later-revision-joins-the-lineage-that-already-holds-its-name")
@authoring_formats
def test_two_revisions_of_one_name_are_members_one_and_two(
    runtime: DbosRuntime, authoring: AuthoringFormat
) -> None:
    """The same authored name twice is one lineage, not a second one."""

    api = client(runtime)
    first_hash = published_over_http(api, authoring)
    second_hash = published_over_http(api, authoring, NAME, "Review it again.")
    assert second_hash != first_hash
    lineage_id = api.post(LINEAGES, json=founding(authoring, first_hash)).json()[
        "lineage_id"
    ]

    held = api.post(LINEAGES, json=founding(authoring, second_hash))
    admitted = api.post(
        f"{LINEAGES}/{lineage_id}/members", json=membership(authoring, second_hash)
    )

    assert held.status_code == 409
    assert held.json()["type"].endswith("catalog-name-held")
    assert admitted.status_code == 201, admitted.text
    assert admitted.json()["revision_number"] == 2
    head = api.get(by_name_path(authoring, NAME))
    assert head.status_code == 200, head.text
    assert head.json()["catalog_revision_hash"] == second_hash


@authoring_formats
def test_retiring_a_lineage_removes_its_name_but_keeps_its_published_revision(
    runtime: DbosRuntime, authoring: AuthoringFormat
) -> None:
    api = client(runtime)
    revision_hash = published_over_http(api, authoring)
    founded = api.post(LINEAGES, json=founding(authoring, revision_hash))
    assert founded.status_code == 201, founded.text
    lineage_id = founded.json()["lineage_id"]

    retired = api.post(f"{LINEAGES}/{lineage_id}/retirements", json=retirement())
    repeated = api.post(f"{LINEAGES}/{lineage_id}/retirements", json=retirement())

    assert retired.status_code == 204, retired.text
    assert repeated.status_code == 204, repeated.text
    by_name = api.get(by_name_path(authoring, NAME))
    assert by_name.status_code == 410, by_name.text
    assert by_name.json()["type"].endswith("catalog-lineage-retired")
    exact_revision = api.get(f"{authoring.publish_path}/{revision_hash}")
    assert exact_revision.status_code == 200, exact_revision.text


def test_a_workflow_and_an_agent_may_carry_the_same_name(
    runtime: DbosRuntime,
) -> None:
    """A name is unique within its kind, so both lineages hold `review-bounded-diff`."""

    api = client(runtime)
    workflow_hash = published_over_http(api, WORKFLOW)
    agent_hash = published_over_http(api, AGENT)

    workflow_lineage = api.post(LINEAGES, json=founding(WORKFLOW, workflow_hash))
    agent_lineage = api.post(LINEAGES, json=founding(AGENT, agent_hash))

    assert workflow_lineage.status_code == 201, workflow_lineage.text
    assert agent_lineage.status_code == 201, agent_lineage.text
    assert workflow_lineage.json()["display_name"] == NAME
    assert agent_lineage.json()["display_name"] == NAME
    assert workflow_lineage.json()["lineage_id"] != agent_lineage.json()["lineage_id"]
    assert (
        api.get(by_name_path(WORKFLOW, NAME)).json()["catalog_revision_hash"]
        == workflow_hash
    )
    assert (
        api.get(by_name_path(AGENT, NAME)).json()["catalog_revision_hash"] == agent_hash
    )


def test_retiring_the_agent_lineage_leaves_the_workflow_name_answering(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)
    workflow_hash = published_over_http(api, WORKFLOW)
    agent_hash = published_over_http(api, AGENT)
    api.post(LINEAGES, json=founding(WORKFLOW, workflow_hash))
    agent_lineage_id = api.post(LINEAGES, json=founding(AGENT, agent_hash)).json()[
        "lineage_id"
    ]

    retired = api.post(f"{LINEAGES}/{agent_lineage_id}/retirements", json=retirement())

    assert retired.status_code == 204, retired.text
    assert api.get(by_name_path(AGENT, NAME)).status_code == 410
    assert api.get(by_name_path(WORKFLOW, NAME)).status_code == 200


@authoring_formats
def test_an_authored_name_cannot_be_restated_by_the_api(
    runtime: DbosRuntime, authoring: AuthoringFormat
) -> None:
    api = client(runtime)
    revision_hash = published_over_http(api, authoring)
    before = catalog_snapshot(runtime)

    for caller_name in (NAME, "caller-other"):
        response = api.post(
            LINEAGES, json=founding(authoring, revision_hash, caller_name)
        )

        assert response.status_code == 422, response.text
        assert response.json()["type"].endswith("invalid-request")
    assert catalog_snapshot(runtime) == before


@pytest.mark.proves(
    "a-v3-workflow-with-a-64-hex-authored-name-is-refused-before-any-catalog-write"
)
@authoring_formats
@pytest.mark.parametrize("authored_name", ["a" * 64, "Invalid Name"])
def test_an_invalid_authored_name_writes_no_catalog_row(
    runtime: DbosRuntime, authoring: AuthoringFormat, authored_name: str
) -> None:
    api = client(runtime)
    revision_hash = published_over_http(api, authoring, authored_name)
    before = catalog_snapshot(runtime)

    response = api.post(LINEAGES, json=founding(authoring, revision_hash))

    assert response.status_code == 422
    assert response.json()["type"].endswith("invalid-request")
    assert catalog_snapshot(runtime) == before


def test_an_impossible_founding_time_writes_no_catalog_row(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)
    revision_hash = published_over_http(api, WORKFLOW)
    before = catalog_snapshot(runtime)
    request = founding(WORKFLOW, revision_hash)
    request["activated_at"] = "2026-13-17T00:00:00Z"

    response = api.post(LINEAGES, json=request)

    assert response.status_code == 422
    assert response.json()["type"].endswith("invalid-request")
    assert catalog_snapshot(runtime) == before


def test_an_impossible_admission_time_changes_no_catalog_row(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)
    first_hash = published_over_http(api, WORKFLOW)
    second_hash = published_over_http(api, WORKFLOW, SECOND_NAME)
    lineage_id = api.post(LINEAGES, json=founding(WORKFLOW, first_hash)).json()[
        "lineage_id"
    ]
    before = catalog_snapshot(runtime)

    response = api.post(
        f"{LINEAGES}/{lineage_id}/members",
        json=membership(WORKFLOW, second_hash, "2026-13-17T00:01:00Z"),
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith("invalid-request")
    assert catalog_snapshot(runtime) == before


@pytest.mark.parametrize(
    ("lineage_id", "payload", "status", "problem_type"),
    [
        ("a" * 64, retirement(), 404, "catalog-lineage-missing"),
        ("not-a-lineage", retirement(), 404, "catalog-lineage-missing"),
        (
            "a" * 64,
            {"actor": "", "activated_at": "2026-08-17T00:02:00Z"},
            422,
            "invalid-request",
        ),
    ],
)
def test_retirement_route_names_missing_and_invalid_requests(
    runtime: DbosRuntime,
    lineage_id: str,
    payload: dict[str, str],
    status: int,
    problem_type: str,
) -> None:
    response = client(runtime).post(
        f"{LINEAGES}/{lineage_id}/retirements", json=payload
    )

    assert response.status_code == status, response.text
    assert response.json()["type"].endswith(problem_type)


def test_a_name_asked_under_a_kind_the_registry_has_no_word_for_is_refused(
    runtime: DbosRuntime,
) -> None:
    response = client(runtime).get(
        f"{API_PREFIX}/catalog-revisions/by-name/not-a-kind/{NAME}"
    )

    assert response.status_code == 422, response.text
    assert response.json()["type"].endswith("invalid-request")


@pytest.mark.proves("a-later-revision-joins-the-lineage-that-already-holds-its-name")
def test_two_names_survive_a_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    external_path = tmp_path / "external.sqlite"

    def started() -> DbosRuntime:
        instance = DbosRuntime(
            DbosRuntimeSettings(database_path, "admission-route-test"),
            LoopbackEffectAdapterFactory(
                external_path,
                AdapterRevision("loopback-v1"),
                EffectDestination("loopback-test"),
            ),
        )
        instance.initialize_storage()
        return instance

    runtime = started()
    try:
        api = client(runtime)
        first_hash = published_over_http(api, WORKFLOW)
        second_hash = published_over_http(api, WORKFLOW, SECOND_NAME)
        lineage_id = api.post(LINEAGES, json=founding(WORKFLOW, first_hash)).json()[
            "lineage_id"
        ]
        response = api.post(
            f"{LINEAGES}/{lineage_id}/members",
            json=membership(WORKFLOW, second_hash),
        )
        assert response.status_code == 201, response.text
    finally:
        runtime.close()

    restarted = started()
    try:
        restarted_api = client(restarted)
        for name in (NAME, SECOND_NAME):
            answered = restarted_api.get(by_name_path(WORKFLOW, name))
            assert answered.status_code == 200, answered.text
            assert answered.json() == {
                "display_name": SECOND_NAME,
                "lineage_id": lineage_id,
                "catalog_revision_hash": second_hash,
                "revision_number": 2,
            }
        assert (
            restarted_api.get(by_name_path(WORKFLOW, "caller-other")).status_code == 404
        )
    finally:
        restarted.close()


@pytest.mark.proves("an-admission-the-catalog-refuses-is-named-by-its-own-reason")
@authoring_formats
def test_a_revision_nobody_published_is_refused_by_name(
    runtime: DbosRuntime, authoring: AuthoringFormat
) -> None:
    unpublished = PublishedRevision(
        authoring.kind, authoring.document(SECOND_NAME, "Never published.")
    )

    response = client(runtime).post(
        LINEAGES, json=founding(authoring, unpublished.revision_hash.value)
    )

    assert response.status_code == 409
    problem = response.json()
    assert problem["type"].endswith("catalog-revision-unpublished")
    assert problem["detail"] == (
        "Publish the revision through the door of its kind before giving it a name."
    )


@pytest.mark.proves("an-admission-the-catalog-refuses-is-named-by-its-own-reason")
@authoring_formats
def test_a_revision_another_lineage_owns_is_refused_by_name(
    runtime: DbosRuntime, authoring: AuthoringFormat
) -> None:
    # An authored name comes from its own bytes, so the only way to ask the
    # catalog to found a second lineage over a revision is to name a revision
    # that already belongs to a lineage as someone else's later member.
    api = client(runtime)
    first_hash = published_over_http(api, authoring)
    second_hash = published_over_http(api, authoring, SECOND_NAME)
    lineage_id = api.post(LINEAGES, json=founding(authoring, first_hash)).json()[
        "lineage_id"
    ]
    api.post(
        f"{LINEAGES}/{lineage_id}/members", json=membership(authoring, second_hash)
    )

    response = api.post(LINEAGES, json=founding(authoring, second_hash))

    assert response.status_code == 409
    assert response.json()["type"].endswith("catalog-revision-owned")


@pytest.mark.proves("an-admission-the-catalog-refuses-is-named-by-its-own-reason")
def test_admission_into_a_lineage_that_does_not_exist_is_refused_by_name(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)
    revision_hash = published_over_http(api, WORKFLOW)

    response = api.post(
        f"{LINEAGES}/{'a' * 64}/members", json=membership(WORKFLOW, revision_hash)
    )

    assert response.status_code == 404
    assert response.json()["type"].endswith("catalog-lineage-missing")


@pytest.mark.proves("an-admission-the-catalog-refuses-is-named-by-its-own-reason")
def test_admission_into_a_retired_lineage_is_refused_by_name(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)
    first_hash = published_over_http(api, WORKFLOW)
    second_hash = published_over_http(api, WORKFLOW, SECOND_NAME)
    lineage_id = api.post(LINEAGES, json=founding(WORKFLOW, first_hash)).json()[
        "lineage_id"
    ]
    DbosCatalogStore(runtime.engine).retire_lineage(
        CatalogLineageId(lineage_id),
        CatalogRetirementState.RETIRED,
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-17T00:02:00Z"),
    )

    response = api.post(
        f"{LINEAGES}/{lineage_id}/members",
        json=membership(WORKFLOW, second_hash, "2026-08-17T00:03:00Z"),
    )

    # 410, because the reason is the one #200 already named for a retired
    # lineage: one reason, one problem code.
    assert response.status_code == 410
    assert response.json()["type"].endswith("catalog-lineage-retired")
    assert (
        DbosCatalogStore(runtime.engine).resolve_name(
            RevisionKind.WORKFLOW, CatalogLineageDisplayName(NAME), "head"
        )
        is not None
    )


@dataclass
class _LineageLookupFails:
    """A real store for everything except the lineage lookup an admission opens
    with, which answers a scripted store failure instead.

    A wildcard once folded that failure into `CatalogAdmissionLineageMissing`
    (a 404) the same as a lineage that never existed; this double proves the
    route now tells the two apart (#735 review delta).
    """

    store: DbosCatalogStore
    failure: ResolveCatalogNameResult

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
        return self.store.resolve_reference(kind, lineage_id, revision_hash)

    def resolve_name(
        self,
        kind: RevisionKind,
        lineage_id_or_name: CatalogLineageQuery,
        position: CatalogRevisionPosition,
    ) -> ResolveCatalogNameResult:
        del kind, lineage_id_or_name, position
        return self.failure


@pytest.mark.parametrize(
    ("failure", "status", "problem_type"),
    [
        (PublishedRevisionsUnavailable(), 503, "temporarily-unavailable"),
        (PortDurableStateCorrupt(), 500, "durable-state-corrupt"),
    ],
)
def test_a_lineage_lookup_outage_answers_its_own_refusal_not_a_404(
    runtime: DbosRuntime,
    failure: ResolveCatalogNameResult,
    status: int,
    problem_type: str,
) -> None:
    """A store failure opening an admission is its own refusal (#735 review
    delta), never the same 404 a genuinely missing lineage answers."""
    api = client(runtime)
    first_hash = published_over_http(api, WORKFLOW)
    second_hash = published_over_http(api, WORKFLOW, SECOND_NAME)
    lineage_id = api.post(LINEAGES, json=founding(WORKFLOW, first_hash)).json()[
        "lineage_id"
    ]
    failing_client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=durable_ports(
                runtime.engine,
                runtime.settings,
                runtime.agent_executor_registry,
                catalog_resolver=_LineageLookupFails(
                    DbosCatalogStore(runtime.engine), failure
                ),
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )

    response = failing_client.post(
        f"{LINEAGES}/{lineage_id}/members", json=membership(WORKFLOW, second_hash)
    )

    assert response.status_code == status
    assert response.json()["type"].endswith(problem_type)


def test_a_revision_the_store_holds_but_the_publish_door_never_saw_is_admitted(
    runtime: DbosRuntime,
) -> None:
    """A workflow's bytes reach the catalog through `published_revisions` too."""

    revision = PublishedRevision(RevisionKind.WORKFLOW, workflow_document(NAME))
    store = DbosCatalogStore(runtime.engine)
    assert isinstance(store.publish_revision(revision), PublishedRevisionCreated)

    response = client(runtime).post(
        LINEAGES, json=founding(WORKFLOW, revision.revision_hash.value)
    )

    assert response.status_code == 201, response.text
    assert response.json()["display_name"] == NAME
