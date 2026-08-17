"""Raise an existing canonical store to the current product schema, offline.

This command does not serve, does not start a runtime, and does not create a
store. It opens the named SQLite file, walks the published step ladder, and
either commits one complete hop or leaves the file unaltered. Runtime startup
still refuses every predecessor; this is the tool that refusal names.
"""

from __future__ import annotations

from pathlib import Path

from atelier2.adapters.dbos.schema import StoreMigrationReport, migrate_store


def describe_migration(report: StoreMigrationReport) -> str:
    if report.already_current:
        return (
            f"schema version {report.target_version} is already current\n"
            f"  fingerprint {report.fingerprint_sha256}\n"
            "  nothing to migrate"
        )
    lines = [
        f"migrated schema version {report.source_version} to {report.target_version}"
    ]
    for source_version, target_version, fingerprint in report.steps:
        lines.append(
            f"  step {source_version} -> {target_version}  fingerprint {fingerprint}"
        )
    return "\n".join(lines)


def execute_migrate(database_path: Path) -> StoreMigrationReport:
    return migrate_store(database_path)
