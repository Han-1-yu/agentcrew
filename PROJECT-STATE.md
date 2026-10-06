# AgentCrew 状态

V2 七项增强全部完成，master 干净，1286 passed / 1 skipped / ruff 全清 / mypy 0 error。
V1 原提示词七条验收 7/7，真实模型端到端 8/8（$0.10，产物实测可跑）。
V2 真实协同验证通过（摘要压缩 + 复杂度选模型，Todo 演示 14 次 LLM 调用零平台故障）。

V3 三十二项增强进行中：P0-1、P0-2、P0-3 已完成（全量测试中）。P0-4 已完成（全量测试中）。

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

## V3 三十二项增强（进行中）

### 阶段一 P0（立即做）
- [x] **P0-1** G2 评审 score 阈值放行 — `config.py` + `gate/gatekeeper.py`，env `AGENTCREW_REVIEW_SCORE_THRESHOLD`
- [x] **P0-2** Provider 故障转移 — `config.py` + `models/factory.py`，env `AGENTCREW_FAILOVER_<ALIAS>`（别名中 `:` 用 `__`）
- [x] **P0-3** 成本预算硬上限 — `config.py` + `models/factory.py` + `crew/crew.py` + `crew/scheduler.py`，CLI `--max-cost` / `--max-llm-calls`
- [x] **P0-4** 可删除会话 — `runtime/checkpoint.py` + `main.py` + `api/server.py` + `api/dashboard.py`，CLI `delete` / API `DELETE /runs/{id}?confirm=1`

### 阶段二 P1（近期做）
- [x] **P1-2** 并行 G2 评审 — `gate/reviewer.py`，多评审人 asyncio.gather 并行，单评审人保持串行
- [x] **P1-3** 优先级调度 — `task/task_board.py` take_ready() 按 (priority, id) 排序，T-FINAL priority=9 最后执行
- [x] **P1-4** 任务级超时 — `task/task.py` + `task/planner.py` + `crew/scheduler.py`，timeout_seconds=300，asyncio.wait_for 超时走 attempts 重试
- [ ] P1-1 MCP 工具集成到 Agent 执行链路
- [ ] P1-2 并行 G2 评审
- [ ] P1-3 优先级调度
- [ ] P1-4 任务级超时
- [ ] P1-5 创建 run 的 REST API
- [ ] P1-6 切换大模型选项

### 阶段三 P2（中期做）
- [ ] P2-1 运行时动态子任务
- [ ] P2-2 超时停滞检测
- [ ] P2-3 评审意见去重与收敛检测
- [ ] P2-4 G1 扩展：代码可执行性校验
- [ ] P2-5 门禁配置化
- [ ] P2-6 计划质量评分
- [ ] P2-7 增量计划编辑
- [ ] P2-8 Agent 自我修正循环
- [ ] P2-9 Ledger 查询与分析 API
- [ ] P2-10 Web UI 操作化

### 阶段四 P3（长期做）
- [ ] P3-1 复杂度动态校准
- [ ] P3-2 LLM 响应缓存
- [ ] P3-3 流式输出
- [ ] P3-4 多轮 Agent 对话
- [ ] P3-5 成功经验沉淀
- [ ] P3-6 产物正文智能摘要
- [ ] P3-7 跨 run 知识复用
- [ ] P3-8 增量 Checkpoint
- [ ] P3-9 MCP SSE/HTTP 传输 + 工具权限管控

### 阶段五 CLI 与工程化
- [ ] E-1 doctor 命令
- [ ] E-2 diff 命令
- [ ] E-3 API 认证与速率限制

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
