# AgentCrew 状态

V1 完成，master 干净，1227 passed / ruff 全清 / mypy 0 error。
原提示词七条验收 7/7，真实模型端到端 8/8（$0.10，产物实测可跑）。

```bash
python main.py run --demo --mock --auto --no-live   # 离线
python main.py run --goal "..." --auto --no-live     # 真实模型（.env 里配 Key）
python -m pytest -q
```

## 改代码前必读
- **两级并发不得嵌套**：门禁必须在任务槽位释放后执行。守卫是 `test_scheduler_deadlock.py` 的结构性检查，**别删**（三个超时测试对嵌套是盲的）。
- `bump_rework` 全仓只有 `Gatekeeper` 一处调用，删掉 → 返工回路无界。
- 人工闸门构造期强制 `ValueError`；**别在 `Crew` 里补默认 `NullPrompter`**。
- `has_placeholder` 三常量共同标定，长度单位是**信息量**（CJK ×3）非字符数。
- `ArtifactStore.commit` 路径比较必须**双侧**规范化。
- `reviews.json` 夹具的判断数必须等于该任务验收标准条数。

## 未完成
- V2 七项全未开工（外部 CLI / MCP / HTTP API+WS / LLM 摘要 / 树状拆解 / 自动选模型 / Web UI）
- Python 仅语法层覆盖 3.10+（本机只有 3.12）

## 文档
规格 `docs/superpowers/specs/2026-10-05-agentcrew-design.md` · 真实运行证据 `output/realrun{2..7}/`
