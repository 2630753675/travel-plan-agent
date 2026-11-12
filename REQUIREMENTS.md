# 旅行规划多 Agent 编排助手 — 需求规格 v1.1

状态：v1 已实现并通过验收（2026-11-12）
验收基线：24 个脚本全部通过（功能、接口、浏览器、契约四层）

> v1.0 与本文件的差异见 §10「变更记录」。v1.0 里「key 走环境变量」「最多 2 轮」
> 「下一步 P1」等约定在实现阶段被推翻，以本文件为准。

---

## 1. 目标

用户填一张表单，系统用 LangGraph 编排多个 agent 产出一份可直接执行的按天行程单，
并在生成过程中实时展示编排过程；用户可对单日提意见触发局部重排。

交付定位：可演示的作品集项目，本地一键启动，可经隧道公网演示。

## 2. 非目标（v1 不做）

- 不做真实预订、支付、下单。行情数据为静态快照（见 §7）。
- 不做账号体系与按人授权；只做演示级访问门禁（FR-16）。
- 不做对话式多轮需求澄清。
- 不做出境游、签证、汇率、国际航班。
- 不做行程项拖拽式全手动编辑。
- 不做演示视频（明确砍掉）。

## 3. 用户流程

```
填表单 → 提交 → [实时看到 agent 并行工作与工具调用] → 行程单页
                                                       ├─ 看花费构成与按类分组的提示
                                                       ├─ 展开某天 → 看该天各项成本与耗时
                                                       ├─ 提交单日意见 → 只重排该天 → 新版本
                                                       ├─ 回滚到历史版本
                                                       └─ 复制签名分享链接 → 他人只读打开
```

## 4. 功能需求

| ID | 需求 | 验收标准 |
|---|---|---|
| FR-1 | 表单收集约束 | 必填项缺失时前后端都拒绝；预算、日期区间、同行人组合做合法性校验 |
| FR-2 | 并行检索 | 交通/住宿/景点/天气四路在同一 superstep 并行；单路失败不阻断全局，写入 `errors` |
| FR-3 | 行程生成 | 输出按天日程，含时段、耗时、费用；单日时长上限 600 分钟 |
| FR-4 | 约束校验 | Critic 校验预算、单日时长、闭馆日、**活动重叠**、**开放时间**、**返程赶机截止**、缺餐、雨天、老人儿童友好 |
| FR-5 | 自动重排 | Critic 不通过时回退 Planner；`max_revisions` 默认 3（＝最多 2 次重排），达上限强制放行并附警告 |
| FR-6 | 人工介入 | 超支 > 20% 时中断，选项为「接受超支 / 降配重排 / 取消」；协商僵持时中断，给出三个倾向供选择 |
| FR-7 | 过程可视化 | 右侧抽屉流式显示 agent 讨论、工具调用（含参数与返回）、校验结论与路由决策 |
| FR-8 | 单日重排 | 冻结其余各天，只重跑 planner→critic；未编辑日期的 JSON 必须逐字节一致 |
| FR-9 | 结果持久化 | 落 SQLite；凭 plan_id 取回行程、事件与版本 |
| FR-10 | 分享 | 只读链接，用 HMAC 签名，不共用访问口令 |
| FR-11 | 降级提示 | 任一路检索失败时，行程单对应位置显示原因，不得静默省略 |
| FR-12 | 多轮协商 | 预算官提议 → 体验官基于工具证据表态 → 调解者裁决；僵持时问用户，第二轮必须出方案 |
| FR-13 | 检索结果协作 | 住宿与景点结果对不上时（平均距离超阈值），住宿 agent 向景点 agent 要坐标后重选 |
| FR-14 | 统一事件契约 | 每条事件带 run_id / event_id / 递增 seq / parent_id；实时流与回放同一套结构 |
| FR-15 | 版本与回滚 | 生成、重排、降配、回滚各留一个版本；回滚本身也记版本，历史不覆盖 |
| FR-16 | 访问门禁 | 本机回环免口令；外部访问需口令；分享只读；`/api/config/test` 对非本机拒绝内网地址 |
| FR-17 | 结果可解释 | 成本按类别拆解且合计必须等于总花费；提示按交通/住宿/景点/天气/预算/模型/行程分组 |
| FR-18 | 中断可恢复 | 服务重启后，未确认的中断仍可 resume |

## 5. 数据契约

### 5.1 输入 TripBrief

```python
class TripBrief(BaseModel):
    origin: str                  # 出发城市
    destination: str             # 目的地城市
    start_date: date
    end_date: date
    travelers: int
    budget_total: float          # 总预算（元）
    companions: list[Literal["老人", "儿童", "无"]] = ["无"]
    preferences: list[str] = []  # 美食/人文/自然/购物/慢节奏/夜景
    pace: Literal["轻松", "适中", "紧凑"] = "适中"
```

校验：`end_date < start_date`、出发地等于目的地、天数 > 14、`"无"` 与其他同行人混用，均拒绝。

### 5.2 数据规模（静态快照）

| 文件 | 条数 | 覆盖 |
|---|---|---|
| flights.json | 47 | 上海↔成都/西安/厦门/北京、北京↔成都、广州↔成都 |
| hotels.json | 26 | 成都 6、西安 7、厦门 7、北京 6 |
| pois.json | 59 | 成都 21、西安 15、厦门 12、北京 11 |
| weather.json | 26 | 成都 5、西安 7、厦门 7、北京 7 |

行内含 `city` 字段的按城市过滤，历史行没有该字段默认归成都。

### 5.3 输出 Itinerary

```python
class Item(BaseModel):
    slot: Literal["上午", "中午", "下午", "晚上"]
    start_time: time
    end_time: time
    type: Literal["transport", "sight", "meal", "hotel", "rest"]
    title: str
    ref_id: str | None
    cost: float
    duration_min: int
    note: str

class DayPlan(BaseModel):
    date: date
    items: list[Item]
    day_cost: float
    notes: list[str]

class Itinerary(BaseModel):
    days: list[DayPlan]
    total_cost: float
    budget_total: float
    warnings: list[str]

class Critique(BaseModel):
    passed: bool
    issues: list[Issue]          # severity: blocker | warning
    suggestions: list[str]
```

### 5.4 事件

```python
class RunEvent(BaseModel):
    schema_version: int = 1
    run_id: str
    plan_version: int = 1
    event_id: str
    seq: int                     # run 内严格递增
    occurred_at: datetime
    event_type: Literal[...]     # run_/stage_/agent_/tool_/validation/route/user_/artifact
    stage: str
    actor: str
    parent_id: str | None        # tool_result → tool_call；agent → stage
    payload: dict
```

## 6. 编排图

```
        clarify
           │
   ┌───────┼───────┬─────────┐
 transport lodging poi   weather        ← 并行 superstep
   └───────┴───┬───┴─────────┘
               │
          coordinator ──(结果对不上)──→ lodging_fix ──┐
               │                                    │
               └──────────────→ planner ←───────────┘
                                  │
                               critic ──(超支 >20% 且未协商)──→ negotiation
                                  │                                │
                                  ├─(通过)──────────────→ END       ├─(采纳)──→ planner
                                  ├─(未通过)────────────→ planner    ├─(僵持)──→ ask → negotiation
                                  └─(超支且达条件)──────→ confirm ──┘
                                                          ├─ accept/abort → END
                                                          └─ downgrade    → planner
```

State 字段（`TripState`，全部显式声明——**未声明的键会被 LangGraph 静默丢弃**）：

```python
brief, notes,
transport, lodging, poi, weather,
draft, critique, revision, decision,
negotiation, negotiated, neg_round, user_hint,
forced_drops, force_cheaper_hotel,
collab_done, collab_requests, focus_poi_ids,
log, errors
```

实现约束：

1. 并行节点只写各自字段；需要 reducer 的是 `log` 与 `errors`。
2. 循环上限用 `state["revision"]`（critic 执行次数），不用图递归深度。
3. `interrupt()` 用于 FR-6 的两类中断；`confirm`/`ask` 节点只调 `interrupt()`，不做副作用。
4. 单日重排走独立接口，冻结其余日期后重跑 planner→critic。

## 7. 非功能需求

| 项 | 要求 |
|---|---|
| 单次规划耗时 | 目标 < 60s；每节点设 timeout，超时按失败处理 |
| 图循环 | 上限来自界面配置 `max_revisions`（默认 3，即最多 2 次重排） |
| LLM 成本 | token 上限可配置，超限终止 |
| LLM 接入 | OpenAI 兼容协议；**base_url / model / key 全部在界面配置并存 SQLite**，不读环境变量；key 只以掩码回传、不进日志 |
| 错误处理 | 节点异常写入 `errors` 并降级，不抛穿整图 |
| 数据快照 | 多城市静态 JSON，经 `TravelSource` 协议访问；换真实 API 只改实现层 |
| 访问控制 | 演示级：口令 + 分享签名 + SSRF 防护，见 FR-16 |

## 8. 里程碑与实际状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 需求定稿 | 完成（v1.0） |
| P1 | 环境 + 图行为验证 | 完成 |
| P2 | 数据层 + 单 agent | 完成 |
| P3 | 完整编排图 | 完成 |
| P4 | 服务化 + 前端 | 完成 |
| P5 | 打磨演示 | 完成（录屏已砍） |
| 后续 | 时间轴硬约束、事件契约、协商闭环、版本恢复、结果可解释、访问门禁 | 完成 |
| 后续 | 真实数据源接入、质量评测集 | 进行中 |

## 9. 未决事项

- 真实数据接口未接入；当前为静态快照，价格与余票不随日期变化。
- 公网部署方式未定（隧道 + 口令可满足演示，未做正式部署）。
- 密钥在库中明文，仅有门禁没有加密；仅适用于演示环境。

## 10. 变更记录

v1.0 → v1.1：

| 项 | v1.0 | v1.1 实际 |
|---|---|---|
| LLM 密钥 | 走环境变量，不入库 | 界面配置，存 SQLite，仅掩码回显 |
| 重排轮次 | 最多 2 轮，第 3 轮放行 | 可配置，默认 `max_revisions=3`（最多 2 次重排） |
| 人工介入选项 | 接受 / 降配 / 改日期 | 接受 / 降配 / **取消**（另加协商僵持三倾向） |
| 过程可视化 | 节点状态与回退箭头 | 统一事件流：agent 讨论 + 工具调用 + 校验 + 路由，可回放 |
| 协商 | 无 | 预算官 / 体验官 / 调解者三角色，最多 2 轮 |
| 检索协作 | 无 | coordinator 触发，住宿向景点要坐标后重选 |
| 版本 | 无 | 只追加版本 + 回滚 |
| 访问控制 | 无 | 口令 + 分享签名 + SSRF 防护 |
| 降级提示 | 提示"建议手动确认" | 按交通/住宿/景点/天气/预算/模型/行程分组 |
