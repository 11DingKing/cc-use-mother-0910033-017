"""领域模型：部件、BOM 版本、业务引用与变更提案。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class PartStatus(str, Enum):
    ACTIVE = "在用"
    DEACTIVATED = "停用"


class RefKind(str, Enum):
    PURCHASE_ORDER = "采购订单"
    IN_TRANSIT = "在途批次"
    PRODUCTION_TASK = "生产任务"


class RefState(str, Enum):
    DRAFT = "草拟"
    PENDING = "待确认"
    RELEASED = "已下达"
    IN_PROGRESS = "履行中"
    CLOSED = "已关闭"


# 未关闭的业务引用仍可能受变更影响
OPEN_STATES = frozenset({RefState.DRAFT, RefState.PENDING, RefState.RELEASED, RefState.IN_PROGRESS})


class ProposalAction(str, Enum):
    DEACTIVATE = "停用"


@dataclass(frozen=True)
class Part:
    part_id: str
    name: str
    status: PartStatus = PartStatus.ACTIVE


@dataclass(frozen=True)
class BomItem:
    child_id: str
    quantity: float = 1.0


@dataclass(frozen=True)
class BomVersion:
    """BOM 版本：仅在生效区间覆盖时构成有效引用。"""

    bom_id: str
    parent_id: str
    version: int
    items: tuple[BomItem, ...]
    effective_from: date
    effective_to: date | None = None

    def covers(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to

    def child_ids(self) -> tuple[str, ...]:
        return tuple(item.child_id for item in self.items)


@dataclass(frozen=True)
class BusinessRef:
    """业务引用：采购订单、在途批次或生产任务对部件的依赖。"""

    ref_id: str
    kind: RefKind
    part_id: str
    quantity: float
    state: RefState
    created_at: date
    plant: str = ""
    supplier_id: str | None = None
    unit_price: float | None = None
    contract_terms: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


@dataclass
class ChangeProposal:
    """变更提案：先评估影响与循环，再沿状态机推进。"""

    proposal_id: str
    target_part: str
    action: ProposalAction = ProposalAction.DEACTIVATE
    state: RefState = RefState.DRAFT
