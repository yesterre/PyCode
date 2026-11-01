from __future__ import annotations

import os
from collections.abc import Callable
from threading import Lock
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.core.errors import BackendError
from backend.app.config import DatabaseSettings, load_environment


class DatabaseSessionManager:
    """Lazily own one Engine/sessionmaker while creating a Session per request."""

    def __init__(self, database_url: str | None = None) -> None:
        self._database_url = database_url
        self._engine: Engine | None = None
        self._session_factory: Callable[[], Session] | None = None
        self._guard = Lock()

    def open_session(self) -> Session:
        load_environment()
        if self._session_factory is None:
            with self._guard:
                if self._session_factory is None:
                    database_url = self._database_url or os.environ.get("DATABASE_URL")
                    if not database_url:
                        raise BackendError(
                            "database_unavailable",
                            "Database configuration is unavailable on the server.",
                        )
                    self._engine = create_engine(
                        database_url,
                        pool_pre_ping=True,
                        hide_parameters=True,
                        connect_args=_connect_args(database_url),
                    )
                    self._session_factory = sessionmaker(
                        bind=self._engine,
                        autoflush=False,
                        expire_on_commit=False,
                    )
        return self._session_factory()

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    def probe(self) -> bool:
        """Bounded, read-only schema check independent of the business pool."""
        engine = None
        try:
            url = make_url(self._database_url or require_database_url())
            if url.drivername != "postgresql+psycopg":
                return False
            options = url.query.get("options", "")
            engine = create_engine(url, poolclass=NullPool, hide_parameters=True, connect_args={
                "connect_timeout": 2,
                "options": f"{options} -c statement_timeout=1000 -c default_transaction_read_only=on",
            })
            with engine.connect() as connection:
                revisions = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
                return revisions == _schema_heads()
        except Exception:
            # No URL, password, SQL exception or traceback in public health output.
            return False
        finally:
            if engine is not None:
                engine.dispose()


@lru_cache(maxsize=1)
def _schema_heads() -> set[str]:
    from alembic.script import ScriptDirectory
    return set(ScriptDirectory(str(Path(__file__).resolve().parents[2] / "migrations")).get_heads())


def require_database_url(variable: str = "DATABASE_URL") -> str:
    load_environment()
    value = os.environ.get(variable)
    if not value:
        raise RuntimeError(f"{variable} is required.")
    return value


def _connect_args(database_url: str) -> dict[str, int]:
    """Bound PostgreSQL connect only; do not impose a statement timeout."""

    if not database_url.lower().startswith(("postgresql", "postgres:")):
        return {}
    return {
        "connect_timeout": DatabaseSettings.from_environment().connect_timeout_seconds,
    }
