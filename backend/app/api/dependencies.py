from collections.abc import Iterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.errors import BackendError
from backend.app.auth import unauthenticated
from backend.app.config import AuthSettings
from backend.app.core.models import User
from backend.app.repositories import InMemoryUnitOfWork, SqlAlchemyUnitOfWork
from backend.app.runtime import build_agent_run_service, build_project_service
from backend.app.services.agent_runs import AgentRunService
from backend.app.services.background_tasks import BackgroundTaskService
from backend.app.services.projects import ProjectService
from backend.app.services.auth import AuthService


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


def get_auth_service(
    request: Request, session: Session | None = Depends(get_db_session),
) -> AuthService:
    try:
        settings = request.app.state.auth_settings or AuthSettings.from_environment()
    except (ValueError, UnicodeError):
        raise BackendError("auth_unavailable", "Authentication is unavailable.") from None
    if request.app.state.persistence is not None:
        unit_of_work = InMemoryUnitOfWork(request.app.state.persistence)
    else:
        if session is None:  # pragma: no cover - dependency invariant
            raise RuntimeError("Database Session dependency is unavailable.")
        unit_of_work = SqlAlchemyUnitOfWork(session)
    return AuthService(unit_of_work, settings)


_bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth", bearerFormat="JWT")


def get_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None or len(credentials.credentials) > 4096:
        raise unauthenticated()
    return credentials.credentials


def get_current_user(
    token: str = Depends(get_bearer_token), service: AuthService = Depends(get_auth_service),
) -> User:
    return service.current_user(token)


def get_project_service(
    request: Request, session: Session | None = Depends(get_db_session),
) -> ProjectService:
    if request.app.state.persistence is not None:
        unit_of_work = InMemoryUnitOfWork(request.app.state.persistence)
    else:
        if session is None:  # pragma: no cover - dependency invariant
            raise RuntimeError("Database Session dependency is unavailable.")
        unit_of_work = SqlAlchemyUnitOfWork(session)
    return build_project_service(
        unit_of_work, adapter=request.app.state.adapter,
        workspace=request.app.state.workspace,
        operations=request.app.state.project_operations,
        artifacts=request.app.state.artifacts,
        admission=request.app.state.admission,
    )


def get_background_task_service(
    request: Request, session: Session | None = Depends(get_db_session),
) -> BackgroundTaskService:
    if request.app.state.persistence is not None:
        unit_of_work = InMemoryUnitOfWork(request.app.state.persistence)
    else:
        if session is None:  # pragma: no cover - dependency invariant
            raise RuntimeError("Database Session dependency is unavailable.")
        unit_of_work = SqlAlchemyUnitOfWork(session)
    return BackgroundTaskService(
        unit_of_work,
        request.app.state.task_dispatcher,
        request.app.state.reconciliation_settings,
        admission=request.app.state.admission,
    )


def get_agent_run_service(
    request: Request, session: Session | None = Depends(get_db_session),
) -> AgentRunService:
    if request.app.state.persistence is not None:
        unit_of_work = InMemoryUnitOfWork(request.app.state.persistence)
    else:
        if session is None:  # pragma: no cover - dependency invariant
            raise RuntimeError("Database Session dependency is unavailable.")
        unit_of_work = SqlAlchemyUnitOfWork(session)
    background_tasks = BackgroundTaskService(
        unit_of_work,
        request.app.state.task_dispatcher,
        request.app.state.reconciliation_settings,
        admission=request.app.state.admission,
    )
    projects = build_project_service(
        unit_of_work,
        adapter=request.app.state.adapter,
        workspace=request.app.state.workspace,
        operations=request.app.state.project_operations,
        artifacts=request.app.state.artifacts,
    )
    return build_agent_run_service(
        unit_of_work,
        background_tasks=background_tasks,
        projects=projects,
        adapter=request.app.state.adapter,
        agent_workspace=request.app.state.agent_workspace,
    )
