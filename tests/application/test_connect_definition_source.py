"""Registering a definition source, decided without a command line in reach."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NoReturn

from atelier2.application.connect_definition_source import (
    ConnectRefused,
    connect_definition_source,
)
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.definition_sources import (
    DefinitionSourceAccess,
    DefinitionSourceActor,
    DefinitionSourceConfiguration,
    DefinitionSourceId,
    DefinitionSourceKind,
    DefinitionSourceRefusal,
    DefinitionSourceRevision,
    DefinitionSourceSelection,
    RepositoryLocation,
    RepositoryRef,
    SelectionPattern,
    SourceCommit,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.ports.definition_sources import (
    DefinitionSourceFound,
    DefinitionSourceMissing,
    DefinitionSourceRegistered,
    DefinitionSourceUnchanged,
    DefinitionSourceUnreadable,
    ReadDefinitionSourceResult,
    ReadSourceIntakesResult,
    RegisterDefinitionSourceResult,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable

COMMIT = SourceCommit("c" * 40)


def registration(*patterns: str) -> DefinitionSourceConfiguration:
    return DefinitionSourceConfiguration(
        DefinitionSourceKind.GIT,
        RepositoryLocation("/srv/definitions.git"),
        RepositoryRef("refs/heads/main"),
        DefinitionSourceAccess.ANONYMOUS,
        DefinitionSourceActor("felix"),
        tuple(
            DefinitionSourceSelection(SelectionPattern(pattern), RevisionKind.WORKFLOW)
            for pattern in (patterns or ("workflows/*.yaml",))
        ),
    )


@dataclass
class SourcesKeptInMemory:
    """A registrar that keeps one revision per source, or refuses as prepared."""

    registered: dict[str, DefinitionSourceRevision] = field(default_factory=dict)
    refusal: DurableWriteUnavailable | PortDurableStateCorrupt | None = None

    def register(
        self, configuration: DefinitionSourceConfiguration
    ) -> RegisterDefinitionSourceResult:
        if self.refusal is not None:
            return self.refusal
        standing = self.registered.get(configuration.source_id.value)
        if standing is not None and standing.configuration == configuration:
            return DefinitionSourceUnchanged(standing)
        appended = DefinitionSourceRevision(
            configuration, 1 if standing is None else standing.revision_number + 1
        )
        self.registered[configuration.source_id.value] = appended
        return DefinitionSourceRegistered(appended)

    def read_source(self, source_id: DefinitionSourceId) -> ReadDefinitionSourceResult:
        standing = self.registered.get(source_id.value)
        if standing is None:
            return DefinitionSourceMissing(source_id)
        return DefinitionSourceFound(standing)

    def latest_intakes(self, source_id: DefinitionSourceId) -> ReadSourceIntakesResult:
        del source_id
        return {}

    def record_intakes(self, *_asked: object, **_named: object) -> NoReturn:
        raise AssertionError("connecting takes no content in")


@dataclass
class SourceAnsweringOnce:
    """A reader that resolves the configured ref, or refuses with one word."""

    refusal: DefinitionSourceRefusal | None = None
    resolved: list[DefinitionSourceConfiguration] = field(default_factory=list)

    def resolve(self, configuration: DefinitionSourceConfiguration) -> SourceCommit:
        if self.refusal is not None:
            raise DefinitionSourceUnreadable(self.refusal, "as the scenario prepared")
        self.resolved.append(configuration)
        return COMMIT

    def scan(self, configuration: DefinitionSourceConfiguration) -> NoReturn:
        del configuration
        raise AssertionError("connecting reads no file of the source")


def test_a_source_that_answers_is_registered_under_its_first_revision() -> None:
    configured = registration()
    registrar = SourcesKeptInMemory()

    result = connect_definition_source(configured, SourceAnsweringOnce(), registrar)

    assert result == DefinitionSourceRegistered(DefinitionSourceRevision(configured, 1))


def test_the_standing_configuration_registered_again_is_unchanged() -> None:
    configured = registration()
    registrar = SourcesKeptInMemory()
    connect_definition_source(configured, SourceAnsweringOnce(), registrar)

    result = connect_definition_source(configured, SourceAnsweringOnce(), registrar)

    assert result == DefinitionSourceUnchanged(DefinitionSourceRevision(configured, 1))


def test_a_source_that_cannot_be_reached_is_refused_before_anything_is_kept() -> None:
    """There is no disconnect yet, so a wire to nowhere is never recorded."""

    registrar = SourcesKeptInMemory()

    result = connect_definition_source(
        registration(),
        SourceAnsweringOnce(DefinitionSourceRefusal.UNREACHABLE),
        registrar,
    )

    assert result == ConnectRefused(
        DefinitionSourceRefusal.UNREACHABLE, "as the scenario prepared"
    )
    assert registrar.registered == {}


def test_only_the_location_and_the_ref_are_answered_for_before_registering() -> None:
    """A selection matching nothing yet is ordinary; the scan is where it shows."""

    reader = SourceAnsweringOnce()
    configured = registration("workflows/*.yaml", "agents/*.md")

    connect_definition_source(configured, reader, SourcesKeptInMemory())

    assert reader.resolved == [configured]


def test_a_store_that_would_not_write_becomes_this_layers_own_refusal() -> None:
    for port_answer, expected in (
        (DurableWriteUnavailable(), WriteUnavailable()),
        (PortDurableStateCorrupt(), DurableStateCorrupt()),
    ):
        result = connect_definition_source(
            registration(),
            SourceAnsweringOnce(),
            SourcesKeptInMemory(refusal=port_answer),
        )

        assert result == expected
