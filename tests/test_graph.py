"""多层依赖遍历、循环检测与影响路径解释测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from impact_graph import models
from impact_graph.catalog import CatalogService
from impact_graph.errors import CycleDetected, ValidationFailed
from impact_graph.graph import (
    DependencyGraph,
    detect_substitution_cycles,
    would_create_substitution_cycle,
)
from impact_graph.repository import Repository


def clock_at(y: int, m: int, d: int):
    return lambda: datetime(y, m, d, tzinfo=timezone.utc)


def build_layered():
    repo = Repository(clock=clock_at(2026, 10, 6))
    cat = CatalogService(repo)
    for pid in ("C", "B", "A", "X"):
        cat.create_part(pid, pid)
    # A 包含 B 包含 C（三层）
    cat.create_bom_version("BOM-B", "B", [{"component_id": "C", "qty": 2}],
                           effective_from="2026-01-01", effective=True)
    cat.create_bom_version("BOM-A", "A", [{"component_id": "B", "qty": 1},
                                          {"component_id": "X", "qty": 3}],
                           effective_from="2026-01-01", effective=True)
    return repo, cat


class GraphTraversalTest(unittest.TestCase):
    def test_direct_and_multilevel_indirect_impacts(self) -> None:
        repo, cat = build_layered()
        cat.create_ref("PO-1", models.REF_PURCHASE_ORDER, "C", 10,
                       models.STATE_PENDING, "2026-12-01")
        cat.create_ref("IT-1", models.REF_IN_TRANSIT_BATCH, "B", 5,
                       models.STATE_DRAFT, "2026-12-01")
        cat.create_ref("MO-1", models.REF_PRODUCTION_ORDER, "A", 2,
                       models.STATE_DRAFT, "2026-12-01")
        cat.create_ref("MO-X", models.REF_PRODUCTION_ORDER, "X", 9,
                       models.STATE_DRAFT, "2026-12-01")

        report = DependencyGraph(repo).impact("C")
        direct_ids = {i.ref_id for i in report.direct}
        indirect_ids = {i.ref_id for i in report.indirect}
        self.assertEqual(direct_ids, {"PO-1"})
        self.assertEqual(indirect_ids, {"IT-1", "MO-1"})
        # X 不依赖 C，不应出现
        self.assertNotIn("MO-X", direct_ids | indirect_ids)

    def test_path_explains_every_hop(self) -> None:
        repo, cat = build_layered()
        cat.create_ref("MO-1", models.REF_PRODUCTION_ORDER, "A", 2,
                       models.STATE_DRAFT, "2026-12-01")
        report = DependencyGraph(repo).impact("C")
        item = next(i for i in report.indirect if i.ref_id == "MO-1")
        # ref -> A 的 BOM 含 B -> B 的 BOM 含 C -> target
        kinds = [h.kind for h in item.path]
        self.assertEqual(kinds, ["ref", "bom", "bom", "target"])
        chain_parts = [item.path[1].detail["parent_id"],
                       item.path[2].detail["parent_id"]]
        self.assertEqual(chain_parts, ["A", "B"])
        labels = " ".join(h.label for h in item.path)
        self.assertIn("BOM-A", labels)
        self.assertIn("BOM-B", labels)

    def test_bom_cycle_rejected_at_creation(self) -> None:
        repo, cat = build_layered()
        # 让 C 包含 A，形成 A→B→C→A
        with self.assertRaises(CycleDetected) as ctx:
            cat.create_bom_version("BOM-C", "C", [{"component_id": "A", "qty": 1}],
                                   effective_from="2026-02-01", effective=True)
        self.assertEqual(ctx.exception.cycles[0]["kind"], "bom")

    def test_self_reference_rejected(self) -> None:
        repo = Repository()
        cat = CatalogService(repo)
        cat.create_part("P", "P")
        with self.assertRaises(ValidationFailed):
            cat.create_bom_version("BOM-P", "P", [{"component_id": "P", "qty": 1}])

    def test_bom_versions_are_immutable_history(self) -> None:
        repo = Repository()
        cat = CatalogService(repo)
        for pid in ("P", "C1", "C2"):
            cat.create_part(pid, pid)
        v1 = cat.create_bom_version("BOM-P", "P", [{"component_id": "C1", "qty": 1}],
                                    effective_from="2026-01-01", effective=True)
        v2 = cat.create_bom_version("BOM-P", "P", [{"component_id": "C2", "qty": 1}],
                                    effective_from="2026-06-01", effective=True)
        self.assertEqual(v1.version, 1)
        self.assertEqual(v2.version, 2)
        # 旧版本自动转 obsolete，图中只使用新生效版本
        self.assertEqual(v1.state, models.BOM_OBSOLETE)
        current = repo.effective_bom_for("P")
        self.assertIsNotNone(current)
        self.assertEqual(current.version, 2)


class SubstitutionCycleTest(unittest.TestCase):
    def test_substitution_chain_resolves(self) -> None:
        repo = Repository()
        cat = CatalogService(repo)
        for pid in ("A", "B", "C"):
            cat.create_part(pid, pid)
        cat.add_substitution("A", "B")
        cat.add_substitution("B", "C")
        # 新增 C→A 前，A 已能沿现有边 A→B→C 回到 C，必然成环
        self.assertEqual(would_create_substitution_cycle(repo, "C", "A"),
                         ["C", "A", "B", "C"])
        with self.assertRaises(CycleDetected):
            cat.add_substitution("C", "A")
        cycles = detect_substitution_cycles(repo)
        self.assertEqual(cycles, [])

    def test_self_substitution_rejected(self) -> None:
        repo = Repository()
        cat = CatalogService(repo)
        cat.create_part("A", "A")
        with self.assertRaises(CycleDetected):
            cat.add_substitution("A", "A")

    def test_existing_cycle_detected_in_graph(self) -> None:
        """绕过写入防护直接构造环，检测函数必须能发现。"""
        repo = Repository()
        for pid in ("A", "B", "C"):
            p = models.Part(id=pid, name=pid)
            repo.add_part(p)
        repo.add_sub_link(models.SubstitutionLink("A", "B"))
        repo.add_sub_link(models.SubstitutionLink("B", "C"))
        repo.add_sub_link(models.SubstitutionLink("C", "A"))
        cycles = detect_substitution_cycles(repo, start="A")
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0].kind, "substitution")


if __name__ == "__main__":
    unittest.main()
