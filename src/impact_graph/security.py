"""按角色裁剪供应商商业数据。

与 domain/contract.json 的角色对齐：

- 采购计划员：可见供应商身份、单价等全部商业数据；
- 质量工程师：可见供应商身份，不可见单价；
- 仓储管理员：不可见供应商身份与单价，仅见履约所需信息；
- 供应商：仅可见与本供应商相关的数据，且不可见其他供应商的商业字段。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import models

ROLE_PLANNER = "采购计划员"
ROLE_QUALITY = "质量工程师"
ROLE_WAREHOUSE = "仓储管理员"
ROLE_SUPPLIER = "供应商"
ROLES = (ROLE_PLANNER, ROLE_QUALITY, ROLE_WAREHOUSE, ROLE_SUPPLIER)

# 角色 -> 可见的敏感字段集合
_COMMERCIAL_FIELDS = frozenset({"supplier_id", "unit_price", "supplier_name"})
_SEE_SUPPLIER = frozenset({ROLE_PLANNER, ROLE_QUALITY})
_SEE_PRICE = frozenset({ROLE_PLANNER})


@dataclass(frozen=True)
class Viewer:
    """API 调用者身份。"""

    role: str
    supplier_id: Optional[str] = None  # 供应商角色必填其所属供应商

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"未知角色：{self.role}")
        if self.role == ROLE_SUPPLIER and not self.supplier_id:
            raise ValueError("供应商角色必须指定 supplier_id")


def _can_see(item: models.ImpactItem | models.BusinessRef, viewer: Viewer, field: str) -> bool:
    if viewer.role == ROLE_SUPPLIER:
        # 供应商只能看到自己单据上的商业字段
        return item.supplier_id is not None and item.supplier_id == viewer.supplier_id
    if field in ("supplier_id", "supplier_name"):
        return viewer.role in _SEE_SUPPLIER
    if field == "unit_price":
        return viewer.role in _SEE_PRICE
    return True


def redact_ref(ref: models.BusinessRef, viewer: Viewer) -> dict[str, Any]:
    data = models.to_data(ref)
    for field_name in _COMMERCIAL_FIELDS:
        if field_name in data and not _can_see(ref, viewer, field_name):
            data[field_name] = None
            data.setdefault("_redacted", []).append(field_name)
    return data


def redact_impact_item(item: models.ImpactItem, viewer: Viewer) -> dict[str, Any]:
    data = models.to_data(item)
    for field_name in ("supplier_id", "unit_price"):
        if not _can_see(item, viewer, field_name):
            data[field_name] = None
            data.setdefault("_redacted", []).append(field_name)
    return data


def redact_report(report: models.ImpactReport, viewer: Viewer) -> dict[str, Any]:
    return {
        "target_part_id": report.target_part_id,
        "replacement_part_id": report.replacement_part_id,
        "summary": report.summary(),
        "cycles": [models.to_data(c) for c in report.cycles],
        "direct": [redact_impact_item(i, viewer) for i in report.direct],
        "indirect": [redact_impact_item(i, viewer) for i in report.indirect],
    }


def redact_part(part: models.Part, viewer: Viewer, supplier_name: Optional[str] = None) -> dict[str, Any]:
    data = models.to_data(part)
    if viewer.role != ROLE_PLANNER:
        data["unit_cost"] = None
        data.setdefault("_redacted", []).append("unit_cost")
    if viewer.role == ROLE_SUPPLIER:
        data["supplier_ids"] = [
            s for s in part.supplier_ids if s == viewer.supplier_id
        ]
    elif viewer.role == ROLE_WAREHOUSE:
        data["supplier_ids"] = []
        data.setdefault("_redacted", []).append("supplier_ids")
    return data
