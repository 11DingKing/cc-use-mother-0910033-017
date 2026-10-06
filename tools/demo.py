"""演示：构建样例数据并打印变更提案的影响报告。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from impact_graph.seed import build_demo_service

if __name__ == "__main__":
    service = build_demo_service()
    report = service.impact_view("ECP-001", role="采购计划员")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
