from __future__ import annotations

from pycode.agent.types import AgentResult
from pycode.constants import DEFAULT_MEMORY_DIR
from pycode.utils import dedupe_preserve_order


def collect_agent_evidence(result: AgentResult) -> list[str]:
    evidence: list[str] = []
    if result.memory is not None:
        for item in result.memory.relevant_memories:
            if item.path:
                evidence.append(f"{DEFAULT_MEMORY_DIR}/{item.path}")
        for item in result.memory.extracted_memories:
            if item.path:
                evidence.append(f"{DEFAULT_MEMORY_DIR}/{item.path}")
    for tool_result in result.tool_results:
        data = tool_result.data
        for item in data.get("evidence", []):
            evidence.append(str(item))
        for item in data.get("files", []):
            evidence.append(str(item))
        if data.get("path"):
            evidence.append(str(data["path"]))
        for item in data.get("matches", []):
            path = item.get("path")
            line_number = item.get("line_number")
            if path and line_number:
                evidence.append(f"{path}:{line_number}")
            elif path:
                evidence.append(str(path))
        for item in data.get("items", []):
            path = item.get("path")
            if path:
                evidence.append(str(path))
            evidence.extend(str(node_id) for node_id in item.get("node_ids", []))
            evidence.extend(str(edge) for edge in item.get("edges", []))
        for edge in data.get("edges", []):
            source = edge.get("source")
            edge_type = edge.get("type")
            target = edge.get("target")
            if source and edge_type and target:
                evidence.append(f"{source} --{edge_type}--> {target}")
        for node in data.get("nodes", []):
            node_id = node.get("id")
            if node_id:
                evidence.append(str(node_id))
    return dedupe_preserve_order(evidence)
