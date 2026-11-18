# 执行日志

## 2026-11-16

- 审计最近真实运行：`e2cee0918366` 运行 767.4 秒、251 事件，最终中断；`8e6e83445025` 运行 386 秒、174 事件，最终失败。
- 识别主要根因：默认 peer、每个工具调用都需要模型回合、协议退回整轮重跑、上下文膨胀、Critic 可提出不可执行返工、复审计数器分裂。
- 完成第一阶段整改并提交 `675c72b`：默认 fast、运行硬上限、上下文裁剪、时间线修正、失败/中断持久化、固定事件、前端确认面板和 README 同步。
- 完成第二阶段整改并提交 `c5a6f3a`：最小快速图、共享 LLM 调用计数、usage/字符/延迟统计、结构化确认请求、前端用量展示、README 更新。
- 迁移实际 SQLite 配置：旧 `agent_mode=peer` 改为 `fast`，补齐运行限制字段。
- 最终验证：后端 compileall 通过；前端 `npm.cmd run build` 通过；快速图确定性运行通过；调用上限测试通过；服务 `http://127.0.0.1:8848` 健康检查通过。
- 最后一次构建时根目录执行 npm 曾因无 `package.json` 报错，随后在 `frontend` 正确目录重新构建并通过；这不是代码构建失败。
- 当前未调用真实模型，未提交密钥、数据库、日志、截图和无关媒体。
- 完成剩余优化：fast 图独立 `fast_planner` 不产生 proposal/message；API 统一失败、中断和完成路径的 `llm_stats`；异常运行细分 `failed_non_resumable`、`checkpoint_lost`；前端失败/中断面板显示用量并识别恢复状态。
- 验证通过：后端 compileall、前端 `npm.cmd run build`、`git diff --check`、`verify_event_contract.py`，以及显式 fast 图 mock 执行（无 proposal/message 且产出 draft/critique）。
- `verify_agent_collaboration.py` 未作为本轮通过项：其默认配置仍按旧 expert proposal 断言，且调用路径错误；本轮未修改该旧验收脚本，也未将其失败误报为 fast 实现失败。
- 本轮将有效模型下三种模式统一接入现有 `hub.run_session`，增加关键 Agent 结论 evidence 校验，协作上限/事实不足时由 Agent 升级为用户待确认状态；README 和前端确认文案已同步。
- 后端 compileall、前端 build、事件契约、三模式 Agent 接线和 evidence 协议验证通过；`stop/continue` 恢复语义已补齐。
- 旧 `verify_peer_agents.py` 仍因废弃角色脚本失败，已确认不代表当前实现失败，后续需迁移为 orchestrator/logistics/local_expert/critic_agent/writer 协议测试。
- 2026-11-17 peer 整改：新增 `docs/peer-agent-remediation-plan.md`，定义集中式 DAG 调度语义与五阶段整改/验收计划；修复 `_run_graph` snapshot 未初始化的中断崩溃，异常保存类型/消息/服务端 traceback 并打日志；`chat_tools` 区分截断、reasoning-only、空响应，Agent 回合对瞬时/空响应最多重试一次，失败后按真实类型升级；任务校验报错细分；硬 blocker 在复审上限后只允许停止或调整需求重开；stop 返回 `run_needs_input` 终态；peer 模型请求上限默认值改为 40、最大 80，Agent 回合独立计数且不自动提高已保存配置；工具成功结果生成服务端 `evidence_id`，关键结论引用必须属于本会话证据目录；Writer 增加有限敏感断言扫描；checkpoint 加实例锁、唯一临时文件并对异步 IO 使用 `asyncio.to_thread`；前端/README 更新运行预算和集中调度说明。
- 新增 `backend/scripts/verify_peer_remediation.py`；迁移 `verify_peer_runtime.py` 到当前角色协议。整改脚本验证空响应重试、evidence 伪造拒绝/真实引用通过、Agent 失败升级、stop 终态、任务协议、Writer 扫描、checkpoint 并发和 API `run_interrupted → run_needs_input`，全项通过。
- 验证：backend compileall、frontend `npm.cmd run build`、`verify_event_contract.py`、`verify_peer_runtime.py`、`verify_peer_remediation.py`、`git diff --check` 均通过。Checkpoint 加载仍打印 LangGraph 自定义类型未注册 warning；没有真实模型调用。
- 尚未完成：预算 blocker 全自动返工后成功的五角色脚本化完整会话；跨中断累计调用/时间账本；真正回合级 checkpoint（目前仍是 peer 节点边界）；checkpoint 增量/按线程存储与长期保留/清理；通用自然语言事实验证（Writer 目前是有限敏感断言扫描）；重启实际服务并在浏览器验证新产物。
- 后续修复：checkpoint 异步写入改为 `asyncio.to_thread` 执行完整“内存更新+原子快照”事务，使用跨调用的 `RLock`，移除跨事件循环锁；修复 Windows 并发替换 `PermissionError` 与 TestClient 多 loop `RuntimeError`。新增 `ModelCallBudgetExceeded` 和 `AgentTurn.error_type`，hub 按类型识别预算耗尽；禁用后端 `accept_risk` 绕过入口；复审上限只允许 restart/stop；Settings 露出 max_agent_turns；peer tool_result 透传 evidence_id；前端按中断类型显示实际原因。
- 最终验证通过：`verify_peer_remediation.py`（空响应、超时、预算异常、evidence、Writer、checkpoint 线程并发与真实 `aput_writes` 异步并发、API 中断→恢复→stop）；`verify_peer_runtime.py`；`verify_event_contract.py`；backend compileall；frontend `npm.cmd run build`；`git diff --check`。仍有 LangGraph 未注册自定义类型 warning，未调用真实模型/API。
- 追加修复：peer session 持久化 `pending_issue_codes`；checkpoint 异步写入统一为线程事务，避免跨事件循环锁；Settings 可编辑 `max_agent_turns`；tool_result 事件透传 evidence_id；后端禁用 accept_risk 绕过路径。
- checkpoint 改按 thread_id 分片：旧 `checkpoints.pkl` 只读兼容，新写入落到 `checkpoints.pkl.d/*.pkl`，只序列化当前线程；删除写 tombstone；已验证旧文件 405 线程可读、主文件大小不变、并发 `aput_writes` 安全、分片可重载与 reset 清理。
- 新增 `verify_peer_success.py`，脚本化五角色跑通完整成功路径：24 次调用（orchestrator 2 / logistics 5 / local_expert 10 / critic 5 / writer 2）、6 个 Agent 回合、总价 5700/8000、产出批准文档；并验证评审上限后升级为用户 restart/stop。
- 该测试发现并修复两个真实缺陷：1) 主控无工具却必须提供证据——改为专业交付把 evidence_ids 随消息和任务状态转交主控引用；2) 中断恢复后主控看不到待返工问题——改为在提示中显式渲染 `待返工问题`。
- 2026-11-17 文档同步：README 补齐事件类型（含 `run_needs_input` 等）、终态载荷、设置项（模型请求数/Agent 回合数/输出上限/上限 80）、`max_tokens` 实际行为、验收状态与证据；补充证据引用/转交、恢复待返工、升级类型与 `accept_risk` 禁用、`GET /api/run` 状态、检查点分片与恢复段预算限制。整改计划补“执行状态”表。STATE 更新重启 PID、前端资源 `index-Dvt-jbjM.js`、已保存配置 8/10 与新增回归结果。
- 2026-11-17 方案 B 整改：模型请求不再发送 `max_tokens`，`max_output_tokens` 标记废弃并在 `settings.load/merge_incoming` 迁移为 `None`（旧库 1400 不再生效）；保留请求数、Agent 回合、单回合工具步数、复审轮次、总时长边界；用量继续按供应商 usage/字符数记录。新增 `app/cancellation.py`（threading.Event 协作式取消）与 `POST /api/plan/{id}/cancel`；`LLMClient` 在请求前后检查取消，`_run_graph` 在节点边界检查并返回 `run_needs_input`（`recovery_status=cancelled`、DB 状态 `cancelled`）；前端运行区新增“取消本次运行”，刷新可恢复 cancelled 终态。
- 新增 `backend/scripts/verify_budget_cancel.py`（29 项）覆盖：不发送 max_tokens、旧配置迁移、取消登记、取消后不发请求、流中途取消、取消接口 404/200、build 接受取消标记。
- 数据审计：实测成都老人同行候选 19→22，5 天排 10 个景点不变，7 天 16 个，10 天以上候选耗尽。联网核对 3 个场馆后新增 p22 成都市天府美术馆、p23 四川大学博物馆（周末闭馆、30 元）、p24 浣花溪公园；无法核实开放时间的成都自然博物馆未写入。新增 `backend/data/mock/SOURCES.md` 记录来源、核对日期、近似字段与容量缺口；README 4.5 与限制章节同步。
- 验证：backend compileall、frontend `npm.cmd run build`、`verify_budget_cancel.py`、`verify_peer_success.py`、`verify_peer_remediation.py`、`verify_peer_runtime.py`、`verify_event_contract.py`、`verify_timeline.py` 通过；`verify_graph.py` 仅剩 1 条**改动前已存在**的规则图断言失败（“达到轮次上限后放行 revision=1”，已在 HEAD 干净 worktree 复现，与本次改动无关）。
- 未做：未重启 :8848 现有服务（仍是 12:41 启动的旧进程，需重启才加载新代码）；未在真实模型上验证取消时延与长输出；未扩充到能排满 14 天。
- 2026-11-17 用户复测反馈“看不到终止按钮、提过一次上限仍跑不完”。核查结论：1) 取消按钮已进构建产物，用户页面是重启前的旧 JS（未刷新）；2) 数据库 `max_model_calls` 实际仍是 8，最新运行 `f626e4f842d1` 按 8 触顶；用最小载荷 PUT 可正常保存，说明此前“提高上限”没有真正提交或被校验拒绝；3) peer 脚本化成功路径需 24 次请求，8 结构性不足。
- 追加修复：`ResumeRequest` 新增 `max_model_calls`（1–80，仅升不降），恢复时写回设置；额度耗尽升级新增 `max_calls`/`suggested_max_calls`（40 或当前 3 倍、上限 80）；前端把建议值渲染为“提高到 40 次并继续”一键提交；顶栏运行中新增“停止运行”，`cancelPlan` 用 URL plan 兜底；`verify_budget_cancel.py` 增加恢复提额（400 不改配置、422 越界、PUT 持久化）与升级字段断言。
- 运行中配置已改为 `max_model_calls=40`；服务 15:08 重启（PID 25800，前端 `index-DOQ5OSvE.js`）。本轮改动（hub/main/App/verify/STATE/WORK）尚未提交。
- 2026-11-17 深度团队整改执行（P0/P1/P2/P4 切片）：
  - runtime：新增 `_complete_history`（按 assistant tool_calls + tool 结果事务裁剪，不再裸切 12 条）与 `_validate_history_messages`（请求前拒绝孤立 tool，返回 `HistoryCorrupt`）。
  - 账本：`LLMClient(initial_stats)`、`build(initial_stats)`；resume 从 `store.load_run` 带入上次 calls/字符/token/耗时，预算按累计值判断。
  - 工具：`FixedEvent.event_id`；`replace_fixed_event`/`remove_fixed_event`；`ctx.operations` 操作账本防重放；只读工具同参缓存；`REPAIR_HINTS` 随 issue 返回。
  - 校验/终态：返程日多条送机 → `AIRPORT_TRANSFER_DUPLICATE`；`peer_stopped` 载荷补 blockers/issue_codes/deliverable=false。
  - 排程：固定事件含午餐时不再报“没有午餐时间窗”并计入餐费；返程日按早餐+午餐估算，不再为 0。
  - 提示词：logistics/local_expert 要求同回合批量只读调用，local_expert 明确用 replace/remove 修复固定事件。
- 新增 `backend/scripts/verify_peer_deep_team.py`（17 项）；`verify_budget_cancel.py` 增加 [H] 历史与固定事件 6 项。全部通过：deep_team、peer_success（24 次/5880）、remediation、runtime、event_contract、timeline、edge_cases、compileall、frontend build。`verify_graph.py` 仍只剩改动前既有的 revision 断言失败。
- 服务 17:40 重启（PID 7928，配置 40/null，资源 `index-DOQ5OSvE.js`）。本轮改动尚未提交；真实道路/步行数据与完整确定性工具 DAG 未实现，checkpoint 清理策略仍缺。
- 2026-11-17 深度团队整改续作（Round 6）：
  - 新增 `submit_day_plans` 声明式批量提交：闭馆/未知 ID/重复 ID/越界日期全部拒绝，`expected_version` 冲突检测，全量校验后原子应用且只递增一次版本，返回 before/after 差异；local_expert 提示词改为优先使用。
  - 路线能力诚实化：`route_distances` 返回 `mode=straight_line`、`road_data_available=false` 并在 limitation 中禁止“相邻/步行可达”；Writer 敏感断言新增“步行可达/步行即到”。
  - 恢复账本端到端：`_prior_run_stats(plan_id)` 从上次 run 读取累计用量并在 resume 传入 `build(initial_stats=...)`；新增 API 级测试断言恢复图收到 calls=18、total_tokens=255305。
  - `verify_peer_deep_team.py` 扩到 6 组 26 项；全部通过。其余回归：peer_success（24 次/5880）、remediation、runtime、event_contract、timeline、edge_cases、budget_cancel、compileall、frontend build 通过；`verify_graph.py` 仅剩既有 revision 断言失败。
  - 服务 18:05 重启（PID 2152，配置 40/null，资源 `index-DOQ5OSvE.js`）。本轮改动仍未提交。

- 2026-11-17 单模式改造：移除 fast/expert 入口与 build 分支；TripBrief.planning_mode / OrchestrationSettings.agent_mode 删除；未配置模型时 /api/plan、/resume、/refine 返回 400，build 也硬校验。修复 chat_tools 输入字符统计、无变化写不递增版本、set_flights/materialize 重复查数据源、say 1200 字上限、额度 80 死循环、前端证据按 Agent 挂载。自检：compileall、frontend build、verify_peer_success（24 次）、peer_deep_team、peer_remediation、peer_runtime、event_contract、budget_cancel、单模式 smoke 全部通过。旧规则图夹具（verify_graph/verify_negotiation*/eval_quality）已不适用。下一阶段：ReviewPacket + Issue Ledger + 并行专业 Agent。

- 2026-11-18：已提交 0c81cc7（评审包/账本）、b8253b1（修复工具映射）、0e7370e（返工传播）。本轮实现任务 delivery_base 与版本绑定 delivery_patch：按日期/交通住宿字段合并，完整工具事实复核通过后更新交付并清除基线。新增 verify_delivery_patch，覆盖真实工具修改、旧版本/重复日期/未知字段拒绝、持久化基线恢复后评审与 Writer；该脚本、peer_success、peer_runtime、compileall 通过。新阶段尚未提交；不声称真实 token 收益或完整 Critic 发起返工链路已验收。阶段完成后提醒用户 commit，不自动提交。
- 2026-11-18 提示上下文瘦身（未提交）：新增 `.harness/measure_sections.py` 分段测量。发现「本回合收件」把整份消息 payload（含完整 ReviewPacket 与任务表）原样写入提示，且评审包随后又单独注入一次。改为 `_compact_message` / `_compact_payload` / `_task_view`：收件只保留 sender/kind/content(≤300) 与版本、evidence_ids、changes、issue_ledger、任务摘要；任务表只保留 id/owner/status/depends_on/交付摘要/基线版本。实测脚本化 happy path：6 个回合提示合计 27,131→18,837 字符（−30.6%），Critic 提示 8,945→5,001（−44%），全流程 20 次请求输入字符 145,765→130,454（−10.5%）；请求数仍为 20。压缩过程中发现并修复：精简任务视图丢了 `delivery_base.version`，导致返工无法提交差异交付——已只补版本号。回归：peer_success、rework_resume、delivery_patch、peer_remediation、peer_runtime、peer_deep_team、event_contract、compileall 全部通过。未做真实模型/浏览器验收，未测真实 token 账单；checkpoint 体积未改。
- 2026-11-18 返工闭环集成验收：新增 `verify_rework_resume.py`，跑通「Critic 反对 → 主控按 issue 派回 T2 → 返工回合中断 → resume 继续 → 受限工具删除重复送机 → materialize/check → delivery_patch → 复审关闭 issue → Writer」，恢复段未重跑 logistics（3 次脚本请求）。过程中修复两个真实缺陷：1) Critic 无工具却必须提供 evidence，objection 每次被协议拒绝直到 32 步上限——改为 Critic 的 accept/objection 以服务端 ReviewPacket 为证据（问题代码仍须存在于包内并由 make_issue_ledger 复核）；2) 返工工具集缺少读取 event_id 的手段，真实模型无法执行删除——本地返工范围补入只读 `get_plan`（与 materialize/check 一起）。回归：peer_success、peer_runtime、delivery_patch、rework_resume、peer_remediation、peer_deep_team、event_contract、compileall、git diff --check 全部通过。仍未做：真实模型/浏览器验收、token 实测、并行 Agent、checkpoint 瘦身。
