"""跨业务依赖图：多层 BOM 向上遍历、业务引用归集、循环检测。

依赖方向：

    业务单据 --引用--> 部件
    父部件 --BOM 行包含--> 子部件

停用某部件时需要沿 BOM 边**反向向上**遍历，找到所有（直接或多层）依赖它的
父部件，再归集这些父部件上的采购订单 / 在途批次 / 生产任务。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import models
from .repository import Repository


@dataclass(frozen=True)
class _BomEdge:
    parent_id: str
    child_id: str
    bom_id: str
    version: int
    qty: float


class DependencyGraph:
    """基于某一时刻仓储快照构建的不可变依赖图。"""

    def __init__(self, repo: Repository):
        self._reverse: dict[str, list[_BomEdge]] = {}
        for bom in repo.all_effective_boms():
            for line in bom.lines:
                edge = _BomEdge(
                    parent_id=bom.parent_id,
                    child_id=line.component_id,
                    bom_id=bom.bom_id,
                    version=bom.version,
                    qty=line.qty,
                )
                self._reverse.setdefault(line.component_id, []).append(edge)
        self._refs_by_part: dict[str, list[models.BusinessRef]] = {}
        for ref in repo.all_refs():
            self._refs_by_part.setdefault(ref.part_id, []).append(ref)

    # ---- BOM 向上遍历 ----

    def _ancestors(self, target: str) -> tuple[dict[str, list[_BomEdge]], list[models.Cycle]]:
        """BFS 求 target 到每个祖先的最短向上边序列，并检测可达 BOM 环。"""
        paths: dict[str, list[_BomEdge]] = {target: []}
        queue = [target]
        head = 0
        while head < len(queue):
            current = queue[head]
            head += 1
            for edge in self._reverse.get(current, ()):
                if edge.parent_id not in paths:
                    paths[edge.parent_id] = paths[current] + [edge]
                    queue.append(edge.parent_id)
        return paths, self._detect_bom_cycles(target)

    def _detect_bom_cycles(self, start: str) -> list[models.Cycle]:
        """从 start 可达的 BOM 反向边中找环（祖先包含链回到自身）。"""
        cycles: list[models.Cycle] = []
        reported: set[tuple[str, ...]] = set()
        stack: list[str] = []
        on_stack: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            visited.add(node)
            stack.append(node)
            on_stack.add(node)
            for edge in self._reverse.get(node, ()):
                if edge.parent_id in on_stack:
                    idx = stack.index(edge.parent_id)
                    loop = stack[idx:] + [edge.parent_id]
                    key = tuple(sorted(set(loop)))
                    if key not in reported:
                        reported.add(key)
                        cycles.append(
                            models.Cycle(
                                kind="bom",
                                nodes=loop,
                                detail=(
                                    f"BOM 循环：{' 包含 '.join(loop)}"
                                    f"（{edge.bom_id} v{edge.version}）"
                                ),
                            )
                        )
                elif edge.parent_id not in visited:
                    visit(edge.parent_id)
            on_stack.remove(node)
            stack.pop()

        visit(start)
        return cycles

    # ---- 影响分析 ----

    def impact(self, target_part_id: str) -> models.ImpactReport:
        paths, cycles = self._ancestors(target_part_id)
        report = models.ImpactReport(
            target_part_id=target_part_id, replacement_part_id=None
        )
        for ancestor, upward in paths.items():
            for ref in self._refs_by_part.get(ancestor, []):
                item = self._build_item(target_part_id, ancestor, upward, ref)
                if ancestor == target_part_id:
                    report.direct.append(item)
                else:
                    report.indirect.append(item)
        report.cycles = cycles
        report.direct.sort(key=lambda i: i.ref_id)
        report.indirect.sort(key=lambda i: (i.part_id, i.ref_id))
        return report

    def _build_item(
        self,
        target: str,
        owner_part: str,
        upward: list[_BomEdge],
        ref: models.BusinessRef,
    ) -> models.ImpactItem:
        """构造可解释影响项；path 自业务单据向下追溯到停用部件。"""
        path: list[models.PathHop] = [
            models.PathHop(
                kind="ref",
                label=(
                    f"{models.REF_TYPE_LABELS.get(ref.type, ref.type)} {ref.id}"
                    f" 引用部件 {ref.part_id}"
                ),
                detail={
                    "ref_id": ref.id,
                    "ref_type": ref.type,
                    "state": ref.state,
                    "business_date": ref.business_date,
                    "qty": ref.qty,
                    "part_id": ref.part_id,
                },
            )
        ]
        # upward 为 target -> ... -> owner，反向展开为 owner -> ... -> target
        for edge in reversed(upward):
            path.append(
                models.PathHop(
                    kind="bom",
                    label=(
                        f"部件 {edge.parent_id} 的 BOM {edge.bom_id} v{edge.version}"
                        f" 包含 {edge.child_id}（用量 {edge.qty:g}）"
                    ),
                    detail={
                        "bom_id": edge.bom_id,
                        "version": edge.version,
                        "parent_id": edge.parent_id,
                        "component_id": edge.child_id,
                        "qty": edge.qty,
                    },
                )
            )
        if owner_part != target:
            path.append(
                models.PathHop(
                    kind="target",
                    label=f"多层依赖终点：停用部件 {target}",
                    detail={"part_id": target},
                )
            )
        return models.ImpactItem(
            ref_id=ref.id,
            ref_type=ref.type,
            part_id=ref.part_id,
            qty=ref.qty,
            state=ref.state,
            business_date=ref.business_date,
            dependency="direct" if owner_part == target else "indirect",
            path=path,
            supplier_id=ref.supplier_id,
            unit_price=ref.unit_price,
        )


# ---- 替代链循环检测 ----


def detect_substitution_cycles(
    repo: Repository, start: Optional[str] = None
) -> list[models.Cycle]:
    """检测替代链中的环（A→B→C→A）。

    ``start`` 给定时只遍历从其可达的边；否则检查全图。
    """
    graph: dict[str, list[str]] = {}
    for link in repo.all_sub_links():
        if link.active:
            graph.setdefault(link.old_part_id, []).append(link.new_part_id)

    cycles: list[models.Cycle] = []
    reported: set[tuple[str, ...]] = set()
    stack: list[str] = []
    on_stack: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        visited.add(node)
        stack.append(node)
        on_stack.add(node)
        for nxt in graph.get(node, ()):
            if nxt in on_stack:
                idx = stack.index(nxt)
                loop = stack[idx:] + [nxt]
                key = tuple(sorted(set(loop)))
                if key not in reported:
                    reported.add(key)
                    cycles.append(
                        models.Cycle(
                            kind="substitution",
                            nodes=loop,
                            detail="替代链循环：" + " → ".join(loop),
                        )
                    )
            elif nxt not in visited:
                visit(nxt)
        on_stack.remove(node)
        stack.pop()

    if start is not None:
        visit(start)
    else:
        for node in list(graph):
            if node not in visited:
                visit(node)
    return cycles


def would_create_substitution_cycle(repo: Repository, old: str, new: str) -> list[str]:
    """判断新增活动边 old→new 是否成环：new 能否沿现有边回到 old。

    返回闭环节点序列；为空表示无环。
    """
    if old == new:
        return [old, new]
    stack = [new]
    parent: dict[str, str] = {new: ""}
    while stack:
        node = stack.pop()
        for link in repo.sub_targets(node):
            nxt = link.new_part_id
            if nxt == old:
                chain: list[str] = []
                cur = node
                while cur:
                    chain.append(cur)
                    cur = parent.get(cur, "")
                return [old, *reversed(chain), old]
            if nxt not in parent:
                parent[nxt] = node
                stack.append(nxt)
    return []
