from datetime import datetime, timezone
from uuid import UUID, uuid4

from backend.app.db.models import AgentRunORM
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b03_project_ownership import actor, create
from test_b07_worker_isolation import mutate
from test_postgresql import postgresql_engine


def test_history_keeps_tie_order_visibility_pagination_and_owner_boundaries(auth_context):
    context = auth_context
    _, alice = actor(context, "alice")
    _, bob = actor(context, "bob")
    own, sibling, foreign = [UUID(create(context, headers)["id"]) for headers in [alice, alice, bob]]
    eligible = []
    cases = [("completed", kind, "answer") for kind in ["ask", "impact", "agent"]] * 3
    cases += [("queued", "agent", None), ("running", "ask", None), ("failed", "agent", None),
              ("completed", "unknown", "hidden"), ("completed", "ask", None)]
    with context.uow_factory() as uow, uow.transaction():
        for project in [own, sibling, foreign]:
            for status, kind, answer in cases:
                run = (uow.agent_runs.create_queued if status == "queued" else uow.agent_runs.create)(
                    project, None, task=f"private-{project}", run_type=kind, model="internal-model",
                )
                if status == "completed":
                    run = uow.agent_runs.complete(run.id, answer)
                elif status == "failed":
                    uow.agent_runs.fail(run.id, "safe failure")
                mutate(uow, "agent_runs", AgentRunORM, run.id,
                       created_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
                if project == own and status == "completed" and kind != "unknown" and answer is not None:
                    eligible.append(run.id)
    expected = [str(identifier) for identifier in sorted(eligible, reverse=True)]
    url = f"/api/v1/projects/{own}/analysis-history"
    for limit, offset in [(1, 0), (2, 0), (2, 2), (2, 8), (20, 0), (100, 0), (20, 9000)]:
        response = context.client.get(url, headers=alice, params={"limit": limit, "offset": offset})
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["id"] for item in body["items"]] == expected[offset:offset + limit]
        assert body["has_more"] == (offset + limit < len(expected))
        assert all(set(item) == {"id", "created_at", "mode", "question", "answer"} for item in body["items"])
        assert all(item["question"] == f"private-{own}" for item in body["items"])
    first = context.client.get(url, headers=alice).json()
    assert context.client.get(url, headers=alice).json() == first
    assert context.client.post(f"/api/v1/projects/{own}/archive", headers=alice).status_code == 200
    assert context.client.get(url, headers=alice).json() == first
    for target in [own, uuid4()]:
        assert context.client.get(f"/api/v1/projects/{target}/analysis-history", headers=bob).status_code == 404
    assert context.client.get(url).status_code == 401
    for params in [{"limit": 0}, {"limit": 101}, {"offset": -1}]:
        assert context.client.get(url, headers=alice, params=params).status_code == 422
    sibling_items = context.client.get(f"/api/v1/projects/{sibling}/analysis-history", headers=alice).json()["items"]
    assert {item["id"] for item in sibling_items}.isdisjoint(expected)
