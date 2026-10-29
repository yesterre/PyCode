"""Trusted offline ownership assignment: python -m backend.manage_project_owner --help."""
import argparse
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from backend.app.core.errors import BackendError
from backend.app.core.models import UserStatus
from backend.app.db.session import DatabaseSessionManager
from backend.app.repositories import SqlAlchemyUnitOfWork, UnitOfWork


def assign_project_owner(
    unit_of_work: UnitOfWork, project_id: UUID, owner_id: UUID, *, apply: bool = False,
) -> None:
    """Called only by a trusted operator with database access, never an HTTP user."""
    with unit_of_work.transaction():
        project = unit_of_work.projects.get(project_id)
        owner = unit_of_work.users.get(owner_id)
        if project.owner_id is not None or owner.status != UserStatus.ACTIVE:
            raise BackendError("database_conflict", "Legacy ownership assignment is not allowed.")
        if apply:
            unit_of_work.projects.assign_legacy_owner(project_id, owner_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assign one legacy project to an existing active user.")
    parser.add_argument("--project-id", required=True, type=UUID)
    parser.add_argument("--owner-id", required=True, type=UUID)
    parser.add_argument("--apply", action="store_true", help="Commit assignment; default is dry-run.")
    args = parser.parse_args(argv)
    database = DatabaseSessionManager()
    try:
        with database.open_session() as session:
            assign_project_owner(
                SqlAlchemyUnitOfWork(session), args.project_id, args.owner_id, apply=args.apply,
            )
    except (BackendError, SQLAlchemyError) as exc:
        code = exc.code if isinstance(exc, BackendError) else "database_unavailable"
        print(f"Ownership assignment rejected: {code}")
        return 1
    finally:
        database.dispose()
    action = "Applied" if args.apply else "Dry-run only"
    print(f"{action}: project_id={args.project_id} owner_id={args.owner_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
