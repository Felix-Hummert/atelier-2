"""Connect a git definition source, look at where it stands, take it in. Offline.

`connect` resolves the repository and the ref the operator gave, then registers
what they configured -- and takes nothing in. It resolves first because there
is no way to disconnect a source yet, so a location or ref that answers nothing
would otherwise stand registered forever as a wire to nowhere; what a selection
claims is not its question, and surfaces at scan. `scan` resolves the ref, reads
every selected file, and reports per path whether the catalog is behind.
`intake` is the only one that publishes, it publishes the whole commit or none
of it, and `serve` performs none of the three at startup: a newer version
arrives because the operator asked for it (`#660` ruled line 2).

The store is named on the command line while installation-wide source
registration has no other home; a project-bundle slice owns that later, and
this flag is the temporary shape, not the intended one.
"""

from __future__ import annotations

import argparse
import sys
from enum import StrEnum
from pathlib import Path
from typing import assert_never

from atelier2.adapters.dbos.definition_source_store import DbosDefinitionSources
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import UnsupportedSchemaVersion, initialize_schema
from atelier2.adapters.git_definition_source import GitDefinitionSource
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.limits import durable_projection_limit
from atelier2.application.intake_definition_source import (
    DefinitionSourceIntaken,
    IntakeDefinitionSourceResult,
    IntakeNameUnusable,
    SourcePositionMoved,
    intake_definition_source,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.application.scan_definition_source import (
    DefinitionSourceScanned,
    DefinitionSourceUnknown,
    ScannedDocumentInvalid,
    ScanRefused,
    scan_definition_source,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogAdmissionNameHeld,
    CatalogAdmissionRetired,
    CatalogAdmissionRevisionOwned,
)
from atelier2.contracts.definition_sources import (
    DefinitionSourceAccess,
    DefinitionSourceActor,
    DefinitionSourceConfiguration,
    DefinitionSourceId,
    DefinitionSourceKind,
    DefinitionSourceRevision,
    DefinitionSourceSelection,
    RepositoryLocation,
    RepositoryRef,
    SelectionPattern,
    SourceCommit,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.host.run_command import catalog_activated_at
from atelier2.host.serving import api_limits
from atelier2.ports.definition_sources import (
    DefinitionSourceReader,
    DefinitionSourceRegistered,
    DefinitionSourceRegistrar,
    DefinitionSourceUnchanged,
    DefinitionSourceUnreadable,
    PathAlreadyInCatalog,
    PathIntaken,
    SourceIntakeConflict,
    SourceIntakeRefused,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable

CONNECT_COMMAND = "connect"
SCAN_COMMAND = "scan"
INTAKE_COMMAND = "intake"
_SELECTION_SEPARATOR = "="


class IntakeWord(StrEnum):
    """What `intake` says about one path, and the whole of what it can say.

    Three words for three outcomes: the bytes entered the catalog, the catalog
    already held them, or nothing of this commit entered at all. They are the
    operator's vocabulary for the operation, so they live here with the surface
    that speaks them rather than in a layer that has no reader.
    """

    INTAKEN = "aufgenommen"
    PRESENT = "vorhanden"
    REFUSED = "verweigert"


DEFINITION_SOURCE_DESCRIPTION = """\
Register a git repository the catalog may take definitions out of, look at
where it stands, and take one commit of it in. Only `intake` publishes, and it
publishes the whole commit or none of it.

  atelier2 definition-source connect --database atelier.sqlite \\
      --location /srv/definitions.git --ref refs/heads/main \\
      --select 'workflows/*.yaml=workflow' --actor felix

  atelier2 definition-source scan --database atelier.sqlite --source-id <id>

  atelier2 definition-source intake --database atelier.sqlite \\
      --source-id <id> --actor felix

A selection is PATTERN=KIND. The kind is configured, never guessed from the
repository's layout. The one wildcard is `*`, matching inside a single path
segment.
"""


class _CommandRefused(Exception):
    """What the operator gave does not make one command, said in one sentence."""


def add_definition_source_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Declare `definition-source` and its two operations on the host's parser."""

    parser = commands.add_parser(
        "definition-source",
        help="register a git definition source and see where it stands, offline",
        description=DEFINITION_SOURCE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    operations = parser.add_subparsers(dest="definition_source_command", required=True)
    connect = operations.add_parser(
        CONNECT_COMMAND, help="register a source; takes no content in"
    )
    connect.add_argument("--database", type=Path, required=True)
    connect.add_argument(
        "--location", required=True, help="where the git repository is"
    )
    connect.add_argument(
        "--ref", required=True, help="the ref every scan of this source resolves"
    )
    connect.add_argument(
        "--select",
        required=True,
        action="append",
        metavar="PATTERN=KIND",
        help="which paths carry which kind of document; repeatable",
    )
    connect.add_argument(
        "--actor", required=True, help="the operator accountable for this connect"
    )
    scan = operations.add_parser(
        SCAN_COMMAND, help="read a registered source; writes nothing"
    )
    scan.add_argument("--database", type=Path, required=True)
    scan.add_argument("--source-id", required=True)
    intake = operations.add_parser(
        INTAKE_COMMAND, help="take one commit of a registered source into the catalog"
    )
    intake.add_argument("--database", type=Path, required=True)
    intake.add_argument("--source-id", required=True)
    intake.add_argument(
        "--source-position",
        help="the commit a scan showed; the resolved ref when it is left out",
    )
    intake.add_argument(
        "--actor", required=True, help="the operator accountable for this intake"
    )


def execute_definition_source(parsed: argparse.Namespace) -> int:
    """Run the one operation the parser admitted, or say why nothing ran."""

    try:
        _require_store(parsed.database)
        if parsed.definition_source_command == CONNECT_COMMAND:
            return _connect(parsed)
        if parsed.definition_source_command == SCAN_COMMAND:
            return _scan(parsed)
        return _intake(parsed)
    except _CommandRefused as refused:
        return _refused(str(refused))


def _require_store(database: Path) -> None:
    if not database.is_file() or database.stat().st_size == 0:
        raise _CommandRefused(
            f"{database} is not a database file; this command does not create a store"
        )


def _connect(parsed: argparse.Namespace) -> int:
    """Build what this command depends on, then let the decision have it."""

    configuration = _configured(parsed)
    engine = create_canonical_engine(parsed.database)
    try:
        try:
            initialize_schema(engine)
        except UnsupportedSchemaVersion as refusal:
            raise _CommandRefused(str(refusal)) from refusal
        return connect_definition_source(
            configuration, GitDefinitionSource(), DbosDefinitionSources(engine)
        )
    finally:
        engine.dispose()


def connect_definition_source(
    configuration: DefinitionSourceConfiguration,
    reader: DefinitionSourceReader,
    registrar: DefinitionSourceRegistrar,
) -> int:
    """Verify the source answers, then record it, and say which of the two happened."""

    _verified(configuration, reader)
    result = registrar.register(configuration)
    match result:
        case DefinitionSourceRegistered(registered):
            configured = registered.configuration
            print(
                f"connected {configured.kind.value} source "
                f"{configured.location.value!r} at {configured.ref.value!r} as "
                f"{configured.source_id.value} revision "
                f"{registered.revision_number}"
            )
            for selection in configured.selections:
                print(f"  {selection.pattern.value} -> {selection.kind.value}")
            return 0
        case DefinitionSourceUnchanged(standing):
            configured = standing.configuration
            print(
                f"{configured.source_id.value} is already connected to "
                f"{configured.location.value!r} at {configured.ref.value!r}; "
                f"revision {standing.revision_number} is unchanged"
            )
            return 0
        case DurableWriteUnavailable():
            return _refused("the store could not be written")
        case PortDurableStateCorrupt():
            return _refused("the store holds a definition source it cannot read back")
        case _ as unreachable:
            assert_never(unreachable)


def _scan(parsed: argparse.Namespace) -> int:
    source_id = _source_id(parsed.source_id)
    engine = create_canonical_engine(parsed.database)
    try:
        try:
            initialize_schema(engine)
        except UnsupportedSchemaVersion as refusal:
            raise _CommandRefused(str(refusal)) from refusal
        result = scan_definition_source(
            source_id,
            DbosDefinitionSources(engine),
            GitDefinitionSource(),
            parse_workflow_document,
            durable_projection_limit(api_limits()),
        )
    finally:
        engine.dispose()
    match result:
        case DefinitionSourceScanned(revision, commit, paths, _):
            print(_resolution(revision, commit))
            for scanned in paths:
                print(
                    f"  {scanned.freshness.value} {scanned.kind.value} "
                    f"{scanned.path.value}"
                )
            return 0
        case DefinitionSourceUnknown() | ScanRefused() | ScannedDocumentInvalid():
            return _unread(result)
        case ReadUnavailable():
            return _refused("the store could not be read")
        case DurableStateCorrupt():
            return _refused("the store holds a definition source it cannot read back")
        case _ as unreachable:
            assert_never(unreachable)


def _intake(parsed: argparse.Namespace) -> int:
    source_id = _source_id(parsed.source_id)
    position = _source_position(parsed.source_position)
    actor = _catalog_actor(parsed.actor)
    engine = create_canonical_engine(parsed.database)
    try:
        try:
            initialize_schema(engine)
        except UnsupportedSchemaVersion as refusal:
            raise _CommandRefused(str(refusal)) from refusal
        result = intake_definition_source(
            source_id,
            position,
            actor,
            CatalogActivatedAt(catalog_activated_at()),
            DbosDefinitionSources(engine),
            GitDefinitionSource(),
            parse_workflow_document,
            durable_projection_limit(api_limits()),
        )
    finally:
        engine.dispose()
    return _intake_report(result)


def _intake_report(result: IntakeDefinitionSourceResult) -> int:
    """One line per path, in the three words this operation has for a path."""

    match result:
        case DefinitionSourceIntaken(revision, commit, paths):
            print(_resolution(revision, commit))
            for entered in paths:
                match entered:
                    case PathIntaken(intake):
                        print(
                            f"  {IntakeWord.INTAKEN.value} "
                            f"{intake.revision_kind.value} {intake.path.value}"
                        )
                    case PathAlreadyInCatalog(path, kind, _):
                        print(f"  {IntakeWord.PRESENT.value} {kind.value} {path.value}")
                    case _ as unreachable:
                        assert_never(unreachable)
            return 0
        case SourceIntakeRefused(path, conflict):
            return _refused(
                f"{IntakeWord.REFUSED.value} {path.value}: {_conflict(conflict)}"
            )
        case IntakeNameUnusable(path, name):
            return _refused(
                f"{IntakeWord.REFUSED.value} {path.value}: {name!r} is no catalog "
                "name, which must match [a-z][a-z0-9._-]*"
            )
        case SourcePositionMoved(asked, resolved):
            return _refused(
                f"the ref stands at {resolved.value}, not at the {asked.value} "
                "this intake was asked for"
            )
        case DefinitionSourceUnknown() | ScanRefused() | ScannedDocumentInvalid():
            return _unread(result)
        case WriteUnavailable():
            return _refused("the store could not be written")
        case DurableStateCorrupt():
            return _refused("the store holds catalog state it cannot read back")
        case _ as unreachable:
            assert_never(unreachable)


def _conflict(conflict: SourceIntakeConflict) -> str:
    """Why the catalog would not admit one path, in the catalog's own terms."""

    match conflict:
        case CatalogAdmissionNameHeld(name, holder):
            return f"the name {name.value!r} is already held by lineage {holder.value}"
        case CatalogAdmissionRevisionOwned(_, owner):
            return f"these exact bytes already belong to lineage {owner.value}"
        case CatalogAdmissionRetired(lineage_id):
            return f"lineage {lineage_id.value} is retired"
        case _ as unreachable:
            assert_never(unreachable)


def _resolution(revision: DefinitionSourceRevision, commit: SourceCommit) -> str:
    return (
        f"{revision.configuration.source_id.value} at "
        f"{revision.configuration.ref.value!r} resolves to {commit.value}"
    )


def _unread(
    result: DefinitionSourceUnknown | ScanRefused | ScannedDocumentInvalid,
) -> int:
    """Why reading the source produced nothing to act on, said once for both doors."""

    match result:
        case DefinitionSourceUnknown(unknown):
            return _refused(f"no definition source is registered as {unknown.value}")
        case ScanRefused(refusal, detail):
            return _refused(f"{refusal.value}: {detail}")
        case ScannedDocumentInvalid(path, detail, refusal):
            named = (
                ""
                if refusal is None
                else f" ({refusal.reason.value} at {refusal.field!r})"
            )
            return _refused(f"{path.value} would not be published{named}: {detail}")
        case _ as unreachable:
            assert_never(unreachable)


def _configured(parsed: argparse.Namespace) -> DefinitionSourceConfiguration:
    try:
        return DefinitionSourceConfiguration(
            DefinitionSourceKind.GIT,
            RepositoryLocation(parsed.location),
            RepositoryRef(parsed.ref),
            DefinitionSourceAccess.ANONYMOUS,
            DefinitionSourceActor(parsed.actor),
            tuple(_selection(declared) for declared in parsed.select),
        )
    except (TypeError, ValueError) as error:
        raise _CommandRefused(str(error)) from error


def _verified(
    configuration: DefinitionSourceConfiguration, reader: DefinitionSourceReader
) -> None:
    """Answer for the location and the ref before recording either.

    Only those two: there is no disconnect yet, so a registration pointing at
    no repository or no ref would stand forever -- while a selection that
    matches nothing today is an ordinary thing to configure before the files
    exist, and the scan is where it shows.
    """

    try:
        reader.resolve(configuration)
    except DefinitionSourceUnreadable as refused:
        raise _CommandRefused(f"{refused.refusal.value}: {refused.detail}") from refused


def _selection(declared: str) -> DefinitionSourceSelection:
    pattern, separator, kind = declared.rpartition(_SELECTION_SEPARATOR)
    if not separator:
        raise _CommandRefused(
            f"{declared!r} is no selection; write PATTERN{_SELECTION_SEPARATOR}KIND"
        )
    try:
        return DefinitionSourceSelection(SelectionPattern(pattern), RevisionKind(kind))
    except ValueError as error:
        raise _CommandRefused(f"{declared!r}: {error}") from error


def _source_id(declared: str) -> DefinitionSourceId:
    try:
        return DefinitionSourceId(declared)
    except ValueError as error:
        raise _CommandRefused(f"{declared!r} is no definition source id") from error


def _source_position(declared: str | None) -> SourceCommit | None:
    if declared is None:
        return None
    try:
        return SourceCommit(declared)
    except ValueError as error:
        raise _CommandRefused(f"{declared!r} is no git commit") from error


def _catalog_actor(declared: str) -> CatalogActor:
    try:
        return CatalogActor(declared)
    except (TypeError, ValueError) as error:
        raise _CommandRefused(f"{declared!r} is no catalog actor: {error}") from error


def _refused(detail: str) -> int:
    print(detail, file=sys.stderr)
    return 1
