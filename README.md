# 替代料影响图谱

本项目维护替代料影响图谱的领域约定、角色边界与样例数据，并提供 Python 后端：维护部件替代链、BOM 版本和业务引用图；变更提案先计算直接与间接影响并检测循环；替代范围、临时豁免、分阶段生效和撤回形成版本，发布后只影响符合条件的未来业务；API 可解释每条影响路径，并按角色隐藏供应商商业数据。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/impact_graph/`：后端服务（见下）。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tools/demo.py`：构建样例数据并打印影响报告。
- `tools/serve.py`：启动 HTTP 服务。
- `tests/`：契约与后端行为回归测试。

## 后端模块（src/impact_graph/）

- `models.py`：部件、BOM 版本、业务引用（采购订单/在途批次/生产任务）、变更提案。
- `graph.py`：业务引用图——BOM 依赖索引、多层反查（where-used）、循环检测。
- `substitution.py`：替代链与版本。版本由替代范围（`scope_plants`）、分阶段生效（`phases`）、临时豁免（`exemptions`）和发布/撤回共同决定适用性；发布后只影响符合条件的未来业务（`created_at >= published_at`）。新增规则时检测替代链循环。
- `impact.py`：变更提案的影响计算——直接影响（部件上未关闭的业务引用、直接包含它的 BOM 版本）与间接影响（沿多层反查路径向上的祖先部件业务引用），每条影响带可解释路径。
- `permissions.py`：按角色裁剪供应商商业数据。采购计划员全量可见；仓储管理员隐藏价格与合同条款；质量工程师额外隐藏供应商身份；供应商只能看到自己名下的单据。
- `service.py`：应用服务（内存仓储 + 领域规则 + 提案状态机：草拟 → 待确认 → 已下达 → 履行中 → 已关闭）。
- `api.py`：基于标准库的 HTTP API，零外部依赖。
- `seed.py`：演示数据。

## HTTP API

角色通过请求头 `X-Actor-Role`（中文需百分号编码）或查询参数 `role` 传入；供应商需同时传 `X-Supplier-Id` / `supplier_id`。写操作仅采购计划员与质量工程师可用。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/parts` | 新建部件 |
| GET | `/parts/{id}` | 部件详情 |
| GET | `/parts/{id}/where-used?at=` | 多层反查路径 |
| POST | `/boms` | 新增 BOM 版本（循环返回 409） |
| POST | `/refs` | 登记业务引用 |
| GET | `/refs/{id}` | 业务引用（按角色裁剪） |
| GET | `/refs/{id}/substitutions?today=` | 该引用当前适用的替代 |
| POST | `/substitutions` | 新建替代规则（链式循环返回 409） |
| POST | `/substitutions/{id}/versions` | 新增版本（范围/阶段/豁免） |
| POST | `/substitutions/{id}/versions/{n}/publish` | 发布版本 |
| POST | `/substitutions/{id}/versions/{n}/withdraw` | 撤回版本 |
| POST | `/proposals` | 新建变更提案 |
| POST | `/proposals/{id}/evaluate` | 计算直接/间接影响并检测循环 |
| POST | `/proposals/{id}/transition` | 状态机推进（submit/return/release/start/close） |
| GET | `/proposals/{id}` | 提案详情 |
| GET | `/proposals/{id}/impact` | 影响报告（逐条路径解释，按角色裁剪） |

启动服务：`python3 tools/serve.py --demo --port=8080`

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

演示影响报告：`python3 tools/demo.py`
