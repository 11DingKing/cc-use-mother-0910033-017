"""命令行演示：停用 IC-400 的完整影响分析与变更提案流程。

用法：PYTHONPATH=src python3 tools/impact_demo.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from impact_graph import build_services, models
from impact_graph.demo import seed
from impact_graph.security import ROLE_PLANNER, ROLE_WAREHOUSE, Viewer, redact_report


def line(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def show_report(report, role: str) -> None:
    data = redact_report(report, Viewer(role))
    print(f"[{role}] 影响摘要：{json.dumps(data['summary'], ensure_ascii=False)}")
    for bucket in ("direct", "indirect"):
        for item in data[bucket]:
            if item["applicable"] is None:
                mark = "未评估"
            elif item["applicable"]:
                mark = "适用"
            elif item["exempted"]:
                mark = "豁免"
            else:
                mark = "不适用"
            print(f"  [{item['dependency']}/{mark}] {item['ref_id']}"
                  f"（{item['state']}，业务日 {item['business_date']}）")
            if item.get("phase"):
                print(f"      阶段：{item['phase']}")
            for reason in item["reasons"]:
                print(f"      · {reason}")


def main() -> None:
    fixed_now = lambda: datetime(2026, 10, 6, tzinfo=timezone.utc)
    repo, catalog, proposals = build_services(now=fixed_now)
    seed(repo, catalog)

    line("1. 即席多层影响分析：停用 IC-400 会波及谁？")
    report = catalog.analyze_part("IC-400")
    show_report(report, ROLE_PLANNER)
    mo3 = next(i for i in report.indirect if i.ref_id == "MO-2026-003")
    print("\n  MO-2026-003 的可解释影响路径：")
    for hop in mo3.path:
        print("    " + hop.label)

    line("2. 创建替代提案 IC-400 → IC-410（分阶段 + 临时豁免）")
    proposal = proposals.create_proposal(
        models.ACTION_SUBSTITUTE,
        target_part_id="IC-400",
        created_by="采购计划员",
        replacement_part_id="IC-410",
        phases=[
            {"name": "采购先行", "effective_date": "2026-11-01",
             "ref_types": [models.REF_PURCHASE_ORDER]},
            {"name": "生产跟进", "effective_date": "2026-12-01",
             "ref_types": [models.REF_PRODUCTION_ORDER]},
        ],
    )
    proposals.add_exemption(
        proposal.id, "采购计划员",
        valid_from="2026-10-01", valid_to="2026-11-30",
        reason="供应商最后一批备货", ref_id="PO-2026-001",
    )
    print(f"  提案 {proposal.id} 已创建，当前版本数：{len(proposal.versions)}")
    show_report(proposals.compute_impact(proposal.id), ROLE_PLANNER)

    line("3. 发布：只影响符合条件的未来业务，历史单据不回写")
    proposals.confirm(proposal.id, "采购计划员")
    proposals.publish(proposal.id, "采购计划员")
    show_report(proposals.compute_impact(proposal.id), ROLE_PLANNER)

    line("4. 同一结果按仓储管理员视角裁剪供应商商业数据")
    show_report(proposals.compute_impact(proposal.id), ROLE_WAREHOUSE)

    line("5. 撤回提案并查看不可变版本历史")
    proposals.withdraw(proposal.id, "采购计划员", "新芯片重新认证通过")
    for v in proposals.list_versions(proposal.id):
        print(f"  v{v.number} [{v.status}] {v.at[:10]} {v.change}"
              f"｜摘要 {json.dumps(v.impact_summary, ensure_ascii=False)}")
    print(f"\n  部件状态：{repo.require_part('IC-400').status}")


if __name__ == "__main__":
    main()
