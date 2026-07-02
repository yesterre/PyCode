from pathlib import Path

from pycode.agent import AgentTask, classify_task, plan_task


def test_plan_task_for_git_diff_starts_with_diff_tools() -> None:
    task = AgentTask(
        description="分析当前 git diff 是否影响登录逻辑",
        project_path=Path("."),
    )

    steps = plan_task(task)

    assert [step.tool for step in steps[:3]] == [
        "changed_files",
        "git_diff",
        "search_code",
    ]
    assert steps[2].arguments["pattern"] == "登录"
    assert task.task_type == "diff-impact"


def test_plan_task_for_target_file_impact_queries_graph() -> None:
    task = AgentTask(
        description="检查 services/user_service.py 的改动影响",
        project_path=Path("."),
    )

    steps = plan_task(task)

    assert [step.tool for step in steps] == [
        "changed_files",
        "git_diff",
        "read_file",
        "retrieve_context",
        "query_graph",
        "query_graph",
    ]
    assert steps[2].arguments["file_path"] == "services/user_service.py"
    assert steps[3].arguments == {
        "question": "检查 services/user_service.py 的改动影响",
        "target": "services/user_service.py",
        "intent": "impact",
    }
    assert steps[4].arguments == {
        "query_type": "imports",
        "target": "services/user_service.py",
    }
    assert steps[5].arguments == {
        "query_type": "imported-by",
        "target": "services/user_service.py",
    }
    assert task.task_type == "diff-impact"


def test_plan_task_adds_run_tests_only_when_allowed() -> None:
    no_tests_task = AgentTask(
        description="运行测试并总结失败原因",
        project_path=Path("."),
        allow_tests=False,
    )
    tests_task = AgentTask(
        description="运行测试并总结失败原因",
        project_path=Path("."),
        allow_tests=True,
    )

    no_tests_steps = plan_task(no_tests_task)
    tests_steps = plan_task(tests_task)

    assert "run_tests" not in [step.tool for step in no_tests_steps]
    assert "run_tests" in [step.tool for step in tests_steps]
    assert no_tests_task.task_type == "test-failure"
    assert tests_task.task_type == "test-failure"


def test_classify_task_distinguishes_stage_four_task_types() -> None:
    assert classify_task("分析当前 git diff 是否影响登录逻辑") == "diff-impact"
    assert classify_task("检查 services/user_service.py 的测试覆盖") == "test-coverage"
    assert classify_task("运行测试并总结失败原因") == "test-failure"
    assert classify_task("这个项目入口在哪里") == "entry-question"
