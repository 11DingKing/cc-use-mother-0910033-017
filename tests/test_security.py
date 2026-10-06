"""按角色裁剪供应商商业数据的测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from impact_graph import build_services, models
from impact_graph.demo import seed
from impact_graph.security import (
    ROLE_PLANNER,
    ROLE_QUALITY,
    ROLE_WAREHOUSE,
    ROLE_SUPPLIER,
    Viewer,
    redact_impact_item,
    redact_part,
    redact_ref,
    redact_report,
)


class RedactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.catalog, _ = build_services()
        seed(self.repo, self.catalog)

    def test_ref_redaction_matrix(self) -> None:
        ref = self.repo.get_ref("PO-2026-001")
        planner = redact_ref(ref, Viewer(ROLE_PLANNER))
        self.assertEqual(planner["supplier_id"], "SUP-A")
        self.assertEqual(planner["unit_price"], 12.5)

        quality = redact_ref(ref, Viewer(ROLE_QUALITY))
        self.assertEqual(quality["supplier_id"], "SUP-A")
        self.assertIsNone(quality["unit_price"])
        self.assertIn("unit_price", quality["_redacted"])

        warehouse = redact_ref(ref, Viewer(ROLE_WAREHOUSE))
        self.assertIsNone(warehouse["supplier_id"])
        self.assertIsNone(warehouse["unit_price"])

    def test_supplier_only_sees_own_commercial_data(self) -> None:
        ref = self.repo.get_ref("PO-2026-001")  # SUP-A
        own = redact_ref(ref, Viewer(ROLE_SUPPLIER, supplier_id="SUP-A"))
        self.assertEqual(own["supplier_id"], "SUP-A")
        self.assertEqual(own["unit_price"], 12.5)
        other = redact_ref(ref, Viewer(ROLE_SUPPLIER, supplier_id="SUP-B"))
        self.assertIsNone(other["supplier_id"])
        self.assertIsNone(other["unit_price"])

    def test_part_cost_hidden(self) -> None:
        part = self.repo.require_part("IC-400")
        self.assertEqual(redact_part(part, Viewer(ROLE_PLANNER))["unit_cost"], 12.5)
        self.assertIsNone(redact_part(part, Viewer(ROLE_QUALITY))["unit_cost"])
        supplier_view = redact_part(part, Viewer(ROLE_SUPPLIER, supplier_id="SUP-A"))
        self.assertEqual(supplier_view["supplier_ids"], ["SUP-A"])
        supplier_b = redact_part(part, Viewer(ROLE_SUPPLIER, supplier_id="SUP-B"))
        self.assertEqual(supplier_b["supplier_ids"], [])

    def test_report_redaction_keeps_paths_explainable(self) -> None:
        from impact_graph.graph import DependencyGraph

        report = DependencyGraph(self.repo).impact("IC-400")
        view = redact_report(report, Viewer(ROLE_WAREHOUSE))
        po = next(i for i in view["direct"] if i["ref_id"] == "PO-2026-001")
        self.assertIsNone(po["supplier_id"])
        # 路径解释不依赖商业数据，仍然完整
        self.assertTrue(any(h["kind"] == "ref" for h in po["path"]))
        self.assertEqual(view["summary"]["direct"], 1)

    def test_supplier_requires_supplier_id(self) -> None:
        with self.assertRaises(ValueError):
            Viewer(ROLE_SUPPLIER)


if __name__ == "__main__":
    unittest.main()
