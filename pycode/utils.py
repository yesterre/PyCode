import json
import re
from collections.abc import Hashable, Iterable
from pathlib import Path
from typing import Any, TypeVar


T = TypeVar("T", bound=Hashable)


def dedupe_preserve_order(items: Iterable[T]) -> list[T]:
    seen: set[T] = set()
    result: list[T] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def normalize_path(path: str, *, strip_current_dir: bool = False) -> str:
    normalized = path.replace("\\", "/")
    if strip_current_dir:
        return normalized.lstrip("./")
    return normalized


def parse_json_array_response(response: str, *, allow_empty: bool = False) -> list[Any]:
    text = response.strip()
    if not text:
        if allow_empty:
            return []
        raise ValueError("Response was empty.")
    if not text.startswith("["):
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            raise ValueError("Response did not contain a JSON array.")
        text = match.group(0)
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Response JSON was not an array.")
    return data


def count_by_type(items: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        item_type = str(item.type)
        counts[item_type] = counts.get(item_type, 0) + 1
    return counts


def safe_read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
