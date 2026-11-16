# 当前任务状态

## 目标
继续完善旅行规划助手：默认快速规划，专家模式按需启用，深度多 Agent 团队显式启用；降低模型调用、耗时和 token；统一失败/中断/用户确认；同步文档并完成自检。

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
- 服务已重启：`http://127.0.0.1:8848`
- 健康检查：`{"ok":true}`
- 当前配置：`agent_mode=fast`、`max_runtime_s=180`、`max_model_calls=8`、`max_agent_turns=10`、`max_output_tokens=1400`
- 前端入口：`Cache-Control: no-store`
- 最新前端资源：`/assets/index-z8KDp3ax.js`

## 已完成
1. 默认快速模式 `fast`，专家模式 `expert`，深度团队 `peer` 显式启用。
2. 新增最小快速图：并行查询交通/住宿/景点/天气，快速 Planner 一次排程，确定性 Critic 一次校验；fast 图不含 `peer_run`。
3. 运行级上限：模型请求数、Agent 回合、单次输出、总运行时长；共享 `LLMClient` 计数，重排不重置。
4. LLM 计量：调用次数、输入/输出字符、等待时间；若兼容接口返回 usage，则记录 prompt/completion/total tokens，否则明确标记供应商未提供。
5. 前端显示运行模型用量和等待时间。
6. 用户确认请求支持 `kind/decision/accepted_issue_codes/note`，兼容旧字符串。
7. 失败/中断持久化、固定事件工具、机场接驳校验、返程日移除无意义 `rest` 时间项等之前整改已保留。
8. README 已同步三种模式、运行上限、时间线责任和确认规则。

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
- 服务重启、健康接口、配置接口、最新静态资源：通过。

## 下一步（新窗口优先）
1. 先读本文件、`DECISIONS.md`、`WORK.md`，再运行 `git status --short` 和最新编译/构建核验。
2. 审查快速图的 `planner_node` 是否仍生成非必要 Agent proposal；若 fast 不需要，继续删除/隔离纯展示消息和旧 reviewer 节点。
3. 补齐运行计量在失败/中断事件中的前端展示，并确认 `llm_stats` 在所有路径传递一致。
4. 细化 `checkpoint_lost`、`failed_non_resumable` 等运行状态，避免记录显示可恢复但 checkpoint 不存在。
5. 做真实模型验收前，不要自行调用外部模型；只做 mock/monkeypatch 确定性测试。

## 未完成/风险
- 未真实验证供应商 usage 字段和真实 token 账单。
- 专家模式仍复用受控规则图，不是完整的 2-3 Agent 专家团队。
- 深度 peer 模式仍有消息协议和返工复杂度，虽然已有硬上限。
- 运行级 token 目前以供应商 usage 为准；未返回 usage 时只能展示字符回退值。
- 工作区还有未提交的无关截图、运行日志和动画媒体，不能误加入提交。
