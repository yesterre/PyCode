import re

from pycode.agent.types import AgentStep, AgentTask


TASK_ENTRY_QUESTION = "entry-question"
TASK_ONBOARD_QUESTION = "onboard-question"
TASK_EXPLAIN_QUESTION = "explain-question"
TASK_DEPENDENCY_QUESTION = "dependency-question"
TASK_DIFF_IMPACT = "diff-impact"
TASK_TEST_COVERAGE = "test-coverage"
TASK_TEST_FAILURE = "test-failure"
TASK_IMPACT = "impact-question"
TASK_GENERAL = "general-question"

ENTRY_WORDS = ("\u5165\u53e3", "\u542f\u52a8", "entry", "main")
ONBOARD_WORDS = (
    "\u9605\u8bfb\u987a\u5e8f",
    "\u4ece\u54ea\u5f00\u59cb",
    "\u5148\u770b",
    "\u65b0\u624b",
    "onboard",
    "reading order",
)
EXPLAIN_WORDS = ("\u89e3\u91ca", "\u8bf4\u660e", "\u505a\u4ec0\u4e48", "explain")
DEPENDENCY_WORDS = ("\u4f9d\u8d56", "\u8c03\u7528", "\u5173\u7cfb", "dependency", "import", "call")
IMPACT_WORDS = ("\u5f71\u54cd", "\u6539\u52a8", "\u4fee\u6539", "impact")
DIFF_WORDS = (
    "git diff",
    "diff",
    "\u6539\u52a8",
    "\u4fee\u6539",
    "\u5f53\u524d\u6539\u52a8",
    "\u5f53\u524d\u4fee\u6539",
)
TEST_WORDS = ("\u6d4b\u8bd5", "test", "pytest")
TEST_FAILURE_WORDS = ("\u5931\u8d25", "\u62a5\u9519", "fail", "failed", "error")
TEST_COVERAGE_WORDS = ("\u8986\u76d6", "\u6709\u6ca1\u6709\u6d4b\u8bd5", "coverage")
SEARCH_KEYWORDS = (
    "\u767b\u5f55",
    "\u7528\u6237",
    "\u6743\u9650",
    "\u8ba4\u8bc1",
    "\u914d\u7f6e",
    "\u5165\u53e3",
    "\u6d4b\u8bd5",
)


def plan_task(task: AgentTask) -> list[AgentStep]:
    """Create a deterministic tool plan that can be consumed by the runtime loop."""
    text = task.description.lower()
    task.task_type = classify_task(task.description)
    steps: list[AgentStep] = []
    target_files = _extract_python_paths(task.description)
    primary_target = target_files[0] if target_files else None
    retrieval_intents = _retrieval_intents(task.description, task.task_type, primary_target)

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

    keyword = _extract_search_keyword(task.description)
    keyword_consumed = False
    if keyword and _mentions_diff(text) and not primary_target and _should_search_code(task.task_type, text):
        steps.append(
            AgentStep(
                tool="search_code",
                arguments={"pattern": keyword},
                reason=f"Search code for task keyword: {keyword}.",
                required=False,
            )
        )
        keyword_consumed = True

    if primary_target and _needs_file_read(task.task_type, text):
        steps.append(
            AgentStep(
                tool="read_file",
                arguments={"file_path": primary_target},
                reason=f"Read the requested target file: {primary_target}.",
                required=False,
            )
        )

    for intent in retrieval_intents:
        steps.append(
            AgentStep(
                tool="retrieve_context",
                arguments=_retrieval_arguments(task, intent, primary_target),
                reason=f"Reuse stage-3 retrieval for {intent} context.",
                required=False,
            )
        )

    if primary_target and _needs_graph_relationships(task.task_type, text):
        steps.extend(_graph_relationship_steps(task, primary_target))

    if keyword and not keyword_consumed and _should_search_code(task.task_type, text):
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
        steps.append(
            AgentStep(
                tool="retrieve_context",
                arguments=_retrieval_arguments(task, "general", primary_target),
                reason="Use stage-3 retrieval as the general project-question fallback.",
                required=False,
            )
        )

    return steps[: task.max_steps]


def classify_task(description: str) -> str:
    text = description.lower()
    if _mentions_tests(text) and _contains_any(text, TEST_FAILURE_WORDS):
        return TASK_TEST_FAILURE
    if _contains_any(text, TEST_COVERAGE_WORDS):
        return TASK_TEST_COVERAGE
    if _mentions_diff(text):
        return TASK_DIFF_IMPACT
    if _contains_any(text, ONBOARD_WORDS):
        return TASK_ONBOARD_QUESTION
    if _contains_any(text, ENTRY_WORDS):
        return TASK_ENTRY_QUESTION
    if _contains_any(text, EXPLAIN_WORDS):
        return TASK_EXPLAIN_QUESTION
    if _contains_any(text, DEPENDENCY_WORDS):
        return TASK_DEPENDENCY_QUESTION
    if _contains_any(text, IMPACT_WORDS):
        return TASK_IMPACT
    return TASK_GENERAL


def _retrieval_intents(
    description: str,
    task_type: str,
    primary_target: str | None,
) -> list[str]:
    text = description.lower()
    intents: list[str] = []
    if _contains_any(text, ENTRY_WORDS):
        intents.append("entry")
    if _contains_any(text, ONBOARD_WORDS):
        intents.append("onboard")
    if task_type == TASK_EXPLAIN_QUESTION or (_contains_any(text, EXPLAIN_WORDS) and primary_target):
        intents.append("explain")
    if task_type == TASK_DEPENDENCY_QUESTION:
        intents.append("dependency")
    if task_type in {TASK_DIFF_IMPACT, TASK_IMPACT} or _contains_any(text, IMPACT_WORDS):
        intents.append("impact")
    if not intents and task_type == TASK_GENERAL:
        intents.append("general")
    return _dedupe(intents)


def _retrieval_arguments(
    task: AgentTask,
    intent: str,
    primary_target: str | None,
) -> dict:
    arguments = {"question": task.description, "intent": intent}
    if primary_target and intent in {"dependency", "explain", "impact"}:
        arguments["target"] = primary_target
    if task.graph_path is not None:
        arguments["graph_path"] = task.graph_path
    return arguments


def _graph_relationship_steps(task: AgentTask, primary_target: str) -> list[AgentStep]:
    imports_args = {"query_type": "imports", "target": primary_target}
    imported_by_args = {"query_type": "imported-by", "target": primary_target}
    if task.graph_path is not None:
        imports_args["graph_path"] = task.graph_path
        imported_by_args["graph_path"] = task.graph_path
    return [
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


def _needs_file_read(task_type: str, text: str) -> bool:
    return (
        task_type
        in {TASK_EXPLAIN_QUESTION, TASK_DEPENDENCY_QUESTION, TASK_DIFF_IMPACT, TASK_IMPACT}
        or _contains_any(text, EXPLAIN_WORDS + IMPACT_WORDS + DEPENDENCY_WORDS)
    )


def _needs_graph_relationships(task_type: str, text: str) -> bool:
    return (
        task_type in {TASK_DEPENDENCY_QUESTION, TASK_DIFF_IMPACT, TASK_IMPACT}
        or _contains_any(text, DEPENDENCY_WORDS + IMPACT_WORDS)
    )


def _needs_test_lookup(task_type: str, text: str) -> bool:
    return task_type in {TASK_TEST_COVERAGE, TASK_TEST_FAILURE} or _mentions_tests(text)


def _should_search_code(task_type: str, text: str) -> bool:
    if task_type in {TASK_ENTRY_QUESTION, TASK_ONBOARD_QUESTION, TASK_GENERAL}:
        return False
    return _mentions_diff(text) or task_type in {
        TASK_DEPENDENCY_QUESTION,
        TASK_EXPLAIN_QUESTION,
        TASK_IMPACT,
        TASK_TEST_COVERAGE,
        TASK_TEST_FAILURE,
    }


def _test_search_pattern(description: str) -> str | None:
    paths = _extract_python_paths(description)
    if paths:
        name = paths[0].rsplit("/", 1)[-1].removesuffix(".py")
        return name
    keyword = _extract_search_keyword(description)
    return keyword


def _mentions_diff(text: str) -> bool:
    return _contains_any(text, DIFF_WORDS)


def _mentions_tests(text: str) -> bool:
    return _contains_any(text, TEST_WORDS + TEST_COVERAGE_WORDS + TEST_FAILURE_WORDS)


def _extract_python_paths(description: str) -> list[str]:
    matches = re.findall(r"[\w./\\-]+\.py", description)
    return [_normalize_path(match) for match in matches]


def _extract_search_keyword(description: str) -> str | None:
    for keyword in SEARCH_KEYWORDS:
        if keyword in description:
            return keyword

    description_without_paths = re.sub(r"[\w./\\-]+\.py", " ", description)
    quoted = re.findall(r"[\"']([^\"']{2,})[\"']", description_without_paths)
    for item in quoted:
        if not item.endswith(".py"):
            return item.strip()

    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", description_without_paths):
        lowered = token.lower()
        if lowered not in {"git", "diff", "test", "pytest", "pycode"} and not lowered.endswith("py"):
            return token
    return None


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
