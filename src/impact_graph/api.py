"""基于标准库 http.server 的 JSON HTTP API。

鉴权约定（无外部依赖的简化方案，生产应替换为网关/JWT）：
请求头 ``X-Role`` 指定角色，``X-Supplier-Id`` 在供应商角色下必填。

写权限：
- 主数据 / 提案编辑：采购计划员、质量工程师；
- 提案确认 / 发布 / 撤回 / 豁免 / 范围与阶段调整：仅采购计划员。

所有响应中的供应商商业字段经 ``security`` 按角色裁剪。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from . import models
from .catalog import CatalogService
from .errors import DomainError
from .proposal import ProposalService
from .repository import Repository
from .security import (
    ROLE_QUALITY,
    ROLE_PLANNER,
    ROLE_SUPPLIER,
    ROLE_WAREHOUSE,
    Viewer,
    redact_part,
    redact_ref,
    redact_report,
)

EDIT_ROLES = frozenset({ROLE_PLANNER, ROLE_QUALITY})
PLANNER_ONLY = frozenset({ROLE_PLANNER})

# HTTP 头只能传 latin-1，因此 X-Role 使用 ASCII 别名，中文角色名同样接受
ROLE_ALIASES = {
    "planner": ROLE_PLANNER,
    "quality": ROLE_QUALITY,
    "warehouse": ROLE_WAREHOUSE,
    "supplier": ROLE_SUPPLIER,
    ROLE_PLANNER: ROLE_PLANNER,
    ROLE_QUALITY: ROLE_QUALITY,
    ROLE_WAREHOUSE: ROLE_WAREHOUSE,
    ROLE_SUPPLIER: ROLE_SUPPLIER,
}


class ApiContext:
    def __init__(self, repo: Repository, catalog: CatalogService, proposals: ProposalService):
        self.repo = repo
        self.catalog = catalog
        self.proposals = proposals


def _ref_out(ref: models.BusinessRef, viewer: Viewer) -> dict[str, Any]:
    data = redact_ref(ref, viewer)
    data["type_label"] = models.REF_TYPE_LABELS.get(ref.type, ref.type)
    return data


def _proposal_out(proposal: models.ChangeProposal) -> dict[str, Any]:
    data = models.to_data(proposal)
    data["version_count"] = len(proposal.versions)
    data["latest_version"] = proposal.versions[-1].number if proposal.versions else 0
    return data


# 路由表：(method, pattern) -> handler(ctx, params, body, viewer)
ROUTES: list[tuple[str, re.Pattern[str], Callable[..., Any], frozenset[str] | None]] = []


def route(method: str, path: str, roles: Optional[frozenset[str]] = None):
    pattern = re.compile(r"^" + re.sub(r"{([^}]+)}", r"(?P<\1>[^/]+)", path) + r"$")

    def wrap(func: Callable[..., Any]) -> Callable[..., Any]:
        ROUTES.append((method, pattern, func, roles))
        return func

    return wrap


# ---- 主数据 ----


@route("GET", "/health")
def health(ctx: ApiContext, params: dict, body: Any, viewer: Viewer) -> Any:
    return {"status": "ok", "role": viewer.role}


@route("POST", "/suppliers", EDIT_ROLES)
def create_supplier(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    supplier = ctx.catalog.create_supplier(body["id"], body["name"])
    return models.to_data(supplier)


@route("POST", "/parts", EDIT_ROLES)
def create_part(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    part = ctx.catalog.create_part(
        body["id"],
        body["name"],
        supplier_ids=body.get("supplier_ids"),
        unit_cost=body.get("unit_cost"),
    )
    return redact_part(part, viewer)


@route("GET", "/parts")
def list_parts(ctx: ApiContext, params: dict, body: Any, viewer: Viewer) -> Any:
    return {"items": [redact_part(p, viewer) for p in ctx.catalog.list_parts()]}


@route("GET", "/parts/{part_id}/impact")
def part_impact(ctx: ApiContext, params: dict, body: Any, viewer: Viewer) -> Any:
    report = ctx.catalog.analyze_part(params["part_id"])
    return redact_report(report, viewer)


@route("POST", "/boms", EDIT_ROLES)
def create_bom(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    version = ctx.catalog.create_bom_version(
        bom_id=body["bom_id"],
        parent_id=body["parent_id"],
        lines=body["lines"],
        effective_from=body.get("effective_from"),
        effective=bool(body.get("effective", False)),
    )
    return models.to_data(version)


@route("GET", "/boms/{bom_id}")
def get_bom(ctx: ApiContext, params: dict, body: Any, viewer: Viewer) -> Any:
    versions = ctx.catalog.list_bom_versions(params["bom_id"])
    from .errors import NotFound

    if not versions:
        raise NotFound(f"BOM 不存在：{params['bom_id']}")
    return {"bom_id": params["bom_id"], "versions": [models.to_data(v) for v in versions]}


@route("POST", "/boms/{bom_id}/versions/{version}/activate", EDIT_ROLES)
def activate_bom(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    version = ctx.catalog.activate_bom_version(
        params["bom_id"], int(params["version"]), body["effective_from"]
    )
    return models.to_data(version)


@route("POST", "/refs", EDIT_ROLES)
def create_ref(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    ref = ctx.catalog.create_ref(
        ref_id=body["id"],
        ref_type=body["type"],
        part_id=body["part_id"],
        qty=float(body.get("qty", 1)),
        state=body.get("state", models.STATE_DRAFT),
        business_date=body["business_date"],
        supplier_id=body.get("supplier_id"),
        unit_price=body.get("unit_price"),
        note=body.get("note", ""),
    )
    return _ref_out(ref, viewer)


@route("GET", "/refs")
def list_refs(ctx: ApiContext, params: dict, body: Any, viewer: Viewer) -> Any:
    return {"items": [_ref_out(r, viewer) for r in ctx.catalog.list_refs()]}


@route("POST", "/refs/{ref_id}/transition", EDIT_ROLES)
def transition_ref(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    ref = ctx.catalog.transition_ref(params["ref_id"], body["state"])
    return _ref_out(ref, viewer)


@route("POST", "/substitutions", EDIT_ROLES)
def add_substitution(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    link = ctx.catalog.add_substitution(
        old_part_id=body["old_part_id"],
        new_part_id=body["new_part_id"],
        scope=body.get("scope", models.SCOPE_FULL),
        ref_types=body.get("ref_types"),
        note=body.get("note", ""),
    )
    return models.to_data(link)


@route("GET", "/substitutions")
def list_substitutions(ctx: ApiContext, params: dict, body: Any, viewer: Viewer) -> Any:
    links = ctx.catalog.list_substitutions()
    items = []
    for link in links:
        data = models.to_data(link)
        if viewer.role == "供应商":
            # 替代链不含商业字段，但备注可能涉及供应商，统一对供应商角色隐藏备注
            data["note"] = ""
        items.append(data)
    return {"items": items}


# ---- 变更提案 ----


@route("POST", "/proposals", EDIT_ROLES)
def create_proposal(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    proposal = ctx.proposals.create_proposal(
        action=body["action"],
        target_part_id=body["target_part_id"],
        created_by=viewer.role + ":" + str(body.get("created_by", viewer.role)),
        replacement_part_id=body.get("replacement_part_id"),
        scope=body.get("scope"),
        phases=body.get("phases"),
    )
    return _proposal_out(proposal)


@route("GET", "/proposals")
def list_proposals(ctx: ApiContext, params: dict, body: Any, viewer: Viewer) -> Any:
    return {"items": [_proposal_out(p) for p in ctx.proposals.list_proposals()]}


@route("GET", "/proposals/{proposal_id}")
def get_proposal(ctx: ApiContext, params: dict, body: Any, viewer: Viewer) -> Any:
    return _proposal_out(ctx.proposals.get_proposal(params["proposal_id"]))


@route("GET", "/proposals/{proposal_id}/impact")
def proposal_impact(ctx: ApiContext, params: dict, body: Any, viewer: Viewer) -> Any:
    report = ctx.proposals.compute_impact(params["proposal_id"])
    data = redact_report(report, viewer)
    data["proposal_id"] = params["proposal_id"]
    return data


@route("POST", "/proposals/{proposal_id}/scope", PLANNER_ONLY)
def update_scope(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    proposal = ctx.proposals.update_scope(params["proposal_id"], viewer.role, body)
    return _proposal_out(proposal)


@route("POST", "/proposals/{proposal_id}/phases", PLANNER_ONLY)
def set_phases(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    proposal = ctx.proposals.set_phases(
        params["proposal_id"], viewer.role, body.get("phases", [])
    )
    return _proposal_out(proposal)


@route("POST", "/proposals/{proposal_id}/exemptions", PLANNER_ONLY)
def add_exemption(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    exemption = ctx.proposals.add_exemption(
        proposal_id=params["proposal_id"],
        author=viewer.role,
        valid_from=body["valid_from"],
        valid_to=body["valid_to"],
        reason=body["reason"],
        ref_id=body.get("ref_id"),
        part_id=body.get("part_id"),
    )
    return models.to_data(exemption)


@route("POST", "/proposals/{proposal_id}/confirm", PLANNER_ONLY)
def confirm_proposal(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    return _proposal_out(ctx.proposals.confirm(params["proposal_id"], viewer.role))


@route("POST", "/proposals/{proposal_id}/publish", PLANNER_ONLY)
def publish_proposal(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    return _proposal_out(ctx.proposals.publish(params["proposal_id"], viewer.role))


@route("POST", "/proposals/{proposal_id}/withdraw", PLANNER_ONLY)
def withdraw_proposal(ctx: ApiContext, params: dict, body: dict, viewer: Viewer) -> Any:
    return _proposal_out(
        ctx.proposals.withdraw(params["proposal_id"], viewer.role, body.get("reason", ""))
    )


@route("GET", "/proposals/{proposal_id}/versions")
def list_versions(ctx: ApiContext, params: dict, body: Any, viewer: Viewer) -> Any:
    versions = ctx.proposals.list_versions(params["proposal_id"])
    return {"items": [models.to_data(v) for v in versions]}


@route("GET", "/proposals/{proposal_id}/versions/{number}")
def get_version(ctx: ApiContext, params: dict, body: Any, viewer: Viewer) -> Any:
    version = ctx.proposals.get_version(params["proposal_id"], int(params["number"]))
    return models.to_data(version)


# ---- HTTP 装配 ----


class ImpactGraphHandler(BaseHTTPRequestHandler):
    ctx: ApiContext = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默，测试输出干净
        return

    def _send(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _viewer(self) -> Optional[Viewer]:
        raw_role = self.headers.get("X-Role", "planner")
        role = ROLE_ALIASES.get(raw_role)
        if role is None:
            self._send(401, {"error": "unauthorized", "message": f"未知角色：{raw_role}"})
            return None
        supplier_id = self.headers.get("X-Supplier-Id")
        try:
            return Viewer(role=role, supplier_id=supplier_id)
        except ValueError as exc:
            self._send(401, {"error": "unauthorized", "message": str(exc)})
            return None

    def _dispatch(self, method: str) -> None:
        viewer = self._viewer()
        if viewer is None:
            return
        path = urlparse(self.path).path
        body: Any = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except json.JSONDecodeError as exc:
                    self._send(400, {"error": "bad_json", "message": str(exc)})
                    return
        for verb, pattern, handler, roles in ROUTES:
            match = pattern.match(path)
            if verb == method and match:
                if roles is not None and viewer.role not in roles:
                    self._send(
                        403,
                        {
                            "error": "forbidden",
                            "message": f"角色 {viewer.role} 无权执行该操作",
                        },
                    )
                    return
                try:
                    result = handler(self.ctx, match.groupdict(), body, viewer)
                    self._send(200 if method == "GET" else 201, result)
                except DomainError as exc:
                    payload = {"error": exc.code, "message": str(exc)}
                    if hasattr(exc, "cycles") and exc.cycles:
                        payload["cycles"] = exc.cycles
                    self._send(exc.status, payload)
                except KeyError as exc:
                    self._send(422, {"error": "validation_failed", "message": f"缺少字段：{exc}"})
                return
        self._send(404, {"error": "not_found", "message": f"无此路由：{method} {path}"})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


def build_server(ctx: ApiContext, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (ImpactGraphHandler,), {"ctx": ctx})
    server = ThreadingHTTPServer((host, port), handler)
    return server
