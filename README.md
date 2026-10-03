# 生物多样性项目审查

国际生物多样性基金的成果审查后端。基金同时资助多个发展中国家的项目，秘书处用它判断里程碑成果是否成立，并防止不同申报方把同一片保护地、同一组受益社区或同一份生态调查重复算作成果。

## 架构

```
src/biodiversity_review/
  contracts.py     归档用契约（项目边界、成果申报的只读记录格式）
  models.py        领域模型：项目/周期/指标、边界版本、申报、证据附件摘要、冲突、汇总快照
  statemachine.py  申报状态机：草稿→申报→补充材料→专家复核→批准/驳回/撤回→撤销
  conflicts.py     重复申报检测（跨申报方的保护地/社区/调查重叠）
  aggregation.py   基金批次汇总：不可变快照、撤销影响定位、版本演进
  services.py      ReviewService 应用服务：全部业务流程与审查查询
  api.py           只读 HTTP API（标准库实现）
```

关键设计：

- **可审计状态流**：每次状态迁移追加 `AuditEvent`（操作者、时间、前后状态、理由），非法迁移直接拒绝。
- **重复拦截**：申报提交时检测其他申报方活跃申报的重叠；命中则置为 `BLOCKED` 并落冲突记录。对方撤回/驳回/撤销，或本方调整申报范围后，冲突自动解除，可重新申报。同一申报方复用自己的成果不算重复。
- **边界版本**：项目边界以不可变版本保存，变更必须记录核准理由；原边界永不修改，新申报按当前边界校验。
- **撤销不抹除**：成果撤销后，包含它的已发布快照被标记为 `STALE` 并记录 `affected_by`，数据原样保留；重新发布产生新版本（`supersedes` 指向前版），旧版转为 `SUPERSEDED` 仍可查。`affected_snapshots(claim_id)` 可定位受影响的已发布统计。

## 审查人员 API

```python
from biodiversity_review import ReviewService
from biodiversity_review.api import create_server

service = ReviewService()
# ... 注册项目、申报、复核 ...
server = create_server(service, port=8080)
server.serve_forever()
```

- `GET /projects/{id}/evidence` — 按项目查看申报与证据附件摘要
- `GET /projects/{id}/conflicts` — 按项目查看跨申报方冲突关系
- `GET /claims/{id}/audit` — 申报的完整审计轨迹
- `GET /batches/{id}/snapshots` — 基金批次汇总的版本演进

## 测试

运行测试：`python3 -m unittest discover -s tests -v`

编译检查：`python3 -m compileall -q src tests`

测试覆盖：重复申报被拦截（保护地/社区/调查三个维度）、独立成果正常通过、同一申报方不误伤、冲突解除后重新申报、边界变更留痕、撤销后汇总版本演进且不抹除已发布数据。
