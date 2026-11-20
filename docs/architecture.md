# 当前系统架构

> 更新：2026-11-20（阶段 3 清理旧图节点后）
> 这份文档描述**实际运行路径**，不保留已废弃的 fast/expert/legacy 分支。

## 1. 分层

```
React + Vite
   │  HTTP / SSE
FastAPI（backend/app/api/main.py）
   │
LangGraph StateGraph（backend/app/graph/build.py）
   │
深度团队会话（backend/app/agents/hub.py）
   │
工具与数据源（backend/app/agents/tools_spec.py / tools/）
   │
SQLite（plans / plan_versions / run_events / runs）+ artifact store
```

## 2. 真实编排图

外层 LangGraph 只有 4 个节点：

```
START
  ↓
clarify（确定性归一化，不调模型）
  ↓
peer_run（深度团队多 Agent 会话）
  ↕ peer_ask（需要用户取舍时中断）
  ↓
peer_finalize（确定性硬校验）
  ├─ 未通过且未到上限 → peer_run 返工
  ├─ 未通过且需要用户决定 → peer_ask
  └─ 通过 → END
```

代码事实：

- 节点注册：`build.py` 末尾只有 `clarify / peer_run / peer_ask / peer_finalize`；
- `confirm`（预算超支确认）在阶段 3 已删除：当前预算超支由确定性 Critic 判 blocker，不再存在“接受超支”入口；
- 旧版 `transport / lodging / poi / weather / planner / critic / negotiation` 节点函数已删除，避免与真实图混淆。

## 3. peer_run 内部：多 Agent 会话

五个角色，均为独立 LLM 回合、独立系统提示、独立工具白名单与历史：

| 角色 | 职责 | 工具 |
|---|---|---|
| orchestrator | 拆任务、派工、按评审意见返工 | 无 |
| logistics | 选航班、酒店 | search_flights / set_flights / search_hotels / set_hotel 等 |
| local_expert | 排每日景点、天气、固定事件 | list_pois / assign_pois / materialize_plan / check_plan 等 |
| critic_agent | 依据 ReviewPacket 独立评审 | 无写工具 |
| writer | 只整合已批准版本 | get_plan |

协作机制：

- 消息总线 `Bus`：`request / proposal / objection / accept / escalate / note`；
- 任务 DAG：主控产出任务，代码校验依赖并调度；
- 代码强制：工具白名单、字段沙箱、版本号、证据 ID、ReviewPacket、Issue Ledger；
- 终审：`peer_finalize` 调用确定性 `critic.run`，模型不能自行宣布通过。

因此：**是多 Agent，但调度是中心化、协议受控的；不是自治 Agent 社会。**

## 4. 运行前追问

```
POST /api/plan/questions
  → 单次模型调用
  → 0–3 个问题（infer / ask）
  → 前端卡片确认
  → 答案并入 preferences（确认：问题=答案）
  → POST /api/plan
```

失败时返回 `warning / retryable / error_code`；前端可重试或直接开始。

## 5. 已知边界

- 追问答案还没有独立字段，暂存在 `preferences`；
- 追问质量与耗时仍是后续优化项；
- 真实库存/预订、登录与多租户、监控告警未实现；
- 运行指标目前落在 `runs` 表，尚未有页面。

## 6. 相关文档

- [MVP 状态](mvp-status.md)
- [优化计划](mvp-optimization-plan.md)
- [设计冻结](ui-system/V1.md)
