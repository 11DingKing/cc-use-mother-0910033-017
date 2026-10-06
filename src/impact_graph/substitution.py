"""部件替代链：版本化的替代范围、临时豁免、分阶段生效与撤回。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .graph import _find_cycles
from .models import BusinessRef


class SubstitutionCycleError(ValueError):
    """替代链成环。"""


class VersionStatus(str, Enum):
    DRAFT = "草拟"
    PUBLISHED = "已发布"
    WITHDRAWN = "已撤回"


@dataclass(frozen=True)
class Phase:
    """分阶段生效：自 effective_from 起对指定工厂生效，空工厂集表示全部。"""

    effective_from: date
    plants: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Exemption:
    """临时豁免：until 及之前，命中的业务引用暂不适用本替代。"""

    until: date
    ref_ids: frozenset[str] = frozenset()
    part_ids: frozenset[str] = frozenset()
    plants: frozenset[str] = frozenset()
    reason: str = ""

    def covers(self, ref: BusinessRef, today: date) -> bool:
        if today > self.until:
            return False
        return (
            ref.ref_id in self.ref_ids
            or ref.part_id in self.part_ids
            or (bool(self.plants) and ref.plant in self.plants)
        )


@dataclass
class SubstitutionVersion:
    """替代规则的一个版本：范围、阶段、豁免与发布/撤回共同决定适用性。"""

    version: int
    scope_plants: frozenset[str] = frozenset()
    phases: tuple[Phase, ...] = ()
    exemptions: tuple[Exemption, ...] = ()
    note: str = ""
    status: VersionStatus = VersionStatus.DRAFT
    published_at: date | None = None
    withdrawn_at: date | None = None

    def active_phase(self, day: date) -> Phase | None:
        """某日实际生效的阶段；未配置阶段时视为发布即生效。"""
        current: Phase | None = None
        for phase in sorted(self.phases, key=lambda p: p.effective_from):
            if phase.effective_from <= day:
                current = phase
        if current is None and not self.phases and self.published_at is not None:
            if self.published_at <= day:
                return Phase(self.published_at)
        return current

    def applies_to(self, ref: BusinessRef, today: date) -> bool:
        """发布后只影响符合条件（范围、阶段、未豁免）的未来业务。"""
        if self.status is not VersionStatus.PUBLISHED or self.published_at is None:
            return False
        if ref.created_at < self.published_at:
            return False  # 只约束发布之后产生的业务
        if self.scope_plants and ref.plant not in self.scope_plants:
            return False
        phase = self.active_phase(ref.created_at)
        if phase is None:
            return False  # 尚未进入任何生效阶段
        if phase.plants and ref.plant not in phase.plants:
            return False
        return not any(exemption.covers(ref, today) for exemption in self.exemptions)


@dataclass
class SubstitutionRule:
    rule_id: str
    from_part: str  # 被替代部件
    to_part: str  # 替代料
    versions: list[SubstitutionVersion] = field(default_factory=list)

    def next_version(self) -> int:
        return len(self.versions) + 1

    def is_live(self) -> bool:
        """仍有未撤回的版本（或尚未建版本）的规则参与循环检测。"""
        return not self.versions or any(v.status is not VersionStatus.WITHDRAWN for v in self.versions)


class SubstitutionChain:
    """替代链：维护规则集合并保证不成环。"""

    def __init__(self) -> None:
        self._rules: dict[str, SubstitutionRule] = {}

    def add_rule(self, rule: SubstitutionRule) -> None:
        if rule.from_part == rule.to_part:
            raise SubstitutionCycleError(f"{rule.from_part} 不能替代自身")
        if rule.rule_id in self._rules:
            raise ValueError(f"替代规则 {rule.rule_id} 已存在")
        if self._is_reachable(rule.to_part, rule.from_part):
            raise SubstitutionCycleError(
                f"替代链成环：{rule.to_part} 经已有规则已可追溯到 {rule.from_part}"
            )
        self._rules[rule.rule_id] = rule

    def get(self, rule_id: str) -> SubstitutionRule:
        return self._rules[rule_id]

    def rules_from(self, part_id: str) -> list[SubstitutionRule]:
        return [rule for rule in self._rules.values() if rule.from_part == part_id]

    def find_cycles(self) -> list[str]:
        """对现行（未全部撤回）规则做防御性循环检测。"""
        adjacency: dict[str, set[str]] = {}
        for rule in self._rules.values():
            if rule.is_live():
                adjacency.setdefault(rule.from_part, set()).add(rule.to_part)
        return _find_cycles(adjacency)

    def _edges(self) -> dict[str, set[str]]:
        edges: dict[str, set[str]] = {}
        for rule in self._rules.values():
            if rule.is_live():
                edges.setdefault(rule.from_part, set()).add(rule.to_part)
        return edges

    def _is_reachable(self, start: str, target: str) -> bool:
        edges = self._edges()
        stack = [start]
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(edges.get(node, ()))
        return False
