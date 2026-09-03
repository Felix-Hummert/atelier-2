"""Registering a definition source: answer for it first, then keep it.

The location and the ref are resolved before anything is written, and only
those two: there is no disconnect yet, so a registration pointing at no
repository or at no ref would stand forever as a wire to nowhere. What a
*selection* claims is not this door's question -- configuring a pattern before
the files exist is ordinary, and the scan is where it shows.

Like `scan_definition_source` next door, this answers with a result rather than
a printed line or an exit code: the surface that asked decides how to say it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.definition_sources import (
    DefinitionSourceConfiguration,
    DefinitionSourceRefusal,
)
from atelier2.ports.definition_sources import (
    DefinitionSourceReader,
    DefinitionSourceRegistered,
    DefinitionSourceRegistrar,
    DefinitionSourceUnchanged,
    DefinitionSourceUnreadable,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable


@dataclass(frozen=True)
class ConnectRefused:
    """The source would not answer, in its own closed vocabulary."""

    refusal: DefinitionSourceRefusal
    detail: str


type ConnectDefinitionSourceResult = (
    DefinitionSourceRegistered
    | DefinitionSourceUnchanged
    | ConnectRefused
    | WriteUnavailable
    | DurableStateCorrupt
)


def connect_definition_source(
    configuration: DefinitionSourceConfiguration,
    reader: DefinitionSourceReader,
    registrar: DefinitionSourceRegistrar,
) -> ConnectDefinitionSourceResult:
    """Verify the source answers, then record it, and say which of the two happened."""

    try:
        reader.resolve(configuration)
    except DefinitionSourceUnreadable as refused:
        return ConnectRefused(refused.refusal, refused.detail)
    match registrar.register(configuration):
        case DefinitionSourceRegistered() | DefinitionSourceUnchanged() as answered:
            return answered
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
