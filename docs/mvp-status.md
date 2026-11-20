# MVP 完成状态

- 日期：2026-11-20
- 对应提交：`eb53070` 之后的 MVP 收口提交
- 标签：`mvp-2026-11-20`（annotated，注释“MVP 开发完成”）

## 1. 结论

MVP 功能闭环已完成：需求输入、模型运行前追问、多 Agent 规划、运行过程、方案查看、单日返工、版本与交付。

这不是生产完成版。真实库存/预订、登录与多租户、监控告警、完整自动化回归仍属于后续阶段。

## 2. 已完成范围

| 模块 | 状态 | 说明 |
|---|---|---|
| 入口 | 完成 | 装订式入口，CSS 场景 + 粒子增强 |
| 需求 | 完成 | 两栏星图卡，地点/日期/人数/预算/偏好/节奏 |
| 运行前追问 | 完成 | `POST /api/plan/questions`，模型返回 0–3 个问题，可自定义、可回改 |
| 多 Agent 编排 | 完成 | Orchestrator / Logistics / Local Expert / Critic / Writer |
| 运行与中断 | 完成 | SSE、LangGraph checkpoint、中断恢复 |
| 方案与交付 | 完成 | 行程表、预算、变更、分享、版本 |
| 单日返工 | 完成 | 按天反馈并重排 |

## 3. 已验证

- `frontend npm run build`（tsc + vite）通过；
- 追问接口真实调用一次约 14.9 秒，返回结构化问题；
- 线上流程：需求 → 追问卡 → 逐题确认 → 开始规划；
- 追问 chip 可回改；
- 答案进入 `/api/plan` 请求体；
- 桌面 1440 与手机 390 需求页截图核对；
- 控制台 0 错误；
- 阶段 1：追问失败时显示提示，可重试或直接开始（Playwright 模拟超时验证）；
- 阶段 1：失败分类单元验证（超时/429/5xx/网络/配置/格式）；
- 阶段 2：`GET /api/plan/{id}` 返回 `version` 与 `llm_stats`；刷新后交付页仍有调用数；
- 阶段 2：方案页只显示一个用户可见版本号（`计划 vN`）；
- 阶段 3：真实图只剩 `clarify / peer_run / peer_ask / peer_finalize`，旧节点与 `confirm` 已删除；
- 阶段 4：追问上限 2 条，重复/泛问过滤生效；前 3 个评测样本无重复、无泛问，见 `docs/eval-followups.json`；
- 阶段 5：追问页刷新后恢复问题、答案和当前卡，不重复调用模型；开始规划后清空草稿；
- 阶段 6：`GET /api/metrics/runs` 返回调用数/tokens/耗时/结果；设置面板可见最近 10 次运行。

## 4. 已知限制

1. 追问生成是额外一次模型调用，10–20 秒，未缓存；
2. 追问答案暂以 `确认：问题=答案` 并入 `preferences`，没有独立字段；3. ~~追问失败会静默直接开跑~~（阶段 1 已修复：显示失败原因，可重试或直接开始）；
4. ~~`/api/plan/{id}` 不返回 `llm_stats`~~（阶段 2 已修复：刷新/交付页保留调用数）；
5. ~~计划版本与团队批准版本编号口径不一致~~（阶段 2 已修复：界面只显示方案版本）；
6. ~~`build.py` 仍保留未接线的旧图节点和过期注释~~（阶段 3 已清理）；
7. ~~追问页刷新会丢进度~~（阶段 5 已修复：sessionStorage 草稿恢复，开始规划后清空）；
8. ~~缺少按运行的指标看板~~（阶段 6 已修复：设置面板运行指标，接口 `/api/metrics/runs`）；
9. 改造后尚未完整跑完一次 100 秒交付回归。

## 5. 相关文档

- 优化计划：[mvp-optimization-plan.md](mvp-optimization-plan.md)
- 当前架构：[architecture.md](architecture.md)
- 运行指标：[observability.md](observability.md)
- 追问评测：[eval-followups.json](eval-followups.json)
- 设计冻结：[ui-system/V1.md](ui-system/V1.md)
- 架构评审：[deep-team-architecture-review.md](deep-team-architecture-review.md)
