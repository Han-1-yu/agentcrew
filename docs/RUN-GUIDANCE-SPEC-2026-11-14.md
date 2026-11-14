# C10 切片一：run 中途指引（后端规格）

> 2026-11-14 · 状态：**切片一已实现**（分支 `c10-run-guidance`，待合并）· 对应 `完善/AgentCrew落地完善路线-2026-11-12.md` 的 C10
>
> 本文只覆盖**后端通道**（用户在一场 run 跑到一半时补一句话，引擎在下一次派发时吃到）。
> Studio 的输入框与流式回显是切片二，需产品决策，另行立项。

## 0. 一句话

现在一场 run 开跑后只能看或取消，不能加约束、不能纠偏。切片一让「人话」能进入运行中的 run，
并且**不改变任何既有行为**：开关默认关，关掉时上下文包与提示词逐字节不变。

## 1. 差距证据（不是推测）

| 事实 | 位置 |
| --- | --- |
| 上下文包由六级优先级组装，任务派发时一次成型 | `agentcrew/context/context_bus.py::ContextBus.build` |
| 提示词渲染只有一个出口，`fix_hints` 是唯一的「运行期追加」通道，且只服务返工 | `agentcrew/agents/prompting.py::render_pack` |
| 调度器在任务槽位内 `bus.build(task)` → `agent.execute(task, pack)` | `agentcrew/crew/scheduler.py::_execute_one` |
| HTTP 控制面已有 approvals / cancel / resume / model / agents，**没有**「对运行中的 run 说话」的入口 | `agentcrew/api/server.py::API_ROUTES` |
| 协议里 run 只有 `pending → decided → consumed` 的审批单，没有自由文本通道 | `agentcrew/runtime/approvals.py` |

## 2. 语义（先定死，再写码）

1. **落地形态**：`<run_dir>/guidance.jsonl`，追加写。一行一条：
   `{"id": "...", "ts": "...", "author": "...", "text": "...", "consumed_at": ...}`
   与 `approvals.jsonl` 同族，跨进程可见、崩溃后仍在、不需要额外数据库。
2. **吸收时机**：只在**任务派发前**（即 `ContextBus.build` 被调用的那一刻）。不打断在跑任务，
   不改调度顺序，不新增调度分支 —— 确定性引擎的边界不动。
3. **吸收粒度与 at-most-once**：一条指引只会被**一个**任务吃掉，吃到即写 `consumed_at`；
   该任务后续返工重跑不再重复注入（返工有 `fix_hints` 通道，语义不混）。
4. **顺序**：按落盘顺序注入，最多 `max_guidance_items` 条，单条超 `max_guidance_chars`
   截断并留 `guidance_truncated` 事件。
5. **冲突优先级**：指引与计划冲突时以指引为准，但**不得**让模型跳过交付物契约
   （契约段仍在最后，近因效应不变，见 `render_pack` 的注释）。
6. **关闭态**：`guidance_enabled=False`（默认）时 provider 为 `None`，`build()` 不走任何
   新分支，`render_pack` 不新增段落 —— 用一条「提示词逐字节不变」的黄金用例钉住。
7. **不碰的东西**：ledger 写路径、状态机、验收门槛、审批单、预算。指引是上下文，不是裁决。

## 3. 落点（按实施顺序）

### 3.1 新模块 `agentcrew/runtime/guidance.py`（约 90 行）

```python
@dataclass(frozen=True)
class GuidanceItem:
    id: str; ts: str; author: str; text: str

class GuidanceStore:
    def __init__(self, run_dir: Path) -> None: ...
    def append(self, text: str, author: str = "") -> GuidanceItem      # 加锁追加，拒绝空文本
    def pending(self, *, limit: int, max_chars: int) -> list[GuidanceItem]
    def consume(self, ids: Sequence[str]) -> int                        # 幂等；已消费再调返回 0
    def items(self) -> list[GuidanceItem]                               # 含已消费，供 GET 读
```

约束：文本做 `redact` 脱敏（复用 `runtime/redact.py` 的既有词表，密钥不进盘）；
空/纯空白拒绝；坏行跳过而不是抛（与 `Ledger` 的容错口径一致）；文件权限 0600。

### 3.2 `agentcrew/context/context_bus.py`

- `ContextPack` 新增 `guidance: list[str] = Field(default_factory=list)`（`extra="forbid"` 保持）。
- `ContextBus.__init__` 新增 `guidance_provider: Callable[[], list[str]] | None = None`。
- `build()` 在定级 1 之后、级 2 之前调用 provider（异常吞掉并降级为空，指引失败不能弄停 run）。
- provider 由 Crew 提供，内部完成「读 pending → 标记 consumed → emit 事件 → 返回文本」，
  因此 `ContextBus` 不持有存储、不认识 run 目录。

### 3.3 `agentcrew/agents/prompting.py`

`render_pack` 在「本任务」之后插入一段，**仅当** `pack.guidance` 非空：

```
## 用户中途指引（优先于计划，与交付物契约冲突时先问清楚再改）
- ...
```

### 3.4 `agentcrew/crew/crew.py`

- `_execute` 与 `_clone_bus` 构造 `ContextBus` 时传 `guidance_provider=self._guidance_provider()`。
- 新增 `_guidance_provider(run)`：`guidance_enabled` 关闭或无 run 目录时返回 `None`；
  否则返回闭包，内部用 `GuidanceStore`，并在注入后 `run.ledger.emit("guidance_injected",
  ids=[...], count=n, chars=n)`；截断时补 `guidance_truncated`。
- resume 路径同样生效（store 绑定 `run.checkpoint.run_dir`，不依赖内存状态）。

### 3.5 `agentcrew/config.py`

三个字段 + 三处登记（`_ENV_TO_FIELD` / `_YAML_SCALAR_KEYS` / `_COMPARABLE_KEYS`，与既有开关同路径）：

| 字段 | 默认 | env |
| --- | --- | --- |
| `guidance_enabled: bool` | `False` | `AGENTCREW_GUIDANCE_ENABLED` |
| `max_guidance_items: int`（ge=1, le=50） | `5` | `AGENTCREW_MAX_GUIDANCE_ITEMS` |
| `max_guidance_chars: int`（ge=1, le=2000） | `400` | `AGENTCREW_MAX_GUIDANCE_CHARS` |

注意 `_COMPARABLE_KEYS`：三个都是 run 级快照的一部分，resume 时开关变化要能比对出来。

### 3.6 HTTP 控制面

| 方法 | 路径 | 作用域 | 行为 |
| --- | --- | --- | --- |
| `POST` | `/runs/{run_id}/guidance` | `runs:admin` | `{"text": "...", "author"?: "..."}` → 202 + `{"id","count"}`；run 不存在 404、非运行中 409、空文本 400、超条数 429 |
| `GET` | `/runs/{run_id}/guidance` | `runs:read` | `{"items":[...],"pending":n}` |

必须同步改：`API_ROUTES` 元组、`_ROUTE_SCOPES` 作用域表、dispatch 分支、
`artifacts/contracts/openapi.yaml`（`tests/test_api_contract.py` 做双向比对，漏一处就红）、
`artifacts/contracts/event-types.md`（补 `guidance_injected` / `guidance_truncated`）。

### 3.7 CLI

`python main.py guide <run_id> "把登录页改成绿色"`（另有 `--author`、`--json`）。
写入与 API 同一个 store，终端里另开一个窗口即可对正在跑的 run 说话。
`status` 输出增加「待吸收指引 N 条」。

## 4. 失败模式

| 场景 | 期望 |
| --- | --- |
| 指引文件被截断/半行 | 跳过坏行，其余可用；不抛异常 |
| 存储写失败（磁盘满） | provider 返回空并记 warning；run 继续，绝不因指引失败而失败 |
| run 已终态后写入 | 允许落盘但永不吸收；`GET` 仍可读（留审计） |
| 并发写（多终端/API） | `O_APPEND` + 进程内锁；id 用 `时间戳-序号`，重复消费幂等 |
| resume 后旧指引 | `consumed_at` 已在盘上，不会二次注入 |
| 指引里带密钥 | 落盘前过 `redact`，且指引**不进** `config_snapshot` |

## 5. 验收标准

1. 关闭态零差异：`tests/test_prompting_golden.py`（若不存在则新建）断言渲染文本逐字节不变，
   且 `ContextPack.model_dump()` 不含 `guidance` 键以外的任何变化。
2. 打开态生效：mock 通道构造一场三任务 run，在 T-002 派发前塞一条指引，断言
   T-002 的 prompt 含该段、T-001（已派发）不含、T-003 不含（已被 T-002 吃掉）。
3. 跨进程：`serve` 起在另一进程写指引，`run` 侧下次派发可见（at-most-once 只吃一次）。
4. 契约：`test_api_contract.py` 双向比对通过；404/409/400/429 四个错误码各有用例。
5. 不回归：`scripts/test.py` 全量绿（当前基线 3256 passed / 6 skipped），ruff + mypy 零错误。
6. 事件可见：ledger 里 `guidance_injected` 带 `ids` 与 `count`；`diff` 能看出两个 run 的指引差异。

## 6. 明确不做（切片一）

- Studio 对话页输入框与流式回显（切片二，前端 + 产品口径）
- 让指引触发重规划 / 改 DAG（那是另一种能力，且会动确定性边界）
- 把指引接到审批单、跨角色咨询、webhook 上
- 指引的优先级仲裁（模型自己判断；不做规则引擎）

## 7. 工作量与风险

约 1.5–2 人日（含 8–10 条测试与三份契约文档同步）。风险集中在**契约守卫**（漏改一处即红，
但有测试兜住）与**提示词回归**（黄金用例兜住）。引擎侧改动是加法：一个可选参数 + 一个字段
+ 一个渲染段，不改调度与门禁。

---

## 8. 实施记录（2026-11-14，切片一）

### 8.1 落地文件

| 文件 | 改动 |
| --- | --- |
| `agentcrew/runtime/guidance.py` | **新增**：`GuidanceItem` / `GuidanceStore`（追加写 + 归并读、进程锁、脱敏、0600） |
| `agentcrew/context/context_bus.py` | `ContextPack.guidance` + `guidance_provider` 参数；`build()` 在级 1 之后、级 2 之前吸收 |
| `agentcrew/agents/prompting.py` | `render_pack` 插入「用户中途指引」段（仅非空时） |
| `agentcrew/crew/crew.py` | `_guidance_provider(run)`；`_execute` / `_clone_bus` 两处 `ContextBus` 接线 |
| `agentcrew/config.py` | 三字段 + `_ENV_TO_FIELD` + `_YAML_SCALAR_KEYS` |
| `agentcrew/api/server.py` / `rbac.py` | `API_ROUTES` 两条、`_ROUTE_SCOPES` 两条、`_get_guidance` / `_add_guidance` |
| `main.py` | `guide` 子命令 + `_HANDLERS` 登记 + `status` 的「待吸收」行 |
| `artifacts/contracts/openapi.yaml` / `event-types.md` | 两个端点 + `GuidanceItem`；`guidance_injected` / `guidance_truncated` |
| `tests/test_run_guidance.py` | **新增 21 例** |

### 8.2 与规格原文的偏差（都以真实代码为准）

1. **`runtime/redact.py` 不存在**。真实脱敏入口是 `utils/logger.py::redact(text, secrets)`，
   词表来自 `Settings.secret_values()`。`GuidanceStore` 通过构造参数收 `secrets`，
   避免 `runtime` 层 import `config`（分层护栏）。
2. **`_COMPARABLE_KEYS` 不存在**（全仓只在 `plan_only` 的注释里出现）。`run.json` 的
   `config_snapshot` 是 `Settings.model_dump()` 全量快照，新增三个字段**自动**进入
   resume 比对，无需登记任何常量。
3. **落盘形态改为 `op` 追加**（`add` / `consume` 两行），对外视图仍是 spec 的
   `id / ts / author / text / consumed_at`。理由：改 `consumed_at` 要重写整个文件，
   多终端/API 并发写会互相覆盖；追加写与 `approvals.jsonl` 同族。代价是空文本被拒
   的文案与 `items()` 的归并逻辑多一层。
4. **指引不占 token 预算**（级 1.5）。规格没写计费规则；若按级 2 计费，预算紧张时
   唯一一条人写的约束会被挤掉——那是「人说了算」被机器预算否决。
5. **`GuidanceItem` 多一个 `truncated` 字段**（不落盘）。`pending()` 返回时不带
   「是否被截断」，Crew 就没法补 `guidance_truncated` 事件；这个标志只在本次读取内有效。
6. **终态 run 的写入口径分裂**：API 回 409（§3.6），CLI `guide` 允许落盘但永不吸收
   （§4 的审计口径）。API 是控制面，不该给已结束的 run 伪造「还能纠偏」的错觉；
   终端留痕则有用。
7. **写入作用域 `runs:admin`**（与规格一致），不是 `runs:write`：给正在跑的 run 加约束
   会改变产出，普通 operator 不该默认拥有。

### 8.3 验证

- `tests/test_run_guidance.py`：**21 passed**（存储 8 / 上下文与提示词 5 / 引擎接线 3 / HTTP 5）。
- `tests/test_api_contract.py` + `tests/test_architecture.py`：**106 passed**（路由双向比对与逐条探测自动纳入两个新端点）。
- `ruff check agentcrew/ main.py tests/`：**All checks passed**。
- `mypy agentcrew/`：**140 文件零错误**。
- 全量回归：见提交信息与 `PROJECT-STATE.md` 回填（分支上不重复记录）。
