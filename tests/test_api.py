"""HTTP API 端到端测试：路由、权限、循环拦截、提案全流程。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from impact_graph import build_services
from impact_graph.api import ApiContext, ImpactGraphHandler
from impact_graph.demo import seed


class ApiClient:
    def __init__(self, base: str, role: str = "planner", supplier_id: str | None = None):
        self.base = base
        self.role = role
        self.supplier_id = supplier_id

    def request(self, method: str, path: str, body=None):
        url = self.base + path
        data = None
        headers = {"X-Role": self.role}
        if self.supplier_id:
            headers["X-Supplier-Id"] = self.supplier_id
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body or {})


class ApiTest(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        repo, catalog, proposals = build_services(
            now=lambda: datetime(2026, 10, 6, tzinfo=timezone.utc)
        )
        seed(repo, catalog)
        ctx = ApiContext(repo, catalog, proposals)
        handler = type("BoundHandler", (ImpactGraphHandler,), {"ctx": ctx})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def client(self, role="planner", supplier_id=None) -> ApiClient:
        return ApiClient(f"http://127.0.0.1:{self.port}", role, supplier_id)

    def test_health(self) -> None:
        status, body = self.client().get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_part_impact_finds_multilevel_refs(self) -> None:
        status, body = self.client().get("/parts/IC-400/impact")
        self.assertEqual(status, 200)
        self.assertEqual({i["ref_id"] for i in body["direct"]}, {"PO-2026-001"})
        self.assertEqual(
            {i["ref_id"] for i in body["indirect"]},
            {"IT-2026-002", "MO-2026-003", "MO-2026-004"},
        )
        mo3 = next(i for i in body["indirect"] if i["ref_id"] == "MO-2026-003")
        self.assertEqual(
            [h["kind"] for h in mo3["path"]], ["ref", "bom", "bom", "bom", "target"]
        )

    def test_redaction_by_role(self) -> None:
        _, planner = self.client("planner").get("/refs")
        po = next(i for i in planner["items"] if i["id"] == "PO-2026-001")
        self.assertEqual(po["unit_price"], 12.5)

        _, warehouse = self.client("warehouse").get("/refs")
        po = next(i for i in warehouse["items"] if i["id"] == "PO-2026-001")
        self.assertIsNone(po["unit_price"])
        self.assertIsNone(po["supplier_id"])

        _, supplier = self.client("supplier", "SUP-B").get("/refs")
        po = next(i for i in supplier["items"] if i["id"] == "PO-2026-001")
        self.assertIsNone(po["unit_price"])  # SUP-B 看不到 SUP-A 的价格

    def test_write_permission_enforced(self) -> None:
        status, body = self.client("warehouse").post(
            "/parts", {"id": "X", "name": "x"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")
        # 质量工程师可以建主数据，但不能发布提案
        status, _ = self.client("quality").post(
            "/parts", {"id": "Q-1", "name": "q"}
        )
        self.assertEqual(status, 201)

    def test_proposal_full_flow_over_http(self) -> None:
        client = self.client()
        status, prop = client.post("/proposals", {
            "action": "SUBSTITUTE",
            "target_part_id": "IC-400",
            "replacement_part_id": "IC-410",
            "created_by": "计划员甲",
        })
        self.assertEqual(status, 201)
        pid = prop["id"]
        self.assertEqual(prop["version_count"], 1)

        # 影响预演
        _, impact = client.get(f"/proposals/{pid}/impact")
        self.assertEqual(impact["summary"]["direct"], 1)
        self.assertEqual(impact["summary"]["indirect"], 3)

        # 加豁免 → 形成新版本
        status, ex = client.post(f"/proposals/{pid}/exemptions", {
            "valid_from": "2026-10-01",
            "valid_to": "2026-12-31",
            "reason": "旧批次备货",
            "ref_id": "PO-2026-001",
        })
        self.assertEqual(status, 201)

        # 确认与发布
        self.assertEqual(client.post(f"/proposals/{pid}/confirm")[0], 201)
        status, published = client.post(f"/proposals/{pid}/publish")
        self.assertEqual(status, 201)
        self.assertEqual(published["status"], "published")

        # 发布后影响：PO 被豁免，MO-003 适用，MO-004 因已下达不适用
        _, after = client.get(f"/proposals/{pid}/impact")
        po_after = next(i for i in after["direct"] if i["ref_id"] == "PO-2026-001")
        self.assertTrue(po_after["exempted"])
        self.assertFalse(po_after["applicable"])
        by_id = {i["ref_id"]: i for i in after["indirect"]}
        self.assertTrue(by_id["MO-2026-003"]["applicable"])
        self.assertFalse(by_id["MO-2026-004"]["applicable"])

        # 版本历史
        _, versions = client.get(f"/proposals/{pid}/versions")
        self.assertEqual(len(versions["items"]), 4)
        self.assertEqual(versions["items"][-1]["status"], "published")

        # 撤回
        status, withdrawn = client.post(
            f"/proposals/{pid}/withdraw", {"reason": "重新认证"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(withdrawn["status"], "withdrawn")
        _, versions = client.get(f"/proposals/{pid}/versions")
        self.assertEqual(len(versions["items"]), 5)

    def test_cycle_detected_rejected(self) -> None:
        client = self.client()
        # 构造 A→B→C 后尝试 C→A
        for pid in ("LP-A", "LP-B", "LP-C"):
            self.assertEqual(client.post("/parts", {"id": pid, "name": pid})[0], 201)
        self.assertEqual(client.post("/substitutions",
                                    {"old_part_id": "LP-A", "new_part_id": "LP-B"})[0], 201)
        self.assertEqual(client.post("/substitutions",
                                    {"old_part_id": "LP-B", "new_part_id": "LP-C"})[0], 201)
        status, body = client.post("/substitutions",
                                   {"old_part_id": "LP-C", "new_part_id": "LP-A"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "cycle_detected")
        self.assertIn("cycles", body)

    def test_unknown_role_rejected(self) -> None:
        status, body = self.client("guest").get("/health")
        self.assertEqual(status, 401)

    def test_not_found(self) -> None:
        status, body = self.client().get("/parts/NOPE/impact")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
