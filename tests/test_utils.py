from dataclasses import dataclass
from pathlib import Path

import pytest

from pycode.utils import (
    count_by_type,
    ensure_directory,
    parse_json_array_response,
    safe_read_text,
)


@dataclass
class TypedItem:
    type: str


def test_parse_json_array_response_accepts_plain_array() -> None:
    assert parse_json_array_response('[{"tool": "read_file"}]') == [
        {"tool": "read_file"}
    ]


def test_parse_json_array_response_extracts_array_from_wrapped_text() -> None:
    assert parse_json_array_response('Result:\n["a", "b"]') == ["a", "b"]


def test_parse_json_array_response_handles_empty_when_allowed() -> None:
    assert parse_json_array_response("", allow_empty=True) == []


def test_parse_json_array_response_rejects_empty_by_default() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_json_array_response("")


def test_parse_json_array_response_requires_array() -> None:
    with pytest.raises(ValueError, match="array"):
        parse_json_array_response('{"name": "not-array"}')


def test_count_by_type_counts_objects_with_type_attribute() -> None:
    items = [TypedItem("file"), TypedItem("class"), TypedItem("file")]
    assert count_by_type(items) == {"file": 2, "class": 1}


def test_safe_read_text_uses_utf8_sig(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("\ufeffhello", encoding="utf-8")

    assert safe_read_text(path) == "hello"


def test_ensure_directory_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir"

    ensure_directory(target)

    assert target.is_dir()
