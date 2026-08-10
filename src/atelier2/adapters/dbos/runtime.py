from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from dbos import DBOS, DBOSConfig, SQLAlchemyDatasource
from sqlalchemy import event
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.schema import initialize_schema
from atelier2.adapters.dbos.workflow import QUEUE_NAME, register_durable_run_workflow

EXECUTOR_ID = "atelier2-local"
_SQLITE_LOCK_TIMEOUT_SECONDS = 30.0
_SQLITE_WAL_RETRY_SECONDS = 0.01
_SQLITE_RETRYABLE_ERRORS = frozenset((sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))


@dataclass(frozen=True)
class DbosRuntimeSettings:
    database_path: Path
    application_version: str

    def __post_init__(self) -> None:
        if not self.application_version.strip():
            raise ValueError("application_version must be nonempty")


def sqlite_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.resolve()}"


def create_canonical_engine(database_path: Path) -> Engine:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = sa.create_engine(
        sqlite_url(database_path),
        connect_args={
            "check_same_thread": False,
            "timeout": _SQLITE_LOCK_TIMEOUT_SECONDS,
        },
    )

    @event.listens_for(engine, "connect")
    def configure(connection: Any, _record: Any) -> None:
        connection.isolation_level = "IMMEDIATE"
        connection.execute(
            f"PRAGMA busy_timeout={int(_SQLITE_LOCK_TIMEOUT_SECONDS * 1000)}"
        )
        connection.execute("PRAGMA foreign_keys=ON")
        _establish_wal_journal_mode(connection)

    return engine


def _establish_wal_journal_mode(connection: Any) -> None:
    deadline = time.monotonic() + _SQLITE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        except sqlite3.OperationalError as error:
            if (
                error.sqlite_errorcode not in _SQLITE_RETRYABLE_ERRORS
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(_SQLITE_WAL_RETRY_SECONDS)
            continue
        if journal_mode != "wal":
            raise RuntimeError("canonical SQLite database requires WAL journal mode")
        return


class DbosRuntime:
    def __init__(self, settings: DbosRuntimeSettings) -> None:
        self.settings = settings
        self.engine = create_canonical_engine(settings.database_path)
        initialize_schema(self.engine)
        self.datasource = SQLAlchemyDatasource.create(
            sqlite_url(settings.database_path), engine=self.engine
        )
        register_durable_run_workflow(self.datasource)
        self._launched = False

    def _config(self) -> DBOSConfig:
        return {
            "name": "atelier2",
            "system_database_url": sqlite_url(self.settings.database_path),
            "system_database_engine": self.engine,
            "application_version": self.settings.application_version,
            "executor_id": EXECUTOR_ID,
            "use_listen_notify": False,
            "notification_listener_polling_interval_sec": 0.01,
        }

    def launch(self) -> None:
        DBOS(config=self._config())
        DBOS.launch()
        DBOS.register_queue(
            QUEUE_NAME, polling_interval_sec=0.05, on_conflict="always_update"
        )
        self._launched = True

    def initialize_storage(self) -> None:
        self.launch()
        DBOS.destroy(destroy_registry=True)
        self._launched = False
        register_durable_run_workflow(self.datasource)

    def close(self) -> None:
        DBOS.destroy(
            destroy_registry=True,
            workflow_completion_timeout_sec=1 if self._launched else 0,
        )
        self._launched = False
        self.engine.dispose()
