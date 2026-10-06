"""主数据应用服务：部件、供应商、BOM 版本、业务引用、替代链。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from . import graph, models
from .errors import Conflict, CycleDetected, NotFound, ValidationFailed
from .repository import Repository


class CatalogService:
    def __init__(self, repo: Repository, now: Optional[Callable[[], datetime]] = None):
        self.repo = repo
        self._now = now

    def _ts(self) -> str:
        return self._now().isoformat() if self._now else self.repo.now_iso()

    # ---- 供应商 / 部件 ----

    def create_supplier(self, supplier_id: str, name: str) -> models.Supplier:
        supplier = models.Supplier(id=supplier_id, name=name)
        self.repo.add_supplier(supplier)
        return supplier

    def create_part(
        self,
        part_id: str,
        name: str,
        supplier_ids: Optional[list[str]] = None,
        unit_cost: Optional[float] = None,
    ) -> models.Part:
        if part_id in self.repo.parts:
            raise Conflict(f"部件已存在：{part_id}")
        for sid in supplier_ids or []:
            if sid not in self.repo.suppliers:
                raise ValidationFailed(f"供应商不存在：{sid}")
        part = models.Part(
            id=part_id,
            name=name,
            supplier_ids=list(supplier_ids or []),
            unit_cost=unit_cost,
        )
        self.repo.add_part(part)
        return part

    def list_parts(self) -> list[models.Part]:
        return sorted(self.repo.parts.values(), key=lambda p: p.id)

    # ---- BOM 版本 ----

    def create_bom_version(
        self,
        bom_id: str,
        parent_id: str,
        lines: list[dict[str, Any]],
        effective_from: Optional[str] = None,
        effective: bool = False,
    ) -> models.BomVersion:
        """新建 BOM 版本；版本号在同一 bom_id 内自增。

        effective=True 时直接置为生效（要求 effective_from），同父部件其它已生效
        版本自动转 obsolete（同一时间只允许一个生效版本）。
        """
        parent = self.repo.require_part(parent_id)
        if not lines:
            raise ValidationFailed("BOM 至少需要一行")
        parsed_lines = []
        for raw in lines:
            component_id = raw["component_id"]
            self.repo.require_part(component_id)
            qty = float(raw.get("qty", 1))
            if qty <= 0:
                raise ValidationFailed(f"部件 {component_id} 用量必须为正数")
            parsed_lines.append(
                models.BomLine(
                    component_id=component_id, qty=qty, note=raw.get("note", "")
                )
            )
        versions = self.repo.get_bom_versions(bom_id)
        next_version = (versions[-1].version + 1) if versions else 1
        if effective and not effective_from:
            raise ValidationFailed("生效版本必须提供 effective_from")
        if effective_from:
            models.parse_date(effective_from)
        version = models.BomVersion(
            bom_id=bom_id,
            parent_id=parent.id,
            version=next_version,
            state=models.BOM_EFFECTIVE if effective else models.BOM_DRAFT,
            effective_from=effective_from,
            lines=parsed_lines,
            created_at=self._ts(),
        )
        # 自环与重复子项
        if any(line.component_id == parent_id for line in parsed_lines):
            raise ValidationFailed(f"BOM {bom_id} 不能直接包含父部件自身 {parent_id}")
        self._assert_no_bom_cycle(version)

        if effective:
            for other in self.repo.all_effective_boms():
                if other.parent_id == parent_id:
                    other.state = models.BOM_OBSOLETE
        self.repo.save_bom_version(version)
        return version

    def _assert_no_bom_cycle(self, candidate: models.BomVersion) -> None:
        """以“候选版本已加入”的假设做 BOM 环检测。"""
        reverse: dict[str, set[str]] = {}
        for bom in self.repo.all_effective_boms():
            for line in bom.lines:
                reverse.setdefault(line.component_id, set()).add(bom.parent_id)
        # 草稿候选同样纳入检测，防止发布时才发现环
        for versions in self.repo.boms.values():
            for bom in versions.values():
                if bom.parent_id == candidate.parent_id:
                    continue
                for line in bom.lines:
                    reverse.setdefault(line.component_id, set()).add(bom.parent_id)
        for line in candidate.lines:
            reverse.setdefault(line.component_id, set()).add(candidate.parent_id)

        # 从候选父部件沿反向边 DFS，能回到自身即有环
        stack = list(reverse.get(candidate.parent_id, ()))
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == candidate.parent_id:
                raise CycleDetected(
                    f"BOM 版本会引入循环依赖：{candidate.parent_id}",
                    cycles=[
                        {
                            "kind": "bom",
                            "nodes": [candidate.parent_id],
                            "detail": "新增 BOM 版本使父部件间接包含自身",
                        }
                    ],
                )
            if node not in seen:
                seen.add(node)
                stack.extend(reverse.get(node, ()))

    def activate_bom_version(self, bom_id: str, version: int, effective_from: str) -> models.BomVersion:
        models.parse_date(effective_from)
        versions = {v.version: v for v in self.repo.get_bom_versions(bom_id)}
        target = versions.get(version)
        if target is None:
            raise NotFound(f"BOM 版本不存在：{bom_id} v{version}")
        self._assert_no_bom_cycle(target)
        for other in self.repo.all_effective_boms():
            if other.parent_id == target.parent_id:
                other.state = models.BOM_OBSOLETE
        target.state = models.BOM_EFFECTIVE
        target.effective_from = effective_from
        return target

    def list_bom_versions(self, bom_id: str) -> list[models.BomVersion]:
        return self.repo.get_bom_versions(bom_id)

    # ---- 业务引用 ----

    def create_ref(
        self,
        ref_id: str,
        ref_type: str,
        part_id: str,
        qty: float,
        state: str,
        business_date: str,
        supplier_id: Optional[str] = None,
        unit_price: Optional[float] = None,
        note: str = "",
    ) -> models.BusinessRef:
        if ref_id in self.repo.refs:
            raise Conflict(f"业务单据已存在：{ref_id}")
        if ref_type not in models.REF_TYPES:
            raise ValidationFailed(f"未知单据类型：{ref_type}")
        if state not in models.DOC_STATES:
            raise ValidationFailed(f"未知单据状态：{state}")
        self.repo.require_part(part_id)
        if supplier_id is not None and supplier_id not in self.repo.suppliers:
            raise ValidationFailed(f"供应商不存在：{supplier_id}")
        models.parse_date(business_date)
        if qty <= 0:
            raise ValidationFailed("数量必须为正数")
        ref = models.BusinessRef(
            id=ref_id,
            type=ref_type,
            part_id=part_id,
            qty=qty,
            state=state,
            business_date=business_date,
            created_at=self._ts(),
            supplier_id=supplier_id,
            unit_price=unit_price,
            note=note,
        )
        self.repo.add_ref(ref)
        return ref

    def transition_ref(self, ref_id: str, new_state: str) -> models.BusinessRef:
        return self.repo.transition_ref(ref_id, new_state)

    def list_refs(self) -> list[models.BusinessRef]:
        return sorted(self.repo.refs.values(), key=lambda r: r.id)

    # ---- 替代链 ----

    def add_substitution(
        self,
        old_part_id: str,
        new_part_id: str,
        scope: str = models.SCOPE_FULL,
        ref_types: Optional[list[str]] = None,
        note: str = "",
    ) -> models.SubstitutionLink:
        self.repo.require_part(old_part_id)
        self.repo.require_part(new_part_id)
        if scope not in (models.SCOPE_FULL, models.SCOPE_PARTIAL):
            raise ValidationFailed(f"未知替代范围：{scope}")
        if scope == models.SCOPE_PARTIAL:
            if not ref_types:
                raise ValidationFailed("限定范围替代必须声明 ref_types")
            unknown = set(ref_types) - set(models.REF_TYPES)
            if unknown:
                raise ValidationFailed("未知单据类型：" + "、".join(sorted(unknown)))
        loop = graph.would_create_substitution_cycle(self.repo, old_part_id, new_part_id)
        if loop:
            raise CycleDetected(
                "替代链不允许出现循环：" + " → ".join(loop),
                cycles=[
                    {"kind": "substitution", "nodes": loop, "detail": "新增替代边使替代链回到自身"}
                ],
            )
        link = models.SubstitutionLink(
            old_part_id=old_part_id,
            new_part_id=new_part_id,
            scope=scope,
            ref_types=list(ref_types) if ref_types else None,
            note=note,
            created_at=self._ts(),
        )
        self.repo.add_sub_link(link)
        return link

    def remove_substitution(self, old_part_id: str, new_part_id: str) -> None:
        self.repo.remove_sub_link(old_part_id, new_part_id)

    def list_substitutions(self) -> list[models.SubstitutionLink]:
        return self.repo.all_sub_links()

    # ---- 即席影响分析（不依赖提案） ----

    def analyze_part(self, part_id: str) -> models.ImpactReport:
        self.repo.require_part(part_id)
        return graph.DependencyGraph(self.repo).impact(part_id)
