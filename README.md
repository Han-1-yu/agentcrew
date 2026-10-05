# AgentCrew

AgentCrew 是一个多 Agent 协作开发平台：给它一个项目目标，由 8 个岗位角色组成的
虚拟团队自动完成需求分析、架构设计、前后端实现、测试、部署配置与文档，最终在
`output/<run_id>/artifacts/` 下交付一套带产物清单的完整项目。它既是可运行的演示，
也是一份「把 LLM 关进确定性引擎」的工程实现：调度、状态流转、依赖解析、契约校验
全部由可测试的代码完成，模型只负责它真正擅长的事。

---

## 项目介绍

AgentCrew 把一次交付拆成**两个阶段**：

- **规划阶段**：项目经理（`project_manager`）用一次受治理的 LLM 调用把目标拆解为
  任务 DAG。每个任务声明执行角色、依赖、验收标准与产物契约（路径、类型、最小字节数、
  必需符号等）。引擎随后对拆解结果做结构性校验：任务 id 唯一、依赖无环且存在、
  角色已注册、产物路径不被两个任务同时声明、每个任务都有验收标准，等等；校验失败会
  把具体错误回灌给 PM 重新拆解（上限 `plan_max_attempts`，默认 2 次）。通过后引擎
  追加保留的收尾任务 `T-FINAL`（由 `project_manager` 承担，产出 `DELIVERY.md`）。
  规划阶段可以设置人工卡点（`--gate plan`），在开始烧 token 之前让人确认或编辑拆解结果。
- **执行阶段**：确定性调度器按 DAG 依赖派发任务、限流并发、组装上下文、执行状态机，
  并对每个任务的产物依次跑三层门禁（G1 静态校验 → G2 LLM 评审 → G3 人工卡点）。
  **调度决策全程没有 LLM 参与** —— LLM 只在两处被调用：任务执行（各角色 Agent）与
  G2 评审。这使平台可测试、可复现、可断点续跑。

```
┌──────────────────────────────────────────────────────────────┐
│ main.py  —  CLI（run / resume / status / roles / clean / cancel）│
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ Crew — 确定性执行引擎（不包含任何 LLM 调度逻辑）                 │
│                                                              │
│  ┌────────────┐   ┌────────────┐   ┌──────────────┐          │
│  │ TaskBoard  │──▶│ Scheduler  │──▶│  Gatekeeper  │          │
│  │ DAG+状态机 │◀──│ 就绪队列   │   │  G1/G2/G3    │          │
│  │ 唯一事实源 │   │ 信号量限流 │   │              │          │
│  └─────┬──────┘   └─────┬──────┘   └──────┬───────┘          │
│        │                │                 │                  │
│  ┌─────▼──────┐   ┌─────▼──────┐   ┌──────▼───────┐          │
│  │ Checkpoint │   │ ContextBus │   │ ArtifactStore│          │
│  │ JSON 原子写│   │ 上下文包   │   │ 契约+安全落盘│          │
│  └────────────┘   └────────────┘   └──────────────┘          │
│  ┌────────────────────────────────────────────┐              │
│  │ Ledger — 日志 / token / 成本 审计            │              │
│  └────────────────────────────────────────────┘              │
└───────────────────────────┬──────────────────────────────────┘
                            │  dispatch(Task) → AgentResult
┌───────────────────────────▼──────────────────────────────────┐
│ Agent 层（唯一持有 LLM 的地方）                                │
│  BaseAgent（模板方法：组装提示词 → 调用 → 解析 → 校验 → 提交）   │
│    └─ 8 个角色子类，各自实现钩子 + 声明 RoleSpec                │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ Model 层：ModelFactory → BaseProvider                         │
│   OpenAICompatProvider / AnthropicProvider / MockProvider      │
│   + StructuredOutput 降级链 + 能力位 + 计费表                  │
└──────────────────────────────────────────────────────────────┘
```

几条贯穿全局的设计原则：

1. **单一事实源**：任务状态只存在于 `TaskBoard`；任何状态变更必须经
   `TaskBoard.transition()` 走状态机校验，非法跃迁抛 `IllegalTransitionError`。
2. **角色是数据，不是代码**：8 个角色的差异集中在提示词与输出契约，全部由
   `RoleSpec` 数据对象承载；新增角色 = 新增一份 `RoleSpec`，引擎代码零改动。
3. **契约先行**：「完成」由机器可判定的检查定义（产物契约 + 门禁），不由 Agent 自称。
4. **一切可中断**：任意时刻 `Ctrl-C`，checkpoint 已落盘；`resume` 从最近一致状态继续。
5. **诚实**：mock 产物标注为非模型生成，评审失败、停滞、降级全部留痕（见「已知限制」）。

---

## 角色说明

8 个内置角色，每份 `RoleSpec` 位于 `agentcrew/agents/<key>.py`：

| key | 岗位 | 核心职责 | 主要交付物 | 默认评审人 |
|---|---|---|---|---|
| `project_manager` | 项目经理 | 目标拆解、计划制定、终验 | 结构化 `plan`（任务 DAG，不落盘为普通文件）；`DELIVERY.md`（收尾任务 `T-FINAL`） | architect + product_manager（双评审，都 approve 才算过） |
| `product_manager` | 产品经理 | 需求分析、验收标准 | `docs/PRD.md`、`docs/acceptance.md` | project_manager |
| `architect` | 架构师 | 技术选型、模块划分、接口定义 | `docs/architecture.md`、`docs/api-spec.yaml` | project_manager |
| `backend_dev` | 后端开发工程师 | 业务逻辑、数据模型、API 实现 | `backend/app/` 下的代码（附依赖与运行说明） | architect |
| `frontend_dev` | 前端开发工程师 | 页面、交互、样式 | `frontend/` 下的 HTML / CSS / JS | architect |
| `qa_engineer` | 测试工程师 | 测试用例、执行测试、Bug 报告 | `tests/` 下的测试与 `docs/test-report.md` | backend_dev |
| `devops_engineer` | 运维工程师 | 部署方案、Docker、CI/CD | `Dockerfile`、`docker-compose.yml`、`docs/deploy.md` | architect |
| `technical_writer` | 文档工程师 | README、使用文档、接口文档 | `README.md`、`docs/usage.md` | product_manager |

评审关系由 `RoleCatalog.reviewer_for(role_key)` 解析：先取角色自身声明的
`RoleSpec.reviewer_for`，再追加声明 `["*"]` 的通用评审人，过滤自评审与未注册 key；
没有任何评审人时回落到 `project_manager`（它也不在目录中则跳过 G2，由 G1 决定）。
`python main.py roles` 可列出这 8 个内置角色的 key、职责、边界与产出契约。

---

## 快速开始

要求 Python 3.12（开发与验证环境为 3.12.10 / Windows 11）。三步：

```bash
# 1. 安装依赖（运行与测试都在这份清单里）
pip install -r requirements.txt

# 2. 配置模型 Key（Windows cmd / PowerShell 用 copy .env.example .env）
cp .env.example .env
#    编辑 .env，至少填一个 provider 的 Key，例如：
#    OPENAI_API_KEY=sk-...

# 3. 用内置演示目标跑一次完整交付
python main.py run --demo
```

说明：

- `--demo` 等价于使用 `examples/todo_web_project.py` 中内置的 Todo List Web 应用
  目标；也可以换成自己的目标：`python main.py run --goal "你的项目目标"`。
  `--goal` 与 `--demo` 二选一，都不给或都给会以退出码 3 报错。
- 默认模型是 `openai:gpt-4o-mini`，可在 `.env` 中用 `AGENTCREW_DEFAULT_MODEL`
  改成任意 `<provider>:<model>` 别名（见「模型绑定」）。
- 在真实终端里默认 `gate=both`：规划完成后与封版前各有一个人工确认点；在管道 /
  CI 等非 TTY 环境会**显式降级为 `gate=none`** 并在 stderr 说明，不会偷偷把人工
  闸门换成自动放行。想全程无人值守就加 `--auto`。
- 想看实时看板直接运行；想关掉看板（CI 友好）加 `--no-live`。

---

## 离线演示

没有 API Key、不想联网时，用 mock 模式跑完整流程：

```bash
python main.py run --demo --mock --auto
```

> **重要声明（mock 的诚实性）**：mock 模式下的产物来自
> `examples/fixtures/` 中**手写的预置回复**
> （`plan_todo.json`、`responses/<task_id>/attempt-<n>.md`、`reviews.json`），
> **不是模型生成的**。mock 的用途是**离线演示编排流程**与**支撑 hermetic 测试**
> —— 它证明的是「管线能跑通」，不是「模型能写出这样的东西」。把 mock 产物当成
> 模型能力证据会得出完全错误的结论；真实生成能力必须用 `--goal` + 真实 API Key
> 验证。mock 回放走与真实模型完全相同的数据通路（围栏解析、契约校验、安全落盘、
> 三层门禁一条不跳），因此它能真正验证管线，也仍然不能证明模型质量。

mock 演示的几个设计细节：

- 夹具里有一个**故意不合格**的尝试（`T-005` 的 `attempt-1.md` 是占位骨架：
  只有 `import` 与注释，缺少 `must_contain` 要求的 `def test_` 符号），用来演示
  G1 驳回 → 返工 → `attempt-2.md` 通过的完整闭环，所以一次 mock 运行中出现
  1 次返工是预期行为。
- `--mock` 是全局开关：覆盖所有角色绑定，任何 provider 别名都解析为 MockProvider，
  不需要任何 Key。
- 未匹配到夹具时返回「结构合法但内容为占位」的响应，并在 ledger 记一条
  `mock_fallback` 事件，绝不静默。
- `AGENTCREW_MOCK_LATENCY_MS` 可以给每次 mock 调用加固定延迟，方便观察实时看板。

**产物可运行性（人工验收步骤）**：mock 夹具里的 Todo 项目是手写的、真实可运行的
（FastAPI + 内存存储 + 原生前端），但平台不执行生成的代码（见「已知限制 1」）。
要确认它可运行，需要人工执行：

```bash
cd output/<run_id>/artifacts/backend
pip install -r requirements.txt
uvicorn app.main:app --reload
# 浏览器打开 http://127.0.0.1:8000/  → 增删改查 Todo 应正常工作
```

---

## 命令速查

六个子命令（`python main.py --help` 查看完整帮助；无子命令时打印帮助）：

| 子命令 | 用途 | 常用参数 |
|---|---|---|
| `python main.py run` | 执行一次完整 run（规划 → 执行 → 交付） | `--goal TEXT` 与 `--demo` 二选一；`--mock`；`--auto`；`--gate both\|plan\|delivery\|none`；`--max-concurrency N`；`--default-model ALIAS`；`--output-dir DIR`；`--context-budget N`；`--no-live` |
| `python main.py resume <run_id>` | 续跑一个中断或未完成的 run | `--force`（配置快照不一致时仍继续）；以及 `run` 的全部配置参数（`--mock` / `--auto` / `--gate` / `--max-concurrency` / `--default-model` / `--output-dir` / `--context-budget` / `--no-live`） |
| `python main.py status <run_id>` | 打印 run 记录、统计与任务看板 | `--output-dir DIR` |
| `python main.py roles` | 列出 8 个内置角色的职责 / 边界 / 产出 | 无 |
| `python main.py clean` | 保留最近 N 个 run，删除更旧的（默认 N=5） | `--keep N`；`--dry-run`（只列不删）；`--output-dir DIR` |
| `python main.py cancel <run_id>` | 给离线 run 打 cancelled 标记（下次 resume 直接收尾） | `--output-dir DIR` |

行为说明：

- `--auto` 等价于 `--gate none`；`--auto` 与 `--gate` 同时给出时 `--auto` 胜出。
- `resume` 会先比对 run 创建时的配置快照与当前配置：不一致（例如换了默认模型、
  或用非 mock 参数续跑一个 mock run）会打印差异并以退出码 3 拒绝，确认无误后加
  `--force`。`Ctrl-C` 中断的 run 会落盘为 `INTERRUPTED` 并提示 `resume <run_id>`；
  `RUNNING` 的任务在 resume 时回到 `READY` 重新执行（产物可能只写了一半），
  `REWORK` 的任务保持原状、由调度器直接重新派发，`REVIEW` 的任务保持原状只重跑门禁；
  `attempts` / `rework_count` 一律不重置。
- `clean` 的删除范围被限制在 `--output-dir` 之下，`--dry-run` 与真删共用同一套
  边界校验。

退出码（`agentcrew/crew/crew.py::EXIT_CODES`，对应 spec §7.4）：

| 退出码 | 含义 |
|---|---|
| `0` | 正常结束（全部任务 `SUCCEEDED`） |
| `1` | 存在 `FAILED` / `BLOCKED` / `SKIPPED` 任务 |
| `2` | 拆解连续未通过校验（`plan_max_attempts` 用尽） |
| `3` | 配置 / 参数错误（缺 Key、参数非法、resume 配置不一致未加 `--force` 等） |
| `4` | 停滞（死锁：没有可运行任务但仍有任务未终结） |
| `130` | 用户中断（`Ctrl-C` → `INTERRUPTED`）或取消（`cancel` → `CANCELLED`） |

---

## 自定义角色 / 团队开发指南

「角色是数据，不是代码」不是口号：一个新角色的全部差异（提示词、输出契约、
温度、模型绑定、评审关系）都写在 `RoleSpec` 里，Agent 行为默认由 `GenericAgent`
（`BaseAgent` 的通用子类）承载，**引擎代码零改动**。只有需要特殊输出解析逻辑的
角色才值得写 `.py` 子类。

`RoleSpec` 的字段（`agentcrew/roles/spec.py`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `key` | str | 角色唯一标识，任务通过它引用角色（如 `wizard`） |
| `title` | str | 岗位名称 |
| `goal` | str | 核心职责（对应 CrewAI 的 goal） |
| `backstory` | str | 背景设定 |
| `responsibilities` | list[str] | 职责清单 |
| `boundaries` | list[str] | 能力边界：明确不做什么 |
| `output_contract` | str | 输出规范的自然语言描述 |
| `system_prompt` | str | 系统提示词 |
| `temperature` | float（默认 0.3） | 采样温度 |
| `max_output_tokens` | int（默认 8192） | 单次输出上限 |
| `model` | str（默认 `"default"`） | 模型别名；保持 `"default"` 时按三级优先级解析（见「模型绑定」） |
| `reviewer_for` | list[str]（默认空） | 默认评审本角色的角色 key；`["*"]` 表示声明者是通用评审人 |

### 方式一：`roles.d/` 目录（YAML，推荐给团队）

1. 在项目根目录的 `agentcrew.yaml` 里指定目录：

   ```yaml
   roles_dir: roles.d
   ```

2. 把角色 YAML 放进该目录（启动时自动加载所有 `*.yaml` / `*.yml`，按文件名排序；
   字段非法或 key 与已有角色重复都会直接报错，绝不静默跳过）：

   ```yaml
   # roles.d/wizard.yaml
   key: wizard
   title: 向导
   goal: 把项目目标翻译成一份向导说明。
   backstory: 你擅长把复杂目标压缩成三步。
   responsibilities:
     - 产出 docs/wizard.md
   boundaries:
     - 不写代码
   output_contract: 产出 docs/wizard.md（Markdown）。
   system_prompt: 你是向导，只输出 docs/wizard.md 的内容。
   temperature: 0.2
   reviewer_for:
     - project_manager
   ```

之后 `python main.py run --goal "..."` 会把这个角色注册进 `RoleCatalog`；PM 在
拆解计划里引用 `wizard` 即可派发任务，引擎与 CLI 代码均无需改动。

### 方式二：代码注册 `crew.register_role(RoleSpec(...))`

把 AgentCrew 当库使用时，可以在构造 `Crew` 之后、`run()` 之前直接注册
（完整可复制示例，`mock=True` 下离线即可验证）：

```python
import asyncio

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.roles.spec import RoleSpec

WIZARD = RoleSpec(
    key="wizard",
    title="向导",
    goal="把项目目标翻译成一份向导说明。",
    backstory="你擅长把复杂目标压缩成三步。",
    responsibilities=["产出 docs/wizard.md"],
    boundaries=["不写代码"],
    output_contract="产出 docs/wizard.md（Markdown）。",
    system_prompt="你是向导，只输出 docs/wizard.md 的内容。",
    temperature=0.2,
    reviewer_for=["project_manager"],
)


async def main() -> None:
    settings = Settings.load(mock=True, gate="none")  # 离线，无需 API Key
    crew = Crew(settings)
    crew.register_role(WIZARD)  # 引擎代码零改动
    record = await crew.run("开发一个 Todo List Web 应用。")
    print(record.status.value)


asyncio.run(main())
```

需要特殊解析逻辑时，再给角色配一个 `BaseAgent` 子类并把类一并传入：
`crew.register_role(WIZARD, WizardAgent)`；未登记专用类的 key 一律回落到
`GenericAgent`，行为完全由 `RoleSpec` 驱动。

注意：

- 注册必须发生在 run 开始之前（`AgentFactory` 在 run 开始时首次取用角色）。
- 任务引用未注册的角色 key 时，引擎会回落到带通用提示词的 `GenericAgent`，
  让任务仍有机会跑完，而不是把整次 run 打崩；但计划校验（任务角色必须已注册）
  会先拦住这种拆解。
- `python main.py roles` 列出的是 8 个内置角色；自定义角色通过目录 / 注册进入
  `Crew.catalog`，不改变这份内置清单。

---

## 模型绑定

模型别名格式为 `<provider>:<model>`，**只按第一个冒号切分**，因此模型名可以
自带冒号，例如 `anthropic:claude-sonnet-5`、`ollama:qwen2.5:14b`。

角色 → 模型的三级优先级（高者胜，`agentcrew/crew/crew.py::_alias_for`）：

1. `RoleSpec.model`（角色级；保持默认值 `"default"` 时不参与优先级）；
2. `agentcrew.yaml` 的 `models.role_bindings.<role_key>`，或用环境变量
   `AGENTCREW_MODEL_<ROLE_KEY>`（大写、下划线，如 `AGENTCREW_MODEL_QA_ENGINEER`）；
3. `Settings.default_model`（默认 `openai:gpt-4o-mini`，可用
   `AGENTCREW_DEFAULT_MODEL` 或 `--default-model` 覆盖）。

`--mock` 是全局开关，会覆盖以上所有绑定，把一切别名解析为 MockProvider。
`ModelFactory` 按别名缓存 provider 实例，避免重复建连。

8 个内置 provider（`agentcrew/models/config.py::PROVIDER_PRESETS`，可被
`agentcrew.yaml` 的 `models.providers` 覆盖）：

| 别名 | 实现 | 默认 base_url | `json_schema` | `json_object` | API Key 环境变量 |
|---|---|---|---|---|---|
| `openai` | OpenAI 兼容 | `https://api.openai.com/v1` | ✅ | ✅ | `OPENAI_API_KEY` |
| `anthropic` | Anthropic 原生 Messages API | `https://api.anthropic.com` | ❌ | ❌ | `ANTHROPIC_API_KEY` |
| `deepseek` | OpenAI 兼容 | `https://api.deepseek.com/v1` | ✅ | ✅ | `DEEPSEEK_API_KEY` |
| `doubao` | OpenAI 兼容 | `https://ark.cn-beijing.volces.com/api/v3` | ❌ | ✅ | `ARK_API_KEY`（豆包 / 火山方舟） |
| `qwen` | OpenAI 兼容 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | ❌ | ✅ | `DASHSCOPE_API_KEY`（通义千问） |
| `ernie` | OpenAI 兼容 | `https://qianfan.baidubce.com/v2` | ❌ | ❌ | `QIANFAN_API_KEY`（文心一言，千帆 v2） |
| `ollama` | OpenAI 兼容 | `http://localhost:11434/v1` | ❌ | ✅ | 不需要（本地；`OLLAMA_BASE_URL` 可改端点） |
| `mock` | MockProvider | — | ✅ | ✅ | 不需要（离线确定性回放） |

**`json_schema` 支持情况为什么重要**：需要结构化输出的调用（PM 拆解计划、G2 评审）
走三级降级链 + 修复循环：

1. 支持 `json_schema` 时，由服务端按 JSON Schema 严格约束输出（最稳、正常一次调用）；
2. 只支持 `json_object` 时，不传 schema 的 JSON 模式是「无形状」的，引擎把 schema
   内嵌进提示词补足；
3. 两者都不支持时（`anthropic`、`ernie`），纯提示词约束 + 从回复中提取 `json` 围栏块
   （兜底提取首个平衡的 `{...}`），解析 / 校验失败进入修复循环，把 Pydantic 校验错误
   原文回灌重试，最多 2 次（即最多 3 次模型调用）。

因此同一个结构化任务，在不支持 `json_schema` 的 provider 上可能花更多调用次数、
稳定性也略低（功能不受影响）。每次降级都会在 ledger 记一条 `llm_degraded` 事件，
便于事后诊断「哪家 provider 不稳定」。能力位描述的是实际在跑的 provider，`--mock`
下就是 mock 预设的能力位。

---

## 配置项

配置来源与优先级（低 → 高，`agentcrew/config.py::Settings.load`）：

1. 代码内默认值；
2. `agentcrew.yaml`（可选；默认读工作目录下的这一文件，可用 `AGENTCREW_CONFIG`
   环境变量指定其他路径）；
3. 环境变量（`python-dotenv` 会把 `.env` 载入这一层；同名时真实环境变量优先于
   `.env` 中的值）；
4. CLI 参数。

### `.env`（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 空 | `openai` 的 Key |
| `ANTHROPIC_API_KEY` | 空 | `anthropic` 的 Key |
| `DEEPSEEK_API_KEY` | 空 | `deepseek` 的 Key |
| `ARK_API_KEY` | 空 | `doubao`（豆包 / 火山方舟）的 Key |
| `DASHSCOPE_API_KEY` | 空 | `qwen`（通义千问）的 Key |
| `QIANFAN_API_KEY` | 空 | `ernie`（文心一言，千帆 v2 API Key） |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | 本地 ollama 端点（无需真实 Key） |
| `AGENTCREW_DEFAULT_MODEL` | `openai:gpt-4o-mini` | 默认模型别名 |
| `AGENTCREW_MODEL_<ROLE>` | 空 | 按角色覆盖模型，8 个：`PROJECT_MANAGER` / `PRODUCT_MANAGER` / `ARCHITECT` / `BACKEND_DEV` / `FRONTEND_DEV` / `QA_ENGINEER` / `DEVOPS_ENGINEER` / `TECHNICAL_WRITER` |
| `AGENTCREW_MAX_CONCURRENCY` | `4` | 全局并发上限（含 LLM 调用） |
| `AGENTCREW_OUTPUT_DIR` | `output` | run 输出根目录 |
| `AGENTCREW_GATE` | 真实 TTY 下 `both`，否则 `none` | 人工闸门：`both` / `plan` / `delivery` / `none` |
| `AGENTCREW_CONTEXT_BUDGET` | `24000` | 上下文预算（tokens，启发式估算） |
| `AGENTCREW_LOG_LEVEL` | `INFO` | 日志级别 |
| `AGENTCREW_MOCK` | `false` | 全局 mock 开关 |
| `AGENTCREW_MOCK_LATENCY_MS` | `0` | mock 调用固定延迟（毫秒） |
| `AGENTCREW_CONFIG` | `agentcrew.yaml` | YAML 配置文件路径 |

### `agentcrew.yaml`

YAML 只识别以下键；`output_dir` / `mock` / `log_level` 等请用环境变量或 CLI
设置，未识别的键会被忽略：

| 键 | 说明 |
|---|---|
| `max_concurrency` | 全局并发上限 |
| `context_budget_tokens` | 上下文预算（tokens） |
| `gate` | 人工闸门模式 |
| `roles_dir` | 自定义角色目录（见「自定义角色」） |
| `models.default` | 默认模型别名（等价 `AGENTCREW_DEFAULT_MODEL`） |
| `models.role_bindings.<role_key>` | 按角色覆盖模型 |
| `models.providers.<provider>` | 覆盖内置 provider 预设：`base_url` / `capabilities`（逐位合并） / `implementation` |

```yaml
max_concurrency: 4
context_budget_tokens: 24000
gate: both
roles_dir: roles.d

models:
  default: openai:gpt-4o-mini
  role_bindings:
    architect: anthropic:claude-sonnet-5
    qa_engineer: deepseek:deepseek-chat
  providers:                    # 覆盖内置能力位（逐位合并，未写的保持预设值）
    doubao:
      capabilities: { supports_json_schema: false, supports_json_object: true }
```

引擎级参数（如 `plan_max_attempts`，规划重拆上限，默认 2）当前只在代码层设置：
`Settings.load(plan_max_attempts=3)`。CLI 覆盖只写「命令行真的给了」的键，不会把
`--mock` 的 `False` 缺省反过来盖掉 `AGENTCREW_MOCK=true` 之类的环境变量。

---

## 产物说明

每次 `run` 在 `output/<run_id>/` 下建立独立命名空间，`run_id` 格式为
`YYYYMMDD-HHMMSS-<4位随机十六进制>`（本地时间）：

```
output/<run_id>/
├── run.json                 # RunRecord：目标、状态、时间、配置快照、统计
├── tasks.json               # TaskBoard 全量快照（每个任务的契约、评审记录、状态历史）
├── ledger.jsonl             # 追加式事件日志：llm_call / gate_result / human_decision …
├── context/
│   └── decisions.jsonl      # 上下文决策记录（返工意见等，供下游 Agent 引用）
├── review/                  # 预留目录（V1 的评审记录随 tasks.json 落盘）
├── diagnostics/
│   └── stall-report.md      # 仅停滞（死锁）时生成
└── artifacts/               # 真实交付物根目录（按契约路径分布）
    ├── README.md、docs/、backend/、frontend/、tests/、Dockerfile …
    ├── DELIVERY.md          # T-FINAL（project_manager）汇总的交付总结
    └── MANIFEST.json        # 产物清单
```

- `run.json`：`run_id`、`schema_version`、项目目标、`status`（`running` /
  `interrupted` / `completed` / `failed` / `cancelled`）、创建/更新时间、run 创建时
  捕获的配置快照（resume 用它判断结果可比性）、任务与 token/成本统计。
- `tasks.json`：任务 DAG 的唯一事实源快照；每次状态跃迁后落盘，配合原子写
  （临时文件 + `os.replace`）保证中断后可恢复。
- `ledger.jsonl`：逐行 JSON 事件，token 与成本审计的来源；`status <run_id>`
  输出的「llm_call N 次」即来自这里。
- `DELIVERY.md`：由引擎追加的收尾任务 `T-FINAL` 产出，依赖全部叶子任务，
  汇总完成清单与验收结论（契约要求含「完成」字样且不小于 100 字节）。
- `MANIFEST.json`：产物清单，随每次状态跃迁刷新、封版时最终写定；
  字段为 `run_id`、`generated_at` 与 `entries` 列表；每条 entry 含 `path`、
  `kind`、`size_bytes`、`sha256`、`produced_by_task`、`produced_by_role`、
  `written_at`。resume 时引擎从它重建产物记录，跨任务的路径冲突检测与返工前的
  清理都依赖这份清单。

---

## 运行测试

```bash
python -m pytest -q
```

- 测试套件**不需要网络、不需要 API Key**：模型调用要么由 mock provider 回放
  `examples/fixtures/` 中的夹具，要么被测试内的假 Provider 拦截；端到端测试在
  `tmp_path` 下跑完整流程（零网络）。
- 覆盖范围包括：状态机与非法跃迁、产物契约与解析、安全落盘、DAG 与调度、
  三层门禁、模型接入与结构化输出降级、配置合并、checkpoint 与 resume、
  CLI 行为、mock 夹具与 mock 无旁路，以及本文档的交付验收。
- 本机 `pytest` 裸命令不在 PATH 上，请统一用 `python -m pytest`；只跑 README
  验收可以指定 `python -m pytest tests/test_readme.py -v`。

---

## 已知限制

V1 的功能边界如下，请在使用前明确知悉。这些限制不是「以后再说」的注脚，
而是会影响你对产物的信任方式的硬约束：

1. **不执行生成的代码**。G1 只做静态校验（产物存在、大小、`.py` 可 `ast.parse()`、
   `.json` / `.yaml` / `.toml` 可解析、`must_contain` 符号齐全、HTML/CSS/JS 非纯
   占位符），**不运行产物测试、不启动服务**。因此产物「可运行」是由人工按
   「离线演示」一节的 uvicorn 步骤验收确认的，不是平台自动保证的。这既是安全边界
   （不在宿主上跑生成的代码），也是 V1 的复杂度边界。
2. **Token 估算为启发式**。上下文预算判定使用字符启发式估算 token 数，与真实
   tokenizer 存在偏差；成本统计以 API 返回的 usage 为准，未知模型按 0 计但照样
   记录 token 数。预算是软约束方向上的工程近似，不是精确计量。
3. **文心一言仅支持千帆 v2 的 API Key 模式**。旧版 `access_token` 鉴权不在支持范围；
   并且千帆 v2 不支持 `json_schema` / `json_object`，结构化输出走提示词约束 +
   围栏提取 + 修复循环（见「模型绑定」）。
4. **单进程**。调度器是单进程 asyncio，不支持多机分布式执行；并发上限由
   `max_concurrency` 控制。
5. **mock 产物不是模型生成的**。`--mock` 的产物来自 `examples/fixtures/` 中手写的
   预置回复，用于离线演示编排流程与支撑 hermetic 测试，**不代表模型能力**；
   真实生成能力需要 `--goal` + 真实 API Key 验证。

---

## 状态机与门禁

### 任务状态机（10 态）

任务状态只存在于 `TaskBoard`，跃迁表硬编码在 `agentcrew/task/state_machine.py`，
非法跃迁抛 `IllegalTransitionError`：

| 起始态 | 可跃迁到 | 触发条件 |
|---|---|---|
| `PENDING` | `READY` / `BLOCKED` / `SKIPPED` / `CANCELLED` | 依赖全部成功 / 任一依赖失败或取消 / 任一依赖被跳过 / 用户取消 |
| `READY` | `RUNNING` / `BLOCKED` / `CANCELLED` | 取得并发槽位并派发 / 派发前发现依赖已失败 / 用户取消 |
| `RUNNING` | `REVIEW` / `READY` / `FAILED` / `CANCELLED` | 正常返回且产物解析成功 / 执行异常且 `attempts < max_attempts`（重试）/ 异常且额度用尽 / 用户硬取消 |
| `REVIEW` | `SUCCEEDED` / `REWORK` / `FAILED` / `CANCELLED` | G1+G2+G3 全 approve / 任一门禁 reject 且 `rework_count < max_reworks` / 驳回且返工额度用尽 / 用户取消 |
| `REWORK` | `RUNNING` / `CANCELLED` | 再次派发（携带 `fix_hints`）/ 用户取消 |
| `BLOCKED` | `READY` | 每轮调度重算：全部依赖变为 `SUCCEEDED` |
| `SUCCEEDED` / `FAILED` / `CANCELLED` / `SKIPPED` | — | 终态（无出边） |

额度语义：`attempts` 是执行尝试次数（每次 `READY → RUNNING` 时 +1，派发即扣除，
所以「中断 → resume」也会消耗一次，杜绝无限循环）；`rework_count` 是返工次数
（每次 `REVIEW → REWORK` 时 +1）。默认 `max_attempts=3`、`max_reworks=2`，
两者独立计数，上限明确。`FAILED` 是终态，不是状态机边；人工重试走
`TaskBoard.reset_failed()` 的显式接口（交付卡点打回 `FAILED` 任务即用这条路径，
`SUCCEEDED` 任务被打回则记 `reason="delivery-rework"` 后置回 `READY`）。

### 三层门禁

`Gatekeeper.evaluate` 是任务离开 `REVIEW` 的唯一入口，三层顺序执行、任一层
reject 即短路：

- **G1 — 静态校验**（确定性，零 token）：契约中每个必需产物都已产出、未声明产物
  未出现、文件字节数达标、`.py` 通过 `ast.parse()`、`.json` / `.yaml` / `.toml`
  可解析、HTML/CSS/JS 非空且不含纯占位内容、`must_contain` 字面量齐全。G1 驳回
  直接短路 G2 —— 缺文件、语法错误不是判断题，一次评审调用都不发。
- **G2 — LLM 评审**（结构化输出，走降级链）：评审人由
  `RoleCatalog.reviewer_for(task.role)` 决定（`project_manager` 的任务由
  architect + product_manager 双评审，取都 approve）；评审输入为任务描述 +
  验收标准 + 产物内容（按 8000 tokens 的评审节选预算截断）+ 返工历史。`verdict=reject`
  时 `issues` 必须非空，否则视为评审格式错误重试（不计任务返工额度，最多 2 次；
  仍失败按 approve 放行并记 warning —— 宁可放过，也不因评审器故障卡死全流程）。
  评审的 token 消耗单独记入 ledger。
- **G3 — 人工闸门**（CLI）：由 `--gate` 控制两个卡点。`plan` 卡点在 PM 拆解通过
  校验、开始烧 token 以前展示任务 DAG，可选 `[a] 批准执行 / [e] 编辑后执行 /
  [s] 保存退出 / [q] 取消`；`delivery` 卡点在全部任务 `SUCCEEDED`、封版以前展示
  产物清单与统计，可选 `[a] 接受交付 / [r] 打回指定任务返工 / [q] 放弃`。
  `--gate none`（等价 `--auto`）跳过两层；非 TTY 环境下含人工卡点的模式会
  **显式降级为 `none`** 并在 stderr 说明，绝不「声称有人工卡点、实际全自动」。
  人工裁决作为 `ReviewRecord(layer="human")` 完整留痕。

每条门禁裁决都会写入任务历史（`board.record_review`），并落一条 `gate_result`
审计事件（含层、裁决、分数、问题列表），`status` 命令的「G1 拦截 / G2 驳回 /
人工打回」统计即来自这些事件。
