"""HTTP API 的端到端测试。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from impact_graph.api import make_server
from impact_graph.seed import build_demo_service


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = make_server(build_demo_service(), "127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method: str, path: str, body: dict | None = None,
                role: str | None = "采购计划员", supplier: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = {"Content-Type": "application/json"}
        if role is not None:
            headers["X-Actor-Role"] = quote(role)
        if supplier is not None:
            headers["X-Supplier-Id"] = quote(supplier)
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, payload

    def test_health(self) -> None:
        status, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_missing_or_unknown_role_rejected(self) -> None:
        status, _ = self.request("GET", "/parts/COMP-9", role=None)
        self.assertEqual(status, 403)
        status, _ = self.request("GET", "/parts/COMP-9", role="访客")
        self.assertEqual(status, 403)

    def test_supplier_cannot_write(self) -> None:
        status, payload = self.request(
            "POST", "/parts", {"part_id": "X-1", "name": "x"}, role="供应商", supplier="SUP-A"
        )
        self.assertEqual(status, 403)
        self.assertIn("error", payload)

    def test_bom_cycle_returns_409(self) -> None:
        status, payload = self.request("POST", "/boms", {
            "bom_id": "BOM-LOOP", "parent_id": "COMP-9", "version": 1,
            "items": [{"child_id": "FG-1", "quantity": 1}],
            "effective_from": "2026-01-01",
        })
        self.assertEqual(status, 409)
        self.assertIn("循环", payload["error"])

    def test_where_used_endpoint(self) -> None:
        status, payload = self.request("GET", "/parts/COMP-9/where-used?at=2026-10-06")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(len(p["steps"]) for p in payload["paths"]), [1, 2])

    def test_impact_endpoint_redacts_by_role(self) -> None:
        status, payload = self.request("GET", "/proposals/ECP-001/impact", role="质量工程师")
        self.assertEqual(status, 200)
        refs = [item["ref"] for item in payload["impacts"] if "ref" in item]
        po100 = next(ref for ref in refs if ref["ref_id"] == "PO-100")
        self.assertEqual(po100["unit_price"], "已隐藏")
        self.assertEqual(po100["supplier_id"], "已隐藏")
        explanations = [item["explanation"] for item in payload["impacts"]]
        self.assertTrue(any("BOM-SA1" in text and "BOM-FG1" in text for text in explanations))

    def test_impact_endpoint_supplier_filtered(self) -> None:
        status, payload = self.request(
            "GET", "/proposals/ECP-001/impact", role="供应商", supplier="SUP-A"
        )
        self.assertEqual(status, 200)
        visible = {item["ref"]["ref_id"] for item in payload["impacts"] if "ref" in item}
        self.assertEqual(visible, {"PO-100", "IT-200"})
        self.assertEqual(payload["hidden_count"], 2)

    def test_proposal_flow_and_future_business(self) -> None:
        # 新提案：评估 → 提交 → 下达后部件停用
        status, _ = self.request("POST", "/proposals", {"proposal_id": "ECP-100", "target_part": "COMP-9"})
        self.assertEqual(status, 201)
        status, payload = self.request("POST", "/proposals/ECP-100/evaluate", {"at": "2026-10-06"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["direct_count"], 3)
        self.assertEqual(payload["indirect_count"], 2)
        status, _ = self.request("POST", "/proposals/ECP-100/transition", {"action": "submit"})
        self.assertEqual(status, 200)
        status, _ = self.request("POST", "/proposals/ECP-100/transition", {"action": "release"})
        self.assertEqual(status, 200)
        _, part = self.request("GET", "/parts/COMP-9")
        self.assertEqual(part["status"], "停用")

        # 发布前创建的旧业务不适用替代；发布后符合条件的新业务适用
        status, _ = self.request("POST", "/refs", {
            "ref_id": "PO-900", "kind": "采购订单", "part_id": "COMP-9",
            "quantity": 100, "created_at": "2026-11-05", "state": "已下达", "plant": "P1",
        })
        self.assertEqual(status, 201)
        _, old = self.request("GET", "/refs/PO-100/substitutions?today=2026-11-10")
        self.assertEqual(old["matches"], [])  # 创建于发布日之前
        _, new = self.request("GET", "/refs/PO-900/substitutions?today=2026-11-10")
        self.assertEqual([(m["rule_id"], m["to_part"]) for m in new["matches"]], [("SUB-1", "ALT-9")])

    def test_unknown_route_returns_404(self) -> None:
        status, _ = self.request("GET", "/no-such-thing")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
