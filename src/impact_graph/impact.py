"""影响分析：直接/间接影响计算与逐条路径解释。"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from .graph import DependencyGraph, WhereUsedStep
from .models import BusinessRef

DIRECT = "直接"
INDIRECT = "间接"


@dataclass(frozen=True)
class Impact:
    """一条影响：命中的业务引用或 BOM，以及从目标部件到它的完整路径。"""

    impact_id: str
    level: str  # 直接 / 间接
    target_part: str
    ref: BusinessRef | None
    bom_id: str | None
    path: tuple[WhereUsedStep, ...]
    explanation: str


@dataclass(frozen=True)
class ImpactReport:
    proposal_id: str
    target_part: str
    evaluated_at: date
    impacts: tuple[Impact, ...]
    cycles: tuple[str, ...]

    @property
    def direct(self) -> tuple[Impact, ...]:
        return tuple(impact for impact in self.impacts if impact.level == DIRECT)

    @property
    def indirect(self) -> tuple[Impact, ...]:
        return tuple(impact for impact in self.impacts if impact.level == INDIRECT)

    @property
    def has_cycle(self) -> bool:
        return bool(self.cycles)


def explain_path(target_part: str, path: tuple[WhereUsedStep, ...], ref: BusinessRef | None) -> str:
    """把一条路径翻译成可读的因果链。"""
    segments: list[str] = []
    current = target_part
    for step in path:
        segments.append(f"{current} 经 {step.bom_id}@v{step.bom_version} 被 {step.parent_id} 使用")
        current = step.parent_id
    if ref is not None:
        segments.append(f"{ref.kind.value} {ref.ref_id}（{ref.state.value}）引用 {current}")
    return "；".join(segments)


def compute_impacts(
    target_part: str,
    graph: DependencyGraph,
    refs: Iterable[BusinessRef],
    at: date,
    prefix: str,
) -> tuple[Impact, ...]:
    """计算停用 target_part 的直接与间接影响。

    直接：目标部件上未关闭的业务引用，以及直接包含它的 BOM 版本。
    间接：沿多层反查路径向上的祖先部件，其未关闭的业务引用。
    """
    open_refs = [ref for ref in refs if ref.is_open]
    impacts: list[Impact] = []
    seq = 0

    def next_id() -> str:
        nonlocal seq
        seq += 1
        return f"{prefix}-{seq:03d}"

    for ref in open_refs:
        if ref.part_id == target_part:
            impacts.append(
                Impact(next_id(), DIRECT, target_part, ref, None, (), explain_path(target_part, (), ref))
            )

    seen_boms: set[tuple[str, int]] = set()
    for step in graph.parents_of(target_part, at):
        key = (step.bom_id, step.bom_version)
        if key in seen_boms:
            continue
        seen_boms.add(key)
        impacts.append(
            Impact(
                next_id(), DIRECT, target_part, None, step.bom_id, (step,),
                explain_path(target_part, (step,), None),
            )
        )

    for path in graph.ancestor_paths(target_part, at):
        ancestor = path[-1].parent_id
        for ref in open_refs:
            if ref.part_id == ancestor:
                impacts.append(
                    Impact(next_id(), INDIRECT, target_part, ref, None, path, explain_path(target_part, path, ref))
                )
    return tuple(impacts)
