# 替代料影响图谱

工程部门停用零件时，需要判断哪些 **BOM、采购订单、在途批次、生产任务** 仍依赖它。
本项目在领域契约之上提供零第三方依赖的 Python 后端：维护部件替代链、BOM 版本与
业务引用图，变更提案先计算直接与间接影响并检测循环；替代范围、临时豁免、分阶段
生效和撤回均形成不可变版本；发布后只影响符合条件的未来业务；API 可解释每条影响
路径，并按权限隐藏供应商商业数据。

## 领域模型

- **部件 / 供应商**：部件含供货关系与单价（商业敏感字段）。
- **BOM 版本**：同一父部件的 BOM 多版本管理（草拟/生效/作废），新版本生效时旧版
  自动作废；依赖图只遍历当前生效版本。
- **业务引用**：采购订单 `PURCHASE_ORDER`、在途批次 `IN_TRANSIT_BATCH`、
  生产任务 `PRODUCTION_ORDER`，状态遵循契约：草拟 → 待确认 → 已下达 → 履行中 → 已关闭。
- **替代链**：有向边 old → new，支持全量（FULL）与限定单据类型（PARTIAL）替代；
  写入即做成环拦截。
- **变更提案**：动作 DISCONTINUE / SUBSTITUTE；生命周期 draft → confirmed →
  published，可 withdrawn；范围调整、豁免、阶段、发布、撤回都追加不可变版本快照。

## 影响与生效判定

`graph.DependencyGraph` 沿 BOM “包含”边反向向上 BFS：

- **直接影响**：业务单据直接引用停用部件；
- **间接影响**：单据引用的父部件经多层 BOM 包含停用部件；
- 每条影响带可解释路径（单据 → 各层 BOM → 停用部件）；
- BOM 环与替代链环分别检测，存在环的提案禁止确认/发布。

`policy` 在查询时即时判定单据是否真正受发布提案影响，**从不回写历史单据**：

1. 单据状态须在范围允许状态内（默认草拟/待确认；终态与已锁定状态不受影响）；
2. 单据类型须在替代范围内；
3. 分阶段：业务锚定日期（交货/到货/开工日）不早于该类型阶段生效日；
4. 业务日期不早于发布日（只影响未来业务）；
5. 查询当日不在覆盖该单据/部件的有效临时豁免窗口内；
6. PARTIAL 替代链按单据类型解析最终替代料。

## 商业数据裁剪（`security.Viewer`）

| 角色 | 供应商身份 | 单价/成本 |
| --- | --- | --- |
| 采购计划员 planner | 可见 | 可见 |
| 质量工程师 quality | 可见 | 隐藏 |
| 仓储管理员 warehouse | 隐藏 | 隐藏 |
| 供应商 supplier（带 X-Supplier-Id） | 仅本供应商 | 仅本供应商单据 |

影响路径解释不含商业字段，裁剪后仍可完整追溯。

## HTTP API

启动：`PYTHONPATH=src python3 -m impact_graph --demo [--port 8080]`
鉴权头：`X-Role: planner|quality|warehouse|supplier`（供应商另需 `X-Supplier-Id`）。

主要端点：

- `GET  /parts/{id}/impact` 即席多层影响分析（含逐条路径解释）
- `POST /parts` / `POST /suppliers` / `POST /boms` / `POST /refs`
- `POST /boms/{bom_id}/versions/{v}/activate` BOM 版本生效
- `POST /substitutions` 登记替代链（成环返回 409 cycle_detected）
- `POST /proposals` 创建变更提案（body 可带 scope / phases）
- `GET  /proposals/{id}/impact` 提案影响（带适用判定、阶段、豁免与原因）
- `POST /proposals/{id}/scope` `/phases` `/exemptions` 范围/分阶段/临时豁免（形成版本）
- `POST /proposals/{id}/confirm` `/publish` `/withdraw`
- `GET  /proposals/{id}/versions` `/versions/{n}` 不可变版本历史

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/impact_graph/`：models / repository / graph / policy / catalog /
  proposal / security / api / demo。
- `tools/check_contract.py`：契约摘要检查。
- `tools/impact_demo.py`：停用零件的端到端命令行演示。
- `tests/`：契约、依赖图、提案策略、权限裁剪、HTTP API 回归测试。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 35 个用例
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
python3 tools/impact_demo.py
```
