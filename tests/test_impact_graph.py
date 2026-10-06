"""替代料影响图谱后端的行为测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from impact_graph.graph import CycleError
from impact_graph.models import BomItem, PartStatus
from impact_graph.permissions import AccessDenied
from impact_graph.service import ImpactService
from impact_graph.substitution import (
    Exemption,
    Phase,
    SubstitutionCycleError,
    SubstitutionRule,
)

AT = date(2026, 10, 6)


def build_service() -> ImpactService:
    """两层 BOM：COMP-9 → SA-1 → FG-1，FG-1 另含 SA-2。"""
    service = ImpactService()
    for part_id in ("COMP-9", "ALT-9", "ALT-X", "SA-1", "SA-2", "FG-1"):
        service.add_part(part_id, name=part_id)
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
    service.add_ref("PO-101", "采购订单", "COMP-9", 800, date(2026, 8, 1), state="已关闭", plant="P1")
    return service


class WhereUsedTest(unittest.TestCase):
    def test_multilevel_paths(self) -> None:
        service = build_service()
        view = service.where_used_view("COMP-9", at=AT)
        depths = sorted(len(path["steps"]) for path in view["paths"])
        self.assertEqual(depths, [1, 2])  # 一层到 SA-1，两层经 SA-1 到 FG-1

    def test_bom_cycle_rejected(self) -> None:
        service = build_service()
        with self.assertRaises(CycleError):
            service.add_bom_version("BOM-X", "COMP-9", 1, [BomItem("FG-1", 1)], date(2026, 1, 1))
        with self.assertRaises(CycleError):
            service.add_bom_version("BOM-Y", "SA-2", 1, [BomItem("SA-2", 1)], date(2026, 1, 1))


class ImpactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        self.service.create_proposal("ECP-1", "COMP-9")
        self.report = self.service.evaluate_proposal("ECP-1", at=AT)

    def test_direct_and_indirect_impacts(self) -> None:
        direct_refs = {i.ref.ref_id for i in self.report.direct if i.ref}
        direct_boms = {i.bom_id for i in self.report.direct if i.bom_id}
        indirect_refs = {i.ref.ref_id for i in self.report.indirect if i.ref}
        self.assertEqual(direct_refs, {"PO-100", "IT-200"})  # 已关闭的 PO-101 不计入
        self.assertEqual(direct_boms, {"BOM-SA1"})
        self.assertEqual(indirect_refs, {"MO-300", "MO-301"})

    def test_every_impact_has_explainable_path(self) -> None:
        for impact in self.report.impacts:
            self.assertTrue(impact.explanation)
        mo301 = next(i for i in self.report.indirect if i.ref and i.ref.ref_id == "MO-301")
        self.assertEqual(len(mo301.path), 2)
        self.assertIn("BOM-SA1", mo301.explanation)
        self.assertIn("BOM-FG1", mo301.explanation)
        self.assertIn("MO-301", mo301.explanation)

    def test_no_cycle_in_healthy_graph(self) -> None:
        self.assertFalse(self.report.has_cycle)


class SubstitutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def _published_rule(self, **kwargs) -> None:
        self.service.add_substitution("SUB-1", "COMP-9", "ALT-9")
        self.service.add_substitution_version("SUB-1", **kwargs)
        self.service.publish_substitution("SUB-1", 1, on=date(2026, 10, 15))

    def test_chain_cycle_rejected(self) -> None:
        self.service.add_substitution("S1", "COMP-9", "ALT-9")
        self.service.add_substitution("S2", "ALT-9", "ALT-X")
        with self.assertRaises(SubstitutionCycleError):
            self.service.add_substitution("S3", "ALT-X", "COMP-9")
        with self.assertRaises(SubstitutionCycleError):
            self.service.add_substitution("S4", "COMP-9", "COMP-9")

    def test_only_future_business_affected(self) -> None:
        self._published_rule()
        self.service.add_ref("OLD-1", "采购订单", "COMP-9", 10, date(2026, 10, 1), plant="P1")
        self.service.add_ref("NEW-1", "采购订单", "COMP-9", 10, date(2026, 10, 20), plant="P1")
        self.assertEqual(self.service.applicable_substitutions("OLD-1", today=date(2026, 10, 25)), [])
        matches = self.service.applicable_substitutions("NEW-1", today=date(2026, 10, 25))
        self.assertEqual([(m["rule_id"], m["to_part"]) for m in matches], [("SUB-1", "ALT-9")])

    def test_phased_effectiveness_and_scope(self) -> None:
        self._published_rule(
            scope_plants={"P1", "P2"},
            phases=[
                Phase(date(2026, 11, 1), frozenset({"P1"})),
                Phase(date(2026, 12, 1), frozenset({"P1", "P2"})),
            ],
        )
        cases = [
            ("N-1", "P1", date(2026, 10, 20), False),  # 阶段未开始
            ("N-2", "P1", date(2026, 11, 5), True),    # P1 进入阶段一
            ("N-3", "P2", date(2026, 11, 5), False),   # P2 需等阶段二
            ("N-4", "P2", date(2026, 12, 2), True),    # P2 进入阶段二
            ("N-5", "P3", date(2026, 12, 2), False),   # 范围外工厂
        ]
        for ref_id, plant, created, expected in cases:
            self.service.add_ref(ref_id, "采购订单", "COMP-9", 10, created, plant=plant)
            matches = self.service.applicable_substitutions(ref_id, today=date(2026, 12, 5))
            self.assertEqual(bool(matches), expected, ref_id)

    def test_temporary_exemption_expires(self) -> None:
        self._published_rule(
            exemptions=[Exemption(until=date(2026, 11, 15), ref_ids=frozenset({"EX-1"}), reason="在途履约")]
        )
        self.service.add_ref("EX-1", "采购订单", "COMP-9", 10, date(2026, 11, 5), plant="P1")
        self.assertEqual(self.service.applicable_substitutions("EX-1", today=date(2026, 11, 10)), [])
        matches = self.service.applicable_substitutions("EX-1", today=date(2026, 11, 16))
        self.assertEqual(len(matches), 1)

    def test_withdrawn_version_stops_applying(self) -> None:
        self._published_rule()
        self.service.add_ref("W-1", "采购订单", "COMP-9", 10, date(2026, 10, 20), plant="P1")
        self.assertEqual(len(self.service.applicable_substitutions("W-1", today=date(2026, 10, 25))), 1)
        self.service.withdraw_substitution("SUB-1", 1, on=date(2026, 10, 26))
        self.assertEqual(self.service.applicable_substitutions("W-1", today=date(2026, 10, 27)), [])
        with self.assertRaises(ValueError):
            self.service.withdraw_substitution("SUB-1", 1, on=date(2026, 10, 28))


class ProposalLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_full_lifecycle_deactivates_part(self) -> None:
        self.service.create_proposal("ECP-1", "COMP-9")
        with self.assertRaises(ValueError):
            self.service.transition_proposal("ECP-1", "release")  # 草拟不能直接下达
        self.service.evaluate_proposal("ECP-1", at=AT)
        self.service.transition_proposal("ECP-1", "submit")
        self.service.transition_proposal("ECP-1", "release")
        self.assertEqual(self.service.parts["COMP-9"].status, PartStatus.DEACTIVATED)
        self.service.transition_proposal("ECP-1", "start")
        self.service.transition_proposal("ECP-1", "close")
        with self.assertRaises(ValueError):
            self.service.transition_proposal("ECP-1", "close")

    def test_submit_requires_evaluation(self) -> None:
        self.service.create_proposal("ECP-2", "COMP-9")
        with self.assertRaises(ValueError):
            self.service.transition_proposal("ECP-2", "submit")

    def test_cycle_blocks_submit(self) -> None:
        # 绕过入口校验注入替代链环，验证评估能检出并阻止提交
        self.service.chain._rules["HACK-1"] = SubstitutionRule("HACK-1", "ALT-9", "ALT-X")
        self.service.chain._rules["HACK-2"] = SubstitutionRule("HACK-2", "ALT-X", "ALT-9")
        self.service.create_proposal("ECP-3", "COMP-9")
        report = self.service.evaluate_proposal("ECP-3", at=AT)
        self.assertTrue(report.has_cycle)
        self.assertIn("ALT-9", report.cycles[0])
        with self.assertRaises(ValueError):
            self.service.transition_proposal("ECP-3", "submit")


class PermissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        self.service.create_proposal("ECP-1", "COMP-9")
        self.service.evaluate_proposal("ECP-1", at=AT)

    def test_buyer_sees_commercial_data(self) -> None:
        view = self.service.get_ref_view("PO-100", "采购计划员")
        self.assertEqual(view["unit_price"], 3.5)
        self.assertEqual(view["redacted_fields"], [])

    def test_quality_engineer_commercial_data_masked(self) -> None:
        view = self.service.get_ref_view("PO-100", "质量工程师")
        self.assertEqual(view["unit_price"], "已隐藏")
        self.assertEqual(view["contract_terms"], "已隐藏")
        self.assertEqual(view["supplier_id"], "已隐藏")
        self.assertEqual(set(view["redacted_fields"]), {"unit_price", "contract_terms", "supplier_id"})

    def test_warehouse_keeps_supplier_but_not_price(self) -> None:
        view = self.service.get_ref_view("PO-100", "仓储管理员")
        self.assertEqual(view["unit_price"], "已隐藏")
        self.assertEqual(view["supplier_id"], "SUP-A")

    def test_supplier_only_sees_own_refs(self) -> None:
        view = self.service.get_ref_view("PO-100", "供应商", supplier_id="SUP-A")
        self.assertEqual(view["unit_price"], 3.5)
        with self.assertRaises(AccessDenied):
            self.service.get_ref_view("PO-100", "供应商", supplier_id="SUP-B")
        with self.assertRaises(AccessDenied):
            self.service.get_ref_view("PO-100", "供应商")

    def test_impact_view_hides_other_suppliers(self) -> None:
        view = self.service.impact_view("ECP-1", "供应商", supplier_id="SUP-A")
        visible_refs = {item["ref"]["ref_id"] for item in view["impacts"] if "ref" in item}
        self.assertEqual(visible_refs, {"PO-100", "IT-200"})
        self.assertEqual(view["hidden_count"], 2)  # MO-300、MO-301 不可见
        buyer_view = self.service.impact_view("ECP-1", "采购计划员")
        self.assertEqual(buyer_view["hidden_count"], 0)
        self.assertEqual(buyer_view["direct_count"], 3)
        self.assertEqual(buyer_view["indirect_count"], 2)


if __name__ == "__main__":
    unittest.main()
