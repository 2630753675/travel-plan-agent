# 当前任务状态

## 目标
继续完善旅行规划助手：只保留深度团队单模式；降低模型调用、耗时和 token；统一失败/中断/用户确认；同步文档并完成自检。

## 约束
- 默认中文沟通。
- 不调用真实模型/API 验收，用户自行测试。
- 不提交密钥、数据库、运行日志、截图或无关媒体。
- 不执行破坏性 Git 操作，不 push。
- 合成旅行数据允许继续使用。

## 当前状态
- 工作目录：`E:\项目\agent_study\dsh_work2`
- 后端：`backend`
- 前端：`frontend`
- 服务已于 2026-11-17 18:05 用整改后代码重启：`http://127.0.0.1:8848`（监听 PID 2152），健康检查 `{"ok":true}`；`max_model_calls=40`、`max_output_tokens=null`，前端资源 `index-DOQ5OSvE.js`
- 已提交：`1ad5e36 整改：取消输出 token 硬上限并支持运行中取消`；本轮深度团队整改尚未提交
- 单模式配置：`agent_mode` / `planning_mode` 已移除；只保留深度团队。未配置模型时 `/api/plan`、`/resume`、`/refine` 返回 400。`max_runtime_s=180`、`max_model_calls=40`、`max_agent_turns=20`；`max_output_tokens` 已废弃，旧值 1400 在加载时迁移为 None
- 前端入口：`Cache-Control: no-store`
- 最新前端资源：`/assets/index-BCHEiKlU.js`

## 已完成
> 2026-11-17 单模式改造：下列历史条目中涉及 fast/expert/legacy 的内容已失效，仅保留追溯；当前只保留深度团队。
1. 系统只保留深度团队 `peer` 单模式；`fast` / `expert` 入口与规则降级已删除，未配置模型时明确报错要求配置。
2. Agent 关键 proposal/objection/accept/final 必须引用本会话工具结果的 `evidence_id`，或明确标注未核实/待确认；主控无工具时引用专业交付转交的 `evidence_ids`。
3. Agent 会话在回合失败、请求额度耗尽或事实不足时升级为用户确认，保留可恢复会话；用户 stop/restart 返回 `run_needs_input` 终态。
4. peer 会话保留真实工具、消息、返工、版本和 Writer 协议；综述复审上限只给 restart/stop，`accept_risk` 已禁用。
5. fast 图改用独立 `fast_planner`，不生成 proposal/message 协作链；expert/legacy 的 reviewer 协议保持不变。
6. 失败/中断/完成路径统一传递 `llm_stats`；运行状态区分 `interrupted`、`needs_input`、`failed`、`failed_non_resumable`，异常事件带 `error_type` 与服务端 traceback。
7. 前端失败与中断状态展示运行用量，刷新可识别上述新状态，可恢复失败不显示“重新规划”替代恢复提示。
8. 新增最小快速图：并行查询交通/住宿/景点/天气，快速 Planner 一次排程，确定性 Critic 一次校验；fast 图不含 `peer_run`。
9. 运行级上限：模型请求数与 Agent 回合分离计数，另有单次输出和总运行时长；共享 `LLMClient` 计数，重排不重置。
10. LLM 计量：调用次数、输入/输出字符、等待时间；若兼容接口返回 usage，则记录 prompt/completion/total tokens，否则明确标记供应商未提供。
11. 前端显示运行模型用量和等待时间。
12. 用户确认请求支持 `kind/decision/accepted_issue_codes/note`，兼容旧字符串。
13. 失败/中断持久化、固定事件工具、机场接驳校验、返程日移除无意义 `rest` 时间项等之前整改已保留。
14. checkpoint 兼容旧单文件、新写入按 thread_id 分片；README 已同步事件契约、预算语义、协议、检查点与验收状态。
15. 方案 B：模型请求不再发送 `max_tokens`，`max_output_tokens` 废弃并迁移为 None，只记录 usage/字符数；保留请求数、Agent 回合、工具步数、复审轮次与总时长边界。
16. 运行中可取消：`app/cancellation.py` + `POST /api/plan/{id}/cancel`；协作式在请求返回后的节点边界停止，返回 `run_needs_input`（`recovery_status=cancelled`，DB 状态 `cancelled`），前端运行区有取消按钮，刷新可恢复。
17. mock 数据：成都新增 3 个已核对景点（p22 天府美术馆、p23 川大博物馆、p24 浣花溪公园），新增 `backend/data/mock/SOURCES.md` 记录来源与容量缺口；README 4.5 同步。
18. 深度团队恢复协议：Agent 历史按完整 assistant tool_calls + tool 事务裁剪，请求前本地校验并拒绝孤立 tool 结果（`HistoryCorrupt`），修复复测中的三次 HTTP 400。
19. 跨恢复累计账本：`LLMClient(initial_stats=...)` + `build(initial_stats=...)`；resume 从上次 run 记录带入 calls/字符/token/耗时，预算按累计计数，不再清零。
20. 工具能力：`FixedEvent` 增加稳定 `event_id`；新增 `replace_fixed_event`/`remove_fixed_event`；工具操作账本 `ctx.operations` 防止恢复后重复写；同参数只读结果缓存；checkpoint 硬校验 issue 附带 `repair_tools`。
21. 终态与排程：返程日多条送机判 `AIRPORT_TRANSFER_DUPLICATE`；`peer_stopped` 终态补 `blockers/issue_codes/deliverable=false`；固定事件含午餐时不再误报“无午餐窗口”，返程日餐费按早餐+午餐估算不再为 0。
22. 声明式返工：新增 `submit_day_plans`（全量校验后原子应用、单次版本递增、`expected_version` 冲突检测、闭馆拒绝、返回前后差异）；`route_distances` 明确 `road_data_available=false`，Writer 拒绝“步行可达/步行即到”断言；恢复接口通过 `_prior_run_stats` 带入累计用量并有 API 级测试。

## Git 存档
- `675c72b`：`整改：默认快速规划并限制多Agent成本`
- `c5a6f3a`：`优化：收缩快速路径并完善运行计量`

## 已验证
- `backend/.venv/Scripts/python.exe -m compileall -q app scripts`：通过。
- `frontend` 中 `npm.cmd run build`：通过。
- `git diff --check`：通过。
- 默认 fast 图结构：通过，包含 `fast_planner/fast_review`，不含 `peer_run`。
- 完整 fast 图合成数据执行：通过，硬校验通过，返程日无 `rest` 项。
- 模型调用上限确定性测试：通过。
- `verify_peer_success.py`：通过（五角色 24 次调用/6 回合/总价 5700/8000，评审上限升级 restart/stop）。
- `verify_peer_remediation.py`：通过（空响应/超时/预算异常/证据/Writer/分片 checkpoint/API 中断→恢复→stop）。
- `verify_peer_runtime.py`、`verify_event_contract.py`：通过。
- `verify_budget_cancel.py`：通过（不发送 max_tokens、旧配置迁移、取消、历史裁剪、孤立 tool 拒绝、固定事件增删改）。
- `verify_peer_deep_team.py`：通过（跨恢复累计用量、恢复接口带累计量、只读缓存、写操作不重放、固定午餐与返程餐费、重复送机修复闭环、`submit_day_plans` 闭馆/版本冲突/单次版本递增、路线无道路数据声明）。
- `verify_peer_success.py`：通过（24 次调用/6 回合/总价 5880/8000）；`verify_peer_remediation.py`、`verify_peer_runtime.py`、`verify_event_contract.py`、`verify_timeline.py`、`verify_edge_cases.py`：通过。
- 服务重启、健康接口、配置接口（40/null）、前端资源 `index-DOQ5OSvE.js`：通过。
- `verify_graph.py`：1 条失败为改动前既有（规则图 revision 断言），已在 HEAD 干净 worktree 复现，与本次改动无关。
- 服务重启、健康接口、配置接口、最新静态资源 `index-Dvt-jbjM.js`：通过。

## 下一步（新窗口优先）
1. 重启 :8848 服务（当前进程仍是旧代码），在浏览器确认：无模型时 400 提示、单模式表单、4 个工具证据显示为 4 次。
2. 2026-11-18 已提交评审包/账本、工具映射、返工传播（最新 0e7370e）。差异交付 + Critic 发起返工的中断续跑闭环已实现并通过 verify_delivery_patch/verify_rework_resume 等回归，待 commit；每阶段完成提醒提交。后续：真实模型与浏览器验收、上下文/工件存储瘦身、并行 Agent。
3. 需要长行程时继续扩充同城市景点：成都老人可用候选约 22 条，10 天以上仍会耗尽；扩到 40+ 才能排满 14 天。
4. 真实模型下验证取消时延（请求返回后才生效）与供应商是否返回 usage；跨中断累计调用账本仍未实现。

## 未完成/风险
- peer 架构整改正在进行：计划文档 `docs/peer-agent-remediation-plan.md`；本轮已完成 P0 API 中断、异常诊断/有限重试、停止终态、任务协议、证据引用、Writer 敏感断言、checkpoint 异步写入与集中调度文档等部分。
- 五角色成功路径已用脚本化模型验证（24 次调用、6 回合、总价 5700/8000）；评审上限升级为用户 restart/stop 已验证。恢复段仍会重新获得调用/时间预算，尚无跨恢复累计账本。
- checkpoint 已改为按 thread_id 分片（旧文件只读兼容、删除 tombstone、异步线程事务），真实 `aput_writes` 并发通过；尚无分片保留/清理策略与体积观测。
- Writer 的敏感断言扫描不是通用事实校验，不能替代结构化事实生成。
- 未真实验证供应商 usage 字段和真实 token 账单。
- 未配置模型时直接返回 400；`fast` / `expert` 规则降级已删除，不再存在三种模式。
- 旧 `verify_peer_agents.py` 仍引用废弃的 intake/flight_scout/planner/budget_critic 角色名，需要迁移到当前角色协议后才能作为完整回归脚本。
- 深度 peer 模式仍有消息协议和返工复杂度，虽然已有硬上限；Agent 的 `wake` 不是实际调度器输入，系统定位为集中式依赖调度。
- 运行级 token 目前以供应商 usage 为准；未返回 usage 时只能展示字符回退值。单次输出已不设应用侧上限，长输出风险由总时长与请求额度约束。
- 取消是协作式的：线程里的 urllib 请求无法被强杀，取消在下一个节点/请求返回后生效；已保存的 checkpoint 仍可能被 resume 接口复用。
- mock 数据容量：成都老人可用候选约 22 条，5 天够用、7 天接近上限、10 天以上排不满；来源与近似字段见 `backend/data/mock/SOURCES.md`。
- 工作区还有未提交的无关截图、运行日志和动画媒体，不能误加入提交。
