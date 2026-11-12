# 旅行规划多 Agent 编排助手

用 LangGraph 编排多个专职 agent，把一张结构化的出行需求变成可执行的按天行程，
并把编排过程实时展示出来；生成后支持对单日提意见做局部重排。

演示形态：本地一键启动，浏览器里填写需求 → 观察 agent 并行工作 → 拿到行程单。

---

## 1. 它是怎么工作的

```
                    clarify（需求归一化）
                          │
        ┌─────────┬───────┴────────┬──────────┐
        ▼         ▼                ▼          ▼
   transport   lodging            poi      weather      ← 同一 superstep 并行
        └─────────┴───────┬────────┴──────────┘
                          ▼
                     planner（排程）
                          │
                      critic（可行性校验）
        ┌─────────────────┼─────────────────────┐
   通过 / 达上限      超支 > 20%          未通过且未达上限
        ▼                 ▼                     ▼
       END         confirm（中断等人）        planner
                          │
              ┌───────────┼───────────┐
           accept     downgrade      abort
              ▼           ▼             ▼
             END       planner         END
```

四个检索 agent 并行执行，任一失败只降级该路结果，不阻断整体；
`critic` 只判可行性、`planner` 只负责排程，职责分离让"为什么改了这一版"可追溯。
预算超支超过 20% 时不替用户拍板，而是中断执行让用户在三个选项里选。

## 2. 目录

```
backend/
  app/
    models.py            领域模型（Pydantic，全部 agent 的输入输出契约）
    agents/              七个 agent：clarify / transport / lodging / poi / weather / planner / critic
    graph/               状态定义与图组装
    tools/               数据源协议 + mock 实现（换真实 API 只改这里）
    api/                 FastAPI 路由与 SQLite 持久化
    cli.py               命令行入口
  data/mock/             航班 / 酒店 / 景点 / 天气
  scripts/               五个验收脚本
frontend/
  src/App.tsx            表单、编排时间线、行程单、单日重排
```

## 3. 快速开始

### 3.1 准备

```bash
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install langgraph pydantic fastapi "uvicorn[standard]" playwright
```

### 3.2 生产模式（单端口，推荐演示时用）

```bash
cd frontend && npm install && npm run build
cd ../backend
set PYTHONPATH=%cd%          # PowerShell: $env:PYTHONPATH=(Get-Location).Path
.venv/Scripts/python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8848
```

打开 <http://127.0.0.1:8848/> 即可。UI 与 API 同端口，无跨域问题。
模型配置在页面右上角「设置」里填（见第 6 节）；不配也能用，走内置规则引擎。

### 3.3 开发模式（前端热更新）

```bash
# 终端 1：后端
cd backend && .venv/Scripts/python.exe -m uvicorn app.api.main:app --port 8848

# 终端 2：前端（默认 5180，可在 vite.config.ts 改）
cd frontend && npm run dev
```

> 端口 5173/5180 在本机可能被其他服务占用，`vite.config.ts` 里的 `strictPort: true`
> 会让冲突直接报错而不是静默换端口。

### 3.4 命令行（不走 UI）

```bash
cd backend
$env:PYTHONPATH=(Get-Location).Path
.venv/Scripts/python.exe -m app.cli --origin 上海 --destination 成都 \
  --start 2026-11-20 --end 2026-11-24 --travelers 2 --budget 8000 --elder
```

## 4. 关键设计决策

### 4.1 并行写入不冲突

四个检索节点只写各自的 state 字段，唯一需要 reducer 合并的是 `errors`
（`Annotated[list[str], operator.add]`）。共用字段会在运行时直接冲突。

### 4.2 循环上限用 state 计数

`revision` 记录 critic 执行次数，条件边里判断 `revision >= 3`（即最多 2 次重排），
不依赖图递归深度，避免死循环烧钱。达到上限后强制放行，未解决的 blocker 保留在 `critique` 里。

### 4.3 失败与"查无结果"必须区分

- 数据源不可用 → agent 抛 `SourceError` → 转成 `warnings` 文案，行程照常生成
- 线路确实没航班 → 返回空列表 → 同样给明确提示，但性质不同
- agent 未预期异常 → 写入 `errors`，图不崩溃

三种情况在前端分别呈现，不会静默省略。

### 4.4 单日重排冻结其余各天

`POST /api/plan/{id}/refine` 把目标日之外的 `DayPlan` 原样保留（包括酒店选择），
只对目标日重跑 `planner → critic`，并把其余天已占用的景点登记为已用，避免撞车。

### 4.5 mock 数据的三个刻意的坑

- 成都博物馆、四川博物院、金沙遗址博物馆周一闭馆 → 检验闭馆日规避
- 天气模板含 2 个雨天 → 检验室内优先与雨天提醒
- 都江堰/青城山带"老人不宜"标签 → 检验人群过滤

### 4.6 时间轴也要校验，不能只校验"排了多少"

`critic` 除了预算、时长、闭馆日和人群，还检查这张表**能不能照着走**：

| 检查 | 代码 | 严重度 |
|---|---|---|
| 相邻活动重叠（含航班占用时段） | `TIME_OVERLAP` | blocker |
| 景点被排在开放时间之外 | `OUTSIDE_OPEN_HOURS` | blocker |
| 返程日错过赶飞机的时间 | `MISSED_AIRPORT_DEADLINE` | blocker |
| 长行程缺午餐/晚餐 | `MISSING_LUNCH` / `MISSING_DINNER` | warning |

这条规则抓出过一个真实缺陷：14 天行程里把都江堰排在 16:30-20:30，
而它 18:00 关门。根因是 `_place_poi` 只查开始时间不查结束时间。
`verify_timeline` 里同时做了正反两侧验证——真实行程必须自洽，
人为构造的违规必须被抓到。

## 5. 执行轨迹（右侧抽屉，流式）

点顶栏「执行轨迹」打开右侧面板。各 agent 的讨论与工具调用**边执行边出现**，
不是跑完才一次性给出——后端每产出一条轨迹就通过 SSE 推一条。

```
▼ 并行检索                                        18:04:46
    住宿
      ⚙ search_hotels(成都 距市中心≤12km) → 5 家
      · 推荐 春熙路亚朵酒店（520 元/晚，距市中心 0.5 km）
    景点
      ⚙ list_pois(成都) → 19 个
      · 19 个可用景点
      ! 排除 都江堰景区（老人同行不适宜）、青城山前山（老人同行不适宜）
    交通
      ⚙ search_flights(上海→成都 2026-11-20) → 4 条
      ⚙ search_flights(成都→上海 2026-11-24) → 3 条
      · 选中 MU5401 / CA4515，往返 2030 元

▼ 预算与体验协商                                  18:05:12
    预算官
      “ 优先砍高价景点且保证每天仍有安排…
      ⚙ cuttable_scan → 已执行
      ! 拟砍掉：成都大熊猫繁育研究基地
    体验官
      “ 熊猫基地不可替代性 0.676 且适合老人，砍掉后当天仅剩 0.211
      · 被砍项目不可替代性：0.68
    调解者
      “ 折中：砍掉 1 个可替代性较低的项目
```

层级由 `(group, depth)` 决定：

| depth | 含义 | 例子 |
|---|---|---|
| 0 | 阶段块标题（点击折叠） | 并行检索 |
| 1 | agent 的动作或主张 | 选中 MU5401 / CA4515 |
| 2 | 工具调用、证据、约束 | `search_flights(上海→成都 11-20) → 4 条` |

记录原则：只写具体事实（选中哪个航班、砍掉哪个景点、超了多少），
不写「执行中/已完成」。轨迹随结果与中断一并下发，也落进 SQLite，分享链接回放时仍在。

## 6. 配置：全部在界面维护

右上角「设置」打开，保存后立即生效。**不读环境变量、不需要 `.env` 文件、不用重启。**

| 分组 | 配置项 | 说明 |
|---|---|---|
| 模型连接 | 启用开关、base_url、model、api_key、temperature | OpenAI 兼容协议，DeepSeek / OpenAI / Kimi 只换 base_url |
| 编排参数 | 最大校验轮次、节点超时、token 预算 | 校验轮次设为 3 表示最多重排 2 次 |
| 数据源 | mock / HTTP | HTTP 适配器已实现，本机无真实接口，未实测 |

存储与安全约定：

- 配置存后端 SQLite（`backend/data/plans.db` 的 `app_config` 表），不依赖浏览器缓存，换浏览器仍在。
- `api_key` **永不回传明文**：读取接口只给 `sk-u****7890`；前端原样回传掩码时，后端保留库里的真实值。
- **「测试连接」不要求勾选启用模型**：先测通再决定是否启用。缺字段、域名解析失败、输出被截断
  会分别给出不同提示。
- 验收脚本检测到已配置真实密钥时，会自动跳过会覆写密钥的用例并明确打印，不会破坏你的配置。
- 密钥在库里是明文（本地演示项目，未加密）。**不要填生产密钥。** 日志与错误信息不打印密钥。
- 本轮没有登录和权限：能打开这个服务的人就能改配置，公网部署前必须加鉴权。

模型启用后的分工：

- 模型只决定「每天去哪些景点」，输出经 Pydantic 校验，未知 id 与闭馆景点会被过滤掉；
- 时间轴、单日时长上限、预算校验仍由规则层执行，模型给出的不可行方案不会产出不可行行程；
- 模型超时 / 报错 / 输出非 JSON 时自动回落规则引擎，并在行程 `warnings` 中写明「本次行程由规则引擎生成」。

## 7. 验收

五个脚本都不依赖测试框架，直接运行，失败即非零退出：

```bash
cd backend
$env:PYTHONPATH=(Get-Location).Path

.venv/Scripts/python.exe scripts/verify_langgraph.py    # 图行为：并行 / 条件回边 / astream
.venv/Scripts/python.exe scripts/verify_transport.py    # 数据层 / 模型校验 / 单 agent
.venv/Scripts/python.exe scripts/verify_graph.py        # 完整图：正常 / 回退 / 降级 / 闭馆
.venv/Scripts/python.exe scripts/verify_edge_cases.py   # 11 条边界用例
.venv/Scripts/python.exe scripts/verify_api.py          # REST + SSE（需先起服务）
.venv/Scripts/python.exe scripts/verify_ui.py           # 浏览器交互（需先起服务）
.venv/Scripts/python.exe scripts/verify_settings.py     # 配置层 + LLM 接入（需先起服务）
.venv/Scripts/python.exe scripts/verify_settings_ui.py  # 设置页交互（需先起服务）
.venv/Scripts/python.exe scripts/verify_interrupt.py    # 人工介入：accept / downgrade / abort（需先起服务）
.venv/Scripts/python.exe scripts/verify_interrupt_ui.py # 中断面板交互（需先起服务）
.venv/Scripts/python.exe scripts/verify_negotiation.py  # 协商轨迹结构（需先起服务）
.venv/Scripts/python.exe scripts/verify_negotiation_ui.py # 协商轨迹展示（需先起服务）
.venv/Scripts/python.exe scripts/verify_mediation.py    # 协商裁决逻辑（纯单元，不需服务）
.venv/Scripts/python.exe scripts/verify_trace_drawer.py # 轨迹抽屉与流式输出（需先起服务）
.venv/Scripts/python.exe scripts/verify_collaboration.py # 检索 agent 按需协作（纯单元，不需服务）
.venv/Scripts/python.exe scripts/verify_timeline.py     # 时间轴与硬约束（正反双向，不需服务）
```

`verify_settings*` 用内置的假 OpenAI 兼容服务验证链路，不需要真实 key。

当前状态（2026-11-11 实测）：

| 脚本 | 覆盖 | 结果 |
|---|---|---|
| verify_langgraph | 3 项行为断言 | 通过 |
| verify_transport | 13 项 | 通过 |
| verify_graph | 6 组 17 项 | 通过 |
| verify_edge_cases | 11 条用例 × 5 项不变量 | 通过 |
| verify_api | 5 组 16 项 | 通过 |
| verify_ui | 4 组浏览器交互 | 通过 |
| verify_settings | 11 组 24 项 | 通过（有真实密钥时自动跳过覆写用例） |
| verify_settings_ui | 7 组 10 项 | 通过（同上，只读模式） |
| verify_interrupt | 3 组 18 项 | 通过 |
| verify_interrupt_ui | 3 组 9 项 | 通过 |
| verify_negotiation | 5 组 20 项 | 通过 |
| verify_negotiation_ui | 3 组 8 项 | 通过 |
| verify_mediation | 7 组 18 项 | 通过 |
| verify_trace_drawer | 6 组 16 项 | 通过 |
| verify_collaboration | 5 组 13 项 | 通过 |
| verify_timeline | 4 组 17 项 | 通过 |

配置相关覆盖：默认值、修改回读、越界拒绝、密钥掩码、掩码回传不覆盖、
测试连接成功/失败、数据源缺地址报错、模型开启后景点来自模型、
模型故障回落并提示、`max_revisions` 立即生效、设置页保存后顶栏模式切换。

人工介入覆盖：超支 >20% 触发中断而非直接出结果、三个选项各自的结果、
决策被记录进 warnings、无待确认中断时 resume 返回 400、降配后费用确实下降。

协商覆盖：三条发言的角色与顺序、每条主张都带工具计算的证据、
预算严重超支才触发协商（小额超支不打扰用户）、协商无进展时仍升级给用户、
被砍景点不进最终行程、轨迹可持久化并回放。

裁决覆盖（纯单元，不依赖模型）：体验官让步即采纳、全高价值项目时僵持升级、
存在低价值项时折中且只砍低价值、用户已表态的最后一轮必须给方案、
用户偏好含"省钱"时优先预算、被驳回项不会原样重提。

## 7.1 检索 agent 的按需协作

四个检索 agent 并行跑完后，协调者校对一次结果之间有没有对不上的地方。
**只在发现真问题时**才发起一次请求，没有就直���往下走——不为了"看起来在协作"而空转。

真实存在的问题：住宿 agent 只看"距市中心多近"，看不到用户实际要去哪。
成都的景点偏西（宽窄巷子、杜甫草堂、武侯祠），酒店却在东边春熙路，平均 3.8km。

```
▼ 检索结果校对
住宿  “ → 景点：我选的 春熙路亚朵酒店 到这些景点平均 3.8 km，太远了。请给出它们的坐标，我重选酒店
景点  ✓ ← 住宿：收到，提供 10 个景点的坐标
      ! 住宿的位置和行程对不上：春熙路亚朵酒店 离你常去的几个景点平均 3.8 km
住宿  “ 按景点位置重选为 锦江宾馆
```

结果：锦江宾馆 620 元/晚，到目标景点平均 2.7km（原 3.8km）。
多花 100 元/晚，换来每天少打车——对带老人的行程是划算的。

实现约束：

- 协调者的判断是**纯规则**（只比距离），不花 LLM 调用
- 协作**上限一轮**：`coordinator → lodging_fix → planner`，回路里没有其它检索节点，构造上不可能循环
- 重选时位置优先：先圈出离目标景点最近的 3 家，再在里面挑性价比最高的。
  只调权重是不够的——权重差几百分之一结果不会变，协作就成了走过场

## 8. 协商细节预算严重超支（>20%）时，不是让一个模型自己决定怎么省，而是两个**有明确立场**的角色各说各话：

```
预算官（只对钱负责）   → 提议砍掉 X、换便宜酒店，可省 N 元
体验官（只对值不值负责）→ 用不可替代性评分判断 X 该不该保
调解者（规则裁决）     → 达成方案 / 折中 / 僵持
```

关键在于**每个角色的判断都来自自己的计算工具**，不是同一份数据换一句 prompt：

| 角色 | 工具 | 输出 |
|---|---|---|
| 预算官 | `day_cost_breakdown`、可砍清单扫描 | 每天钱花在哪、砍掉能省多少 |
| 体验官 | `poi_irreplaceability`、`day_experience` | 每个景点在同城有多难被替代（0-1） |
| 调解者 | 规则 | 按体验损失与用户偏好裁决 |

不可替代性由同类稀缺度、时长、门票、室内外配比加权得出，纯规则、可解释。
所以体验官能说"熊猫基地 0.676，砍掉后当天只剩 0.211"，而不是"我觉得可惜"。

### 僵持时会回来问你，然后继续谈

```
第 1 轮：预算官提议 → 体验官反对 → 调解者判定僵持
   ↓ 暂停，问你倾向（体验优先 / 预算优先 / 先换酒店再谈）
第 2 轮：预算官带着你的倾向重新提议 → 体验官再评 → 调解者定案
```

第 2 轮是**强制出方案**的：用户已经表态，不能再往返。此时哪怕要牺牲体验，
也会明确写出牺牲了什么，而不是继续把球踢回来。

多轮上限刻意设成 2 轮——在"预算 vs 体验"这个二维冲突上，第三轮不会产生新信息。

## 9. 已知限制

- **模型只负责「去哪」，不负责「怎么排」**。时间轴、时长上限、预算校验仍是确定性规则。
  让模型直接产出完整行程（含时段、费用）没有做，因为那样模型的一次幻觉就会变成一张不可执行的表。
- **LLM 链路是用假服务验证的**。正常响应、500 故障、非 JSON 输出、超时都覆盖了，但没接真实
  provider 跑过完整行程。真实连通性请用设置页的「测试连接」确认。
- **单日重排**：模型启用时由模型理解意见并重排该日（阅读"多看看人文、别太累"这类复合意见有效），
  未启用或模型失败时回落关键词匹配（"轻松/少"降景点数，"多/紧凑"加景点数）。
- **推理模型需要更大的 max_tokens**：`deepseek-flash` 这类模型会先把 token 花在 `reasoning_content`，
  `content` 才是答案。额度不足时会返回空 `content` 且 `finish_reason=length`，
  系统明确报"输出被截断"并降级，不会拿半截 JSON 去用。
- **中断用的 checkpointer 是进程内的**（`MemorySaver`）：服务重启后，尚未确认的中断会失效，
  需要重新规划。已完成的行程存在 SQLite 里，不受影响。
- **超支阈值 20% 是代码常量**，暂未做成界面配置项。
- **数据默认全为 mock**，不含真实价格与余票。HTTP 适配器已写好但本机没有接口可测，标注为未验证。
- **密钥在库里是明文**，且没有登录与权限——知道地址就能改配置。仅适合本机演示。
- **分享链接是隐式公开的**：知道 `plan_id` 就能读。
- **无并发压力测试**，SQLite 单文件写入在演示规模下够用。

## 10. 如果继续做

1. 让模型参与单日重排的反馈理解与重排决策，替换现在的关键词匹配
2. 接真实数据源：按 `TravelSource` 协议实现，先用一个只读接口验证
3. 加 `interrupt()`：预算超支 20% 时暂停，让用户在「接受 / 降配 / 改日期」中选择
4. 评测集：把 11 条边界用例扩成带期望值的评分表，用于比较不同 prompt 与模型
