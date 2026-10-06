"""领域模型：部件、BOM 版本、业务引用、替代链、变更提案。

时间字段统一使用 ISO-8601 字符串（日期为 ``YYYY-MM-DD``，时间戳带时区），
模型本身保持可序列化，业务判定放在 ``policy`` / ``graph`` 中。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional

# ---- 基础枚举（与 domain/contract.json 的状态集合对齐） ----

PART_ACTIVE = "active"
PART_DISCONTINUED = "discontinued"

BOM_DRAFT = "draft"
BOM_EFFECTIVE = "effective"
BOM_OBSOLETE = "obsolete"

REF_PURCHASE_ORDER = "PURCHASE_ORDER"
REF_IN_TRANSIT_BATCH = "IN_TRANSIT_BATCH"
REF_PRODUCTION_ORDER = "PRODUCTION_ORDER"
REF_TYPES = (REF_PURCHASE_ORDER, REF_IN_TRANSIT_BATCH, REF_PRODUCTION_ORDER)

REF_TYPE_LABELS = {
    REF_PURCHASE_ORDER: "采购订单",
    REF_IN_TRANSIT_BATCH: "在途批次",
    REF_PRODUCTION_ORDER: "生产任务",
}

# 业务单据状态：草拟 → 待确认 → 已下达 → 履行中 → 已关闭
STATE_DRAFT = "草拟"
STATE_PENDING = "待确认"
STATE_RELEASED = "已下达"
STATE_EXECUTING = "履行中"
STATE_CLOSED = "已关闭"
DOC_STATES = (STATE_DRAFT, STATE_PENDING, STATE_RELEASED, STATE_EXECUTING, STATE_CLOSED)
TERMINAL_STATES = frozenset({STATE_CLOSED})
STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_DRAFT: frozenset({STATE_PENDING, STATE_CLOSED}),
    STATE_PENDING: frozenset({STATE_RELEASED, STATE_DRAFT, STATE_CLOSED}),
    STATE_RELEASED: frozenset({STATE_EXECUTING, STATE_CLOSED}),
    STATE_EXECUTING: frozenset({STATE_CLOSED}),
    STATE_CLOSED: frozenset(),
}

# 提案生命周期
PROP_DRAFT = "draft"          # 草拟
PROP_CONFIRMED = "confirmed"  # 待确认
PROP_PUBLISHED = "published"  # 已发布（已下达生效规则）
PROP_WITHDRAWN = "withdrawn"  # 已撤回

ACTION_DISCONTINUE = "DISCONTINUE"  # 停用
ACTION_SUBSTITUTE = "SUBSTITUTE"    # 替代

SCOPE_FULL = "FULL"        # 全量替代
SCOPE_PARTIAL = "PARTIAL"  # 限定范围替代


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


@dataclass
class Supplier:
    id: str
    name: str


@dataclass
class Part:
    id: str
    name: str
    status: str = PART_ACTIVE
    # 供应商商业数据：供货关系与单价仅对有权限的角色可见
    supplier_ids: list[str] = field(default_factory=list)
    unit_cost: Optional[float] = None


@dataclass
class BomLine:
    component_id: str
    qty: float
    note: str = ""


@dataclass
class BomVersion:
    """一个父部件的 BOM 的某个版本。"""

    bom_id: str
    parent_id: str
    version: int
    state: str = BOM_DRAFT
    effective_from: Optional[str] = None
    lines: list[BomLine] = field(default_factory=list)
    created_at: str = ""


@dataclass
class BusinessRef:
    """跨业务引用：采购订单 / 在途批次 / 生产任务。"""

    id: str
    type: str
    part_id: str
    qty: float
    state: str
    # 业务锚定日期：采购交货期 / 预计到货日 / 计划开工日，用于“未来业务”判定
    business_date: str
    created_at: str
    supplier_id: Optional[str] = None
    unit_price: Optional[float] = None
    note: str = ""


@dataclass
class SubstitutionLink:
    """替代链的一条有向边：old_part 可由 new_part 替代。"""

    old_part_id: str
    new_part_id: str
    scope: str = SCOPE_FULL
    ref_types: Optional[list[str]] = None  # PARTIAL 时限定单据类型
    active: bool = True
    note: str = ""
    created_at: str = ""


@dataclass
class Exemption:
    """临时豁免：在有效期内，指定单据（或部件下全部单据）不受提案影响。"""

    id: str
    proposal_id: str
    ref_id: Optional[str]
    part_id: Optional[str]
    reason: str
    valid_from: str
    valid_to: str
    created_by: str = ""
    created_at: str = ""


@dataclass
class Phase:
    """分阶段生效：某类单据自 effective_date 起适用。"""

    name: str
    effective_date: str
    ref_types: Optional[list[str]] = None  # None 表示覆盖全部单据类型


@dataclass
class ProposalScope:
    """替代范围：限定单据类型与受影响的单据状态。"""

    ref_types: Optional[list[str]] = None
    affect_states: list[str] = field(
        default_factory=lambda: [STATE_DRAFT, STATE_PENDING]
    )
    note: str = ""


@dataclass
class ProposalVersion:
    """不可变版本快照：范围调整、豁免、发布、撤回都会形成新版本。"""

    number: int
    status: str
    author: str
    at: str
    change: str
    config: dict[str, Any]
    impact_summary: dict[str, int]


@dataclass
class ChangeProposal:
    id: str
    action: str
    target_part_id: str
    replacement_part_id: Optional[str]
    scope: ProposalScope
    phases: list[Phase]
    created_by: str
    created_at: str
    status: str = PROP_DRAFT
    exemption_ids: list[str] = field(default_factory=list)
    published_at: Optional[str] = None
    versions: list[ProposalVersion] = field(default_factory=list)


# ---- 影响分析结果 ----


@dataclass
class PathHop:
    """影响路径上的一跳，供 API 逐条解释。"""

    kind: str  # ref / bom / target
    label: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImpactItem:
    ref_id: str
    ref_type: str
    part_id: str
    qty: float
    state: str
    business_date: str
    dependency: str  # direct / indirect
    path: list[PathHop]
    supplier_id: Optional[str] = None
    unit_price: Optional[float] = None
    # policy 填充
    applicable: Optional[bool] = None
    phase: Optional[str] = None
    exempted: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class Cycle:
    kind: str  # substitution / bom
    nodes: list[str]
    detail: str


@dataclass
class ImpactReport:
    target_part_id: str
    replacement_part_id: Optional[str]
    direct: list[ImpactItem] = field(default_factory=list)
    indirect: list[ImpactItem] = field(default_factory=list)
    cycles: list[Cycle] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "direct": len(self.direct),
            "indirect": len(self.indirect),
            "applicable": sum(
                1 for i in (*self.direct, *self.indirect) if i.applicable
            ),
            "cycles": len(self.cycles),
        }


def to_data(value: Any) -> Any:
    """ dataclass 递归转普通字典。"""
    if hasattr(value, "__dataclass_fields__"):
        return {k: to_data(v) for k, v in asdict(value).items()}
    if isinstance(value, list):
        return [to_data(v) for v in value]
    if isinstance(value, dict):
        return {k: to_data(v) for k, v in value.items()}
    return value
