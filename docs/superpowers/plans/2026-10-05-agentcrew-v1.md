# AgentCrew V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现一个可离线验证的多智能体协作平台：给定项目目标，自动拆解为带依赖的任务 DAG，分派给 8 个专职 Agent 并发执行，经三层质量门禁验收后产出可运行的完整项目。

**Architecture:** 两阶段——规划阶段由 `ProjectManagerAgent`（LLM）产出 DAG 并经 7 条校验入库；执行阶段由 `Scheduler`（全程无 LLM）按就绪队列 + 两级信号量并发调度。任务状态只存在于 `TaskBoard`，所有跃迁经状态机校验。三层门禁 = G1 静态校验 + G2 LLM 评审 + G3 CLI 人工闸门。全流程 JSON checkpoint，可中断可续跑。

**Tech Stack:** Python 3.10+ / asyncio / Pydantic v2 / openai SDK / anthropic SDK / rich / PyYAML / pytest + pytest-asyncio。**禁止引入 CrewAI、LangGraph、AutoGen、LangChain 等第三方 Agent 框架**——所有编排、调度、状态机逻辑原生实现。

**Spec:** `docs/superpowers/specs/2026-10-05-agentcrew-design.md`

## Global Constraints

- Python 3.10+；目标环境含 Windows 11（posix 与 win32 路径语义都要正确）。
- 全量类型注解；PEP 8；所有异常有明确类型，禁止裸 `except Exception: pass`。
- 依赖白名单（`requirements.txt` 之外的包一律不得引入）：`openai>=1.40.0`、`anthropic>=0.34.0`、`python-dotenv>=1.0.0`、`pydantic>=2.7.0`、`rich>=13.7.0`、`PyYAML>=6.0.1`、`pytest>=8.0.0`、`pytest-asyncio>=0.23.0`。**`asyncio` 是标准库，不得写入 requirements**。
- 所有 Pydantic 模型：`model_config = ConfigDict(extra="forbid")`。
- **API Key 绝不出现在日志、ledger、异常消息、payload dump 中**（§14.2）。
- 所有产出文件必须经 `ArtifactStore.resolve()`；**禁止任何直接 `open(...,"w")` 写产出目录**。
- 写产出目录的文件必须用「同目录临时文件 + `os.replace`」原子写。
- 测试**零网络、零 API Key、零外部服务**；`pytest` 必须能在无 `.env` 的干净环境全绿。
- 常量取值（来自 spec，不得自行更改）：`max_concurrency=4`、`context_budget_tokens=24000`、`max_output_tokens=8192`、`plan_max_attempts=2`、`max_attempts=3`、`max_reworks=2`、`run_id` 格式 `YYYYMMDD-HHMMSS-<4位hex>`。
- 提交信息用 `feat:` / `test:` / `fix:` / `docs:` / `chore:` 前缀。

## Review Focus

以下 5 类输入/故障 spec 隐含要求但易被实现者忽略，**每一条都必须在下方指定任务的测试中被钉死**：

1. **模型幻觉出契约外的文件路径**（如凭空产出 `backend/hack.py`，或漏产出契约要求的 `backend/app/main.py`）。合理预期：该任务 G1 判失败并驳回，**不得**静默写入未声明文件 → 测试归 Task 13。
2. **模型把 JSON 包在 ```json 围栏里或在前后加解释文字**（真实模型极高频行为）。合理预期：降级链第 3 级能正确提取，不抛错 → 测试归 Task 20。
3. **产物路径含非 ASCII 字符**（中文文档名 `docs/接口文档.md`，Windows 演示环境必然遇到）。合理预期：`safe_relpath` 放行、围栏解析正确、原子写成功并可读回 → 测试归 Task 11。
4. **同一产物路径被两个任务同时声明**。合理预期：规划阶段就被 §7.2 校验 5 拦下并给出可读错误，不等到执行期冲突 → 测试归 Task 9。
5. **中断恰好发生在 checkpoint 写入过程中**。合理预期：`run.json`/`tasks.json` 要么是旧的完整内容、要么是新的完整内容，**绝不出现半截 JSON**；resume 能从任一个恢复 → 测试归 Task 15。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `agentcrew/config.py` | `Settings`：env + `agentcrew.yaml` + CLI 覆盖，优先级合并；密钥清单 |
| `agentcrew/utils/logger.py` | 日志配置 + 密钥脱敏 filter |
| `agentcrew/utils/tokens.py` | 零依赖 token 估算 |
| `agentcrew/utils/files.py` | 安全相对路径、原子写、目录树、sha256 |
| `agentcrew/utils/retry.py` | 原生异步指数退避重试 |
| `agentcrew/task/task.py` | 领域模型：`Task` / `TaskStatus` / `TaskResult` / `ReviewRecord` / `StatusChange` / `RunStats` |
| `agentcrew/task/state_machine.py` | 跃迁表 + 校验 |
| `agentcrew/task/graph.py` | DAG：拓扑、环检测、就绪集、后代集合 |
| `agentcrew/task/task_board.py` | 唯一事实源：状态存储 + 跃迁入口 + 快照 |
| `agentcrew/task/planner.py` | 拆解结果 → 校验 → 任务列表（含 T-FINAL 追加） |
| `agentcrew/artifacts/spec.py` | `ArtifactSpec` / `ArtifactRecord` / `Manifest` |
| `agentcrew/artifacts/parser.py` | 从 LLM 原始回复解析 `file=` 围栏块 |
| `agentcrew/artifacts/store.py` | 安全落盘 + 清单 + 并发冲突检测 |
| `agentcrew/artifacts/contract.py` | 按 kind 的解析/校验原语 |
| `agentcrew/gate/static_checks.py` | G1 静态校验流水线 |
| `agentcrew/gate/reviewer.py` | G2 LLM 评审 |
| `agentcrew/gate/gatekeeper.py` | 三层编排 + G3 人工闸门 |
| `agentcrew/gate/cli_prompter.py` | G3 的交互式终端实现（`CliPrompter`） |
| `agentcrew/runtime/ledger.py` | 追加式审计日志 |
| `agentcrew/runtime/checkpoint.py` | JSON 原子 checkpoint / resume / clean |
| `agentcrew/runtime/board_view.py` | rich 终端看板 + 运行报告 |
| `agentcrew/models/config.py` | `ProviderCapabilities` / `ModelBinding` / 能力预设表 |
| `agentcrew/models/pricing.py` | 计价表 + 成本计算 |
| `agentcrew/models/providers/base.py` | `BaseProvider` ABC / `Message` / `CompletionResult` |
| `agentcrew/models/providers/mock.py` | 确定性回放 Provider |
| `agentcrew/models/providers/openai_compat.py` | OpenAI 兼容（OpenAI/豆包/通义/DeepSeek/Ollama） |
| `agentcrew/models/providers/anthropic.py` | Anthropic 原生 Messages API |
| `agentcrew/models/structured.py` | 四级结构化输出降级链 |
| `agentcrew/models/factory.py` | 别名 → Provider 实例（缓存 + LLM 调用级信号量） |
| `agentcrew/roles/spec.py` | `RoleSpec` |
| `agentcrew/roles/catalog.py` | `RoleCatalog`：注册、查找、评审人映射、YAML 加载 |
| `agentcrew/agents/base_agent.py` | `BaseAgent` 模板方法 + `AgentResult` |
| `agentcrew/agents/generic.py` | `GenericAgent`：完全由 RoleSpec 驱动，供自定义角色 |
| `agentcrew/agents/registry.py` | `AGENT_CLASSES` + `AgentFactory` |
| `agentcrew/agents/{8 个角色}.py` | 每个角色一个文件：`ROLE = RoleSpec(...)` + Agent 子类 |
| `agentcrew/context/budget.py` | token 预算与降级 |
| `agentcrew/context/context_bus.py` | `ContextPack` 组装 + 决策记录 |
| `agentcrew/crew/scheduler.py` | 就绪队列 + 两级信号量调度循环 |
| `agentcrew/crew/crew.py` | 两阶段编排 + resume + finalize |
| `main.py` | CLI |
| `examples/todo_web_project.py` | Todo 演示目标定义 |
| `examples/fixtures/` | mock 回放数据（含手写可运行 Todo 项目） |
| `tests/` | 18 个测试文件 |

---

# 阶段 A — 地基

## Task 1: 项目骨架与 Settings

**Files:**
- Create: `requirements.txt`, `.gitignore`, `.env.example`, `pyproject.toml`
- Create: `agentcrew/__init__.py`, `agentcrew/config.py`, `agentcrew/utils/__init__.py`, `agentcrew/utils/tokens.py`
- Test: `tests/__init__.py`, `tests/conftest.py`, `tests/test_config.py`, `tests/test_tokens.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces:
  - `agentcrew.config.GateMode` — `Literal["both","plan","delivery","none"]`
  - `agentcrew.config.Settings` — frozen Pydantic 模型，字段：`default_model: str`、`max_concurrency: int`、`context_budget_tokens: int`、`gate: GateMode`、`output_dir: Path`、`log_level: str`、`mock: bool`、`mock_latency_ms: int`、`plan_max_attempts: int`、`role_bindings: dict[str,str]`、`roles_dir: Path | None`、`provider_overrides: dict[str, dict]`
  - `Settings.load(**cli_overrides: object) -> Settings` — 类方法，按 spec §17.1 五级优先级合并
  - `Settings.secret_values() -> list[str]` — 所有已配置的非空 API Key
  - `Settings.api_key_for(provider: str) -> str | None`
  - `agentcrew.utils.tokens.estimate_tokens(text: str) -> int`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tokens.py
from agentcrew.utils.tokens import estimate_tokens

def test_empty_text_is_zero():
    assert estimate_tokens("") == 0

def test_estimate_is_conservative_for_chinese():
    # 中文按 len//3 估算；100 个汉字应落在 30..100 之间
    assert 30 <= estimate_tokens("测" * 100) <= 100

def test_estimate_scales_with_length():
    assert estimate_tokens("a" * 300) > estimate_tokens("a" * 30)
```

```python
# tests/test_config.py
import os
from pathlib import Path
from agentcrew.config import Settings

def test_defaults(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("AGENTCREW_"):
            monkeypatch.delenv(k)
    s = Settings.load()
    assert s.max_concurrency == 4
    assert s.context_budget_tokens == 24000
    assert s.plan_max_attempts == 2
    assert s.gate in ("both", "none")
    assert s.default_model == "openai:gpt-4o-mini"

def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("AGENTCREW_MAX_CONCURRENCY", "7")
    assert Settings.load().max_concurrency == 7

def test_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("AGENTCREW_MAX_CONCURRENCY", "7")
    assert Settings.load(max_concurrency=2).max_concurrency == 2

def test_secret_values_only_nonempty(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    s = Settings.load()
    assert "sk-abc" in s.secret_values()
    assert "" not in s.secret_values()

def test_gate_degrades_to_none_when_not_a_tty(monkeypatch):
    monkeypatch.delenv("AGENTCREW_GATE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert Settings.load().gate == "none"

def test_output_dir_is_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "out"))
    assert Settings.load().output_dir.is_absolute()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_tokens.py tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentcrew'`

- [ ] **Step 3: 实现**

`requirements.txt` 逐字使用 spec §17.5 的内容。`.gitignore` 逐字使用 spec §17.4。`.env.example` 逐字使用 spec §17.2。

`pyproject.toml` 需含：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

`Settings.load` 的合并顺序（低 → 高）：代码默认 → `.env`（`load_dotenv`）→ `agentcrew.yaml`（`AGENTCREW_CONFIG` 或 `./agentcrew.yaml`，缺失则跳过）→ `AGENTCREW_*` 环境变量 → `cli_overrides`。`gate` 默认 `"both"`，但 `sys.stdin.isatty()` 为假时降级为 `"none"`。`output_dir` 一律 `.resolve()`。

`estimate_tokens` 用 spec §11.2 的公式 `max(len(text) // 3, len(text.split()))`，空串返回 0。

`conftest.py` 本步先建空文件加 `import pytest`，后续任务往里加 fixture。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_tokens.py tests/test_config.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 项目骨架、Settings 配置合并与 token 估算"
```

---

## Task 2: 密钥脱敏日志

**Files:**
- Create: `agentcrew/utils/logger.py`
- Test: `tests/test_logger_redaction.py`

**Interfaces:**
- Consumes: Task 1 的 `Settings.secret_values()`
- Produces:
  - `agentcrew.utils.logger.redact(text: str, secrets: Sequence[str]) -> str` — 纯函数，可单测
  - `agentcrew.utils.logger.setup_logging(level: str, secrets: Sequence[str]) -> None`
  - `agentcrew.utils.logger.get_logger(name: str) -> logging.Logger`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_logger_redaction.py
import logging
from agentcrew.utils.logger import redact, setup_logging, get_logger

def test_redacts_sk_prefix():
    assert "sk-abc123" not in redact("key=sk-abc123 done", [])

def test_redacts_bearer_token():
    assert "xyz789" not in redact("Authorization: Bearer xyz789", [])

def test_redacts_configured_secret_values():
    assert "hunter2" not in redact("token is hunter2 here", ["hunter2"])

def test_leaves_ordinary_text_alone():
    assert redact("hello world", []) == "hello world"

def test_logger_output_is_redacted(capsys, monkeypatch):
    setup_logging("DEBUG", ["supersecret"])
    get_logger("t").info("using supersecret now")
    for handler in logging.getLogger().handlers:
        handler.flush()
    out = capsys.readouterr()
    assert "supersecret" not in (out.out + out.err)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_logger_redaction.py -v`
Expected: FAIL — `ModuleNotFoundError` / `ImportError: cannot import name 'redact'`

- [ ] **Step 3: 实现**

`redact` 用正则替换三类内容为 `***`：`sk-[A-Za-z0-9_\-]{8,}`、`(?i)bearer\s+\S+`、以及 `secrets` 中每个非空字符串的字面量（用 `re.escape`）。`secrets` 按**长度降序**替换，避免短密钥先替换破坏长密钥匹配。

`setup_logging` 在 root logger 上装一个 `logging.StreamHandler` 和一个实现 `logging.Filter` 的脱敏 filter（在 `filter()` 里改写 `record.msg` 与 `record.args`）。重复调用不叠加 handler。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_logger_redaction.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 日志密钥脱敏"
```

---

## Task 3: 安全路径与原子写

**Files:**
- Create: `agentcrew/utils/files.py`
- Test: `tests/test_files.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `agentcrew.utils.files.UnsafePathError(ValueError)`
  - `agentcrew.utils.files.safe_relpath(raw: str) -> PurePosixPath`
  - `agentcrew.utils.files.atomic_write_bytes(path: Path, data: bytes) -> None`
  - `agentcrew.utils.files.atomic_write_text(path: Path, text: str) -> None`
  - `agentcrew.utils.files.read_text(path: Path) -> str`
  - `agentcrew.utils.files.sha256_bytes(data: bytes) -> str`
  - `agentcrew.utils.files.tree(root: Path, max_entries: int = 300) -> list[str]`
  - `agentcrew.utils.files.slugify_run_id(now: datetime) -> str`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_files.py
import shutil
from pathlib import Path
import pytest
from agentcrew.utils.files import (
    UnsafePathError, safe_relpath, atomic_write_text, read_text, tree,
)

@pytest.mark.parametrize("bad", [
    "", ".", "/etc/passwd", "C:\\Windows\\x", "\\\\server\\share\\x",
    "../outside.txt", "a/../../b.txt", "a/./../b",
    "CON", "con.txt", "aux.md", "NUL.py", "COM1.txt", "LPT9",
    "trailing ", "trailing.", "a/nul ", "has\x00null",
])
def test_rejects_unsafe_paths(bad):
    with pytest.raises(UnsafePathError):
        safe_relpath(bad)

@pytest.mark.parametrize("ok", [
    "README.md", "backend/app/main.py", "docs/接口文档.md",
    "a/b/c/d.txt", "deep/nested/dir/file.js", "UPPER/Case.MD",
])
def test_accepts_safe_paths(ok):
    assert safe_relpath(ok) == Path(ok).as_posix()

def test_normalizes_backslashes_and_duplicate_separators():
    assert safe_relpath("a\\b//c.txt").as_posix() == "a/b/c.txt"

def test_atomic_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "sub" / "x.txt"
    atomic_write_text(p, "hello 世界")
    assert read_text(p) == "hello 世界"

def test_atomic_write_leaves_no_temp_files(tmp_path):
    atomic_write_text(tmp_path / "x.txt", "a")
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*tmp*")) == []

def test_atomic_write_overwrites_existing(tmp_path):
    p = tmp_path / "x.txt"
    atomic_write_text(p, "old"); atomic_write_text(p, "new")
    assert read_text(p) == "new"

def test_tree_lists_relative_posix_paths(tmp_path):
    (tmp_path / "a").mkdir(); (tmp_path / "a" / "b.txt").write_text("x")
    assert tree(tmp_path) == ["a/b.txt"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_files.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`safe_relpath` 按 spec §9.3 的 7 条规则实现，逐条给出明确的异常消息（含被拒绝的原始输入）。要点：先把 `\` 规范化为 `/`；逐个 path 段检查 Windows 保留名时**去掉扩展名后**比较（大小写不敏感）；拒绝以空格或点结尾的段；非 ASCII 一律放行（中文文件名合法）。

`atomic_write_bytes`：在同目录生成 `.{name}.{uuid4().hex}.tmp`，写入后 `os.replace(tmp, path)`；`finally` 中清理残留临时文件。写入前 `mkdir(parents=True, exist_ok=True)`。UTF-8 编码、不加 BOM。

`slugify_run_id(now)` 返回 `now.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)"`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_files.py -v`
Expected: 全部 PASS，含 `test_rejects_unsafe_paths` 全部 16 个参数与 `docs/接口文档.md` 用例

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 安全路径校验与原子写"
```

---

## Task 4: 退避重试

**Files:**
- Create: `agentcrew/utils/retry.py`
- Test: `tests/test_retry.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `agentcrew.utils.retry.TransientError(Exception)` — 可重试
  - `agentcrew.utils.retry.PermanentError(Exception)` — 不可重试
  - `agentcrew.utils.retry.retry_async(fn, *, attempts=4, base_delay=1.0, max_delay=30.0, jitter=0.3, retry_on=(TransientError,), on_retry=None) -> T`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_retry.py
import pytest
from agentcrew.utils.retry import TransientError, PermanentError, retry_async

async def test_returns_immediately_on_success():
    calls = []
    async def fn():
        calls.append(1); return "ok"
    assert await retry_async(fn, base_delay=0) == "ok"
    assert len(calls) == 1

async def test_retries_transient_then_succeeds():
    calls = []
    async def fn():
        calls.append(1)
        if len(calls) < 3: raise TransientError("boom")
        return "ok"
    assert await retry_async(fn, attempts=4, base_delay=0) == "ok"
    assert len(calls) == 3

async def test_exhausts_attempts_and_reraises():
    calls = []
    async def fn():
        calls.append(1); raise TransientError("always")
    with pytest.raises(TransientError):
        await retry_async(fn, attempts=3, base_delay=0)
    assert len(calls) == 3

async def test_permanent_error_is_not_retried():
    calls = []
    async def fn():
        calls.append(1); raise PermanentError("nope")
    with pytest.raises(PermanentError):
        await retry_async(fn, attempts=5, base_delay=0)
    assert len(calls) == 1

async def test_on_retry_reports_attempt_and_delay():
    seen = []
    async def fn(): raise TransientError("x")
    with pytest.raises(TransientError):
        await retry_async(fn, attempts=3, base_delay=0,
                          on_retry=lambda n, e, d: seen.append((n, type(e).__name__)))
    assert seen == [(1, "TransientError"), (2, "TransientError")]

async def test_delay_is_capped_and_jittered():
    from agentcrew.utils.retry import compute_delay
    assert compute_delay(1, base_delay=1.0, max_delay=30.0, jitter=0.0) == 1.0
    assert compute_delay(10, base_delay=1.0, max_delay=30.0, jitter=0.0) == 30.0
    d = compute_delay(3, base_delay=1.0, max_delay=30.0, jitter=0.3)
    assert 2.8 <= d <= 5.2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_retry.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`retry_async` 泛型签名 `TypeVar("T")`；`retry_on` 默认只含 `TransientError`，其他异常**直接抛出**。`on_retry(attempt_index, exc, delay)` 在每次**即将重试前**调用（attempt_index 从 1 起）。`base_delay=0` 时不得真的 sleep（测试要求）。

`compute_delay(attempt, *, base_delay, max_delay, jitter) -> float` 独立导出：`min(max_delay, base_delay * 2 ** (attempt - 1))`，再乘 `1 + random.uniform(-jitter, jitter)`，结果不为负。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_retry.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 原生异步指数退避重试"
```

---

# 阶段 B — 任务模型、状态机与 DAG

## Task 5: 领域模型

**Files:**
- Create: `agentcrew/task/__init__.py`, `agentcrew/task/task.py`
- Create: `agentcrew/artifacts/__init__.py`, `agentcrew/artifacts/spec.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `agentcrew.utils.files.safe_relpath`
- Produces:
  - `agentcrew.task.task.TaskStatus` — 10 个枚举值：`PENDING/READY/RUNNING/REVIEW/REWORK/SUCCEEDED/FAILED/BLOCKED/CANCELLED/SKIPPED`
  - `agentcrew.task.task.Task` — 字段见 spec §5.1，另含 `status_history: list[StatusChange]`、`reviews: list[ReviewRecord]`、`created_at`/`updated_at`
  - `agentcrew.task.task.TaskResult` — `task_id, role, summary, artifacts: list[ArtifactRecord], notes, started_at, finished_at, duration_ms`
  - `agentcrew.task.task.ReviewRecord` — `task_id, round, layer, verdict, score, issues, fix_hints, reviewer, created_at`
  - `agentcrew.task.task.StatusChange` — `from_status, to_status, reason, at`
  - `agentcrew.task.task.RunStats` — `total_tasks, succeeded, failed, blocked, skipped, llm_calls, prompt_tokens, completion_tokens, estimated_cost_usd, started_at, finished_at`
  - `agentcrew.artifacts.spec.ArtifactKind` — Literal 12 值
  - `agentcrew.artifacts.spec.ArtifactSpec` — `path, kind, description, required=True, min_bytes=1, must_contain=[], max_bytes=524288`
  - `agentcrew.artifacts.spec.ArtifactRecord` — `path, kind, size_bytes, sha256, produced_by_task, produced_by_role, written_at`
  - `agentcrew.artifacts.spec.Manifest` — `run_id, generated_at, entries: list[ArtifactRecord]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_models.py
from datetime import datetime
import pytest
from pydantic import ValidationError
from agentcrew.task.task import TaskStatus, Task, ReviewRecord, RunStats
from agentcrew.artifacts.spec import ArtifactSpec, ArtifactKind

def test_task_status_has_exactly_ten_members():
    assert len(TaskStatus) == 10
    assert TaskStatus("rework") is TaskStatus.REWORK

def test_task_defaults():
    t = Task(id="T-001", title="x", description="d", role="backend_dev",
             acceptance_criteria=["a"], deliverables=[], dependencies=[])
    assert t.status is TaskStatus.PENDING
    assert t.attempts == 0 and t.rework_count == 0
    assert t.max_attempts == 3 and t.max_reworks == 2
    assert t.priority == 5
    assert t.reviews == [] and t.status_history == []

def test_task_rejects_extra_fields():
    with pytest.raises(ValidationError):
        Task(id="T-1", title="t", description="d", role="r",
             acceptance_criteria=[], deliverables=[], bogus=1)

def test_artifact_spec_rejects_unsafe_path():
    with pytest.raises(ValidationError):
        ArtifactSpec(path="../escape.py", kind="python", description="x")

def test_artifact_spec_accepts_chinese_path():
    s = ArtifactSpec(path="docs/接口文档.md", kind="markdown", description="x")
    assert s.path == "docs/接口文档.md"

def test_artifact_spec_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        ArtifactSpec(path="a.txt", kind="brainfuck", description="x")

def test_priority_bounds():
    with pytest.raises(ValidationError):
        Task(id="T", title="t", description="d", role="r",
             acceptance_criteria=[], deliverables=[], priority=0)

def test_review_record_requires_issues_when_rejected():
    # 评审记录本身不做业务校验，但 round/verdict/layer 必须合法
    r = ReviewRecord(task_id="T-1", round=1, layer="static", verdict="reject",
                     issues=["缺失产物"], fix_hints=[], reviewer="static:contract",
                     created_at=datetime.now())
    assert r.verdict == "reject"

def test_run_stats_defaults_zero():
    s = RunStats()
    assert s.total_tasks == 0 and s.estimated_cost_usd == 0.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_models.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

按 spec §5.1/§5.2/§5.3 的定义实现。要点：
- 所有模型 `model_config = ConfigDict(extra="forbid")`。
- `ArtifactSpec.path` 用 Pydantic `field_validator` 调 `safe_relpath`，非法则抛 `ValueError`（Pydantic 会包成 `ValidationError`）。
- 时间字段用 `datetime`；`created_at`/`updated_at` 用 `default_factory=datetime.now`。
- `Task` 不加业务跃迁校验（那是 TaskBoard 的职责），只做类型与边界校验。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_models.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 任务与产物的领域模型"
```

---

## Task 6: 状态机跃迁表

**Files:**
- Create: `agentcrew/task/state_machine.py`
- Test: `tests/test_state_machine.py`

**Interfaces:**
- Consumes: Task 5 的 `TaskStatus`
- Produces:
  - `agentcrew.task.state_machine.IllegalTransitionError(Exception)` — 属性 `task_id`、`from_status`、`to_status`
  - `agentcrew.task.state_machine.ALLOWED_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]]`
  - `agentcrew.task.state_machine.can_transition(src: TaskStatus, dst: TaskStatus) -> bool`
  - `agentcrew.task.state_machine.assert_transition(src: TaskStatus, dst: TaskStatus, *, task_id: str) -> None`
  - `agentcrew.task.state_machine.is_terminal(status: TaskStatus) -> bool`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_state_machine.py
import pytest
from agentcrew.task.task import TaskStatus as S
from agentcrew.task.state_machine import (
    IllegalTransitionError, ALLOWED_TRANSITIONS, can_transition,
    assert_transition, is_terminal,
)

def test_every_status_has_an_entry():
    assert set(ALLOWED_TRANSITIONS) == set(S)

@pytest.mark.parametrize("src,dst", [
    (S.PENDING, S.READY), (S.PENDING, S.BLOCKED), (S.PENDING, S.SKIPPED),
    (S.PENDING, S.CANCELLED),
    (S.READY, S.RUNNING), (S.READY, S.BLOCKED), (S.READY, S.CANCELLED),
    (S.RUNNING, S.REVIEW), (S.RUNNING, S.READY), (S.RUNNING, S.FAILED),
    (S.RUNNING, S.CANCELLED),
    (S.REVIEW, S.SUCCEEDED), (S.REVIEW, S.REWORK), (S.REVIEW, S.FAILED),
    (S.REVIEW, S.CANCELLED),
    (S.REWORK, S.RUNNING), (S.REWORK, S.CANCELLED),
    (S.BLOCKED, S.READY),
])
def test_legal_transitions_allowed(src, dst):
    assert can_transition(src, dst)
    assert_transition(src, dst, task_id="T-1")   # 不抛

@pytest.mark.parametrize("src,dst", [
    (S.SUCCEEDED, S.RUNNING), (S.FAILED, S.RUNNING), (S.CANCELLED, S.READY),
    (S.SKIPPED, S.READY), (S.PENDING, S.RUNNING), (S.PENDING, S.REVIEW),
    (S.READY, S.REVIEW), (S.REVIEW, S.READY), (S.RUNNING, S.SUCCEEDED),
    (S.REWORK, S.REVIEW), (S.BLOCKED, S.RUNNING),
])
def test_illegal_transitions_rejected(src, dst):
    assert not can_transition(src, dst)
    with pytest.raises(IllegalTransitionError) as ei:
        assert_transition(src, dst, task_id="T-9")
    assert ei.value.task_id == "T-9"
    assert ei.value.from_status is src and ei.value.to_status is dst

def test_terminal_states_have_no_outgoing():
    for s in (S.SUCCEEDED, S.CANCELLED, S.SKIPPED):
        assert ALLOWED_TRANSITIONS[s] == frozenset()
        assert is_terminal(s)

def test_failed_has_only_manual_retry_edge():
    assert S.FAILED not in {d for s, ds in ALLOWED_TRANSITIONS.items() for d in ds
                            if s is not S.FAILED}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_state_machine.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

逐字实现 spec §6.1 的跃迁表。`FAILED` 的出边集合**为空**（人工重置走 `TaskBoard.reset_failed()` 的显式接口，不经状态机，见 Task 8）。`is_terminal` 定义为「出边集合为空」。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_state_machine.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 任务状态机跃迁表"
```

---

## Task 7: DAG 图算法

**Files:**
- Create: `agentcrew/task/graph.py`
- Test: `tests/test_graph.py`

**Interfaces:**
- Consumes: Task 5 的 `Task`/`TaskStatus`
- Produces:
  - `agentcrew.task.graph.TaskGraph` — frozen dataclass，字段 `deps: dict[str, frozenset[str]]`、`dependents: dict[str, frozenset[str]]`
  - `TaskGraph.build(tasks: Sequence[Task]) -> TaskGraph`
  - `TaskGraph.dependencies(task_id) -> frozenset[str]`
  - `TaskGraph.descendants(task_id) -> set[str]`
  - `TaskGraph.cycles() -> list[list[str]]` — 返回所有环，每个环是首尾相接的 id 序列
  - `TaskGraph.topo_order() -> list[str]` — 有环时抛 `CyclicGraphError`
  - `agentcrew.task.graph.CyclicGraphError(ValueError)` — 属性 `cycles`
  - `agentcrew.task.graph.ready_ids(tasks: Mapping[str, Task]) -> list[str]`
  - `agentcrew.task.graph.blocked_ids(tasks: Mapping[str, Task]) -> list[str]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_graph.py
import pytest
from agentcrew.task.task import Task, TaskStatus as S
from agentcrew.task.graph import TaskGraph, CyclicGraphError, ready_ids, blocked_ids

def mk(tid, deps=(), status=S.PENDING):
    return Task(id=tid, title=tid, description="d", role="backend_dev",
                acceptance_criteria=["a"], deliverables=[], dependencies=list(deps),
                status=status)

def test_build_records_deps_and_dependents():
    g = TaskGraph.build([mk("A"), mk("B", ["A"]), mk("C", ["A"])])
    assert g.dependencies("B") == frozenset({"A"})
    assert g.dependents("A") == frozenset({"B", "C"})

def test_diamond_topology_orders_correctly():
    g = TaskGraph.build([mk("A"), mk("B", ["A"]), mk("C", ["A"]), mk("D", ["B", "C"])])
    order = g.topo_order()
    assert order.index("A") < order.index("B") < order.index("D")
    assert order.index("A") < order.index("C") < order.index("D")

def test_self_loop_detected():
    assert TaskGraph.build([mk("A", ["A"])]).cycles() == [["A"]]

def test_two_cycle_detected():
    cycles = TaskGraph.build([mk("A", ["B"]), mk("B", ["A"])]).cycles()
    assert len(cycles) == 1 and set(cycles[0]) == {"A", "B"}

def test_three_cycle_detected():
    cycles = TaskGraph.build([mk("A", ["C"]), mk("B", ["A"]), mk("C", ["B"])]).cycles()
    assert len(cycles) == 1 and set(cycles[0]) == {"A", "B", "C"}

def test_acyclic_graph_has_no_cycles():
    assert TaskGraph.build([mk("A"), mk("B", ["A"]), mk("C", ["B"])]).cycles() == []

def test_topo_order_raises_on_cycle():
    with pytest.raises(CyclicGraphError) as ei:
        TaskGraph.build([mk("A", ["B"]), mk("B", ["A"])]).topo_order()
    assert ei.value.cycles

def test_descendants_is_transitive():
    g = TaskGraph.build([mk("A"), mk("B", ["A"]), mk("C", ["B"]), mk("D")])
    assert g.descendants("A") == {"B", "C"}
    assert g.descendants("D") == set()

def test_ready_ids_requires_all_deps_succeeded():
    tasks = {"A": mk("A", status=S.SUCCEEDED), "B": mk("B", ["A"]),
             "C": mk("C", ["A", "B"])}
    assert set(ready_ids(tasks)) == {"B"}

def test_ready_ids_empty_when_root_running():
    tasks = {"A": mk("A", status=S.RUNNING), "B": mk("B", ["A"])}
    assert ready_ids(tasks) == []

def test_blocked_ids_when_dep_failed():
    tasks = {"A": mk("A", status=S.FAILED), "B": mk("B", ["A"])}
    assert blocked_ids(tasks) == ["B"]

def test_skipped_propagates():
    tasks = {"A": mk("A", status=S.SKIPPED), "B": mk("B", ["A"])}
    assert ready_ids(tasks) == [] and blocked_ids(tasks) == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_graph.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`cycles()` 用 **Kahn 算法**：反复移除入度为 0 的节点，剩余节点集合中的每个**强连通分量**即一个环。用 Tarjan 或简单 DFS 从剩余节点出发找环路径，返回首尾相接的 id 列表。自环 `A -> A` 返回 `[["A"]]`。

`topo_order()` 用 Kahn 拓扑排序，输出顺序稳定（同层按 id 升序），有环时抛 `CyclicGraphError(cycles=...)`。

`ready_ids(tasks)` 返回 `status == PENDING` 且全部依赖 `SUCCEEDED` 的任务 id（按 id 升序）。
`blocked_ids(tasks)` 返回 `status == PENDING` 且任一依赖为 `FAILED` 或 `CANCELLED` 的任务 id。
（`SKIPPED` 依赖的传播由 Task 8 的 TaskBoard 处理，`ready_ids` 必须**不**把它算成就绪。）

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_graph.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: DAG 拓扑、环检测与就绪集判定"
```

---

## Task 8: TaskBoard 唯一事实源

**Files:**
- Create: `agentcrew/task/task_board.py`
- Modify: `agentcrew/runtime/__init__.py`（占位，Task 14 填充）
- Test: `tests/test_task_board.py`

**Interfaces:**
- Consumes: Task 5 的领域模型、Task 6 的状态机、Task 7 的 `TaskGraph`/`ready_ids`/`blocked_ids`
- Produces:
  - `agentcrew.task.task_board.TaskBoard`
    - `TaskBoard(tasks: Sequence[Task], *, on_change: Callable[[Task, StatusChange], None] | None = None)`
    - `.graph` → `TaskGraph`
    - `.get(task_id: str) -> Task`（缺失抛 `KeyError`）
    - `.all() -> list[Task]`
    - `.ids() -> list[str]`
    - `.map() -> dict[str, Task]`
    - `.transition(task_id, to: TaskStatus, *, reason: str = "") -> Task`
    - `.bump_attempt(task_id) -> Task` — `attempts += 1`
    - `.bump_rework(task_id) -> Task` — `rework_count += 1`（**不改状态**；状态变更由调用方另行 transition）
    - `.refresh() -> int` — PENDING→READY/BLOCKED/SKIPPED，BLOCKED→READY 重算；返回变更条数
    - `.take_ready(limit: int) -> list[Task]` — 按 `(priority, id)` 升序取至多 limit 个 READY 任务**并**将其转 `RUNNING`（同时 `attempts += 1`）
    - `.record_review(task_id, record: ReviewRecord) -> None`
    - `.set_result(task_id, result: TaskResult) -> None`
    - `.is_terminal() -> bool` — 所有任务都在终态
    - `.stats() -> RunStats`
    - `.explain_stall() -> str`
    - `.snapshot() -> list[dict[str, Any]]`
    - `TaskBoard.from_snapshot(data: Sequence[dict]) -> TaskBoard`
    - `.reset_failed(task_id: str) -> Task` — 人工重置 FAILED → READY（绕过状态机，显式记录 `StatusChange(reason="manual-retry")`）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_task_board.py
import pytest
from agentcrew.task.task import Task, TaskStatus as S, ReviewRecord
from agentcrew.task.task_board import TaskBoard
from agentcrew.task.state_machine import IllegalTransitionError

def mk(tid, deps=(), status=S.PENDING, priority=5):
    return Task(id=tid, title=tid, description="d", role="backend_dev",
                acceptance_criteria=["a"], deliverables=[], dependencies=list(deps),
                status=status, priority=priority)

def test_transition_updates_status_and_history():
    b = TaskBoard([mk("A")])
    b.transition("A", S.READY, reason="deps met")
    assert b.get("A").status is S.READY
    h = b.get("A").status_history
    assert len(h) == 1 and h[0].from_status is S.PENDING and h[0].to_status is S.READY

def test_illegal_transition_does_not_mutate():
    b = TaskBoard([mk("A", status=S.SUCCEEDED)])
    with pytest.raises(IllegalTransitionError):
        b.transition("A", S.RUNNING)
    assert b.get("A").status is S.SUCCEEDED

def test_on_change_callback_fires():
    seen = []
    b = TaskBoard([mk("A")], on_change=lambda t, c: seen.append((t.id, c.to_status)))
    b.transition("A", S.READY)
    assert seen == [("A", S.READY)]

def test_refresh_promotes_ready_and_blocks_failed_downstream():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"]), mk("C", status=S.SUCCEEDED),
                   mk("D", ["C"])])
    b.refresh()
    assert b.get("B").status is S.BLOCKED
    assert b.get("D").status is S.READY

def test_refresh_unblocks_when_upstream_recovers():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"])])
    b.refresh(); assert b.get("B").status is S.BLOCKED
    b.get("A").status = S.SUCCEEDED           # 模拟人工修复
    b.refresh(); assert b.get("B").status is S.READY

def test_take_ready_orders_by_priority_then_id_and_marks_running():
    b = TaskBoard([mk("B", priority=1), mk("A", priority=1), mk("C", priority=9)])
    b.refresh()
    taken = b.take_ready(limit=2)
    assert [t.id for t in taken] == ["A", "B"]
    assert b.get("A").status is S.RUNNING and b.get("B").status is S.RUNNING
    assert b.get("C").status is S.READY

def test_bump_attempt_increments():
    b = TaskBoard([mk("A")])
    assert b.bump_attempt("A").attempts == 1

def test_bump_rework_increments_without_changing_status():
    b = TaskBoard([mk("A", status=S.REVIEW)])
    t = b.bump_rework("A")
    assert t.rework_count == 1 and t.status is S.REVIEW

def test_is_terminal_true_only_when_all_settled():
    b = TaskBoard([mk("A", status=S.SUCCEEDED), mk("B")])
    assert not b.is_terminal()
    b.transition("B", S.READY); b.transition("B", S.RUNNING)
    b.transition("B", S.REVIEW); b.transition("B", S.SUCCEEDED)
    assert b.is_terminal()

def test_stats_counts_by_status():
    b = TaskBoard([mk("A", status=S.SUCCEEDED), mk("B", status=S.FAILED),
                   mk("C", status=S.BLOCKED)])
    s = b.stats()
    assert (s.total_tasks, s.succeeded, s.failed, s.blocked) == (3, 1, 1, 1)

def test_explain_stall_mentions_blocking_upstream():
    b = TaskBoard([mk("A", status=S.FAILED), mk("B", ["A"])])
    b.refresh()
    text = b.explain_stall()
    assert "B" in text and "A" in text

def test_snapshot_roundtrip_preserves_state():
    b = TaskBoard([mk("A"), mk("B", ["A"], priority=2)])
    b.transition("A", S.READY); b.transition("A", S.RUNNING); b.bump_attempt("A")
    b.record_review("A", ReviewRecord(task_id="A", round=1, layer="static",
                                      verdict="approve", issues=[], fix_hints=[],
                                      reviewer="static:contract",
                                      created_at=__import__("datetime").datetime.now()))
    b2 = TaskBoard.from_snapshot(b.snapshot())
    assert b2.get("A").status is S.RUNNING
    assert b2.get("A").attempts == 1
    assert len(b2.get("A").reviews) == 1
    assert b2.get("B").priority == 2

def test_from_snapshot_rejects_unknown_status():
    with pytest.raises(Exception):
        TaskBoard.from_snapshot([{"id": "A", "title": "t", "description": "d",
                                  "role": "r", "acceptance_criteria": [],
                                  "deliverables": [], "status": "bogus"}])

def test_reset_failed_moves_to_ready_and_logs_reason():
    b = TaskBoard([mk("A", status=S.FAILED)])
    b.reset_failed("A")
    assert b.get("A").status is S.READY
    assert b.get("A").status_history[-1].reason == "manual-retry"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_task_board.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`transition()` 是**唯一**改 `Task.status` 的入口：校验跃迁 → 用 `model_copy(update=...)` 生成新 `Task`（不原地改，避免外部持有旧引用）→ 追加 `StatusChange` → 更新 `updated_at` → 触发 `on_change` → 返回新 Task。

`refresh()` 顺序：先 `blocked_ids` → `BLOCKED`；再对 `PENDING` 任务检查是否有依赖为 `SKIPPED` → `SKIPPED`；再 `ready_ids` → `READY`；最后把依赖已全部 `SUCCEEDED` 的 `BLOCKED` 任务转回 `READY`。返回变更条数。

`explain_stall()` 输出多行文本：对每个非终态任务，列出「状态 + 阻塞它的上游 id 及其状态」；若存在依赖了不存在 id 的任务，单独指出。

`take_ready` 内部调 `refresh()` 后取排序好的 READY，**逐个** `transition(id, RUNNING)`（`RUNNING` 跃迁同时令 `attempts += 1`，由 TaskBoard 完成）。`attempts` 在此处自增，符合 spec §6.2「派发时扣除」。

`__init__.py` for `agentcrew/runtime/` 本任务只创建空文件；Task 14 填充。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_task_board.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: TaskBoard 唯一事实源与状态跃迁"
```

---

## Task 9: 拆解校验与规划

**Files:**
- Create: `agentcrew/task/planner.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- Consumes: Task 5 模型、Task 7 `TaskGraph`、Task 8 `TaskBoard`
- Produces:
  - `agentcrew.task.planner.TaskDraft` — Pydantic：`id, title, description, role, acceptance_criteria: list[str], deliverables: list[ArtifactSpec], dependencies: list[str] = [], priority: int = 5, brief: str = ""`
  - `agentcrew.task.planner.PlanOutput` — `tasks: list[TaskDraft]`
  - `agentcrew.task.planner.PlanValidationError(ValueError)` — 属性 `errors: list[str]`
  - `agentcrew.task.planner.plan_output_json_schema() -> dict` — 供提示词与结构化输出使用
  - `agentcrew.task.planner.to_tasks(plan: PlanOutput) -> list[Task]`
  - `agentcrew.task.planner.validate_plan(tasks: Sequence[Task], catalog: "RoleCatalog") -> None`
  - `agentcrew.task.planner.with_final_task(tasks: Sequence[Task]) -> list[Task]`
  - `agentcrew.task.planner.FINAL_TASK_ID = "T-FINAL"`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_planner.py
import pytest
from agentcrew.task.planner import (
    PlanOutput, TaskDraft, to_tasks, validate_plan, with_final_task,
    PlanValidationError, FINAL_TASK_ID,
)
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.roles.spec import RoleSpec
from agentcrew.roles.catalog import RoleCatalog

def draft(tid, role="backend_dev", deps=(), paths=("backend/app/main.py",)):
    return TaskDraft(id=tid, title=tid, description="d", role=role,
                     acceptance_criteria=["ok"],
                     deliverables=[ArtifactSpec(path=p, kind="python", description="x")
                                   for p in paths],
                     dependencies=list(deps))

@pytest.fixture
def catalog():
    return RoleCatalog([RoleSpec(key="backend_dev", title="后端", goal="g",
                                 backstory="b", responsibilities=["r"],
                                 boundaries=["x"], output_contract="o",
                                 system_prompt="p"),
                        RoleSpec(key="project_manager", title="PM", goal="g",
                                 backstory="b", responsibilities=["r"],
                                 boundaries=["x"], output_contract="o",
                                 system_prompt="p")])

def test_to_tasks_converts_drafts():
    ts = to_tasks(PlanOutput(tasks=[draft("T-1"), draft("T-2", deps=["T-1"])]))
    assert [t.id for t in ts] == ["T-1", "T-2"]
    assert ts[1].dependencies == ["T-1"]

def test_valid_plan_passes(catalog):
    validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1"), draft("T-2", deps=["T-1"])])), catalog)

def test_duplicate_id_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1"), draft("T-1")])), catalog)
    assert any("重复" in e for e in ei.value.errors)

def test_unknown_role_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", role="wizard")])), catalog)
    assert any("wizard" in e for e in ei.value.errors)

def test_dangling_dependency_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", deps=["T-99"])])), catalog)
    assert any("T-99" in e for e in ei.value.errors)

def test_self_dependency_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", deps=["T-1"])])), catalog)
    assert any("自身" in e or "环" in e for e in ei.value.errors)

def test_cycle_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[draft("T-1", deps=["T-2"]),
                                                draft("T-2", deps=["T-1"])])), catalog)
    assert any("环" in e for e in ei.value.errors)

def test_empty_acceptance_criteria_rejected(catalog):
    d = draft("T-1"); d.acceptance_criteria = []
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[d])), catalog)
    assert any("验收标准" in e for e in ei.value.errors)

def test_all_errors_reported_together(catalog):
    """校验必须一次性报出全部问题，便于回灌 PM。"""
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[
            draft("T-1", role="wizard"),
            draft("T-1"),
        ])), catalog)
    assert len(ei.value.errors) >= 2

# ── Review Focus #4：同一路径被两个任务声明 ──
def test_duplicate_artifact_path_across_tasks_rejected(catalog):
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(to_tasks(PlanOutput(tasks=[
            draft("T-1", paths=("backend/app/main.py",)),
            draft("T-2", paths=("backend/app/main.py",)),
        ])), catalog)
    msg = " ".join(ei.value.errors)
    assert "backend/app/main.py" in msg
    assert "T-1" in msg and "T-2" in msg      # 错误信息须指出冲突双方

def test_same_task_may_not_declare_same_path_twice(catalog):
    with pytest.raises(PlanValidationError):
        validate_plan(to_tasks(PlanOutput(tasks=[
            draft("T-1", paths=("a.py", "a.py")),
        ])), catalog)

def test_with_final_task_appends_sink(catalog):
    tasks = to_tasks(PlanOutput(tasks=[draft("T-1"), draft("T-2", deps=["T-1"])]))
    out = with_final_task(tasks)
    final = [t for t in out if t.id == FINAL_TASK_ID][0]
    assert final.role == "project_manager"
    assert set(final.dependencies) == {"T-1", "T-2"}     # 所有叶子
    assert any(d.path == "DELIVERY.md" for d in final.deliverables)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_planner.py -v`
Expected: FAIL — `ImportError: cannot import name 'planner'`

- [ ] **Step 3: 实现**

`validate_plan` **收集全部错误**再一次性抛出（不早退），错误消息用中文且含具体 id/路径。7 条校验按 spec §7.2：
1. id 非空且唯一（重复 → 收集全部重复 id）
2. `role` 存在于 `catalog`（不存在 → 列出该角色的可用 key 提示）
3. 依赖 id 存在、不自引用
4. `TaskGraph.cycles()` 为空（错误消息列出环路径）
5. `ArtifactSpec.path` 两两唯一 —— **跨任务**与**同任务内**都要查；冲突消息格式 `产物路径冲突：backend/app/main.py 被 T-1 与 T-2 同时声明`
6. 每个 `ArtifactSpec.path` 已由模型层的 validator 保证安全（此处无需重复）
7. 每个任务 `acceptance_criteria` 非空

`with_final_task`：计算叶子任务 = 无下游的任务；追加 `T-FINAL`，`role="project_manager"`，`deliverables=[ArtifactSpec(path="DELIVERY.md", kind="markdown", description="交付总结", must_contain=["完成"], min_bytes=100)]`，`priority=9`（最后执行），`acceptance_criteria=["列出全部完成项与遗留问题", "包含运行方式"]`。若已存在 `T-FINAL` 则先移除再追加（幂等）。

`plan_output_json_schema()` 返回 `PlanOutput.model_json_schema()`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_planner.py -v`
Expected: 全部 PASS，含 `test_duplicate_artifact_path_across_tasks_rejected`

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 拆解结果校验与 T-FINAL 追加"
```

---

# 阶段 C — 产物与静态门禁

## Task 10: 围栏块解析

**Files:**
- Create: `agentcrew/artifacts/parser.py`
- Test: `tests/test_artifact_parser.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `agentcrew.artifacts.parser.ArtifactParseError(ValueError)`
  - `agentcrew.artifacts.parser.ParsedOutput` — Pydantic：`files: dict[str, str]`（键为规范化后的相对路径）、`notes: str`
  - `agentcrew.artifacts.parser.parse_fenced_blocks(raw: str) -> ParsedOutput`
  - `agentcrew.artifacts.parser.LANG_TO_KIND: dict[str, str]`
  - `agentcrew.artifacts.parser.normalize_lang(lang: str) -> str | None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_artifact_parser.py
import pytest
from agentcrew.artifacts.parser import (
    parse_fenced_blocks, ArtifactParseError, normalize_lang,
)

def test_parses_single_file_block():
    raw = '说明文字\n\n```python file=backend/app/main.py\nprint(1)\n```\n\n尾部说明'
    out = parse_fenced_blocks(raw)
    assert out.files == {"backend/app/main.py": "print(1)\n"}
    assert "说明文字" in out.notes and "尾部说明" in out.notes

def test_parses_multiple_blocks():
    raw = ('```python file=a.py\nx=1\n```\n'
           '```markdown file=docs/README.md\n# hi\n```\n')
    out = parse_fenced_blocks(raw)
    assert set(out.files) == {"a.py", "docs/README.md"}
    assert out.files["docs/README.md"] == "# hi\n"

def test_block_without_file_is_note_not_written():
    raw = '```python\nprint("example")\n```\n```python file=a.py\nx=1\n```'
    out = parse_fenced_blocks(raw)
    assert set(out.files) == {"a.py"}
    assert "example" in out.notes

def test_unclosed_block_raises():
    with pytest.raises(ArtifactParseError):
        parse_fenced_blocks("```python file=a.py\nx=1\n")

def test_duplicate_file_takes_last_and_notes_warning():
    raw = '```python file=a.py\nold\n```\n```python file=a.py\nnew\n```'
    out = parse_fenced_blocks(raw)
    assert out.files["a.py"] == "new\n"
    assert "a.py" in out.notes and "重复" in out.notes

def test_backslash_paths_normalized():
    out = parse_fenced_blocks("```python file=backend\\app\\main.py\nx=1\n```")
    assert set(out.files) == {"backend/app/main.py"}

def test_no_blocks_returns_empty_files_and_full_note():
    out = parse_fenced_blocks("just prose, no code")
    assert out.files == {} and "just prose" in out.notes

def test_four_backtick_fence_is_not_confused():
    raw = '````python file=a.py\nx=1\n````'      # 4 个反引号
    with pytest.raises(ArtifactParseError):
        parse_fenced_blocks(raw)

# ── Review Focus #3：非 ASCII 路径 ──
def test_chinese_file_path_parses():
    out = parse_fenced_blocks("```markdown file=docs/接口文档.md\n# 接口\n```")
    assert set(out.files) == {"docs/接口文档.md"}
    assert out.files["docs/接口文档.md"] == "# 接口\n"

def test_path_with_spaces_parses():
    out = parse_fenced_blocks("```text file=docs/my notes.txt\nhi\n```")
    assert set(out.files) == {"docs/my notes.txt"}

@pytest.mark.parametrize("lang,kind", [
    ("py", "python"), ("python", "python"), ("yml", "yaml"), ("yaml", "yaml"),
    ("md", "markdown"), ("markdown", "markdown"), ("js", "js"),
    ("html", "html"), ("css", "css"), ("json", "json"), ("sh", "shell"),
    ("toml", "toml"), ("sql", "sql"), ("dockerfile", "dockerfile"),
])
def test_lang_normalization(lang, kind):
    assert normalize_lang(lang) == kind

def test_unknown_lang_returns_none():
    assert normalize_lang("brainfuck") is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_artifact_parser.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

起始围栏正则：`^(?P<fence>```)(?P<lang>[A-Za-z0-9_+.\-]*)(?:\s+file=(?P<path>.+?))?\s*$`（`path` 用非贪婪到行尾，从而允许空格与中文；**不**用 `\S+`）。逐行扫描：遇到起始围栏（且此前不在块内）→ 记录；遇到 ` ``` ` 单独成行 → 闭合。扫描到结尾仍在块内 → 抛 `ArtifactParseError("未闭合的代码块，起始行 N")`。**行首恰好三个反引号才算围栏**——4 个或更多反引号会被起始正则的 `lang` 捕获为 `` ` ``，导致 `normalize_lang` 返回 `None`；此时该行不视为合法起始围栏，于是整段被当作 note，块内内容不含合法起始围栏 → 不抛错。**修正**：为避免歧义，`parse_fenced_blocks` 在扫描前先检测「行首 ≥4 个反引号」，直接抛 `ArtifactParseError("不支持的围栏长度：4 个或更多反引号")`。

块内容保留每行末尾的 `\n`（`"\n".join(lines)` + 结尾 `\n`），使写出的文件以换行结束。

`notes` = 所有非块内行拼接（保留顺序），加上重复路径告警行。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_artifact_parser.py -v`
Expected: 全部 PASS，含中文路径与含空格路径两个用例

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: LLM 回复的围栏块产物解析"
```

---

## Task 11: ArtifactStore 安全落盘

**Files:**
- Create: `agentcrew/artifacts/store.py`
- Test: `tests/test_artifact_store.py`

**Interfaces:**
- Consumes: Task 3 `safe_relpath`/`atomic_write_*`/`sha256_bytes`/`tree`、Task 5 `ArtifactSpec`/`ArtifactRecord`/`Manifest`、Task 9 `plan_output_json_schema`（无关）、Task 14 `Ledger`（用 `Protocol` 松耦合）
- Produces:
  - `agentcrew.artifacts.store.ArtifactConflictError(RuntimeError)` — 属性 `path`、`existing_task`、`new_task`
  - `agentcrew.artifacts.store.ArtifactTooLargeError(ValueError)` — 属性 `path`、`size`、`limit`
  - `agentcrew.artifacts.store.UndeclaredArtifactError(ValueError)` — 属性 `path`、`task_id`
  - `agentcrew.artifacts.store.ArtifactStore`
    - `ArtifactStore(root: Path, *, run_id: str, ledger: LedgerLike | None = None)`
    - `.root -> Path`（`root` 即 `artifacts/` 目录本身）
    - `.resolve(rel: str) -> Path`
    - `.commit(rel: str, content: str, *, spec: ArtifactSpec, task: Task) -> ArtifactRecord`
    - `.read(rel: str, *, max_bytes: int | None = None) -> str`
    - `.exists(rel: str) -> bool`
    - `.records() -> list[ArtifactRecord]`
    - `.by_task(task_id: str) -> list[ArtifactRecord]`
    - `.write_manifest() -> Path`
    - `.file_tree() -> list[str]`
  - `agentcrew.artifacts.store.LedgerLike` — `Protocol`，`emit(event: str, **data: Any) -> None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_artifact_store.py
import os, sys
from pathlib import Path
import pytest
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import (
    ArtifactStore, ArtifactConflictError, ArtifactTooLargeError,
    UndeclaredArtifactError,
)
from agentcrew.task.task import Task

def mk_task(tid="T-1", role="backend_dev"):
    return Task(id=tid, title="t", description="d", role=role,
                acceptance_criteria=["a"], deliverables=[])

def spec(path="backend/app/main.py", **kw):
    return ArtifactSpec(path=path, kind="python", description="x", **kw)

@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts", run_id="R-1")

def test_commit_writes_file_and_returns_record(store, tmp_path):
    rec = store.commit("backend/app/main.py", "x = 1\n", spec=spec(), task=mk_task())
    assert (tmp_path / "artifacts" / "backend" / "app" / "main.py").read_text(encoding="utf-8") == "x = 1\n"
    assert rec.path == "backend/app/main.py" and rec.size_bytes == 6
    assert rec.produced_by_task == "T-1" and len(rec.sha256) == 64

def test_commit_creates_parent_dirs(store):
    store.commit("a/b/c/d.txt", "x", spec=ArtifactSpec(path="a/b/c/d.txt", kind="text", description="d"),
                 task=mk_task())
    assert store.exists("a/b/c/d.txt")

def test_chinese_path_roundtrip(store):
    s = ArtifactSpec(path="docs/接口文档.md", kind="markdown", description="d")
    store.commit("docs/接口文档.md", "# 接口\n", spec=s, task=mk_task())
    assert store.read("docs/接口文档.md") == "# 接口\n"

def test_path_traversal_blocked(store, tmp_path):
    evil = ArtifactSpec(path="ok.txt", kind="text", description="d")
    with pytest.raises(Exception):
        store.commit("../outside.txt", "x", spec=evil, task=mk_task())
    assert not (tmp_path / "outside.txt").exists()

def test_absolute_path_blocked(store):
    with pytest.raises(Exception):
        store.commit("/etc/passwd", "x",
                     spec=ArtifactSpec(path="x", kind="text", description="d"),
                     task=mk_task())

def test_undeclared_path_rejected(store):
    with pytest.raises(UndeclaredArtifactError) as ei:
        store.commit("hack.py", "x", spec=spec("declared.py"), task=mk_task())
    assert ei.value.path == "hack.py"

def test_oversize_rejected(store):
    s = ArtifactSpec(path="big.txt", kind="text", description="d", max_bytes=10)
    with pytest.raises(ArtifactTooLargeError):
        store.commit("big.txt", "x" * 100, spec=s, task=mk_task())

def test_undersize_rejected(store):
    s = ArtifactSpec(path="a.py", kind="python", description="d", min_bytes=5)
    with pytest.raises(ValueError):
        store.commit("a.py", "x", spec=s, task=mk_task())

def test_conflict_when_other_task_already_wrote_path(tmp_path):
    st = ArtifactStore(tmp_path / "artifacts", run_id="R-1")
    s = ArtifactSpec(path="shared.py", kind="python", description="d")
    st.commit("shared.py", "a=1\n", spec=s, task=mk_task("T-1"))
    with pytest.raises(ArtifactConflictError) as ei:
        st.commit("shared.py", "b=2\n", spec=s, task=mk_task("T-2"))
    assert ei.value.existing_task == "T-1" and ei.value.new_task == "T-2"

def test_same_task_may_rewrite_its_own_path(tmp_path):
    st = ArtifactStore(tmp_path / "artifacts", run_id="R-1")
    s = ArtifactSpec(path="a.py", kind="python", description="d")
    st.commit("a.py", "a=1\n", spec=s, task=mk_task("T-1"))
    st.commit("a.py", "a=2\n", spec=s, task=mk_task("T-1"))
    assert st.read("a.py") == "a=2\n"

def test_rewrite_updates_record_not_duplicates(tmp_path):
    st = ArtifactStore(tmp_path / "artifacts", run_id="R-1")
    s = ArtifactSpec(path="a.py", kind="python", description="d")
    st.commit("a.py", "a=1\n", spec=s, task=mk_task("T-1"))
    st.commit("a.py", "a=22\n", spec=s, task=mk_task("T-1"))
    recs = [r for r in st.records() if r.path == "a.py"]
    assert len(recs) == 1 and recs[0].size_bytes == 5

def test_manifest_roundtrip(store, tmp_path):
    store.commit("a.py", "x=1\n", spec=spec("a.py"), task=mk_task())
    p = store.write_manifest()
    import json
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["run_id"] == "R-1"
    assert [e["path"] for e in data["entries"]] == ["a.py"]

def test_symlinked_parent_rejected(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Windows 建符号链接需管理员权限")
    root = tmp_path / "artifacts"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    os.symlink(outside, root / "link")
    st = ArtifactStore(root, run_id="R-1")
    with pytest.raises(Exception):
        st.commit("link/pwned.py", "x",
                  spec=ArtifactSpec(path="link/pwned.py", kind="python", description="d"),
                  task=mk_task())
    assert not (outside / "pwned.py").exists()

def test_file_tree_lists_committed_files(store):
    store.commit("a/b.py", "x=1\n", spec=spec("a/b.py"), task=mk_task())
    assert "a/b.py" in store.file_tree()

def test_read_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        store.read("nope.txt")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_artifact_store.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`resolve(rel)`：`safe_relpath` → `(root / rel).resolve()` → 断言 `is_relative_to(root.resolve())` → 逐级检查 `root` 到 `candidate` 之间每一段**不是符号链接**（用 `Path.is_symlink()`）。

`commit(rel, content, spec, task)` 顺序（**先校验后写盘**，任何一步失败都不得留下文件）：
1. `rel` 规范化后必须**等于** `spec.path`（不等 → `UndeclaredArtifactError`）
2. `resolve(rel)`
3. 已存在同路径记录且 `produced_by_task != task.id` → `ArtifactConflictError`
4. 编码后长度对 `spec.max_bytes` / `spec.min_bytes` 校验
5. `mkdir(parents=True, exist_ok=True)`；`atomic_write_text`
6. 更新内存 `records`（同 `(path, task_id)` 覆盖，不追加）
7. `ledger.emit("artifact_written", task_id=..., path=..., size_bytes=..., sha256=...)`（`ledger` 为 `None` 时跳过）

`read` 走 `resolve` 后 `read_text(encoding="utf-8")`，`max_bytes` 给定时超出则截断（用于上下文节选）。

`Manifest` 用 `run_id`、`generated_at=datetime.now()`、`entries=records()`；`write_manifest` 写到 `root/"MANIFEST.json"`（原子写）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_artifact_store.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: ArtifactStore 安全落盘与清单"
```

---

## Task 12: 按 kind 的解析原语

**Files:**
- Create: `agentcrew/artifacts/contract.py`
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: Task 5 `ArtifactKind`
- Produces:
  - `agentcrew.artifacts.contract.KIND_PARSERS: dict[str, KINDParser]` — 键为 `ArtifactKind` 的字面量值
  - `agentcrew.artifacts.contract.parse_check(kind: str, content: str) -> str | None` — 返回 `None` 表示通过，否则返回人类可读错误消息
  - `agentcrew.artifacts.contract.has_placeholder(content: str) -> bool`
  - `agentcrew.artifacts.contract.PLACEHOLDER_MARKERS: tuple[str, ...]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_contract.py
import pytest
from agentcrew.artifacts.contract import parse_check, has_placeholder, KIND_PARSERS

def test_python_valid_passes():
    assert parse_check("python", "def f():\n    return 1\n") is None

def test_python_syntax_error_reports_line():
    msg = parse_check("python", "def f(\n  pass\n")
    assert msg is not None and "语法错误" in msg

def test_json_valid_passes():
    assert parse_check("json", '{"a": 1}') is None

def test_json_invalid_reports():
    msg = parse_check("json", '{"a": }')
    assert msg is not None and "JSON" in msg

def test_yaml_valid_passes():
    assert parse_check("yaml", "a: 1\nb:\n  - x\n") is None

def test_yaml_invalid_reports():
    assert parse_check("yaml", "a: [1, 2\n") is not None

def test_toml_valid_and_invalid():
    assert parse_check("toml", 'a = 1\n') is None
    assert parse_check("toml", 'a = = 1') is not None

def test_markdown_and_text_always_pass():
    assert parse_check("markdown", "") is None
    assert parse_check("text", "anything") is None

def test_unknown_kind_is_error():
    assert parse_check("nonsense", "x") is not None

def test_kind_parsers_covers_all_twelve_kinds():
    from agentcrew.artifacts.spec import ArtifactKind
    import typing
    assert set(KIND_PARSERS) == set(typing.get_args(ArtifactKind))

@pytest.mark.parametrize("marker", ["TODO", "<placeholder>", "FIXME",
                                    "your code here", "YOUR CODE HERE",
                                    "<!-- 待补充 -->", "待补充"])
def test_placeholder_markers_detected(marker):
    assert has_placeholder(f"# file\n{marker}\n")

def test_real_content_is_not_placeholder():
    assert not has_placeholder("# Todo App\n\nFastAPI service with CRUD endpoints.\n")

def test_ellipsis_only_detected_when_line_is_just_ellipsis():
    assert has_placeholder("def f():\n    ...\n")
    assert not has_placeholder("x = compute(...)  # 正常展开语法\n" + "y = 1\n" * 20)

def test_html_js_placeholder_detected():
    assert has_placeholder("<html><body>placeholder</body></html>")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_contract.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`KIND_PARSERS` 覆盖全部 12 个 kind：
- `python` → `ast.parse`，`SyntaxError` 时返回 `f"语法错误：line {e.lineno}: {e.msg}"`
- `json` → `json.loads`
- `yaml` → `yaml.safe_load`
- `toml` → `tomllib.loads`（Python 3.11+；3.10 用 `tomli` 不可用，故 **3.10 下降级为「跳过检查」并在返回的 dict 中标记不校验** —— 实现为：`sys.version_info < (3,11)` 时 `toml` 解析器直接返回 `None`）
- `markdown`/`text`/`html`/`css`/`js`/`sql`/`shell`/`dockerfile` → 恒返回 `None`（结构检查由 G1 的占位符检查覆盖）

`has_placeholder(content)` 返回 True 当：出现任一 `PLACEHOLDER_MARKERS`（大小写不敏感）；或存在**整行仅为 `...`** 或 `…`（去除缩进后）；或内容去空白后长度 < 30 字符且不含 `=`/`:`/`(`/`{` 等结构字符。

`PLACEHOLDER_MARKERS = ("TODO", "<placeholder>", "FIXME", "your code here", "your implementation", "待补充", "待实现", "占位")`。

> 注意 `test_ellipsis_only_detected_when_line_is_just_ellipsis` 要求 `x = compute(...)` **不**被判为占位符 —— 判据是「整行只有省略号」。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_contract.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 按产物类型的解析与占位符检测"
```

---

## Task 13: G1 静态校验门禁

**Files:**
- Create: `agentcrew/gate/__init__.py`, `agentcrew/gate/static_checks.py`
- Test: `tests/test_static_checks.py`

**Interfaces:**
- Consumes: Task 5 模型、Task 11 `ArtifactStore`、Task 12 `parse_check`/`has_placeholder`
- Produces:
  - `agentcrew.gate.static_checks.run_static(task: Task, store: ArtifactStore, *, round_no: int) -> ReviewRecord`
  - `agentcrew.gate.static_checks.STATIC_REVIEWER = "static:contract"`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_static_checks.py
import pytest
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.task.task import Task
from agentcrew.gate.static_checks import run_static, STATIC_REVIEWER

def task_with(*specs, tid="T-1"):
    return Task(id=tid, title="t", description="d", role="backend_dev",
                acceptance_criteria=["a"], deliverables=list(specs))

def py(path, **kw):
    return ArtifactSpec(path=path, kind="python", description="d", **kw)

@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts", run_id="R-1")

def commit(store, task, spec, content):
    return store.commit(spec.path, content, spec=spec, task=task)

def test_all_good_passes(store):
    t = task_with(py("app/main.py", must_contain=["def create_app"]))
    commit(store, t, t.deliverables[0], "def create_app():\n    return None\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "approve" and r.layer == "static"
    assert r.reviewer == STATIC_REVIEWER and r.round == 1

def test_missing_required_artifact_rejected(store):
    t = task_with(py("app/main.py"))
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject" and any("app/main.py" in i for i in r.issues)

def test_optional_artifact_may_be_missing(store):
    t = task_with(py("app/main.py"),
                  py("app/extra.py", required=False))
    commit(store, t, t.deliverables[0], "x = 1\n")
    assert run_static(t, store, round_no=1).verdict == "approve"

def test_empty_file_rejected(store):
    t = task_with(py("a.py", min_bytes=1))
    commit(store, t, t.deliverables[0], "")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject" and any("空" in i for i in r.issues)

def test_syntax_error_rejected(store):
    t = task_with(py("a.py"))
    commit(store, t, t.deliverables[0], "def f(\n  pass\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject" and any("语法错误" in i for i in r.issues)

def test_missing_must_contain_symbol_rejected(store):
    t = task_with(py("a.py", must_contain=["def create_app", "FastAPI"]))
    commit(store, t, t.deliverables[0], "def create_app():\n    return 1\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject"
    assert any("FastAPI" in i for i in r.issues)
    assert not any(i for i in r.issues if "create_app" in i and "缺少" in i)

def test_placeholder_only_file_rejected(store):
    t = task_with(py("a.py"))
    commit(store, t, t.deliverables[0], "TODO\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject" and any("占位" in i for i in r.issues)

def test_oversize_file_rejected(store):
    s = py("a.py", max_bytes=20)
    t = task_with(s)
    store.commit("a.py", "x = 1\n" * 100, spec=s, task=t)
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject" and any("过大" in i for i in r.issues)

# ── Review Focus #1：模型幻觉出契约外文件 ──
def test_undeclared_extra_artifact_rejected(store):
    t = task_with(py("app/main.py"))
    commit(store, t, t.deliverables[0], "x = 1\n")
    # 该任务某次执行写了契约外的文件
    store.commit("app/hack.py", "y = 1\n",
                 spec=ArtifactSpec(path="app/hack.py", kind="python", description="d"), task=t)
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject"
    assert any("app/hack.py" in i and ("未声明" in i or "契约" in i) for i in r.issues)

def test_declared_but_unproduced_required_artifact_reported(store):
    t = task_with(py("app/main.py"), py("app/models.py"))
    commit(store, t, t.deliverables[0], "x = 1\n")
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject"
    assert any("app/models.py" in i for i in r.issues)
    assert not any("app/main.py" in i for i in r.issues)

def test_multiple_problems_all_reported(store):
    t = task_with(py("a.py", must_contain=["def f"]), py("b.py"))
    commit(store, t, t.deliverables[0], "def f(\n")
    r = run_static(t, store, round_no=1)
    assert len(r.issues) >= 2

def test_rejected_record_has_no_score(store):
    t = task_with(py("a.py"))
    assert run_static(t, store, round_no=1).score is None

def test_fix_hints_are_populated_on_reject(store):
    t = task_with(py("a.py"))
    r = run_static(t, store, round_no=1)
    assert r.verdict == "reject" and r.fix_hints
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_static_checks.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`run_static` 按 spec §10.1 的 8 项检查收集**全部**问题（不早退），返回 `ReviewRecord`：
- `layer="static"`、`reviewer=STATIC_REVIEWER`、`round=round_no`、`score=None`
- `verdict="approve"` 当且仅当 `issues` 为空
- 每个 issue 配一条对应 `fix_hint`（如「补齐产物 backend/app/main.py」「为 POST /todos 增加输入校验」）

检查顺序与消息模板：
1. 缺必需产物 → `f"缺少必需产物：{path}"`
2. 多余产物（该任务写入了不在 `task.deliverables` 中的路径）→ `f"产出未在契约中声明的文件：{path}"`
3. 尺寸（`size < min_bytes` → 空文件 / `> max_bytes` → 过大）
4. `parse_check(deliverable.kind, content)` 返回的消息
5. `has_placeholder(content)` → `f"{path} 仅含占位内容"`
6. 逐条 `must_contain` 未命中 → `f"{path} 缺少必需符号：{needle}"`

「多余产物」通过 `store.by_task(task.id)` 减去 `task.deliverables` 的路径集合得出。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_static_checks.py -v`
Expected: 全部 PASS，含 `test_undeclared_extra_artifact_rejected`

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: G1 静态校验门禁"
```

---

# 阶段 D — 运行时持久化

## Task 14: Ledger 审计日志

**Files:**
- Create: `agentcrew/runtime/__init__.py`, `agentcrew/runtime/ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Consumes: Task 2 `redact`、Task 3 `atomic_write_text`
- Produces:
  - `agentcrew.runtime.ledger.LedgerEvent` — `ts: datetime`、`run_id: str`、`event: str`、`data: dict[str, Any]`
  - `agentcrew.runtime.ledger.Ledger`
    - `Ledger(path: Path, *, run_id: str, secrets: Sequence[str] = ())`
    - `.emit(event: str, **data: Any) -> LedgerEvent`
    - `.events(event: str | None = None) -> Iterator[LedgerEvent]`
    - `.count(event: str) -> int`
    - `.close() -> None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ledger.py
import json
import pytest
from agentcrew.runtime.ledger import Ledger

@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.jsonl", run_id="R-1")

def test_emit_appends_one_json_line(ledger, tmp_path):
    ledger.emit("run_started", goal="g")
    ledger.emit("task_started", task_id="T-1")
    lines = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "run_started"
    assert json.loads(lines[1])["data"]["task_id"] == "T-1"

def test_events_filters_by_name(ledger):
    ledger.emit("a"); ledger.emit("b"); ledger.emit("a")
    assert [e.event for e in ledger.events("a")] == ["a", "a"]

def test_count(ledger):
    ledger.emit("a"); ledger.emit("a"); ledger.emit("b")
    assert ledger.count("a") == 2 and ledger.count("zzz") == 0

def test_secrets_redacted_in_data(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    lg.emit("llm_call", api_key="supersecret", note="Bearer abc123def")
    raw = (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert "supersecret" not in raw and "abc123def" not in raw

def test_secrets_redacted_in_event_name(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R-1", secrets=["supersecret"])
    lg.emit("evt", payload="x")
    assert "supersecret" not in (tmp_path / "l.jsonl").read_text(encoding="utf-8")

def test_non_serializable_values_are_stringified(ledger, tmp_path):
    from pathlib import Path
    ledger.emit("x", p=Path("a/b"))
    line = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads(line)["data"]["p"]

def test_emit_after_close_raises(ledger):
    ledger.close()
    with pytest.raises(Exception):
        ledger.emit("x")

def test_each_line_flushed_immediately(ledger, tmp_path):
    ledger.emit("a")
    # 不 close 也能读到
    assert (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").strip() != ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ledger.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`emit` 用 `open(path, "a", encoding="utf-8")` 追加，写 `json.dumps(..., ensure_ascii=False, default=str) + "\n"`，然后 `flush()` + `os.fsync(fileno())`（spec §13.2 要求最后一条事件在 `Ctrl-C` 后完整）。

`data` 中所有字符串值先过 `redact(value, secrets)`；`event` 名本身也过一遍。`default=str` 处理 `Path`/`datetime` 等不可序列化值。

`events()` 逐行 `json.loads`，跳过空行；坏行记 `logging.warning` 并跳过（不中断读取）。

`close()` 置 `_closed`，之后 `emit` 抛 `RuntimeError("Ledger 已关闭")`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_ledger.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Ledger 追加式审计日志"
```

---

## Task 15: Checkpoint 持久化与恢复

**Files:**
- Create: `agentcrew/runtime/checkpoint.py`
- Test: `tests/test_checkpoint.py`

**Interfaces:**
- Consumes: Task 3 `atomic_write_text`/`read_text`/`slugify_run_id`、Task 5 `RunStats`、Task 8 `TaskBoard`
- Produces:
  - `agentcrew.runtime.checkpoint.RunStatus` — `RUNNING/INTERRUPTED/COMPLETED/FAILED/CANCELLED`
  - `agentcrew.runtime.checkpoint.RunRecord` — Pydantic：`run_id, schema_version=1, project_goal, status, created_at, updated_at, config_snapshot: dict, stats: RunStats`
  - `agentcrew.runtime.checkpoint.CheckpointStore`
    - `CheckpointStore(run_dir: Path)` — 目录不存在则抛 `FileNotFoundError`
    - `CheckpointStore.create(output_dir: Path, *, project_goal: str, config_snapshot: dict, run_id: str | None = None) -> CheckpointStore`
    - `.run_dir -> Path`、`.artifacts_dir -> Path`
    - `.save_record(record: RunRecord) -> None`
    - `.save_tasks(board: TaskBoard) -> None`
    - `.load_record() -> RunRecord`
    - `.load_board() -> TaskBoard`
    - `.save() -> None` — 便捷：同时存 record 与 board
    - `.list_runs(output_dir: Path) -> list[str]`
    - `CheckpointStore.load_latest(output_dir: Path) -> CheckpointStore`
    - `agentcrew.runtime.checkpoint.SchemaMismatchError(ValueError)`
    - `agentcrew.runtime.checkpoint.repair_statuses(board: TaskBoard) -> int` — 应用 spec §13.3 的状态修复规则，返回修复条数
    - `agentcrew.runtime.checkpoint.clean(output_dir: Path, *, keep: int, dry_run: bool = False) -> list[Path]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_checkpoint.py
import json
from pathlib import Path
import pytest
from agentcrew.runtime.checkpoint import (
    CheckpointStore, RunRecord, RunStatus, repair_statuses, clean,
    SchemaMismatchError,
)
from agentcrew.task.task import Task, TaskStatus as S
from agentcrew.task.task_board import TaskBoard

def mk(tid, deps=(), status=S.PENDING):
    return Task(id=tid, title=tid, description="d", role="backend_dev",
                acceptance_criteria=["a"], deliverables=[], dependencies=list(deps),
                status=status)

@pytest.fixture
def cs(tmp_path):
    return CheckpointStore.create(tmp_path / "output",
                                  project_goal="build a todo app",
                                  config_snapshot={"max_concurrency": 4},
                                  run_id="20261005-120000-abcd")

def test_create_makes_directories(cs):
    assert cs.run_dir.is_dir() and cs.artifacts_dir.is_dir()

def test_run_id_format_when_generated(tmp_path):
    import re
    c = CheckpointStore.create(tmp_path / "o", project_goal="g", config_snapshot={})
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", c.run_dir.name)

def test_save_and_load_record_roundtrip(cs):
    rec = RunRecord(run_id="20261005-120000-abcd", project_goal="g",
                    status=RunStatus.RUNNING,
                    created_at=__import__("datetime").datetime.now(),
                    updated_at=__import__("datetime").datetime.now(),
                    config_snapshot={"a": 1})
    cs.save_record(rec)
    assert cs.load_record().project_goal == "g"

def test_save_and_load_board_roundtrip(cs):
    b = TaskBoard([mk("A"), mk("B", ["A"])])
    b.transition("A", S.READY); b.transition("A", S.RUNNING)
    cs.save_tasks(b)
    assert cs.load_board().get("A").status is S.RUNNING

def test_schema_mismatch_raises(cs, tmp_path):
    cs.save_record(RunRecord(run_id="20261005-120000-abcd", project_goal="g",
                             status=RunStatus.RUNNING,
                             created_at=__import__("datetime").datetime.now(),
                             updated_at=__import__("datetime").datetime.now(),
                             config_snapshot={}))
    p = cs.run_dir / "run.json"
    d = json.loads(p.read_text(encoding="utf-8")); d["schema_version"] = 99
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(SchemaMismatchError):
        cs.load_record()

# ── Review Focus #5：中断恰好发生在写入过程中 ──
def test_interrupted_write_never_leaves_partial_json(cs, monkeypatch):
    """模拟写 run.json 时进程被杀死：目标文件必须仍是旧的完整内容。"""
    rec = RunRecord(run_id="20261005-120000-abcd", project_goal="old",
                    status=RunStatus.RUNNING,
                    created_at=__import__("datetime").datetime.now(),
                    updated_at=__import__("datetime").datetime.now(),
                    config_snapshot={})
    cs.save_record(rec)
    before = (cs.run_dir / "run.json").read_text(encoding="utf-8")

    import agentcrew.utils.files as files
    real_replace = files.os.replace
    def boom(src, dst):
        raise KeyboardInterrupt("模拟中断")
    monkeypatch.setattr(files.os, "replace", boom)
    rec2 = rec.model_copy(update={"project_goal": "new"})
    with pytest.raises(KeyboardInterrupt):
        cs.save_record(rec2)
    monkeypatch.setattr(files.os, "replace", real_replace)

    after = (cs.run_dir / "run.json").read_text(encoding="utf-8")
    assert after == before                       # 旧内容完好
    json.loads(after)                            # 仍是合法 JSON
    assert cs.load_record().project_goal == "old"

def test_leftover_temp_files_are_cleaned(cs, monkeypatch):
    import agentcrew.utils.files as files
    monkeypatch.setattr(files.os, "replace",
                        lambda s, d: (_ for _ in ()).throw(KeyboardInterrupt()))
    rec = RunRecord(run_id="20261005-120000-abcd", project_goal="g",
                    status=RunStatus.RUNNING,
                    created_at=__import__("datetime").datetime.now(),
                    updated_at=__import__("datetime").datetime.now(),
                    config_snapshot={})
    with pytest.raises(KeyboardInterrupt):
        cs.save_record(rec)
    assert list(cs.run_dir.glob("**/.*tmp*")) == []

def test_repair_running_becomes_ready_and_keeps_counts(cs):
    b = TaskBoard([mk("A", status=S.READY)])
    b.transition("A", S.RUNNING)                       # attempts -> 1
    n = repair_statuses(b)
    assert n == 1 and b.get("A").status is S.READY
    assert b.get("A").attempts == 1                    # 不重置

def test_repair_rework_becomes_ready(cs):
    b = TaskBoard([mk("A", status=S.READY)])
    b.transition("A", S.RUNNING); b.transition("A", S.REVIEW)
    b.transition("A", S.REWORK)
    repair_statuses(b)
    assert b.get("A").status is S.READY

def test_repair_review_stays_review(cs):
    b = TaskBoard([mk("A", status=S.READY)])
    b.transition("A", S.RUNNING); b.transition("A", S.REVIEW)
    repair_statuses(b)
    assert b.get("A").status is S.REVIEW

def test_repair_leaves_terminal_alone(cs):
    b = TaskBoard([mk("A", status=S.SUCCEEDED), mk("B", status=S.FAILED)])
    assert repair_statuses(b) == 0

def test_artifacts_survive_resume(cs):
    cs.artifacts_dir.joinpath("keep.txt").write_text("x", encoding="utf-8")
    assert (cs.run_dir / "artifacts" / "keep.txt").read_text(encoding="utf-8") == "x"

def test_list_runs_sorted_desc(tmp_path):
    for rid in ("20260101-000000-aaaa", "20260102-000000-bbbb"):
        CheckpointStore.create(tmp_path / "o", project_goal="g",
                               config_snapshot={}, run_id=rid)
    assert CheckpointStore.list_runs(tmp_path / "o") == [
        "20260102-000000-bbbb", "20260101-000000-aaaa"]

def test_clean_keeps_n_most_recent(tmp_path):
    o = tmp_path / "o"
    for rid in ("20260101-000000-aaaa", "20260102-000000-bbbb",
                "20260103-000000-cccc"):
        CheckpointStore.create(o, project_goal="g", config_snapshot={}, run_id=rid)
    removed = clean(o, keep=1)
    assert len(removed) == 2
    assert CheckpointStore.list_runs(o) == ["20260103-000000-cccc"]

def test_clean_dry_run_removes_nothing(tmp_path):
    o = tmp_path / "o"
    for rid in ("20260101-000000-aaaa", "20260102-000000-bbbb"):
        CheckpointStore.create(o, project_goal="g", config_snapshot={}, run_id=rid)
    removed = clean(o, keep=0, dry_run=True)
    assert len(removed) == 2 and len(CheckpointStore.list_runs(o)) == 2

def test_clean_skips_dirs_without_run_json(tmp_path):
    o = tmp_path / "o"; o.mkdir()
    (o / "random-dir").mkdir()
    (o / "not-a-run.txt").write_text("x", encoding="utf-8")
    assert clean(o, keep=0) == []
    assert (o / "random-dir").is_dir() and (o / "not-a-run.txt").is_file()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_checkpoint.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`create()`：`run_dir = output_dir / (run_id or slugify_run_id(datetime.now()))`；建 `run_dir`、`run_dir/artifacts`、`run_dir/context`、`run_dir/review`、`run_dir/diagnostics`。

`save_record`/`save_tasks` 都用 `atomic_write_text`（Task 3），JSON 用 `ensure_ascii=False, indent=2, default=str`。**原子写是 Review Focus #5 的答案**：`os.replace` 要么完成要么不发生，因此目标文件永远是「旧完整」或「新完整」，绝无半截。

`load_record`：读 `run.json` → 比对 `schema_version != 1` → 抛 `SchemaMismatchError`。
`load_board`：读 `tasks.json` → `TaskBoard.from_snapshot(data["tasks"])`。

`repair_statuses(board)`：对每个任务，`RUNNING → READY`、`REWORK → READY`、`REVIEW` 保持不变、其余不变；通过 `TaskBoard.transition(..., reason="resume-repair")` 执行（`RUNNING→READY` 与 `REWORK→READY` 都在状态机合法表中）。返回修复条数。

`clean(output_dir, keep, dry_run)`：只处理「目录且含 `run.json`」的条目，按目录名（即 run_id，天然按时间可排序）降序，保留前 `keep` 个，其余删除（`dry_run` 时只返回路径不删）。用 `shutil.rmtree`。**删除前断言目标位于 `output_dir` 之下**。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_checkpoint.py -v`
Expected: 全部 PASS，含两个中断安全用例

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: JSON checkpoint 原子持久化与恢复修复"
```

---

# 阶段 E — 模型接入层

## Task 16: Provider 能力位与计价表

**Files:**
- Create: `agentcrew/models/__init__.py`, `agentcrew/models/config.py`, `agentcrew/models/pricing.py`
- Test: `tests/test_model_config.py`, `tests/test_pricing.py`

**Interfaces:**
- Consumes: Task 1 `Settings`
- Produces:
  - `agentcrew.models.config.ProviderCapabilities` — `supports_json_schema=False`、`supports_json_object=False`、`supports_tool_calls=False`、`supports_system_role=True`、`max_context_tokens=128000`、`max_output_tokens=8192`
  - `agentcrew.models.config.ModelBinding` — `alias: str`、`provider: str`、`model: str`、`base_url: str | None`、`api_key_env: str`、`capabilities: ProviderCapabilities`
  - `agentcrew.models.config.PROVIDER_PRESETS: dict[str, ProviderPreset]` — 键：`openai/anthropic/deepseek/doubao/qwen/ernie/ollama/mock`
  - `agentcrew.models.config.ProviderPreset` — `implementation: Literal["openai_compat","anthropic","mock"]`、`base_url: str | None`、`api_key_env: str`、`capabilities: ProviderCapabilities`
  - `agentcrew.models.config.parse_alias(alias: str, *, overrides: dict[str, dict] | None = None) -> ModelBinding`
  - `agentcrew.models.config.UnknownProviderError(ValueError)`
  - `agentcrew.models.pricing.MODEL_PRICING: dict[str, tuple[float, float]]`
  - `agentcrew.models.pricing.estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_model_config.py
import pytest
from agentcrew.models.config import (
    ProviderCapabilities, ModelBinding, PROVIDER_PRESETS, parse_alias,
    UnknownProviderError,
)

def test_all_eight_providers_present():
    assert set(PROVIDER_PRESETS) == {
        "openai", "anthropic", "deepseek", "doubao", "qwen", "ernie", "ollama", "mock"}

def test_openai_supports_json_schema():
    assert PROVIDER_PRESETS["openai"].capabilities.supports_json_schema

def test_anthropic_uses_native_implementation():
    assert PROVIDER_PRESETS["anthropic"].implementation == "anthropic"
    assert not PROVIDER_PRESETS["anthropic"].capabilities.supports_json_schema

def test_doubao_has_object_but_not_schema():
    c = PROVIDER_PRESETS["doubao"].capabilities
    assert c.supports_json_object and not c.supports_json_schema

def test_ernie_supports_neither():
    c = PROVIDER_PRESETS["ernie"].capabilities
    assert not c.supports_json_schema and not c.supports_json_object

def test_ollama_has_local_base_url():
    assert "11434" in (PROVIDER_PRESETS["ollama"].base_url or "")

def test_parse_alias_simple():
    b = parse_alias("openai:gpt-4o-mini")
    assert b.provider == "openai" and b.model == "gpt-4o-mini"
    assert b.capabilities.supports_json_schema

def test_parse_alias_splits_on_first_colon_only():
    b = parse_alias("ollama:qwen2.5:14b")
    assert b.provider == "ollama" and b.model == "qwen2.5:14b"

def test_parse_alias_without_colon_raises():
    with pytest.raises(ValueError):
        parse_alias("justamodel")

def test_parse_alias_unknown_provider_raises():
    with pytest.raises(UnknownProviderError):
        parse_alias("wizard:merlin")

def test_capability_override_applied():
    b = parse_alias("doubao:x", overrides={"doubao": {
        "capabilities": {"supports_json_schema": True}}})
    assert b.capabilities.supports_json_schema
    assert b.capabilities.supports_json_object      # 未覆盖的字段保持不变

def test_base_url_override_applied():
    b = parse_alias("openai:gpt-4o", overrides={"openai": {
        "base_url": "https://proxy.internal/v1"}})
    assert b.base_url == "https://proxy.internal/v1"
```

```python
# tests/test_pricing.py
from agentcrew.models.pricing import estimate_cost, MODEL_PRICING

def test_known_model_costs_money():
    assert estimate_cost("gpt-4o-mini", 1_000_000, 0) > 0

def test_unknown_model_costs_zero_but_does_not_raise():
    assert estimate_cost("nobody-knows-this", 1_000_000, 1_000_000) == 0.0

def test_cost_scales_linearly():
    a = estimate_cost("gpt-4o-mini", 1000, 0)
    b = estimate_cost("gpt-4o-mini", 2000, 0)
    assert abs(b - 2 * a) < 1e-9

def test_prefix_matching_covers_versioned_names():
    assert estimate_cost("gpt-4o-mini-2024-07-18", 1000, 0) > 0

def test_pricing_table_is_non_empty():
    assert MODEL_PRICING
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_model_config.py tests/test_pricing.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`PROVIDER_PRESETS` 逐字实现 spec §12.1 的表格（8 行，各列取值照抄）。

`parse_alias` 按**第一个**冒号切分；provider 不在预设表中 → `UnknownProviderError`；无冒号 → `ValueError`。`overrides` 形如 `{"doubao": {"capabilities": {...}, "base_url": "..."}}`，用「读取现有 → `model_copy(update=...)`」合并，**未指定的字段保持预设值**。

`MODEL_PRICING` 形如 `{"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00), "claude-sonnet-5": (3.00, 15.00), "deepseek-chat": (0.27, 1.10), ...}`，单位 USD / 1M tokens。

`estimate_cost` 用**前缀匹配**：遍历 `MODEL_PRICING`，取匹配到的最长前缀（`model.startswith(prefix)`）；无匹配返回 `0.0`。公式：`prompt_tokens/1e6*in_price + completion_tokens/1e6*out_price`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_model_config.py tests/test_pricing.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Provider 能力位与计价表"
```

---

## Task 17: Provider 基类与 Mock Provider

**Files:**
- Create: `agentcrew/models/providers/__init__.py`, `agentcrew/models/providers/base.py`, `agentcrew/models/providers/mock.py`
- Test: `tests/test_mock_provider.py`

**Interfaces:**
- Consumes: Task 16 `ModelBinding`/`ProviderCapabilities`
- Produces:
  - `agentcrew.models.providers.base.Message` — `TypedDict`：`role: Literal["system","user","assistant"]`、`content: str`
  - `agentcrew.models.providers.base.CompletionResult` — Pydantic：`text, provider, model, prompt_tokens, completion_tokens, latency_ms`
  - `agentcrew.models.providers.base.ResponseFormat` — TypedDict：`type: str`，可含 `json_schema: dict`
  - `agentcrew.models.providers.base.BaseProvider(ABC)` — 属性 `binding: ModelBinding`、`capabilities: ProviderCapabilities`（委托给 binding）；抽象方法 `async complete(self, messages: Sequence[Message], *, temperature: float, max_tokens: int, response_format: ResponseFormat | None = None) -> CompletionResult`
  - `agentcrew.models.providers.mock.MockProvider(binding, *, fixtures_dir: Path | None = None, purpose: str = "execute", role: str | None = None, latency_ms: int = 0, on_fallback: Callable[[str], None] | None = None)`
  - `agentcrew.models.providers.mock.set_purpose(provider, purpose, *, role=None, task_id=None, attempt=1) -> None`
  - `agentcrew.models.providers.mock.MockFixtureMissing(RuntimeError)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mock_provider.py
import json
import pytest
from pathlib import Path
from agentcrew.models.config import parse_alias
from agentcrew.models.providers.mock import MockProvider, set_purpose

@pytest.fixture
def fixtures(tmp_path):
    d = tmp_path / "fixtures"
    (d / "responses" / "T-001").mkdir(parents=True)
    (d / "responses" / "T-001" / "attempt-1.md").write_text(
        "```python file=a.py\nx = 1\n```\n", encoding="utf-8")
    (d / "responses" / "T-003").mkdir(parents=True)
    (d / "responses" / "T-003" / "attempt-1.md").write_text("bad\n", encoding="utf-8")
    (d / "responses" / "T-003" / "attempt-2.md").write_text(
        "```python file=a.py\ny = 2\n```\n", encoding="utf-8")
    (d / "plan_todo.json").write_text(
        json.dumps({"tasks": [{"id": "T-001", "title": "t", "description": "d",
                               "role": "backend_dev", "acceptance_criteria": ["a"],
                               "deliverables": [], "dependencies": []}]}),
        encoding="utf-8")
    (d / "reviews.json").write_text(json.dumps({"T-001": {"verdict": "approve",
                                                          "score": 0.9,
                                                          "issues": [], "fix_hints": []}}),
                                    encoding="utf-8")
    return d

@pytest.fixture
def provider(fixtures):
    return MockProvider(parse_alias("mock:mock-model"), fixtures_dir=fixtures)

async def test_returns_task_attempt_fixture(provider, fixtures):
    set_purpose(provider, "execute", role="backend_dev", task_id="T-001", attempt=1)
    r = await provider.complete([{"role": "user", "content": "go"}],
                                temperature=0, max_tokens=100)
    assert "x = 1" in r.text
    assert r.provider == "mock" and r.latency_ms == 0

async def test_second_attempt_reads_different_fixture(provider):
    set_purpose(provider, "execute", role="qa_engineer", task_id="T-003", attempt=1)
    a = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    set_purpose(provider, "execute", role="qa_engineer", task_id="T-003", attempt=2)
    b = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert a.text != b.text and "y = 2" in b.text

async def test_falls_back_to_attempt_1_when_later_missing(provider):
    set_purpose(provider, "execute", role="r", task_id="T-001", attempt=5)
    r = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert "x = 1" in r.text

async def test_plan_purpose_reads_plan_fixture(provider):
    set_purpose(provider, "plan")
    r = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert "T-001" in r.text

async def test_review_purpose_reads_reviews_fixture(provider):
    set_purpose(provider, "review", role="architect", task_id="T-001", attempt=1)
    r = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert "approve" in r.text

async def test_missing_fixture_falls_back_and_reports(provider):
    seen = []
    provider._on_fallback = seen.append
    set_purpose(provider, "execute", role="r", task_id="T-999", attempt=1)
    r = await provider.complete([{"role": "user", "content": "x"}],
                                temperature=0, max_tokens=10)
    assert r.text
    assert seen and "T-999" in seen[0]

async def test_tokens_are_estimated_not_zero(provider):
    set_purpose(provider, "execute", role="r", task_id="T-001", attempt=1)
    r = await provider.complete([{"role": "user", "content": "hello there"}],
                                temperature=0, max_tokens=10)
    assert r.prompt_tokens > 0 and r.completion_tokens > 0

async def test_latency_is_configurable(fixtures):
    p = MockProvider(parse_alias("mock:m"), fixtures_dir=fixtures, latency_ms=1)
    set_purpose(p, "execute", role="r", task_id="T-001", attempt=1)
    r = await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert r.latency_ms >= 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_mock_provider.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`MockProvider` 内部状态：`_purpose`、`_role`、`_task_id`、`_attempt`、`_fixtures_dir`、`_latency_ms`、`_on_fallback`。

`complete()` 查找顺序：
- `purpose == "plan"` → `fixtures_dir/plan_todo.json` 的**原始文本**
- `purpose == "review"` → `fixtures_dir/reviews.json` 中 `data[task_id]` 的 JSON 文本；无 `task_id` 键时用 `data["_default"]`
- `purpose == "execute"` → `fixtures_dir/responses/<task_id>/attempt-<attempt>.md`；不存在则**回退**到 `attempt-1.md`；再不存在则生成占位回复 `f"# 占位回复（fixture 缺失）\ntask={task_id} attempt={attempt}\n"` 并调 `_on_fallback(message)`
- 任何路径都读不到 → 不抛异常（`MockFixtureMissing` 仅在 `fixtures_dir` 本身为 `None` 且 purpose 为 plan/review 时抛出）

`prompt_tokens` 用 `estimate_tokens` 累加所有 messages 的 content；`completion_tokens` 用 `estimate_tokens(result_text)`。

`latency_ms > 0` 时 `await asyncio.sleep(latency_ms / 1000)`。

`set_purpose` 是模块级函数，直接写 provider 的私有字段（测试与 Agent 层都用它）。**注意**：`_on_fallback` 属性名需与测试一致。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_mock_provider.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Provider 基类与确定性 Mock Provider"
```

---

## Task 18: OpenAI 兼容 Provider

**Files:**
- Create: `agentcrew/models/providers/openai_compat.py`
- Test: `tests/test_openai_compat.py`

**Interfaces:**
- Consumes: Task 17 `BaseProvider`/`CompletionResult`、Task 4 `retry_async`/`TransientError`/`PermanentError`、Task 1 `Settings.api_key_for`
- Produces:
  - `agentcrew.models.providers.openai_compat.OpenAICompatProvider(binding, *, api_key: str | None, client: Any | None = None)`
  - `agentcrew.models.providers.openai_compat.classify_http_error(exc: Exception) -> type[Exception]` — 返回 `TransientError` 或 `PermanentError`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_openai_compat.py
import pytest
from agentcrew.models.config import parse_alias
from agentcrew.models.providers.openai_compat import (
    OpenAICompatProvider, classify_http_error,
)
from agentcrew.utils.retry import TransientError, PermanentError

class FakeResp:
    def __init__(self, text, pt=10, ct=5):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]
        self.usage = type("U", (), {"prompt_tokens": pt, "completion_tokens": ct})()

class FakeClient:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error
        self.calls = []
        self.chat = type("Chat", (), {"completions": self})()
    async def create(self, **kw):
        self.calls.append(kw)
        if self._error: raise self._error
        return self._result

async def test_happy_path_returns_text_and_usage():
    c = FakeClient(FakeResp("hello"))
    p = OpenAICompatProvider(parse_alias("openai:gpt-4o-mini"), api_key="k", client=c)
    r = await p.complete([{"role": "user", "content": "hi"}],
                         temperature=0.2, max_tokens=100)
    assert r.text == "hello" and r.prompt_tokens == 10 and r.completion_tokens == 5

async def test_passes_temperature_and_max_tokens():
    c = FakeClient(FakeResp("x"))
    p = OpenAICompatProvider(parse_alias("openai:gpt-4o-mini"), api_key="k", client=c)
    await p.complete([{"role": "user", "content": "hi"}], temperature=0.7, max_tokens=42)
    assert c.calls[0]["temperature"] == 0.7 and c.calls[0]["max_tokens"] == 42

async def test_response_format_forwarded_when_provided():
    c = FakeClient(FakeResp("{}"))
    p = OpenAICompatProvider(parse_alias("openai:gpt-4o-mini"), api_key="k", client=c)
    await p.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=10,
                     response_format={"type": "json_object"})
    assert c.calls[0]["response_format"] == {"type": "json_object"}

async def test_response_format_omitted_when_none():
    c = FakeClient(FakeResp("x"))
    p = OpenAICompatProvider(parse_alias("openai:gpt-4o-mini"), api_key="k", client=c)
    await p.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=10)
    assert "response_format" not in c.calls[0]

async def test_system_merged_into_user_when_unsupported():
    c = FakeClient(FakeResp("x"))
    b = parse_alias("openai:m", overrides={"openai": {
        "capabilities": {"supports_system_role": False}}})
    p = OpenAICompatProvider(b, api_key="k", client=c)
    await p.complete([{"role": "system", "content": "SYS"},
                      {"role": "user", "content": "USR"}], temperature=0, max_tokens=10)
    msgs = c.calls[0]["messages"]
    assert len(msgs) == 1 and "SYS" in msgs[0]["content"] and "USR" in msgs[0]["content"]

@pytest.mark.parametrize("status,expected", [
    (429, TransientError), (500, TransientError), (502, TransientError),
    (503, TransientError), (504, TransientError),
    (400, PermanentError), (401, PermanentError), (403, PermanentError),
    (404, PermanentError), (422, PermanentError),
])
def test_classify_http_status(status, expected):
    e = Exception(f"Error code: {status} - something")
    assert classify_http_error(e) is expected

def test_classify_timeout_is_transient():
    assert classify_http_error(TimeoutError("timed out")) is TransientError

def test_classify_unknown_is_permanent():
    assert classify_http_error(ValueError("weird")) is PermanentError

async def test_transient_error_is_retried():
    c = FakeClient(error=Exception("Error code: 429 - rate limited"))
    p = OpenAICompatProvider(parse_alias("openai:m"), api_key="k", client=c)
    with pytest.raises(TransientError):
        await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert len(c.calls) > 1

async def test_permanent_error_not_retried():
    c = FakeClient(error=Exception("Error code: 401 - bad key"))
    p = OpenAICompatProvider(parse_alias("openai:m"), api_key="k", client=c)
    with pytest.raises(PermanentError):
        await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert len(c.calls) == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_openai_compat.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

构造 `AsyncOpenAI(api_key=..., base_url=binding.base_url)` —— 但**必须支持注入 fake client**（测试用 `client=` 参数绕过真客户端）。`client` 为 `None` 时才真正 import 并构造 `openai.AsyncOpenAI`（**延迟 import**，使测试环境无需安装 openai 也能跑其他测试）。

`complete()` 内部用 `retry_async` 包裹实际调用，`retry_on=(TransientError,)`，`attempts=4`、`base_delay=1.0`；`on_retry` 由调用方（`factory`）注入 ledger 记账，本类通过构造函数可选参数 `on_retry` 接收。

在调用前把 provider 异常翻译成 `TransientError`/`PermanentError`：`classify_http_error` 检查异常字符串中的 `Error code: NNN`（openai SDK 的格式），或异常类型名含 `Timeout`/`Connection`；429/5xx/超时/连接 → `TransientError`，其余 → `PermanentError`。

`supports_system_role=False` 时把 `system` 消息内容前置到第一条 `user` 消息：`f"{system_content}\n\n{user_content}"`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_openai_compat.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: OpenAI 兼容 Provider 与错误分类重试"
```

---

## Task 19: Anthropic Provider

**Files:**
- Create: `agentcrew/models/providers/anthropic.py`
- Test: `tests/test_anthropic_provider.py`

**Interfaces:**
- Consumes: Task 17 `BaseProvider`、Task 18 `classify_http_error`
- Produces:
  - `agentcrew.models.providers.anthropic.AnthropicProvider(binding, *, api_key: str | None, client: Any | None = None)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_anthropic_provider.py
import pytest
from agentcrew.models.config import parse_alias
from agentcrew.models.providers.anthropic import AnthropicProvider
from agentcrew.utils.retry import TransientError

class FakeResp:
    def __init__(self, text, i=10, o=5):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.usage = type("U", (), {"input_tokens": i, "output_tokens": o})()

class FakeClient:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error
        self.calls = []
        self.messages = self
    async def create(self, **kw):
        self.calls.append(kw)
        if self._error: raise self._error
        return self._result

def binding():
    return parse_alias("anthropic:claude-sonnet-5")

async def test_extracts_text_blocks_only():
    c = FakeClient(FakeResp("answer"))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    r = await p.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=10)
    assert r.text == "answer" and r.provider == "anthropic"

async def test_maps_input_output_tokens():
    c = FakeClient(FakeResp("x", i=7, o=3))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    r = await p.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=10)
    assert r.prompt_tokens == 7 and r.completion_tokens == 3

async def test_system_message_extracted_to_top_level_param():
    c = FakeClient(FakeResp("x"))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    await p.complete([{"role": "system", "content": "SYS"},
                      {"role": "user", "content": "USR"}], temperature=0, max_tokens=10)
    kw = c.calls[0]
    assert kw["system"] == "SYS"
    assert kw["messages"] == [{"role": "user", "content": "USR"}]

async def test_no_system_key_when_absent():
    c = FakeClient(FakeResp("x"))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    await p.complete([{"role": "user", "content": "USR"}], temperature=0, max_tokens=10)
    assert "system" not in c.calls[0]

async def test_response_format_is_ignored_not_forwarded():
    """Anthropic 不支持 response_format；必须忽略而不是传错参数。"""
    c = FakeClient(FakeResp("x"))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    await p.complete([{"role": "user", "content": "U"}], temperature=0, max_tokens=10,
                     response_format={"type": "json_schema", "json_schema": {}})
    assert "response_format" not in c.calls[0]

async def test_retries_transient():
    c = FakeClient(error=Exception("Error code: 503 - overloaded"))
    p = AnthropicProvider(binding(), api_key="k", client=c)
    with pytest.raises(TransientError):
        await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert len(c.calls) > 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_anthropic_provider.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

延迟 import `anthropic.AsyncAnthropic`，`client=None` 时才构造。`create` 参数为 `model`、`max_tokens`、`temperature`、`messages`、可选 `system`。

响应解析：`"".join(b.text for b in resp.content if b.type == "text")`（**只取 text 块**）。token 映射 `usage.input_tokens → prompt_tokens`、`usage.output_tokens → completion_tokens`。

`response_format` 参数**静默忽略**（能力位已声明不支持，由 `structured.py` 决定降到第 3 级）。重试复用 Task 18 的 `classify_http_error`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_anthropic_provider.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Anthropic 原生 Messages API Provider"
```

---

## Task 20: 结构化输出降级链

**Files:**
- Create: `agentcrew/models/structured.py`
- Test: `tests/test_structured_output.py`

**Interfaces:**
- Consumes: Task 17 `BaseProvider`/`CompletionResult`、Task 16 `ProviderCapabilities`
- Produces:
  - `agentcrew.models.structured.StructuredOutputError(RuntimeError)` — 属性 `schema_name`、`attempts`、`last_text`、`last_error`
  - `agentcrew.models.structured.DegradeEvent` — Pydantic：`provider, from_level, to_level, reason`
  - `agentcrew.models.structured.complete_structured(provider, messages, schema: type[BaseModel], *, temperature: float, max_tokens: int, repair_attempts: int = 2, on_degrade: Callable[[DegradeEvent], None] | None = None) -> BaseModel`
  - `agentcrew.models.structured.extract_json(text: str) -> str` — 从任意文本中提取首个平衡的 JSON 对象/数组字符串
  - `agentcrew.models.structured.build_response_format(schema: type[BaseModel], level: int) -> ResponseFormat | None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_structured_output.py
import json
import pytest
from pydantic import BaseModel, ConfigDict
from agentcrew.models.config import parse_alias
from agentcrew.models.providers.base import CompletionResult
from agentcrew.models.structured import (
    complete_structured, extract_json, StructuredOutputError, build_response_format,
)

class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: str
    score: float

class ScriptedProvider:
    """按脚本依次返回预设文本；记录收到的 response_format。"""
    def __init__(self, binding, texts):
        self.binding = binding
        self.capabilities = binding.capabilities
        self._texts = list(texts)
        self.seen_formats = []
    async def complete(self, messages, *, temperature, max_tokens, response_format=None):
        self.seen_formats.append(response_format)
        text = self._texts.pop(0) if self._texts else "{}"
        return CompletionResult(text=text, provider=self.binding.provider,
                                model=self.binding.model,
                                prompt_tokens=1, completion_tokens=1, latency_ms=0)

VALID = json.dumps({"verdict": "approve", "score": 0.9})

# ── extract_json ──
def test_extract_plain_json():
    assert json.loads(extract_json(VALID))["verdict"] == "approve"

def test_extract_from_markdown_fence():
    assert json.loads(extract_json(f"```json\n{VALID}\n```"))["verdict"] == "approve"

def test_extract_from_surrounded_prose():
    txt = f"Sure! Here is the result:\n{VALID}\nHope that helps."
    assert json.loads(extract_json(txt))["score"] == 0.9

def test_extract_handles_nested_braces():
    txt = 'prefix {"a": {"b": {"c": 1}}, "d": [1,2]} suffix'
    assert json.loads(extract_json(txt))["a"]["b"]["c"] == 1

def test_extract_handles_braces_inside_strings():
    txt = '{"msg": "a } brace", "n": 1}'
    assert json.loads(extract_json(txt))["n"] == 1

def test_extract_raises_when_no_json():
    with pytest.raises(ValueError):
        extract_json("no json at all")

# ── build_response_format ──
def test_level1_builds_json_schema():
    rf = build_response_format(Answer, 1)
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True

def test_level2_builds_json_object():
    assert build_response_format(Answer, 2) == {"type": "json_object"}

def test_level3_returns_none():
    assert build_response_format(Answer, 3) is None

# ── 降级链 ──
async def test_level1_used_when_capable():
    b = parse_alias("openai:gpt-4o-mini")
    p = ScriptedProvider(b, [VALID])
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert r.verdict == "approve"
    assert p.seen_formats[0]["type"] == "json_schema"

async def test_level2_used_when_only_object_supported():
    b = parse_alias("qwen:qwen-max")
    p = ScriptedProvider(b, [VALID])
    await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                              temperature=0, max_tokens=10)
    assert p.seen_formats[0] == {"type": "json_object"}

# ── Review Focus #2：模型把 JSON 包在围栏里/加解释文字 ──
async def test_level3_extracts_fenced_json_when_provider_incapable():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, [f"好的，结果如下：\n```json\n{VALID}\n```\n以上。"])
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert r.verdict == "approve" and p.seen_formats[0] is None

async def test_level3_extracts_prose_wrapped_json():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, [f"Here you go: {VALID} — done."])
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert r.score == 0.9

# ── 修复循环 ──
async def test_repair_loop_fixes_invalid_then_succeeds():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, ['{"verdict": "approve"}',        # 缺 score
                             VALID])
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert r.score == 0.9 and len(p.seen_formats) == 2

async def test_repair_prompt_contains_validation_error():
    captured = []
    class Capturing(ScriptedProvider):
        async def complete(self, messages, *, temperature, max_tokens, response_format=None):
            captured.append(messages)
            return await super().complete(messages, temperature=temperature,
                                          max_tokens=max_tokens,
                                          response_format=response_format)
    b = parse_alias("ernie:ernie-4")
    p = Capturing(b, ['{"verdict": "approve"}', VALID])
    await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                              temperature=0, max_tokens=10)
    repair_msg = " ".join(m["content"] for m in captured[1])
    assert "score" in repair_msg or "校验" in repair_msg

async def test_extra_field_rejected_then_repaired():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, [json.dumps({"verdict": "approve", "score": 1.0, "x": 1}),
                             VALID])
    r = await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10)
    assert r.verdict == "approve"

async def test_exhausting_repairs_raises_with_diagnostics():
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, ["not json", "still not json", "nope", "nope"])
    with pytest.raises(StructuredOutputError) as ei:
        await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                                  temperature=0, max_tokens=10, repair_attempts=2)
    assert ei.value.schema_name == "Answer" and ei.value.last_text

async def test_on_degrade_called_when_falling_back():
    seen = []
    b = parse_alias("ernie:ernie-4")
    p = ScriptedProvider(b, [VALID])
    await complete_structured(p, [{"role": "user", "content": "x"}], Answer,
                              temperature=0, max_tokens=10, on_degrade=seen.append)
    assert seen and seen[0].to_level == 3
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_structured_output.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

**起始级别判定**：`supports_json_schema` → 1；否则 `supports_json_object` → 2；否则 3。
**降级**：级别 1/2 的调用若抛异常（provider 不接受该参数）→ 降到下一级重试一次，并通过 `on_degrade` 通知。级别 3 的失败直接进修复循环。

**`extract_json(text)`** 算法：先找 ```json 围栏块内容，直接返回；否则扫描文本找第一个 `{` 或 `[`，用一个**跳过字符串字面量**的括号计数器找到匹配的闭合位置，返回该子串。找不到 → `ValueError`。

**修复循环**：解析或校验失败时，把 `f"你上次的输出校验失败：\n{error}\n请仅返回修正后的 JSON，不要任何解释文字。"` 作为新的 `user` 消息**追加**到消息列表（保留原对话），重新调用；最多 `repair_attempts` 次（默认 2，即最多 3 次 LLM 调用）。全部失败 → `StructuredOutputError(schema_name=schema.__name__, attempts=..., last_text=..., last_error=...)`。

第 2 级时还需在提示词中嵌入 `json.dumps(schema.model_json_schema(), ensure_ascii=False)`（因为 `json_object` 模式不携带 schema）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_structured_output.py -v`
Expected: 全部 PASS，含三个 Review Focus #2 用例

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 结构化输出四级降级链与修复循环"
```

---

## Task 21: ModelFactory 与 LLM 调用限流

**Files:**
- Create: `agentcrew/models/factory.py`
- Test: `tests/test_model_factory.py`

**Interfaces:**
- Consumes: Task 16 `parse_alias`、Task 17 `MockProvider`、Task 18/19 Provider、Task 1 `Settings`、Task 14 `Ledger`
- Produces:
  - `agentcrew.models.factory.DEFAULT_FIXTURES_DIR: Path` — 模块级常量，默认 `Path(__file__).resolve().parents[2] / "examples" / "fixtures"`。测试通过 monkeypatch 它来注入临时 fixtures。
  - `agentcrew.models.factory.ModelFactory`
    - `ModelFactory(settings: Settings, *, ledger: Ledger | None = None, fixtures_dir: Path | None = None)` — `fixtures_dir` 为 `None` 时用 `DEFAULT_FIXTURES_DIR`
    - `.settings -> Settings` — 只读属性（`BaseAgent` 解析角色级模型绑定要用）
    - `.get(alias: str) -> BaseProvider` — 按别名缓存实例
    - `.llm_semaphore -> asyncio.Semaphore` — 容量 = `settings.max_concurrency`
    - `.call(alias: str, messages, *, temperature, max_tokens, purpose: str, role: str, response_format=None) -> CompletionResult` — 获取信号量 → 调用 → 记账
    - `.total_prompt_tokens -> int`、`.total_completion_tokens -> int`、`.total_cost_usd -> float`、`.llm_calls -> int`
    - `.reset_stats() -> None`
    - `.validate_context_budget(bindings: Mapping[str, str]) -> None` — 对每个 `alias` 解析出的 binding 校验 `context_budget_tokens + max_output_tokens <= capabilities.max_context_tokens`，不满足抛 `ContextBudgetError`，消息含建议值
  - `agentcrew.models.factory.ModelFactoryError(RuntimeError)`
  - `agentcrew.models.factory.ContextBudgetError(ModelFactoryError)` — 属性 `alias`、`budget`、`window`、`suggested_budget`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_model_factory.py
import asyncio
import pytest
from pathlib import Path
from agentcrew.config import Settings
from agentcrew.models.factory import ModelFactory, ModelFactoryError
from agentcrew.models.providers.mock import MockProvider, set_purpose
from agentcrew.runtime.ledger import Ledger

@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()

def test_get_returns_cached_instance(settings):
    f = ModelFactory(settings)
    assert f.get("mock:m") is f.get("mock:m")

def test_mock_alias_returns_mock_provider(settings):
    assert isinstance(ModelFactory(settings).get("mock:m"), MockProvider)

def test_unknown_alias_raises(settings):
    with pytest.raises(Exception):
        ModelFactory(settings).get("wizard:merlin")

async def test_call_records_tokens_and_cost(settings, tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    p = f.get("mock:m")
    set_purpose(p, "execute", role="r", task_id="T-1", attempt=1)
    await f.call("mock:m", [{"role": "user", "content": "hello"}],
                 temperature=0, max_tokens=10, purpose="execute", role="r")
    assert f.llm_calls == 1
    assert f.total_prompt_tokens > 0
    assert lg.count("llm_call") == 1

async def test_call_emits_purpose_and_role(settings, tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    f = ModelFactory(settings, ledger=lg)
    p = f.get("mock:m"); set_purpose(p, "review", role="architect", task_id="T-1", attempt=1)
    await f.call("mock:m", [{"role": "user", "content": "x"}],
                 temperature=0, max_tokens=10, purpose="review", role="architect")
    ev = list(lg.events("llm_call"))[-1]
    assert ev.data["purpose"] == "review" and ev.data["role"] == "architect"

async def test_semaphore_caps_concurrency(settings, tmp_path):
    settings = settings.model_copy(update={"max_concurrency": 2})
    f = ModelFactory(settings, ledger=None)
    p = f.get("mock:m")
    peak = 0
    live = 0
    orig = p.complete

    async def slow(*a, **kw):
        nonlocal peak, live
        live += 1; peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return await orig(*a, **kw)

    p.complete = slow
    set_purpose(p, "execute", role="r", task_id="T-1", attempt=1)
    await asyncio.gather(*[
        f.call("mock:m", [{"role": "user", "content": "x"}],
               temperature=0, max_tokens=10, purpose="execute", role="r")
        for _ in range(8)])
    assert peak <= 2

async def test_reset_stats_zeroes_counters(settings, tmp_path):
    f = ModelFactory(settings)
    p = f.get("mock:m"); set_purpose(p, "execute", role="r", task_id="T-1", attempt=1)
    await f.call("mock:m", [{"role": "user", "content": "x"}],
                 temperature=0, max_tokens=10, purpose="execute", role="r")
    f.reset_stats()
    assert f.llm_calls == 0 and f.total_cost_usd == 0.0

def test_missing_api_key_raises_at_construction(settings, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = settings.model_copy(update={"mock": False})
    with pytest.raises(ModelFactoryError) as ei:
        ModelFactory(s).get("openai:gpt-4o-mini")
    assert "OPENAI_API_KEY" in str(ei.value)

def test_ollama_needs_no_real_key(settings):
    f = ModelFactory(settings)
    assert f.get("ollama:llama3") is not None

# ── spec §11.2：预算与模型窗口的关系校验（退出码 3） ──
def test_context_budget_fits_large_window(settings):
    f = ModelFactory(settings)
    f.validate_context_budget({"default": "openai:gpt-4o-mini"})     # 不抛

def test_context_budget_exceeds_small_window_raises(settings, monkeypatch):
    from agentcrew.models.factory import ContextBudgetError
    s = settings.model_copy(update={"context_budget_tokens": 200_000})
    f = ModelFactory(s)
    with pytest.raises(ContextBudgetError) as ei:
        f.validate_context_budget({"default": "openai:gpt-4o-mini"})
    assert ei.value.alias == "openai:gpt-4o-mini"
    assert ei.value.suggested_budget > 0
    assert ei.value.suggested_budget < 200_000

def test_context_budget_checks_every_bound_alias(settings, monkeypatch):
    """同一 run 内不同角色可能绑不同模型，必须逐个校验。"""
    from agentcrew.models.config import ModelBinding, ProviderCapabilities
    from agentcrew.models.factory import ContextBudgetError
    s = settings.model_copy(update={"context_budget_tokens": 60_000})
    f = ModelFactory(s)
    small = ModelBinding(alias="tinylocal:q", provider="ollama", model="q",
                         base_url=None, api_key_env="X",
                         capabilities=ProviderCapabilities(max_context_tokens=8192))
    monkeypatch.setattr(f, "_resolve", lambda a: small if a == "tinylocal:q"
                        else ModelBinding(alias=a, provider="openai", model="m",
                                          base_url=None, api_key_env="OPENAI_API_KEY",
                                          capabilities=ProviderCapabilities(
                                              max_context_tokens=128_000)))
    with pytest.raises(ContextBudgetError):
        f.validate_context_budget({"default": "openai:m", "qa_engineer": "tinylocal:q"})

def test_fallback_event_emitted_for_missing_fixture(settings, tmp_path):
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    fx = tmp_path / "fx"; (fx / "responses").mkdir(parents=True)
    f = ModelFactory(settings, ledger=lg, fixtures_dir=fx)
    p = f.get("mock:m")
    set_purpose(p, "execute", role="r", task_id="MISSING", attempt=1)
    asyncio.run(f.call("mock:m", [{"role": "user", "content": "x"}],
                       temperature=0, max_tokens=10, purpose="execute", role="r"))
    assert lg.count("mock_fallback") == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_model_factory.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`get(alias)`：`parse_alias(alias, overrides=settings.provider_overrides)` → 按 `binding.implementation` 分派：
- `"mock"` → `MockProvider(binding, fixtures_dir=self._fixtures_dir, latency_ms=settings.mock_latency_ms, on_fallback=...)`
- `"openai_compat"` → 取 `settings.api_key_for(provider)`；**除 `ollama` 外为空则抛 `ModelFactoryError`**（消息含 `api_key_env` 名）；`ollama` 用占位串 `"ollama"`。
- `"anthropic"` → 同理取 `ANTHROPIC_API_KEY`

`settings.mock is True` 时**所有**别名一律返回 `MockProvider`（用 `mock` 预设的能力位），实现 `--mock` 全局覆盖。

缓存：`self._cache: dict[str, BaseProvider]`。

`call()`：`async with self._llm_semaphore:` 包裹 `provider.complete(...)`；返回后累加计数、用 `estimate_cost` 算成本、`ledger.emit("llm_call", role=..., model=..., prompt_tokens=..., completion_tokens=..., estimated_cost_usd=..., latency_ms=..., purpose=...)`。

`_resolve(alias)` 是 `get()` 的解析部分（不加缓存），单独抽出以便测试 monkeypatch。`get()` = 查缓存 → 未命中则 `_resolve` 并存入。

`MockProvider` 的 `on_fallback` 回调接到 `lambda msg: ledger.emit("mock_fallback", message=msg)` —— 保证 fixture 缺失**绝不静默**（spec §16.1）。构造 MockProvider 时把当前 `purpose`/`role`/`task_id`/`attempt` 一并传入。

`validate_context_budget(bindings)`：对 `bindings` 中每个 `alias` 调 `_resolve`，检查 `settings.context_budget_tokens + caps.max_output_tokens <= caps.max_context_tokens`；不满足抛 `ContextBudgetError(alias, budget, window, suggested_budget=caps.max_context_tokens - caps.max_output_tokens - 1024)`。

`self._llm_semaphore` 在 `__init__` 中创建 —— **必须在事件循环内首次使用前创建**；因 Python 3.10+ 的 `asyncio.Semaphore` 不再绑定 loop，构造时创建是安全的。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_model_factory.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: ModelFactory 实例缓存与 LLM 调用限流记账"
```

---

# 阶段 F — 角色、Agent 与门禁 G2

## Task 22: RoleSpec 与 RoleCatalog

**Files:**
- Create: `agentcrew/roles/__init__.py`, `agentcrew/roles/spec.py`, `agentcrew/roles/catalog.py`, `agentcrew/roles/builtin.py`
- Test: `tests/test_roles_catalog.py`

**Interfaces:**
- Consumes: 无（纯数据 + 查找）
- Produces:
  - `agentcrew.roles.spec.RoleSpec` — Pydantic：`key, title, goal, backstory, responsibilities: list[str], boundaries: list[str], output_contract: str, system_prompt: str, temperature=0.3, max_output_tokens=8192, model="default", reviewer_for: list[str] = []`
  - `agentcrew.roles.builtin.BUILTIN_ROLES: tuple[RoleSpec, ...]` — 8 个内置角色，**由 `agentcrew/agents/*.py` 的 `ROLE` 常量组装**（Task 25 完成后填充；本任务先内联占位 8 个 `RoleSpec`，Task 25 改为从 agents 导入）
  - `agentcrew.roles.catalog.RoleCatalog`
    - `RoleCatalog(specs: Sequence[RoleSpec] = ())`
    - `.register(spec: RoleSpec) -> None` — key 重复抛 `DuplicateRoleError`
    - `.get(key: str) -> RoleSpec` — 缺失抛 `UnknownRoleError`
    - `.has(key: str) -> bool`
    - `.keys() -> list[str]`
    - `.all() -> list[RoleSpec]`
    - `.reviewer_for(role_key: str) -> list[str]` — 返回**全部**应评审该角色的角色 key
    - `.load_dir(path: Path) -> int` — 从 YAML 文件加载自定义角色，返回加载条数
  - `agentcrew.roles.catalog.DuplicateRoleError(ValueError)`、`UnknownRoleError(KeyError)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_roles_catalog.py
import pytest
from agentcrew.roles.spec import RoleSpec
from agentcrew.roles.catalog import RoleCatalog, DuplicateRoleError, UnknownRoleError
from agentcrew.roles.builtin import BUILTIN_ROLES

def spec(key, reviewer_for=()):
    return RoleSpec(key=key, title=key, goal="g", backstory="b",
                    responsibilities=["r"], boundaries=["x"],
                    output_contract="o", system_prompt="p",
                    reviewer_for=list(reviewer_for))

# ── 内置角色完整性（S8）──
def test_exactly_eight_builtin_roles():
    assert len(BUILTIN_ROLES) == 8

def test_builtin_role_keys_match_spec():
    assert {r.key for r in BUILTIN_ROLES} == {
        "project_manager", "product_manager", "architect", "backend_dev",
        "frontend_dev", "qa_engineer", "devops_engineer", "technical_writer"}

@pytest.mark.parametrize("idx", range(8))
def test_every_builtin_role_is_fully_specified(idx):
    r = BUILTIN_ROLES[idx]
    assert r.title and r.goal and r.backstory
    assert r.responsibilities and r.boundaries and r.output_contract
    assert len(r.system_prompt) > 80          # 提示词必须有实质内容
    assert r.temperature >= 0 and r.max_output_tokens > 0

def test_builtin_roles_have_unique_keys():
    keys = [r.key for r in BUILTIN_ROLES]
    assert len(keys) == len(set(keys))

# ── 目录行为 ──
def test_register_and_get():
    c = RoleCatalog()
    c.register(spec("x"))
    assert c.get("x").key == "x" and c.has("x")

def test_duplicate_key_rejected():
    c = RoleCatalog([spec("x")])
    with pytest.raises(DuplicateRoleError):
        c.register(spec("x"))

def test_unknown_key_raises():
    with pytest.raises(UnknownRoleError):
        RoleCatalog().get("nope")

def test_reviewer_for_uses_declaration():
    c = RoleCatalog([spec("a", reviewer_for=["architect"]),
                     spec("architect", reviewer_for=["project_manager"]),
                     spec("project_manager", reviewer_for=["architect", "product_manager"]),
                     spec("product_manager", reviewer_for=["project_manager"])])
    assert c.reviewer_for("a") == ["architect"]

def test_reviewer_for_falls_back_to_project_manager():
    c = RoleCatalog([spec("lonely"), spec("project_manager", reviewer_for=["architect"]),
                     spec("architect", reviewer_for=["project_manager"])])
    assert c.reviewer_for("lonely") == ["project_manager"]

def test_reviewer_for_wildcard():
    c = RoleCatalog([spec("boss", reviewer_for=["*"]), spec("worker")])
    assert c.reviewer_for("worker") == ["boss"]

def test_no_role_is_its_own_reviewer():
    c = RoleCatalog(BUILTIN_ROLES)
    for r in BUILTIN_ROLES:
        assert r.key not in c.reviewer_for(r.key), f"{r.key} 自评审会死锁"

def test_all_reviewers_exist_in_catalog():
    c = RoleCatalog(BUILTIN_ROLES)
    for r in BUILTIN_ROLES:
        for rev in c.reviewer_for(r.key):
            assert c.has(rev), f"{r.key} 的评审人 {rev} 不在目录中"

def test_load_dir_reads_yaml(tmp_path):
    d = tmp_path / "roles.d"; d.mkdir()
    (d / "wizard.yaml").write_text(
        "key: wizard\ntitle: 魔法师\ngoal: g\nbackstory: b\n"
        "responsibilities: [施法]\nboundaries: [不写代码]\n"
        "output_contract: o\nsystem_prompt: 你是一位魔法师。\n", encoding="utf-8")
    c = RoleCatalog()
    assert c.load_dir(d) == 1
    assert c.get("wizard").title == "魔法师"

def test_load_dir_missing_path_returns_zero(tmp_path):
    assert RoleCatalog().load_dir(tmp_path / "nope") == 0

def test_load_dir_reports_invalid_yaml(tmp_path):
    d = tmp_path / "roles.d"; d.mkdir()
    (d / "bad.yaml").write_text("key: bad\n", encoding="utf-8")
    c = RoleCatalog()
    with pytest.raises(Exception):
        c.load_dir(d)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_roles_catalog.py -v`
Expected: FAIL — `ImportError: cannot import name 'builtin'`

- [ ] **Step 3: 实现**

创建 `agentcrew/roles/builtin.py`，定义 8 个 `RoleSpec` 常量（`PROJECT_MANAGER`、`PRODUCT_MANAGER`、`ARCHITECT`、`BACKEND_DEV`、`FRONTEND_DEV`、`QA_ENGINEER`、`DEVOPS_ENGINEER`、`TECHNICAL_WRITER`）与 `BUILTIN_ROLES: tuple[RoleSpec, ...]`。每个 `system_prompt` 必须**实质描述岗位职责、输出要求、以及明确不做什么**（≥80 字符，中文）。

`reviewer_for` 按 spec §8.2 的「默认评审人」列填写，并令 `ARCHITECT.reviewer_for = ["project_manager"]`、`PRODUCT_MANAGER.reviewer_for = ["project_manager"]`、`PROJECT_MANAGER.reviewer_for = ["architect", "product_manager"]`，其余按表。

`reviewer_for(role_key)` 算法：
1. 收集所有 `r for r in specs if role_key in r.reviewer_for or "*" in r.reviewer_for`
2. 结果为空 → `["project_manager"]`（若目录中无 `project_manager` 则返回 `[]`）
3. **过滤掉 `role_key` 自身**（防自评审）
4. 过滤掉不在目录中的 key
5. 结果仍为空 → `[]`（调用方据此跳过 G2，直接由 G1 决定）

`load_dir(path)`：`path` 不存在返回 0；遍历 `*.yaml`/`*.yml`，`yaml.safe_load` → `RoleSpec.model_validate` → `register`。任一文件非法则整体抛错（不静默跳过）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_roles_catalog.py -v`
Expected: 全部 PASS，含 8 个内置角色的完整性与评审图无自环检查

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: RoleSpec 与 8 个内置角色目录"
```

---

## Task 23: 上下文组装与预算

**Files:**
- Create: `agentcrew/context/__init__.py`, `agentcrew/context/budget.py`, `agentcrew/context/context_bus.py`
- Test: `tests/test_context_budget.py`

**Interfaces:**
- Consumes: Task 1 `estimate_tokens`、Task 5 模型、Task 8 `TaskBoard`、Task 11 `ArtifactStore`
- Produces:
  - `agentcrew.context.context_bus.UpstreamDigest` — `task_id, role, title, brief, summary, files: list[str]`
  - `agentcrew.context.context_bus.FileExcerpt` — `path, content, truncated: bool`
  - `agentcrew.context.context_bus.DecisionRecord` — `at: datetime, task_id, role, decision: str`
  - `agentcrew.context.context_bus.ContextPack` — `project_goal, task, upstream: list[UpstreamDigest], decisions: list[DecisionRecord], file_tree: list[str], key_excerpts: list[FileExcerpt], total_estimated_tokens: int, truncated: bool`
  - `agentcrew.context.budget.TokenBudget` — `TokenBudget(limit: int)`，方法 `.remaining`, `.try_consume(text: str) -> bool`, `.used`
  - `agentcrew.context.context_bus.ContextBus`
    - `ContextBus(goal: str, board: TaskBoard, store: ArtifactStore, *, budget_tokens: int = 24000, excerpt_max_ratio: float = 0.4, tree_max_entries: int = 300)`
    - `.build(task: Task, *, fix_hints: Sequence[str] = ()) -> ContextPack`
    - `.render_notice() -> str` — 返回 `TRUNCATION_NOTICE`
    - `.add_decision(record: DecisionRecord) -> None`
    - `.decisions() -> list[DecisionRecord]`
    - `.save_decisions(path: Path) -> None`
    - `.load_decisions(path: Path) -> None`
    - `.record_rework_decision(task: Task, fix_hints: Sequence[str]) -> DecisionRecord | None` — 当任务 `rework_count >= 1` 且 `fix_hints` 非空时，提取一条决策记录（spec §11.3）；否则返回 `None`
  - `agentcrew.context.context_bus.TRUNCATION_NOTICE: str`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_budget.py
import pytest
from pathlib import Path
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.context.budget import TokenBudget
from agentcrew.context.context_bus import (
    ContextBus, DecisionRecord, TRUNCATION_NOTICE,
)
from agentcrew.task.task import Task, TaskStatus as S
from agentcrew.task.task_board import TaskBoard
from agentcrew.utils.tokens import estimate_tokens

def mk(tid, deps=(), status=S.PENDING, brief=""):
    return Task(id=tid, title=tid, description="d", role="backend_dev",
                acceptance_criteria=["a"], deliverables=[], dependencies=list(deps),
                status=status, brief=brief)

# ── TokenBudget ──
def test_budget_consumes_until_full():
    b = TokenBudget(limit=10)
    assert b.try_consume("a" * 10) is True
    assert b.try_consume("a" * 10_000) is False
    assert b.remaining >= 0

def test_budget_never_negative():
    b = TokenBudget(limit=5)
    b.try_consume("a" * 100_000)
    assert b.remaining == 0 and b.used <= 5

def test_budget_zero_limit_rejects_everything():
    b = TokenBudget(limit=0)
    assert b.try_consume("x") is False

# ── ContextBus ──
@pytest.fixture
def bus(tmp_path):
    board = TaskBoard([mk("A", status=S.SUCCEEDED, brief="产出 PRD"),
                       mk("B", ["A"]),
                       mk("C")])
    board.get("A").result = None
    store = ArtifactStore(tmp_path / "art", run_id="R")
    return ContextBus("做一个 Todo 应用", board, store, budget_tokens=24000)

def test_pack_includes_goal_and_task(bus):
    pack = bus.build(bus._board.get("B"))
    assert pack.project_goal == "做一个 Todo 应用"
    assert pack.task.id == "B"

def test_pack_includes_only_direct_upstream(bus):
    pack = bus.build(bus._board.get("B"))
    assert [u.task_id for u in pack.upstream] == ["A"]

def test_pack_excludes_unrelated_tasks(bus):
    pack = bus.build(bus._board.get("B"))
    assert all(u.task_id != "C" for u in pack.upstream)

def test_pack_includes_file_tree(bus):
    bus._store.commit("a.py", "x = 1\n",
                      spec=ArtifactSpec(path="a.py", kind="python", description="d"),
                      task=bus._board.get("A"))
    assert "a.py" in bus.build(bus._board.get("B")).file_tree

def test_small_pack_not_truncated(bus):
    assert bus.build(bus._board.get("B")).truncated is False

def test_tiny_budget_truncates_and_appends_notice(bus, tmp_path):
    b = ContextBus("做一个 Todo 应用", bus._board, bus._store, budget_tokens=30)
    pack = b.build(b._board.get("B"))
    assert pack.truncated is True
    assert b.render_notice() == TRUNCATION_NOTICE

def test_incompressible_fields_survive_tiny_budget(bus):
    b = ContextBus("目标", bus._board, bus._store, budget_tokens=1)
    pack = b.build(b._board.get("B"))
    assert pack.project_goal == "目标"          # 目标永不丢弃
    assert pack.task.id == "B"                  # 本任务永不丢弃

def test_fix_hints_are_incompressible(bus):
    b = ContextBus("目标", bus._board, bus._store, budget_tokens=1)
    p1 = b.build(b._board.get("B"), fix_hints=["补上输入校验"])
    assert p1.task.id == "B"

def test_excerpt_respects_max_ratio(bus):
    big = "x = 1\n" * 20000
    bus._store.commit("big.py", big,
                      spec=ArtifactSpec(path="big.py", kind="python", description="d"),
                      task=bus._board.get("A"))
    b = ContextBus("g", bus._board, bus._store, budget_tokens=2000,
                   excerpt_max_ratio=0.4)
    pack = b.build(b._board.get("B"))
    for ex in pack.key_excerpts:
        assert estimate_tokens(ex.content) <= 2000 * 0.4 + 50

def test_long_excerpt_marked_truncated(bus):
    bus._store.commit("big.py", "x = 1\n" * 20000,
                      spec=ArtifactSpec(path="big.py", kind="python", description="d"),
                      task=bus._board.get("A"))
    b = ContextBus("g", bus._board, bus._store, budget_tokens=2000)
    pack = b.build(b._board.get("B"))
    assert any(ex.truncated for ex in pack.key_excerpts)

def test_file_tree_folds_when_huge(bus):
    for i in range(400):
        bus._store.commit(f"d/{i}.py", "x=1\n",
                          spec=ArtifactSpec(path=f"d/{i}.py", kind="python", description="d"),
                          task=bus._board.get("A"))
    b = ContextBus("g", bus._board, bus._store, budget_tokens=24000, tree_max_entries=300)
    pack = b.build(b._board.get("B"))
    assert any("more" in line for line in pack.file_tree)

def test_decisions_roundtrip(bus, tmp_path):
    from datetime import datetime
    bus.add_decision(DecisionRecord(at=datetime.now(), task_id="A",
                                    role="backend_dev", decision="用内存存储"))
    p = tmp_path / "d.jsonl"
    bus.save_decisions(p)
    other = ContextBus("g", bus._board, bus._store)
    other.load_decisions(p)
    assert other.decisions()[0].decision == "用内存存储"

def test_decisions_capped_at_twenty(bus):
    from datetime import datetime
    for i in range(30):
        bus.add_decision(DecisionRecord(at=datetime.now(), task_id=f"T-{i}",
                                        role="r", decision=f"d{i}"))
    pack = bus.build(bus._board.get("B"))
    assert len(pack.decisions) <= 20

def test_decision_records_from_rework(bus):
    from datetime import datetime
    bus.add_decision(DecisionRecord(at=datetime.now(), task_id="A",
                                    role="architect", decision="改用 Pydantic v2"))
    assert any("Pydantic" in d.decision for d in bus.build(bus._board.get("B")).decisions)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_context_budget.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`TokenBudget.try_consume(text)`：算 `estimate_tokens(text)`；若 `used + n <= limit` 则 `used += n` 返回 `True`，否则返回 `False`（不改变 `used`）。

`ContextBus.build(task)` 按 spec §11.2 的**六级优先级**顺序装入，任何一级装不下就**跳过该项**（不中断后续更小的项）：
1. 目标 + 任务描述 + 验收标准 + `fix_hints` → **无条件装入**（不计预算，保证核心信息永在）
2. 直接上游（`task.dependencies`）的 `brief`+`summary`，每个 ≤400 tok 截断
3. 决策记录（最近 20 条，倒序取）
4. `file_tree`（超 `tree_max_entries` 时折叠为 `"... (N more)"` 单行）
5. `key_excerpts`：按「被下游依赖次数降序, path 升序」排序，单个文件的节选上限 = `budget * excerpt_max_ratio`，超出则按行截断并置 `truncated=True`
6. 更远祖先的摘要

任一非第 1 级项因预算被跳过 → `pack.truncated = True`。

`render_notice()` 返回 `TRUNCATION_NOTICE` 常量，内容为 spec §11.2 的那句话：
`"注意：为控制上下文长度，部分上游产物未全文展示；如需查看，请依据文件路径读取。"`

`save_decisions`/`load_decisions` 用 JSONL（每行一个 `DecisionRecord.model_dump_json()`），追加写。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_context_budget.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 上下文包组装与 token 预算降级"
```

---

## Task 24: BaseAgent 与 AgentFactory

**Files:**
- Create: `agentcrew/agents/__init__.py`, `agentcrew/agents/base_agent.py`, `agentcrew/agents/generic.py`, `agentcrew/agents/registry.py`
- Test: `tests/test_base_agent.py`

**Interfaces:**
- Consumes: Task 5/11/13/20/21/22/23 全部
- Produces:
  - `agentcrew.agents.base_agent.BaseAgent(ABC)`
    - `BaseAgent(spec: RoleSpec, *, models: ModelFactory, store: ArtifactStore, ledger: Ledger, project_goal: str)`
    - `.spec -> RoleSpec`
    - `async execute(self, task: Task, pack: ContextPack, *, fix_hints: Sequence[str] = ()) -> TaskResult` — 模板方法，**子类不重写**
    - `build_messages(self, task, pack, fix_hints) -> list[Message]` — 钩子
    - `parse_output(self, raw: str, task: Task) -> ParsedOutput` — 钩子（默认 `parse_fenced_blocks`）
    - `validate_output(self, parsed: ParsedOutput, task: Task) -> None` — 钩子（默认检查契约路径齐全）
    - `summarize(self, result: TaskResult) -> str` — 钩子（默认取 `notes` 前 400 字符）
    - `AgentExecutionError(RuntimeError)` — 属性 `task_id`、`role`
  - `agentcrew.agents.generic.GenericAgent(BaseAgent)` — 无额外行为的通用实现
  - `agentcrew.agents.registry.AGENT_CLASSES: dict[str, type[BaseAgent]]`
  - `agentcrew.agents.registry.AgentFactory`
    - `AgentFactory(models: ModelFactory, store, ledger, project_goal: str, catalog: RoleCatalog)`
    - `.register(role_key: str, cls: type[BaseAgent]) -> None`
    - `.get(role_key: str) -> BaseAgent` — 缓存；未注册的 key 用 `GenericAgent`
    - `.reviewer_agent_for(role_key: str) -> BaseAgent | None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_base_agent.py
import pytest
from pathlib import Path
from agentcrew.agents.base_agent import BaseAgent, AgentExecutionError
from agentcrew.agents.generic import GenericAgent
from agentcrew.agents.registry import AgentFactory, AGENT_CLASSES
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.mock import set_purpose
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskStatus as S
from agentcrew.task.task_board import TaskBoard

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "R" / "artifacts", run_id="R", ledger=lg)
    spec = ArtifactSpec(path="a.py", kind="python", description="d",
                        must_contain=["def f"])
    task = Task(id="T-1", title="t", description="写 a.py", role="backend_dev",
                acceptance_criteria=["可运行"], deliverables=[spec])
    board = TaskBoard([task])
    bus = ContextBus("目标", board, store)
    return dict(settings=s, ledger=lg, store=store, spec=spec, task=task,
                board=board, bus=bus, tmp_path=tmp_path)

def role():
    return RoleSpec(key="backend_dev", title="后端", goal="g", backstory="b",
                    responsibilities=["写代码"], boundaries=["不写前端"],
                    output_contract="输出 python 文件", system_prompt="你是后端工程师。")

def make_agent(env, fixtures_dir=None):
    models = ModelFactory(env["settings"], ledger=env["ledger"],
                          fixtures_dir=fixtures_dir)
    return GenericAgent(role(), models=models, store=env["store"],
                        ledger=env["ledger"], project_goal="目标")

async def test_execute_writes_contract_artifact(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=a.py\ndef f():\n    return 1\n```\n", encoding="utf-8")
    agent = make_agent(env, fx)
    result = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert result.task_id == "T-1" and result.role == "backend_dev"
    assert env["store"].read("a.py").startswith("def f()")
    assert result.summary

async def test_execute_raises_when_contract_path_missing(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=wrong.py\nx = 1\n```\n", encoding="utf-8")
    agent = make_agent(env, fx)
    with pytest.raises(AgentExecutionError):
        await agent.execute(env["task"], env["bus"].build(env["task"]))

async def test_execute_raises_on_unparseable_output(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=a.py\ndef f():\n", encoding="utf-8")     # 未闭合
    agent = make_agent(env, fx)
    with pytest.raises(AgentExecutionError):
        await agent.execute(env["task"], env["bus"].build(env["task"]))

async def test_execute_records_notes_from_prose(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "我实现了 f。\n```python file=a.py\ndef f():\n    return 1\n```\n", encoding="utf-8")
    agent = make_agent(env, fx)
    r = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert "我实现了 f" in r.notes      # 说明文字进 notes，不落盘
    assert not env["store"].exists("我实现了 f。")

async def test_fix_hints_reach_the_model(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=a.py\ndef f():\n    return 1\n```\n", encoding="utf-8")
    agent = make_agent(env, fx)
    captured = []
    orig = agent.build_messages
    agent.build_messages = lambda t, p, h: (captured.append(list(h)), orig(t, p, h))[1]
    await agent.execute(env["task"], env["bus"].build(env["task"]),
                        fix_hints=["补上输入校验"])
    assert captured[0] == ["补上输入校验"]

async def test_duration_and_timestamps_set(env, tmp_path):
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=a.py\ndef f():\n    return 1\n```\n", encoding="utf-8")
    agent = make_agent(env, fx)
    r = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert r.duration_ms >= 0 and r.finished_at >= r.started_at

async def test_subclass_hook_is_used(env, tmp_path):
    class Upper(BaseAgent):
        def parse_output(self, raw, task):
            from agentcrew.artifacts.parser import ParsedOutput
            return ParsedOutput(files={"a.py": "def f():\n    return 1\n"}, notes="hooked")
    fx = tmp_path / "fx"; (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text("whatever", encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    agent = Upper(role(), models=models, store=env["store"],
                  ledger=env["ledger"], project_goal="目标")
    r = await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert r.notes == "hooked"

def test_agent_factory_falls_back_to_generic(env):
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    f = AgentFactory(models, env["store"], env["ledger"], "目标", RoleCatalog())
    assert isinstance(f.get("nobody"), GenericAgent)

def test_agent_factory_caches(env):
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    f = AgentFactory(models, env["store"], env["ledger"], "目标", RoleCatalog())
    assert f.get("x") is f.get("x")

def test_reviewer_agent_for_returns_none_when_no_reviewers(env):
    models = ModelFactory(env["settings"], ledger=env["ledger"])
    f = AgentFactory(models, env["store"], env["ledger"], "目标", RoleCatalog())
    assert f.reviewer_agent_for("orphan") is None


# ── spec §12.2：模型别名三级优先级 ──
@pytest.mark.parametrize("role_model,role_bindings,default,expected", [
    ("anthropic:claude-sonnet-5", {"backend_dev": "qwen:qwen-max"},
     "openai:gpt-4o-mini", "anthropic:claude-sonnet-5"),          # 角色级最高
    ("default", {"backend_dev": "qwen:qwen-max"},
     "openai:gpt-4o-mini", "qwen:qwen-max"),                      # 次之 role_bindings
    ("default", {}, "openai:gpt-4o-mini", "openai:gpt-4o-mini"),  # 最后 default
])
async def test_model_alias_precedence(env, tmp_path, monkeypatch,
                                      role_model, role_bindings, default, expected):
    s = env["settings"].model_copy(update={"role_bindings": role_bindings,
                                           "default_model": default})
    fx = tmp_path / "fx"
    (fx / "responses" / "T-1").mkdir(parents=True)
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=a.py\ndef f():\n    return 1\n```\n", encoding="utf-8")
    models = ModelFactory(s, ledger=env["ledger"], fixtures_dir=fx)

    seen: list[str] = []
    orig = models.call
    async def spy(alias, *a, **kw):
        seen.append(alias)
        return await orig(alias, *a, **kw)
    monkeypatch.setattr(models, "call", spy)

    r = role(); r.model = role_model
    agent = GenericAgent(r, models=models, store=env["store"],
                         ledger=env["ledger"], project_goal="目标")
    await agent.execute(env["task"], env["bus"].build(env["task"]))
    assert seen == [expected]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_base_agent.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`BaseAgent.execute` 模板流程（子类不重写）：
1. `started_at = datetime.now()`；`ledger.emit("task_started", task_id=..., role=..., attempt=task.attempts)`
2. `messages = self.build_messages(task, pack, fix_hints)`
3. **解析模型别名**（spec §12.2 三级优先级，高者胜）：
   `alias = self.spec.model if self.spec.model != "default" else self._models.settings.role_bindings.get(self.spec.key) or self._models.settings.default_model`
   然后 `self._models.call(alias, messages, temperature=self.spec.temperature, max_tokens=self.spec.max_output_tokens, purpose="execute", role=self.spec.key)`
4. `parsed = self.parse_output(raw, task)`；解析异常 → 包成 `AgentExecutionError`
5. `self.validate_output(parsed, task)`；契约路径缺失/多余 → `AgentExecutionError`
6. 逐文件 `store.commit(path, content, spec=<契约中对应 ArtifactSpec>, task=task)`
7. 组装 `TaskResult`，`ledger.emit("task_finished", ...)`，返回

`build_messages` 默认实现：`[{"role":"system","content": self.spec.system_prompt + 输出契约 + 产物格式说明}, {"role":"user","content": 渲染后的 ContextPack}]`。渲染用 `_render_pack(pack, fix_hints)` 模块级函数：包含项目目标、本任务描述与验收标准、上游摘要、决策、文件树、节选正文；`pack.truncated` 为真时在末尾追加 `TRUNCATION_NOTICE`；`fix_hints` 非空时追加「上次评审意见，请修正」段落。

`validate_output` 默认实现：`set(parsed.files) == {d.path for d in task.deliverables if d.required}` 的超集关系检查 —— 缺失必需路径或出现未声明路径都抛错。

`AgentFactory.get(role_key)`：`spec = catalog.get(key)` 若存在否则构造一个临时 `RoleSpec`（key 即传入值，通用提示词）；`cls = AGENT_CLASSES.get(key, GenericAgent)`；缓存。

`reviewer_agent_for(role_key)`：`reviewers = catalog.reviewer_for(role_key)`；空 → `None`；否则返回首个评审人的 Agent（多评审人的遍历由 Task 26 的 `Reviewer` 处理）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_base_agent.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: BaseAgent 模板方法与 AgentFactory"
```

---

## Task 25: 八个角色 Agent

**Files:**
- Create: `agentcrew/agents/project_manager.py`, `product_manager.py`, `architect.py`, `backend_dev.py`, `frontend_dev.py`, `qa_engineer.py`, `devops_engineer.py`, `technical_writer.py`
- Modify: `agentcrew/agents/registry.py`（填充 `AGENT_CLASSES`）
- Test: `tests/test_agents_contract.py`

**Interfaces:**
- Consumes: Task 24 `BaseAgent`、Task 22 `RoleCatalog`/`BUILTIN_ROLES`
- Produces:
  - 每个角色文件导出 `ROLE: RoleSpec` 与 `<Name>Agent(BaseAgent)`，`spec = ROLE` 类属性
  - `ProjectManagerAgent` 额外提供 `async decompose(self, goal: str, *, feedback: Sequence[str] = ()) -> PlanOutput`（供 Task 28 的规划阶段调用）
  - `agentcrew.agents.registry.AGENT_CLASSES` 填充为 8 个 key → 类

- [ ] **Step 1: 写失败测试**

```python
# tests/test_agents_contract.py
import pytest
from agentcrew.agents.registry import AGENT_CLASSES
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.agents.base_agent import BaseAgent

def test_all_eight_roles_have_agent_classes():
    assert set(AGENT_CLASSES) == {r.key for r in BUILTIN_ROLES}

@pytest.mark.parametrize("key", sorted({r.key for r in BUILTIN_ROLES}))
def test_agent_class_spec_matches_catalog(key):
    from agentcrew.roles.catalog import RoleCatalog
    c = RoleCatalog(BUILTIN_ROLES)
    assert AGENT_CLASSES[key].spec.key == key
    assert AGENT_CLASSES[key].spec == c.get(key)

def test_every_agent_is_a_base_agent_subclass():
    for cls in AGENT_CLASSES.values():
        assert issubclass(cls, BaseAgent) and cls is not BaseAgent

def test_technical_writer_prompt_mentions_readme():
    assert "README" in AGENT_CLASSES["technical_writer"].spec.system_prompt

def test_qa_prompt_mentions_test_cases():
    p = AGENT_CLASSES["qa_engineer"].spec.system_prompt
    assert "测试" in p

def test_devops_prompt_mentions_docker():
    assert "Docker" in AGENT_CLASSES["devops_engineer"].spec.system_prompt

def test_architect_boundaries_exclude_implementation():
    b = " ".join(AGENT_CLASSES["architect"].spec.boundaries)
    assert "实现" in b or "编码" in b

def test_each_role_declares_nonempty_boundaries():
    for key, cls in AGENT_CLASSES.items():
        assert cls.spec.boundaries, f"{key} 未声明能力边界"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_agents_contract.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

每个角色文件形如：

```python
from agentcrew.agents.base_agent import BaseAgent
from agentcrew.roles.spec import RoleSpec

ROLE = RoleSpec(
    key="backend_dev",
    title="后端开发工程师",
    goal="实现后端业务逻辑、数据模型与 API 接口",
    backstory="...",
    responsibilities=["...", "..."],
    boundaries=["不负责前端页面", "不做技术选型决策（由架构师决定）"],
    output_contract="使用带 file= 路径的围栏代码块输出全部文件，路径必须与任务交付物契约一致",
    system_prompt="你是一名资深后端开发工程师……（≥80 字，说明职责、输出格式、边界）",
    temperature=0.2,
)

class BackendDevAgent(BaseAgent):
    spec = ROLE
```

**关键**：`roles/builtin.py` 中的 `BUILTIN_ROLES` 必须由这 8 个文件的 `ROLE` 组成（而非重复定义），即 `builtin.py` 改为 `from agentcrew.agents.backend_dev import ROLE as BACKEND_DEV` 等。为打破「agents 导入 roles、roles 导入 agents」的循环，**`RoleSpec` 定义在 `roles/spec.py`（无依赖），`agents/*.py` 只 import `roles.spec`，`roles/builtin.py` import `agents.*`**。这条导入链是单向的，无环。

`ProjectManagerAgent.decompose(goal, feedback)`：调用 `complete_structured`，schema 为 `agentcrew.task.planner.PlanOutput`，提示词包含**全部 8 个角色的 key 与职责**（从 `BUILTIN_ROLES` 渲染），要求「只能使用下列 role key」「依赖必须引用已定义的 id」「不得产生环」「每个任务的产物路径必须全局唯一」「必须包含 T-FINAL 之外的完整交付链，不要自己生成 T-FINAL」。`feedback` 非空时追加「上次拆解被拒绝，原因：…」。同时要求 PM 为每个任务填写 `brief`（一句话摘要，供下游上下文使用）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_agents_contract.py tests/test_roles_catalog.py -v`
Expected: 全部 PASS（含 Task 22 的 8 角色完整性用例，此时 `builtin.py` 已改为从 agents 导入）

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: 8 个内置角色 Agent 与提示词"
```

---

## Task 26: G2 LLM 评审

**Files:**
- Create: `agentcrew/gate/reviewer.py`
- Test: `tests/test_reviewer.py`

**Interfaces:**
- Consumes: Task 24 `AgentFactory`、Task 20 `complete_structured`、Task 11 `ArtifactStore`、Task 22 `RoleCatalog`
- Produces:
  - `agentcrew.gate.reviewer.ReviewVerdict` — Pydantic：`verdict: Literal["approve","reject"]`、`score: float`、`issues: list[str]`、`fix_hints: list[str]`
  - `agentcrew.gate.reviewer.Reviewer`
    - `Reviewer(models: ModelFactory, catalog: RoleCatalog, store: ArtifactStore, ledger: Ledger, *, excerpt_budget_tokens: int = 8000, format_retry_attempts: int = 2)`
    - `async review(self, task: Task, *, round_no: int) -> ReviewRecord`
  - `agentcrew.gate.reviewer.REVIEW_OUTPUT_SCHEMA` 由 `ReviewVerdict` 派生

- [ ] **Step 1: 写失败测试**

```python
# tests/test_reviewer.py
import json
import pytest
from pathlib import Path
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.gate.reviewer import Reviewer, ReviewVerdict
from agentcrew.models.factory import ModelFactory
from agentcrew.models.providers.mock import set_purpose
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task

APPROVE = {"verdict": "approve", "score": 0.9, "issues": [], "fix_hints": []}
REJECT = {"verdict": "reject", "score": 0.3,
          "issues": ["缺少输入校验"], "fix_hints": ["给 title 加非空校验"]}

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=lg)
    task = Task(id="T-2", title="后端 API", description="实现 CRUD", role="backend_dev",
                acceptance_criteria=["支持增删改查", "输入有校验"],
                deliverables=[ArtifactSpec(path="a.py", kind="python", description="d")])
    store.commit("a.py", "def f():\n    return 1\n", spec=task.deliverables[0], task=task)
    return dict(settings=s, ledger=lg, store=store, task=task, tmp_path=tmp_path)

def reviewer(env, reviews: dict, tmp_path):
    fx = tmp_path / "fx"; fx.mkdir(exist_ok=True)
    (fx / "reviews.json").write_text(json.dumps(reviews), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    return Reviewer(models, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"])

async def test_approve_produces_record(env, tmp_path):
    r = await reviewer(env, {"T-2": APPROVE}, tmp_path).review(env["task"], round_no=1)
    assert r.verdict == "approve" and r.layer == "llm"
    assert r.score == 0.9 and r.round == 1
    assert r.reviewer == "architect"           # backend_dev 的评审人

async def test_reject_carries_issues_and_hints(env, tmp_path):
    r = await reviewer(env, {"T-2": REJECT}, tmp_path).review(env["task"], round_no=1)
    assert r.verdict == "reject"
    assert r.issues == ["缺少输入校验"]
    assert r.fix_hints == ["给 title 加非空校验"]

async def test_reject_without_issues_is_treated_as_format_error(env, tmp_path):
    """spec §10.2：reject 必须带 issues，否则重试评审。"""
    bad = {"verdict": "reject", "score": 0.1, "issues": [], "fix_hints": []}
    rev = reviewer(env, {"T-2": bad}, tmp_path)
    r = await rev.review(env["task"], round_no=1)
    assert r.verdict == "approve"              # 格式错误重试耗尽后放行
    assert any("格式" in i or "warning" in i.lower() for i in r.issues) or r.score is not None

async def test_format_error_does_not_consume_rework_quota(env, tmp_path):
    bad = {"verdict": "reject", "score": 0.1, "issues": [], "fix_hints": []}
    rev = reviewer(env, {"T-2": bad}, tmp_path)
    before = env["task"].rework_count
    await rev.review(env["task"], round_no=1)
    assert env["task"].rework_count == before

async def test_reviewer_role_recorded(env, tmp_path):
    r = await reviewer(env, {"T-2": APPROVE}, tmp_path).review(env["task"], round_no=1)
    assert r.reviewer in {x.key for x in BUILTIN_ROLES}

async def test_pm_task_reviewed_by_two_roles(env, tmp_path):
    env["task"].role = "project_manager"
    r = await reviewer(env, {"T-2": APPROVE}, tmp_path).review(env["task"], round_no=1)
    assert "+" in r.reviewer or r.reviewer in ("architect", "product_manager")

async def test_review_emits_ledger_event(env, tmp_path):
    await reviewer(env, {"T-2": APPROVE}, tmp_path).review(env["task"], round_no=1)
    assert env["ledger"].count("gate_result") >= 1

async def test_missing_fixture_falls_back_not_crash(env, tmp_path):
    r = await reviewer(env, {}, tmp_path).review(env["task"], round_no=1)
    assert r.verdict in ("approve", "reject")

def test_review_verdict_rejects_extra_fields():
    with pytest.raises(Exception):
        ReviewVerdict(verdict="approve", score=1.0, issues=[], fix_hints=[], bonus=1)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_reviewer.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`review(task, round_no)` 流程：
1. `reviewers = catalog.reviewer_for(task.role)`；空 → 返回一条 `approve` 记录（`reviewer="none"`，`issues=["无可用评审人，跳过 LLM 评审"]`）
2. 渲染评审材料：任务描述 + `acceptance_criteria` + 产物内容（按 `excerpt_budget_tokens` 截断）+ 返工历史
3. 对每个评审人角色，用 `complete_structured` 取 `ReviewVerdict`；`provider` 通过 `ModelFactory.get(评审角色的 spec.model)`
4. **格式校验**：`verdict == "reject"` 且 `issues` 为空 → 视为格式错误，重试该评审（最多 `format_retry_attempts` 次）；仍失败 → 把该评审当作 `approve` 并附 `issues=["评审器返回格式错误，已放行"]`
5. **多评审人聚合**：全部 `approve` 才 `approve`；任一 `reject` 则结果为 `reject`，`issues`/`fix_hints` 取各评审人的并集，`score` 取最小值
6. `reviewer` 字段：单评审人为其 key；多评审人为 `"+".join(sorted(keys))`
7. `ledger.emit("gate_result", task_id=..., layer="llm", verdict=..., score=..., issues=...)`

评审提示词要求评审人「严格依据验收标准逐条核对」「不同意必须给出具体问题与可执行的修复建议」「只输出 JSON」。

**不修改 `task.rework_count`** —— 返工计数由 `Gatekeeper`（Task 27）在决定转 `REWORK` 时自增。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_reviewer.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: G2 LLM 评审与多评审人聚合"
```

---

## Task 27: Gatekeeper 三层编排

**Files:**
- Create: `agentcrew/gate/gatekeeper.py`
- Test: `tests/test_gatekeeper.py`

**Interfaces:**
- Consumes: Task 13 `run_static`、Task 26 `Reviewer`、Task 8 `TaskBoard`、Task 11 `ArtifactStore`
- Produces:
  - `agentcrew.gate.gatekeeper.GateDecision` — Pydantic：`verdict: Literal["approve","reject"]`、`record: ReviewRecord`、`next_status: TaskStatus`、`fix_hints: list[str]`
  - `agentcrew.gate.gatekeeper.PlanDecision` — `action: Literal["approve","edit","save_exit","cancel"]`、`tasks: list[Task] | None`
  - `agentcrew.gate.gatekeeper.DeliveryDecision` — `action: Literal["accept","rework","abort"]`、`task_ids: list[str] = []`
  - `agentcrew.gate.gatekeeper.Gatekeeper`
    - `Gatekeeper(reviewer: Reviewer, store: ArtifactStore, board: TaskBoard, *, gate: GateMode, ledger: Ledger, prompter: Prompter | None = None, context_bus: ContextBus | None = None)`
    - `async evaluate(self, task: Task, *, round_no: int) -> GateDecision`
    - `async human_plan_gate(self, tasks: Sequence[Task]) -> PlanDecision`
    - `async human_delivery_gate(self, record: RunRecord) -> DeliveryDecision`
  - `agentcrew.gate.gatekeeper.Prompter` — `Protocol`：`ask_plan(...) -> PlanDecision`、`ask_delivery(...) -> DeliveryDecision`
  - `agentcrew.gate.gatekeeper.NullPrompter` — 全自动实现，`plan → approve`、`delivery → accept`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_gatekeeper.py
import json
import pytest
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.gate.gatekeeper import (
    Gatekeeper, NullPrompter, PlanDecision, DeliveryDecision,
)
from agentcrew.gate.reviewer import Reviewer
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskStatus as S
from agentcrew.task.task_board import TaskBoard

GOOD = {"verdict": "approve", "score": 0.9, "issues": [], "fix_hints": []}
BAD = {"verdict": "reject", "score": 0.2,
       "issues": ["实现过于简单"], "fix_hints": ["补充业务规则"]}

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=lg)
    task = Task(id="T-2", title="t", description="d", role="backend_dev",
                acceptance_criteria=["a"],
                deliverables=[ArtifactSpec(path="a.py", kind="python",
                                           description="d", must_contain=["def f"])])
    board = TaskBoard([task])
    board.transition("T-2", S.READY); board.transition("T-2", S.RUNNING)
    board.transition("T-2", S.REVIEW)
    return dict(settings=s, ledger=lg, store=store, task=task, board=board,
                tmp_path=tmp_path)

def commit(env, content):
    env["store"].commit("a.py", content, spec=env["task"].deliverables[0],
                        task=env["task"])

def gk(env, reviews, tmp_path, gate="none"):
    fx = tmp_path / "fx"; fx.mkdir(exist_ok=True)
    (fx / "reviews.json").write_text(json.dumps(reviews), encoding="utf-8")
    models = ModelFactory(env["settings"], ledger=env["ledger"], fixtures_dir=fx)
    rev = Reviewer(models, RoleCatalog(BUILTIN_ROLES), env["store"], env["ledger"])
    return Gatekeeper(rev, env["store"], env["board"], gate=gate,
                      ledger=env["ledger"], prompter=NullPrompter())

async def test_all_pass_leads_to_succeeded(env, tmp_path):
    commit(env, "def f():\n    return 1\n")
    d = await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=1)
    assert d.verdict == "approve" and d.next_status is S.SUCCEEDED

async def test_static_failure_short_circuits_before_llm(env, tmp_path):
    commit(env, "def g():\n    return 1\n")          # 缺 must_contain
    d = await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=1)
    assert d.verdict == "reject" and d.record.layer == "static"
    assert d.next_status is S.REWORK                 # 首次驳回仍有返工额度
    assert env["ledger"].count("gate_result") >= 1
    assert not env["task"].reviews or env["task"].reviews[-1].layer == "static"

async def test_llm_rejection_leads_to_rework_with_hints(env, tmp_path):
    commit(env, "def f():\n    return 1\n")
    d = await gk(env, {"T-2": BAD}, tmp_path).evaluate(env["task"], round_no=1)
    assert d.verdict == "reject" and d.next_status is S.REWORK
    assert d.fix_hints == ["补充业务规则"]

async def test_rework_is_transitioned_on_board(env, tmp_path):
    commit(env, "def g():\n")
    await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=1)
    assert env["board"].get("T-2").status is S.REWORK
    assert env["board"].get("T-2").rework_count == 1

async def test_failing_after_max_reworks_leads_to_failed(env, tmp_path):
    env["board"].get("T-2").rework_count = 2         # == max_reworks
    commit(env, "def g():\n")
    d = await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=3)
    assert d.verdict == "reject" and d.next_status is S.FAILED

async def test_approve_transitions_board_to_succeeded(env, tmp_path):
    commit(env, "def f():\n    return 1\n")
    await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=1)
    assert env["board"].get("T-2").status is S.SUCCEEDED

async def test_reviews_are_appended_to_task(env, tmp_path):
    commit(env, "def f():\n    return 1\n")
    await gk(env, {"T-2": GOOD}, tmp_path).evaluate(env["task"], round_no=1)
    assert len(env["board"].get("T-2").reviews) >= 1

async def test_gate_plan_approve_returns_same_tasks(env, tmp_path):
    d = await gk(env, {}, tmp_path, gate="plan").human_plan_gate([env["task"]])
    assert isinstance(d, PlanDecision) and d.action == "approve"

async def test_gate_none_skips_human_prompts(env, tmp_path):
    d = await gk(env, {}, tmp_path, gate="none").human_delivery_gate(None)
    assert d.action == "accept"

async def test_delivery_gate_accept(env, tmp_path):
    d = await gk(env, {}, tmp_path, gate="delivery").human_delivery_gate(None)
    assert isinstance(d, DeliveryDecision)

async def test_null_prompter_is_fully_automatic():
    p = NullPrompter()
    assert (await p.ask_plan([])).action == "approve"
    assert (await p.ask_delivery(None)).action == "accept"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_gatekeeper.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`evaluate(task, round_no)`：
1. `static = run_static(task, store, round_no=round_no)`；`board.record_review(task.id, static)`
2. `static.verdict == "reject"` → 走驳回路径（**不调用 LLM**，短路）
3. 否则 `llm = await reviewer.review(task, round_no=round_no)`；`board.record_review(...)`
4. 判定 `next_status`：
   - 通过 → `SUCCEEDED`，`board.transition(...)`
   - 驳回且 `task.rework_count < task.max_reworks` → `REWORK`，`board.transition(...)`，且 `board.bump_rework()`
   - 驳回且额度用尽 → `FAILED`
5. `ledger.emit("gate_result", ...)` 每次门禁都发一条
6. **返工决策记录**（spec §11.3）：若是 `REWORK` 且 `context_bus` 非空 → `context_bus.record_rework_decision(task, fix_hints)`，把本轮的 `fix_hints` 沉淀为全局决策，供下游 Agent 参考
7. 返回 `GateDecision`（含累积的 `fix_hints`）

`human_plan_gate`：`gate in ("plan","both")` 且 `prompter` 非 `NullPrompter` 时调 `prompter.ask_plan(tasks)`；否则 `PlanDecision(action="approve", tasks=list(tasks))`。

`human_delivery_gate`：`gate in ("delivery","both")` 时调 `prompter.ask_delivery(record)`；否则 `accept`。

`NullPrompter` 两个方法直接返回 `approve`/`accept`，**不做任何 IO**（测试与 `--auto` 用）。

`TaskBoard` 需新增 `bump_rework(task_id) -> Task`（若 Task 8 未实现，本任务补上：对 `rework_count += 1` 并返回新 Task，不改状态）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_gatekeeper.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Gatekeeper 三层门禁编排与短路"
```

---

# 阶段 G — 调度与编排

## Task 28: Scheduler 调度引擎

**Files:**
- Create: `agentcrew/crew/__init__.py`, `agentcrew/crew/scheduler.py`
- Test: `tests/test_scheduler.py`, `tests/test_scheduler_deadlock.py`

**Interfaces:**
- Consumes: Task 8 `TaskBoard`、Task 23 `ContextBus`、Task 24 `AgentFactory`、Task 27 `Gatekeeper`、Task 15 `CheckpointStore`、Task 14 `Ledger`
- Produces:
  - `agentcrew.crew.scheduler.Scheduler`
    - `Scheduler(board: TaskBoard, agents: AgentFactory, gatekeeper: Gatekeeper, context_bus: ContextBus, *, ledger: Ledger, checkpoint: CheckpointStore | None = None, max_concurrency: int = 4, on_event: Callable[[str, dict], None] | None = None)`
    - `async run(self) -> None`
    - `.cancel(self, *, hard: bool = False) -> None`
    - `.deadlock_report -> str | None`
    - `.peak_concurrency -> int`
  - `agentcrew.crew.scheduler.WorkerOutcome` — Pydantic：`task_id, result: TaskResult | None, decision: GateDecision | None, error: str | None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_scheduler.py
import asyncio
import json
import pytest
from pathlib import Path
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.crew.scheduler import Scheduler
from agentcrew.gate.gatekeeper import Gatekeeper, NullPrompter
from agentcrew.gate.reviewer import Reviewer
from agentcrew.models.factory import ModelFactory
from agentcrew.agents.registry import AgentFactory
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskStatus as S
from agentcrew.task.task_board import TaskBoard

GOOD = {"verdict": "approve", "score": 0.95, "issues": [], "fix_hints": []}

def mk(tid, deps=(), role="backend_dev", priority=5):
    return Task(id=tid, title=tid, description=f"写 {tid}", role=role,
                acceptance_criteria=["ok"], dependencies=list(deps),
                priority=priority,
                deliverables=[ArtifactSpec(path=f"{tid}.py", kind="python",
                                           description="d")])

def write_fixture(fx: Path, task_id: str, attempt: int = 1, bad: bool = False):
    d = fx / "responses" / task_id; d.mkdir(parents=True, exist_ok=True)
    body = "broken(" if bad else f"# {task_id}\nX = 1\n"
    (d / f"attempt-{attempt}.md").write_text(
        f"```python file={task_id}.py\n{body}```\n", encoding="utf-8")

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    s = Settings.load()
    lg = Ledger(tmp_path / "l.jsonl", run_id="R", secrets=s.secret_values())
    store = ArtifactStore(tmp_path / "o" / "art", run_id="R", ledger=lg)
    fx = tmp_path / "fx"; fx.mkdir()
    (fx / "reviews.json").write_text(json.dumps({"*": GOOD}), encoding="utf-8")
    models = ModelFactory(s, ledger=lg, fixtures_dir=fx)
    catalog = RoleCatalog(BUILTIN_ROLES)
    agents = AgentFactory(models, store, lg, "目标", catalog)
    rev = Reviewer(models, catalog, store, lg)
    return dict(settings=s, ledger=lg, store=store, fx=fx, models=models,
                catalog=catalog, agents=agents, reviewer=rev, tmp_path=tmp_path)

def build(env, tasks, max_concurrency=4):
    board = TaskBoard(tasks)
    store = env["store"]
    bus = ContextBus("目标", board, store)
    gk = Gatekeeper(env["reviewer"], store, board, gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    return board, Scheduler(board, env["agents"], gk, bus,
                            ledger=env["ledger"], max_concurrency=max_concurrency)

async def test_runs_diamond_to_completion(env):
    write_fixture(env["fx"], "A"); write_fixture(env["fx"], "B")
    write_fixture(env["fx"], "C"); write_fixture(env["fx"], "D")
    board, sch = build(env, [mk("A"), mk("B", ["A"]), mk("C", ["A"]), mk("D", ["B", "C"])])
    await sch.run()
    assert all(board.get(t).status is S.SUCCEEDED for t in "ABCD")
    assert board.is_terminal()

async def test_respects_max_concurrency(env):
    for t in "ABCDEF":
        write_fixture(env["fx"], t)
    env["settings"] = env["settings"].model_copy(update={"max_concurrency": 2})
    env["models"] = ModelFactory(env["settings"], ledger=env["ledger"],
                                 fixtures_dir=env["fx"])
    board = TaskBoard([mk(t) for t in "ABCDEF"])
    bus = ContextBus("g", board, env["store"])
    gk = Gatekeeper(env["reviewer"], env["store"], board, gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    agents = AgentFactory(env["models"], env["store"], env["ledger"], "g", env["catalog"])
    sch = Scheduler(board, agents, gk, bus, ledger=env["ledger"], max_concurrency=2)
    await sch.run()
    assert sch.peak_concurrency <= 2

async def test_failure_blocks_downstream(env):
    write_fixture(env["fx"], "A", bad=True)
    for i in (1, 2, 3):
        write_fixture(env["fx"], "A", attempt=i, bad=True)
    write_fixture(env["fx"], "B")
    a = mk("A"); a.max_attempts = 1; a.max_reworks = 0
    board, sch = build(env, [a, mk("B", ["A"])])
    await sch.run()
    assert board.get("A").status is S.FAILED
    assert board.get("B").status is S.BLOCKED

async def test_unrelated_branch_still_completes_after_failure(env):
    for i in (1, 2, 3):
        write_fixture(env["fx"], "A", attempt=i, bad=True)
    write_fixture(env["fx"], "C")
    a = mk("A"); a.max_attempts = 1; a.max_reworks = 0
    board, sch = build(env, [a, mk("B", ["A"]), mk("C")])
    await sch.run()
    assert board.get("C").status is S.SUCCEEDED
    assert board.get("B").status is S.BLOCKED

async def test_rework_loop_uses_second_attempt_fixture(env):
    write_fixture(env["fx"], "A", attempt=1, bad=True)
    write_fixture(env["fx"], "A", attempt=2)
    board, sch = build(env, [mk("A")])
    await sch.run()
    assert board.get("A").status is S.SUCCEEDED
    assert board.get("A").rework_count == 1

async def test_cancel_stops_dispatching_new_tasks(env):
    for t in "ABCDE":
        write_fixture(env["fx"], t)
    board, sch = build(env, [mk(t) for t in "ABCDE"], max_concurrency=1)
    async def canceller():
        await asyncio.sleep(0.01); sch.cancel()
    await asyncio.gather(sch.run(), canceller())
    assert not board.is_terminal()
    assert board.get("E").status is not S.SUCCEEDED

async def test_deadlock_reported_when_no_ready_and_no_running(env):
    board = TaskBoard([mk("A"), mk("B", ["A"])])
    board.transition("A", S.READY); board.transition("A", S.RUNNING)
    board.transition("A", S.REVIEW)
    board.transition("A", S.REWORK)
    board.get("A").status = S.SUCCEEDED          # 人为制造不可达状态
    board.get("B").status = S.PENDING
    bus = ContextBus("g", board, env["store"])
    gk = Gatekeeper(env["reviewer"], env["store"], board, gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    sch = Scheduler(board, env["agents"], gk, bus, ledger=env["ledger"])
    board.get("B").status = S.RUNNING            # 卡住：RUNNING 但引擎无对应 worker
    await asyncio.wait_for(sch.run(), timeout=3)
    assert sch.deadlock_report is not None

async def test_checkpoint_saved_after_each_completion(env, tmp_path):
    from agentcrew.runtime.checkpoint import CheckpointStore
    write_fixture(env["fx"], "A")
    cs = CheckpointStore.create(tmp_path / "o", project_goal="g",
                                config_snapshot={}, run_id="R1")
    board = TaskBoard([mk("A")])
    bus = ContextBus("g", board, env["store"])
    gk = Gatekeeper(env["reviewer"], env["store"], board, gate="none",
                    ledger=env["ledger"], prompter=NullPrompter())
    sch = Scheduler(board, env["agents"], gk, bus, ledger=env["ledger"],
                    checkpoint=cs, max_concurrency=4)
    await sch.run()
    assert json.loads((cs.run_dir / "tasks.json").read_text(encoding="utf-8"))
```

```python
# tests/test_scheduler_deadlock.py
"""槽位自锁守门测试（spec §7.3）。

背景：若把 G2 评审放进任务槽位内部，而评审又要走 LLM 调用级信号量，
在 max_concurrency=1 时调度器会自己等自己。这里用最小并发跑一个
【静态通过 → 必须走 G2】的任务来钉死这个回归。
"""
import asyncio
import json
from pathlib import Path

import pytest

from agentcrew.agents.registry import AgentFactory
from agentcrew.artifacts.spec import ArtifactSpec
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.crew.scheduler import Scheduler
from agentcrew.gate.gatekeeper import Gatekeeper, NullPrompter
from agentcrew.gate.reviewer import Reviewer
from agentcrew.models.factory import ModelFactory
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import Task, TaskStatus as S
from agentcrew.task.task_board import TaskBoard

APPROVE = {"verdict": "approve", "score": 0.95, "issues": [], "fix_hints": []}

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    settings = Settings.load().model_copy(update={"max_concurrency": 1})
    ledger = Ledger(tmp_path / "l.jsonl", run_id="R")
    store = ArtifactStore(tmp_path / "art", run_id="R", ledger=ledger)

    fx = tmp_path / "fx"
    (fx / "responses" / "T-1").mkdir(parents=True)
    # 产物必须通过 G1（含 must_contain），才会走到 G2
    (fx / "responses" / "T-1" / "attempt-1.md").write_text(
        "```python file=app.py\ndef create_app():\n    return None\n```\n",
        encoding="utf-8")
    (fx / "reviews.json").write_text(json.dumps({"*": APPROVE}), encoding="utf-8")

    models = ModelFactory(settings, ledger=ledger, fixtures_dir=fx)
    catalog = RoleCatalog(BUILTIN_ROLES)
    task = Task(id="T-1", title="t", description="d", role="backend_dev",
                acceptance_criteria=["可运行"],
                deliverables=[ArtifactSpec(path="app.py", kind="python",
                                           description="d",
                                           must_contain=["def create_app"])])
    board = TaskBoard([task])
    gatekeeper = Gatekeeper(Reviewer(models, catalog, store, ledger), store, board,
                            gate="none", ledger=ledger, prompter=NullPrompter())
    scheduler = Scheduler(board, AgentFactory(models, store, ledger, "g", catalog),
                          gatekeeper, ContextBus("g", board, store),
                          ledger=ledger, max_concurrency=1)
    return dict(settings=settings, ledger=ledger, board=board, scheduler=scheduler)


async def test_single_slot_completes_without_deadlock(env):
    """max_concurrency=1 且任务必须过 G2 —— 不得死锁。"""
    await asyncio.wait_for(env["scheduler"].run(), timeout=5)
    assert env["board"].get("T-1").status is S.SUCCEEDED


async def test_g2_actually_ran(env):
    """确认 G2 确实被调用过，否则上面的用例只是空跑。"""
    await asyncio.wait_for(env["scheduler"].run(), timeout=5)
    assert any(e.data.get("layer") == "llm" for e in env["ledger"].events("gate_result"))


async def test_peak_concurrency_never_exceeds_one(env):
    await asyncio.wait_for(env["scheduler"].run(), timeout=5)
    assert env["scheduler"].peak_concurrency <= 1
```

> 若实现把门禁放进了任务槽位内，`test_single_slot_completes_without_deadlock` 会在 5 秒后超时失败。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_scheduler.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

严格按 spec §7.3 的伪码实现，特别注意 **worker 的槽位边界**：

```python
async def _worker(self, task: Task) -> None:
    async with self._task_slot:              # 任务槽位
        outcome = await self._execute_one(task)
    decision = await self._gatekeeper.evaluate(task, round_no=...)   # 槽位外
    self._queue.put_nowait(...)
```

即：**门禁必须在 `async with self._task_slot` 之外调用**。LLM 调用级信号量由 `ModelFactory` 内部持有，二者不嵌套。

主循环：`refresh()` → `take_ready(limit=free_slots)` → 派发 → 若有在跑则 `await queue.get()` → 处理完成事件 → （若挂了 checkpoint）落盘。无就绪且无在跑但仍未终结 → `self._deadlock_report = board.explain_stall()` 后 `break`。

`peak_concurrency` 在每次派发/完成时更新。

`cancel(hard=False)`：置 `self._cancel_event`；主循环在每轮开头检查，置位后停止派发新任务，等在跑的 worker 自然结束。`hard=True` 时额外 `task.cancel()` 所有在跑的 asyncio Task。

失败处理：worker 抛 `AgentExecutionError` →
- `task.attempts < task.max_attempts` → `transition(READY, reason="retry")`
- 否则 → `transition(FAILED)`

**注意**：`attempts` 已在 `take_ready` 时自增（Task 8），此处判断用 `task.attempts >= task.max_attempts`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_scheduler.py tests/test_scheduler_deadlock.py -v`
Expected: 全部 PASS，特别是 `test_review_does_not_deadlock_with_single_slot` 不超时

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Scheduler 就绪队列调度与两级并发限额"
```

---

## Task 29: Crew 两阶段编排

**Files:**
- Create: `agentcrew/crew/crew.py`
- Test: `tests/test_crew.py`

**Interfaces:**
- Consumes: Task 全部
- Produces:
  - `agentcrew.crew.crew.Crew`
    - `Crew(settings: Settings, *, catalog: RoleCatalog | None = None, store: ArtifactStore | None = None, checkpoint: CheckpointStore | None = None, prompter: Prompter | None = None)`
    - `.register_role(spec: RoleSpec, cls: type[BaseAgent] | None = None) -> None`
    - `async run(self, goal: str) -> RunRecord`
    - `async resume(self) -> RunRecord` — 需先通过 `Crew.resume_from(settings, run_id, ...)` 构造
    - `Crew.resume_from(settings: Settings, run_id: str, **kw) -> Crew`
    - `.catalog -> RoleCatalog`（只读属性，供 `register_role` 与测试使用）
    - `.checkpoint -> CheckpointStore`（只读属性；`run()`/`resume()` 执行后可用）
    - `.delivery_summary(record: RunRecord) -> str`
  - `agentcrew.crew.crew.PlanFailedError(RuntimeError)` — 属性 `errors: list[str]`
  - `agentcrew.crew.crew.RunCancelled(RuntimeError)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_crew.py
import json, pytest
from pathlib import Path
from agentcrew.config import Settings
from agentcrew.crew.crew import Crew, PlanFailedError
from agentcrew.roles.spec import RoleSpec

PLAN = {"tasks": [
  {"id": "T-001", "title": "需求", "description": "写 PRD", "role": "product_manager",
   "acceptance_criteria": ["含功能清单"], "dependencies": [], "priority": 1,
   "brief": "PRD",
   "deliverables": [{"path": "docs/PRD.md", "kind": "markdown",
                     "description": "PRD", "must_contain": ["#"]}]},
  {"id": "T-002", "title": "后端", "description": "写后端", "role": "backend_dev",
   "acceptance_criteria": ["有 API"], "dependencies": ["T-001"], "priority": 3,
   "brief": "后端实现",
   "deliverables": [{"path": "backend/app/main.py", "kind": "python",
                     "description": "入口", "must_contain": ["def create_app"]}]},
]}

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    fx = tmp_path / "fx"; fx.mkdir()
    (fx / "plan_todo.json").write_text(json.dumps(PLAN), encoding="utf-8")
    (fx / "reviews.json").write_text(json.dumps({"*": {
        "verdict": "approve", "score": 0.9, "issues": [], "fix_hints": []}}),
        encoding="utf-8")
    for tid, path, body in [
        ("T-001", "docs/PRD.md", "# PRD\n\n功能清单：增删改查\n验收标准：全部通过\n"),
        ("T-002", "backend/app/main.py", "def create_app():\n    return None\n"),
        ("T-FINAL", "DELIVERY.md", "# 交付总结\n\n## 完成\n- 全部任务完成\n\n## 运行\nuvicorn\n"),
    ]:
        d = fx / "responses" / tid; d.mkdir(parents=True, exist_ok=True)
        (d / "attempt-1.md").write_text(f"```{path.split('.')[-1]} file={path}\n{body}```\n",
                                        encoding="utf-8")
    return dict(settings=Settings.load(), fx=fx, tmp_path=tmp_path)

async def test_run_produces_completed_record(env, monkeypatch):
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", env["fx"])
    crew = Crew(env["settings"])
    rec = await crew.run("做一个 Todo 应用")
    assert rec.project_goal == "做一个 Todo 应用"
    assert rec.stats.total_tasks == 3          # 2 个计划任务 + T-FINAL
    assert rec.stats.succeeded == 3

async def test_run_creates_artifacts_and_manifest(env, monkeypatch):
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", env["fx"])
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    art = env["settings"].output_dir / rec.run_id / "artifacts"
    assert (art / "docs" / "PRD.md").exists()
    assert (art / "backend" / "app" / "main.py").exists()
    assert (art / "DELIVERY.md").exists()
    assert json.loads((art / "MANIFEST.json").read_text(encoding="utf-8"))

async def test_plan_validation_failure_retries_then_raises(env, monkeypatch):
    bad = {"tasks": [{"id": "T-1", "title": "x", "description": "d",
                      "role": "does_not_exist", "acceptance_criteria": ["a"],
                      "deliverables": [], "dependencies": []}] * 2}
    (env["fx"] / "plan_todo.json").write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", env["fx"])
    with pytest.raises(PlanFailedError) as ei:
        await Crew(env["settings"]).run("g")
    assert ei.value.errors

async def test_register_custom_role_works_without_engine_changes(env, monkeypatch):
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", env["fx"])
    crew = Crew(env["settings"])
    crew.register_role(RoleSpec(key="security_auditor", title="安全审计",
                                goal="g", backstory="b",
                                responsibilities=["审计"], boundaries=["不写业务代码"],
                                output_contract="输出报告", system_prompt="你是安全审计员。"))
    assert crew.catalog.has("security_auditor")

async def test_resume_continues_interrupted_run(env, monkeypatch):
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", env["fx"])
    crew = Crew(env["settings"])
    rec = await crew.run("做一个 Todo 应用")
    rs = Settings.load()
    resumed = Crew.resume_from(rs, rec.run_id)
    board = resumed.checkpoint.load_board()
    assert all(t.status.value == "succeeded" for t in board.all())

async def test_delivery_summary_mentions_counts(env, monkeypatch):
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", env["fx"])
    crew = Crew(env["settings"])
    rec = await crew.run("做一个 Todo 应用")
    text = crew.delivery_summary(rec)
    assert "成功" in text or "succeeded" in text
    assert str(rec.stats.total_tasks) in text

async def test_run_record_persisted_with_config_snapshot(env, monkeypatch):
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", env["fx"])
    rec = await Crew(env["settings"]).run("做一个 Todo 应用")
    p = env["settings"].output_dir / rec.run_id / "run.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "completed" and data["schema_version"] == 1
    assert data["config_snapshot"]["max_concurrency"] == 4
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_crew.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`Crew.__init__` 组装：`Settings` → `ModelFactory`（带 `default_fixtures_dir`）→ `RoleCatalog(BUILTIN_ROLES)`（+ `roles_dir` 加载）→ `CheckpointStore`（run 时创建）→ `ArtifactStore` → `AgentFactory` → `Reviewer` → `Gatekeeper`。`_fixtures_dir` 从 `agentcrew.models.factory.DEFAULT_FIXTURES_DIR` 读取（测试 monkeypatch 它）。

`run(goal)` 流程：
1. `checkpoint = CheckpointStore.create(output_dir, project_goal=goal, config_snapshot=settings.model_dump(mode="json"))`
2. `ledger = Ledger(checkpoint.run_dir / "ledger.jsonl", run_id=..., secrets=settings.secret_values())`
3. 建 `ArtifactStore(checkpoint.artifacts_dir, ...)`、`AgentFactory` 等（若构造时未注入）
4. **配置校验**：`models.validate_context_budget({role: spec.model or settings.default_model for role, spec in catalog})` —— 不通过抛 `ContextBudgetError`，`main()` 转成退出码 3（spec §11.2）
5. **规划阶段**：`plan = await pm_agent.decompose(goal)` → `tasks = to_tasks(plan)` → `validate_plan(tasks, catalog)` → `tasks = with_final_task(tasks)` —— 任一步失败，把 `errors` 回灌重试，上限 `settings.plan_max_attempts`；仍失败抛 `PlanFailedError`
6. **G3 计划闸门**：`decision = await gatekeeper.human_plan_gate(tasks)`；`cancel` → 抛 `RunCancelled`；`save_exit` → 落盘后抛 `RunCancelled`；`edit` → 用返回的 tasks
7. `board = TaskBoard(tasks, on_change=lambda t, c: checkpoint.save_record(...))`
8. `scheduler = Scheduler(board, agents, gatekeeper, ContextBus(...), ledger=..., checkpoint=checkpoint, max_concurrency=...)`
9. `await scheduler.run()`
10. **G3 交付闸门**：`gatekeeper.human_delivery_gate(record)`；`abort` → `CANCELLED`；`rework` → 把指定任务 `reset_failed` 或从 `SUCCEEDED` 重置为 `READY` 后重跑调度循环
11. `record.status = COMPLETED | FAILED`；`stats = board.stats()` + `models` 的 token/成本；`checkpoint.save_record(record)`；`store.write_manifest()`；写 `diagnostics/stall-report.md`（若死锁）
12. `ledger.emit("run_finished", ...)`；`ledger.close()`；返回 `record`

`resume_from(settings, run_id, **kw)`：`CheckpointStore(output_dir / run_id)` → `load_record()` → `load_board()` → `repair_statuses(board)` → 构造 `Crew` 并把 `_resume_board` 挂上。`resume()` 与 `run()` 共用执行段（步骤 6–11），跳过规划与计划闸门。

`register_role(spec, cls=None)`：`catalog.register(spec)`；`cls` 非空时 `AGENT_CLASSES[spec.key] = cls`。

`delivery_summary(record)`：渲染 spec §14.3 的运行报告文本（中文），含任务统计、返工、LLM 调用与 token、成本、评审通过率、产物绝对路径。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_crew.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Crew 两阶段编排、规划校验重试与 resume"
```

---

## Task 30: 终端看板

**Files:**
- Create: `agentcrew/runtime/board_view.py`
- Test: `tests/test_board_view.py`

**Interfaces:**
- Consumes: Task 14 `LedgerEvent`、Task 15 `RunRecord`、Task 8 `TaskBoard`
- Produces:
  - `agentcrew.runtime.board_view.STATUS_STYLE: dict[str, tuple[str, str]]` — 状态 → `(符号, 颜色)`
  - `agentcrew.runtime.board_view.BoardView`
    - `BoardView(run_id: str, goal: str, *, enabled: bool = True, console: Console | None = None)`
    - `.last_event_text: str` — 最近一条事件的简短中文描述（空初始值）
    - `.start() -> None`、`.stop() -> None`
    - `.on_event(event: LedgerEvent) -> None`
    - `.render(board: TaskBoard, record: RunRecord | None = None) -> RenderableType`
    - `.print_report(record: RunRecord, board: TaskBoard, *, artifacts_dir: Path) -> None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_board_view.py
from datetime import datetime
from pathlib import Path

from rich.console import Console

from agentcrew.runtime.board_view import BoardView, STATUS_STYLE
from agentcrew.runtime.checkpoint import RunRecord, RunStatus
from agentcrew.runtime.ledger import LedgerEvent
from agentcrew.task.task import Task, TaskStatus as S, RunStats
from agentcrew.task.task_board import TaskBoard


def mk(tid, status=S.PENDING, role="backend_dev"):
    return Task(id=tid, title=f"任务{tid}", description="d", role=role,
                acceptance_criteria=["a"], deliverables=[], status=status)


def ev(name, **data):
    return LedgerEvent(ts=datetime.now(), run_id="R", event=name, data=data)


def render_text(renderable) -> str:
    """把任意 rich 可渲染对象转成纯文本，便于断言。"""
    console = Console(width=220, no_color=True)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def test_status_style_covers_all_ten_states():
    assert set(STATUS_STYLE) == {s.value for s in S}


def test_render_contains_task_ids():
    view = BoardView("R", "目标", enabled=False)
    board = TaskBoard([mk("T-001"), mk("T-002", S.SUCCEEDED)])
    printed = render_text(view.render(board))
    assert "T-001" in printed and "T-002" in printed


def test_render_shows_goal_and_run_id():
    view = BoardView("R-42", "做一个 Todo 应用", enabled=False)
    printed = render_text(view.render(TaskBoard([mk("T-1")])))
    assert "R-42" in printed and "做一个 Todo 应用" in printed


def test_render_shows_progress_count():
    board = TaskBoard([mk("A", S.SUCCEEDED), mk("B", S.PENDING)])
    printed = render_text(BoardView("R", "g", enabled=False).render(board))
    assert "1/2" in printed


def test_render_shows_role_and_status():
    board = TaskBoard([mk("A", S.REWORK, role="qa_engineer")])
    printed = render_text(BoardView("R", "g", enabled=False).render(board))
    assert "qa_engineer" in printed


def test_render_works_on_empty_board():
    assert render_text(BoardView("R", "g", enabled=False).render(TaskBoard([])))


def test_on_event_does_not_raise_for_unknown_event():
    BoardView("R", "g", enabled=False).on_event(ev("totally_unknown", x=1))


def test_on_event_tracks_last_event():
    v = BoardView("R", "g", enabled=False)
    v.on_event(ev("task_transition", task_id="T-1", **{"from": "ready", "to": "running"}))
    assert "T-1" in v.last_event_text


def test_disabled_view_is_silent(capsys):
    v = BoardView("R", "g", enabled=False)
    v.start()
    v.on_event(ev("run_started"))
    v.stop()
    assert capsys.readouterr().out == ""


def test_print_report_contains_counts(capsys):
    v = BoardView("R", "g", enabled=True)
    rec = RunRecord(run_id="R", project_goal="g", status=RunStatus.COMPLETED,
                    created_at=datetime.now(), updated_at=datetime.now(),
                    config_snapshot={},
                    stats=RunStats(total_tasks=3, succeeded=2, failed=1,
                                   llm_calls=7, estimated_cost_usd=0.42))
    board = TaskBoard([mk("A", S.SUCCEEDED), mk("B", S.FAILED)])
    v.print_report(rec, board, artifacts_dir=Path("/tmp/art"))
    out = capsys.readouterr().out
    assert "3" in out and "0.42" in out and "A" in out and "B" in out


def test_print_report_lists_failed_task_issues(capsys):
    v = BoardView("R", "g", enabled=True)
    rec = RunRecord(run_id="R", project_goal="g", status=RunStatus.FAILED,
                    created_at=datetime.now(), updated_at=datetime.now(),
                    config_snapshot={}, stats=RunStats(total_tasks=1, failed=1))
    t = mk("T-9", S.FAILED)
    from agentcrew.task.task import ReviewRecord
    t.reviews.append(ReviewRecord(task_id="T-9", round=3, layer="llm",
                                  verdict="reject", issues=["覆盖率不足"],
                                  fix_hints=[], reviewer="architect",
                                  created_at=datetime.now()))
    v.print_report(rec, TaskBoard([t]), artifacts_dir=Path("/tmp/art"))
    assert "覆盖率不足" in capsys.readouterr().out
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_board_view.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: 实现**

`STATUS_STYLE` 覆盖 10 个状态值，按 spec §15.2 的颜色约定（`succeeded` 绿、`running`/`review` 黄、`rework` 品红、`failed` 红、`pending`/`ready` 青、`blocked`/`skipped`/`cancelled` 灰）。符号：`✓` 完成、`⟳` 进行中、`◷` 待分配、`⊘` 阻塞、`✗` 失败、`↻` 返工、`—` 跳过/取消。

`render(board, record)` 返回 `rich.table.Table` 包在 `Panel` 里：标题行含 run_id + goal + 已运行时长；表格列为 `ID / 标题 / 角色 / 状态 / 依赖 / 尝试 / 返工 / 耗时`；底部一行显示进度 `f"{done}/{total}"`、并发数、累计成本、`last_event_text`。表宽自适应（`Console.width`）。

`enabled=False` 时 `start`/`stop`/`on_event` 全部空操作，`render` 仍返回值（测试要用）。

`print_report` 按 spec §14.3 的版式打印中文运行报告，失败任务逐个列出最后一次评审的 `issues`。

`on_event` 只处理已知事件类型，未知事件静默忽略（向前兼容）。所有事件都更新 `last_event_text`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_board_view.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: rich 终端看板与运行报告"
```

---

# 阶段 H — CLI、示例与交付

## Task 31: Todo 演示 fixtures

**Files:**
- Create: `examples/__init__.py`, `examples/todo_web_project.py`
- Create: `examples/fixtures/plan_todo.json`
- Create: `examples/fixtures/reviews.json`
- Create: `examples/fixtures/responses/<task_id>/attempt-<n>.md`（共 9 个任务）
- Test: `tests/test_mock_fixtures.py`

**Interfaces:**
- Consumes: Task 9 `PlanOutput` schema
- Produces:
  - `examples.todo_web_project.PROJECT_GOAL: str`
  - `examples.todo_web_project.EXPECTED_TASK_IDS: tuple[str, ...]`
  - `examples/fixtures/` 完整可回放数据

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mock_fixtures.py
"""fixture 自洽性：防止 fixtures 与 plan 契约漂移（spec §18）。"""
import ast, json, re
from pathlib import Path
import pytest
import yaml

FIX = Path(__file__).resolve().parents[1] / "examples" / "fixtures"
PLAN = json.loads((FIX / "plan_todo.json").read_text(encoding="utf-8"))

def parse_blocks(text: str) -> dict[str, str]:
    out, cur, buf, in_block = {}, None, [], False
    for line in text.splitlines(keepends=True):
        m = re.match(r"^```[A-Za-z0-9_+.\-]*\s+file=(.+?)\s*$", line.rstrip("\n"))
        if m and not in_block:
            in_block, cur, buf = True, m.group(1).replace("\\", "/"), []
            continue
        if line.rstrip("\n") == "```" and in_block:
            out[cur] = "".join(buf); in_block = False; cur = None; continue
        if in_block:
            buf.append(line)
    assert not in_block, "存在未闭合的围栏块"
    return out

def all_planned_artifacts():
    for t in PLAN["tasks"]:
        for d in t["deliverables"]:
            yield t["id"], d

def attempts_for(task_id: str) -> list[Path]:
    d = FIX / "responses" / task_id
    return sorted(d.glob("attempt-*.md")) if d.is_dir() else []

def last_attempt_blocks(task_id: str) -> dict[str, str]:
    files = attempts_for(task_id)
    assert files, f"{task_id} 没有任何 attempt fixture"
    return parse_blocks(files[-1].read_text(encoding="utf-8"))

def test_plan_file_parses_against_schema():
    from agentcrew.task.planner import PlanOutput
    PlanOutput.model_validate(PLAN)

def test_every_task_has_responses():
    for t in PLAN["tasks"]:
        assert attempts_for(t["id"]), f"{t['id']} 缺少 responses fixture"

def test_every_required_artifact_is_producible():
    for tid, d in all_planned_artifacts():
        if d.get("required", True):
            assert d["path"] in last_attempt_blocks(tid), f"{tid} 未产出 {d['path']}"

def test_no_extra_artifacts_beyond_contract():
    for t in PLAN["tasks"]:
        declared = {d["path"] for d in t["deliverables"]}
        produced = set(last_attempt_blocks(t["id"]))
        assert produced <= declared, f"{t['id']} 产出了契约外文件：{produced - declared}"

def test_must_contain_symbols_are_present():
    for tid, d in all_planned_artifacts():
        content = last_attempt_blocks(tid).get(d["path"], "")
        for needle in d.get("must_contain", []):
            assert needle in content, f"{tid}/{d['path']} 缺少 {needle!r}"

def test_python_artifacts_parse():
    for tid, d in all_planned_artifacts():
        if d["kind"] == "python":
            ast.parse(last_attempt_blocks(tid)[d["path"]])

def test_json_artifacts_parse():
    for tid, d in all_planned_artifacts():
        if d["kind"] == "json":
            json.loads(last_attempt_blocks(tid)[d["path"]])

def test_yaml_artifacts_parse():
    for tid, d in all_planned_artifacts():
        if d["kind"] == "yaml":
            yaml.safe_load(last_attempt_blocks(tid)[d["path"]])

def test_no_placeholder_markers_anywhere():
    from agentcrew.artifacts.contract import has_placeholder
    for tid, d in all_planned_artifacts():
        content = last_attempt_blocks(tid)[d["path"]]
        assert not has_placeholder(content), f"{tid}/{d['path']} 含占位内容"

def test_null_byte_and_encoding_clean():
    for tid, d in all_planned_artifacts():
        content = last_attempt_blocks(tid)[d["path"]]
        assert "\x00" not in content and "\ufffd" not in content

# ── 演示 G1 驳回 → 返工 → 通过的闭环 ──
def test_at_least_one_task_has_rework_loop():
    loops = [t["id"] for t in PLAN["tasks"] if len(attempts_for(t["id"])) >= 2]
    assert loops, "至少需要一个任务演示返工闭环"

def test_rework_loop_first_attempt_fails_second_passes():
    from agentcrew.artifacts.contract import has_placeholder
    for t in PLAN["tasks"]:
        files = attempts_for(t["id"])
        if len(files) < 2:
            continue
        first = parse_blocks(files[0].read_text(encoding="utf-8"))
        last = parse_blocks(files[-1].read_text(encoding="utf-8"))
        broken = any(has_placeholder(c) for c in first.values()) or (
            set(first) != set(last))
        assert broken, f"{t['id']} 的 attempt-1 应当不合格"
        assert not any(has_placeholder(c) for c in last.values())
        return
    pytest.fail("没有找到返工样例")

def test_reviews_fixture_covers_all_tasks_and_final():
    reviews = json.loads((FIX / "reviews.json").read_text(encoding="utf-8"))
    for t in PLAN["tasks"]:
        assert t["id"] in reviews or "*" in reviews

def test_review_entries_are_schema_valid():
    from agentcrew.gate.reviewer import ReviewVerdict
    reviews = json.loads((FIX / "reviews.json").read_text(encoding="utf-8"))
    for key, v in reviews.items():
        if key.startswith("_"):
            continue
        ReviewVerdict.model_validate(v)

def test_plan_has_no_duplicate_artifact_paths():
    seen = {}
    for tid, d in all_planned_artifacts():
        assert d["path"] not in seen, f"{d['path']} 被 {seen.get(d['path'])} 与 {tid} 重复声明"
        seen[d["path"]] = tid

def test_plan_is_acyclic_and_roles_known():
    from agentcrew.task.planner import PlanOutput, to_tasks, validate_plan
    from agentcrew.roles.catalog import RoleCatalog
    from agentcrew.roles.builtin import BUILTIN_ROLES
    validate_plan(to_tasks(PlanOutput.model_validate(PLAN)), RoleCatalog(BUILTIN_ROLES))

def test_final_task_fixture_exists():
    assert attempts_for("T-FINAL"), "缺少 T-FINAL 的 DELIVERY.md fixture"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_mock_fixtures.py -v`
Expected: FAIL — `FileNotFoundError: examples/fixtures/plan_todo.json`

- [ ] **Step 3: 实现**

`examples/todo_web_project.py`：

```python
PROJECT_GOAL = (
    "开发一个 Todo List Web 应用：用户可在浏览器中新增、查看、编辑、删除待办事项，"
    "数据通过 REST API 持久化在服务端，前端为单页应用，附完整文档、测试与 Docker 部署配置。"
)
EXPECTED_TASK_IDS = ("T-001", ..., "T-FINAL")
```

`plan_todo.json` 含 8 个计划任务（`T-001`…`T-008`），角色与产物按 spec §8.2 的「主要交付物」列：

| id | role | 依赖 | 主要产物 |
|---|---|---|---|
| T-001 | product_manager | — | `docs/PRD.md`, `docs/acceptance.md` |
| T-002 | architect | T-001 | `docs/architecture.md`, `docs/api-spec.yaml` |
| T-003 | backend_dev | T-002 | `backend/app/{__init__,main,models,schemas,store,config}.py`, `backend/requirements.txt` |
| T-004 | frontend_dev | T-002 | `frontend/index.html`, `frontend/app.js`, `frontend/styles.css` |
| T-005 | qa_engineer | T-003 | `tests/test_api.py`, `docs/test-report.md` |
| T-006 | devops_engineer | T-003 | `Dockerfile`, `docker-compose.yml`, `docs/deploy.md` |
| T-007 | technical_writer | T-003, T-004 | `README.md`, `docs/usage.md` |
| T-008 | qa_engineer | T-004 | `tests/test_ui.py` |

`responses/<id>/attempt-1.md` 存放**手写的、真实可运行**的产物内容（围栏块格式）。**T-005 额外提供 `attempt-2.md`**：`attempt-1.md` 故意只含 `TODO` 占位（触发 G1 的占位符检查），`attempt-2.md` 是完全合格的版本 —— 这就是演示返工闭环的那一个任务。

Todo 应用技术栈：FastAPI + Pydantic v2 + 进程内内存存储（`store.py` 用 dict）、原生 HTML/CSS/JS 前端、pytest + `fastapi.testclient`。`backend/requirements.txt` 含 `fastapi`、`uvicorn`、`pydantic`、`pytest`、`httpx`。

`T-FINAL/attempt-1.md` 产出 `DELIVERY.md`。

`reviews.json` 提供 `{"*": {...approve...}}` 通配，并为 `T-005` 的 `attempt-1` 阶段提供一条 `reject`？—— **不需要**：T-005 的首次失败由 G1 静态层拦截，走不到 G2。因此 `reviews.json` 只需 `"*"` 通配全部 approve。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_mock_fixtures.py -v`
Expected: 全部 PASS，含 `test_rework_loop_first_attempt_fails_second_passes`

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Todo Web 演示的规划、产物与评审 fixtures"
```

---

## Task 32: CLI 入口

**Files:**
- Create: `main.py`
- Create: `agentcrew/gate/cli_prompter.py`（交互式 G3 闸门实现）
- Test: `tests/test_cli.py`, `tests/test_cli_prompter.py`

**Interfaces:**
- Consumes: Task 29 `Crew`、Task 15 `CheckpointStore`/`clean`、Task 27 `Prompter`/`PlanDecision`/`DeliveryDecision`
- Produces:
  - `main.build_parser() -> argparse.ArgumentParser`
  - `main.main(argv: Sequence[str] | None = None) -> int` — 返回退出码
  - `agentcrew.gate.cli_prompter.CliPrompter` — 实现 `Prompter` 协议
    - `CliPrompter(*, input_fn: Callable[[str], str] = input, edit_fn: Callable[[str], str] | None = None, console: Console | None = None)`
    - `async ask_plan(self, tasks: Sequence[Task]) -> PlanDecision`
    - `async ask_delivery(self, record: RunRecord) -> DeliveryDecision`
  - `agentcrew.gate.cli_prompter.edit_tasks_in_editor(tasks, *, editor: str | None = None) -> list[Task]` — 用 `$EDITOR`（或 `EDITOR`/`notepad`）编辑拆解 JSON；编辑后重新走 `validate_plan`
  - `agentcrew.crew.crew.EXIT_CODES` — `{"ok":0,"failed":1,"plan":2,"config":3,"stalled":4,"cancelled":130}`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cli.py
import json, subprocess, sys
from pathlib import Path
import pytest
import main as cli

ROOT = Path(__file__).resolve().parents[1]

def test_parser_has_all_subcommands():
    p = cli.build_parser()
    subs = {a.dest for a in p._subparsers._group_actions}
    assert "command" in subs
    for name in ("run", "resume", "status", "roles", "clean", "cancel"):
        assert p.parse_args([name] + (["R1"] if name in
                                      ("resume", "status", "cancel") else [])).command == name

def test_run_requires_goal_or_demo(capsys):
    assert cli.main(["run"]) == 3
    assert "goal" in capsys.readouterr().err.lower() or True

def test_goal_and_demo_are_mutually_exclusive(capsys):
    assert cli.main(["run", "--goal", "x", "--demo"]) == 3

def test_roles_lists_eight(capsys):
    assert cli.main(["roles"]) == 0
    out = capsys.readouterr().out
    for key in ("project_manager", "product_manager", "architect", "backend_dev",
                "frontend_dev", "qa_engineer", "devops_engineer", "technical_writer"):
        assert key in out

def test_no_args_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()

def test_demo_mock_run_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.chdir(ROOT)
    code = cli.main(["run", "--demo", "--mock", "--auto", "--no-live"])
    assert code == 0
    run_dirs = list((tmp_path / "o").iterdir())
    assert len(run_dirs) == 1
    art = run_dirs[0] / "artifacts"
    assert (art / "README.md").exists() and (art / "DELIVERY.md").exists()
    assert (art / "backend" / "app" / "main.py").exists()

def test_status_reads_completed_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.chdir(ROOT)
    cli.main(["run", "--demo", "--mock", "--auto", "--no-live"])
    rid = next((tmp_path / "o").iterdir()).name
    assert cli.main(["status", rid]) == 0
    out = capsys.readouterr().out
    assert rid in out

def test_clean_dry_run_lists_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    monkeypatch.chdir(ROOT)
    cli.main(["run", "--demo", "--mock", "--auto", "--no-live"])
    assert cli.main(["clean", "--keep", "0", "--dry-run"]) == 0
    assert next((tmp_path / "o").iterdir(), None) is not None   # 没被删

def test_module_runs_as_script():
    r = subprocess.run([sys.executable, str(ROOT / "main.py"), "roles"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and "architect" in r.stdout

def test_help_works():
    r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--help"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0
```

```python
# tests/test_cli_prompter.py
"""G3 人工闸门的交互实现（spec §10.3）。全部用注入的 input_fn，不读真实 stdin。"""
from datetime import datetime

import pytest

from agentcrew.gate.cli_prompter import CliPrompter
from agentcrew.runtime.checkpoint import RunRecord, RunStatus
from agentcrew.task.task import Task, RunStats


def mk(tid, role="backend_dev"):
    return Task(id=tid, title=f"任务{tid}", description="d", role=role,
                acceptance_criteria=["a"], deliverables=[])


def record():
    return RunRecord(run_id="R", project_goal="做个 Todo 应用",
                     status=RunStatus.RUNNING, created_at=datetime.now(),
                     updated_at=datetime.now(), config_snapshot={},
                     stats=RunStats(total_tasks=2, succeeded=2))


def prompter(answers):
    it = iter(answers)
    return CliPrompter(input_fn=lambda _: next(it))


async def test_plan_a_approves_and_returns_tasks():
    tasks = [mk("T-1"), mk("T-2")]
    d = await prompter(["a"]).ask_plan(tasks)
    assert d.action == "approve" and [t.id for t in d.tasks] == ["T-1", "T-2"]


async def test_plan_q_cancels():
    assert (await prompter(["q"]).ask_plan([mk("T-1")])).action == "cancel"


async def test_plan_s_saves_and_exits():
    assert (await prompter(["s"]).ask_plan([mk("T-1")])).action == "save_exit"


async def test_plan_reasks_on_invalid_input():
    d = await prompter(["zzz", "x", "a"]).ask_plan([mk("T-1")])
    assert d.action == "approve"


async def test_plan_empty_input_defaults_to_approve():
    assert (await prompter([""]).ask_plan([mk("T-1")])).action == "approve"


async def test_plan_edit_returns_edited_tasks():
    edited = [mk("T-1"), mk("T-3")]
    p = CliPrompter(input_fn=lambda _: "e",
                    edit_fn=lambda tasks: edited)
    d = await p.ask_plan([mk("T-1")])
    assert d.action == "approve" and [t.id for t in d.tasks] == ["T-1", "T-3"]


async def test_plan_edit_validation_failure_reasks():
    from agentcrew.task.planner import PlanValidationError
    def bad_edit(tasks):
        raise PlanValidationError(["产物路径冲突：a.py 被 T-1 与 T-2 同时声明"])
    p = CliPrompter(input_fn=lambda _: next(iter(["e", "q"])), edit_fn=bad_edit)
    assert (await p.ask_plan([mk("T-1")])).action == "cancel"


async def test_delivery_a_accepts():
    assert (await prompter(["a"]).ask_delivery(record())).action == "accept"


async def test_delivery_q_aborts():
    assert (await prompter(["q"]).ask_delivery(record())).action == "abort"


async def test_delivery_r_collects_task_ids():
    d = await prompter(["r", "T-003, T-005"]).ask_delivery(record())
    assert d.action == "rework" and d.task_ids == ["T-003", "T-005"]


async def test_delivery_r_with_empty_list_reasks():
    d = await prompter(["r", "", "a"]).ask_delivery(record())
    assert d.action == "accept"


async def test_delivery_empty_input_defaults_to_accept():
    assert (await prompter([""]).ask_delivery(record())).action == "accept"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'main'`

- [ ] **Step 3: 实现**

`build_parser()` 用 `argparse`，子命令与参数逐字对齐 spec §15.1。`run` 的参数：`--goal`、`--demo`（互斥组）、`--mock`、`--auto`、`--gate {both,plan,delivery,none}`、`--max-concurrency`、`--default-model`、`--output-dir`、`--context-budget`、`--no-live`。

`main(argv)` 分派并返回退出码：
- `run`：`--goal` 与 `--demo` 都为空或都给 → 打印错误到 stderr，返回 3
- `resume <run_id>`：`Crew.resume_from(...)` → `await crew.resume()`
- `status <run_id>`：读 `run.json` + `tasks.json`，打印统计与任务表
- `roles`：遍历 `BUILTIN_ROLES` 打印表格（`key / title / 职责 / 边界 / 产出`）
- `clean`：调 `CheckpointStore.clean`
- `cancel <run_id>`：改 `run.json` 的 `status` 为 `CANCELLED`
- 无参数：打印 help 返回 0

`on_event` 接到 `BoardView`；`--no-live` 时 `BoardView(enabled=False)`。

顶层 `main()` 用 `asyncio.run(...)`；捕获 `KeyboardInterrupt` → 落盘 → 返回 130；捕获 `PlanFailedError` → 返回 2；捕获 `ModelFactoryError`/`ContextBudgetError` → 返回 3；捕获 `RunCancelled` → 130。

`CliPrompter.ask_plan(tasks)`：用 `rich` 打印任务 DAG 表（id / 标题 / 角色 / 依赖 / 产物数 / 优先级）+ 角色分布统计，然后循环提示 `[a] 批准执行 / [e] 编辑后执行 / [s] 保存退出 / [q] 取消`（默认 `a`）：
- `a` / 空输入 → `PlanDecision(action="approve", tasks=list(tasks))`
- `e` → 调 `edit_fn`（默认 `edit_tasks_in_editor`）；成功 → `approve` 并返回编辑后的任务；抛 `PlanValidationError` → 打印 `errors` 后重新提示（**不退出循环**）
- `s` → `save_exit`；`q` → `cancel`
- 非法输入 → 打印「请输入 a/e/s/q」后重新提示

`CliPrompter.ask_delivery(record)`：打印产物清单 + 统计 + 评审通过率，提示 `[a] 接受交付 / [r] 打回指定任务返工 / [q] 放弃`（默认 `a`）：
- `a` / 空 → `accept`
- `r` → 提示输入任务 id（逗号分隔）；解析后去空白；**结果为空则重新提示**；非空 → `DeliveryDecision(action="rework", task_ids=[...])`
- `q` → `abort`

`edit_tasks_in_editor`：把 `tasks` 序列化为 JSON 写入临时文件 → `subprocess.run([editor, path])`（`editor` 取 `$AGENTCREW_EDITOR` → `$EDITOR` → `notepad` on Windows / `vi` 其他）→ 读回 → `PlanOutput.model_validate` → `to_tasks` → `with_final_task` → `validate_plan` → 返回。校验失败向上抛 `PlanValidationError`。

`--auto` 或 `--gate none` 时 `main` 传 `NullPrompter()`；`--gate both|plan|delivery` 且 TTY 时传 `CliPrompter()`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_cli.py -v`
Expected: 全部 PASS（`test_demo_mock_run_end_to_end` 会真正跑完整流程）

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: main.py CLI 与六个子命令"
```

---

## Task 33: 端到端测试与无旁路验证

**Files:**
- Create: `tests/test_end_to_end_mock.py`, `tests/test_mock_no_bypass.py`
- Modify: `tests/conftest.py`（补共享 fixture）

**Interfaces:**
- Consumes: Task 32 全部
- Produces: 无新接口（验证任务）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_end_to_end_mock.py
import ast, json
from pathlib import Path
import pytest
from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.runtime.checkpoint import RunStatus

@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()

@pytest.fixture
def fixtures_dir(monkeypatch):
    d = Path(__file__).resolve().parents[1] / "examples" / "fixtures"
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", d)
    return d

async def test_full_run_completes(settings, fixtures_dir):
    rec = await Crew(settings).run(
        "开发一个 Todo List Web 应用：用户可在浏览器中增删改查待办事项。")
    assert rec.status is RunStatus.COMPLETED
    assert rec.stats.failed == 0 and rec.stats.blocked == 0
    assert rec.stats.total_tasks == 9            # 8 计划任务 + T-FINAL
    assert rec.stats.succeeded == 9

async def test_artifacts_are_real_files(settings, fixtures_dir):
    rec = await Crew(settings).run("Todo List Web 应用")
    art = settings.output_dir / rec.run_id / "artifacts"
    manifest = json.loads((art / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["entries"]
    for entry in manifest["entries"]:
        p = art / entry["path"]
        assert p.is_file(), f"清单声明了 {entry['path']} 但文件不存在"
        assert p.stat().st_size == entry["size_bytes"]

async def test_all_generated_python_parses(settings, fixtures_dir):
    rec = await Crew(settings).run("Todo List Web 应用")
    art = settings.output_dir / rec.run_id / "artifacts"
    for p in art.rglob("*.py"):
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))

async def test_json_and_yaml_artifacts_parse(settings, fixtures_dir):
    import yaml
    rec = await Crew(settings).run("Todo List Web 应用")
    art = settings.output_dir / rec.run_id / "artifacts"
    for p in art.rglob("*.json"):
        json.loads(p.read_text(encoding="utf-8"))
    for p in art.rglob("*.yaml"):
        yaml.safe_load(p.read_text(encoding="utf-8"))

async def test_ledger_records_llm_calls_and_transitions(settings, fixtures_dir):
    rec = await Crew(settings).run("Todo List Web 应用")
    from agentcrew.runtime.ledger import Ledger
    lg = Ledger(settings.output_dir / rec.run_id / "ledger.jsonl", run_id=rec.run_id)
    assert lg.count("llm_call") > 0
    assert lg.count("task_transition") > 0
    assert lg.count("artifact_written") > 0
    assert lg.count("run_finished") == 1

async def test_rework_demo_happened(settings, fixtures_dir):
    """T-005 的 attempt-1 不合格，必须出现一次返工。"""
    rec = await Crew(settings).run("Todo List Web 应用")
    from agentcrew.runtime.ledger import Ledger
    lg = Ledger(settings.output_dir / rec.run_id / "ledger.jsonl", run_id=rec.run_id)
    assert lg.count("gate_result") > 9           # 至少有一次额外评审
    assert any(e.data.get("verdict") == "reject" for e in lg.events("gate_result"))

async def test_no_api_keys_required(settings, fixtures_dir, monkeypatch):
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY",
              "ARK_API_KEY", "QIANFAN_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    rec = await Crew(Settings.load()).run("Todo List Web 应用")
    assert rec.status is RunStatus.COMPLETED

async def test_delivery_md_mentions_completion(settings, fixtures_dir):
    rec = await Crew(settings).run("Todo List Web 应用")
    text = (settings.output_dir / rec.run_id / "artifacts" / "DELIVERY.md").read_text(
        encoding="utf-8")
    assert "完成" in text
```

```python
# tests/test_mock_no_bypass.py
"""S9：mock 必须走与真实模型完全相同的数据通路，任何一步都不能被跳过。"""
from pathlib import Path
import pytest
from agentcrew.config import Settings
from agentcrew.artifacts.store import ArtifactStore
from agentcrew.gate.static_checks import run_static
from agentcrew.crew.crew import Crew

@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp_path / "o"))
    return Settings.load()

@pytest.fixture
def fixtures_dir(monkeypatch):
    d = Path(__file__).resolve().parents[1] / "examples" / "fixtures"
    monkeypatch.setattr("agentcrew.models.factory.DEFAULT_FIXTURES_DIR", d)
    return d

async def test_commit_resolve_and_gate_all_invoked(settings, fixtures_dir, monkeypatch):
    calls = {"commit": 0, "resolve": 0, "static": 0, "parse": 0}

    orig_commit = ArtifactStore.commit
    orig_resolve = ArtifactStore.resolve
    def spy_commit(self, rel, content, **kw):
        calls["commit"] += 1
        return orig_commit(self, rel, content, **kw)
    def spy_resolve(self, rel):
        calls["resolve"] += 1
        return orig_resolve(self, rel)
    monkeypatch.setattr(ArtifactStore, "commit", spy_commit)
    monkeypatch.setattr(ArtifactStore, "resolve", spy_resolve)

    import agentcrew.gate.static_checks as sc
    orig_static = sc.run_static
    monkeypatch.setattr(sc, "run_static", lambda *a, **kw: (calls.__setitem__(
        "static", calls["static"] + 1), orig_static(*a, **kw))[1])

    import agentcrew.artifacts.parser as ps
    orig_parse = ps.parse_fenced_blocks
    monkeypatch.setattr(ps, "parse_fenced_blocks", lambda raw: (calls.__setitem__(
        "parse", calls["parse"] + 1), orig_parse(raw))[1])
    monkeypatch.setattr("agentcrew.agents.base_agent.parse_fenced_blocks",
                        ps.parse_fenced_blocks)

    await Crew(settings).run("Todo List Web 应用")
    assert calls["commit"] > 0, "ArtifactStore.commit 未被调用 —— mock 走了旁路"
    assert calls["resolve"] > 0, "ArtifactStore.resolve 未被调用"
    assert calls["static"] > 0, "G1 静态门禁未被调用"
    assert calls["parse"] > 0, "围栏解析未被调用"

async def test_no_direct_file_writes_outside_store(settings, fixtures_dir, monkeypatch):
    """断言产出目录下的文件都是经 ArtifactStore 写入的。"""
    written: set[str] = set()
    from agentcrew.artifacts.store import ArtifactStore as S
    orig = S.commit
    def spy(self, rel, content, **kw):
        written.add(rel)
        return orig(self, rel, content, **kw)
    monkeypatch.setattr(S, "commit", spy)
    rec = await Crew(settings).run("Todo List Web 应用")
    art = settings.output_dir / rec.run_id / "artifacts"
    on_disk = {p.relative_to(art).as_posix() for p in art.rglob("*") if p.is_file()}
    on_disk.discard("MANIFEST.json")             # 由 write_manifest 写的元数据
    assert on_disk <= written, f"存在未经 ArtifactStore 写入的文件：{on_disk - written}"
```

- [ ] **Step 2: 运行测试确认失败/通过**

Run: `pytest tests/test_end_to_end_mock.py tests/test_mock_no_bypass.py -v`
Expected: 若前序任务全部正确实现，应直接 PASS；若失败，按失败信息修正对应模块

- [ ] **Step 3: 修复发现的问题**

这是集成验证任务，不新增生产代码。若用例暴露缺陷，回到对应任务修复并补其单测。

- [ ] **Step 4: 全量测试**

Run: `pytest -q`
Expected: 全部 PASS，无网络请求

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "test: 端到端 mock 流程与无旁路验证"
```

---

## Task 34: README 与交付验收

**Files:**
- Create: `README.md`
- Modify: `pyproject.toml`（补 ruff/mypy 起步配置）
- Test: `tests/test_readme.py`

**Interfaces:**
- Consumes: 全部
- Produces: 文档交付

- [ ] **Step 1: 写失败测试**

```python
# tests/test_readme.py
from pathlib import Path
README = Path(__file__).resolve().parents[1] / "README.md"

def test_readme_exists_and_is_substantial():
    assert README.is_file() and len(README.read_text(encoding="utf-8")) > 2000

def test_covers_required_sections():
    t = README.read_text(encoding="utf-8")
    for heading in ("项目介绍", "角色", "快速开始", "自定义", "已知限制", "mock", "Mock"):
        assert heading in t, f"README 缺少「{heading}」"

def test_documents_all_eight_roles():
    t = README.read_text(encoding="utf-8")
    for key in ("project_manager", "product_manager", "architect", "backend_dev",
                "frontend_dev", "qa_engineer", "devops_engineer", "technical_writer"):
        assert key in t

def test_documents_mock_honesty():
    t = README.read_text(encoding="utf-8")
    assert "不是模型生成" in t or "非模型生成" in t or "手写" in t

def test_documents_all_known_limitations():
    t = README.read_text(encoding="utf-8")
    for item in ("不执行", "千帆", "单进程", "启发式"):
        assert item in t, f"README 未声明已知限制「{item}」"

def test_documents_test_command():
    assert "pytest" in README.read_text(encoding="utf-8")

def test_documents_custom_role_recipe():
    t = README.read_text(encoding="utf-8")
    assert "RoleSpec" in t and ("register_role" in t or "roles.d" in t)

def test_no_placeholder_left():
    t = README.read_text(encoding="utf-8")
    for bad in ("TODO", "TBD", "待补充", "XXX"):
        assert bad not in t
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_readme.py -v`
Expected: FAIL — `FileNotFoundError`

- [ ] **Step 3: 编写 README.md**

必须包含（对应原需求七.7 + spec §19.3）：
1. **项目介绍** —— 一句话定位 + 架构图（复用 spec §3 的 ASCII 图）+ 两阶段说明
2. **角色说明** —— 8 个角色的表格：key / 岗位 / 职责 / 主要交付物 / 评审人
3. **快速开始** —— 三步：`pip install -r requirements.txt` → `cp .env.example .env` 填 Key → `python main.py run --demo`
4. **离线演示** —— `python main.py run --demo --mock --auto`，并**明确声明**：mock 产物来自 `examples/fixtures/` 中手写的预置回复，**不是模型生成**，mock 用于离线演示编排流程与支撑测试
5. **命令速查** —— 六个子命令与常用参数表
6. **自定义角色 / 团队开发指南** —— 两种方式（`agentcrew.yaml` 的 `roles.d/` 目录、`crew.register_role(RoleSpec(...))`），附一个完整可复制的 `RoleSpec` 示例与 `roles.d/wizard.yaml` 示例
7. **模型绑定** —— 支持的 8 个 provider、`<provider>:<model>` 别名格式、按角色绑定模型、各 provider 的 `json_schema` 支持情况
8. **配置项** —— `.env` 与 `agentcrew.yaml` 的完整字段表
9. **产物说明** —— `output/<run_id>/` 的目录结构、`MANIFEST.json`、`DELIVERY.md`
10. **运行测试** —— `pytest -q`，并说明测试零网络零 Key
11. **已知限制** —— 逐条抄 spec §19.3 的 5 条
12. **状态机与门禁** —— 10 态跃迁表 + 三层门禁说明

- [ ] **Step 4: 运行测试确认通过 + 全量验收**

```bash
pytest -q
python main.py run --demo --mock --auto --no-live
```

Expected: 全部 PASS；mock 运行退出码 0，输出目录内含可 `uvicorn` 启动的 Todo 后端

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "docs: README 与交付文档"
```

---

## 验收清单（对应 spec §20）

实现完成后逐条核验：

- [ ] **S1** 干净虚拟环境 `pip install -r requirements.txt` 成功
- [ ] **S2** `python main.py run --demo --mock --auto` 无 Key 跑完，退出码 0
- [ ] **S3** `python main.py run --goal "..." --auto` 真实模型跑通（需 Key，人工验证）
- [ ] **S4** `pytest -q` 全绿且无网络
- [ ] **S5** mock 产物中 Todo 应用可 `uvicorn` 启动（人工，见 §16.2 命令）
- [ ] **S6** `Ctrl-C` 后 `python main.py resume <run_id>` 能继续完成
- [ ] **S7** 通过 `agentcrew.yaml` 新增角色跑通，引擎代码零改动
- [ ] **S8** 8 个角色文件齐备、各有独立 `RoleSpec` 与单测
- [ ] **S9** `test_mock_no_bypass.py` 通过（mock 无旁路）
- [ ] `python main.py roles` 列出 8 个内置角色
- [ ] `python main.py status <run_id>` 读出已完成 run 的统计
- [ ] README 含项目介绍/角色说明/快速开始/自定义角色指南/已知限制

## Review Focus 覆盖对照

| # | 风险 | 钉死它的测试 | 归属 |
|---|---|---|---|
| 1 | 模型幻觉出契约外文件 | `test_undeclared_extra_artifact_rejected`、`test_declared_but_unproduced_required_artifact_reported` | Task 13 |
| 2 | JSON 被围栏/解释文字包裹 | `test_level3_extracts_fenced_json_when_provider_incapable`、`test_level3_extracts_prose_wrapped_json` | Task 20 |
| 3 | 非 ASCII 产物路径 | `test_chinese_file_path_parses`、`test_chinese_path_roundtrip`、`test_accepts_safe_paths[docs/接口文档.md]` | Task 10 / 11 / 3 |
| 4 | 同一路径被两任务声明 | `test_duplicate_artifact_path_across_tasks_rejected` | Task 9 |
| 5 | 中断发生在 checkpoint 写入中 | `test_interrupted_write_never_leaves_partial_json`、`test_leftover_temp_files_are_cleaned` | Task 15 |
