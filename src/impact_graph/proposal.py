"""变更提案应用服务。

提案生命周期：草拟 draft → 待确认 confirmed → 已发布 published；
任意非撤回状态可撤回（withdrawn 为终态）。

每次配置变更（范围调整、豁免增删、分阶段、发布、撤回）都追加一条**不可变版本**，
版本保存当时的配置快照与影响摘要，支持回溯与审计。
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Callable, Optional

from . import graph, models, policy
from .errors import Conflict, CycleDetected, NotFound, ValidationFailed
from .repository import Repository


class ProposalService:
    def __init__(self, repo: Repository, now: Optional[Callable[[], datetime]] = None):
        self.repo = repo
        self._now = now

    def _ts(self) -> str:
        return self._now().isoformat() if self._now else self.repo.now_iso()

    # ---- 创建 ----

    def create_proposal(
        self,
        action: str,
        target_part_id: str,
        created_by: str,
        replacement_part_id: Optional[str] = None,
        scope: Optional[dict[str, Any]] = None,
        phases: Optional[list[dict[str, Any]]] = None,
    ) -> models.ChangeProposal:
        if action not in (models.ACTION_DISCONTINUE, models.ACTION_SUBSTITUTE):
            raise ValidationFailed(f"未知变更动作：{action}")
        self.repo.require_part(target_part_id)
        if action == models.ACTION_SUBSTITUTE:
            if not replacement_part_id:
                raise ValidationFailed("替代提案必须指定 replacement_part_id")
            self.repo.require_part(replacement_part_id)
            if replacement_part_id == target_part_id:
                raise ValidationFailed("替代料不能与停用部件相同")
            loop = graph.would_create_substitution_cycle(
                self.repo, target_part_id, replacement_part_id
            )
            if loop:
                raise CycleDetected(
                    "提案替代关系会使替代链成环：" + " → ".join(loop),
                    cycles=[{"kind": "substitution", "nodes": loop}],
                )

        proposal_scope = self._build_scope(scope or {})
        policy.validate_scope(proposal_scope)
        proposal_phases = self._build_phases(phases or [])
        policy.validate_phases(proposal_phases)

        proposal = models.ChangeProposal(
            id=self.repo.next_id("CP-"),
            action=action,
            target_part_id=target_part_id,
            replacement_part_id=replacement_part_id,
            scope=proposal_scope,
            phases=proposal_phases,
            created_by=created_by,
            created_at=self._ts(),
        )
        self.repo.add_proposal(proposal)
        self._snapshot(proposal, created_by, "创建提案")
        return proposal

    def _build_scope(self, raw: dict[str, Any]) -> models.ProposalScope:
        ref_types = raw.get("ref_types")
        if ref_types is not None:
            ref_types = list(ref_types)
        affect_states = raw.get("affect_states") or [
            models.STATE_DRAFT,
            models.STATE_PENDING,
        ]
        return models.ProposalScope(
            ref_types=ref_types,
            affect_states=list(affect_states),
            note=raw.get("note", ""),
        )

    def _build_phases(self, raw: list[dict[str, Any]]) -> list[models.Phase]:
        return [
            models.Phase(
                name=p["name"],
                effective_date=p["effective_date"],
                ref_types=list(p["ref_types"]) if p.get("ref_types") else None,
            )
            for p in raw
        ]

    # ---- 影响分析 ----

    def compute_impact(self, proposal_id: str) -> models.ImpactReport:
        proposal = self.repo.require_proposal(proposal_id)
        report = graph.DependencyGraph(self.repo).impact(proposal.target_part_id)
        return policy.evaluate_report(self.repo, proposal, report)

    def get_proposal(self, proposal_id: str) -> models.ChangeProposal:
        return self.repo.require_proposal(proposal_id)

    def list_proposals(self) -> list[models.ChangeProposal]:
        return self.repo.all_proposals()

    # ---- 范围 / 阶段调整（形成新版本） ----

    def update_scope(
        self, proposal_id: str, author: str, raw: dict[str, Any]
    ) -> models.ChangeProposal:
        proposal = self._require_editable(proposal_id)
        new_scope = self._build_scope(raw)
        policy.validate_scope(new_scope)
        proposal.scope = new_scope
        self._snapshot(proposal, author, "调整替代范围")
        return proposal

    def set_phases(
        self, proposal_id: str, author: str, raw: list[dict[str, Any]]
    ) -> models.ChangeProposal:
        proposal = self._require_editable(proposal_id)
        phases = self._build_phases(raw)
        policy.validate_phases(phases)
        proposal.phases = phases
        self._snapshot(proposal, author, "调整分阶段生效计划")
        return proposal

    # ---- 豁免（形成新版本） ----

    def add_exemption(
        self,
        proposal_id: str,
        author: str,
        valid_from: str,
        valid_to: str,
        reason: str,
        ref_id: Optional[str] = None,
        part_id: Optional[str] = None,
    ) -> models.Exemption:
        proposal = self._require_editable(proposal_id)
        if not ref_id and not part_id:
            raise ValidationFailed("豁免必须指定 ref_id 或 part_id")
        if ref_id and self.repo.get_ref(ref_id) is None:
            raise NotFound(f"业务单据不存在：{ref_id}")
        if part_id:
            self.repo.require_part(part_id)
        policy.validate_exemption_window(valid_from, valid_to)
        exemption = models.Exemption(
            id=self.repo.next_id("EX-"),
            proposal_id=proposal_id,
            ref_id=ref_id,
            part_id=part_id,
            reason=reason,
            valid_from=valid_from,
            valid_to=valid_to,
            created_by=author,
            created_at=self._ts(),
        )
        self.repo.add_exemption(exemption)
        proposal.exemption_ids.append(exemption.id)
        self._snapshot(proposal, author, f"新增临时豁免 {exemption.id}")
        return exemption

    # ---- 状态流转 ----

    def confirm(self, proposal_id: str, author: str) -> models.ChangeProposal:
        proposal = self.repo.require_proposal(proposal_id)
        if proposal.status != models.PROP_DRAFT:
            raise Conflict(f"提案当前状态 {proposal.status}，不能提交确认")
        report = self.compute_impact(proposal_id)
        if report.cycles:
            raise CycleDetected(
                "依赖图存在循环，不能提交确认",
                cycles=[models.to_data(c) for c in report.cycles],
            )
        proposal.status = models.PROP_CONFIRMED
        self._snapshot(proposal, author, "提交待确认", report)
        return proposal

    def publish(self, proposal_id: str, author: str) -> models.ChangeProposal:
        """发布：登记替代链、标记部件停用。

        历史/在行业务从不被回写；发布后单据是否受影响由 ``policy`` 在查询时
        按发布门槛、阶段、豁免即时判定，因此只影响符合条件的未来业务。
        """
        proposal = self.repo.require_proposal(proposal_id)
        if proposal.status not in (models.PROP_DRAFT, models.PROP_CONFIRMED):
            raise Conflict(f"提案当前状态 {proposal.status}，不能发布")
        report = self.compute_impact(proposal_id)
        if report.cycles:
            raise CycleDetected(
                "依赖图存在循环，不能发布",
                cycles=[models.to_data(c) for c in report.cycles],
            )

        proposal.status = models.PROP_PUBLISHED
        proposal.published_at = self._ts()

        if proposal.action == models.ACTION_SUBSTITUTE:
            existing = self.repo.sub_targets(proposal.target_part_id)
            if not any(
                l.new_part_id == proposal.replacement_part_id for l in existing
            ):
                link = models.SubstitutionLink(
                    old_part_id=proposal.target_part_id,
                    new_part_id=proposal.replacement_part_id,
                    scope=models.SCOPE_FULL,
                    active=True,
                    note=f"由提案 {proposal.id} 发布登记",
                    created_at=proposal.published_at,
                )
                self.repo.add_sub_link(link)
                cycles = graph.detect_substitution_cycles(
                    self.repo, proposal.target_part_id
                )
                if cycles:
                    # create 时已拦截，此处为防御性回滚
                    self.repo.remove_sub_link(link.old_part_id, link.new_part_id)
                    proposal.status = models.PROP_CONFIRMED
                    proposal.published_at = None
                    raise CycleDetected(
                        "发布后替代链成环",
                        cycles=[models.to_data(c) for c in cycles],
                    )

        self.repo.require_part(proposal.target_part_id).status = (
            models.PART_DISCONTINUED
        )

        report = self.compute_impact(proposal_id)
        self._snapshot(proposal, author, "发布提案", report)
        return proposal

    def withdraw(self, proposal_id: str, author: str, reason: str) -> models.ChangeProposal:
        """撤回提案：恢复部件状态、撤销登记的替代链。

        历史单据从未被回写，无需数据补偿；撤回后影响判定随之失效。
        """
        proposal = self.repo.require_proposal(proposal_id)
        if proposal.status == models.PROP_WITHDRAWN:
            raise Conflict("提案已撤回")
        if not reason.strip():
            raise ValidationFailed("撤回必须填写原因")

        if proposal.status == models.PROP_PUBLISHED:
            if proposal.action == models.ACTION_SUBSTITUTE:
                self.repo.remove_sub_link(
                    proposal.target_part_id, proposal.replacement_part_id
                )
            target = self.repo.get_part(proposal.target_part_id)
            if target and target.status == models.PART_DISCONTINUED:
                target.status = models.PART_ACTIVE

        proposal.status = models.PROP_WITHDRAWN
        self._snapshot(proposal, author, f"撤回提案：{reason}")
        return proposal

    # ---- 版本 ----

    def list_versions(self, proposal_id: str) -> list[models.ProposalVersion]:
        return list(self.repo.require_proposal(proposal_id).versions)

    def get_version(self, proposal_id: str, number: int) -> models.ProposalVersion:
        proposal = self.repo.require_proposal(proposal_id)
        for version in proposal.versions:
            if version.number == number:
                return version
        raise NotFound(f"提案 {proposal_id} 不存在版本 {number}")

    def _require_editable(self, proposal_id: str) -> models.ChangeProposal:
        proposal = self.repo.require_proposal(proposal_id)
        if proposal.status == models.PROP_WITHDRAWN:
            raise Conflict("提案已撤回，不可修改")
        if proposal.status == models.PROP_PUBLISHED:
            raise Conflict("提案已发布；如需调整请先撤回，或另提变更提案")
        return proposal

    def _snapshot(
        self,
        proposal: models.ChangeProposal,
        author: str,
        change: str,
        report: Optional[models.ImpactReport] = None,
    ) -> None:
        if report is None:
            try:
                report = self.compute_impact(proposal.id)
            except CycleDetected:
                report = None
        config = {
            "action": proposal.action,
            "target_part_id": proposal.target_part_id,
            "replacement_part_id": proposal.replacement_part_id,
            "scope": models.to_data(proposal.scope),
            "phases": models.to_data(proposal.phases),
            "exemption_ids": list(proposal.exemption_ids),
            "published_at": proposal.published_at,
        }
        if report is not None:
            summary = report.summary()
            summary["cycle_kinds"] = sorted({c.kind for c in report.cycles})
        else:
            summary = {
                "direct": 0,
                "indirect": 0,
                "applicable": 0,
                "cycles": 0,
            }
        proposal.versions.append(
            models.ProposalVersion(
                number=len(proposal.versions) + 1,
                status=proposal.status,
                author=author,
                at=self._ts(),
                change=change,
                config=copy.deepcopy(config),
                impact_summary=summary,
            )
        )
