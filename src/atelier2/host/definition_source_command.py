"""Connect a git definition source, and look at where it stands. Offline, both.

`connect` registers what the operator configured -- repository, ref, and which
paths carry which kind -- and takes nothing in; `scan` resolves the ref, reads
every selected file, and reports per path whether the catalog is behind. Taking
content in is its own operation, and `serve` performs neither at startup: a
newer version arrives because the operator asked for it (`#660` ruled line 2).

The store is named on the command line while installation-wide source
registration has no other home; a project-bundle slice owns that later, and
this flag is the temporary shape, not the intended one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import assert_never

from atelier2.adapters.dbos.definition_source_store import DbosDefinitionSources
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import UnsupportedSchemaVersion, initialize_schema
from atelier2.adapters.git_definition_source import GitDefinitionSource
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.limits import durable_projection_limit
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.application.scan_definition_source import (
    DefinitionSourceScanned,
    DefinitionSourceUnknown,
    ScannedDocumentInvalid,
    ScanRefused,
    scan_definition_source,
)
from atelier2.contracts.definition_sources import (
    DefinitionSourceAccess,
    DefinitionSourceActor,
    DefinitionSourceId,
    DefinitionSourceKind,
    DefinitionSourceRevision,
    DefinitionSourceSelection,
    RepositoryLocation,
    RepositoryRef,
    SelectionPattern,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.host.serving import api_limits
from atelier2.ports.definition_sources import (
    DefinitionSourceRegistered,
    DefinitionSourceUnchanged,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable

CONNECT_COMMAND = "connect"
SCAN_COMMAND = "scan"
_SELECTION_SEPARATOR = "="
_FIRST_REVISION_NUMBER = 1

DEFINITION_SOURCE_DESCRIPTION = """\
Register a git repository the catalog may take definitions out of, and look at
where it stands. Neither operation publishes anything.

  atelier2 definition-source connect --database atelier.sqlite \\
      --location /srv/definitions.git --ref refs/heads/main \\
      --select 'workflows/*.yaml=workflow' --actor felix

  atelier2 definition-source scan --database atelier.sqlite --source-id <id>

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


def execute_definition_source(parsed: argparse.Namespace) -> int:
    """Run the one operation the parser admitted, or say why nothing ran."""

    try:
        _require_store(parsed.database)
        if parsed.definition_source_command == CONNECT_COMMAND:
            return _connect(parsed)
        return _scan(parsed)
    except _CommandRefused as refused:
        return _refused(str(refused))


def _require_store(database: Path) -> None:
    if not database.is_file() or database.stat().st_size == 0:
        raise _CommandRefused(
            f"{database} is not a database file; this command does not create a store"
        )


def _connect(parsed: argparse.Namespace) -> int:
    revision = _configured(parsed)
    engine = create_canonical_engine(parsed.database)
    try:
        try:
            initialize_schema(engine)
        except UnsupportedSchemaVersion as refusal:
            raise _CommandRefused(str(refusal)) from refusal
        result = DbosDefinitionSources(engine).register(revision)
    finally:
        engine.dispose()
    match result:
        case DefinitionSourceRegistered(registered):
            print(
                f"connected {registered.kind.value} source "
                f"{registered.location.value!r} at {registered.ref.value!r} as "
                f"{registered.source_id.value} revision "
                f"{registered.revision_number}"
            )
            for selection in registered.selections:
                print(f"  {selection.pattern.value} -> {selection.kind.value}")
            return 0
        case DefinitionSourceUnchanged(standing):
            print(
                f"{standing.source_id.value} is already connected to "
                f"{standing.location.value!r} at {standing.ref.value!r}; revision "
                f"{standing.revision_number} is unchanged"
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
        case DefinitionSourceScanned(revision, commit, paths):
            print(
                f"{revision.source_id.value} at {revision.ref.value!r} "
                f"resolves to {commit.value}"
            )
            for scanned in paths:
                print(
                    f"  {scanned.freshness.value} {scanned.kind.value} "
                    f"{scanned.path.value}"
                )
            return 0
        case DefinitionSourceUnknown(unknown):
            return _refused(f"no definition source is registered as {unknown.value}")
        case ScanRefused(refusal, detail):
            return _refused(f"{refusal.value}: {detail}")
        case ScannedDocumentInvalid(path, detail, _refusal):
            return _refused(f"{path.value} would not be published: {detail}")
        case ReadUnavailable():
            return _refused("the store could not be read")
        case DurableStateCorrupt():
            return _refused("the store holds a definition source it cannot read back")
        case _ as unreachable:
            assert_never(unreachable)


def _configured(parsed: argparse.Namespace) -> DefinitionSourceRevision:
    try:
        return DefinitionSourceRevision(
            _FIRST_REVISION_NUMBER,
            DefinitionSourceKind.GIT,
            RepositoryLocation(parsed.location),
            RepositoryRef(parsed.ref),
            DefinitionSourceAccess.ANONYMOUS,
            DefinitionSourceActor(parsed.actor),
            tuple(_selection(declared) for declared in parsed.select),
        )
    except (TypeError, ValueError) as error:
        raise _CommandRefused(str(error)) from error


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


def _refused(detail: str) -> int:
    print(detail, file=sys.stderr)
    return 1
