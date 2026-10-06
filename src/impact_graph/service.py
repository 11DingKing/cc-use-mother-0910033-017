"""应用服务：维护替代链、BOM 版本与业务引用图，计算影响并按权限裁剪。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Iterable

from . import permissions
from .graph import DependencyGraph
from .impact import ImpactReport, compute_impacts, explain_path
from .models import (
    BomItem,
    BomVersion,
    BusinessRef,
    ChangeProposal,
    Part,
    PartStatus,
    ProposalAction,
    RefKind,
    RefState,
)
from .serialize import to_jsonable
from .substitution import (
    Exemption,
    Phase,
    SubstitutionChain,
    SubstitutionRule,
    SubstitutionVersion,
    VersionStatus,
)


class ImpactService:
    """替代料影响图谱的应用入口（内存仓储 + 领域规则）。"""

    def __init__(self) -> None:
        self.parts: dict[str, Part] = {}
        self.refs: dict[str, BusinessRef] = {}
        self.proposals: dict[str, ChangeProposal] = {}
        self.reports: dict[str, ImpactReport] = {}
        self.graph = DependencyGraph()
        self.chain = SubstitutionChain()

    # ---- 基础数据 ----

    def add_part(self, part_id: str, name: str) -> Part:
        if part_id in self.parts:
            raise ValueError(f"部件 {part_id} 已存在")
        part = Part(part_id, name)
        self.parts[part_id] = part
        return part

    def add_bom_version(
        self,
        bom_id: str,
        parent_id: str,
        version: int,
        items: Iterable[BomItem],
        effective_from: date,
        effective_to: date | None = None,
    ) -> BomVersion:
        items = tuple(items)
        missing = sorted({parent_id, *(item.child_id for item in items)} - self.parts.keys())
        if missing:
            raise ValueError("部件不存在：" + "、".join(missing))
        bom = BomVersion(bom_id, parent_id, version, items, effective_from, effective_to)
        self.graph.add_version(bom)
        return bom

    def add_ref(
        self,
        ref_id: str,
        kind: str | RefKind,
        part_id: str,
        quantity: float,
        created_at: date,
        state: str | RefState = RefState.DRAFT,
        plant: str = "",
        supplier_id: str | None = None,
        unit_price: float | None = None,
        contract_terms: str | None = None,
    ) -> BusinessRef:
        if ref_id in self.refs:
            raise ValueError(f"业务引用 {ref_id} 已存在")
        if part_id not in self.parts:
            raise ValueError(f"部件 {part_id} 不存在")
        ref = BusinessRef(
            ref_id=ref_id,
            kind=RefKind(kind),
            part_id=part_id,
            quantity=float(quantity),
            state=RefState(state),
            created_at=created_at,
            plant=plant,
            supplier_id=supplier_id,
            unit_price=unit_price,
            contract_terms=contract_terms,
        )
        self.refs[ref_id] = ref
        return ref

    # ---- 替代链 ----

    def add_substitution(self, rule_id: str, from_part: str, to_part: str) -> SubstitutionRule:
        for part_id in (from_part, to_part):
            if part_id not in self.parts:
                raise ValueError(f"部件 {part_id} 不存在")
        rule = SubstitutionRule(rule_id, from_part, to_part)
        self.chain.add_rule(rule)
        return rule

    def add_substitution_version(
        self,
        rule_id: str,
        scope_plants: Iterable[str] = (),
        phases: Iterable[Phase] = (),
        exemptions: Iterable[Exemption] = (),
        note: str = "",
    ) -> SubstitutionVersion:
        rule = self.chain.get(rule_id)
        version = SubstitutionVersion(
            version=rule.next_version(),
            scope_plants=frozenset(scope_plants),
            phases=tuple(phases),
            exemptions=tuple(exemptions),
            note=note,
        )
        rule.versions.append(version)
        return version

    def publish_substitution(self, rule_id: str, version_no: int, on: date | None = None) -> SubstitutionVersion:
        version = self._version(rule_id, version_no)
        if version.status is not VersionStatus.DRAFT:
            raise ValueError(f"版本 {version_no} 当前为{version.status.value}，不能发布")
        version.status = VersionStatus.PUBLISHED
        version.published_at = on or date.today()
        return version

    def withdraw_substitution(self, rule_id: str, version_no: int, on: date | None = None) -> SubstitutionVersion:
        version = self._version(rule_id, version_no)
        if version.status is VersionStatus.WITHDRAWN:
            raise ValueError(f"版本 {version_no} 已撤回")
        version.status = VersionStatus.WITHDRAWN
        version.withdrawn_at = on or date.today()
        return version

    def applicable_substitutions(self, ref_id: str, today: date | None = None) -> list[dict]:
        """某业务引用当前适用的替代（只含发布后符合条件的未来业务）。"""
        ref = self.refs[ref_id]
        today = today or date.today()
        matches: list[dict] = []
        for rule in self.chain.rules_from(ref.part_id):
            for version in rule.versions:
                if version.applies_to(ref, today):
                    matches.append(
                        {
                            "rule_id": rule.rule_id,
                            "version": version.version,
                            "from_part": rule.from_part,
                            "to_part": rule.to_part,
                        }
                    )
        return matches

    def _version(self, rule_id: str, version_no: int) -> SubstitutionVersion:
        rule = self.chain.get(rule_id)
        for version in rule.versions:
            if version.version == version_no:
                return version
        raise KeyError(f"替代规则 {rule_id} 没有版本 {version_no}")

    # ---- 变更提案 ----

    _TRANSITIONS = {
        "submit": (RefState.DRAFT, RefState.PENDING),
        "return": (RefState.PENDING, RefState.DRAFT),
        "release": (RefState.PENDING, RefState.RELEASED),
        "start": (RefState.RELEASED, RefState.IN_PROGRESS),
        "close": (RefState.IN_PROGRESS, RefState.CLOSED),
    }

    def create_proposal(
        self, proposal_id: str, target_part: str, action: str | ProposalAction = ProposalAction.DEACTIVATE
    ) -> ChangeProposal:
        if proposal_id in self.proposals:
            raise ValueError(f"提案 {proposal_id} 已存在")
        if target_part not in self.parts:
            raise ValueError(f"部件 {target_part} 不存在")
        proposal = ChangeProposal(proposal_id, target_part, ProposalAction(action))
        self.proposals[proposal_id] = proposal
        return proposal

    def evaluate_proposal(self, proposal_id: str, at: date | None = None) -> ImpactReport:
        """先计算直接与间接影响，并对 BOM 图与替代链做循环检测。"""
        proposal = self.proposals[proposal_id]
        at = at or date.today()
        impacts = compute_impacts(proposal.target_part, self.graph, self.refs.values(), at, prefix=proposal_id)
        cycles = tuple(self.chain.find_cycles() + self.graph.find_cycles())
        report = ImpactReport(proposal_id, proposal.target_part, at, impacts, cycles)
        self.reports[proposal_id] = report
        return report

    def transition_proposal(self, proposal_id: str, action: str) -> ChangeProposal:
        proposal = self.proposals[proposal_id]
        if action not in self._TRANSITIONS:
            raise ValueError(f"未知操作：{action}")
        src, dst = self._TRANSITIONS[action]
        if proposal.state is not src:
            raise ValueError(f"提案当前为{proposal.state.value}，不能执行 {action}")
        if action == "submit":
            report = self.reports.get(proposal_id)
            if report is None:
                raise ValueError("请先评估影响再提交")
            if report.has_cycle:
                raise ValueError("存在循环依赖，不能提交：" + "；".join(report.cycles))
        proposal.state = dst
        if action == "release":
            self._apply(proposal)
        return proposal

    def _apply(self, proposal: ChangeProposal) -> None:
        if proposal.action is ProposalAction.DEACTIVATE:
            part = self.parts[proposal.target_part]
            self.parts[part.part_id] = replace(part, status=PartStatus.DEACTIVATED)

    # ---- 查询视图 ----

    def where_used_view(self, part_id: str, at: date | None = None) -> dict:
        if part_id not in self.parts:
            raise KeyError(part_id)
        at = at or date.today()
        paths = [
            {
                "steps": [to_jsonable(step) for step in path],
                "explanation": explain_path(part_id, path, None),
            }
            for path in self.graph.ancestor_paths(part_id, at)
        ]
        return {"part_id": part_id, "at": at.isoformat(), "paths": paths}

    def get_ref_view(self, ref_id: str, role: str, supplier_id: str | None = None) -> dict:
        view = permissions.ref_view(self.refs[ref_id], role, supplier_id)
        if view is None:
            raise permissions.AccessDenied(f"无权查看业务引用 {ref_id}")
        return view

    def proposal_view(self, proposal_id: str) -> dict:
        proposal = self.proposals[proposal_id]
        view = to_jsonable(proposal)
        view["evaluated"] = proposal_id in self.reports
        return view

    def impact_view(self, proposal_id: str, role: str, supplier_id: str | None = None) -> dict:
        """影响报告视图：每条影响带路径解释，商业数据按角色裁剪。"""
        permissions.require_role(role)
        report = self.reports[proposal_id]
        items: list[dict] = []
        hidden = 0
        for impact in report.impacts:
            item = {
                "impact_id": impact.impact_id,
                "level": impact.level,
                "explanation": impact.explanation,
                "path": [to_jsonable(step) for step in impact.path],
            }
            if impact.bom_id is not None:
                item["bom_id"] = impact.bom_id
            if impact.ref is not None:
                view = permissions.ref_view(impact.ref, role, supplier_id)
                if view is None:
                    hidden += 1
                    continue
                item["ref"] = view
            items.append(item)
        return {
            "proposal_id": report.proposal_id,
            "target_part": report.target_part,
            "evaluated_at": report.evaluated_at.isoformat(),
            "cycles": list(report.cycles),
            "direct_count": sum(1 for item in items if item["level"] == "直接"),
            "indirect_count": sum(1 for item in items if item["level"] == "间接"),
            "hidden_count": hidden,
            "impacts": items,
        }
