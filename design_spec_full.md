1	# AgentCrew 多智能体协作平台 — 设计规格（V1）
2	
3	- 日期：2026-10-05
4	- 状态：待评审
5	- 源需求：`docs/reference/original-prompt.md`（由 `AgentCrew项目提示词.docx` 提取）
6	- 目标运行时：Python 3.10+（开发环境为 3.12.10），Windows / macOS / Linux
7	
8	---
9	
10	## 1. 目标、范围与成功标准
11	
12	### 1.1 目标
13	
14	交付一个**可离线验证**的虚拟项目团队系统：用户给出顶层项目目标，平台自动拆解为带依赖关系的任务 DAG，按角色分派给 8 个专职 Agent 并发执行，经三层质量门禁验收后，在 `output/<run_id>/artifacts/` 下产出可运行的完整项目交付物。全流程可在终端实时观测，可中断、可从断点续跑、可取消。
15	
16	### 1.2 本版（V1）成功标准
17	
18	| # | 标准 | 验证方式 |
19	|---|---|---|
20	| S1 | `pip install -r requirements.txt` 一次装全（含测试依赖） | 干净虚拟环境执行 |
21	| S2 | `python main.py run --demo --mock` 无需任何 API Key 跑完全流程 | CI / 人工 |
22	| S3 | `python main.py run --goal "..." --auto` 用真实模型跑完全流程 | 需 Key |
23	| S4 | `pytest` 全绿，且**零网络调用** | CI / 人工 |
24	| S5 | 演示运行结束后 `output/<run_id>/artifacts/` 内含可 `uvicorn` 启动的 Todo List Web 应用 | **人工验证**（§16.2 命令），不在 pytest 中执行用户代码 |
25	| S6 | 进程被 `Ctrl-C` 中断后，`python main.py resume <run_id>` 能继续执行至完成 | 人工 |
26	| S7 | 新增角色只需新增 `RoleSpec` 数据，无需改动引擎代码 | 新增角色后跑通 |
27	| S8 | 8 个内置角色各有独立文件、独立提示词、可独立测试 | 代码审查 + 单测 |
28	| S9 | mock 走与真实模型完全相同的数据通路（解析→契约→落盘→门禁），无旁路 | 代码审查 + `test_mock_fixtures.py` |
29	
30	### 1.3 明确非目标（V2 留口见 §19）
31	
32	Web UI、HTTP API 服务端、MCP 协议互操作、外部 CLI 引擎（Claude Code / Codex / Cursor）接入、树状任务拆解、按认知复杂度自动选模型、LLM 摘要式上下文压缩。
33	
34	---
35	
36	## 2. 与原提示词的差异（审计修正表）
37	
38	原提示词是需求来源，本节逐条列出**修正项**及理由。实现时以本规格为准。
39	
40	### 2.1 硬矛盾修正
41	
42	| # | 原文位置 | 矛盾 | 本规格的修正 |
43	|---|---|---|---|
44	| A1 | 六.3 vs 六.4 vs 五 | 「项目经理是核心调度者」「TaskBoard 自动按依赖调度」「crew.py 是团队调度器」——三处争夺调度权 | **三层职责分离**（§3、§7）：`Crew` 是确定性执行引擎，掌管调度循环；`TaskBoard` 是任务状态与依赖的唯一事实源；`ProjectManagerAgent` 只做 LLM 决策（拆解 / 验收），**不参与任何调度**。调度不依赖 LLM。 |
45	| A2 | 七.3 vs 八 | 七.3 要求「填 API Key 后示例可直接运行」，八 关键词要求「模拟运行模式」——无 Key 用户与 CI 无法运行 | Mock 提升为**一等 Provider**（§16）：`--mock` 离线跑完整流程并产出真实可运行交付物；`pytest` 默认全 mock。真实模型与 mock 走同一条代码路径，仅 Provider 实现不同。 |
46	| A3 | 四 | 把 `asyncio` 列为 pip 依赖（它是标准库）；关键词承诺「自动化测试」但无 `tests/`、无 pytest | 修正依赖清单（§17.5）；新增 `tests/` 全套离线测试（§18）。 |
47	
48	### 2.2 缺失补全
49	
50	| # | 缺失 | 补全位置 |
51	|---|---|---|
52	| B1 | 评审无执行者、无重试上限（会无限驳回） | §10 三层门禁 + `max_reworks` / `max_attempts` 上限 |
53	| B2 | 无机器可校验的交付物契约 | §9.1 `ArtifactSpec` + §10.1 静态契约校验 |
54	| B3 | 无文件落盘模块、无路径穿越防护 | §9.3 `ArtifactStore` 安全写入 |
55	| B4 | 无持久化模块，TaskBoard 仅内存 | §13 JSON checkpoint + resume / cancel |
56	| B5 | 全量上下文互灌会撑爆窗口 | §11 上下文包（引用 + 摘要 + 预算降级） |
57	| B6 | 状态机不完整、无跃迁表、无环检测 | §6 十态状态机 + 跃迁表；§7.2 Kahn 环检测 |
58	| B7 | 无并发管控、无限流退避 | §7.3 信号量 + 就绪队列；§17.6 原生退避重试 |
59	| B8 | 模型适配描述不准确 | §12 按 Provider 声明能力位 |
60	| B9 | 结构化输出无降级链 | §12.3 四级降级 + 修复循环 |
61	| B10 | 无人真的校验生成物 | §10.1 静态层（含 `ast.parse`）+ 契约符号检查 |
62	| B11 | 无 token / 成本统计 | §14 Ledger 记账 + 终端汇总 |
63	
64	### 2.3 次要修正
65	
66	| # | 问题 | 修正 |
67	|---|---|---|
68	| C1 | 无 `.gitignore`，`.env` 有提交泄露风险 | §17.4 提供 `.gitignore`，`.env` 永不入库 |
69	| C2 | 多 Agent 并发写同一文件会互相覆盖 | §9.2 契约路径唯一性校验：同一路径在同一次 run 内只允许一个任务声明 |
70	| C3 | 整次 run 失败后的兜底策略未定义 | §7.4 死锁/全阻塞判定 + 明确退出码与诊断报告 |
71	| C4 | `Task` 与 `asyncio.Task` 命名混淆 | `agentcrew.task.task.Task` 为领域模型；代码中 `asyncio.Task` 一律以 `asyncio.Task` 全名引用 |
72	| C5 | V1/V2 边界混杂 | §19 明确分界 |
73	| C6 | 产出目录未做 run 级命名空间 | §13.1 `output/<run_id>/`，run_id = `YYYYMMDD-HHMMSS-<4位随机>` |
74	| C7 | 原目录结构缺少多个必需模块的归属 | §4 目录结构（保留原结构，新增 `roles/` `artifacts/` `gate/` `context/` `runtime/` `tests/`，每项对应上表某条修正） |
75	
76	### 2.4 保留不动的设计
77	
78	原提示词中以下约定**不做改动**：8 个内置角色及其职责划分（三）、禁用第三方 Agent 框架（四）、Pydantic v2 + 全量类型注解 + PEP 8（四）、`output/` 作为交付物根目录（五）、BaseAgent 模板方法 + 角色子类（六.1/六.2）、Crew 一键启动（六.5）、README 内容要求（七.7）。
79	
80	---
81	
82	## 3. 架构总览
83	
84	```
85	┌──────────────────────────────────────────────────────────────┐
86	│ main.py  —  CLI（run / resume / status / roles / demo / clean）│
87	└───────────────────────────┬──────────────────────────────────┘
88	                            │
89	┌───────────────────────────▼──────────────────────────────────┐
90	│ Crew — 确定性执行引擎（不包含任何 LLM 逻辑）                    │
91	│                                                              │
92	│  ┌────────────┐   ┌────────────┐   ┌──────────────┐          │
93	│  │ TaskBoard  │──▶│ Scheduler  │──▶│  Gatekeeper  │          │
94	│  │ DAG+状态机 │◀──│ 就绪队列   │   │  G1/G2/G3    │          │
95	│  │ 唯一事实源 │   │ 信号量限流 │   │              │          │
96	│  └─────┬──────┘   └─────┬──────┘   └──────┬───────┘          │
97	│        │                │                 │                  │
98	│  ┌─────▼──────┐   ┌─────▼──────┐   ┌──────▼───────┐          │
99	│  │ Checkpoint │   │ ContextBus │   │ ArtifactStore│          │
100	│  │ JSON 原子写│   │ 上下文包   │   │ 契约+安全落盘│          │
101	│  └────────────┘   └────────────┘   └──────────────┘          │
102	│  ┌────────────────────────────────────────────┐              │
103	│  │ Ledger — 日志 / token / 成本 审计            │              │
104	│  └────────────────────────────────────────────┘              │
105	└───────────────────────────┬──────────────────────────────────┘
106	                            │  dispatch(Task) → AgentResult
107	┌───────────────────────────▼──────────────────────────────────┐
108	│ Agent 层（唯一持有 LLM 的地方）                                │
109	│  BaseAgent（模板方法：组装提示词 → 调用 → 解析 → 校验 → 提交）   │
110	│    └─ 8 个角色子类，各自实现钩子 + 声明 RoleSpec                │
111	└───────────────────────────┬──────────────────────────────────┘
112	                            │
113	┌───────────────────────────▼──────────────────────────────────┐
114	│ Model 层：ModelFactory → BaseProvider                         │
115	│   OpenAICompatProvider / AnthropicProvider / MockProvider      │
116	│   + StructuredOutput 降级链 + 能力位 + 计费表                  │
117	└──────────────────────────────────────────────────────────────┘
118	```
119	
120	### 3.1 关键设计原则
121	
122	1. **LLM 只在两个地方被调用**：任务执行（Agent）与 LLM 评审（Gatekeeper G2）。调度、状态流转、依赖解析、契约校验全部确定性实现 —— 这使平台可测试、可复现、可断点。
123	2. **角色是数据，不是代码**：8 个角色的差异集中在提示词与输出契约。`RoleSpec` 是数据对象；角色 `.py` 文件只负责声明。新增角色 = 新增一个 `RoleSpec`，引擎零改动。
124	3. **单一事实源**：任务状态只存在于 `TaskBoard`。任何模块不得缓存任务状态副本；所有状态变更必须经 `TaskBoard.transition()` 走状态机校验。
125	4. **契约先行**：任务在创建时就声明产物路径与验收标准；「完成」由机器可判定的检查定义，不由 Agent 自称。
126	5. **一切可中断**：任意时刻 `Ctrl-C`，checkpoint 已落盘；resume 后从最近一致状态继续。
127	
128	---
129	
130	## 4. 目录结构
131	
132	保留原提示词（五）的骨架，新增 5 个包与 1 个测试目录；每项新增都对应 §2 中的一条修正。
133	
134	```
135	agentcrew/
136	├── .env.example                    # API 配置模板（C1）
137	├── .gitignore                      # 忽略 .env / output/ / __pycache__（C1）
138	├── requirements.txt                # 依赖清单（含测试依赖，A3）
139	├── pyproject.toml                  # pytest / asyncio / 代码风格配置
140	├── README.md                       # 项目说明（七.7）
141	├── main.py                         # CLI 入口（六，扩展为多子命令）
142	│
143	├── agentcrew/
144	│   ├── __init__.py
145	│   ├── config.py                   # Settings：env + 可选 agentcrew.yaml（§17）
146	│   │
147	│   ├── models/                     # 【原结构】多模型接入层
148	│   │   ├── __init__.py
149	│   │   ├── factory.py              # ModelFactory：别名 → Provider 实例（缓存）
150	│   │   ├── config.py               # ModelBinding / ProviderCapabilities
151	│   │   ├── pricing.py              # 计价表 + 成本计算（B11）
152	│   │   ├── structured.py           # 结构化输出四级降级链（B9）
153	│   │   └── providers/
154	│   │       ├── __init__.py
155	│   │       ├── base.py             # BaseProvider ABC + CompletionResult
156	│   │       ├── openai_compat.py    # OpenAI 兼容（OpenAI/豆包/通义/DeepSeek/Ollama）
157	│   │       ├── anthropic.py        # Anthropic 原生 Messages API
158	│   │       └── mock.py             # MockProvider（确定性回放，A2）
159	│   │
160	│   ├── roles/                      # 【新增】角色即数据（S7）
161	│   │   ├── __init__.py
162	│   │   ├── spec.py                 # RoleSpec 模型 + 加载/校验
163	│   │   └── catalog.py              # 8 个内置角色的声明式定义
164	│   │
165	│   ├── agents/                     # 【原结构】角色 Agent 层
166	│   │   ├── __init__.py
167	│   │   ├── base_agent.py           # BaseAgent 模板方法（六.1）
168	│   │   ├── project_manager.py      # 拆解 + 终验（六.3）
169	│   │   ├── product_manager.py
170	│   │   ├── architect.py
171	│   │   ├── backend_dev.py
172	│   │   ├── frontend_dev.py
173	│   │   ├── qa_engineer.py
174	│   │   ├── devops_engineer.py
175	│   │   └── technical_writer.py
176	│   │
177	│   ├── task/                       # 【原结构】任务管理模块
178	│   │   ├── __init__.py
179	│   │   ├── task.py                 # Task / TaskStatus / TaskResult（B6）
180	│   │   ├── graph.py                # 邻接表 / Kahn 拓扑 / 环检测 / 就绪集（B6）
181	│   │   ├── state_machine.py        # 跃迁表 + 校验（B6）
182	│   │   ├── task_board.py           # 全局任务看板 = 唯一事实源
183	│   │   └── planner.py              # PM 拆解结果 → 校验 → 建图（B2/B6）
184	│   │
185	│   ├── artifacts/                  # 【新增】产物契约与安全落盘（B2/B3/C2）
186	│   │   ├── __init__.py
187	│   │   ├── spec.py                 # ArtifactSpec / Manifest
188	│   │   ├── parser.py               # 从 Agent 输出中解析带路径的围栏块
189	│   │   ├── store.py                # ArtifactStore：安全路径 + 原子写
190	│   │   └── contract.py             # 契约静态校验（G1）
191	│   │
192	│   ├── gate/                       # 【新增】三层质量门禁（B1/B10）
193	│   │   ├── __init__.py
194	│   │   ├── static_checks.py        # G1：存在/非空/可解析/必含符号
195	│   │   ├── reviewer.py             # G2：LLM 评审
196	│   │   └── gatekeeper.py           # G3：编排三层 + 人工闸门
197	│   │
198	│   ├── context/                    # 【新增】上下文组装（B5）
199	│   │   ├── __init__.py
200	│   │   ├── budget.py               # Token 预算与逐级降级
201	│   │   └── context_bus.py          # ContextPack 组装
202	│   │
203	│   ├── crew/                       # 【原结构】团队编排核心
204	│   │   ├── __init__.py
205	│   │   ├── crew.py                 # Crew：一键启动全流程（六.5）
206	│   │   └── scheduler.py            # 就绪队列 + 并发调度（B7）
207	│   │
208	│   ├── runtime/                    # 【新增】运行时持久化与审计（B4/B11）
209	│   │   ├── __init__.py
210	│   │   ├── checkpoint.py           # CheckpointStore：原子写 / resume / cancel
211	│   │   ├── ledger.py               # 追加式审计日志
212	│   │   └── board_view.py           # rich 终端看板渲染
213	│   │
214	│   └── utils/
215	│       ├── __init__.py
216	│       ├── logger.py               # 日志（含密钥脱敏）
217	│       ├── retry.py                # 原生指数退避重试（B7，不引 tenacity）
218	│       ├── tokens.py               # token 估算（无额外依赖）
219	│       └── files.py                # 安全路径工具 / 原子写 / 目录树
220	│
221	├── examples/
222	│   ├── todo_web_project.py         # 完整项目演示（七.5）
223	│   └── fixtures/                   # MockProvider 的确定性回放数据（A2/S4）
224	│       ├── plan_todo.json          # 预置拆解结果（结构化）
225	│       ├── reviews.json            # 预置 G2 评审结论（按 task_id + round）
226	│       └── responses/              # 预置 LLM 原始回复（含 file= 围栏块）
227	│           ├── T-001/attempt-1.md
228	│           ├── T-003/attempt-1.md  # 故意不合格，触发 G1 驳回
229	│           ├── T-003/attempt-2.md  # 返工后的合格版本
230	│           └── T-FINAL/attempt-1.md
231	│
232	├── tests/                          # 【新增】全离线测试（A3/S4）
233	│   ├── conftest.py
234	│   ├── test_state_machine.py
235	│   ├── test_graph.py
236	│   ├── test_task_board.py
237	│   ├── test_planner.py
238	│   ├── test_scheduler.py
239	│   ├── test_scheduler_deadlock.py  # 评审槽位不自锁（§7.3）
240	│   ├── test_artifact_parser.py
241	│   ├── test_artifact_store.py      # 含路径穿越安全用例
242	│   ├── test_contract.py
243	│   ├── test_gatekeeper.py
244	│   ├── test_structured_output.py
245	│   ├── test_checkpoint.py
246	│   ├── test_context_budget.py
247	│   ├── test_roles_catalog.py
248	│   ├── test_agents_contract.py
249	│   ├── test_mock_fixtures.py       # fixture 自洽性
250	│   ├── test_mock_no_bypass.py      # mock 无旁路（S9）
251	│   ├── test_logger_redaction.py    # 密钥脱敏
252	│   └── test_end_to_end_mock.py
253	│
254	├── docs/
255	│   ├── reference/original-prompt.md
256	│   └── superpowers/specs/2026-10-05-agentcrew-design.md
257	│
258	└── output/                         # 运行时生成，不入库
259	    └── <run_id>/...
260	```
261	
262	> 说明：原结构中的 `models/model_factory.py` 与 `models/config.py` 保留，但拆出 `providers/` 子包以容纳多 Provider 实现；原 `utils/` 保留并扩充。
263	
264	---
265	
266	## 5. 数据模型（Pydantic v2）
267	
268	### 5.1 任务与状态
269	
270	```python
271	# agentcrew/task/task.py
272	class TaskStatus(str, Enum):
273	    PENDING   = "pending"     # 依赖未满足，尚未进入调度
274	    READY     = "ready"       # 依赖已满足，等待派发（含重试等待、返工等待）
275	    RUNNING   = "running"     # 已派发给 Agent，执行中
276	    REVIEW    = "review"      # Agent 已提交，正在过门禁
277	    REWORK    = "rework"      # 被驳回，等待再次执行
278	    SUCCEEDED = "succeeded"   # 终态：通过全部门禁
279	    FAILED    = "failed"      # 终态：重试/返工额度用尽，或不可恢复错误
280	    BLOCKED   = "blocked"     # 上游永久失败导致本任务无法开始
281	    CANCELLED = "cancelled"   # 终态：用户取消
282	    SKIPPED   = "skipped"     # 终态：上游被跳过导致本任务跳过
283	```
284	
285	```python
286	class Task(BaseModel):
287	    model_config = ConfigDict(extra="forbid")
288	
289	    id: str                              # "T-001"，run 内唯一
290	    title: str
291	    description: str                     # 发给 Agent 的任务描述
292	    role: str                            # RoleSpec.key
293	    acceptance_criteria: list[str]       # 验收标准，供 G2 评审使用
294	    deliverables: list[ArtifactSpec]     # 交付物契约
295	    dependencies: list[str] = []         # 前置任务 id
296	    priority: int = Field(5, ge=1, le=9) # 数字小者优先
297	    brief: str = ""                      # PM 写给下游的一句话摘要（供上下文压缩）
298	
299	    # 运行时字段
300	    status: TaskStatus = TaskStatus.PENDING
301	    attempts: int = 0                    # 执行尝试次数
302	    rework_count: int = 0                # 返工次数
303	    max_attempts: int = 3                # 执行重试上限（含首次）
304	    max_reworks: int = 2                 # 返工上限
305	    result: TaskResult | None = None
306	    reviews: list[ReviewRecord] = []     # 完整评审轨迹
307	    status_history: list[StatusChange] = []
308	    created_at: datetime
309	    updated_at: datetime
310	```
311	
312	```python
313	class TaskResult(BaseModel):
314	    task_id: str
315	    role: str
316	    summary: str                          # Agent 自述摘要（进上下文包）
317	    artifacts: list[ArtifactRecord]       # 实际落盘的文件
318	    notes: str = ""                       # 围栏块之外的说明文字（不落盘）
319	    started_at: datetime
320	    finished_at: datetime
321	    duration_ms: int
322	```
323	
324	### 5.2 产物契约
325	
326	```python
327	# agentcrew/artifacts/spec.py
328	ArtifactKind = Literal[
329	    "python", "json", "yaml", "toml", "markdown", "text",
330	    "html", "css", "js", "dockerfile", "sql", "shell"
331	]
332	
333	class ArtifactSpec(BaseModel):
334	    path: str                     # 相对 artifacts/ 根，必须是 POSIX 相对路径
335	    kind: ArtifactKind
336	    description: str              # 该文件应当包含什么
337	    required: bool = True
338	    min_bytes: int = 1
339	    must_contain: list[str] = []  # 必须出现的字面量/符号，如 ["def create_app"]
340	    max_bytes: int = 512 * 1024   # 单文件上限，防模型跑飞
341	
342	class ArtifactRecord(BaseModel):
343	    path: str
344	    kind: ArtifactKind
345	    size_bytes: int
346	    sha256: str
347	    produced_by_task: str
348	    produced_by_role: str
349	    written_at: datetime
350	```
351	
352	### 5.3 评审记录
353	
354	```python
355	class ReviewRecord(BaseModel):
356	    task_id: str
357	    round: int                                     # 第几轮评审（从 1 起）
358	    layer: Literal["static", "llm", "human"]
359	    verdict: Literal["approve", "reject"]
360	    score: float | None = None                     # 0..1，仅 LLM 层有
361	    issues: list[str] = []                         # 问题清单
362	    fix_hints: list[str] = []                      # 修复建议（驳回时回灌给 Agent）
363	    reviewer: str                                  # "static:contract" / role key / "human:cli"
364	    created_at: datetime
365	```
366	
367	### 5.4 角色规格
368	
369	```python
370	# agentcrew/roles/spec.py
371	class RoleSpec(BaseModel):
372	    key: str                        # "backend_dev"
373	    title: str                      # "后端开发工程师"
374	    goal: str                       # 核心职责（对应 CrewAI 的 goal）
375	    backstory: str                  # 背景设定（对应 backstory）
376	    responsibilities: list[str]     # 职责清单
377	    boundaries: list[str]           # 能力边界：明确不做什么（原需求二.1）
378	    output_contract: str            # 输出规范的自然语言描述
379	    system_prompt: str              # 系统提示词
380	    temperature: float = 0.3
381	    max_output_tokens: int = 8192
382	    model: str = "default"          # 模型别名，见 §12.2
383	    reviewer_for: list[str] = []    # 该角色可评审哪些角色 key；["*"] 表示全部
384	```
385	
386	### 5.5 运行记录
387	
388	```python
389	class RunStatus(str, Enum):
390	    RUNNING = "running"; INTERRUPTED = "interrupted"
391	    COMPLETED = "completed"; FAILED = "failed"; CANCELLED = "cancelled"
392	
393	class RunRecord(BaseModel):
394	    run_id: str
395	    schema_version: int = 1
396	    project_goal: str
397	    status: RunStatus
398	    created_at: datetime
399	    updated_at: datetime
400	    config_snapshot: dict[str, Any]   # 生效配置快照，保证 resume 可复现
401	    stats: RunStats
402	
403	class RunStats(BaseModel):
404	    total_tasks: int = 0
405	    succeeded: int = 0; failed: int = 0; blocked: int = 0; skipped: int = 0
406	    llm_calls: int = 0
407	    prompt_tokens: int = 0; completion_tokens: int = 0
408	    estimated_cost_usd: float = 0.0
409	    started_at: datetime | None = None
410	    finished_at: datetime | None = None
411	```
412	
413	---
414	
415	## 6. 状态机
416	
417	### 6.1 跃迁表（唯一权威定义）
418	
419	`agentcrew/task/state_machine.py` 中以字典形式硬编码，任何变更必须同步更新本表与单测。
420	
421	| 起始态 | 允许跃迁到 | 触发条件 |
422	|---|---|---|
423	| `PENDING` | `READY` | 全部依赖为 `SUCCEEDED` |
424	| `PENDING` | `BLOCKED` | 任一依赖为 `FAILED` / `CANCELLED` |
425	| `PENDING` | `SKIPPED` | 任一依赖为 `SKIPPED` |
426	| `PENDING` | `CANCELLED` | 用户取消 |
427	| `READY` | `RUNNING` | 调度器取得并发槽位并派发 |
428	| `READY` | `BLOCKED` | 派发前发现依赖已失败 |
429	| `READY` | `CANCELLED` | 用户取消 |
430	| `RUNNING` | `REVIEW` | Agent 正常返回且产物解析成功 |
431	| `RUNNING` | `READY` | 执行异常且 `attempts < max_attempts`（重试） |
432	| `RUNNING` | `FAILED` | 执行异常且 `attempts >= max_attempts` |
433	| `RUNNING` | `CANCELLED` | 用户硬取消 |
434	| `REVIEW` | `SUCCEEDED` | G1 + G2 + G3 全部 approve |
435	| `REVIEW` | `REWORK` | 任一门禁 reject 且 `rework_count < max_reworks` |
436	| `REVIEW` | `FAILED` | 门禁 reject 且 `rework_count >= max_reworks` |
437	| `REVIEW` | `CANCELLED` | 用户取消 |
438	| `REWORK` | `RUNNING` | 调度器取得槽位并派发（携带 `fix_hints`） |
439	| `REWORK` | `CANCELLED` | 用户取消 |
440	| `SUCCEEDED` | — | 终态 |
441	| `FAILED` | `READY` | **仅** 用户通过 `main.py retry-task` 显式重置（V1 保留接口，默认不启用） |
442	| `BLOCKED` | `READY` | 每轮调度重算：全部依赖变为 `SUCCEEDED` |
443	| `CANCELLED` | — | 终态 |
444	| `SKIPPED` | — | 终态 |
445	
446	**非法跃迁必须抛 `IllegalTransitionError`**，并携带 `(task_id, from_status, to_status)`。禁止任何绕过 `TaskBoard.transition()` 的状态直接赋值 —— 单测中通过 monkeypatch 断言 `Task.status` 只被 `transition()` 写入。
447	
448	### 6.2 额度语义
449	
450	- `attempts`：**执行**尝试次数。每次 `READY → RUNNING` 时 +1。覆盖「模型调用失败 / 输出解析失败 / 需要重试」。因额度在**派发时**即扣除，进程中断也会消耗一次额度 —— 这保证「中断 → resume → 再中断」不会无限循环。
451	- `rework_count`：**返工**次数。每次 `REVIEW → REWORK` 时 +1。覆盖「产物不合质量要求」。
452	- 两者独立计数：一个任务最多执行 `max_attempts × max_reworks` 次，上限明确，**杜绝无限驳回**。
453	- 规划阶段的 run 级重试（§7.0 的 `plan_max_attempts`）**不计入**任何任务的额度。
454	- 额度用尽 → `FAILED`，并在终端与 ledger 中输出「人工介入」提示 + 最后一次评审的 `issues` / `fix_hints`。
455	
456	---
457	
458	## 7. DAG 与调度器
459	
460	整个 run 分为**两个阶段**，中间由人工闸门 `plan` 分隔：
461	
462	| 阶段 | 执行者 | 产物 | 是否确定性 |
463	|---|---|---|---|
464	| **阶段 0 · 规划**（§7.0） | `ProjectManagerAgent`（LLM） | 任务 DAG → 写入 `TaskBoard` | 否（但输出经 §7.2 严格校验） |
465	| **阶段 1 · 执行**（§7.3） | `Scheduler`（无 LLM） | 逐任务产物 + 最终交付 | 是 |
466	
467	> 这条划分是 A1 修正的落地：**调度权归 Scheduler，规划权归 PM Agent**，两者不重叠。PM 只在阶段 0（拆解）与门禁 G2（评审）被调用，`Scheduler` 全程不感知 LLM。
468	
469	### 7.0 规划阶段
470	
471	```python
472	async def plan(self) -> list[Task]:
473	    for attempt in range(1, self.settings.plan_max_attempts + 1):   # 默认 2
474	        raw = await self._pm_agent.decompose(self._goal, feedback=self._last_errors)
475	        try:
476	            tasks = parse_plan(raw)          # 结构化 → list[Task]
477	            validate_plan(tasks, self._catalog)   # §7.2 全部 7 条校验
478	            tasks = await self._gatekeeper.human_plan_gate(tasks)   # G3 卡点一（§10.3）
479	            return tasks
480	        except PlanValidationError as exc:
481	            self._last_errors = exc.errors   # 把具体错误回灌给 PM 重新拆解
482	            self._ledger.emit("plan_rejected", attempt=attempt, errors=exc.errors)
483	    raise PlanFailedError(self._last_errors)   # → 退出码 2
484	```
485	
486	规划阶段**不消耗**任何任务的 `attempts` 额度（它是 run 级重试，上限 `plan_max_attempts`）。
487	
488	**终验任务 `T-FINAL`**：规划成功后，自动追加一个由 `project_manager` 承担的收尾任务，`dependencies` = 所有无下游的任务，产物为 `DELIVERY.md`（交付总结：完成清单、验收结论、遗留问题、运行方式）。这让原需求三.1 的「PM 验收成果」落到实处，且复用了普通任务管线的全部门禁。评审人：`architect` + `product_manager` 双评审。
489	
490	### 7.1 任务图
491	
492	`agentcrew/task/graph.py` 提供纯函数，全部无副作用、可单测：
493	
494	```python
495	TaskGraph.build(tasks: Sequence[Task]) -> TaskGraph      # 冻结 dataclass
496	TaskGraph.dependencies(task_id: str) -> frozenset[str]   # 未知 id 抛 KeyError
497	TaskGraph.dependents(task_id: str) -> frozenset[str]     # 未知 id 抛 KeyError
498	TaskGraph.descendants(task_id: str) -> set[str]          # 传递闭包，用于失败传播
499	TaskGraph.cycles() -> list[list[str]]                    # Kahn 剥离 + 简单环枚举，返回所有环
500	TaskGraph.topo_order() -> list[str]                      # 有环时抛 CyclicGraphError（带 .cycles）
501	
502	ready_ids(tasks: Mapping[str, Task]) -> list[str]        # PENDING 且全部依赖 SUCCEEDED，按 id 升序
503	blocked_ids(tasks: Mapping[str, Task]) -> list[str]      # PENDING 且任一依赖 FAILED/CANCELLED
504	```
505	
506	> 命名说明：图算法以 `TaskGraph` 的**方法**与两个**模块级函数**暴露（`cycles()` 而非 `detect_cycles`），字段 `dependents_map` 带 `_map` 后缀以避免与 `dependents(task_id)` 方法同名冲突。§7.2 的校验 4 调用 `TaskGraph.cycles()`。
507	
508	### 7.2 拆解结果入库校验（`planner.py`）
509	
510	PM 返回的拆解方案在建图前必须通过以下校验，任一不过则把**具体错误**回灌给 PM 重新拆解（最多 2 次，仍不过则整次 run 以 `FAILED` 结束）：
511	
512	1. 任务 `id` 唯一且非空。
513	2. `role` 必须命中 `RoleCatalog` 中已注册的 key。
514	3. `dependencies` 中每个 id 必须存在于本次拆解中，且**不得自引用**。
515	4. 无环（`detect_cycles` 返回空）。
516	5. **产物路径唯一性**（C2）：所有任务的 `deliverables[].path` 两两不冲突。
517	6. 每个 `ArtifactSpec.path` 通过 `utils.files.safe_relpath()` 校验（相对路径、无 `..`、无盘符、无 Windows 保留名）。
518	7. `acceptance_criteria` 非空。
519	
520	> 校验 5/6 在**拆解阶段**就拦截，避免执行到一半才发现路径冲突。
521	
522	### 7.3 调度循环（`scheduler.py`）
523	
524	```python
525	async def run(self) -> None:
526	    while not self._board.is_terminal():
527	        self._check_cancel()
528	        self._refresh_blocked()                    # BLOCKED ⇄ READY 重算
529	
530	        launched = 0
531	        for task in self._board.take_ready(limit=self._free_slots()):
532	            self._spawn(task); launched += 1
533	
534	        if self._running:
535	            done = await self._await_any_completion()   # asyncio.Queue 回流
536	            await self._handle_completion(done)
537	        elif launched == 0:
538	            # 无就绪、无在跑，但仍有未终结任务 → 死锁/全阻塞
539	            self._deadlock = self._board.explain_stall()
540	            break
541	
542	    await self._finalize()          # 生成运行报告 + 退出码（§7.4）
543	
544	async def _worker(self, task: Task) -> None:
545	    """一个任务的完整生命周期。注意槽位的获取与释放边界。"""
546	    async with self._task_slot:                  # ① 占用任务槽位
547	        try:
548	            result = await self._agent(task).execute(task, pack, fix_hints=hints)
549	        finally:
550	            pass                                 # ② 离开 with 即释放槽位
551	    # ③ 门禁在槽位释放之后执行 —— 关键：避免「执行占用槽位 → 评审又要槽位」的自锁
552	    review = await self._gatekeeper.evaluate(task, result)
553	    self._completion_queue.put_nowait((task, result, review))
554	```
555	
556	要点：
557	
558	- **两级并发限额，且互不嵌套**（这是上一版设计的自锁缺陷，已修正）：
559	  - `max_concurrency`（默认 4）：同时处于 `RUNNING` 的任务数上限，由 `self._task_slot` 控制。
560	  - **LLM 调用级信号量**：由 `ModelFactory` 持有，`provider.complete()` 内部获取/释放，默认容量同为 `max_concurrency`，保证全局 API 并发可控（G2 评审的 LLM 调用也计入）。
561	  - 门禁在任务槽位**释放后**执行，因此评审所需的 LLM 信号量永远不可能被执行阶段持有 —— 无循环等待，**无死锁**。
562	- **就绪排序**：`(priority, id)` 升序，保证同优先级下顺序确定、可复现。
563	- **完成回流**：worker 完成后把 `(task, result, review)` 推入 `asyncio.Queue`；调度循环 `await queue.get()`，**无轮询、无忙等**。
564	- **状态流转集中在调度循环**：worker 只负责执行与评审，**不直接改任务状态**；所有 `transition()` 由 `_handle_completion` 统一发起，保证单点写入（§3.1 原则 3）。
565	- **失败传播**：任务 `FAILED` 后，`descendants()` 中所有未开始的任务在下一轮 `_refresh_blocked()` 时转 `BLOCKED`；已经是 `SUCCEEDED` 的下游不受影响。
566	- **取消**：`cancel_event` 置位后不再派发新任务；在跑的 worker 在**当前 LLM 调用返回后**停止（协作式），立即落盘 checkpoint，run 状态置 `INTERRUPTED`（§13.4）。
567	
568	### 7.4 停滞判定与退出码（C3）
569	
570	`explain_stall()` 在「无 READY、无 RUNNING、仍有未终结任务」时被调用，返回人类可读诊断（哪些任务被哪些失败的上游阻塞、哪些任务依赖了不存在的 id）。随后：
571	
572	| run 结束原因 | 退出码 |
573	|---|---|
574	| 全部 `SUCCEEDED` | `0` |
575	| 存在 `FAILED` / `BLOCKED` | `1` |
576	| 拆解校验连续失败 | `2` |
577	| 配置/依赖错误（无 Key、包缺失、预算超窗口） | `3` |
578	| 用户中断或取消（`Ctrl-C` → `INTERRUPTED`；`cancel` → `CANCELLED`） | `130` |
579	| 停滞（死锁） | `4` |
580	
581	退出时**必定**打印：任务终态统计表、失败任务清单及其最后一次评审意见、产物目录绝对路径。
582	
583	---
584	
585	## 8. Agent 层与角色目录
586	
587	### 8.1 BaseAgent（模板方法，满足六.1/六.2）
588	
589	`BaseAgent` 承担所有通用能力，子类**只实现钩子**，避免 8 份样板代码：
590	
591	```python
592	class BaseAgent(ABC):
593	    spec: ClassVar[RoleSpec]
594	
595	    async def execute(self, task: Task, pack: ContextPack, *, fix_hints: list[str]) -> TaskResult:
596	        """模板方法 —— 子类不重写。"""
597	        self._ctx.ledger.emit("task_started", task_id=task.id, role=self.spec.key)
598	        messages = self.build_messages(task, pack, fix_hints)      # ← 钩子
599	        raw = await self._call_model(messages)                      # 通用：重试 + 降级 + 记账
600	        parsed = self.parse_output(raw, task)                       # ← 钩子（默认走围栏解析）
601	        self.validate_output(parsed, task)                          # ← 钩子（默认走契约）
602	        record = await self._store.commit(parsed, task)             # 通用：安全落盘
603	        return TaskResult(...)
604	
605	    # ---- 子类可覆写的钩子（均提供默认实现）----
606	    def build_messages(self, task, pack, fix_hints) -> list[Message]: ...
607	    def parse_output(self, raw: str, task: Task) -> ParsedOutput: ...
608	    def validate_output(self, parsed: ParsedOutput, task: Task) -> None: ...
609	    def summarize(self, result: TaskResult) -> str: ...             # 供下游上下文使用
610	```
611	
612	**8 个角色子类**的典型差异仅在于 `build_messages()` 追加岗位特有的输出要求（如 QA 必须给出可执行的测试命令、DevOps 必须给出健康检查端点）。`RoleSpec.system_prompt` 承载 90% 的角色差异。
613	
614	### 8.2 内置角色目录（对齐原需求三）
615	
616	| key | 岗位 | 核心职责 | 主要交付物 | 默认评审人 |
617	|---|---|---|---|---|
618	| `project_manager` | 项目经理 | 目标拆解、计划制定、终验 | `plan`（结构化，不落盘为文件） | — （自持终验权） |
619	| `product_manager` | 产品经理 | 需求分析、验收标准 | `docs/PRD.md`, `docs/acceptance.md` | project_manager |
620	| `architect` | 架构师 | 技术选型、模块划分、接口定义 | `docs/architecture.md`, `docs/api-spec.yaml` | project_manager |
621	| `backend_dev` | 后端开发工程师 | 业务逻辑、数据模型、API 实现 | `backend/app/**/*.py` | architect |
622	| `frontend_dev` | 前端开发工程师 | 页面、交互、样式 | `frontend/**/*.{html,css,js}` | architect |
623	| `qa_engineer` | 测试工程师 | 测试用例、执行测试、Bug 报告 | `tests/**/*.py`, `docs/test-report.md` | backend_dev |
624	| `devops_engineer` | 运维工程师 | 部署方案、Docker、CI/CD | `Dockerfile`, `docker-compose.yml`, `docs/deploy.md` | architect |
625	| `technical_writer` | 文档工程师 | README、使用文档、接口文档 | `README.md`, `docs/usage.md` | product_manager |
626	
627	评审人映射存于 `RoleSpec.reviewer_for` 的**反向表**：`RoleCatalog.reviewer_for(role_key) -> role_key`，默认回落到 `project_manager`；`project_manager` 本身由 `architect` + `product_manager` 联合评审（两者都 approve 才算过）。
628	
629	### 8.3 自定义角色（对应原需求二.1「支持自定义扩展」）
630	
631	两种方式，均**无需改动引擎代码**：
632	
633	1. **配置文件**：在 `agentcrew.yaml` 的 `roles:` 段新增条目，或把 YAML 放进 `roles.d/` 目录（通过 `Settings.roles_dir` 指定），启动时自动加载。
634	2. **代码注册**：`crew.register_role(RoleSpec(...))`。
635	
636	新增角色的 Agent 行为默认由 `GenericAgent`（`BaseAgent` 的通用子类）承载，行为完全由 `RoleSpec` 驱动。只有当角色需要特殊的输出解析逻辑时，才需要一个 `.py` 子类。
637	
638	> `python main.py roles` 列出全部已注册角色及其职责/边界/交付物。
639	
640	---
641	
642	## 9. 产物契约与安全文件操作
643	
644	### 9.1 Agent 产物输出协议
645	
646	Agent 的回复中，**只有带 `file=` 头的围栏代码块会被落盘**，其余文字视为说明，记入 `TaskResult.notes` 与 ledger，**不写文件**：
647	
648	````
649	说明文字……（进 notes）
650	
651	```python file=backend/app/main.py
652	from fastapi import FastAPI
653	...
654	```
655	
656	```markdown file=README.md
657	# Todo App
658	...
659	```
660	````
661	
662	解析规则（`artifacts/parser.py`）：
663	
664	- 起始围栏正则：`^```(?P<lang>[A-Za-z0-9_+.-]*)(?:\s+file=(?P<path>\S+))?\s*$`
665	- 需要**成对**围栏；未闭合的块 → 解析错误（`RUNNING → READY` 重试）。
666	- `file=` 值中的反斜杠统一规范化为 `/`。
667	- 同一个 `file=` 在一条回复中出现多次 → 取**最后一个**，并记 warning 到 ledger。
668	- `file=` 指向的路径**必须**在任务的 `deliverables` 契约中声明；未声明的产物 → 直接判为 G1 失败（防止 Agent 越权写文件，也防止契约漂移）。
669	- 契约中声明为 `required=True` 但回复中缺失的产物 → G1 失败。
670	
671	语法高亮语言名与 `ArtifactKind` 的映射表在 `parser.py` 中维护（`py`/`python` → `python`，`yml`/`yaml` → `yaml`，无语言名时按契约 `kind` 兜底）。
672	
673	### 9.2 并发写冲突防护（C2）
674	
675	- 拆解阶段已保证同一 run 内产物路径两两不冲突（§7.2 校验 5）。
676	- `ArtifactStore.commit()` 落盘前再次查询 run 内已写入记录：若同一路径已由**其他任务**写入 → 抛 `ArtifactConflictError`，任务转 `REVIEW` 后由 G1 判失败（防御性二次检查）。
677	- 同一任务返工重写自己的产物是允许的，会覆盖并更新 `MANIFEST.json` 中的 sha256。
678	
679	### 9.3 安全落盘（B3）
680	
681	`agentcrew/utils/files.py`：
682	
683	```python
684	def safe_relpath(raw: str) -> PurePosixPath:
685	    """校验并规范化一个相对路径。任一条不过即抛 UnsafePathError。"""
686	    # 1. 非空、无 NUL 字符
687	    # 2. 拒绝绝对路径（/ 开头、盘符 C:\、UNC \\server）
688	    # 3. 拒绝任何 ".." 片段
689	    # 4. 拒绝 Windows 保留名（CON PRN AUX NUL COM1-9 LPT1-9，含带扩展名形式）
690	    # 5. 拒绝结尾的空格/点（Windows 会静默截断）
691	    # 6. 规范化重复分隔符与 "." 片段
692	    # 7. 拒绝空路径或仅剩 "." 的路径
693	```
694	
695	```python
696	class ArtifactStore:
697	    def __init__(self, root: Path, ledger: Ledger) -> None: ...
698	
699	    def resolve(self, rel: str) -> Path:
700	        rel_p = safe_relpath(rel)
701	        candidate = (self.root / rel_p).resolve()
702	        root_real = self.root.resolve()
703	        if not candidate.is_relative_to(root_real):     # 二次确认（防符号链接绕过）
704	            raise UnsafePathError(rel)
705	        self._reject_symlink_parents(candidate, root_real)
706	        return candidate
707	
708	    def commit(self, rel: str, content: str, *, task: Task) -> ArtifactRecord:
709	        path = self.resolve(rel)
710	        data = content.encode("utf-8")
711	        if len(data) > spec.max_bytes: raise ArtifactTooLargeError(...)
712	        path.parent.mkdir(parents=True, exist_ok=True)
713	        tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
714	        tmp.write_bytes(data)                            # 同目录临时文件
715	        os.replace(tmp, path)                            # 原子替换（Windows 亦可）
716	        ...
717	```
718	
719	要点：
720	- **原子写**：临时文件与目标**同目录**（跨盘 `os.replace` 会失败），`os.replace` 在 POSIX 与 Windows 上均为原子操作。
721	- **符号链接**：逐级检查 `candidate` 到 `root` 之间的每一段都**不是**符号链接（防 `output/link -> C:\Windows` 类绕过）。
722	- **编码**：统一 UTF-8，写入不加 BOM。
723	- **清单**：每次 `commit` 后更新 `artifacts/MANIFEST.json`（原子写）。
724	
725	---
726	
727	## 10. 质量门禁（B1 / B10）
728	
729	`Gatekeeper.evaluate(task, result) -> ReviewRecord`，三层**顺序执行，任一层 reject 即短路**。
730	
731	### 10.1 G1 — 静态校验（确定性，零 token）
732	
733	| 检查 | 失败信息示例 |
734	|---|---|
735	| 契约中每个 `required=True` 的产物都已产出 | `缺少必需产物：backend/app/main.py` |
736	| 未声明产物未出现 | `产出未在契约中声明的文件：backend/hack.py` |
737	| 文件字节数 `≥ min_bytes` | `backend/app/main.py 为空文件` |
738	| `.py` → `ast.parse()` 通过 | `backend/app/main.py 语法错误：line 12: invalid syntax` |
739	| `.json` / `.yaml` / `.toml` → 可解析 | `docs/api-spec.yaml 解析失败：...` |
740	| `.html/.css/.js` → 非空且不含纯占位符（`TODO`、`<placeholder>`、`...`） | `frontend/index.html 仅含占位内容` |
741	| `must_contain` 中每个字面量都出现 | `backend/app/main.py 缺少必需符号：def create_app` |
742	| `.py` 额外做 `compile()` 二次确认 | — |
743	
744	G1 全部通过 → `verdict=approve, layer="static"`。
745	
746	> 设计取舍：G1 只做「结构性」判断，**不跑**生成的测试、不启动服务。真正执行用户代码属于 V2 的沙箱能力（§19），V1 明确不做 —— 这既是安全边界也是复杂度边界。
747	
748	### 10.2 G2 — LLM 评审（B1）
749	
750	- **评审人选择**：`RoleCatalog.reviewer_for(task.role)`；`project_manager` 任务由 `architect` + `product_manager` 双评审（取**都 approve**）。
751	- **评审输入**：任务描述 + `acceptance_criteria` + 产物内容（按 §11 预算节选，超预算则降级为「文件路径 + 结构摘要 + 关键片段」）+ 该任务的返工历史。
752	- **评审输出**（结构化，走 §12.3 降级链）：
753	
754	```json
755	{
756	  "verdict": "approve" | "reject",
757	  "score": 0.85,
758	  "issues": ["缺少对空输入的校验"],
759	  "fix_hints": ["在 POST /todos 中对 title 做非空校验并返回 400"]
760	}
761	```
762	
763	- **强制约束**：`verdict=reject` 时 `issues` 必须非空，否则视为评审格式错误，重试评审（不计入任务返工额度，最多 2 次；仍失败则按 approve 放行并记 `warning` —— 宁可放过也不要因评审器故障卡死全流程）。
764	- **评审成本**：G2 的 token 消耗单独记入 ledger，`reviewer_role` 字段区分。
765	
766	### 10.3 G3 — 人工闸门（CLI）
767	
768	由 `--gate` 参数控制，两个卡点：
769	
770	| 卡点 | 时机 | 呈现 | 可选项 |
771	|---|---|---|---|
772	| `plan` | PM 拆解完成并通过 §7.2 校验、开始烧 token 以前（**触发点：§7.0 `plan()` 末尾**） | 任务 DAG 表 + 预估任务数/角色分布 | `[a] 批准执行 / [e] 编辑后执行 / [s] 保存退出 / [q] 取消` |
773	| `delivery` | 全部任务 `SUCCEEDED`、封版以前（**触发点：§7.4 `_finalize()` 中**） | 产物清单 + 统计 + 评审通过率 | `[a] 接受交付 / [r] 打回指定任务返工 / [q] 放弃` |
774	
775	- `--gate none`（等价 `--auto`）跳过两层，全自动。
776	- `--gate both` 为默认值（在 TTY 环境下）；非 TTY（CI / 重定向）自动降级为 `none` 并在日志中注明。
777	- `[e] 编辑后执行` 打开 `$EDITOR` 编辑拆解结果 JSON，重新走 §7.2 校验。
778	- 人工裁决写入 `ReviewRecord(layer="human")`，完整留痕。
779	
780	---
781	
782	## 11. 上下文组装（B5）
783	
784	### 11.1 ContextPack
785	
786	```python
787	class ContextPack(BaseModel):
788	    project_goal: str
789	    task: Task
790	    upstream: list[UpstreamDigest]   # 直接依赖（非全部祖先）的摘要
791	    decisions: list[DecisionRecord]  # 全局决策记录
792	    file_tree: list[str]             # artifacts/ 目录树快照
793	    key_excerpts: list[FileExcerpt]  # 按预算挑选的文件正文节选
794	    total_estimated_tokens: int
795	    truncated: bool                  # 是否因预算发生降级
796	```
797	
798	`UpstreamDigest` = `{task_id, role, title, brief, summary, files: [path]}` —— 默认**只给摘要与文件路径，不给全文**。
799	
800	### 11.2 预算与降级（`context/budget.py`）
801	
802	默认总预算 `context_budget_tokens = 24000`。按优先级**逐级装入，装不下即停**：
803	
804	1. 项目目标 + 本任务描述 + 验收标准 + 返工意见 —— **不可压缩**
805	2. 直接上游的 `brief` + `summary`（每个 ≤ 400 tok，超出截断）
806	3. 全局决策记录（最近 20 条）
807	4. `file_tree`（超 300 条时折叠中间层为 `... (N more)`）
808	5. `key_excerpts`：按「被下游依赖次数 × 契约重要度」排序，逐个装入，单个文件最多占剩余预算的 40%
809	6. 仍有余量才装入更远的祖先摘要
810	
811	**降级语义**：任一项因预算被丢弃时，`truncated=True`，并在提示词末尾追加一行说明：`注意：为控制上下文长度，部分上游产物未全文展示；如需查看，请依据文件路径读取。` —— 避免模型误以为「没给的就是不存在」。
812	
813	**Token 估算**：`utils/tokens.py::estimate_tokens(text) -> int`，采用 `max(len(text) // 3, len(text.split()))` 的保守启发式（中英混排场景下偏差可接受，且**零依赖**）。预算判定用估算值，实际消耗以 API 返回的 usage 为准并记入 ledger。
814	
815	**预算与模型窗口的关系**：启动时校验 `context_budget_tokens + max_output_tokens ≤ 目标模型 max_context_tokens`，不满足则报配置错误并给出建议值（退出码 3）。由于同一 run 内不同角色可能绑定不同模型（§12.2），校验按**每个实际绑定的模型**分别进行。默认值 `24000 + 8192 = 32192`，对 128k 窗口的模型有充足余量，对 8k 窗口的本地小模型则会触发校验失败并提示调低 `AGENTCREW_CONTEXT_BUDGET`。
816	
817	### 11.3 决策记录
818	
819	当一个任务被返工 ≥1 次后，其最终 `fix_hints` 会被提取为一条 `DecisionRecord` 写入 `context/decisions.jsonl`，供下游 Agent 参考（例如「后端已确定用内存存储而非 SQLite」）。这是 V1 的轻量级「共享消息池」，对应参考项目中 MetaGPT 的订阅过滤思想，但**不做 LLM 摘要**（留 V2）。
820	
821	---
822	
823	## 12. 模型接入层
824	
825	### 12.1 Provider 能力位（B8）
826	
827	```python
828	class ProviderCapabilities(BaseModel):
829	    supports_json_schema: bool = False     # response_format={"type":"json_schema", strict:True}
830	    supports_json_object: bool = False     # response_format={"type":"json_object"}
831	    supports_tool_calls: bool = False
832	    supports_system_role: bool = True      # False 时把 system 内容并入首条 user 消息
833	    max_context_tokens: int = 128_000
834	    max_output_tokens: int = 8_192
835	```
836	
837	内置能力预设（`models/config.py`，可被 `agentcrew.yaml` 覆盖）：
838	
839	> 能力位描述的是 **OpenAI 风格的 `response_format` 机制**是否可用；不支持时结构化输出自动落到 §12.3 的第 3 级（围栏块提取），功能不受影响，仅稳定性略降。
840	
841	| provider 别名 | 实现 | base_url 默认 | json_schema | json_object | 备注 |
842	|---|---|---|---|---|---|
843	| `openai` | OpenAICompat | `https://api.openai.com/v1` | ✅ | ✅ | 基准 |
844	| `anthropic` | Anthropic 原生 | `https://api.anthropic.com` | ❌ | ❌ | 走 Messages API；结构化走围栏提取 |
845	| `deepseek` | OpenAICompat | `https://api.deepseek.com/v1` | ✅ | ✅ | |
846	| `doubao` | OpenAICompat | `https://ark.cn-beijing.volces.com/api/v3` | ❌ | ✅ | 豆包/方舟，schema 支持不稳定，降级到 json_object |
847	| `qwen` | OpenAICompat | `https://dashscope.aliyuncs.com/compatible-mode/v1` | ❌ | ✅ | 通义千问兼容模式 |
848	| `ernie` | OpenAICompat | `https://qianfan.baidubce.com/v2` | ❌ | ❌ | 文心一言走千帆 v2；**注意**：旧版需 access_token 鉴权，本版只支持千帆 v2 的 API Key 模式 |
849	| `ollama` | OpenAICompat | `http://localhost:11434/v1` | ❌ | ✅ | 本地模型，`api_key` 可为任意非空串 |
850	| `mock` | MockProvider | — | ✅ | ✅ | 离线确定性回放 |
851	
852	> 修正说明（对应 §2.2 B8）：原需求称「统一 OpenAI 兼容接口支持 文心一言」，实际文心需千帆 v2 网关且历史鉴权方式不同；本规格显式声明为「千帆 v2 API Key 模式」，并在能力位中标注其不支持 `json_schema`。
853	
854	### 12.2 角色 → 模型绑定
855	
856	三级优先级（高者胜）：
857	
858	1. `RoleSpec.model`（角色级）
859	2. `agentcrew.yaml` 的 `models.role_bindings.<role_key>`
860	3. `Settings.default_model`（默认 `openai:gpt-4o-mini`）
861	
862	模型别名格式：`"<provider>:<model_name>"`，如 `"anthropic:claude-sonnet-5"`、`"ollama:qwen2.5:14b"`（仅按**第一个**冒号切分，支持模型名含冒号）。
863	
864	`ModelFactory.get(alias) -> BaseProvider` 按别名缓存实例，避免重复建连。
865	
866	### 12.3 结构化输出降级链（B9）
867	
868	`models/structured.py::complete_structured(provider, messages, schema, **kw) -> BaseModel`：
869	
870	| 级 | 策略 | 适用条件 |
871	|---|---|---|
872	| 1 | `response_format={"type":"json_schema","json_schema":{"name":..., "schema":..., "strict":True}}` | `supports_json_schema` |
873	| 2 | `response_format={"type":"json_object"}` + 提示词中嵌入 JSON Schema | `supports_json_object` |
874	| 3 | 纯提示词 + 从回复中提取 ```json 围栏块（兜底：提取首个平衡的 `{...}`） | 总是可用 |
875	| 4 | **修复循环**：把 Pydantic 校验错误原文回灌（`你上次的输出校验失败：<错误>。请仅返回修正后的 JSON`），最多 2 次 | 1–3 级均失败 |
876	
877	- 第 4 级仍失败 → 抛 `StructuredOutputError` → 任务按 `RUNNING → READY`（`attempts < max_attempts`）重试。
878	- 每次降级在 ledger 中记一条 `llm_degraded` 事件（含 `from_level` / `to_level` / `reason`），便于事后诊断「哪个 provider 不稳定」。
879	- 所有结构化模型均继承 `pydantic.BaseModel`，`model_config = ConfigDict(extra="forbid")`，多余字段直接判错（配合修复循环可显著提升跨模型稳定性）。
880	
881	### 12.4 计费表
882	
883	`models/pricing.py` 维护 `{model_prefix: (usd_per_1m_in, usd_per_1m_out)}`，未知模型按 `(0, 0)` 计但**照常记录 token 数**。成本写入 ledger，终端看板汇总展示。
884	
885	---
886	
887	## 13. 持久化、恢复与取消（B4）
888	
889	### 13.1 目录布局（run 级命名空间，C6）
890	
891	```
892	output/<run_id>/
893	├── run.json                # RunRecord（含 stats、config_snapshot）
894	├── tasks.json              # TaskBoard 全量快照（含每个 Task 的 reviews / status_history）
895	├── ledger.jsonl            # 追加式事件日志（§14）
896	├── context/
897	│   └── decisions.jsonl     # 全局决策记录
898	├── artifacts/              # 真实交付物根目录
899	│   ├── ...（按契约路径）
900	│   └── MANIFEST.json       # 产物清单（路径/大小/sha256/产出任务）
901	└── diagnostics/
902	    └── stall-report.md     # 仅在停滞时生成（§7.4）
903	```
904	
905	`run_id` 格式：`YYYYMMDD-HHMMSS-<4位随机十六进制>`（本地时间 + `secrets.token_hex(2)`）。
906	
907	### 13.2 原子写与刷盘
908	
909	- `run.json` / `tasks.json` / `MANIFEST.json`：临时文件 + `os.replace`（同目录）。
910	- `ledger.jsonl`：追加写，每条 `flush() + os.fsync()` —— 保证 `Ctrl-C` 后最后一条事件完整。
911	- `tasks.json` 在**每次状态跃迁后**落盘（一次 run 的任务数量在几十级别，写放大可接受，换来最强的可恢复性）。
912	
913	### 13.3 resume
914	
915	`python main.py resume <run_id>`：
916	
917	1. 读 `run.json` + `tasks.json`；`schema_version` 不匹配 → 明确报错（不做静默迁移）。
918	2. **状态修复**（进程已死，内存态不可信）：
919	   - `RUNNING` → `READY`（产物可能只写了一半，必须重新执行）
920	   - `REWORK` → `READY`（同上）
921	   - `REVIEW` → 保持 `REVIEW`。产物已完整落盘，**只需重跑门禁**，不重跑 Agent；重跑门禁走的是正常 `REVIEW → {SUCCEEDED|REWORK|FAILED}` 跃迁，不需要自环。
922	   - `BLOCKED` → 保持，由正常调度重算
923	   - `attempts` / `rework_count` **一律不重置**。由于额度在派发时即扣除（§6.2），一次中断就消耗一次尝试额度，这从机制上杜绝了「中断 → resume → 中断」的无限循环。
924	3. 对比当前 `config_snapshot` 与现配置：不一致时打印 diff 并要求 `--force` 确认（防止换模型后结果不可比）。
925	4. 从 §7.3 的调度循环继续。
926	
927	### 13.4 取消
928	
929	- `Ctrl-C`（SIGINT）处理器：置 `cancel_event` → 不派发新任务 → 等在跑的任务返回 → 落盘 → `run.status = INTERRUPTED`（**不是** CANCELLED，便于直接 resume）→ 打印断点信息与 resume 命令 → 退出码 `130`。
930	- 第二次 `Ctrl-C`：立即取消所有 asyncio task，落盘后退出（在跑的任务转 `READY`）。
931	- `python main.py cancel <run_id>`：对离线 run 设置 `run.json` 的取消标记，使下次 resume 直接以 `CANCELLED` 收尾。
932	- 取消后 `RUNNING` 任务在 resume 时按 §13.3 规则回到 `READY`。
933	
934	### 13.5 清理
935	
936	`python main.py clean [--keep N] [--dry-run]`：按创建时间倒序保留最近 N 个 run（默认 5），其余删除。`--dry-run` 仅列出。删除前校验目标目录确实位于 `output/` 之下且含 `run.json`（防止误删）。
937	
938	---
939	
940	## 14. 日志与成本审计（B11）
941	
942	### 14.1 Ledger 事件
943	
944	`runtime/ledger.py` 写 `ledger.jsonl`，每行一个 JSON 对象：
945	
946	```python
947	class LedgerEvent(BaseModel):
948	    ts: datetime
949	    run_id: str
950	    event: str          # 见下表
951	    data: dict[str, Any]
952	```
953	
954	| event | 关键字段 |
955	|---|---|
956	| `run_started` / `run_finished` | `goal`, `status`, `exit_code`, `duration_ms` |
957	| `plan_created` | `task_count`, `roles`, `planner_attempts` |
958	| `task_transition` | `task_id`, `from`, `to`, `reason` |
959	| `task_started` / `task_finished` | `task_id`, `role`, `attempt`, `duration_ms` |
960	| `llm_call` | `role`, `model`, `prompt_tokens`, `completion_tokens`, `estimated_cost_usd`, `latency_ms`, `purpose`（`execute` / `review` / `plan`） |
961	| `llm_degraded` | `provider`, `from_level`, `to_level`, `reason` |
962	| `artifact_written` | `task_id`, `path`, `size_bytes`, `sha256` |
963	| `gate_result` | `task_id`, `layer`, `verdict`, `score`, `issues` |
964	| `human_decision` | `checkpoint`, `choice`, `task_ids` |
965	| `error` | `where`, `exc_type`, `message`, `task_id?` |
966	
967	### 14.2 密钥脱敏
968	
969	`utils/logger.py` 注册一个 `logging.Filter`，对任何形如 `sk-...`、`Bearer ...`、以及环境变量中出现的 key 值做 `***` 替换。**任何路径下都不得把 API Key 写进日志、ledger、异常堆栈或 payload dump。** 该行为有专门单测。
970	
971	### 14.3 终端展示
972	
973	运行结束打印汇总块：
974	
975	```
976	━━━ AgentCrew 运行报告 · 20261005-153012-a4f1 ━━━
977	任务      14 个   成功 12 · 失败 1 · 阻塞 1 · 跳过 0
978	返工      3 次（T-006 ×2, T-009 ×1）
979	LLM 调用  41 次   prompt 812k tok · completion 96k tok
980	估算成本  $0.87
981	评审通过率 92%（G1 拦截 2 / G2 驳回 3 / 人工驳回 1）
982	耗时      4m12s
983	产物      E:\agentcrew\output\20261005-153012-a4f1\artifacts
984	━━━ 失败任务 ━━━
985	T-011 后端单元测试  FAILED  最后一次评审：测试覆盖率不足（需 ≥70%，实际 41%）
986	```
987	
988	---
989	
990	## 15. CLI 与终端看板
991	
992	### 15.1 命令
993	
994	```
995	python main.py run      --goal "..." [--demo] [--mock] [--auto] [--gate MODE]
996	                        [--max-concurrency N] [--default-model ALIAS]
997	                        [--output-dir DIR] [--context-budget N]
998	python main.py resume   <run_id> [--force]
999	python main.py status   <run_id>
1000	python main.py roles
1001	python main.py clean    [--keep N] [--dry-run]
1002	python main.py cancel   <run_id>
1003	```
1004	
1005	- `--demo`：等价于 `--goal "<Todo List Web 应用的项目目标>"`，使用 `examples/todo_web_project.py` 中定义的完整目标与验收标准。与 `--goal` **互斥**；`run` 子命令要求二者**必居其一**，否则报错退出（退出码 3）。
1006	- `--mock`：强制使用 `mock` provider（覆盖所有角色绑定）。
1007	- `--auto`：等价 `--gate none`。
1008	- 无参数运行 `python main.py`：打印帮助 + 已注册角色数 + 最近 3 次 run 概览。
1009	
1010	### 15.2 终端看板（rich）
1011	
1012	`rich.live.Live` + `Layout`，刷新由 ledger 事件触发（非定时轮询）：
1013	
1014	```
1015	┌ AgentCrew ─ Todo List Web 应用 ─ 20261005-153012-a4f1 ─ 运行中 00:02:41 ┐
1016	│ 任务看板                                                                  │
1017	│ ID    标题              角色          状态      依赖      尝试 返工 耗时  │
1018	│ T-001 需求分析          产品经理      ✓ 完成    —         1    0   18s   │
1019	│ T-002 架构设计          架构师        ✓ 完成    T-001     1    1   24s   │
1020	│ T-003 后端 API 实现     后端开发      ⟳ 进行中  T-002     2    0   51s   │
1021	│ T-004 前端页面实现      前端开发      ⟳ 进行中  T-002     1    0   44s   │
1022	│ T-005 测试用例编写      测试工程师    ◷ 待分配  T-002     0    0   —     │
1023	│ ...                                                                       │
1024	├───────────────────────────────────────────────────────────────────────────┤
1025	│ 进度 ████████████░░░░░░░░  7/14   并发 2/4   返工 3                       │
1026	│ 成本 $0.41 · 908k tok · 最近事件：T-003 重试（第 2 次）                    │
1027	└───────────────────────────────────────────────────────────────────────────┘
1028	```
1029	
1030	- 状态色：`SUCCEEDED` 绿、`RUNNING`/`REVIEW` 黄、`REWORK` 品红、`FAILED` 红、`BLOCKED`/`SKIPPED`/`CANCELLED` 灰。
1031	- 日志输出与 `Live` **共用同一个 `Console`**（否则会撕裂画面）；在非 TTY 环境自动降级为纯行日志（`Live` 关闭）。
1032	- `--no-live` 显式关闭看板（CI 友好）。
1033	
1034	---
1035	
1036	## 16. Mock 模式与示例（A2 / S2 / S4 / S5）
1037	
1038	### 16.1 MockProvider 语义
1039	
1040	`MockProvider` 是**确定性回放**，不是随机伪造：
1041	
1042	- 按 `purpose` + `role` + `task.attempt` 从 `examples/fixtures/` 查找匹配的预置响应：
1043	  - `plan_todo.json` —— PM 拆解结果（结构化 JSON，走第 1/2 级）
1044	  - `responses/<task_id>/attempt-<n>.md` —— 该任务第 n 次尝试的 **LLM 原始回复**，内容就是普通的带 `file=` 围栏块的文本（走第 3 级）
1045	  - `reviews.json` —— G2 评审结论（结构化 JSON）
1046	- 关键设计：**mock 走与真实模型完全相同的数据通路** —— 围栏解析、契约校验、安全落盘、门禁，一条都不跳过。因此 mock 能真正验证管线，而不只是「假装成功」。
1047	- 部分任务（如 `T-003`）的 `attempt-1.md` **故意不合格**（缺 `must_contain` 符号），用于演示 G1 驳回 → 返工 → `attempt-2.md` 通过 的完整闭环。
1048	- 部分任务有 `attempt-2.md` 但 `attempt-1.md` 缺失时，视为「任何尝试都返回同一内容」，简化 fixture 维护。
1049	- 未匹配到时，返回一个「结构合法但内容为占位」的响应，并在 ledger 记 `mock_fallback` 事件（**绝不静默**）。
1050	- 记录了 token 用量（按字符估算）与固定延迟（默认 0，可用 `AGENTCREW_MOCK_LATENCY_MS` 模拟），使看板与成本统计在 mock 下也真实可演示。
1051	
1052	### 16.2 离线演示的诚实性
1053	
1054	README 与运行报告必须**明确标注**：mock 模式下的产物来自 `examples/fixtures/responses/` 中**手写的预置回复**，**不是模型生成的**。mock 的用途是「离线演示编排流程 + 支撑 hermetic 测试」，不是「证明模型能力」。真实生成能力需 `--goal` + 真实 API Key 验证。
1055	
1056	`examples/fixtures/responses/` 中预置的 Todo 项目是**手写的、真实可运行的**。它按契约路径分布在各任务的围栏块里，运行后落到 `output/<run_id>/artifacts/`：
1057	
1058	```
1059	（artifacts/ 下的最终形态，由下列 fixture 回复渲染而成）
1060	├── README.md                                  ← T-FINAL / technical_writer
1061	├── Dockerfile, docker-compose.yml             ← devops_engineer
1062	├── docs/{PRD.md,acceptance.md}                ← product_manager
1063	├── docs/{architecture.md,api-spec.yaml}       ← architect
1064	├── docs/{test-report.md,deploy.md,usage.md}   ← qa_engineer / devops_engineer / technical_writer
1065	├── DELIVERY.md                                ← project_manager（T-FINAL）
1066	├── backend/
1067	│   ├── app/{__init__.py,main.py,models.py,schemas.py,store.py,config.py}
1068	│   └── requirements.txt
1069	├── frontend/{index.html,app.js,styles.css}
1070	└── tests/{test_api.py,test_store.py}
1071	```
1072	
1073	技术栈：FastAPI + Pydantic v2 + 内存存储（零外部依赖，便于演示）、原生 HTML/CSS/JS 前端、pytest + httpx 测试、多阶段 Dockerfile + docker-compose。
1074	
1075	**产物可运行性验收（人工，不进 pytest）**：
1076	
1077	```bash
1078	cd output/<run_id>/artifacts/backend
1079	pip install -r requirements.txt
1080	uvicorn app.main:app --reload
1081	# 浏览器打开 http://127.0.0.1:8000/  → 增删改查 Todo 应正常工作
1082	```
1083	
1084	`tests/test_end_to_end_mock.py` 只做静态断言（文件存在、`.py` 可 `ast.parse`、契约符号齐全），**不启动服务、不执行产物代码** —— 与 §19.3 已知限制 1 一致。这既是安全边界（不在宿主上跑 LLM 生成的代码），也是测试 hermetic 性的保证。
1085	
1086	### 16.3 验收测试
1087	
1088	`tests/test_end_to_end_mock.py`：以 `tmp_path` 为输出根跑完整 mock 流程，断言 —— 全部任务 `SUCCEEDED`（或按 fixture 预期出现指定数量的返工）、`MANIFEST.json` 中每个文件存在、所有 `.py` 可 `ast.parse`、`run.json` 的 `status == completed`、ledger 中 `llm_call` 事件数 > 0。**全程零网络。**
1089	
1090	---
1091	
1092	## 17. 配置管理
1093	
1094	### 17.1 配置来源与优先级
1095	
1096	`agentcrew/config.py::Settings`，优先级从低到高：
1097	
1098	1. 代码内默认值
1099	2. `.env`（经 `python-dotenv` 加载）
1100	3. `agentcrew.yaml`（可选，工作目录或 `AGENTCREW_CONFIG` 指定）
1101	4. 环境变量（`AGENTCREW_*`）
1102	5. CLI 参数
1103	
1104	### 17.2 环境变量（`.env.example`）
1105	
1106	```dotenv
1107	# ── 模型 Provider API Key（至少配一个；--mock 模式全都不需要）──
1108	OPENAI_API_KEY=
1109	ANTHROPIC_API_KEY=
1110	DEEPSEEK_API_KEY=
1111	ARK_API_KEY=                 # 豆包 / 火山方舟
1112	DASHSCOPE_API_KEY=           # 通义千问
1113	QIANFAN_API_KEY=             # 文心一言（千帆 v2）
1114	OLLAMA_BASE_URL=http://localhost:11434/v1
1115	
1116	# ── 默认模型：<provider>:<model> ──
1117	AGENTCREW_DEFAULT_MODEL=openai:gpt-4o-mini
1118	
1119	# ── 按角色覆盖模型（可选，留空则继承默认）──
1120	AGENTCREW_MODEL_PROJECT_MANAGER=
1121	AGENTCREW_MODEL_PRODUCT_MANAGER=
1122	AGENTCREW_MODEL_ARCHITECT=
1123	AGENTCREW_MODEL_BACKEND_DEV=
1124	AGENTCREW_MODEL_FRONTEND_DEV=
1125	AGENTCREW_MODEL_QA_ENGINEER=
1126	AGENTCREW_MODEL_DEVOPS_ENGINEER=
1127	AGENTCREW_MODEL_TECHNICAL_WRITER=
1128	
1129	# ── 运行时 ──
1130	AGENTCREW_MAX_CONCURRENCY=4
1131	AGENTCREW_OUTPUT_DIR=output
1132	AGENTCREW_GATE=both            # both | plan | delivery | none
1133	AGENTCREW_CONTEXT_BUDGET=24000
1134	AGENTCREW_LOG_LEVEL=INFO
1135	AGENTCREW_MOCK=false
1136	AGENTCREW_MOCK_LATENCY_MS=0
1137	```
1138	
1139	### 17.3 `agentcrew.yaml`（可选）
1140	
1141	```yaml
1142	max_concurrency: 4
1143	context_budget_tokens: 24000
1144	gate: both
1145	roles_dir: roles.d            # 自定义角色目录（可选）
1146	models:
1147	  default: openai:gpt-4o-mini
1148	  role_bindings:
1149	    architect: anthropic:claude-sonnet-5
1150	    qa_engineer: deepseek:deepseek-chat
1151	  providers:                   # 覆盖内置能力位
1152	    doubao:
1153	      capabilities: { supports_json_schema: false, supports_json_object: true }
1154	```
1155	
1156	### 17.4 `.gitignore`
1157	
1158	```
1159	.env
1160	.env.*
1161	!.env.example
1162	output/
1163	__pycache__/
1164	*.py[cod]
1165	.pytest_cache/
1166	.venv/
1167	venv/
1168	*.egg-info/
1169	.ruff_cache/
1170	.mypy_cache/
1171	```
1172	
1173	### 17.5 `requirements.txt`（修正 A3）
1174	
1175	```
1176	# 运行时
1177	openai>=1.40.0          # 同时用于 OpenAI 兼容端点（豆包/通义/DeepSeek/Ollama）
1178	anthropic>=0.34.0       # Anthropic 原生 Messages API
1179	python-dotenv>=1.0.0
1180	pydantic>=2.7.0
1181	rich>=13.7.0
1182	PyYAML>=6.0.1
1183	
1184	# 测试（纳入主清单，保证 `pip install -r requirements.txt && pytest` 一次成功）
1185	pytest>=8.0.0
1186	pytest-asyncio>=0.23.0
1187	```
1188	
1189	修正说明：删除原清单中的 `asyncio`（标准库，非 pip 包）；新增 `anthropic`（原需求要 Claude 支持，OpenAI 兼容端点对 Claude 的支持有限）、`PyYAML`（`agentcrew.yaml` 与自定义角色）、`pytest`/`pytest-asyncio`（关键词承诺的自动化测试）。
1190	
1191	`pyproject.toml` 配置：`asyncio_mode = "auto"`、测试路径、`ruff`/`mypy` 的宽松起步配置。
1192	
1193	### 17.6 重试策略（B7）
1194	
1195	`utils/retry.py` 原生实现（**不引入 tenacity**，符合原需求四「禁用第三方框架」的精神）：
1196	
1197	```python
1198	async def retry_async[T](
1199	    fn: Callable[[], Awaitable[T]],
1200	    *,
1201	    attempts: int = 4,
1202	    base_delay: float = 1.0,
1203	    max_delay: float = 30.0,
1204	    jitter: float = 0.3,
1205	    retry_on: tuple[type[Exception], ...] = (TransientError,),
1206	    on_retry: Callable[[int, Exception, float], None] | None = None,
1207	) -> T
1208	```
1209	
1210	- 指数退避：`delay = min(max_delay, base_delay * 2**(n-1))`，叠加 `±jitter` 随机抖动（防多 Agent 同步重试打爆限流）。
1211	- **可重试**：网络超时、连接错误、HTTP 429/500/502/503/504。
1212	- **不可重试**（快速失败）：HTTP 400/401/403/404/422（配置或鉴权错误，重试无意义）。
1213	- 尊重 `Retry-After` 响应头。
1214	- 每次重试通过 `on_retry` 记入 ledger。
1215	
1216	---
1217	
1218	## 18. 测试策略（A3 / S4）
1219	
1220	**全部测试零网络、零 API Key、零外部服务。**
1221	
1222	| 文件 | 覆盖内容 | 关键断言 |
1223	|---|---|---|
1224	| `test_state_machine.py` | 全部合法跃迁；抽样非法跃迁 | 非法跃迁抛 `IllegalTransitionError`；状态只能经 `transition()` 修改 |
1225	| `test_graph.py` | Kahn 拓扑、环检测、就绪集、后代集合 | 自环/二元环/三元环均被检出；拓扑序满足所有依赖约束 |
1226	| `test_task_board.py` | 事实源一致性、并发访问、快照往返 | 快照 → 反序列化 → 状态完全一致 |
1227	| `test_planner.py` | §7.2 全部 7 条校验 | 重复 id、未知 role、悬空依赖、环、路径冲突、非法路径、空验收标准各有用例 |
1228	| `test_scheduler.py` | 并发上限、依赖顺序、失败传播、取消、停滞诊断 | 并发峰值 ≤ `max_concurrency`；FAILED 的下游全部转 BLOCKED；取消后无新派发 |
1229	| `test_artifact_parser.py` | 围栏解析 | 未闭合块报错；未声明路径报错；重复 `file=` 取最后一个；无语言名兜底 |
1230	| `test_artifact_store.py` | **路径安全**（重点） | 拒绝 `../`、绝对路径、`C:\`、UNC、`\\?\`、Windows 保留名、符号链接父目录、尾随空格/点；断言文件未落盘 |
1231	| `test_contract.py` | G1 全部检查项 | 空文件、语法错误、JSON 不可解析、缺 `must_contain`、超 `max_bytes`、多余文件 |
1232	| `test_gatekeeper.py` | 三层编排、短路、重试上限 | G1 拒绝时不调用 LLM；驳回达 `max_reworks` 后转 FAILED；评审格式错误的重试不计入返工额度 |
1233	| `test_structured_output.py` | 四级降级链 | 能力位正确决定起始级别；校验错误触发修复循环；修复 2 次仍失败抛 `StructuredOutputError` |
1234	| `test_checkpoint.py` | 原子写、resume 状态修复、schema 版本校验、clean 安全性 | RUNNING→READY、REWORK→READY、REVIEW 保持；`--keep 0` 不删非 run 目录 |
1235	| `test_context_budget.py` | 预算降级顺序与截断标记 | 超预算时 `truncated=True`；不可压缩项永不丢弃；降级顺序符合 §11.2 |
1236	| `test_roles_catalog.py` | 8 角色完整性、key 唯一、评审人映射无环 | 每个角色有 goal/responsibilities/boundaries/system_prompt；`reviewer_for` 不产生自评审死锁 |
1237	| `test_agents_contract.py` | 8 个 Agent 在 mock 下的输出契约 | 每个角色都能产出契约要求的产物；围栏块语言标注正确 |
1238	| `test_mock_fixtures.py` | **fixture 自洽性**（防 fixture 腐化） | `plan_todo.json` 中每个任务的每个 `required` 产物，在 `responses/<task_id>/` 中均有对应围栏块；所有 `.py` 产物 `ast.parse` 通过；所有 `.json`/`.yaml` 可解析；`must_contain` 全部命中；**至少一个任务存在 `attempt-1` 不合格 + `attempt-2` 合格的返工闭环** |
1239	| `test_mock_no_bypass.py` | mock 无旁路（S9） | 用 `MockProvider` 跑通一个任务后，断言 `ArtifactStore.commit`、`contract.validate`、`Gatekeeper.evaluate` 均被调用过（monkeypatch 计数） |
1240	| `test_scheduler_deadlock.py` | 评审不自锁（§7.3 修正点） | 在 `max_concurrency=1` 下跑完含 G2 评审的流程，断言不超时、不死锁 |
1241	| `test_end_to_end_mock.py` | 端到端（§16.3） | 见 §16.3 |
1242	| `test_logger_redaction.py` | 密钥脱敏 | `sk-xxx`、`Bearer xxx`、env 中的 key 值均不出现在任何日志输出中 |
1243	
1244	**测试工具约定**：`conftest.py` 提供 `tmp_workspace`（临时 output 根）、`mock_provider`、`sample_task`、`sample_role` 等 fixture。异步测试由 `pytest-asyncio` 的 `asyncio_mode = "auto"` 驱动。
1245	
1246	---
1247	
1248	## 19. V1 范围边界与 V2 留口
1249	
1250	### 19.1 V1 内（本规格全部实现）
1251	
1252	调度引擎、状态机、DAG、8 角色、三层门禁、产物契约与安全落盘、上下文包与预算、多 Provider（含 mock）、JSON 持久化与 resume/cancel、终端看板、ledger 与成本统计、CLI、离线测试套件、Todo 演示与 fixtures。
1253	
1254	### 19.2 V1 明确不做（原需求十，均为 V2）
1255	
1256	| V2 项 | V1 预留的接口 |
1257	|---|---|
1258	| 外部 CLI 引擎接入（Claude Code / Codex / Cursor） | `BaseProvider` 抽象已隔离调用方式，新增 `CliProvider` 即可；不改引擎 |
1259	| MCP 协议互操作 | `BaseProvider`/`BaseAgent` 的边界清晰，工具层可后挂 |
1260	| HTTP API + WebSocket | `Crew` 已通过 `Ledger` 事件流解耦 UI；`board_view.py` 换实现即可 |
1261	| LLM 摘要式上下文压缩 | `context/budget.py` 的降级链已预留第 6 级（LLM 摘要）挂载点 |
1262	| 树状任务拆解 | `TaskGraph` 是通用 DAG，父子层级可作为边的子集表达 |
1263	| 按认知复杂度自动选模型 | `ModelFactory` 已支持别名解析，增加复杂度评估器即可 |
1264	| Web UI 交互面板 | `Gatekeeper` 的 G3 已抽象为「询问 → 决策」，换 UI 实现即可 |
1265	| 沙箱内真实执行生成代码 | G1 的 `static_checks` 已模块化，可插入 `sandbox_checks` |
1266	
1267	### 19.3 已知限制（须在 README 中声明）
1268	
1269	1. **不执行生成的代码**：G1 只做静态校验，不运行产物测试、不启动服务。产物「可运行」由 §S5 的人工验收步骤确认。
1270	2. **Token 估算为启发式**：预算判定用字符启发式，与真实 tokenizer 存在偏差；成本统计以 API 返回的 usage 为准。
1271	3. **文心一言仅支持千帆 v2**：旧版 access_token 鉴权不在支持范围。
1272	4. **单进程**：调度器为单进程 asyncio，不支持多机分布式执行。
1273	5. **mock 产物非模型生成**：见 §16.2。
1274	
1275	---
1276	
1277	## 20. 交付验收清单
1278	
1279	实现完成后逐条核验（对应 §1.2）：
1280	
1281	- [ ] **S1** 干净虚拟环境 `pip install -r requirements.txt` 成功
1282	- [ ] **S2** `python main.py run --demo --mock --auto` 无 Key 跑完，退出码 0
1283	- [ ] **S3** `python main.py run --goal "..." --auto` 真实模型跑通（需 Key 的人工验证项）
1284	- [ ] **S4** `pytest` 全绿，`-p no:cacheprovider` 下无任何网络请求
1285	- [ ] **S5** mock 运行产出的 `artifacts/` 中 Todo 应用可 `uvicorn` 启动并正常使用
1286	- [ ] **S6** 运行中 `Ctrl-C`，`resume <run_id>` 后继续完成
1287	- [ ] **S7** 通过 `agentcrew.yaml` 新增一个角色并跑通，引擎代码零改动
1288	- [ ] **S8** 8 个角色文件齐备、各有独立 `RoleSpec` 与单测
1289	- [ ] **S9** mock 无旁路：`test_mock_no_bypass.py` 断言落盘/契约/门禁均被真实调用
1290	- [ ] `python main.py roles` 正确列出 8 个内置角色及职责/边界/交付物
1291	- [ ] `python main.py status <run_id>` 能读出已完成 run 的统计
1292	- [ ] 原需求七.1 所有文件非空、可直接运行
1293	- [ ] 原需求七.4 终端看板实时展示分工/进度/状态
1294	- [ ] 原需求七.7 README 含：项目介绍、角色说明、快速开始、自定义角色/团队开发指南
1295	- [ ] §19.3 全部已知限制已写入 README
1296	
1297	---
1298	
1299	## 附：与原提示词的对照速查
1300	
1301	| 原需求条目 | 本规格落地位置 |
1302	|---|---|
1303	| 二.1 角色分工体系 / 自定义扩展 | §5.4 `RoleSpec`、§8.2 目录、§8.3 扩展方式 |
1304	| 二.2 任务拆解与分配 | §7.2 planner 校验、§8.2 PM 角色 |
1305	| 二.3 协作流转 / 驳回重做 | §6 状态机、§10 门禁、§11 上下文 |
1306	| 二.4 看板与进度追踪 / 断点继续 | §15 终端看板、§13 持久化 |
1307	| 二.5 多模型灵活绑定 | §12 模型接入层 |
1308	| 三 内置 8 角色 | §8.2 角色目录表 |
1309	| 四 技术栈与约束 | §17.5 依赖、§19.3 限制、§3.1 设计原则 |
1310	| 五 目录结构 | §4（保留骨架 + 5 个新增包，逐项对应修正） |
1311	| 六 核心逻辑要求 | §3 架构、§8.1 BaseAgent、§7 调度、§6 状态 |
1312	| 七 交付验收标准 | §20 验收清单 |
1313	| 八 项目关键词 | §2 修正表逐条覆盖 |
1314	| 九 调研参考范围 | §11.3（MetaGPT 消息池）、§6/§12（MAF durable checkpoint）、§13（Temporal 可恢复执行）、§10（CrewAI guardrail 评审链）、§7.4（死锁诊断） |
1315	| 十 后续扩展方向 | §19.2 V2 留口表 |
1316	

[End of file.]