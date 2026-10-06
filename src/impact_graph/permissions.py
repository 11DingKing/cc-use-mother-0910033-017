"""按角色裁剪供应商商业数据。"""
from __future__ import annotations

from .models import BusinessRef
from .serialize import to_jsonable

ROLE_BUYER = "采购计划员"
ROLE_SUPPLIER = "供应商"
ROLE_QUALITY = "质量工程师"
ROLE_WAREHOUSE = "仓储管理员"

ROLES = (ROLE_BUYER, ROLE_SUPPLIER, ROLE_QUALITY, ROLE_WAREHOUSE)
# 写操作仅采购计划员与质量工程师可用
WRITE_ROLES = (ROLE_BUYER, ROLE_QUALITY)

# 供应商商业数据：价格与合同条款
COMMERCIAL_FIELDS = ("unit_price", "contract_terms")
MASK = "已隐藏"


class AccessDenied(PermissionError):
    """角色缺失或越权。"""


def require_role(role: str) -> None:
    if role not in ROLES:
        raise AccessDenied(f"未知角色：{role or '（未提供）'}")


def require_write_role(role: str) -> None:
    require_role(role)
    if role not in WRITE_ROLES:
        raise AccessDenied(f"{role} 没有写入权限")


def ref_view(ref: BusinessRef, role: str, supplier_id: str | None = None) -> dict | None:
    """按角色返回业务引用视图。

    采购计划员可见全部；仓储管理员隐藏价格与合同；质量工程师额外隐藏供应商身份；
    供应商只能看到自己名下的单据，其余返回 None 表示不可见。
    """
    require_role(role)
    if role == ROLE_SUPPLIER:
        if not supplier_id or ref.supplier_id != supplier_id:
            return None
        view = to_jsonable(ref)
        view["redacted_fields"] = []
        return view
    view = to_jsonable(ref)
    redacted: list[str] = []
    if role in (ROLE_QUALITY, ROLE_WAREHOUSE):
        for field_name in COMMERCIAL_FIELDS:
            if view.get(field_name) is not None:
                view[field_name] = MASK
                redacted.append(field_name)
    if role == ROLE_QUALITY and view.get("supplier_id") is not None:
        view["supplier_id"] = MASK
        redacted.append("supplier_id")
    view["redacted_fields"] = redacted
    return view
