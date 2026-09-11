from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.errors import BackendError
from backend.app.repositories import InMemoryUnitOfWork, SqlAlchemyUnitOfWork
from backend.app.services.projects import ProjectService


def get_db_session(request: Request) -> Iterator[Session | None]:
    if request.app.state.persistence is not None:
        yield None
        return
    try:
        session = request.app.state.database.open_session()
    except BackendError:
        raise
    except SQLAlchemyError as exc:
        raise BackendError("database_unavailable", "The database is unavailable.") from exc
    try:
        yield session
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()


def get_project_service(
    request: Request, session: Session | None = Depends(get_db_session),
) -> ProjectService:
    if request.app.state.persistence is not None:
        unit_of_work = InMemoryUnitOfWork(request.app.state.persistence)
    else:
        if session is None:  # pragma: no cover - dependency invariant
            raise RuntimeError("Database Session dependency is unavailable.")
        unit_of_work = SqlAlchemyUnitOfWork(session)
    return ProjectService(
        unit_of_work,
        request.app.state.adapter,
        workspace=request.app.state.workspace,
        operations=request.app.state.project_operations,
        artifacts=request.app.state.artifacts,
    )
