"""业务引用图：BOM 依赖索引、多层反查（where-used）与循环检测。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date

from .models import BomVersion


class CycleError(ValueError):
    """新增 BOM 依赖会引入循环。"""


@dataclass(frozen=True)
class WhereUsedStep:
    """反查路径上的一步：child 经某 BOM 版本被 parent 使用。"""

    bom_id: str
    bom_version: int
    child_id: str
    parent_id: str
    quantity: float


class DependencyGraph:
    """维护 BOM 版本集合，提供多层反查与循环检测。"""

    def __init__(self) -> None:
        self._versions: dict[str, list[BomVersion]] = {}

    def add_version(self, bom: BomVersion) -> None:
        versions = self._versions.setdefault(bom.bom_id, [])
        if any(v.version == bom.version for v in versions):
            raise ValueError(f"BOM {bom.bom_id} 的版本 {bom.version} 已存在")
        for item in bom.items:
            if item.child_id == bom.parent_id:
                raise CycleError(f"{bom.parent_id} 不能直接包含自身")
            if self._is_reachable(item.child_id, bom.parent_id):
                raise CycleError(
                    f"BOM {bom.bom_id} 会引入循环：{item.child_id} 的下游已包含 {bom.parent_id}"
                )
        versions.append(bom)

    def parents_of(self, child_id: str, at: date) -> list[WhereUsedStep]:
        """某日期下直接引用 child 的 BOM 版本。"""
        steps: list[WhereUsedStep] = []
        for versions in self._versions.values():
            for bom in versions:
                if not bom.covers(at):
                    continue
                for item in bom.items:
                    if item.child_id == child_id:
                        steps.append(
                            WhereUsedStep(bom.bom_id, bom.version, child_id, bom.parent_id, item.quantity)
                        )
        return steps

    def ancestor_paths(self, part_id: str, at: date) -> list[tuple[WhereUsedStep, ...]]:
        """从部件向上的全部多层反查路径，每条路径终止于某个祖先部件。"""
        results: list[tuple[WhereUsedStep, ...]] = []
        queue: deque[tuple[str, tuple[WhereUsedStep, ...]]] = deque([(part_id, ())])
        while queue:
            current, path = queue.popleft()
            on_path = {part_id} | {step.parent_id for step in path}
            for step in self.parents_of(current, at):
                if step.parent_id in on_path:
                    continue  # 防御性跳过环路
                new_path = path + (step,)
                results.append(new_path)
                queue.append((step.parent_id, new_path))
        return results

    def find_cycles(self) -> list[str]:
        """对全部 BOM 版本做防御性循环检测，返回可读的环描述。"""
        adjacency: dict[str, set[str]] = {}
        for versions in self._versions.values():
            for bom in versions:
                adjacency.setdefault(bom.parent_id, set()).update(bom.child_ids())
        return _find_cycles(adjacency)

    def _children_of(self, part_id: str) -> list[str]:
        children: list[str] = []
        for versions in self._versions.values():
            for bom in versions:
                if bom.parent_id == part_id:
                    children.extend(bom.child_ids())
        return children

    def _is_reachable(self, start: str, target: str) -> bool:
        stack = [start]
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self._children_of(node))
        return False


def _find_cycles(adjacency: dict[str, set[str]]) -> list[str]:
    """三色 DFS 找环，环以 "A → B → A" 形式返回。"""
    cycles: list[str] = []
    color: dict[str, int] = {}  # 1=在栈中 2=已完成
    stack: list[str] = []

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in adjacency.get(node, ()):
            state = color.get(nxt, 0)
            if state == 0:
                dfs(nxt)
            elif state == 1:
                idx = stack.index(nxt)
                cycles.append(" → ".join(stack[idx:] + [nxt]))
        stack.pop()
        color[node] = 2

    for node in list(adjacency):
        if color.get(node, 0) == 0:
            dfs(node)
    return cycles
