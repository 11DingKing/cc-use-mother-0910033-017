"""演示数据：构造一个三层 BOM 与跨业务引用场景。

结构示意（BOM 包含关系）：

    成品机 FG-100
      └─ 控制组件 SA-200
           ├─ 电路板 PCBA-300
           │    └─ 芯片 IC-400   ← 计划停用的零件
           └─ 连接器 CN-500

业务单据：
    PO-2026-001 采购订单直接采购 IC-400（直接引用）
    IT-2026-002 在途批次承载 PCBA-300（一层间接）
    MO-2026-003 生产任务生产 FG-100（多层间接）
    MO-2026-004 已下达的历史生产任务（发布后不应被追溯）
"""
from __future__ import annotations

from . import models
from .catalog import CatalogService
from .repository import Repository


def seed(repo: Repository, catalog: CatalogService) -> None:
    catalog.create_supplier("SUP-A", "华东精密电子")
    catalog.create_supplier("SUP-B", "迅达连接器")

    catalog.create_part("IC-400", "主控芯片", supplier_ids=["SUP-A"], unit_cost=12.5)
    catalog.create_part("CN-500", "连接器", supplier_ids=["SUP-B"], unit_cost=0.8)
    catalog.create_part("PCBA-300", "控制电路板")
    catalog.create_part("SA-200", "控制组件")
    catalog.create_part("FG-100", "成品机")
    catalog.create_part("IC-410", "主控芯片（新一代）", supplier_ids=["SUP-A"], unit_cost=11.9)

    # PCBA-300 BOM：含 IC-400
    catalog.create_bom_version(
        bom_id="BOM-PCBA300",
        parent_id="PCBA-300",
        lines=[{"component_id": "IC-400", "qty": 1}, {"component_id": "CN-500", "qty": 2}],
        effective_from="2026-01-01",
        effective=True,
    )
    # SA-200 BOM：含 PCBA-300
    catalog.create_bom_version(
        bom_id="BOM-SA200",
        parent_id="SA-200",
        lines=[{"component_id": "PCBA-300", "qty": 1}],
        effective_from="2026-01-01",
        effective=True,
    )
    # FG-100 BOM：含 SA-200
    catalog.create_bom_version(
        bom_id="BOM-FG100",
        parent_id="FG-100",
        lines=[{"component_id": "SA-200", "qty": 1}, {"component_id": "CN-500", "qty": 4}],
        effective_from="2026-01-01",
        effective=True,
    )

    catalog.create_ref(
        "PO-2026-001", models.REF_PURCHASE_ORDER, "IC-400", 5000,
        models.STATE_PENDING, "2026-12-01", supplier_id="SUP-A", unit_price=12.5,
        note="直接采购芯片",
    )
    catalog.create_ref(
        "IT-2026-002", models.REF_IN_TRANSIT_BATCH, "PCBA-300", 800,
        models.STATE_EXECUTING, "2026-10-20", supplier_id="SUP-A", unit_price=18.0,
        note="在途电路板批次",
    )
    catalog.create_ref(
        "MO-2026-003", models.REF_PRODUCTION_ORDER, "FG-100", 120,
        models.STATE_DRAFT, "2026-11-15", note="未来成品机生产任务",
    )
    catalog.create_ref(
        "MO-2026-004", models.REF_PRODUCTION_ORDER, "FG-100", 60,
        models.STATE_RELEASED, "2026-10-10", note="已下达的历史任务",
    )
