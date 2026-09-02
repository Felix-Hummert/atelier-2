"""Reaching a definition source, and keeping what was configured about it.

Three owners, deliberately apart. The reader answers what a repository holds
at one resolved commit and knows nothing about a store. The registry answers
what is registered and what it has delivered, and nothing else -- a scan is
handed that one, so its write-freedom is the shape of what it was given rather
than a promise about what it does with more. Registering is the registrar's,
and only a caller that means to write asks for it.

Reading refuses in the source's own closed vocabulary
(`DefinitionSourceRefusal`), because every one of those refusals happens before
a transaction is opened. What a *document* is refused for is not this port's
word: the publication door already owns that sentence, and a scan passes it on
unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.definition_sources import (
    DefinitionSourceConfiguration,
    DefinitionSourceId,
    DefinitionSourceRefusal,
    DefinitionSourceRevision,
    DefinitionSourceSelection,
    RepositoryPath,
    SourceCommit,
    SourceIntake,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


class DefinitionSourceUnreadable(Exception):
    """The source could not be read, in the closed vocabulary it refuses with."""

    def __init__(self, refusal: DefinitionSourceRefusal, detail: str) -> None:
        super().__init__(f"{refusal.value}: {detail}")
        self.refusal = refusal
        self.detail = detail


@dataclass(frozen=True)
class SelectedFile:
    """One selected file as the resolved commit holds it."""

    path: RepositoryPath
    selection: DefinitionSourceSelection
    document: bytes


@dataclass(frozen=True)
class ScannedSource:
    """What one write-free read of a source found, in byte-stable path order."""

    commit: SourceCommit
    files: tuple[SelectedFile, ...]


class DefinitionSourceReader(Protocol):
    """The provider-neutral owner of what a configured source currently holds."""

    def scan(self, configuration: DefinitionSourceConfiguration) -> ScannedSource:
        """Resolve the configured ref once and read every selected file of it."""
        ...


@dataclass(frozen=True)
class DefinitionSourceRegistered:
    revision: DefinitionSourceRevision


@dataclass(frozen=True)
class DefinitionSourceUnchanged:
    revision: DefinitionSourceRevision


@dataclass(frozen=True)
class DefinitionSourceFound:
    revision: DefinitionSourceRevision


@dataclass(frozen=True)
class DefinitionSourceMissing:
    source_id: DefinitionSourceId


type RegisterDefinitionSourceResult = (
    DefinitionSourceRegistered
    | DefinitionSourceUnchanged
    | DurableWriteUnavailable
    | DurableStateCorrupt
)
type ReadDefinitionSourceResult = (
    DefinitionSourceFound
    | DefinitionSourceMissing
    | DurableWriteUnavailable
    | DurableStateCorrupt
)
type ReadSourceIntakesResult = (
    Mapping[RepositoryPath, SourceIntake]
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class DefinitionSourceRegistry(Protocol):
    """What is registered and what it has delivered. Nothing here writes."""

    def read_source(self, source_id: DefinitionSourceId) -> ReadDefinitionSourceResult:
        """The newest configuration this source was registered with."""
        ...

    def latest_intakes(self, source_id: DefinitionSourceId) -> ReadSourceIntakesResult:
        """The highest durable intake of every path this source has delivered."""
        ...


class DefinitionSourceRegistrar(DefinitionSourceRegistry, Protocol):
    """The registry, plus the one door that adds to it."""

    def register(
        self, configuration: DefinitionSourceConfiguration
    ) -> RegisterDefinitionSourceResult:
        """Keep this configuration under the next number, or say it already stands.

        The number is this owner's to give: it is the count of what already
        stands under that source id, which no caller can know.
        """
        ...
