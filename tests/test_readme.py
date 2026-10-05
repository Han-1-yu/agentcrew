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

from pathlib import Path

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
