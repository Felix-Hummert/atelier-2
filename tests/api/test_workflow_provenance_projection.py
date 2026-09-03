"""What the wire says about where a listed revision came from, and what it hides.

`provenance_resource` is a pure function of the application's own record, so
every shape pins here without a store: a revision a source delivered names that
source by the public reference a reader may hold, and one nobody delivered
answers with nothing at all. The durable source id never reaches the wire --
the same rule a run's public reference follows.
"""

from __future__ import annotations

from atelier2.api.projection.workflows import provenance_resource
from atelier2.api.references import encode_public_definition_source_reference
from atelier2.api.wire.resources import WorkflowRevisionProvenanceResource
from atelier2.contracts.catalog_v3 import CatalogActivatedAt
from atelier2.contracts.definition_sources import (
    DefinitionSourceId,
    RepositoryPath,
    RevisionProvenance,
    SourceCommit,
)

SOURCE_ID = DefinitionSourceId("a" * 64)
INTAKEN_AT = CatalogActivatedAt("2026-09-03T08:00:00Z")


def delivered() -> RevisionProvenance:
    return RevisionProvenance(
        SOURCE_ID,
        SourceCommit("b" * 40),
        RepositoryPath("workflows/build.yaml"),
        INTAKEN_AT,
    )


def test_a_delivered_revision_names_its_source_commit_path_and_instant() -> None:
    assert provenance_resource(delivered()) == WorkflowRevisionProvenanceResource(
        source=encode_public_definition_source_reference(SOURCE_ID),
        source_commit="b" * 40,
        source_path="workflows/build.yaml",
        intaken_at=INTAKEN_AT.value,
    )


def test_the_durable_source_id_never_reaches_the_wire() -> None:
    """A reader is given a reference to hold, not the store's own identity."""

    projected = provenance_resource(delivered())

    assert projected is not None
    assert SOURCE_ID.value not in projected.model_dump_json()
    assert projected.source.startswith("source1.")


def test_a_revision_no_source_delivered_carries_no_provenance() -> None:
    assert provenance_resource(None) is None
