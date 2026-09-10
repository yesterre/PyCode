from __future__ import annotations

import os
from collections.abc import Callable
from threading import Lock

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.errors import BackendError
from backend.app.config import load_environment


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


def require_database_url(variable: str = "DATABASE_URL") -> str:
    load_environment()
    value = os.environ.get(variable)
    if not value:
        raise RuntimeError(f"{variable} is required.")
    return value
