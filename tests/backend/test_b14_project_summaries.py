from uuid import UUID, uuid4

import pytest
from sqlalchemy import event

from backend.app.core.errors import BackendError
from backend.app.core.models import IndexSummary
from backend.app.db.models import ProjectORM, ProjectSnapshotORM
from backend.app.repositories import InMemoryUnitOfWork
from backend.app.runtime import build_project_service
from scripts.benchmarks.b14_load import frozen_list_owned_page
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b03_project_ownership import actor
from test_b07_worker_isolation import mutate
from test_postgresql import postgresql_engine


REPO = "https://github.com/example/demo.git"


def data(context, *, snapshots=True):
    owner, headers = actor(context, "alice")
    other, other_headers = actor(context, "bob")
    rows = []
    with context.uow_factory() as uow, uow.transaction():
        for number in range(60):
            project = uow.projects.create(str(number), REPO, owner_id=owner)
            snapshot = None
            if snapshots:
                snapshot = uow.snapshots.upsert(project.id, "a" * 40, index_artifact_path="index.json",
                    graph_artifact_path="graph.json", summary=IndexSummary(number + 1, number + 2, number + 3))
                project = uow.projects.set_current_snapshot(project.id, snapshot.id)
            rows.append((project, snapshot))
        uow.projects.set_archived(rows[0][0].id, owner, archived=True)
        foreign = uow.projects.create("foreign", REPO, owner_id=other)
        foreign_snapshot = uow.snapshots.upsert(foreign.id, "a" * 40, index_artifact_path="private-index.json",
            graph_artifact_path="private-graph.json", summary=IndexSummary(777, 888, 999))
        uow.projects.set_current_snapshot(foreign.id, foreign_snapshot.id)
        uow.projects.create("legacy", REPO)
    return owner, headers, other_headers, rows, foreign_snapshot


def service(context, uow):
    app = context.app
    return build_project_service(uow, adapter=app.state.adapter, workspace=app.state.workspace,
                                 artifacts=app.state.artifacts, operations=app.state.project_operations)


@pytest.mark.parametrize("snapshots", [True, False])
def test_list_summaries_match_baseline_without_query_fanout(auth_context, snapshots):
    context = auth_context
    owner, headers, other_headers, rows, foreign = data(context, snapshots=snapshots)
    engine = context.app.state.database._engine if context.app.state.database else None
    for limit, offset, archived in [(1, 0, False), (20, 0, False), (50, 0, False), (100, 20, False),
                                    (20, 100, False), (20, 0, True), (100, 0, None)]:
        with context.uow_factory() as uow:
            expected, has_more = frozen_list_owned_page(service(context, uow), owner, limit, offset, archived=archived)
        statements = []

        def capture(connection, cursor, statement, *args):
            statements.append(statement)

        if engine is not None:
            event.listen(engine, "before_cursor_execute", capture)
        try:
            response = context.client.get("/api/v1/projects", headers=headers, params={
                "limit": limit, "offset": offset, "archive_status": {False: "active", True: "archived", None: "all"}[archived]})
        finally:
            if engine is not None:
                event.remove(engine, "before_cursor_execute", capture)
        assert response.status_code == 200
        body = response.json()
        assert body["has_more"] == has_more
        assert [row["id"] for row in body["projects"]] == [str(project.id) for project in expected]
        for actual, project in zip(body["projects"], expected):
            assert actual["index_summary"] == (vars(project.index_summary) if project.index_summary else None)
            assert "owner_id" not in actual and actual["name"] == project.name
        if engine is not None:
            # One current-user check, one owned page, at most one Snapshot batch.
            assert len(statements) == 2 + int(bool(expected) and snapshots)
    assert context.client.get("/api/v1/projects").status_code == 401
    foreign_page = context.client.get("/api/v1/projects", headers=other_headers).json()["projects"]
    assert len(foreign_page) == 1 and foreign_page[0]["index_summary"]["file_count"] == 777
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert context.client.get("/api/v1/projects", headers=headers, params=params).status_code == 422


def test_batch_snapshots_preserve_duplicate_empty_and_missing_semantics(auth_context):
    context = auth_context
    _, _, _, rows, _ = data(context)
    identifiers = [snapshot.id for _, snapshot in rows[:2]]
    with context.uow_factory() as uow, uow.transaction():
        assert uow.snapshots.get_many([]) == {}
        assert uow.snapshots.get_many([*identifiers, identifiers[0]]) == {
            identifier: uow.snapshots.get(identifier) for identifier in identifiers}
    with context.uow_factory() as uow, pytest.raises(BackendError) as error, uow.transaction():
        uow.snapshots.get_many([identifiers[0], uuid4()])
    assert error.value.code == "snapshot_not_found"


def test_batch_summary_keeps_foreign_and_unready_metadata_private(auth_context):
    context = auth_context
    owner, headers, _, rows, foreign = data(context)
    with context.uow_factory() as uow, uow.transaction():
        mutate(uow, "projects", ProjectORM, rows[1][0].id, current_snapshot_id=foreign.id)
        mutate(uow, "snapshots", ProjectSnapshotORM, rows[2][1].id, status="indexing", file_count=None)
    with context.uow_factory() as uow:
        before, _ = frozen_list_owned_page(service(context, uow), owner, 100, 0, archived=None)
    response = context.client.get("/api/v1/projects?limit=100&archive_status=all", headers=headers)
    assert response.status_code == 200
    result = {UUID(row["id"]): row["index_summary"] for row in response.json()["projects"]}
    assert result[rows[1][0].id] is None and result[rows[2][0].id] is None
    assert result == {project.id: vars(project.index_summary) if project.index_summary else None for project in before}
