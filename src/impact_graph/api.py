"""基于标准库的 HTTP API。

角色通过请求头 ``X-Actor-Role`` 传入（采购计划员/供应商/质量工程师/仓储管理员），
供应商需同时传 ``X-Supplier-Id``；写操作仅采购计划员与质量工程师可用。
"""
from __future__ import annotations

import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .graph import CycleError
from .models import BomItem
from .permissions import AccessDenied, require_role, require_write_role
from .serialize import to_jsonable
from .service import ImpactService
from .substitution import Exemption, Phase, SubstitutionCycleError

ROUTES = [
    ("GET", r"/health", "health"),
    ("POST", r"/parts", "create_part"),
    ("GET", r"/parts/(?P<part_id>[^/]+)", "get_part"),
    ("GET", r"/parts/(?P<part_id>[^/]+)/where-used", "where_used"),
    ("POST", r"/boms", "create_bom"),
    ("POST", r"/refs", "create_ref"),
    ("GET", r"/refs/(?P<ref_id>[^/]+)", "get_ref"),
    ("GET", r"/refs/(?P<ref_id>[^/]+)/substitutions", "ref_substitutions"),
    ("POST", r"/substitutions", "create_substitution"),
    ("POST", r"/substitutions/(?P<rule_id>[^/]+)/versions", "add_version"),
    ("POST", r"/substitutions/(?P<rule_id>[^/]+)/versions/(?P<version>\d+)/publish", "publish_version"),
    ("POST", r"/substitutions/(?P<rule_id>[^/]+)/versions/(?P<version>\d+)/withdraw", "withdraw_version"),
    ("POST", r"/proposals", "create_proposal"),
    ("GET", r"/proposals/(?P<proposal_id>[^/]+)", "get_proposal"),
    ("POST", r"/proposals/(?P<proposal_id>[^/]+)/evaluate", "evaluate_proposal"),
    ("POST", r"/proposals/(?P<proposal_id>[^/]+)/transition", "transition_proposal"),
    ("GET", r"/proposals/(?P<proposal_id>[^/]+)/impact", "proposal_impact"),
]


def _need(body: dict, field: str):
    value = body.get(field)
    if value is None or value == "":
        raise ValueError(f"缺少字段：{field}")
    return value


def _date(value, field: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{field} 必须是 ISO 日期（YYYY-MM-DD）") from None


def _query_date(query: dict, field: str) -> date | None:
    return _date(query.get(field, [None])[0], field)


def make_server(service: ImpactService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):  # noqa: A002 - 保持静默，避免污染日志
            pass

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        # ---- 基础框架 ----

        def _handle(self, method: str) -> None:
            try:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                # 头部按百分号编码传输中文；也接受 role / supplier_id 查询参数
                role = query.get("role", [unquote(self.headers.get("X-Actor-Role", ""))])[0]
                require_role(role)
                if method == "POST":
                    require_write_role(role)
                supplier_id = query.get("supplier_id", [None])[0]
                if supplier_id is None and self.headers.get("X-Supplier-Id"):
                    supplier_id = unquote(self.headers["X-Supplier-Id"])
                body = self._read_body() if method == "POST" else {}
                payload, status = self._dispatch(method, parsed.path, query, body, role, supplier_id)
                self._send(payload, status)
            except AccessDenied as exc:
                self._send({"error": str(exc)}, 403)
            except KeyError as exc:
                self._send({"error": f"未找到：{exc.args[0]}"}, 404)
            except (CycleError, SubstitutionCycleError) as exc:
                self._send({"error": str(exc)}, 409)
            except json.JSONDecodeError:
                self._send({"error": "请求体不是合法 JSON"}, 400)
            except ValueError as exc:
                self._send({"error": str(exc)}, 400)

        def _dispatch(self, method: str, path: str, query: dict, body: dict, role: str, supplier_id: str | None):
            path = path.rstrip("/") or "/"
            for route_method, pattern, handler in ROUTES:
                if route_method != method:
                    continue
                match = re.fullmatch(pattern, path)
                if match:
                    params = {key: unquote(value) for key, value in match.groupdict().items()}
                    return getattr(self, f"_{handler}")(role, supplier_id, query, body, **params)
            raise KeyError(f"{method} {path}")

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _send(self, payload, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---- 路由处理 ----

        def _health(self, role, supplier_id, query, body):
            return {"status": "ok"}, 200

        def _create_part(self, role, supplier_id, query, body):
            part = service.add_part(_need(body, "part_id"), _need(body, "name"))
            return to_jsonable(part), 201

        def _get_part(self, role, supplier_id, query, body, part_id):
            return to_jsonable(service.parts[part_id]), 200

        def _where_used(self, role, supplier_id, query, body, part_id):
            return service.where_used_view(part_id, at=_query_date(query, "at")), 200

        def _create_bom(self, role, supplier_id, query, body):
            items = [
                BomItem(str(item["child_id"]), float(item.get("quantity", 1)))
                for item in body.get("items", [])
            ]
            if not items:
                raise ValueError("BOM 至少需要一个子件")
            bom = service.add_bom_version(
                bom_id=_need(body, "bom_id"),
                parent_id=_need(body, "parent_id"),
                version=int(_need(body, "version")),
                items=items,
                effective_from=_date(_need(body, "effective_from"), "effective_from"),
                effective_to=_date(body.get("effective_to"), "effective_to"),
            )
            return to_jsonable(bom), 201

        def _create_ref(self, role, supplier_id, query, body):
            ref = service.add_ref(
                ref_id=_need(body, "ref_id"),
                kind=_need(body, "kind"),
                part_id=_need(body, "part_id"),
                quantity=float(_need(body, "quantity")),
                created_at=_date(_need(body, "created_at"), "created_at"),
                state=body.get("state", "草拟"),
                plant=body.get("plant", ""),
                supplier_id=body.get("supplier_id"),
                unit_price=body.get("unit_price"),
                contract_terms=body.get("contract_terms"),
            )
            return to_jsonable(ref), 201

        def _get_ref(self, role, supplier_id, query, body, ref_id):
            return service.get_ref_view(ref_id, role, supplier_id), 200

        def _ref_substitutions(self, role, supplier_id, query, body, ref_id):
            matches = service.applicable_substitutions(ref_id, today=_query_date(query, "today"))
            return {"ref_id": ref_id, "matches": matches}, 200

        def _create_substitution(self, role, supplier_id, query, body):
            rule = service.add_substitution(
                _need(body, "rule_id"), _need(body, "from_part"), _need(body, "to_part")
            )
            return to_jsonable(rule), 201

        def _add_version(self, role, supplier_id, query, body, rule_id):
            phases = [
                Phase(
                    effective_from=_date(_need(item, "effective_from"), "phases.effective_from"),
                    plants=frozenset(item.get("plants", [])),
                )
                for item in body.get("phases", [])
            ]
            exemptions = [
                Exemption(
                    until=_date(_need(item, "until"), "exemptions.until"),
                    ref_ids=frozenset(item.get("ref_ids", [])),
                    part_ids=frozenset(item.get("part_ids", [])),
                    plants=frozenset(item.get("plants", [])),
                    reason=item.get("reason", ""),
                )
                for item in body.get("exemptions", [])
            ]
            version = service.add_substitution_version(
                rule_id,
                scope_plants=body.get("scope_plants", []),
                phases=phases,
                exemptions=exemptions,
                note=body.get("note", ""),
            )
            return to_jsonable(version), 201

        def _publish_version(self, role, supplier_id, query, body, rule_id, version):
            result = service.publish_substitution(rule_id, int(version), on=_date(body.get("on"), "on"))
            return to_jsonable(result), 200

        def _withdraw_version(self, role, supplier_id, query, body, rule_id, version):
            result = service.withdraw_substitution(rule_id, int(version), on=_date(body.get("on"), "on"))
            return to_jsonable(result), 200

        def _create_proposal(self, role, supplier_id, query, body):
            proposal = service.create_proposal(
                _need(body, "proposal_id"), _need(body, "target_part"), body.get("action", "停用")
            )
            return to_jsonable(proposal), 201

        def _get_proposal(self, role, supplier_id, query, body, proposal_id):
            return service.proposal_view(proposal_id), 200

        def _evaluate_proposal(self, role, supplier_id, query, body, proposal_id):
            report = service.evaluate_proposal(proposal_id, at=_date(body.get("at"), "at"))
            return {
                "proposal_id": report.proposal_id,
                "target_part": report.target_part,
                "evaluated_at": report.evaluated_at.isoformat(),
                "direct_count": len(report.direct),
                "indirect_count": len(report.indirect),
                "cycles": list(report.cycles),
            }, 200

        def _transition_proposal(self, role, supplier_id, query, body, proposal_id):
            proposal = service.transition_proposal(proposal_id, _need(body, "action"))
            return service.proposal_view(proposal.proposal_id), 200

        def _proposal_impact(self, role, supplier_id, query, body, proposal_id):
            return service.impact_view(proposal_id, role, supplier_id), 200

    return ThreadingHTTPServer((host, port), Handler)


def run(host: str = "127.0.0.1", port: int = 8080, service: ImpactService | None = None) -> None:
    server = make_server(service or ImpactService(), host, port)
    print(f"替代料影响图谱 API 监听 http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
