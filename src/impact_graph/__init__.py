"""替代料影响图谱后端。

模块划分：

- ``models``：部件、BOM 版本、业务单据、替代链、变更提案等领域模型。
- ``repository``：线程安全的内存仓储。
- ``graph``：跨业务依赖图的多层遍历与循环检测。
- ``policy``：分阶段生效、临时豁免、替代范围等纯业务判定。
- ``catalog`` / ``proposal``：主数据与变更提案应用服务。
- ``security``：按角色裁剪供应商商业数据。
- ``api``：基于标准库的 JSON HTTP 接口。
"""
from __future__ import annotations

from .catalog import CatalogService
from .proposal import ProposalService
from .repository import Repository
from .security import Viewer

__all__ = ["CatalogService", "ProposalService", "Repository", "Viewer", "build_services"]


def build_services(now=None) -> tuple[Repository, CatalogService, ProposalService]:
    """组装仓储与应用服务，``now`` 为可注入时钟（测试用）。"""
    repository = Repository(clock=now)
    catalog = CatalogService(repository, now=now)
    proposals = ProposalService(repository, now=now)
    return repository, catalog, proposals
