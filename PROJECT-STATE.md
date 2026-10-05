# AgentCrew 当前状态（2026-10-06）

## 结论
V1 完成并合并进 `master`。工作树干净，118 提交。
- `pytest` 1227 passed / 1 skipped · `ruff` 全清 · `mypy` 0 error（60 文件）
- 源码 11,084 行 / 62 文件；测试 12,382 行 / 39 文件
- 原提示词七条验收 7/7；设计规格 §20 九条 9/9
- 真实模型（DeepSeek）端到端：8/8 成功、退出 0、$0.10；产物经实测完整 CRUD 可跑

## 入口
```bash
python main.py run --demo --mock --auto --no-live      # 离线，无需 Key
python main.py run --goal "..." --auto --no-live        # 真实模型（需 .env）
python -m pytest -q
```
`python main.py` 裸命令 = 帮助 + 角色数 + 最近 3 次 run。

## 架构（两阶段，LLM 只在两处被调用）
```
规划: PM(LLM) 拆解 → validate_plan 七+一条校验 → 追加 T-FINAL
执行: Scheduler(无 LLM) 就绪队列 + 两级信号量 → Agent(LLM) → 门禁
门禁: G1 静态(0 token) → G2 逐条判定验收标准(LLM) → G3 CLI 人工
```
单一事实源 `TaskBoard`；状态只能经 `transition()`；10 态状态机。
产物只经 `ArtifactStore`（路径穿越防护 + 原子写 + MANIFEST）。
checkpoint 支持断点续跑；ledger 全程留痕 + token/成本。

## 让 S3 过关的两个结构性修复（前六次真实运行都卡在这）
1. **计划期契约自相矛盾**：标准要求文档、契约却没有文档交付物 → agent 写被 G1 拒、不写被 G2 拒，死锁。修：`planner._check_criteria_have_deliverables`。
2. **开放式评审不收敛**：直接问模型 approve/reject，认真评审员每轮都能挑毛病。修：schema 去掉 verdict，改逐条判定 + 裁决推导；`suggestions` 不引发返工。

## 易踩的坑（改代码前必读）
- 两级并发**不得嵌套**：门禁必须在任务槽位释放后执行。守门测试是 `test_scheduler_deadlock.py` 的**结构性**守卫（读 `_task_slot.locked()`）——三个超时测试对嵌套是盲的，别删。
- `bump_rework` 全仓只有 `Gatekeeper` 一处调用；删掉 → 返工回路无界。
- 人工闸门构造期强制：`gate` 含人工卡点且 `prompter=None` → `ValueError`。别在 `Crew` 里补默认 `NullPrompter`。
- `has_placeholder` 三个常量共同标定：`_STUB_MAX_UNITS=400`、`_PLACEHOLDER_MAX_UNITS=40`、`_CASE_SENSITIVE_MARKERS={"TODO"}`。长度单位是**信息量**（CJK ×3），不是字符数。
- `ArtifactStore.commit` 路径比较**双侧**规范化，否则 `file=./a.py` 被误判契约外。
- `reviews.json` 夹具按任务 id 逐条给 judgement，数量必须等于该任务验收标准条数（`_default` 兜底已不适用）。
- 测试套件依赖 `pyproject.toml` 的 `asyncio_mode = "auto"` 与 conftest 的 `.env` 隔离夹具。

## 未完成
- **V2 七项全未开工**（原提示词「十」）：外部 CLI 引擎接入、MCP、HTTP API+WebSocket、LLM 摘要压缩、树状拆解、按复杂度选模型、Web UI。
- Python 版本仅在**语法层**覆盖（ruff py310）；本机无 3.10/3.11/3.13/3.14，运行时行为无实证。
- SDD 流程遗留 ~65 条 minor，明细随工作区删除（最终评审曾逐条分流为「可发布」，其中 3 条必修已修）。
- README §19.3 五条已知限制属设计边界，非未完成项。

## 建议下一步
1. 换一个非 Todo 的真实目标跑一次，验证泛化。
2. 上 CI（3.10/3.11/3.12 矩阵 + ruff + mypy）。
3. 从 V2 挑一项（HTTP API + WebSocket 或 MCP 实用价值最高）。

## 参考文档
- 设计规格：`docs/superpowers/specs/2026-10-05-agentcrew-design.md`
- 实施计划：`docs/superpowers/plans/2026-10-05-agentcrew-v1.md`
- 原提示词：`docs/reference/original-prompt.md`
- 真实运行记录：`output/realrun{2..7}/`（2–6 是失败证据，7 是首次通过）
- 前序会话交接：`HANDOFF.md`
