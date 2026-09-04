"""The filesystem side of the auto-redeploy watcher's own visibility file.

The watcher (`scripts/auto_redeploy.sh`) writes this file beside the live
serve's own database on every tick that resolves to a success or a failure --
the same directory `candidate_store.py` and `project_source_credentials.py`
already place their own state in, so no second "where does this serve keep
its state" convention exists here. This adapter only ever reads it; writing
it stays the shell watcher's job, because the failure streak it counts is the
watcher's own, not the served process's.
"""

from __future__ import annotations

import json
from pathlib import Path

from atelier2.contracts.redeploy_status import RedeployStatus, RedeployStatusMalformed
from atelier2.contracts.when import RecordedAt
from atelier2.ports.redeploy_status import RedeployStatusReader

REDEPLOY_STATUS_FILE_NAME = "redeploy-status.json"
"""The one name this file has, derived beside the database like the root's
other adapter-owned paths (`CANDIDATE_STORE_DIRECTORY_NAME`,
`MANAGED_PROJECT_SOURCE_CREDENTIALS_DIRECTORY`)."""


def redeploy_status_path(database_path: Path) -> Path:
    """Where the watcher's visibility file lives, beside the live database."""

    return database_path.parent / REDEPLOY_STATUS_FILE_NAME


def filesystem_redeploy_status_reader(path: Path) -> RedeployStatusReader:
    """The reader `atelier2.host.serving` wires into `ApiPorts` for real."""

    def read() -> RedeployStatus | RedeployStatusMalformed | None:
        return read_redeploy_status(path)

    return read


def read_redeploy_status(path: Path) -> RedeployStatus | RedeployStatusMalformed | None:
    """Read the watcher's visibility file, or `None` when it has never ticked.

    Answers `RedeployStatusMalformed` for a file that exists but does not
    parse as the watcher's own shape -- including a failure streak recorded
    without the failure it belongs to -- instead of silently reading it the
    same as "no file yet".
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return RedeployStatusMalformed()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError("redeploy status file is not a JSON object")
        status = RedeployStatus(
            failure_count=_require_non_negative_int(payload, "failure_count"),
            last_failure_reason=_optional_text(payload, "last_failure_reason"),
            last_failure_at=_optional_recorded_at(payload, "last_failure_at"),
            last_success_commit=_optional_text(payload, "last_success_commit"),
            last_success_at=_optional_recorded_at(payload, "last_success_at"),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return RedeployStatusMalformed()
    if not _is_consistent(status):
        return RedeployStatusMalformed()
    return status


def _is_consistent(status: RedeployStatus) -> bool:
    if (status.last_failure_reason is None) != (status.last_failure_at is None):
        return False
    if (status.last_success_commit is None) != (status.last_success_at is None):
        return False
    return not (status.failure_count > 0 and status.last_failure_at is None)


def _require_non_negative_int(payload: dict[str, object], field: str) -> int:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must not be negative")
    return value


def _optional_text(payload: dict[str, object], field: str) -> str | None:
    value = payload[field]
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise TypeError(f"{field} must be non-empty text or null")
    return value


def _optional_recorded_at(payload: dict[str, object], field: str) -> str | None:
    value = _optional_text(payload, field)
    if value is None:
        return None
    return RecordedAt(value).value
