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
                     ┌────┴─────┐
             未通过且未达上限    通过 / 达上限
                     ▼          ▼
                  planner      END
```

四个检索 agent 并行执行，任一失败只降级该路结果，不阻断整体；
`critic` 只判可行性、`planner` 只负责排程，职责分离让"为什么改了这一版"可追溯。

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
模型配置在页面右上角「设置」里填（见第 5 节）；不配也能用，走内置规则引擎。

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

## 5. 配置：全部在界面维护

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

## 6. 验收

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
| verify_settings | 11 组 24 项 | 通过 |
| verify_settings_ui | 7 组 10 项 | 通过 |

配置相关覆盖：默认值、修改回读、越界拒绝、密钥掩码、掩码回传不覆盖、
测试连接成功/失败、数据源缺地址报错、模型开启后景点来自模型、
模型故障回落并提示、`max_revisions` 立即生效、设置页保存后顶栏模式切换。

## 7. 已知限制

- **模型只负责「去哪」，不负责「怎么排」**。时间轴、时长上限、预算校验仍是确定性规则。
  让模型直接产出完整行程（含时段、费用）没有做，因为那样模型的一次幻觉就会变成一张不可执行的表。
- **LLM 链路是用假服务验证的**。正常响应、500 故障、非 JSON 输出、超时都覆盖了，但没接真实
  provider 跑过完整行程。真实连通性请用设置页的「测试连接」确认。
- **单日重排**：模型启用时由模型理解意见并重排该日（阅读"多看看人文、别太累"这类复合意见有效），
  未启用或模型失败时回落关键词匹配（"轻松/少"降景点数，"多/紧凑"加景点数）。
- **推理模型需要更大的 max_tokens**：`deepseek-flash` 这类模型会先把 token 花在 `reasoning_content`，
  `content` 才是答案。额度不足时会返回空 `content` 且 `finish_reason=length`，
  系统明确报"输出被截断"并降级，不会拿半截 JSON 去用。
- **数据默认全为 mock**，不含真实价格与余票。HTTP 适配器已写好但本机没有接口可测，标注为未验证。
- **密钥在库里是明文**，且没有登录与权限——知道地址就能改配置。仅适合本机演示。
- **分享链接是隐式公开的**：知道 `plan_id` 就能读。
- **无并发压力测试**，SQLite 单文件写入在演示规模下够用。

## 8. 如果继续做

1. 让模型参与单日重排的反馈理解与重排决策，替换现在的关键词匹配
2. 接真实数据源：按 `TravelSource` 协议实现，先用一个只读接口验证
3. 加 `interrupt()`：预算超支 20% 时暂停，让用户在「接受 / 降配 / 改日期」中选择
4. 评测集：把 11 条边界用例扩成带期望值的评分表，用于比较不同 prompt 与模型
