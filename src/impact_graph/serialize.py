"""领域对象的 JSON 友好序列化。"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date
from enum import Enum
from typing import Any


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (frozenset, set)):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    return value
