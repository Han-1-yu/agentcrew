"""架构守卫：包依赖方向与对外再导出（静态检查，不执行被测代码）。

为什么要有这个文件：分层一旦只写在文档里，就会在「赶一个功能」的那次提交里
悄悄反向 —— 基础设施层 import 编排层一行代码看不出问题，直到想单独测试模型层
却发现必须先把整个运行时拉起来。这里把方向钉成断言。

做法：用 ``ast`` 读源码、抽出 ``agentcrew`` 内部 import，**不 import 被测模块**。
因此本文件跑得很快、不受第三方依赖与副作用影响，也不会因为某层正在重构而连
测试都收集不起来。

规则表（``FORBIDDEN``）是「本包**不得**依赖的包」，而不是白名单：新增合法依赖
不需要改这里，新增**反向**依赖会立刻失败。每个例外都在注释里写清理由。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "agentcrew"

#: 包 → 禁止依赖的包集合。``*`` 表示「除自身外一律禁止」。
FORBIDDEN: dict[str, set[str]] = {
    # 跨层契约与纯工具：必须零项目内依赖，否则谁都可以「顺手」往里加实现。
    "contracts": {"*"},
    "utils": {"*"},
    # 模型层是基础设施：解析别名、调 Provider、记账。它不该知道「run」「任务」
    # 「调度」长什么样 —— 早先它 import runtime.ledger 与 task.task，导致模型层
    # 无法脱离编排层测试（本次重构拔掉的正是这两条）。
    "models": {
        "runtime", "task", "crew", "api", "cli", "gate",
        "agents", "context", "tools", "mcp", "knowledge",
    },
    # 产物仓库只依赖路径安全工具与契约；按 TaskLike 协议消费任务字段。
    "artifacts": {
        "task", "models", "runtime", "crew", "api", "cli", "gate",
        "agents", "context", "tools", "mcp", "knowledge",
    },
    # 任务领域：只允许 artifacts（产物契约）、roles（角色 key）、utils。
    "task": {
        "crew", "api", "cli", "gate", "agents", "models",
        "context", "runtime", "tools", "mcp", "knowledge",
    },
    # 运行时（ledger / checkpoint / 看板）：允许 task、utils。
    "runtime": {
        "crew", "api", "cli", "gate", "agents", "models",
        "context", "artifacts", "tools", "mcp", "knowledge",
    },
    # 上下文装配：允许 artifacts、models（token 估算）、task、utils。
    "context": {
        "crew", "api", "cli", "gate", "agents", "runtime",
        "tools", "mcp", "knowledge",
    },
    # 角色层：``spec`` / ``catalog`` 是纯数据，不依赖任何实现包；``builtin`` 是
    # 唯一被允许的聚合点（见文件末尾的模块级断言）。
    "roles": {
        "crew", "api", "cli", "gate", "runtime", "models",
        "context", "tools", "mcp", "knowledge",
    },
    # Agent 层：可以依赖模型/产物/任务/角色/运行时/工具，但不得依赖编排与出口。
    "agents": {"crew", "api", "cli", "knowledge"},
    # 门禁层：不得反向依赖执行引擎与出口。
    "gate": {"crew", "api", "cli"},
    # 编排层是最上层，只允许不依赖任何出口（api / cli 由它们依赖 crew）。
    "crew": {"api", "cli"},
    "api": {"cli"},
    "cli": {"api"},
    "mcp": {"crew", "api", "cli", "gate", "agents", "models", "runtime", "task"},
    "tools": {"crew", "api", "cli", "gate", "agents", "runtime"},
    "knowledge": {"crew", "api", "cli", "gate", "agents", "runtime", "models"},
}


def _iter_modules() -> list[Path]:
    return [
        path
        for path in PACKAGE.rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_packages(path: Path) -> set[str]:
    """源码里出现的 ``agentcrew.<pkg>`` 依赖集合（包粒度）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    module = _module_name(path)
    owner = module.split(".")[1] if "." in module else ""
    found: set[str] = set()
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.ImportFrom):
            if node.level:
                # 相对导入：按层级回退到绝对模块名
                parts = module.split(".")
                base = ".".join(parts[: len(parts) - node.level])
                targets = [f"{base}.{node.module}" if node.module else base]
            elif node.module:
                targets = [node.module]
        elif isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        for target in targets:
            if not target.startswith("agentcrew."):
                continue
            parts = target.split(".")
            if len(parts) > 1:
                pkg = parts[1]
                if pkg != owner:
                    found.add(pkg)
    return found


def test_package_dependencies_follow_layering() -> None:
    """每个包都不得依赖其 ``FORBIDDEN`` 里的包。"""
    violations: list[str] = []
    for path in _iter_modules():
        module = _module_name(path)
        parts = module.split(".")
        if len(parts) < 2:
            continue  # agentcrew/contracts.py 这类单文件，另有专门断言
        pkg = parts[1]
        banned = FORBIDDEN.get(pkg)
        if banned is None:
            continue
        imported = _imported_packages(path)
        forbidden = imported if "*" in banned else imported & banned
        if forbidden:
            violations.append(
                f"{path.relative_to(ROOT).as_posix()} 依赖了 {sorted(forbidden)}"
            )
    assert not violations, "包依赖方向被破坏：\n" + "\n".join(sorted(violations))


def test_contracts_module_is_dependency_free() -> None:
    """``agentcrew/contracts.py`` 只描述形状，不得 import 项目内任何模块。

    它被 models / artifacts / tools 共同依赖，一旦它开始 import 实现，所有下层
    包就通过它重新绑在一起，「零依赖契约」名存实亡。
    """
    path = PACKAGE / "contracts.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    offenders = [
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("agentcrew")
    ]
    offenders += [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.startswith("agentcrew")
    ]
    assert not offenders, f"contracts.py 不得依赖项目内模块：{offenders}"


def test_role_data_layer_does_not_import_agents() -> None:
    """``roles.spec`` / ``roles.catalog`` 是纯数据层，不得 import agents。

    ``roles/builtin.py`` 是**唯一**被允许的聚合点：它把 8 个 ``agents/<key>.py``
    里声明的 ``RoleSpec`` 汇总成 ``BUILTIN_ROLES``，本质是装配而不是依赖。
    """
    for name in ("spec.py", "catalog.py"):
        path = PACKAGE / "roles" / name
        imported = _imported_packages(path)
        assert "agents" not in imported, (
            f"roles/{name} 不得依赖 agents 层（角色是数据，Agent 是实现）"
        )


def test_models_layer_has_no_orchestration_dependency() -> None:
    """模型层不得依赖运行时编排与任务领域（本次重构的回归守卫）。

    单独再断言一遍，是因为这条最容易被「反正 factory 已经 import 一堆东西了」
    重新打破；上面的总规则失败信息以包为单位，这条给出更明确的意图。
    """
    offenders: list[str] = []
    for path in (PACKAGE / "models").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        imported = _imported_packages(path)
        bad = imported & {"runtime", "task", "crew", "api", "cli", "gate", "agents"}
        if bad:
            offenders.append(f"{path.name} -> {sorted(bad)}")
    assert not offenders, "models 层反向依赖：\n" + "\n".join(sorted(offenders))


def test_artifact_store_consumes_task_by_protocol() -> None:
    """产物仓库通过 ``TaskLike`` 协议消费任务字段，不 import 任务领域模型。"""
    path = PACKAGE / "artifacts" / "store.py"
    source = path.read_text(encoding="utf-8")
    assert "from agentcrew.task" not in source
    assert "TaskLike" in source


def test_only_entry_layers_import_agents() -> None:
    """只有 crew / api / cli 与 ``roles.builtin`` 可以 import agents 层。

    Agent 是「唯一持有 LLM 的地方」，被别处随手 import 会让「谁在调模型」这件事
    失去唯一边界。
    """
    allowed = {"crew", "api", "cli"}
    offenders: list[str] = []
    for path in _iter_modules():
        module = _module_name(path)
        parts = module.split(".")
        if len(parts) < 2 or parts[1] in allowed:
            continue
        if module == "agentcrew.roles.builtin":
            continue  # 角色聚合点，见 test_role_data_layer_does_not_import_agents
        if "agents" in _imported_packages(path):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, "以下模块越界 import 了 agents 层：\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    ("module", "symbol"),
    [
        # 重构后模块被拆分，这些符号必须继续从原位置可取（对外接口兼容）。
        ("agentcrew.models.factory", "ModelFactory"),
        ("agentcrew.models.factory", "ModelFactoryError"),
        ("agentcrew.models.factory", "ContextBudgetError"),
        ("agentcrew.models.factory", "BudgetExhaustedError"),
        ("agentcrew.models.factory", "ProviderUnavailableError"),
        ("agentcrew.models.factory", "HealthRecord"),
        ("agentcrew.models.factory", "WeightedAlias"),
        ("agentcrew.models.factory", "DEFAULT_FIXTURES_DIR"),
        ("agentcrew.agents.base_agent", "BaseAgent"),
        ("agentcrew.agents.base_agent", "AgentExecutionError"),
        ("agentcrew.api.server", "ApiServer"),
        ("agentcrew.api.server", "EventBus"),
        ("agentcrew.api.server", "RunState"),
        ("agentcrew.crew.crew", "Crew"),
        ("agentcrew.crew.crew", "EXIT_CODES"),
        ("agentcrew.artifacts.store", "ArtifactStore"),
        ("agentcrew.artifacts.store", "LedgerLike"),
        ("agentcrew.tools.registry", "ToolRegistry"),
        ("agentcrew.tools.registry", "LedgerLike"),
    ],
)
def test_public_symbols_remain_importable(module: str, symbol: str) -> None:
    """拆分实现时不得改变对外 import 位置（兼容层的存在意义）。"""
    imported = __import__(module, fromlist=[symbol])
    assert hasattr(imported, symbol), f"{module} 不再导出 {symbol}"


def test_private_helpers_remain_available() -> None:
    """测试与内部调用点仍在用的私有辅助名，重构后必须保留（再导出即可）。"""
    from agentcrew.agents import base_agent
    from agentcrew.crew import crew as crew_module

    for name in (
        "_render_pack",
        "_render_deliverables",
        "_is_truncated",
        "_language_notes",
        "_canonical_path",
        "_truncate_summary",
        "_join_notes",
        "_fence_languages",
    ):
        assert hasattr(base_agent, name), f"base_agent 缺少 {name}"
    for name in (
        "_format_tokens",
        "_format_duration",
        "_rework_line",
        "_review_line",
        "_elapsed_line",
        "_failure_line",
    ):
        assert hasattr(crew_module, name), f"crew 缺少 {name}"
