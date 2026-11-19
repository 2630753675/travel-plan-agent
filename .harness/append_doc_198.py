import pathlib

path = pathlib.Path(r"E:\项目\agent_study\dsh_work2\docs\deep-team-architecture-review.md")
text = path.read_text(encoding="utf-8")
marker = "## 20. 一句话总结"
section = """### 19.8 修复后的复测（2026-11-19，run `0b12a892549c`）

同类需求（成都 3 天、2 人含老人、预算 8000）复测，**完成**，花费 6320/8000。

| 指标 | 修复前 `6f0d0d4cf107` | 修复后 `0b12a892549c` | 变化 |
|---|---|---|---|
| 供应商请求 | 23 | **16** | −30% |
| 输入字符 | 328,086 | **173,891** | −47% |
| tokens（供应商返回） | 205,955 | **105,562** | −49% |
| 耗时 | 120.2 秒 | **81.6 秒** | −32% |
| 决策工具协议重试 | 5 | **0** | 归零 |
| 失败工具结果 | 0 | 0 | — |
| 历史自愈触发 | — | 0 | — |

剩余 1 次额外请求来自**交付字段与工具状态不一致**（`protocol_rejected` 后模型修正为正确值），不是协议缺字段。已把“期望 delivery”写进驳回信息，让模型一次改对。

顺带补观测缺口：`submit_decision` 成功现在也发 `tool_result` 事件，调用与结果能一一对应。

"""
if "19.8 修复后的复测" not in text:
    text = text.replace(marker, section + marker)
    path.write_text(text, encoding="utf-8")
    print("docs updated")
else:
    print("already recorded")
