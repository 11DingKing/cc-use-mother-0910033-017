"""演示数据：构建一个含多层 BOM、业务引用与替代链的服务实例。"""
from __future__ import annotations

from datetime import date

from .models import BomItem
from .service import ImpactService
from .substitution import Exemption, Phase


def build_demo_service() -> ImpactService:
    service = ImpactService()
    for part_id, name in [
        ("COMP-9", "电解电容 470uF"),
        ("ALT-9", "固态电容 470uF"),
        ("SA-1", "电源板"),
        ("SA-2", "控制板"),
        ("FG-1", "整机"),
    ]:
        service.add_part(part_id, name)

    service.add_bom_version("BOM-SA1", "SA-1", 1, [BomItem("COMP-9", 2)], date(2026, 1, 1))
    service.add_bom_version(
        "BOM-FG1", "FG-1", 1, [BomItem("SA-1", 1), BomItem("SA-2", 1)], date(2026, 1, 1)
    )

    service.add_ref(
        "PO-100", "采购订单", "COMP-9", 5000, date(2026, 9, 1),
        state="已下达", plant="P1", supplier_id="SUP-A", unit_price=3.5, contract_terms="年度框架协议",
    )
    service.add_ref(
        "IT-200", "在途批次", "COMP-9", 1200, date(2026, 9, 15),
        state="履行中", plant="P1", supplier_id="SUP-A", unit_price=3.5,
    )
    service.add_ref("MO-300", "生产任务", "SA-1", 300, date(2026, 9, 20), state="已下达", plant="P1")
    service.add_ref("MO-301", "生产任务", "FG-1", 150, date(2026, 9, 21), state="草拟", plant="P2")

    service.add_substitution("SUB-1", "COMP-9", "ALT-9")
    service.add_substitution_version(
        "SUB-1",
        scope_plants={"P1", "P2"},
        phases=[
            Phase(date(2026, 11, 1), frozenset({"P1"})),
            Phase(date(2026, 12, 1), frozenset({"P1", "P2"})),
        ],
        exemptions=[Exemption(until=date(2026, 11, 15), ref_ids=frozenset({"PO-100"}), reason="在途合同履约")],
        note="电容替代，分工厂阶段生效",
    )
    service.publish_substitution("SUB-1", 1, on=date(2026, 10, 15))

    service.create_proposal("ECP-001", "COMP-9")
    service.evaluate_proposal("ECP-001", at=date(2026, 10, 6))
    return service
