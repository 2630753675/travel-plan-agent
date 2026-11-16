# 决策记录

## 2026-11-16：默认快速、深度团队显式启用

- 旅行规划普通请求不默认进入五角色长循环。
- `fast`：默认快速规划，最多一次模型请求用于景点选择，确定性排程和校验。
- `expert`：受控专家模式，最多三次模型请求。
- `peer`：用户显式选择后才启用 Orchestrator/Logistics/Local Expert/Critic/Writer 深度团队。
- 理由：此前简单需求出现 4-12 分钟运行、上百事件、几十次工具调用但没有结果；多 Agent 的成本大于收益。

## 2026-11-16：运行级硬上限

- 默认最长运行 180 秒。
- 默认最多 8 次模型请求。
- 默认最多 10 个 Agent 回合。
- 默认单次输出 1400 token。
- 共享一个 `LLMClient`，重排/恢复不重置计数。
- 理由：取消“无累计 token 上限”作为无限运行许可，改用请求数、时长、输出和回合上限保护。

## 2026-11-16：快速图最小化

- fast 图节点：clarify、交通/住宿/景点/天气并行查询、fast_planner、fast_review。
- fast 图不注册 peer_run，不构造旧的多 Agent proposal 消息链。
- 规则和硬约束由代码执行，模型只做需要判断的景点选择。

## 2026-11-16：用户确认协议

- resume 请求支持 `kind`、`decision`、`accepted_issue_codes`、`note`。
- 保留旧字符串 decision 兼容历史请求。
- 接受评审风险时必须明确 issue code，不能仅发送模糊的 accept_risk。

## 2026-11-16：数据计量

- LLM 客户端记录 calls、input/output chars、latency_ms。
- 如果供应商响应有 `usage.prompt_tokens/completion_tokens`，记录真实 token；否则 UI 明确显示供应商未提供 usage，不伪称精确 token。
