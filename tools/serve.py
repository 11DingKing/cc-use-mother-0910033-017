"""启动替代料影响图谱 HTTP 服务（--demo 载入样例数据，--port=N 指定端口）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from impact_graph.api import run
from impact_graph.seed import build_demo_service

if __name__ == "__main__":
    service = build_demo_service() if "--demo" in sys.argv else None
    port = 8080
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
    run(port=port, service=service)
