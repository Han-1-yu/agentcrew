# AgentCrew 状态

V2 七项增强全部完成，master 干净，1286 passed / 1 skipped / ruff 全清 / mypy 0 error。
V1 原提示词七条验收 7/7，真实模型端到端 8/8（$0.10，产物实测可跑）。
V2 真实协同验证通过（摘要压缩 + 复杂度选模型，Todo 演示 14 次 LLM 调用零平台故障）。

```bash
python main.py run --demo --mock --auto --no-live   # 离线
python main.py run --goal "..." --auto --no-live     # 真实模型（.env 里配 Key）
python main.py serve --port 8765                     # HTTP API + Web UI（V2）
python -m pytest -q
```

## V2 七项增强（全部完成）
1. LLM 摘要式上下文压缩 — `context/summarizer.py`，未配置时 NullSummarizer 零开销
2. 按复杂度自动选模型 — `models/complexity.py`，env `AGENTCREW_TIER_LOW/MEDIUM/HIGH`
3. 外部 CLI 引擎接入 — `models/providers/cli.py`，provider_overrides 指定 command
4. 树状任务拆解 — `task/task.py` parent_id + `task/graph.py` 树查询
5. HTTP API + SSE — `api/server.py`，`python main.py serve` 启动
6. MCP 协议互操作 — `mcp/client.py`，stdio JSON-RPC 客户端
7. Web UI 面板 — `api/dashboard.py`，Kanban 看板 + 实时事件流

## 改代码前必读
- **两级并发不得嵌套**：门禁必须在任务槽位释放后执行。守卫是 `test_scheduler_deadlock.py` 的结构性检查，**别删**（三个超时测试对嵌套是盲的）。
- `bump_rework` 全仓只有 `Gatekeeper` 一处调用，删掉 → 返工回路无界。
- 人工闸门构造期强制 `ValueError`；**别在 `Crew` 里补默认 `NullPrompter`**。
- `has_placeholder` 三常量共同标定，长度单位是**信息量**（CJK ×3）非字符数。
- `ArtifactStore.commit` 路径比较必须**双侧**规范化。
- `reviews.json` 夹具的判断数必须等于该任务验收标准条数。

## 未完成 / 已知缺口
- Python 仅语法层覆盖 3.10+（CI 配置了 3.10–3.13 矩阵，但需推到 GitHub 实跑验证）
- 仓库无 git remote，CI 尚未在 GitHub Actions 实际运行过
- MCP 客户端仅 stdio 传输，未实现 SSE/HTTP 传输
- Web UI 为只读看板，不支持人工闸门交互
- SDD ~65 条 minor 清单已删除（已分流为"可发布"，3 条必须修已修完）

## 文档
规格 `docs/superpowers/specs/2026-10-05-agentcrew-design.md` · 真实运行证据 `output/realrun{2..8}/` · V2 验证 `output/v2verify/`
