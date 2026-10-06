"""服务入口：python -m impact_graph [--demo] [--host H] [--port P] [--seed-only]"""
from __future__ import annotations

import argparse
import json

from . import build_services
from .api import ApiContext, build_server
from .demo import seed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="替代料影响图谱后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--demo", action="store_true", help="载入演示数据")
    args = parser.parse_args(argv)

    repo, catalog, proposals = build_services()
    if args.demo:
        seed(repo, catalog)
        print("已载入演示数据：部件 6 个、BOM 3 个、业务单据 4 张")
    ctx = ApiContext(repo, catalog, proposals)
    server = build_server(ctx, host=args.host, port=args.port)
    print(f"替代料影响图谱服务监听 http://{args.host}:{args.port}")
    print("角色请求头：X-Role（采购计划员/质量工程师/仓储管理员/供应商），供应商需 X-Supplier-Id")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务停止")


if __name__ == "__main__":
    main()
