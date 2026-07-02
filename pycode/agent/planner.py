import re

from pycode.agent.types import AgentStep, AgentTask


TASK_DIFF_IMPACT = "diff-impact"
TASK_TEST_COVERAGE = "test-coverage"
TASK_TEST_FAILURE = "test-failure"
TASK_IMPACT = "impact"
TASK_GENERAL = "general"


def plan_task(task: AgentTask) -> list[AgentStep]:
    """Create a small deterministic plan for common development-analysis tasks."""
    text = task.description.lower()
    task.task_type = classify_task(task.description)
    steps: list[AgentStep] = []
    target_files = _extract_python_paths(task.description)
    primary_target = target_files[0] if target_files else None

    if _mentions_diff(text):
        steps.extend(
            [
                AgentStep(
                    tool="changed_files",
                    reason="Identify files changed in the current git diff.",
                ),
                AgentStep(
                    tool="git_diff",
                    reason="Read the current git diff as direct evidence.",
                ),
            ]
        )

    if primary_target:
        steps.append(
            AgentStep(
                tool="read_file",
                arguments={"file_path": primary_target},
                reason=f"Read the requested target file: {primary_target}.",
                required=False,
            )
        )

    if primary_target and _needs_impact_context(task.task_type, text):
        imports_args = {"query_type": "imports", "target": primary_target}
        imported_by_args = {"query_type": "imported-by", "target": primary_target}
        retrieval_args = {
            "question": task.description,
            "target": primary_target,
            "intent": "impact",
        }
        if task.graph_path is not None:
            imports_args["graph_path"] = task.graph_path
            imported_by_args["graph_path"] = task.graph_path
            retrieval_args["graph_path"] = task.graph_path
        steps.extend(
            [
                AgentStep(
                    tool="retrieve_context",
                    arguments=retrieval_args,
                    reason="Reuse stage-3 retrieval to select impact-analysis context.",
                    required=False,
                ),
                AgentStep(
                    tool="query_graph",
                    arguments=imports_args,
                    reason="Find files imported by the target file.",
                    required=False,
                ),
                AgentStep(
                    tool="query_graph",
                    arguments=imported_by_args,
                    reason="Find files that depend on the target file.",
                    required=False,
                ),
            ]
        )

    keyword = _extract_search_keyword(task.description)
    if keyword:
        steps.append(
            AgentStep(
                tool="search_code",
                arguments={"pattern": keyword},
                reason=f"Search code for task keyword: {keyword}.",
                required=False,
            )
        )

    if _needs_test_lookup(task.task_type, text):
        test_pattern = _test_search_pattern(task.description) or "def test_"
        steps.append(
            AgentStep(
                tool="search_code",
                arguments={
                    "pattern": test_pattern,
                    "include_globs": ["tests/*.py", "tests/**/*.py"],
                    "max_results": 30,
                },
                reason="Look for existing pytest tests related to the task.",
                required=False,
            )
        )
        if task.allow_tests:
            steps.append(
                AgentStep(
                    tool="run_tests",
                    arguments={
                        "test_paths": ["tests"],
                        "extra_args": [
                            "--basetemp=.pytest_tmp",
                            "--cache-clear",
                            "-o",
                            "cache_dir=.pytest_tmp/.pytest_cache",
                        ],
                    },
                    reason="Run the project test suite because test execution was allowed.",
                    required=False,
                )
            )

    if not steps:
        entry_args = {"query_type": "entry"}
        if task.graph_path is not None:
            entry_args["graph_path"] = task.graph_path
        steps.extend(
            [
                AgentStep(
                    tool="query_graph",
                    arguments=entry_args,
                    reason="Start with likely project entry points.",
                    required=False,
                ),
                AgentStep(
                    tool="search_code",
                    arguments={"pattern": _fallback_keyword(task.description)},
                    reason="Search for the most specific task keyword.",
                    required=False,
                ),
            ]
        )

    return steps[: task.max_steps]


def classify_task(description: str) -> str:
    text = description.lower()
    if _mentions_tests(text) and any(word in text for word in ["失败", "fail", "failed", "error", "报错"]):
        return TASK_TEST_FAILURE
    if any(word in text for word in ["覆盖", "coverage", "有没有测试", "是否有测试"]):
        return TASK_TEST_COVERAGE
    if _mentions_diff(text):
        return TASK_DIFF_IMPACT
    if _mentions_impact(text):
        return TASK_IMPACT
    return TASK_GENERAL


def _needs_impact_context(task_type: str, text: str) -> bool:
    return task_type in {TASK_DIFF_IMPACT, TASK_IMPACT} or _mentions_impact(text)


def _needs_test_lookup(task_type: str, text: str) -> bool:
    return task_type in {TASK_TEST_COVERAGE, TASK_TEST_FAILURE} or _mentions_tests(text)


def _test_search_pattern(description: str) -> str | None:
    paths = _extract_python_paths(description)
    if paths:
        name = paths[0].rsplit("/", 1)[-1].removesuffix(".py")
        return name
    keyword = _extract_search_keyword(description)
    return keyword


def _mentions_diff(text: str) -> bool:
    return any(word in text for word in ["git diff", "diff", "改动", "修改", "变更"])


def _mentions_impact(text: str) -> bool:
    return any(word in text for word in ["影响", "impact", "依赖", "调用", "修改", "改动"])


def _mentions_tests(text: str) -> bool:
    return any(word in text for word in ["测试", "test", "pytest", "覆盖", "失败"])


def _extract_python_paths(description: str) -> list[str]:
    matches = re.findall(r"[\w./\\-]+\.py", description)
    return [_normalize_path(match) for match in matches]


def _extract_search_keyword(description: str) -> str | None:
    for keyword in ["登录", "用户", "权限", "认证", "配置", "入口", "测试"]:
        if keyword in description:
            return keyword

    description_without_paths = re.sub(r"[\w./\\-]+\.py", " ", description)
    quoted = re.findall(r"[\"'“”‘’]([^\"'“”‘’]{2,})[\"'“”‘’]", description_without_paths)
    for item in quoted:
        if not item.endswith(".py"):
            return item.strip()

    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", description_without_paths):
        lowered = token.lower()
        if lowered not in {"git", "diff", "test", "pytest", "pycode"} and not lowered.endswith("py"):
            return token
    return None


def _fallback_keyword(description: str) -> str:
    keyword = _extract_search_keyword(description)
    if keyword:
        return keyword
    stripped = description.strip()
    return stripped[:30] if stripped else "main"


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


from pycode.agent.planner_enhanced import (  # noqa: E402,F401
    TASK_DEPENDENCY_QUESTION,
    TASK_DIFF_IMPACT,
    TASK_ENTRY_QUESTION,
    TASK_EXPLAIN_QUESTION,
    TASK_GENERAL,
    TASK_IMPACT,
    TASK_ONBOARD_QUESTION,
    TASK_TEST_COVERAGE,
    TASK_TEST_FAILURE,
    classify_task,
    plan_task,
)
