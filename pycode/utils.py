from collections.abc import Hashable, Iterable
from typing import TypeVar


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
