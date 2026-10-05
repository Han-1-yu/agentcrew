"""Task 34：README 交付验收（spec §20 七.7 / §19.3）。

只验证「该说的都说了、说的事都对得上」——锚点词而非全文措辞，避免改一句话
就要改测试；但每个锚点都对应一条硬性交付要求：

- 12 个章节齐备（项目介绍 / 角色说明 / 快速开始 / 离线演示 / 命令速查 /
  自定义角色 / 模型绑定 / 配置项 / 产物说明 / 运行测试 / 已知限制 / 状态机与门禁）；
- 8 个内置角色 key 与 8 个 provider 别名全部出现；
- 六个 CLI 子命令全部以 ``main.py <命令>`` 的形式出现；
- §19.3 的五条已知限制逐条声明（含 mock 产物非模型生成）；
- 运行测试用 ``python -m pytest``（bare ``pytest`` 不在 PATH 上）；
- 不留任何占位文本。
"""

import ast
from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[1] / "README.md"

#: Task 34 brief 要求的 12 个章节。
REQUIRED_SECTIONS = (
    "项目介绍",
    "角色说明",
    "快速开始",
    "离线演示",
    "命令速查",
    "自定义角色",
    "模型绑定",
    "配置项",
    "产物说明",
    "运行测试",
    "已知限制",
    "状态机与门禁",
)

#: 8 个内置角色的 key（``agentcrew/roles/builtin.py::BUILTIN_ROLES``）。
BUILTIN_ROLE_KEYS = (
    "project_manager",
    "product_manager",
    "architect",
    "backend_dev",
    "frontend_dev",
    "qa_engineer",
    "devops_engineer",
    "technical_writer",
)

#: 8 个 provider 别名（``agentcrew/models/config.py::PROVIDER_PRESETS``）。
PROVIDER_KEYS = (
    "openai",
    "anthropic",
    "deepseek",
    "doubao",
    "qwen",
    "ernie",
    "ollama",
    "mock",
)

#: 六个 CLI 子命令（``main.py::build_parser``）。
SUBCOMMANDS = ("run", "resume", "status", "roles", "clean", "cancel")

#: spec §19.3 的五条已知限制 → 每条必须命中的锚点词。
KNOWN_LIMITATIONS = {
    "不执行生成的代码，G1 仅静态校验": ("不执行", "静态"),
    "token 估算为启发式": ("启发式",),
    "文心一言仅支持千帆 v2": ("千帆",),
    "单进程、无分布式执行": ("单进程",),
    "mock 产物非模型生成": ("手写", "不是模型生成"),
}

#: 不允许出现在交付文档里的占位文本。
PLACEHOLDERS = ("TODO", "TBD", "待补充", "XXX")


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_exists_and_is_substantial():
    assert README.is_file(), "README.md 不存在"
    assert len(_readme()) > 8000, "README 篇幅过短，无法覆盖交付要求"


def test_covers_required_sections():
    text = _readme()
    missing = [section for section in REQUIRED_SECTIONS if section not in text]
    assert not missing, f"README 缺少章节：{missing}"


def test_documents_all_eight_roles():
    text = _readme()
    missing = [key for key in BUILTIN_ROLE_KEYS if key not in text]
    assert not missing, f"README 未记录内置角色：{missing}"


def test_documents_all_eight_providers():
    text = _readme()
    missing = [key for key in PROVIDER_KEYS if key not in text]
    assert not missing, f"README 未记录 provider：{missing}"


def test_documents_six_subcommands():
    text = _readme()
    missing = [
        command for command in SUBCOMMANDS if f"main.py {command}" not in text
    ]
    assert not missing, f"README 未记录子命令：{missing}"


def test_documents_bare_invocation_overview():
    """不带子命令直接运行时，除了帮助还会打印角色数与最近 3 次 run 概览。"""
    assert "最近 3 次 run" in _readme(), "README 未记录无子命令启动的输出"


def test_documents_ollama_endpoint_override_not_env_var():
    """``OLLAMA_BASE_URL`` 不决定端点：说错了用户照着改仍会连 localhost。"""
    text = _readme()
    assert "models.providers.ollama.base_url" in text, (
        "README 未指明 ollama 端点的正确覆盖方式")
    assert "不决定端点" in text, "README 未声明 OLLAMA_BASE_URL 不决定端点"


def test_documents_all_known_limitations():
    text = _readme()
    for label, anchors in KNOWN_LIMITATIONS.items():
        missing = [anchor for anchor in anchors if anchor not in text]
        assert not missing, f"README 未声明已知限制「{label}」（缺 {missing}）"


def test_mock_artifacts_are_declared_handwritten():
    text = _readme()
    assert "examples/fixtures/" in text, "README 未指明 mock 夹具目录"
    assert "不是模型生成" in text, "README 未声明 mock 产物不是模型生成"


def test_documents_hermetic_test_command():
    text = _readme()
    assert "python -m pytest -q" in text, "README 未给出测试命令 python -m pytest -q"
    assert "网络" in text, "README 未说明测试套件不需要网络"


def test_documents_custom_role_recipe():
    text = _readme()
    assert "RoleSpec" in text, "README 未给出 RoleSpec 示例"
    assert "register_role" in text, "README 未给出 crew.register_role 路径"
    assert "roles.d" in text, "README 未给出 roles.d YAML 路径"


def test_no_placeholder_left():
    text = _readme()
    found = [bad for bad in PLACEHOLDERS if bad in text]
    assert not found, f"README 残留占位文本：{found}"


def test_pyproject_has_lint_starters_and_keeps_pytest_config():
    """spec §17.5：pyproject 补 ruff/mypy 起步配置，且不动 pytest 配置块。"""
    text = (README.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.ruff]" in text, "pyproject 缺少 [tool.ruff] 起步配置"
    assert "[tool.mypy]" in text, "pyproject 缺少 [tool.mypy] 起步配置"
    assert 'asyncio_mode = "auto"' in text, "pytest asyncio_mode 配置被改动"
    assert 'testpaths = ["tests"]' in text, "pytest testpaths 配置被改动"


#: spec 声明的运行下界。
PYTHON_FLOOR = "3.10"


def test_tooling_polices_the_declared_python_floor():
    """I7：ruff / mypy 必须按 spec 声明的 3.10 检查，而不是写成 py312。

    写成 py312 时静态检查**不会**拦下 3.11+ 的语法（``except*``、PEP 695 的
    类型参数列表…），而声明的下界是 3.10：代码一路过检，直到用户在 3.10 上
    导入那一刻炸掉 —— 那时才发现，比在 checker 里早发现贵得多。
    """
    text = (README.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'target-version = "py310"' in text, "ruff target-version 未对齐 3.10"
    assert 'python_version = "3.10"' in text, "mypy python_version 未对齐 3.10"
    assert "py312" not in text and '"3.12"' not in text, "仍有工具链钉在 3.12"


def test_sources_parse_under_the_python_3_10_grammar():
    """配置之外的可执行证据：交付源码本身用的语法 3.10 也认。

    ``ast.parse(feature_version=…)`` 用目标版本的语法解析（``except*`` / PEP 695
    类型参数这类 3.11+ 语法会直接 SyntaxError）。它覆盖的是**语法**，不是运行时
    API —— 但配置项管的就是语法这一层。
    """
    root = README.parent
    targets = sorted((root / "agentcrew").rglob("*.py"))
    targets += [root / "main.py", *sorted((root / "examples").glob("*.py"))]
    assert targets
    for path in targets:
        try:
            ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 10))
        except SyntaxError as exc:
            pytest.fail(f"{path} 用了 3.10 不认的语法：{exc}")
