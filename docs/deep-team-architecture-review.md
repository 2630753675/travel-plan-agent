# 深度团队模式（peer）架构评审

> 评审对象：`E:\项目\agent_study\dsh_work2` 当前 `peer` 深度团队实现（Orchestrator / Logistics / Local Expert / Critic / Writer）。
> 评审目标：高效、准确、可收敛；重点看返工、token、可读性和架构冗余。
> 结论口径：**这不是“多 Agent 团队”，而是一条 5 角色串行流水线 + 一个没有验收标准的自由文本评审器。** 当前的成本和返工不是模型能力问题，是协议和上下文设计问题。
>
> **2026-11-17 产品决策：废除 `fast` / `expert`，只保留“深度团队”单模式。** 本文第 3 节对 fast/expert 的分析保留为“待删除的过渡实现为什么坏”；第 13/14 节的目标架构按单模式收敛。单模式不等于继续走 24 次请求的老路——恰恰相反，废除便宜路径后，深度团队必须自己做到足够便宜。

---

## 0. 先给结论

按严重程度排序，当前设计有 8 个根因性问题：

1. **伪多 Agent**：5 个角色固定串行，任务图被代码强制成“交通住宿 → 本地体验”一条链；不存在独立并行的子问题，也不存在真正的团队协作。`orchestrator` 没有工具，只做一次固定形状的拆解和返工转发，却要消耗完整模型回合。
2. **评审没有“问题身份”**：`changes` 只有 `{owner, problem, required_change}`，没有 issue code、severity、evidence、acceptance test、首次出现版本、关闭状态。同一个问题可以在每一轮以不同措辞重新提出，系统无法证明“已修复”，也无法阻止“新开一个问题”。
3. **评审没有能力边界**：Critic 可以要求“夜景、美食、房型、发票、确认号”，但 Local Expert 根本没有餐厅/夜间活动工具，数据源也没有房型/发票。提示词里写“不得要求”只是建议，代码没有强制，于是产生**不可能完成的返工**。
4. **上下文重复注入**：每个回合的 prompt 同时带 `brief + 最近 12 条 bus 消息 + 角色说明 + 契约 + 完整任务表 + 本回合收件 + 待返工问题 + 交通住宿事实`；Critic 又被强制调用 `get_plan / check_plan / day_costs / experience_check` 四个工具，每个都返回大段 JSON。同一份状态在一次评审里被讲了至少 4 遍。
5. **返工放大**：一个 Critic 反对会把责任任务及其所有依赖任务重置，专业 Agent 必须重新提交完整 `delivery`（`_delivery_valid` 要求与 `ctx.assignment` 完全相等），不能只提交 diff；一次局部问题会触发整条链重跑。
6. **版本语义错误**：`plan_version` 是“每次写工具调用 +1”的全局计数器，不是内容哈希；`assign_pois`、`set_hotel`、`set_flights` 即使提交完全相同的值也会递增版本，导致已批准版本失效、缓存失效、Critic 必须重新评审。
7. **预算与模式名不副实**：脚本化 happy path 已经是 **24 次供应商请求 / 178,341 输入字符**；强制一次软性反对后变成 **60 次请求 / 748,725 输入字符且未收敛**。默认 `max_model_calls=40` 连一次返工都撑不住；`fast` 在配置模型后仍走同一 5 角色引擎，并不快。
8. **UI 不是皮肤问题**：Agent 的长篇 `say` 被原样塞进气泡，没有结构、没有折叠、没有 diff、没有 issue 跟踪；4 次工具调用在“查看依据”里显示成 1 次（事件 parent_id 配对错误）。用户看到的“又长又丑又费 token”是后端协议和前端投影共同造成的。

如果只修一件事：**把“自由文本评审 + 全量上下文”改成“结构化 issue 账本 + 增量评审包 + 确定性修复”**。这一项能同时解决返工、token 和可读性的大部分问题。

### 问题总表（按优先级）

| ID | 级别 | 问题 | 直接后果 | 主要修法 |
|---|---|---|---|---|
| A1 | P0 | 伪多 Agent：5 角色固定串行，任务图只有一条链 | 5 倍协议开销，无并行收益 | 并行专业 Agent + 代码合并；去掉 Orchestrator LLM |
| A2 | P0 | 评审无 issue 身份/验收标准/能力映射 | 同一问题反复提，v9 不收敛 | Issue Ledger + acceptance + repair_tool |
| A3 | P0 | 每回合全量注入 brief/bus/任务/收件/待返工 | happy path 累计 17.8 万输入字符 | 上下文编译器 + 切片/diff |
| A4 | P0 | 返工整任务重置，不能提交 diff | 一条软问题触发 60 次请求 | `apply_patch` + 最小失效范围 |
| A5 | P0 | `plan_version` 是写操作计数器，无变化也 +1 | 批准失效、缓存失效、v9 | content_hash + 无变化不递增 |
| A6 | P0 | 预算/模式名不副实：fast 仍走 5 角色；40 次不够 | 慢、超时、升级死循环 | 废除 fast/expert；单模式内做自适应评审深度 |
| A7 | P0 | 证据是随机 UUID，不绑定结论 | “有证据”是假象 | 内容哈希 + 字段路径 + 映射校验 |
| A8 | P0 | UI 原样渲染长文；4 个工具显示 1 次 | 不可读、不可信 | 结构化卡片 + message_id 配对 |
| B1 | P1 | Critic 强制四工具，数据高度重叠 | 每次评审多 4 次请求 | ReviewPacket 预计算 |
| B2 | P1 | `get_plan` 返回全量 itinerary | 4–15k 字符/次 | summary/day/diff 视图 |
| B3 | P1 | 历史按 12 条消息裁剪，不按 token | 上下文抖动、cache 失效 | token 预算 + 摘要 |
| B4 | P1 | 静态契约放在动态 user prompt | KV cache 命中差 | system 静态化 |
| B5 | P1 | 自由文本 JSON + 严格解析 | 协议重试烧请求 | `submit_decision` 工具/JSON Schema |
| B6 | P1 | `say` 无长度/结构约束 | 长文、token、UI 噪音 | summary/details 分离 |
| B7 | P1 | 工具串行执行却提示“并行” | 延迟累加 | 只读工具真并行 |
| B8 | P1 | `set_flights`/`materialize` 重复查数据源 | 2 倍延迟/调用 | 复用已查结果 |
| B9 | P1 | checkpoint 全量 pickle + 事件重复 payload | 70MB、回放慢 | artifact store + 引用 |
| B10 | P1 | `input_chars` 未累加，预算只算 4000 字符 | 成本不可见/不可控 | 真实计量 + per-agent |
| B11 | P1 | 测试把 24 次调用当 PASS | 成本基线被固化 | 成本/重复 issue 回归 |
| C1 | P2 | 角色无颜色、无实时任务板、无 diff | 可读性差 | UI 重构 |
| C2 | P2 | Writer 自由生成 + 子串校验 | 文档与行程不一致 | 模板槽位 + 事实注入 |
| C3 | P2 | 单进程、无保留策略、API key 明文 | 规模化/安全 | 后续产品化 |

---


## 1. 评估方法与证据分级

本报告的证据分三类，文中会标注：

| 级别 | 含义 | 例子 |
|---|---|---|
| **实测** | 本次在仓库内运行脚本/查询数据库得到 | 24 次请求、178,341 字符；`run_events` 35,144 行 |
| **代码事实** | 直接读代码确认的控制流/数据流 | `hub.py:306` 每回合追加完整任务表 |
| **截图观察** | 从你给的截图推断（未拿到该次运行的完整日志） | `check_plan passed=true` 仍被反对；v9；1 次工具调用 |

实测脚本（不调用真实模型，使用仓库自带 `MockSource` 和 `ScriptedTeam`）：

- `.harness/measure_prompt.py`：脚本化五角色成功路径，统计每次请求的消息总字符数。
- `.harness/measure_rework.py`：在 Critic 第一次准备 accept 时强制注入一条“夜景未落实”的软性反对，观察返工放大。
- `backend/scripts/verify_peer_success.py`：仓库自带回归，确认 24 次调用、6 个 Agent 回合。

> 说明：脚本化模型每一步只发一个工具调用，真实模型可能一次并行发多个工具调用，真实请求数会低于 24；但“每个工具步骤都是一次完整模型请求”这一结构不变。60 次/74.9 万字符是**架构放大效应**的证据，不是对真实模型平均成本的精确预测。

---

## 2. 量化结果：钱到底花在哪

### 2.1 Happy path 已经很高

`measure_prompt.py` 结果：

```
converged True turns 6 reviews 1
 1 orchestrator  input_chars=  2,168
 2 logistics     input_chars=  2,840
 3 logistics     input_chars=  3,889
 4 logistics     input_chars=  5,031
 5 logistics     input_chars=  5,442
 6 logistics     input_chars=  5,652
 7 local_expert  input_chars=  3,585
 8 local_expert  input_chars=  7,899
 9 local_expert  input_chars=  8,463
10 local_expert  input_chars=  8,734
11 local_expert  input_chars=  9,055
12 local_expert  input_chars=  9,358
13 local_expert  input_chars=  9,659
14 local_expert  input_chars=  9,930
15 local_expert  input_chars= 10,730
16 local_expert  input_chars= 10,964
17 orchestrator  input_chars=  7,811
18 critic_agent  input_chars=  4,683
19 critic_agent  input_chars=  9,246
20 critic_agent  input_chars=  9,496
21 critic_agent  input_chars= 10,919
22 critic_agent  input_chars= 11,132
23 writer        input_chars=  3,538
24 writer        input_chars=  8,117
TOTAL requests 24 input_chars 178,341
avg chars/request 7,431
```

几个直接结论：

- **一次 5 天行程，光输入就 17.8 万字符**，还没算输出；按中文约 0.5–1 token/字符，约 9–18 万输入 token。
- `local_expert` 一个角色 10 次请求，因为 5 天每天一次 `assign_pois`；`submit_day_plans` 本来可以一次提交，但脚本化模型没用，提示词也没有强制。
- `critic_agent` 5 次请求：4 个强制工具各一次 + 最终结论一次。
- 请求数随上下文线性增长：同一个 Agent 的每次请求都在重发前面的工具结果和 prompt。

### 2.2 一次软性反对就失控

`measure_rework.py` 结果：

```
converged False turns 20 reviews 2
requests 60 input_chars 748,725
by_role {'orchestrator': 5, 'logistics': 7, 'local_expert': 32, 'critic_agent': 16}
```

只注入一条“11-21 晚上没有安排夜景活动”的反对：

- 请求从 24 → 60；
- 输入字符从 17.8 万 → 74.9 万；
- 20 个 Agent 回合耗尽，仍未收敛；
- `local_expert` 被调用 32 次，因为它必须重新提交完整 5 天安排，而不是只改 11-21 晚上。

这就是你截图里 `v9`、大量返工、token 高的结构性原因：**一条软性问题可以打穿整个预算**。

### 2.3 截图逐项对账

你给的截图不是“某一次模型抽风”，而是当前协议的固定输出。逐项对应：

| 截图内容 | 代码原因 |
|---|---|
| 标题“方案评审 17:36:56 · 团队统筹” | `critic_agent` 的 `to=["orchestrator"]`；前端 `ACTOR_LABEL` 把 `critic_agent` 映射为“方案评审”，`orchestrator` 映射为“团队统筹”（`App.tsx:633-637`）。 |
| “我已按要求在同一回合内一次性调用 get_plan、check_plan、day_costs、experience_check 四个工具” | 这是 `roles.py:87` 的强制要求，不调用就会被 `hub.py:466-468` 退回重做。模型只是在复述提示词。 |
| “总费用 ¥6,450、预算 ¥8,000，其中机票 4,340、住宿 1,040……” | `get_plan` / `day_costs` 返回全量数据，`say` 没有“只讲 diff/只讲问题”的约束，模型自然把数字复述一遍。 |
| “问题一：夜景安排没有到位……问题二：美食安排偏弱……问题三……问题四……” | `changes` 没有 issue code/severity/acceptance；Critic 只能写自然语言段落。四个问题里至少“夜景、美食、房型/发票”没有对应修复工具（见 4.6）。 |
| `(v9)` | `plan_version` 是写操作计数器，不是评审轮次；`assign_pois`、`set_hotel` 等每次调用都 +1，甚至提交相同值也 +1（见 6.3）。 |
| “查看依据 · 1 次工具调用” | 4 个工具调用的 `tool_call` 事件 `parent_id=None`，前端退化为 `seq` 窗口猜测，只命中最后 1 个（见 10.2）。 |
| 长文没有分段/折叠/结构 | `say` 被原样塞进 `.message-bubble`，没有 Markdown、没有 `white-space: pre-wrap`、没有摘要/详情分离（见 10.1）。 |

这条消息同时暴露了 6 个问题：**强制工具、全量上下文、自由文本评审、无 issue 契约、版本计数器、UI 投影错误**。它不是“文风不好”，是协议设计的结果。

### 2.4 默认预算与真实需求不匹配

| 参数 | 默认值 | 代码位置 | 问题 |
|---|---|---|---|
| `max_model_calls` | 40（上限 80） | `settings.py:52` | happy path 24；一次返工 60；40 必然耗尽 |
| `max_agent_turns` | 20（上限 40） | `settings.py:53` | 单角色 `max_steps=48`，一个回合就能烧光请求数 |
| `max_revisions` | 2 | `settings.py:49` | 只允许 2 次评审，但每次评审前可有大量写操作 |
| `max_runtime_s` | 180 | `settings.py:51` | 24 次串行请求，按 5–10 秒/次就是 120–240 秒 |
| `node_timeout_s` | 45 | `settings.py:50` | 单次请求超时；不解决串行累计 |

预算耗尽时 `hub.py:333` 建议 `min(80, max(40, current*3))`。如果当前已经是 80，建议值还是 80，用户点“提高到 80 后继续”不会改变任何东西，形成**升级死循环**。

---

## 3. 产品与模式层：三个模式其实是同一套东西

> 本节分析的是**当前过渡实现**。产品已决定废除 `fast` / `expert`，所以正确的处理不是继续优化这两个模式，而是删除它们；但在删除前要理解它们为什么坏——否则很容易把同样的“按模式切预算”思路带进单模式。

### 3.1 “fast” 在配置模型后并不快（P0）

`graph/build.py:315-316`：

```python
peer_mode = settings.llm.configured and selected_mode in {"fast", "expert", "peer"}
fast_mode = selected_mode == "fast" and not peer_mode
```

只要配置了模型，`peer_mode=True`，`fast_mode=False`，`fast_planner` 根本不会挂载。所谓 `fast` 仍然进入 5 角色 `run_session`，只是 `max_turns=8`（`build.py:1043`）。而 happy path 本身就要 6 个回合，8 个回合只多出 2 次交接余量；任何需要额外“专业 Agent 返工/重新评审”的情况都会触顶，协议重试虽然通常发生在同一回合内，但会消耗请求数而不是回合数，同样会撞上 `max_model_calls=40`。

结果：

- 用户选“快速规划”，得到的是 24 次请求、2–4 分钟的深度团队流程；
- 真正的 `fast_planner` 只在**没配置模型**的规则降级路径里使用，和产品语义相反。

### 3.2 `expert` 与 `peer` 没有架构差异（P1）

`build.py:1040-1044`：

- `max_rounds`：`expert` 和 `peer` 都是 `max_revisions`；
- `max_turns`：`expert=12`，`peer=max_agent_turns`（默认 20）。

除此之外角色、工具、评审协议完全相同。两个模式只是“预算不同的同一个模式”，用户无法从行为上理解差异。UI 上写“专家模式 · 最多 12 个 Agent 回合”“深度团队 · 高成本”，但高成本没有量化，用户无法决策。

### 3.3 “团队”是组织架构图，不是任务图（P0）

`hub._validate_tasks`（`hub.py:101-125`）强制：

- owner 只能是 `logistics` 或 `local_expert`；
- 必须至少各一个；
- 每个 `local_expert` 任务必须依赖一个 `logistics` 任务。

这意味着**任务图永远是“交通住宿 → 本地体验”一条链**，不存在两个可并行的独立子问题。5 个角色里：

- `orchestrator` 没有工具，只拆任务/转返工；
- `logistics` 先做；
- `local_expert` 必须等；
- `critic` 只读；
- `writer` 只读。

真正同时干活的只有 1 个角色；其余是串行等待。多 Agent 的唯一收益是“角色隔离”，代价是 5 倍协议开销和 24 次请求。这不是团队，是流水线。

### 3.4 固定角色不随任务复杂度伸缩（P1）

1 天往返、5 天标准、14 天长假，都走同一套 5 角色。`fast` 也没有“单 Agent 直出”的路径。正确的做法是：

- 简单任务：1 次结构化规划 + 确定性校验；
- 复杂任务：按需启用专家/评审；
- 只有出现真实冲突（预算 vs 体验、交通变更影响日程）才升级到多角色。

当前是“所有任务都上最重的流程”，再用 `max_turns` 砍，体验自然差。

---

## 4. 编排协议层：LLM 在不需要它的地方反复出现

### 4.1 Orchestrator 是“橡皮图章”（P0）

`roles.py:39-52` 的 Orchestrator 没有工具，职责是拆任务、返工、提交评审。但：

- 任务形状被 `_validate_tasks` 固定成两类 owner；
- 任务依赖被强制为“本地依赖交通”；
- 下一个 Agent 由 `hub.schedule()` 按 DAG 直接决定（`hub.py:290-294`），不是 Orchestrator 决定；
- `to` 字段虽然必填，但下一跳基本被代码覆盖；
- `wake` 字段完全不被调度器读取（`hub.py` 没有任何 `wake` 消费逻辑）。

所以 Orchestrator 的每次调用，除了第一次拆解和每次返工转发，几乎不产生新信息。happy path 里它调用 2 次；一次返工后变成 5 次。这是纯协议开销。

### 4.2 `wake` / `done` / `to` 是死字段（P1）

`runtime._validate_decision`（`runtime.py:122-169`）要求每条决策必须包含：

- `kind`、`say`、`to`、`wake`、`done`、`escalate`；
- `to` / `wake` 必须是合法角色数组；
- `done` 必须是布尔。

但 `hub` 只用 `kind`、`say`、`changes`、`delivery`、`evidence`、`escalate`；`wake`、`done` 从未被读取，`to` 只用于 bus 收件人。模型每条消息都要输出这些空数组/布尔值，24 条消息就是 24 次无效字段；更糟的是，提示词让模型以为 `wake` 能决定谁被叫醒，实际不能，容易产生错误心智模型。

### 4.3 自由文本 JSON + 严格解析 = 协议重试（P0）

`runtime.py:285-312`：

- 模型最终回答必须是纯 JSON；
- `_parse_decision` 直接 `json.loads`，没有 `llm._extract_json` 的代码块容错；
- 校验失败后追加一条很长的“上条回复未遵守协作协议……”用户消息，然后 `continue`，再消耗一次模型请求。

模型在长上下文里偶尔加一句解释、用 ```json 包裹、或漏一个字段，就会触发一次完整重试。没有 JSON Schema、没有 function calling 的 `submit_decision`、没有局部修复，只有“重答”。

正确做法：把最终决策做成一个工具（`submit_decision`）或 `response_format=json_schema`，让供应商在解码层约束；解析失败只做一次字段级修复，不要重跑整轮。

### 4.4 评审和硬校验混在一起（P0）

`roles.py:92` 明确告诉 Critic：

> `规则 passed=true 只表示数值硬约束通过，不代表体验合理；需要结合用户需求独立判断。`

但系统没有给“体验合理”任何可验收标准：

- 没有评分维度/权重；
- 没有“最多提几条”；
- 没有“只提新问题”；
- 没有 severity（blocker/warning）；
- 没有 issue code；
- 没有“这条问题对应哪个修复工具”。

于是 Critic 可以每轮提出新的主观问题，且每次都是 blocking。你截图里 `check_plan passed=true, issues=[]`，但 Critic 仍然列出“夜景没安排、美食偏弱、故宫/国博闭馆”等，就是这套设计的必然结果。

### 4.5 评审问题没有生命周期（P0）

`hub.py:478-483` 的 `changes` 结构只有：

```json
{"owner":"logistics或local_expert","problem":"具体问题","required_change":"可执行返工要求"}
```

没有：

- `issue_id` / `code`（无法去重、无法跨轮跟踪）；
- `severity`（无法区分 blocker 和建议）；
- `evidence_ref`（无法证明问题基于哪条事实）；
- `acceptance`（无法判断修到什么程度算关闭）；
- `first_seen_version` / `fixed_in_version`（无法证明“已修复”）；
- `repair_tool`（无法自动修复）。

所以：

- 同一个问题第二轮可以换个说法再提；
- 修完之后无法自动验证关闭；
- 用户看到的“返工次数”只是一个数字，不知道哪些问题解决了；
- `pending_issue_codes` 字段虽然存在，但从未被填充，属于死状态。

### 4.6 评审能力与修复能力不匹配（P0）

截图里的问题可以直接映射到能力缺口：

| Critic 要求 | Local Expert 现有工具 | 结果 |
|---|---|---|
| 夜景没安排 | 没有夜间活动/餐厅工具；`planner` 的“晚上”只用于酒店入住 | 无法修复，只能重复反对 |
| 美食安排偏弱 | 没有餐厅/菜系工具；餐费是确定性估算 | 无法修复 |
| 酒店房型/楼层/发票/确认号 | 数据源没有这些字段 | 提示词说“标未核实”，但 Critic 仍可能要求 |
| 故宫/国博闭馆 | `submit_day_plans` 会拒绝闭馆，但需要替代景点 | 可修复，前提是候选池有替代且模型知道 |

代码里只有 `REPAIR_HINTS`（`tools_spec.py:35-48`）覆盖硬约束 code，主观问题没有修复映射。正确做法是：

- 建立 `issue_code -> repair function` 表；
- 没有 repair function 的问题只能是 `advisory`，不能 blocking；
- 数据缺口统一标记 `data_gap`，不进入返工队列，直接展示给用户。

---

## 5. 上下文与 token：同一份状态被讲四遍

### 5.1 每回合的 prompt 是全量拼装（P0）

`roles.py:34-35`：

```python
def prompt(self, ctx, bus):
    return f"用户需求：{brief_text(ctx)}\n团队已投递消息：\n{render_bus(bus, self.name, limit=12)}\n你的职责：{self.instruction}\n{CONTRACT}"
```

`hub.py:306-318` 继续追加：

```python
prompt += "\n任务状态：" + json.dumps(tasks)
prompt += f"\n当前任务：{active_task}；待审版本：{review_version}；已批准版本：{ctx.approved_version}"
prompt += "\n待返工问题：" + json.dumps(pending_rejections)
prompt += "\n交通住宿依赖交付事实：" + json.dumps(...)
prompt += "\n本回合收件：" + json.dumps([m.to_dict() for m in inbox])
```

一次请求里同时有：

1. `brief` 全量 JSON；
2. 最近 12 条 bus 消息（每条都带 `content`，可能还有 `hard_check` 全量 JSON，见 `runtime.render_bus:354-356`）；
3. 角色说明（几百字）；
4. `CONTRACT`（几百字）；
5. 完整任务表（含每个任务的 `delivery`、`rework` 历史）；
6. 本回合收件（与 bus 消息重复）；
7. 待返工问题（与 bus 里的 objection 重复）；
8. 交通住宿事实（与任务表里的 delivery 重复）。

### 5.2 `render_bus` 不按收件人过滤（P0）

`runtime.py:344-357`：

```python
for m in bus.messages[-limit:]:
    if m.sender == agent: who = "你"
    else: who = m.sender
    ...
    lines.append(f"[{who} → {to}] ({m.kind}) {m.content}")
```

它取的是**全局最后 12 条**，不是“发给这个 Agent 的 12 条”。后果：

- 每个 Agent 都能看到其他 Agent 的私有消息（信息隔离失效）；
- 与 `bus.take(agent)` 的“本回合收件”重复；
- 12 条里可能一半与自己无关，注意力被稀释；
- 消息内容没有截断，一条长 `say` 可以占满上下文。

### 5.3 历史按“消息条数”裁剪，不按 token（P1）

`runtime._complete_history(history, limit=12)`（`runtime.py:57-89`）只保留最后 12 条消息，但：

- 一个 assistant 消息带 4 个 tool_calls + 4 个 tool 结果就是 9 条；
- `get_plan` 一条 tool 结果可能 4–6k 字符；
- 12 条消息可能 2 万字符，也可能 2 千字符，完全不可控。

结果是：Agent 经常看不到自己两轮前的任务上下文，只能靠 prompt 里重新注入的 bus/任务表；而注入的又是全量，形成“历史丢了 → 全量补 → token 更高”的循环。

### 5.4 静态内容放在动态 prompt 里，KV cache 命中差（P1）

`system` 只有角色说明；`CONTRACT`、`你的职责`、`用户需求` 都在 user prompt 的前半段，后面紧跟动态 bus。供应商的 prefix cache 只能命中 system + 开头很短一段；每回合 bus 一变，后面全部失效。

正确做法：

- system：角色 + 契约 + 输出 schema + 评审 rubric（完全静态，可缓存）；
- user：只放“本次增量”（当前任务、变更 diff、待处理问题、必要事实），稳定前缀尽量长；
- 不要在 user 里重复 `brief` 全量；brief 的哈希/版本即可。

### 5.5 强制四工具让评审成本翻倍（P0）

`roles.py:87`：

> 必须在同一回合内一次性调用 `get_plan`、`check_plan`、`day_costs`、`experience_check` 四个工具，再给出结论；漏调用任一工具会被退回重做，浪费一整轮。

`hub.py:466-468` 强制校验这四个工具都调用过。

但：

- `get_plan` 返回完整 itinerary + flights + hotel + assignment + fixed_events + hard_check + total_cost（`tools_spec.py:513-531`）；
- `check_plan` 返回同一份 hard_check（`tools_spec.py:491-510`）；
- `day_costs` 是 itinerary 的按天投影（`tools_spec.py:567-570`）；
- `experience_check` 又跑一遍 `critic.run`，返回子集问题（`tools_spec.py:573-580`）。

四份数据高度重叠；Critic 为了拿这四份数据要发 4 次模型请求（脚本化模型每次一个工具），并且每份结果都会留在历史里，下一轮再发。正确做法是**代码预计算一个 `ReviewPacket`**：

```json
{
  "version": "sha256:...",
  "diff": [{"path":"days[2].items","before":[...],"after":[...]}],
  "hard_check": {"passed": true, "issues": []},
  "costs": {"total": 6450, "by_day": [...]},
  "open_issues": [...],
  "candidate_facts": {"night_pois": [...], "restaurants": [...]}
}
```

一次评审只发一个 packet，评审只输出 issue 列表，不再自己拉全量。

### 5.6 `check_plan` 有 6 个调用点，结果被反复序列化（P1）

同一次评审里，`check_plan` / `critic.run` 的调用点有：

1. `local_expert` 调 `materialize_plan` 时内部已跑 `critic.run` 并写入 `hard_check`（`tools_spec.py:460-474`）；
2. `local_expert` 又调 `check_plan`（角色提示要求）；
3. `hub` 在提交评审前又 `execute(ctx, "check_plan", {})`（`hub.py:425`）；
4. Critic 又调 `check_plan`（`hub.py:469` 还会再调一次）；
5. `writer` 交付前又 `check_plan`（`hub.py:493`）；
6. `peer_finalize` 再跑 `critic.run`（`build.py:1149`）。

由于 `tools_spec.execute` 有按 `plan_version` 的只读缓存，实际重算没有 6 次；但**每个调用点都会把完整 `issues` / `hard_check` 结果重新放进消息、事件和上下文**，并制造多个“看起来像真相源”的副本。正确做法是只算一次，把结果放进 `PlanArtifact.hard_check`，所有角色读同一个字段，不再提供重复的 `check_plan` / `experience_check` 读取工具。

### 5.7 `get_plan` 没有投影/分页（P1）

`_get_plan` 返回整个 `itinerary.model_dump()`，包括每天每项的 `slot/start_time/end_time/type/title/ref_id/cost/duration_min/note`。对 5 天行程约 4–6k 字符；14 天更长。Critic 每次评审都拿全量，无法只看 diff。

正确做法：

- `get_plan(view="summary")` 返回每天一行摘要；
- `get_plan(view="day", date=...)` 返回单日；
- `get_plan(view="diff", since_version=...)` 返回变更；
- 默认绝不返回全量。

### 5.8 `say` 没有长度/结构约束（P0）

`CONTRACT`（`roles.py:11-18`）只要求 `say` 是“对团队的结论和依据”，没有：

- 最大字数；
- 必须列 issue 而不是复述方案；
- 必须引用 evidence id；
- 必须区分“结论 / 证据 / 待办”。

Critic 于是把“总费用 6450、预算 8000、机票 4340、住宿 1040……”整段复述一遍，再列问题。你截图里的长文就是 `say` 字段。它同时承担了：

- 给用户看的总结；
- 给其他 Agent 看的分析；
- 给协议看的理由。

一个字段三种用途，必然又长又难读。正确做法：

- `summary`：≤120 字，只给用户看；
- `issues`：结构化数组，给协议/UI 看；
- `details`：可选，默认折叠，不进入其他 Agent 的上下文。

### 5.9 工具 schema 与角色说明本身也在膨胀（P1）

`tools_spec.py` 有 18 个工具，每个都有中文 description + JSON Schema；每个角色暴露 5–13 个。schema 随每次请求发送（`llm.chat_tools` 的 `payload["tools"]`），24 次请求就是 24 份重复 schema。工具描述里还有长句（例如 `route_distances` 的 `limitation` 结果每次都返回一整段“仅为坐标直线距离估算……”）。

这不是最大头，但在 24 次请求的基数上会累积。更严重的是**重叠工具**：

- `assign_pois` 与 `submit_day_plans` 功能重叠；
- `irreplaceability`、`elder_fit`、`experience_check` 都在回答“体验风险”；
- `hotel_prices` 与 `search_hotels` 返回同一批酒店的不同投影。

模型要在这些工具里选，增加决策成本和错误率。应该合并成少量高层工具，或由代码预计算后作为 `ReviewPacket` 提供。

---

## 6. 返工与收敛：为什么会有 v9

### 6.1 返工是“整任务重置”，不是“补丁”（P0）

`hub._request_rework`（`hub.py:129-147`）：

```python
tasks[tid]["rework"].append(str(request["reason"]))
...
while changed:
    new = {tid for tid, task in tasks.items() if set(task["depends_on"]) & affected} - affected
    affected |= new
for tid in affected:
    tasks[tid]["status"] = "pending"
    tasks[tid]["delivery"] = None
```

只要 Critic 指到一个任务，该任务和所有依赖它的任务全部回到 `pending`，`delivery` 清空。由于 `local_expert` 永远依赖 `logistics`，任何涉及交通/预算的反对都会把本地体验一起重置。

然后 `_delivery_valid`（`hub.py:150-166`）要求：

```python
assignments = {date.fromisoformat(d["date"]): d["poi_ids"] for d in days}
return len(days) == brief.days and assignments == ctx.assignment
```

专业 Agent 不能只提交“把 11-21 晚上加一个活动”，必须重新提交完整 5 天 `days`。这就是 32 次 `local_expert` 调用的来源。

正确做法：

- `apply_patch(task_id, base_version, patch)`，只改变更字段；
- `delivery` 只包含变更集；
- 依赖任务只在“共享事实真的变了”时失效（例如航班时刻变了），而不是任何 rework 都级联；
- 每个 issue 独立关闭，不要求整任务重交。

### 6.2 没有“部分接受”（P0）

`hub.py:478-483` 要求 Critic 的每个 `changes` 都有 owner；`hub.py:388-389` 要求 Orchestrator 的 rework 覆盖所有 owner。系统没有：

- 接受部分修改、保留其余；
- 把某条 issue 标为“已知风险，用户接受”；
- 把某条 issue 延后到下一轮；
- 只重开某个 issue 对应的最小任务。

`accept_risk` 被禁用（`hub.py:248-249`），所有 blocker 必须修；但软性问题也被当成 blocker，于是没有退出通道。

### 6.3 评审上限只有 2 轮，但版本可以到 v9（P0）

`max_rounds` 默认 2，按理最多 2 次评审；但 `plan_version` 是“每次写操作 +1”，一次评审前的准备阶段可以产生 v1→v9 的多次写操作。用户看到 `(v9)` 会以为评审了 9 轮，实际可能只有 1–2 轮评审 + 8 次写工具调用。

这暴露了版本语义问题：

- `plan_version` 不是“评审轮次”，也不是“内容版本”，只是“写操作计数”；
- 每次 `assign_pois` 都是新版本，即使内容相同；
- 评审锁 `review_version == plan_version` 要求评审期间不能有任何写操作，否则 `raise ValueError("评审期间方案版本发生变化")`（`hub.py:464-465`），又触发一次协议重试。

正确做法：

- `content_hash` 作为版本；
- 无变化的写操作不递增；
- `review_round` 与 `plan_version` 分开；
- 评审锁用 hash，不用全局计数器。

### 6.4 没有“问题已修复”的证明（P0）

系统只在 `pending_rejections` 里保存上一轮 Critic 的 changes，下一轮 Orchestrator 必须逐项 rework；但：

- 没有保存“上一轮每条 issue 的关闭状态”；
- 没有在下一轮评审时只检查未关闭 issue；
- 没有把“修复动作”映射回 issue；
- 没有验证“修复后旧问题不再出现”。

所以 Critic 每轮都从零读全量方案，自然可能重复提旧问题或提新问题。正确做法是 issue ledger：

```json
{
  "id": "ISS-003",
  "code": "NIGHT_VIEW_MISSING",
  "severity": "blocker",
  "owner": "local_expert",
  "evidence_ref": "ev_...",
  "acceptance": "11-21 20:00 后有一个夜间活动，且不与闭馆/交通冲突",
  "status": "open",
  "first_seen_version": "sha256:...",
  "fixed_in_version": null,
  "repair_tool": "add_fixed_event"
}
```

评审只允许：

- 对 `open` issue 判断“仍未关闭”；
- 新增 issue（必须带 code/acceptance/repair_tool）；
- 不能重复提已关闭 issue，除非给出“回归证据”。

### 6.5 软性问题不应默认 blocking（P0）

截图里 `check_plan passed=true, issues=[]`，说明硬约束全过；Critic 仍因“夜景/美食”反对。这是产品策略问题：

- 如果“夜景”是用户明确偏好，应该在 Brief 里变成可验收的约束（例如“至少 2 个夜间活动”），由确定性检查器验证；
- 如果只是“建议”，应该是 warning，不阻塞交付；
- 如果是“必须”，需要 Critic 给出可执行的修复路径和验收标准，而不是自由文本。

当前把“偏好”交给一个没有 rubric 的 LLM 评审，等于把产品验收标准外包给模型的心情。

### 6.6 评审的“反对”没有成本意识（P1）

`roles.py` 没有告诉 Critic：

- 每条反对会增加多少请求/多少秒；
- 哪些问题值得返工，哪些可以标为建议；
- 返工上限是多少；
- 当前预算还剩多少。

所以 Critic 可以为了一个“夜景”提出 blocking objection，触发 30+ 次请求。正确做法：

- 在 ReviewPacket 里给 `budget_remaining`、`review_round`、`max_new_issues`；
- 要求 Critic 按“影响 × 修复成本”排序，最多提 N 条；
- 超过 N 条的进入 `advisory`。

---

## 7. 工具与数据层：看起来很多，实际缺口很大

### 7.1 重复请求数据源（P1，代码 bug）

`_set_flights`（`tools_spec.py:378-395`）：

```python
if not ctx.transport.outbound or not ctx.transport.inbound:
    result = _search_flights(ctx, {})          # 已经查了去/返程，并写入 ctx
    if not result.get("ok"):
        return result
    try:
        ctx.transport.outbound = ctx.source.search_flights(...)  # 又查一次
        ctx.transport.inbound = ctx.source.search_flights(...)   # 再查一次
```

`_search_flights` 内部已经调用了两次 `source.search_flights` 并写入 `ctx.transport`；这里又无条件查两次。**一次 `set_flights` 可能打 4 次数据源**。

`_materialize`（`tools_spec.py:426-438`）有同样的重复查询。

在 mock 数据上只是浪费；接真实 API 就是 2 倍延迟和 2 倍调用成本。

### 7.2 只读缓存被“无变化写操作”打穿（P1）

`_cache_key`（`tools_spec.py:831-836`）对 `_STATEFUL_READS` 用 `plan_version` 做 key。`_assign_pois`、`_set_hotel`、`_set_flights` 即使提交相同值也 `plan_version += 1`（`tools_spec.py:194-199`、`370-374`、`408-411`），于是：

- `get_plan` 缓存失效；
- `check_plan` 缓存失效；
- `day_costs` / `experience_check` 缓存失效；
- Critic 必须重新拉全量。

这是“无变化写操作 → 版本膨胀 → 缓存失效 → 上下文膨胀”的连锁反应。

### 7.3 `get_plan` / `check_plan` / `day_costs` / `experience_check` 是同一份数据的四个投影（P1）

如 5.5 所述，四个工具都从 `ctx.itinerary` / `ctx.hard_check` 派生。应该合并成一个 `inspect_plan(view=...)`，或者干脆由代码生成 `ReviewPacket`，不让 Agent 自己拉。

### 7.4 工具结果没有大小上限（P1）

`get_plan` 返回全量 itinerary；`search_flights` 返回所有航班；`list_pois` 返回所有候选（含 `excluded`）；`route_distances` 返回每个点的距离 + 一整段 `limitation`。没有 `limit`、`fields`、`page` 参数，也没有服务端截断。

对 14 天行程，`get_plan` 可能 15k+ 字符；Critic 一次评审 4 个工具就是 20k+ 字符。上下文窗口和费用都不可控。

### 7.5 证据是“随机 UUID”，不是“事实引用”（P0）

`runtime.py:245-257`：

```python
if result.get("ok") and not result.get("evidence_id"):
    evidence_id = uuid4().hex
    evidence = {"evidence_id": evidence_id, "agent": agent, "tool": call["name"], "arguments": ..., "result": deepcopy(result)}
    ctx.evidence[evidence_id] = evidence
    result = {**result, "evidence_id": evidence_id}
```

`_validate_decision` 只检查 `item["id"] in ctx.evidence`（`runtime.py:290-300`）。也就是说：

- 证据 ID 与结论内容没有绑定；
- 模型可以引用任意一条成功工具结果，哪怕它与结论无关；
- “引用证据”变成仪式，不提供可验证性；
- `ctx.evidence` 保存完整 result 的深拷贝，和 `ctx.operations`、`history`、事件各存一份，内存/checkpoint 膨胀。

正确做法：

- 证据用内容哈希 + 字段路径（`sha256(result_json)#/flights/0`）；
- 评审时校验“结论字段 → 证据字段”的映射；
- 证据只存一次（content-addressed），其他位置只存 ID。

### 7.6 Writer 的“事实校验”是子串匹配，且有全局绕过（P0，代码 bug）

`hub._writer_valid`（`hub.py:188-195`）：

```python
text = " ".join(text_parts)
unsupported_claims = ("已预订", "已确认", "确认号", "含早餐", ...)
if any(claim in text and "未核实" not in text and "待确认" not in text for claim in unsupported_claims):
    return False
```

注意条件：`"未核实" not in text` 是对**整段文本**的判断。只要文档任何地方出现一次“未核实”，所有 `unsupported_claims` 的检查全部失效。例如：

> “航班已确认，酒店含早餐；房型未核实。”

这段会通过，因为 `"未核实" in text` 为 True，`any(...)` 里每个 claim 的 `and "未核实" not in text` 都是 False。

这是典型的“用自然语言子串做业务规则”的反模式：

- 无法处理同义改写（“订好了”“保证有房”）；
- 无法定位到具体断言；
- 一个“未核实”可以给全文开免责；
- 无法验证价格/时间/航班号是否与 itinerary 一致。

正确做法：

- Writer 不自由生成事实，只填模板槽位；
- 槽位值来自 `PlanArtifact`，由代码注入；
- 需要自然语言润色的部分，限制在“不得包含数字/专有名词”的段落；
- 用结构化 `claims` 数组 + 每个 claim 的 `evidence_ref` 校验，而不是全文子串。

### 7.7 其他子串规则同样脆弱（P1）

- `critic._timeline_issues` 用 `day.notes` 里是否包含“没有足够午餐时间窗”“自行安排晚餐”来判断是否报缺餐（`critic.py:149-168`）。文案一改，规则就失效。
- `summary.warning_groups` 用“交通/酒店/预算”等中文子串给警告分类（`summary.py:24-32`）。
- `_writer_valid` 用“步行可达/步行即到”判断路线断言（`hub.py:188-192`）。

这些都是“文案即协议”。正确做法是让 planner 输出结构化 `meal_status`、`warning.code`，UI 用 code 分组。

---

## 8. 状态、持久化与事件：70MB 的 checkpoint 是怎么来的

### 8.1 `peer_state` 把整个会话塞进图状态（P0）

`hub.run_session` 返回的 `session`（`hub.py:525-531`）包含：

```python
{
  "architecture": "professional_team",
  "bus": bus.to_dict(),          # 所有消息 + payload
  "context": dump_context(ctx),  # itinerary + evidence + operations + document
  "tasks": tasks,                # 含 delivery/rework
  "histories": histories,        # 每个角色的完整消息数组（含 tool 结果）
  "trace": trace,
  ...
}
```

`build.peer_run_node` 把它写进 `updates["peer_state"]`（`build.py:1051`），LangGraph checkpoint 每次节点写入都会 pickle 这个状态。`checkpoint.py:136-161` 每次 `put` 都会序列化整个 thread 的所有 checkpoint。

代码注释和整改文档已经承认：真实 checkpoint 约 **70.2 MB / 405 个线程**（`docs/peer-agent-remediation-plan.md:15`）。这不是“存储优化”问题，是**同一份事实被复制了 4 份**：

- `histories` 里的 tool 结果；
- `ctx.evidence` 里的完整 result；
- `ctx.operations` 里的完整 result；
- `bus` 消息 payload 里的 `hard_check` / `delivery`；
- 事件流里的 `peer_message.payload`。

正确做法：

- 图状态只存 artifact ID / hash / 任务状态；
- 消息、证据、操作日志存独立的内容寻址存储；
- checkpoint 只存“当前版本 + 未完成步骤”；
- 事件只存引用，不存全量 payload。

### 8.2 事件流重复大 payload（P1）

`event_stream.from_custom` 的 `peer_message` 分支（`event_stream.py:134-155`）把 `row["payload"]` 整个塞进事件 `payload.payload`。而 `post()` 的 payload 可能包含：

- `tasks` 全量（含 delivery）；
- `hard_check` 全量（issues 列表）；
- `changes` 全量；
- `escalation`。

结果是每条 Agent 消息在 `run_events` 里存两份：`text` 一份，`payload` 一份。当前数据库 `run_events` 已有 **35,144 行**（本次查询 `plans.db`）。UI 回放时还要把整条事件加载进内存，再在每个消息渲染时 `events.filter(...)`（`App.tsx:535`），复杂度是 O(消息数 × 事件数)。

正确做法：

- 事件只存 `message_id`、`text`、`kind`、`to`、`version`、`payload_ref`；
- 大对象存 artifact 表，按 ID 取；
- UI 用索引映射，不做全量 filter。

### 8.3 `peer_status` 把“唤醒/状态”变成假消息（P1）

`event_stream.py:157-191` 把每个 `peer_status`（`wake`、`session_end`、`turn_failed`、`escalated`）都包装成 `agent_message`，并分配 `message_id`。于是：

- 每个回合都多一条“被叫醒（执行任务 T1）”消息；
- 这些消息没有信息量，却进入对话流、进入数据库、进入回放；
- 用户看到的“Agent 对话”被状态噪音填满。

正确做法：

- `wake` / `status` 用独立的 `agent_status` 事件，UI 显示为时间线/状态点，不进入消息气泡；
- 只有真正的 `request/proposal/objection/accept/final` 才是消息。

### 8.4 恢复时 `turns` 清零但 `reviews` 不清零（P1）

`hub.py:209-211`：

```python
# 每次恢复按新的回合预算执行；否则中断前的消耗会让恢复后几乎没有余量。
turns = 0
reviews = int(resume.get("reviews", 0))
```

恢复后回合数重置，但评审轮次保留。如果上次已经 `reviews == max_rounds`，恢复后只要再提交评审就会立即触发 `review_rounds_exhausted`，没有“再给一轮”的空间。同时 `max_model_calls` 是累计的（`LLMClient.initial_stats`），恢复后可能一请求就超限。两个预算的语义不一致，用户点“继续”经常直接再次失败。

---

## 9. 性能与可靠性：24 次串行请求的代价

### 9.1 工具调用是串行的，但提示词让模型“并行”（P0）

`runtime.py:221-283`：

```python
calls = resp.get("tool_calls") or []
if calls:
    messages.append(...)
    for call in calls:
        ...
        result = tools_spec.execute(ctx, call["name"], call["arguments"])
        ...
        messages.append({"role": "tool", ...})
```

即使模型一次返回 4 个工具调用，代码也是 `for` 循环逐个执行。提示词却说“一次性并行调用”（`roles.py:58、70、87`）。结果：

- 模型以为并行，实际串行；
- 4 个工具 = 4 次数据源延迟累加；
- 如果模型一次只发一个工具（脚本化模型就是这样），每个工具还要一次完整模型请求。

正确做法：

- 对只读工具用 `asyncio.gather` 真正并行；
- 对写工具保持串行并做事务；
- 提示词不要再写“并行”，除非运行时真的并行。

### 9.2 每次请求新建 HTTP 连接（P1）

`llm.py:44-68` 用 `urllib.request.urlopen`，每次 `_post` 都新建连接，没有连接池/keep-alive。24 次请求就是 24 次 TCP/TLS 握手（HTTPS 更明显）。在 180 秒总预算下，这是实打实的延迟。

正确做法：用 `httpx.AsyncClient` / `aiohttp` 复用连接；设置连接池、超时、重试。

### 9.3 `asyncio.wait_for(to_thread(...))` 超时后线程还在跑（P1）

`llm.py:174-179`：

```python
data = await asyncio.wait_for(
    asyncio.to_thread(_post, ...),
    timeout=self.timeout_s + 1,
)
```

`to_thread` 的线程无法被取消；`wait_for` 超时只是让协程返回，后台线程仍可能继续请求、继续消耗供应商 token，甚至在应用取消后完成。结果是：

- 用户取消/超时后，供应商仍在计费；
- 线程池被慢请求占满；
- `calls` 已计数，但结果被丢弃。

正确做法：用支持取消的 HTTP 客户端；或至少给 `_post` 传超时并在超时后关闭连接。

### 9.4 超时/预算/回合三个限制语义不一致（P0）

- `node_timeout_s=45`：单次 HTTP 请求；
- `max_runtime_s=180`：整个图；
- `max_model_calls=40`：供应商请求总数；
- `max_agent_turns=20`：Agent 交接次数；
- 单角色 `max_steps=32/48`：一个回合内的模型步数。

一个 `local_expert` 回合最多 48 步，已经超过 `max_model_calls=40`。也就是说，“回合”这个限制根本约束不住请求数；真正生效的是 `max_model_calls` 和 180 秒。用户在 UI 上调 `max_agent_turns` 可能完全没有感觉。

正确做法：

- 只保留“总请求数 + 总 token + 总时长”三个硬预算；
- 每个 Agent 回合的步数上限应 ≤ 总请求数；
- UI 只暴露一个有意义的“运行预算”（总请求数/总时长），不要暴露 5 个互相打架的数字；深度团队内部按“是否存在未关闭 issue”自适应，不再让用户选快速/专家。

### 9.5 前端 O(n²) 事件过滤（P2）

`App.tsx:535`：

```tsx
const tools = events.filter((candidate) => candidate.event_type === 'tool_call' && candidate.parent_id === ev.event_id)
```

对每条消息都扫一遍全部事件。`run_events` 已有 3.5 万行；一次运行几百条事件时，消息列表渲染会明显卡顿。应该先建 `Map<parent_id, events[]>` 索引。

---

## 10. 前端与可读性：不是“再美化一下”的问题

### 10.1 消息本体没有结构（P0）

`App.tsx:562`：

```tsx
<div className="message-bubble">{messageText(ev) || '已完成这一项处理。'}</div>
```

`messageText` 直接返回 `p.text`。没有：

- Markdown 渲染；
- 段落/列表；
- 折叠；
- “摘要 + 展开详情”；
- 按消息类型（proposal/objection/accept）不同的卡片；
- issue 列表；
- 版本 diff。

CSS `.message-bubble`（`styles.css:583-591`）也没有 `white-space: pre-wrap`，模型输出里的换行会被 HTML collapse 掉，长文变成一整块。

### 10.2 “查看依据”计数错误（P0，用户可见）

截图里 Critic 明明调用了 4 个工具，UI 显示“1 次工具调用”。原因：

- `_peer_emit` 发出的 `tool_call` 事件 `node=agent`（如 `critic_agent`）；
- `EventFactory.from_custom` 的 `tool_call` 分支用 `parent_id=self._active_agents.get(node)`（`event_stream.py:263-278`）；
- `_active_agents` 只记录图节点（`peer_run`），没有 `critic_agent`；
- 所以 `parent_id=None`；
- `Conversation` 先按 `parent_id === ev.event_id` 找工具，找不到，退化到 `orphanTools`：同 actor、`seq < ev.seq` 且差值 ≤ 3（`App.tsx:535-543`）；
- 4 个工具 + 4 个结果共 8 条事件，只有最后 1 个工具落在 3 条窗口内 → 显示 1 次。

这不是“少显示一个数字”，是**证据链在 UI 上断了**。用户无法验证 Agent 的结论，信任感直接崩。

正确做法：

- `tool_call` 事件带 `parent_message_id`（由 `post()` 生成的消息 ID）；
- 前端按 `message_id` 建索引；
- 不要用 `seq` 窗口猜。

### 10.3 证据抽屉没有参数/结果（P1）

`App.tsx:574-582` 只显示工具名和 `summary/status`：

```tsx
<span className="evidence-tool">{...}</span>
<span>{result?.summary ?? result?.status ?? '已执行'}</span>
```

需求 FR-7 要求“工具调用（含参数与返回）”，但 UI 既没有 arguments，也没有 result 内容。用户点开“查看依据”只看到“get_plan 完成”，无法判断依据是否成立。

### 10.4 没有实时任务板/评审状态（P0）

`team_tasks` 只在最终 `result` 里渲染（`App.tsx:1555-1572`），运行中看不到：

- 谁在跑；
- 哪个任务 delivered；
- 当前评审第几轮；
- 哪些 issue 未关闭；
- 预算还剩多少。

用户只能看“Agent 对话”猜进度。这直接导致“又长又乱”的观感。

### 10.5 角色没有视觉区分（P1）

`styles.css:542-546` 只为旧协商角色（budget_officer/experience_officer/mediator）定义了头像颜色；新的 `orchestrator/logistics/local_expert/critic_agent/writer` 全部用默认 `--accent` 蓝色。截图中“方案评审”和“团队统筹”都是蓝色圆形头像，只能靠文字区分。

### 10.6 阶段分组把 5 个角色压成一块（P1）

`Conversation` 按 `stage` 分组（`App.tsx:487-492`），peer 的所有消息 stage 都是 `peer`，所以 5 个角色、24 条消息全部在一个“专业团队协作”大块里。没有按回合/评审轮次分组，也没有折叠。用户看到的是“一大段聊天记录”，不是“一次评审”。

### 10.7 没有版本 diff / issue 时间线（P0）

用户无法回答：

- v8 → v9 改了什么？
- 上一轮的 3 个问题修了几个？
- 为什么又出现一个新问题？
- 这条反对对应哪个任务、哪个景点、哪个费用？

没有 diff/issue 时间线，“返工”对用户就是一个黑盒，只能感觉“又在重来”。

---

## 11. 正确性与安全：几个会被真实用户踩到的坑

### 11.1 Writer 文档可能与 itinerary 不一致（P0）

`_writer_valid` 只校验：

- `document.days` 的日期序列与 itinerary 一致；
- 每天有非空 `summary`；
- 全文不含敏感断言（且有 7.6 的绕过）。

它不校验：

- 航班号/时间；
- 酒店名；
- 景点名；
- 价格；
- 餐食安排；
- `practical_notes` 与事实的对应。

Writer 可以在 `summary` 里写“上午 9 点飞成都”，而 itinerary 是“下午 3 点”；只要日期对、没有敏感词，就会通过。最终 UI 展示的是 `document`（`App.tsx:1535-1553`），不是 itinerary，用户看到的是可能错误的故事。

### 11.2 `document` 与 `itinerary` 双真相源（P1）

`result.document` 和 `result.itinerary` 同时存在：

- 文档页展示 `document`；
- 费用卡/校验展示 `itinerary`；
- `GET /api/plan/{id}` 用一个相等性检查决定是否展示文档（README:478），但运行中不检查。

两套数据最终会漂移。正确做法：文档只存“叙事层”（每天一段话），所有数字/名称从 itinerary 注入，不重复存。

### 11.3 工具结果可被外部数据注入（P1）

`search_flights` / `search_hotels` / `list_pois` 的结果直接 JSON 序列化进 tool 消息，进入模型上下文。如果接真实 HTTP 数据源，供应商返回的 `name/note/tags` 里可以包含“忽略之前的指令……”之类的内容。当前没有内容隔离/转义/标注。

演示用 mock 风险低；一旦接真实 API，这是 prompt injection 面。

### 11.4 API key 明文存 SQLite（P2，已知）

`settings.py` 把 `api_key` 明文存 `app_config.payload`，只做掩码回传。README 已注明“仅演示环境”。不是本次架构重点，但要在上线前解决（OS keychain / secret manager）。

### 11.5 事件/checkpoint 可能泄露完整工具结果（P2）

`ctx.evidence` / `ctx.operations` / `peer_state` 保存完整 result；如果接真实酒店/机票 API，可能包含价格、库存、内部字段。事件 payload 也会存。需要脱敏/保留策略。

---

## 12. 观测与测试：现在的“24 次调用”被当成成功，而不是成本事故

### 12.1 `input_chars` 在工具路径根本没有累加（P0，代码 bug）

`llm.py:226-309` 的 `chat_tools`：

```python
self._reserve_call()
payload = {...}
if self.budget:
    self.budget.add(json.dumps(payload, ensure_ascii=False)[:4000])
```

它**没有** `self.input_chars += ...`。而 `stats()`（`llm.py:118-130`）把 `input_chars` 返回给前端。结果是：

- peer 的所有请求都走 `chat_tools`；
- `input_chars` 始终是 0（除非 `initial_stats` 带入旧值）；
- 当供应商不返回 usage 时，前端 `RunUsage` 显示“输入/输出 0/xxx 字符”（`App.tsx:160`）；
- 用户/开发者无法从 UI 看到真实的输入 token 成本。

同时 `budget.add(...[:4000])` 只把 payload 前 4000 字符计入预算，实际请求可能是 1 万字符；`TokenBudget` 形同虚设。

### 12.2 没有 per-agent 成本（P1）

`llm_stats` 是全局聚合：

- 不知道 `critic_agent` 花了多少；
- 不知道 `local_expert` 的 10 次请求里哪次最贵；
- 不知道是 prompt 重复还是工具结果导致膨胀；
- 无法做“砍掉某个角色能省多少”的决策。

正确做法：每次 `chat_tools` 记录 `agent`、`purpose`、`input_tokens`、`output_tokens`、`prompt_hash`，写入 trace；UI 按 Agent/阶段聚合。

### 12.3 测试把“24 次调用”当成 PASS（P1）

`verify_peer_success.py` 的断言是“调用数在默认预算内”（24 ≤ 40），于是 24 次被固化为“成功路径”。但：

- 没有断言“输入字符上限”；
- 没有断言“单次 prompt 大小”；
- 没有断言“一次返工不超过 N 次”；
- 没有断言“同一 issue 不重复”；
- 没有真实模型 adherence 测试；
- `verify_graph.py` 还有 1 条已知失败（STATE.md:65）。

这不是测试覆盖问题，是**目标函数错了**：应该把“24 次”当成待优化的基线，而不是通过条件。

### 12.4 没有端到端 UI 断言（P1）

截图里的“4 个工具显示 1 次”这种 bug，现有测试抓不到，因为没有：

- 事件 parent_id 契约测试；
- “N 个工具调用 → UI 显示 N”测试；
- 长消息折叠/结构测试；
- 实时任务板测试。

---

## 13. 目标架构：把“聊天团队”换成“工件流水线”

你明确说只要多 Agent 编排，所以不建议退回单 Agent；建议把“多 Agent”做成真正并行的工件流水线，而不是 5 个角色聊同一份全量状态。

### 13.1 编排图

```
Brief
  │
  ├─ retrieve_facts (code, parallel)          # 航班/酒店/景点/天气，只查一次
  │
  ├─ TransportAgent (LLM) ──┐
  ├─ LodgingAgent   (LLM) ──┤  并行，各自只拿自己的候选集
  ├─ LocalAgent     (LLM) ──┤
  └─ FoodAgent      (LLM) ──┘
            │
        merge_plan (code)                     # 校验 + 组装 PlanArtifact
            │
        hard_check (code)                     # 确定性，一次
            │
        repair (code/LLM, 按 issue code)      # 只修有 repair_tool 的问题
            │
        review_packet (code)                  # diff + open issues + 事实
            │
        Reviewer (LLM, 1 call)                # 只输出 issue ledger 变更
            │
        accept? ── no ──> repair (bounded)
            │ yes
        Writer (template; 可选 LLM 润色)
            │
        PlanArtifact + Document
```

关键差异：

- **事实查询是代码，不是 Agent 工具调用**：一次并行拉取，结果作为工件；
- **专业 Agent 并行**：交通/住宿/本地/餐饮互不依赖（除非明确有依赖），各自只读自己的候选集；
- **合并是代码**：确定性校验、组装、费用计算；
- **评审只做一次 LLM 调用**，输入是 diff + open issues + 事实，不是全量方案；
- **修复按 issue code 走确定性函数**；只有需要“选哪个”时才问 LLM，且候选集很小；
- **Writer 是模板**：数字/名称从 PlanArtifact 注入，LLM 只写叙事段落。

### 13.2 工件契约

```json
{
  "artifact": "PlanArtifact",
  "version": "sha256:...",
  "brief_hash": "sha256:...",
  "transport": {"outbound": "...", "inbound": "..."},
  "lodging": {"hotel_id": "..."},
  "days": [
    {"date": "2026-11-21", "poi_ids": ["p02", "p03"], "fixed_events": []}
  ],
  "derived": {
    "itinerary": {"days": [...]},
    "costs": {"total": 6450, "by_day": [...]},
    "hard_check": {"passed": true, "issues": []}
  }
}
```

Agent 之间不传自然语言全文，只传：

- `artifact_id` / `version`；
- 自己负责的切片；
- 必要的候选事实。

### 13.3 Issue Ledger

```json
{
  "id": "ISS-003",
  "code": "NIGHT_VIEW_MISSING",
  "severity": "blocker",
  "owner": "local_expert",
  "evidence_ref": "sha256:...#/days/2/items",
  "acceptance": "11-21 20:00 后存在一个夜间活动，且不与闭馆/交通冲突",
  "status": "open",
  "first_seen_version": "sha256:...",
  "fixed_in_version": null,
  "repair_tool": "add_fixed_event",
  "attempts": 1
}
```

规则：

- 评审只能对 `open` issue 判“仍未关闭”，或新增 issue；
- 新增 issue 必须带 `code`、`acceptance`、`repair_tool`（或 `advisory`）；
- 已关闭 issue 不能重复提，除非带回归证据；
- 每轮最多新增 3 条 blocker；
- `advisory` 不阻塞交付，UI 单独展示。

### 13.4 上下文预算

每次 LLM 调用硬限制：

| 项 | 目标 |
|---|---|
| system（角色 + 契约 + schema） | ≤ 1,500 tokens |
| user（切片 + 候选 + open issues） | ≤ 4,000 tokens |
| 输出 | ≤ 800 tokens |
| 单次总输入 | ≤ 6,000 tokens |
| 单次运行总输入 | ≤ 60,000 tokens（常规路径）/ 120,000（出现冲突并进入评审/修复） |

超出就截断/摘要，不把全量 JSON 塞进去。

### 13.5 单模式设计（废除 fast/expert 后）

产品只保留一个“深度团队”入口。用户不选快慢；系统按“是否存在未关闭 issue”自适应评审/修复深度。**这不是重新引入 fast/expert**：模式是用户可见的产品分支；自适应深度是同一模式内部的调度策略，用户不需要选，也不应该感知到两套流程。

| 场景 | LLM 调用 | 评审 | 目标 |
|---|---|---|---|
| 无冲突：硬校验一次通过，无软性问题 | 1 次规划（结构化输出）+ 确定性校验 | 0 次 LLM 评审 | 1–3 次请求，<30s |
| 硬约束冲突：预算/闭馆/赶机 | 1 次规划 + 确定性修复；需要取舍时 1 次选择 | 0–1 次 | 3–5 次请求 |
| 软性偏好冲突：夜景/美食/节奏 | 1 次规划 + 1 次评审 + ≤2 次定向修复 | 1–2 轮，issue ledger | 6–10 次请求，<120s |
| 多轮冲突/用户要求更细 | 同上，允许第 2 轮评审 | 最多 2 轮，问题必须可关闭 | 8–12 次请求，<180s |

这样“深度团队”才是“更多评审/修复”，而不是“更多角色聊天”；单模式的成本由**问题是否真的存在**决定，而不是由用户猜“快速还是深度”。

---

### 13.6 业界对照（这些不是拍脑袋）

- Anthropic 的 orchestrator-worker 多 Agent 研究系统明确给出两个前提：任务必须**真正可并行**，且价值足以覆盖成本；他们实测多 Agent 比单 Agent 聊天多消耗约 **15 倍 token**，并强调 token 使用量解释了大部分效果方差（[How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)）。当前旅行任务里，**“选择”可以并行（交通/住宿/景点/餐饮候选），但“最终排程”必须等交通时刻确定**；所以并行收益有限，必须靠工件化、增量评审和确定性修复把成本压住，而不是靠堆角色。当前实现把整条链做成硬依赖，连这点有限并行都没有。
- OpenAI Agents SDK 的 handoff 是“把交接建模成工具”，支持 `input_type` 给交接参数做 schema、`input_filter` 控制下一个 Agent 能看到多少历史、guardrail/tripwire 做输入输出校验（[Handoffs](https://openai.github.io/openai-agents-python/handoffs/)、[Guardrails](https://openai.github.io/openai-agents-python/guardrails/)）。这正好对应本报告的建议：交接要有类型化载荷和可见性过滤，而不是把全量聊天记录丢给下一个角色；校验要在工具边界做，而不是靠提示词“请不要”。
- 结构化输出/工具化决策是主流做法：让模型调用 `submit_decision`/`submit_review` 之类的函数，而不是自由文本 JSON + 正则/`json.loads`。这能消掉 4.3 的协议重试。

---

## 14. 建议的改造顺序（按投入产出比）

### P0：先止血（1–2 天）

1. **合并评审输入**：新增 `ReviewPacket`，Critic 不再调用 4 个工具；`get_plan` 默认只返回摘要。
2. **Issue Ledger**：`changes` 增加 `code/severity/acceptance/repair_tool`；评审只报新问题/未关闭问题。
3. **版本修正**：无变化写操作不递增；`plan_version` 改为 `content_hash`；评审锁用 hash。
4. **修复证据链**：`tool_call` 事件带 `parent_message_id`；前端按 message_id 配对；修“4 次显示 1 次”。
5. **`say` 限长 + 拆分**：`summary` ≤120 字，`details` 可选折叠；默认 UI 只显示 summary。
6. **`input_chars` 修复**：`chat_tools` 累加真实 payload 字符；预算不再 `[:4000]`。
7. **预算死循环**：`suggested_limit` 到达 80 时给出“降低范围/停止”而不是“再提高到 80”。

### P1：结构改造（3–5 天）

8. **确定性修复映射**：`issue.code -> repair(ctx)`；没有 repair 的 issue 只能是 advisory。
9. **去掉 Orchestrator LLM**：任务图由代码模板生成；需要仲裁时用一次 `arbiter` 调用，带 2–3 个选项。
10. **并行专业 Agent**：交通/住宿/本地/餐饮并行，工件契约；合并代码化。
11. **结构化输出**：用 `submit_decision` 工具或 JSON Schema，删掉自由文本解析和长纠正 prompt。
12. **上下文编译**：system 静态化；user 只放切片/diff/open issues；按 token 裁剪。
13. **Writer 模板化**：数字/名称从 artifact 注入；LLM 只写叙事，禁止生成事实。
14. **实时任务板/issue 面板**：运行中显示 roster、任务状态、评审轮次、open issues、预算。

### P2：产品化（1–2 周）

15. **工件存储**：artifact/content-addressed store；checkpoint 只存 ID/hash；事件只存引用。
16. **可观测**：per-agent token/延迟/成本；prompt 大小；缓存命中；预算告警。
17. **UI 重构**：消息卡片化、diff 视图、issue 时间线、证据抽屉（参数+结果）、角色颜色/图标。
18. **真实数据源**：接 API 后加内容隔离/脱敏/重试/限流。
19. **评测体系**：把“请求数/输入 token/评审轮次/重复 issue 率”纳入回归；24 次不再是 PASS，而是基线。

---

## 15. 预期收益（按上面的改造）

以 5 天标准行程、一次软性问题为例：

| 指标 | 当前（实测脚本） | 目标 | 说明 |
|---|---|---|---|
| LLM 请求 | 24（happy）/ 60（一次返工） | 6–10 / 8–14 | 去掉 Orchestrator 往返、强制四工具、重复评审 |
| 输入字符 | 178k / 749k | 30–60k / 50–90k | ReviewPacket + 切片 + 无全量 JSON |
| 评审轮次 | 最多 2，但版本可 v9 | 最多 2，issue 可关闭 | Ledger + 验收标准 |
| 重复 issue | 无跟踪 | 0（同一 code 不重复） | 关闭状态 + 回归证据 |
| 单次运行 | 2–4 分钟，常超 180s | <60–120s | 并行 + 少请求 |
| UI 可读性 | 长文 + 错误证据计数 | 摘要 + 卡片 + diff | 结构化消息 |

> 这些是目标值，不是已验证结果；需要按第 14 节改造后重新测量。

---

## 16. 附：关键代码索引

| 主题 | 文件:行 |
|---|---|
| 角色 prompt / 契约 | `backend/app/agents/roles.py:11-35` |
| Critic 强制四工具 | `backend/app/agents/roles.py:83-98`；`hub.py:466-468` |
| 每回合全量 prompt | `backend/app/agents/hub.py:305-318` |
| 任务校验/固定 DAG | `backend/app/agents/hub.py:101-125` |
| 返工级联 | `backend/app/agents/hub.py:129-147` |
| delivery 全量相等 | `backend/app/agents/hub.py:150-166` |
| Writer 子串校验/绕过 | `backend/app/agents/hub.py:169-195` |
| 评审 changes 结构 | `backend/app/agents/hub.py:478-483` |
| session 全量状态 | `backend/app/agents/hub.py:525-531` |
| 历史按消息裁剪 | `backend/app/agents/runtime.py:57-89` |
| 自由文本 JSON 解析/重试 | `backend/app/agents/runtime.py:285-312` |
| bus 渲染（不过滤收件人） | `backend/app/agents/runtime.py:344-357` |
| `get_plan` 全量 | `backend/app/agents/tools_spec.py:513-531` |
| `check_plan` / `experience_check` | `backend/app/agents/tools_spec.py:491-510、573-580` |
| 无变化写也递增版本 | `backend/app/agents/tools_spec.py:180-199、365-375、408-411` |
| 重复查数据源 | `backend/app/agents/tools_spec.py:378-395、426-438` |
| 证据 UUID | `backend/app/agents/runtime.py:245-257` |
| `input_chars` 缺失 | `backend/app/llm.py:226-258` |
| 新连接/超时线程 | `backend/app/llm.py:44-68、174-179` |
| 事件 payload 重复 | `backend/app/event_stream.py:134-155` |
| peer 状态假消息 | `backend/app/event_stream.py:157-191` |
| tool parent_id 错误 | `backend/app/event_stream.py:263-295` |
| checkpoint 全量 pickle | `backend/app/checkpoint.py:136-161` |
| fast/expert/peer 预算 | `backend/app/graph/build.py:315-323、1040-1044` |
| peer_finalize 重复校验 | `backend/app/graph/build.py:1133-1191` |
| 证据计数 UI | `frontend/src/App.tsx:535-543` |
| 消息原样渲染 | `frontend/src/App.tsx:562`；`styles.css:583-591` |
| 角色颜色缺失 | `frontend/src/styles.css:542-546` |
| 任务板只在结果页 | `frontend/src/App.tsx:1555-1572` |

---

## 17. 其他代码级问题（不单独展开）

这些不是最大头，但会叠加“不可预测”和“返工”：

- **`route_after_peer_finalize` 是死分支**：`build.py:1209-1212` 无论 `approved` 是否等于 `plan_version` 都 `return END`，批准版本校验没有实际效果；版本不一致时也不会报错。
- **`enable_interrupt=False` 时失败静默**：`peer_finalize` 只有在 `enable_interrupt` 为真时才设置 `peer_question`（`build.py:1189`）；没有 checkpointer 时，硬校验失败可能直接走到 `END`，用户只看到“没有结果”。
- **`pending_issue_codes` 是死状态**：`hub.py:224` 初始化、`hub.py:529` 保存，但没有任何地方写入；无法支撑按 issue code 的接受/拒绝。
- **`bus` 的文档和状态是旧对等架构**：`bus.py:1-8` 还写“没有中心调度者”“wake 决定谁被唤醒”，与现在的集中式调度矛盾；`awake/sleeping/wake_log` 仍被序列化进 session，但 `hub` 从不使用。
- **`assign_pois` 与 `submit_day_plans` 并存**：前者逐日、后者批量；提示词说优先用后者，但两个都暴露，模型会选错；`_materialize` 的错误信息仍写“必须对每个日期调用 assign_pois”（`tools_spec.py:423`），与 `submit_day_plans` 的实际能力矛盾。
- **`irreplaceability` / `elder_fit` / `experience_check` 重叠**：都在回答“体验/人群风险”，返回不同投影；模型需要在三个工具里猜。
- **注释与实现漂移**：`build.py:1251` 仍写“真正的多 Agent 对等协作”，但实际是集中式；`bus.py` 同上。新同学读注释会建立错误模型。
- **`_validate_decision` 的 evidence 逃生口**：`say` 里只要出现“未核实/待确认/数据源没有/无法验证”之一，就可以不带 evidence（`runtime.py:164-169`）。这与 Writer 的“未核实”绕过同源：自然语言子串被当成协议开关。
- **`_summarize_result` 只取一个字段**：工具结果的事件摘要只挑 `count/passed/total_cost/...` 中的一个（`runtime.py:323-341`），UI 证据抽屉信息量不足。
- **`max_revisions` 的描述与实现不一致**：`settings.py:49` 描述“快速模式最多 1 次重排；专家模式最多 1 次自动返工”，但代码对 expert/peer 都用 `max_revisions`（默认 2）；配置项语义漂移。

---

## 19. 实施进度（更新于 2026-11-18）

> 维护约定：**每完成一个阶段，必须同步更新本节与 `.harness/STATE.md`**，否则下一轮会误判进度。
> 状态口径：**完成** = 已实现且有脚本回归；**部分** = 方向已落地但仍有明确缺口；**未开始** = 尚未改动。

### 19.1 逐项状态

| ID | 问题 | 状态 | 说明 / 提交 |
|---|---|---|---|
| A1 | 伪多 Agent：并行专业 Agent + 代码合并 | 部分 | 域隔离 + 合并仲裁 + 只读并发 + 代码预取 + 去主控纯转发；仲裁已改为**按域版本**（`transport/lodging/assignment/fixed_events_version`，任务可用 `writes` 声明领域），写不同领域的任务不再互判冲突。多任务真并发调度未做（前置：把 `materialize_plan` 移到代码侧） |
| A2 | 评审无 issue 身份/验收标准/能力映射 | 完成 | Issue Ledger + `acceptance` + `repair_tools`（`0c81cc7`、`b8253b1`） |
| A3 | 每回合全量注入 brief/bus/任务/收件 | 部分 | 收件与任务表已精简；bus 只给最近 6 条、单条截断 180 字；预取摘要限量（8 个景点 / 4 家酒店 / 各 3 个航班）。回合提示 **67,086 → 44,599 字符（−33.5%）**，全流程 82,612 → 72,125。上下文编译器仍未做 |
| B4 | 静态契约放在动态 user prompt | 完成 | 角色设定 + 协作契约移入 system（`Role.system_text`），user 提示只留动态内容，顺带改善前缀缓存命中 |
| A4 | 返工整任务重置，不能提交 diff | 完成 | 差异交付 `delivery_patch` + 按 issue 控制依赖传播（`0e7370e`、`31ba0ab`） |
| A5 | `plan_version` 无变化也递增 | 完成 | 同值写入不递增版本 |
| A6 | 预算/模式名不副实 | 完成 | 仅保留深度团队；未配置模型返回 400 |
| A7 | 证据是随机 UUID，不绑定结论内容 | 部分 | 服务端登记并校验归属；仍是随机 ID，未做内容哈希/字段路径 |
| A8 | UI 原样渲染长文；4 次工具显示 1 次 | 部分 | 证据按 Agent 挂载已修；结构化卡片与 diff 面板未做 |
| B1 | Critic 强制四工具、数据重叠 | 完成 | `ReviewPacket` 预计算，脚本请求 24 → 20 |
| B2 | `get_plan` 返回全量 itinerary | 完成 | 新增 `view=summary|full`，默认摘要只给逐日时间/项目/费用/固定事件；Writer 单次请求工具结果 4,475 → 2,889 字符。断言排程器内部字段的测试显式用 `view=full` |
| B3 | 历史按 12 条消息裁剪而非 token | 完成 | `_complete_history` 增加字符预算（默认 6000）并在每次请求前按预算裁剪上一回合；**实测收益≈0**：15 次请求的总输入 82,612 字符里提示占 81%、工具结果 12%、历史≈0，历史不是瓶颈 |
| B4 | 静态契约放在动态 user prompt | 完成 | 角色设定 + 协作契约移入 system（`Role.system_text`），user 提示只留动态内容，顺带改善前缀缓存命中 |
| B5 | 自由文本 JSON + 严格解析 | 完成 | 新增 `submit_decision` 工具（参数即决策对象，`kind/say` 必填），格式错误变成一次短工具结果可同回合修正；正文 JSON 作为不支持工具时的兼容路径保留。代价是提示 +1.4k 字符（契约说明与工具 schema） |
| B6 | `say` 无长度/结构约束 | 完成 | 1200 字硬上限 + 提示词 500 字要求 |
| B7 | 工具串行执行却提示"并行" | 完成 | 同一响应里的 `list_pois`/`get_weather`/`search_flights`/`search_hotels` 用 `asyncio.gather` 真并发（四者写不同字段且不推进版本）；其余工具仍串行 |
| B8 | `set_flights`/`materialize` 重复查数据源 | 完成 | 复用已查结果 |
| B9 | checkpoint 全量 pickle + 重复 payload | 完成 | 会话工件化：`histories`/`bus`/`trace`/`evidence`/`operations` 移出图状态，状态只留内容哈希引用（`app/artifacts.py`）。单份会话 **77.4 → 12.2 KB**（每快照省 65.2 KB）；工件缺失时明确报错；保留策略见 19.6（默认预览、需 `--apply`） |
| B10 | `input_chars` 未累加 | 完成 | 按完整请求体统计 |
| B11 | 测试把 24 次调用当 PASS | 完成 | 基线改为成本断言：当前脚本成功路径 **15 次请求 / 6 回合**（预取前 20、评审包前 24），并有返工/中断专属脚本 |
| C1 | 角色无颜色、无任务板、无 diff | 完成 | 五个角色有独立头像色；轨迹抽屉顶部加「团队进度」条（进行中/已出场/待命，按 `agent_message.actor` 判定）；任务表有最近变更列；问题清单卡片见 19.3 |
| C2 | Writer 自由生成 + 子串校验 | 部分 | 敏感断言（含"步行可达"）拦截；受控模板生成未做 |
| C3 | 单进程、无保留策略、密钥明文 | 未开始 | 产品化阶段处理 |

### 19.2 汇总

- 完全完成 **16** 项：A2、A4、A5、A6、B1、B2、B3、B4、B5、B6、B7、B8、B9、B10、B11、C1。
- 部分完成 **5** 项：A1、A3、A7、A8、C2。
- 未开始 **1** 项：C3（产品化：多进程锁、密钥明文、保留周期）。

### 19.3 计划外新增（原表未列）

- **返工传播策略**：按 issue code 决定是否波及下游任务，日期级问题只重开被点名任务。
- **Critic 证据规则修正**：Critic 无工具，`accept`/`objection` 以服务端 `ReviewPacket` 为证据，问题代码仍须存在于包内。
- **返工工具可执行性**：本地返工范围补入只读 `get_plan`，使模型能读到 `fixed_events[].event_id`。
- **返工闭环回归**：`verify_rework_resume.py` 覆盖「反对 → 派工 → 中断 → 恢复 → 受限修复 → 差异交付 → 关闭 → Writer」。
- **上下文瘦身**：收件消息与任务表改为精简投影，回合提示 −30.6%，Critic 提示 −44%。
- **检查点版本裁剪（已撤回）**：实测真实线程 14 份快照 6.64 MB，保留 3 份可降到 4.08 MB；但 LangGraph 的 pending writes 挂在被裁掉的快照上，`verify_peer_remediation` 的「中断→恢复」直接失败，因此**不采用**。要安全压缩体积，必须改为“会话引用外部工件存储”，而不是删快照。
- **会话工件化（B9 落地方式）**：新增 `app/artifacts.py` 内容寻址工件库；`hub._pack_session` 把 `histories`/`bus`/`trace` 与 `context.evidence`/`context.operations` 换成 `{artifact_id, bytes}` 引用，`_unpack_session` 在恢复时还原。实测单份会话 77.4 → 12.2 KB；工件缺失抛 `ArtifactMissing`，不会静默当成空会话；旧内联会话无需迁移。
- **界面结构化（C1 部分）**：后端结果新增 `issue_ledger`（裁剪字段，随 `team_deliverable` 落库，`GET /api/plan/{id}` 可回读）；差异交付在任务上留 `changes`（只记真正变化的日期/字段，最多 5 条）。前端新增「问题清单」卡片与任务表「最近变更」列。
- **上下文域隔离（A1 第一段）**：`hub._sandbox_context` 给专业 Agent 一份独立工作副本（证据/工具账本/只读缓存仍共享）；`_merge_sandbox` 按 `SANDBOX_FIELDS` 白名单回写，交通住宿改不了日程、本地体验改不了航班酒店，越域字段被忽略并记入 warnings。`verify_isolation.py` 覆盖独立副本、共享账本、双向越域拒绝与版本推进。
- **版本仲裁 + 只读并发（A1 第二段 / B7）**：工作副本记录 `base_version`，合并时若共享上下文版本已变则抛 `SandboxConflict`：不覆盖、不部分生效、任务退回 pending 并记 `sandbox_conflict` 事件。同一模型响应中的四个只读查询用 `asyncio.gather` 真并发（写不同字段、不推进版本），其余保持串行。
- **代码预取（A1 第三段·方案 C）**：任务一确定，代码并发拉取航班/酒店/景点/天气并注入紧凑摘要，专业 Agent 直接据此选择。实测脚本成功路径 **20 → 16 次请求**，全流程输入字符 **130,454 → 88,835（−31.9%）**；代价是回合提示从 18,837 增至 22,155（+17.6%），净收益为正。预取失败只记 `prefetch_failed`，角色仍可自行查询。
- **去掉主控纯转发（A1 第四段）**：所有专业任务交付后，代码直接生成 ReviewPacket 并提交给 Critic（`review_submitted` 事件），不再让主控为“转交评审”消耗一次模型回合；主控只在需要返工时再次出场。脚本成功路径 **16 → 15**，全流程输入字符 **88,835 → 82,736**；返工续跑段也从 3 次降到 2 次。
- **历史字符预算（B3）**：`_complete_history` 在 12 条消息之外增加 6000 字符预算，并在每次请求前裁剪上一回合。**这次先把假设测了再改**：15 次请求共 82,612 字符，其中本回合提示 67,086（81%）、工具结果 9,792（12%）、上一回合历史≈0——历史不是瓶颈，收益≈0。对照实验：把预算压到 3000 并连本回合步骤一起裁，脚本模型因“忘记已做过什么”把请求从 15 次涨到 50 次，故**不采用**回合内裁剪，只保留安全上限。
- **提示压缩（A3 续 / B4）**：契约移入 system；bus 只渲染最近 6 条且单条截断 180 字；预取摘要限量（景点 14→8、酒店 5→4、航班 4→3，去掉低区分度字段）。实测回合提示 **67,086 → 44,599（−33.5%）**，全流程 **82,612 → 72,125（−12.7%）**，请求数 15 不变。
- **B2 摘要视图**：`get_plan` 增加 `view=summary|full`，默认只返回逐日时间/项目/费用/固定事件；Writer 的工具结果从 4,475 降到 2,889 字符，全流程 72,125 → **70,641**。需要排程器内部字段（notes/meal_estimate）的测试显式传 `view=full`。
- **结构化决策（B5）**：新增 `submit_decision` 工具，参数就是决策对象；`_finalize_decision` 统一做协议/角色/证据校验。格式错误现在返回一次短工具结果（可同回合修正），而不是让正文 JSON 解析失败后整轮重试；正文 JSON 作为兼容路径保留。实测提示 +1.4k 字符（契约与工具 schema），请求数仍 15。**收益主要是鲁棒性**：脚本模型无法真实度量"首次就写对"的比例，因此不宣称成本下降。
- **界面收尾（C1）**：五个角色各自头像色；轨迹抽屉顶部加「团队进度」条，状态取自 `agent_message.actor`（不是图节点级 `agent_started`，后者只有 `peer_run`），显示进行中 / 已出场 / 待命。
- **按域版本（A1 前置）**：`ToolContext` 增加 `transport_version` / `lodging_version` / `assignment_version` / `fixed_events_version`，各自在对应写工具生效时递增；`plan_version` 仍是聚合版本。合并仲裁改为**按任务声明的领域**比较（`_task_domains`，任务可写 `writes: ["fixed_events_version"]`），只要所辖领域没被别人改过就允许合并；写不同领域的任务不再互判冲突。旧会话缺这些字段时按 0 恢复。

### 19.4 下一步

1. **A1 第三段（真并行）**：需要先改协议——把 `materialize_plan` 从本地体验 Agent 移到代码：两个并行任务只提交 `assignment` 与 `fixed_events`，两个都交付后由代码统一落定并校验。这会改变 `_delivery_valid`（不再要求 `itinerary_version == plan_version`）与提示词，属于需要先确认的协议变更。
2. **A7 / A8 / C2 收尾**：证据内容哈希、界面剩余卡片细节、Writer 受控模板。
3. **C3（产品化）**：多进程锁、密钥明文、工件与检查点保留周期。

### 19.6 工件保留策略（已完成）

`backend/scripts/collect_artifacts.py` 负责清理：默认**只预览**，`--apply` 才删除。规则是“未被任何检查点引用且超过保护期（默认 24h）”，引用集通过扫描检查点文件字节获得（LangGraph 状态是 msgpack 序列化，直接反序列化需要注册全部自定义类型）。`backend/scripts/verify_artifacts_gc.py` 覆盖引用扫描、过期删除、保护期保留、仍被引用不删。**未做自动调度**：宁可让运维显式执行，也不在启动时自动删用户数据。

### 19.5 验收边界

已运行的确定性自检见 README 第 7 节；**真实模型、浏览器、真实 token 账单均未验收**，脚本化 20 次请求只是基线，不代表真实成本。

旧的规则图夹具（`verify_graph.py`、`verify_negotiation*`、`eval_quality.py` 等）已不适用，需要重写或删除。

## 20. 一句话总结

**当前系统把“多 Agent”实现成了“5 个角色轮流读同一份全量状态、用自然语言互相说服”，而评审又没有可验收的 issue 契约；所以它必然又慢、又贵、又反复。** 改成“并行专业 Agent + 工件契约 + 增量评审包 + 确定性修复 + 结构化 issue 账本”，才能同时得到效率、准确性和可读性。
