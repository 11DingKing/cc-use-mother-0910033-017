"""线程安全的内存仓储与可注入时钟。"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from . import models


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


class Repository:
    """集中存放全部领域对象；所有变更经同一把锁串行化。"""

    def __init__(self, clock: Optional[Callable[[], datetime]] = None):
        self._lock = threading.RLock()
        self._clock = clock or default_clock
        self.suppliers: dict[str, models.Supplier] = {}
        self.parts: dict[str, models.Part] = {}
        # bom_id -> 版本号 -> BomVersion
        self.boms: dict[str, dict[int, models.BomVersion]] = defaultdict(dict)
        self.refs: dict[str, models.BusinessRef] = {}
        # 替代链：old -> new -> SubstitutionLink
        self.sub_links: dict[str, dict[str, models.SubstitutionLink]] = defaultdict(dict)
        self.exemptions: dict[str, models.Exemption] = {}
        self.proposals: dict[str, models.ChangeProposal] = {}
        self._seq: dict[str, int] = defaultdict(int)

    # ---- 时钟 ----

    def now(self) -> datetime:
        return self._clock()

    def now_iso(self) -> str:
        return self._clock().isoformat()

    def next_id(self, prefix: str) -> str:
        with self._lock:
            self._seq[prefix] += 1
            return f"{prefix}{self._seq[prefix]:04d}"

    # ---- 供应商 / 部件 ----

    def add_supplier(self, supplier: models.Supplier) -> None:
        with self._lock:
            self.suppliers[supplier.id] = supplier

    def add_part(self, part: models.Part) -> None:
        with self._lock:
            self.parts[part.id] = part

    def get_part(self, part_id: str) -> Optional[models.Part]:
        return self.parts.get(part_id)

    def require_part(self, part_id: str) -> models.Part:
        from .errors import NotFound

        part = self.parts.get(part_id)
        if part is None:
            raise NotFound(f"部件不存在：{part_id}")
        return part

    # ---- BOM ----

    def save_bom_version(self, version: models.BomVersion) -> None:
        with self._lock:
            self.boms[version.bom_id][version.version] = version

    def get_bom_versions(self, bom_id: str) -> list[models.BomVersion]:
        return sorted(self.boms.get(bom_id, {}).values(), key=lambda v: v.version)

    def effective_bom_for(self, parent_id: str, on_date: Optional[str] = None) -> Optional[models.BomVersion]:
        """取父部件当前生效的 BOM 版本。

        状态为 effective 且生效日不晚于 ``on_date`` 的版本中取版本号最大者；
        未给日期时取最新生效版本。
        """
        candidates = []
        for versions in self.boms.values():
            for v in versions.values():
                if v.parent_id != parent_id or v.state != models.BOM_EFFECTIVE:
                    continue
                if on_date and v.effective_from and v.effective_from > on_date:
                    continue
                candidates.append(v)
        return max(candidates, key=lambda v: v.version, default=None)

    def all_effective_boms(self) -> list[models.BomVersion]:
        result = []
        for versions in self.boms.values():
            for v in versions.values():
                if v.state == models.BOM_EFFECTIVE:
                    result.append(v)
        return result

    # ---- 业务引用 ----

    def add_ref(self, ref: models.BusinessRef) -> None:
        with self._lock:
            self.refs[ref.id] = ref

    def get_ref(self, ref_id: str) -> Optional[models.BusinessRef]:
        return self.refs.get(ref_id)

    def all_refs(self) -> Iterable[models.BusinessRef]:
        return list(self.refs.values())

    def transition_ref(self, ref_id: str, new_state: str) -> models.BusinessRef:
        from .errors import Conflict, NotFound

        with self._lock:
            ref = self.refs.get(ref_id)
            if ref is None:
                raise NotFound(f"业务单据不存在：{ref_id}")
            allowed = models.STATE_TRANSITIONS.get(ref.state, frozenset())
            if new_state not in allowed:
                raise Conflict(f"单据 {ref_id} 不能从 {ref.state} 迁移到 {new_state}")
            ref.state = new_state
            return ref

    # ---- 替代链 ----

    def add_sub_link(self, link: models.SubstitutionLink) -> None:
        with self._lock:
            self.sub_links[link.old_part_id][link.new_part_id] = link

    def remove_sub_link(self, old: str, new: str) -> None:
        with self._lock:
            self.sub_links.get(old, {}).pop(new, None)

    def sub_targets(self, old: str) -> list[models.SubstitutionLink]:
        return [l for l in self.sub_links.get(old, {}).values() if l.active]

    def all_sub_links(self) -> list[models.SubstitutionLink]:
        return [l for m in self.sub_links.values() for l in m.values()]

    # ---- 豁免 ----

    def add_exemption(self, exemption: models.Exemption) -> None:
        with self._lock:
            self.exemptions[exemption.id] = exemption

    def get_exemption(self, exemption_id: str) -> Optional[models.Exemption]:
        return self.exemptions.get(exemption_id)

    def exemptions_for(self, proposal_id: str) -> list[models.Exemption]:
        return [e for e in self.exemptions.values() if e.proposal_id == proposal_id]

    # ---- 提案 ----

    def add_proposal(self, proposal: models.ChangeProposal) -> None:
        with self._lock:
            self.proposals[proposal.id] = proposal

    def get_proposal(self, proposal_id: str) -> Optional[models.ChangeProposal]:
        return self.proposals.get(proposal_id)

    def require_proposal(self, proposal_id: str) -> models.ChangeProposal:
        from .errors import NotFound

        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise NotFound(f"变更提案不存在：{proposal_id}")
        return proposal

    def all_proposals(self) -> list[models.ChangeProposal]:
        return sorted(self.proposals.values(), key=lambda p: p.created_at)
