# 旅行规划多 Agent 编排助手 — 需求规格 v1.0

状态：已定稿（2026-11-11）
下一步：P1 环境与图行为验证

---

## 1. 目标

用户填一张表单，系统用 LangGraph 编排多个 agent 产出一份可直接执行的按天行程单，
并在生成过程中实时展示编排过程；用户可对单日提意见触发局部重排。

交付定位：可演示的作品集项目，需能本地一键启动，并支持公网演示。

## 2. 非目标（v1 不做）

- 不做真实预订、支付、下单。
- 不做账号体系、多用户权限。
- 不做对话式多轮需求澄清。
- 不做出境游、签证、汇率、国际航班。
- 不做行程项拖拽式全手动编辑。

## 3. 用户流程

```
填表单 → 提交 → [实时看到 agent 并行工作] → 行程单页
                                                  ├─ 展开某天 → 看该天各项成本与耗时
                                                  ├─ 提交单日意见 → 只重排该天 → 更新该天
                                                  └─ 复制分享链接 → 他人只读打开
```

## 4. 功能需求

| ID | 需求 | 验收标准 |
|---|---|---|
| FR-1 | 表单收集约束 | 必填项缺失时前端与后端都拒绝；预算、日期区间做合法性校验 |
| FR-2 | 并行检索 | 交通/住宿/景点/天气四路 agent 在同一 superstep 并行启动，单路失败不阻断全局 |
| FR-3 | 行程生成 | 输出按天日程，每天 3–6 个活动项，含时段、耗时、费用 |
| FR-4 | 约束校验 | Critic 校验预算、单日时长上限、景点闭馆日、雨天备选、老人儿童友好 |
| FR-5 | 自动重排 | Critic 不通过时回退 Planner，最多 2 轮；第 3 轮强制放行并附警告 |
| FR-6 | 人工介入 | 预算超支 > 20% 时中断，让用户在「接受 / 降配 / 改日期」中选 |
| FR-7 | 过程可视化 | 前端实时显示每个 agent 的运行状态与输出摘要，含回退箭头 |
| FR-8 | 单日重排 | 冻结其余各天，只重跑 planner→critic，返回该天新安排 |
| FR-9 | 结果持久化 | 每次规划落 SQLite，凭 plan_id 可取回 |
| FR-10 | 分享 | 只读链接，无需登录 |
| FR-11 | 降级提示 | 任一路检索失败时，行程单对应位置显示"该项查询失败，建议手动确认"，不得静默省略 |

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
    preferences: list[str] = []  # 如 美食/自然/人文/购物/慢节奏
    pace: Literal["轻松", "适中", "紧凑"] = "适中"
```

### 5.2 mock 数据字段

| 实体 | 字段 |
|---|---|
| Flight | flight_no, dep_city, arr_city, dep_time, arr_time, duration_min, price, seats_left |
| Hotel | name, district, price_per_night, rating, tags, distance_to_center_km |
| POI | name, category, open_time, close_time, closed_days, duration_min, ticket_price, recommended_slot, tags, lat, lng |
| WeatherDay | date, condition, temp_low, temp_high, rain_prob |

### 5.3 输出 Itinerary

```python
class DayPlan(BaseModel):
    date: date
    items: list[Item]            # slot: 上午/中午/下午/晚上
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

## 6. 编排图

```
        clarify
           │
   ┌───────┼───────┬─────────┐
 transport lodging poi   weather        ← 并行 superstep
   └───────┴───────┴─────────┘
           │
        planner
           │
        critic
      ┌────┴────┐
   planner    END        ← revision >= 2 强制放行
```

State 字段：

```python
class TripState(TypedDict):
    brief: TripBrief | None
    transport: list[Flight] | None
    lodging: list[Hotel] | None
    pois: list[POI] | None
    weather: list[WeatherDay] | None
    draft: Itinerary | None
    critique: Critique | None
    revision: int
    errors: Annotated[list[str], operator.add]
```

实现约束：

1. 并行节点只写各自字段，唯一需要 reducer 的字段是 `errors`。
2. 循环上限用 `state["revision"]` 判断，不用图递归深度。
3. `interrupt()` 仅用于 FR-6 的预算超支确认。
4. 单日重排（FR-8）走独立子图：`state` 中除目标日外的 `days` 全部冻结，只重跑 planner→critic。

## 7. 非功能需求

| 项 | 要求 |
|---|---|
| 单次规划耗时 | 目标 < 60s；每节点设 timeout，超时按失败处理 |
| 图循环 | 最多 2 轮 |
| LLM 成本 | 单次规划 token 上限可配置，超限终止并返回已完成部分 |
| LLM 接入 | OpenAI 兼容协议，base_url 与 model 可配，key 走环境变量，不入库不入日志 |
| 错误处理 | 节点异常写入 `errors` 并降级，不抛穿整个图 |
| mock 可替换 | 数据访问走适配器接口，后续可换真实 API |

## 8. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 需求定稿 | 本文档 |
| P1 | 环境 + 图行为验证 | 三节点小图：并行两路、条件回边一次、astream 出事件 |
| P2 | 数据层 + 单 agent | 四份 mock JSON + Pydantic 模型 + 交通 agent 跑通 |
| P3 | 完整编排图 | CLI 输出完整行程 JSON，含 2 轮重排与降级路径 |
| P4 | 服务化 + 前端 | 浏览器可见实时时间线与行程单 |
| P5 | 打磨演示 | 10 条边界用例 + README + 录屏 |

## 9. 未决事项

- LLM provider 与 key 尚未确定；P1–P2 用确定性 stub，P3 前必须定。
- 公网部署方式未定（录屏能否满足演示要求待确认）。
