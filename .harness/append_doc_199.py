import pathlib

path = pathlib.Path(r"E:\项目\agent_study\dsh_work2\docs\deep-team-architecture-review.md")
text = path.read_text(encoding="utf-8")
marker = "## 20. 一句话总结"
section = """### 19.9 评审包修复后的复测（2026-11-19，run `90cc86dcc23f`）

| 指标 | 19.8（`0b12a892549c`） | 19.9（`90cc86dcc23f`） |
|---|---|---|
| 结果 | 完成 | 完成 |
| 请求数 | 16 | 17 |
| 输入字符 | 173,891 | 243,466 |
| tokens | 105,562 | 126,143 |
| 耗时 | 81.6 秒 | 82.9 秒 |
| 协议退回 | 1（交付字段不一致） | **0** |
| 历史自愈 | 0 | 0 |
| 评审轮次 / 回合 | 1 / 6 | 1 / 5 |

结论：协议层的循环问题已消除（此前 20 回合耗尽），两次运行都在 5–6 回合内收敛。两次之间的请求与字符差异来自**模型自身的工具迭代次数**：本次本地体验 Agent 调了 3 次 `submit_day_plans`、5 次 `materialize_plan`，还调了一次 `get_plan(view="full")`（返回体最大）。这是模型行为差异，不是协议缺陷；若要压，只能改提示约束或限制 `full` 视图的使用者。

"""
if "19.9 评审包修复后的复测" not in text:
    path.write_text(text.replace(marker, section + marker), encoding="utf-8")
    print("docs updated")
else:
    print("already recorded")
