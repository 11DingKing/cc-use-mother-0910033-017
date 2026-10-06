"""变更提案：影响计算、范围/豁免/分阶段、发布只影响未来业务、撤回、版本历史。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from impact_graph import build_services, models
from impact_graph.demo import seed
from impact_graph.errors import Conflict, ValidationFailed
from impact_graph.policy import resolve_replacement


class FixedClock:
    """可拨动的时钟。"""

    def __init__(self, y: int, m: int, d: int):
        self.at = datetime(y, m, d, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.at

    def set(self, y: int, m: int, d: int) -> None:
        self.at = datetime(y, m, d, tzinfo=timezone.utc)


class ProposalLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(2026, 10, 6)
        self.repo, self.catalog, self.proposals = build_services(now=self.clock)
        seed(self.repo, self.catalog)

    def _impact_map(self, report):
        return {i.ref_id: i for i in (*report.direct, *report.indirect)}

    def test_impact_finds_direct_and_multilevel_refs(self) -> None:
        prop = self.proposals.create_proposal(
            models.ACTION_DISCONTINUE, "IC-400", "计划员甲"
        )
        report = self.proposals.compute_impact(prop.id)
        impacts = self._impact_map(report)
        # PO 直接；IT 经 PCBA-300 一层；MO-003/004 经 PCBA、SA 两层
        self.assertEqual({i.ref_id for i in report.direct}, {"PO-2026-001"})
        self.assertEqual(
            {i.ref_id for i in report.indirect},
            {"IT-2026-002", "MO-2026-003", "MO-2026-004"},
        )
        mo3 = impacts["MO-2026-003"]
        self.assertEqual([h.kind for h in mo3.path], ["ref", "bom", "bom", "bom", "target"])

    def test_draft_is_preview_only(self) -> None:
        prop = self.proposals.create_proposal(
            models.ACTION_DISCONTINUE, "IC-400", "计划员甲"
        )
        impacts = self._impact_map(self.proposals.compute_impact(prop.id))
        # 草拟期所有未锁定的未来单据 applicable=True，但原因中标注“尚未发布（预演）”
        self.assertTrue(impacts["PO-2026-001"].applicable)
        self.assertIn("预演", " ".join(impacts["PO-2026-001"].reasons))

    def test_publish_only_affects_future_eligible_business(self) -> None:
        # 范围：全部单据类型，仅影响草拟/待确认
        prop = self.proposals.create_proposal(
            models.ACTION_DISCONTINUE, "IC-400", "计划员甲"
        )
        self.proposals.confirm(prop.id, "计划员甲")
        self.proposals.publish(prop.id, "计划员甲")
        impacts = self._impact_map(self.proposals.compute_impact(prop.id))

        po = impacts["PO-2026-001"]
        self.assertTrue(po.applicable)  # 待确认 + 12 月交货 → 受影响
        it = impacts["IT-2026-002"]
        self.assertFalse(it.applicable)  # 履行中：状态锁定
        self.assertIn("履行中", " ".join(it.reasons))
        mo3 = impacts["MO-2026-003"]
        self.assertTrue(mo3.applicable)  # 草拟 + 11 月开工
        mo4 = impacts["MO-2026-004"]
        self.assertFalse(mo4.applicable)  # 已下达：不在影响状态
        # 部件已停用
        self.assertEqual(self.repo.require_part("IC-400").status, models.PART_DISCONTINUED)

    def test_history_not_rewritten_by_publish(self) -> None:
        prop = self.proposals.create_proposal(
            models.ACTION_DISCONTINUE, "IC-400", "计划员甲"
        )
        self.proposals.publish(prop.id, "计划员甲")
        # 业务单据本身从未被修改
        self.assertEqual(self.repo.get_ref("MO-2026-004").state, models.STATE_RELEASED)
        self.assertEqual(self.repo.get_ref("PO-2026-001").state, models.STATE_PENDING)

    def test_scope_limits_ref_types(self) -> None:
        prop = self.proposals.create_proposal(
            models.ACTION_DISCONTINUE, "IC-400", "计划员甲",
            scope={"ref_types": [models.REF_PRODUCTION_ORDER]},
        )
        impacts = self._impact_map(self.proposals.compute_impact(prop.id))
        self.assertFalse(impacts["PO-2026-001"].applicable)
        self.assertIn("替代范围", " ".join(impacts["PO-2026-001"].reasons))
        self.assertTrue(impacts["MO-2026-003"].applicable)

    def test_phased_effective_dates(self) -> None:
        # 采购订单 11 月起生效；生产任务 12 月起生效
        prop = self.proposals.create_proposal(
            models.ACTION_SUBSTITUTE, "IC-400", "计划员甲",
            replacement_part_id="IC-410",
            phases=[
                {"name": "采购先行", "effective_date": "2026-11-01",
                 "ref_types": [models.REF_PURCHASE_ORDER]},
                {"name": "生产跟进", "effective_date": "2026-12-01",
                 "ref_types": [models.REF_PRODUCTION_ORDER]},
            ],
        )
        self.proposals.publish(prop.id, "计划员甲")
        impacts = self._impact_map(self.proposals.compute_impact(prop.id))
        po = impacts["PO-2026-001"]
        self.assertTrue(po.applicable)
        self.assertEqual(po.phase, "采购先行")
        mo3 = impacts["MO-2026-003"]  # 11-15 开工但生产阶段 12-01 才生效
        self.assertFalse(mo3.applicable)
        self.assertEqual(mo3.phase, "生产跟进")
        self.assertIn("早于阶段", " ".join(mo3.reasons))
        # 在途批次未配置阶段 → 永不自动适用
        it = impacts["IT-2026-002"]
        self.assertFalse(it.applicable)
        self.assertIn("永不", " ".join(it.reasons))

    def test_business_date_gates_phased_effect(self) -> None:
        """分阶段按业务锚定日期判定：阶段日之前执行的批次始终按旧规则。"""
        self.catalog.create_ref(
            "MO-2026-005", models.REF_PRODUCTION_ORDER, "FG-100", 40,
            models.STATE_DRAFT, "2026-12-15", note="阶段生效后的生产任务",
        )
        prop = self.proposals.create_proposal(
            models.ACTION_SUBSTITUTE, "IC-400", "计划员甲",
            replacement_part_id="IC-410",
            phases=[{"name": "生产跟进", "effective_date": "2026-12-01"}],
        )
        self.proposals.publish(prop.id, "计划员甲")
        impacts = self._impact_map(self.proposals.compute_impact(prop.id))
        early = impacts["MO-2026-003"]  # 11-15 开工，早于阶段生效日
        late = impacts["MO-2026-005"]   # 12-15 开工
        self.assertFalse(early.applicable)
        self.assertIn("早于阶段", " ".join(early.reasons))
        self.assertTrue(late.applicable)
        self.assertEqual(late.phase, "生产跟进")

    def test_temporary_exemption(self) -> None:
        prop = self.proposals.create_proposal(
            models.ACTION_DISCONTINUE, "IC-400", "计划员甲"
        )
        self.proposals.add_exemption(
            prop.id, "计划员甲",
            valid_from="2026-10-01", valid_to="2026-10-31",
            reason="供应商最后一批备货", ref_id="PO-2026-001",
        )
        self.proposals.publish(prop.id, "计划员甲")
        impacts = self._impact_map(self.proposals.compute_impact(prop.id))
        po = impacts["PO-2026-001"]
        self.assertFalse(po.applicable)
        self.assertTrue(po.exempted)
        # 豁免到期后自动恢复适用
        self.clock.set(2026, 11, 1)
        later = self._impact_map(self.proposals.compute_impact(prop.id))
        self.assertTrue(later["PO-2026-001"].applicable)
        self.assertFalse(later["PO-2026-001"].exempted)

    def test_part_level_exemption_covers_indirect_refs(self) -> None:
        prop = self.proposals.create_proposal(
            models.ACTION_DISCONTINUE, "IC-400", "计划员甲"
        )
        self.proposals.add_exemption(
            prop.id, "计划员甲",
            valid_from="2026-10-01", valid_to="2026-12-31",
            reason="整机售后维保", part_id="FG-100",
        )
        self.proposals.publish(prop.id, "计划员甲")
        impacts = self._impact_map(self.proposals.compute_impact(prop.id))
        self.assertTrue(impacts["MO-2026-003"].exempted)
        self.assertFalse(impacts["PO-2026-001"].exempted)

    def test_withdraw_reverses_publish_and_versions_are_immutable(self) -> None:
        prop = self.proposals.create_proposal(
            models.ACTION_SUBSTITUTE, "IC-400", "计划员甲",
            replacement_part_id="IC-410",
        )
        self.proposals.add_exemption(
            prop.id, "计划员甲", "2026-10-01", "2026-10-31", "x",
            ref_id="PO-2026-001",
        )
        self.proposals.publish(prop.id, "计划员甲")
        self.assertEqual(self.repo.require_part("IC-400").status, models.PART_DISCONTINUED)
        self.assertTrue(any(l.new_part_id == "IC-410" for l in self.repo.sub_targets("IC-400")))

        self.proposals.withdraw(prop.id, "计划员甲", "芯片重新认证通过")
        self.assertEqual(self.repo.require_part("IC-400").status, models.PART_ACTIVE)
        self.assertEqual(self.repo.sub_targets("IC-400"), [])
        self.assertEqual(prop.status, models.PROP_WITHDRAWN)

        versions = self.proposals.list_versions(prop.id)
        changes = [v.change for v in versions]
        self.assertEqual(len(versions), 4)  # 创建 / 豁免 / 发布 / 撤回
        self.assertTrue(any("发布" in c for c in changes))
        self.assertTrue(any("撤回" in c for c in changes))
        # 旧版本不可变：发布版本仍记录 published_at
        published_version = next(v for v in versions if "发布" in v.change)
        self.assertIsNotNone(published_version.config["published_at"])
        # 撤回后不可再改
        with self.assertRaises(Conflict):
            self.proposals.update_scope(prop.id, "计划员甲", {})
        with self.assertRaises(Conflict):
            self.proposals.withdraw(prop.id, "计划员甲", "再次撤回")

    def test_version_snapshots_track_scope_changes(self) -> None:
        prop = self.proposals.create_proposal(
            models.ACTION_DISCONTINUE, "IC-400", "计划员甲",
            scope={"ref_types": [models.REF_PURCHASE_ORDER]},
        )
        self.proposals.update_scope(
            prop.id, "计划员甲", {"ref_types": [models.REF_PRODUCTION_ORDER]}
        )
        versions = self.proposals.list_versions(prop.id)
        self.assertEqual(
            versions[0].config["scope"]["ref_types"], [models.REF_PURCHASE_ORDER]
        )
        self.assertEqual(
            versions[1].config["scope"]["ref_types"], [models.REF_PRODUCTION_ORDER]
        )
        # 每个版本的影响摘要都可回溯
        self.assertGreaterEqual(versions[0].impact_summary["direct"], 1)


class SubstitutionPolicyTest(unittest.TestCase):
    def test_partial_scope_resolves_by_ref_type(self) -> None:
        repo, catalog, _ = build_services()
        catalog.create_part("A", "A")
        catalog.create_part("B", "B")
        catalog.create_part("C", "C")
        catalog.add_substitution("A", "B", scope=models.SCOPE_PARTIAL,
                                 ref_types=[models.REF_PURCHASE_ORDER])
        catalog.add_substitution("B", "C")
        repl_po, chain_po = resolve_replacement(repo, "A", models.REF_PURCHASE_ORDER)
        self.assertEqual(repl_po, "C")
        self.assertEqual(chain_po, ["A", "B", "C"])
        repl_mo, _ = resolve_replacement(repo, "A", models.REF_PRODUCTION_ORDER)
        self.assertIsNone(repl_mo)  # PARTIAL 边对生产任务不可用，链断在 A

    def test_overlapping_phases_rejected(self) -> None:
        from impact_graph.catalog import CatalogService
        from impact_graph.errors import ValidationFailed

        repo, _, proposals = build_services()
        CatalogService(repo).create_part("X", "X")
        with self.assertRaises(ValidationFailed):
            proposals.create_proposal(
                models.ACTION_DISCONTINUE, "X", "p",
                phases=[
                    {"name": "p1", "effective_date": "2026-11-01",
                     "ref_types": [models.REF_PURCHASE_ORDER]},
                    {"name": "p2", "effective_date": "2026-11-02",
                     "ref_types": [models.REF_PURCHASE_ORDER, models.REF_PRODUCTION_ORDER]},
                ],
            )


if __name__ == "__main__":
    unittest.main()
