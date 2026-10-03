# 生物多样性项目成果审查后端

国际生物多样性基金秘书处使用的项目成果审查系统：管理不同发展中国家的项目周期、
指标定义与证据附件摘要，把 **申报 → 补充材料 → 专家复核 → 批准/驳回/撤回**
组织成可审计状态流，并在跨项目之间检测同一保护地、同一受益社区、同一份生态
调查的重复计量。成果被撤销时，已发布的基金统计不被抹除，而是逐版本登记影响、
在重新汇总时以新版本更正。

零第三方依赖，仅使用 Python 3.11 标准库。

## 运行

```bash
# 测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tests

# 启动 HTTP API（默认 127.0.0.1:8080）
PYTHONPATH=src python3 -m biodiversity_review.api --port 8080
```

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `src/biodiversity_review/contracts.py` | 值对象与枚举：国家周期、指标、边界版本、成果声明、附件摘要、状态 |
| `src/biodiversity_review/service.py` | 核心领域服务 `ReviewService`：状态流、重复检测、撤销影响、批次版本化 |
| `src/biodiversity_review/audit.py` | 仅追加审计日志（带单调序号与 UTC 时间戳） |
| `src/biodiversity_review/storage.py` | 进程内仓储，支持整体 JSON 快照导出/导入，便于归档与重置 |
| `src/biodiversity_review/api.py` | 标准库 `http.server` 实现的 REST API |
| `src/biodiversity_review/errors.py` | 领域错误及对应 HTTP 状态码 |

## 申报状态流

```
submitted ──专家要求补充──▶ awaiting_supplement ──补交──▶ under_review
    │                            │                            │
    │（专家直接复核）             └──撤回/驳回                  ├──专家 recommend──▶ 秘书处批准 approved
    ▼                                                         ├──专家 reject────▶ 秘书处驳回 rejected
under_review                                                └──申报方撤回 withdrawn（批准前）
```

- `approved` / `rejected` / `withdrawn` 为终态；已批准成果只能逐条 **撤销（revoke）**。
- 每次状态变更都向审计日志追加事件，`GET /audit?entity_type=...&entity_id=...` 可查。
- 专家复核可多轮（request_supplement → 补交 → recommend_approval/reject）。

## 重复计量检测（三个维度）

申报入库时与所有在途及已批准申报两两比对成果：

1. **shared_site** — 成果边界快照有保护地交集（同一片保护地被重复计入）；
2. **shared_beneficiary** — 受益社区归一化（去空白、小写）后相同；
3. **shared_survey** — 证据摘要 `evidence_digest` 归一化后相同（同一份生态调查）。

每条冲突必须由秘书处裁决：

- `resolved` — 合理共享（如双边联合资助、已按比例拆分），附裁决理由后不再阻止批准；
- `confirmed_duplicate` — 确认重复，永久阻止相关申报批准。

未裁决的 `open` 冲突或 `confirmed_duplicate` 冲突存在时，批准返回 `409`。
申报被撤回/驳回后自动让出其占用的三个维度，相关开放冲突自动关闭。

## 边界版本化

- 项目注册时写入 revision 0（立项原始边界）。
- 边界修订只能 **追加新版本**：记录新保护地清单、生效日、核准人、核准理由；
  旧版本永久保留，可通过 `GET /projects/{id}/boundaries` 回溯。
- 每条成果在申报瞬间快照当时的边界版本（`boundary_revision_at_submission`），
  显式声明的保护地必须落在当前边界内。

## 撤销影响与基金批次版本

- `compile_batch` 汇总当前所有「已批准且未撤销」成果，每次编译都 **追加一个
  不可变版本**（`supersedes` 指向上一版，并给出 added/removed 差异），从不覆盖。
- 撤销已批准成果时，系统扫描全部已发布批次版本，为每个包含该成果的版本写入
  影响登记（批次、版本号、成果、发布时间、撤销原因），并在响应中返回
  `affected_publications` —— 秘书处可立即定位哪些已发布统计受影响。
- 下一次重新汇总后，影响登记从 `affected` 变为 `corrected` 并记录
  `corrected_in_version`；旧版本数字仍可原样取出。

## HTTP API 摘要

审查人员身份通过 `X-Actor` 请求头传递。

```
POST   /cycles                                         登记国家周期与指标
GET    /cycles/{country_code}
POST   /projects                                       注册项目（写入边界 rev 0）
GET    /projects                                       项目列表
GET    /projects/{id}                                  项目（含全部边界版本）
GET    /projects/{id}/boundaries                       边界修订历史
POST   /projects/{id}/boundary-revisions               追加边界版本（须 rationale）
POST   /projects/{id}/milestones                       记录里程碑（须属本国周期）
GET    /projects/{id}/evidence-pack                    按项目聚合证据/申报/冲突/审计
POST   /projects/{id}/submissions                      申报（入库即检测重复）
GET    /submissions?project_id=...
GET    /submissions/{id}                               申报详情（含冲突、附件、复核轮次）
POST   /submissions/{id}/supplements                   补交材料
POST   /submissions/{id}/reviews                       专家复核
POST   /submissions/{id}/approve | /reject | /withdraw
POST   /submissions/{id}/claims/{claim_id}/revoke      撤销已批准成果
GET    /conflicts?project_id=&status=open
POST   /conflicts/{id}/resolution                      裁决（resolved/confirmed_duplicate）
POST   /batches/{batch_id}/compile                     重新汇总，追加版本
GET    /batches/{batch_id}                             全部版本
GET    /batches/{batch_id}/versions/{n}                指定版本（冻结快照）
GET    /batches/{batch_id}/impacts                     撤销影响登记
GET    /audit?entity_type=&entity_id=                  审计事件
```

## 测试覆盖

`tests/` 下按关键验收场景组织：

- `test_state_flow.py` — 补充材料、多轮复核、批准/驳回/撤回的状态机与审计轨迹；
- `test_duplicate_detection.py` — **重复申报被拦截**、裁决为合理共享后放行、
  **独立成果正常通过**、撤回释放维度、按项目查看冲突；
- `test_boundary_and_cycles.py` — 原边界与核准理由保留、修订留痕、各国指标/
  里程碑互不混用、成果按申报时边界快照；
- `test_batch_versioning.py` — **基金批次重新汇总时版本如何演进**、撤销后
  受影响发布可定位、旧统计不被抹除、更正闭环；
- `test_api.py` — 通过真实 HTTP 端口端到端走通上述全部场景；
- `test_contracts.py` — 原有契约测试（保持兼容）。
