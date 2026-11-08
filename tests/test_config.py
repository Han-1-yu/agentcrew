import os
from pathlib import Path

import pytest
import yaml

from agentcrew.config import Settings, redact_overrides

#: 一个一眼看得出是密钥的假值（出现在任何输出里都算泄漏）。
YAML_SECRET = "sk-YAMLSECRET_0123456789abcdef"


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


# ── provider_overrides 的校验与脱敏（I1）──────────────────────────────────


def _yaml_config(monkeypatch, tmp_path, payload: dict) -> None:
    path = tmp_path / "agentcrew.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(path))


def test_yaml_api_key_override_is_rejected_without_echoing_the_value(
        monkeypatch, tmp_path):
    """``models.providers.<x>.api_key`` 必须在读取配置时就报错。

    修复前它是「双输」：字段被静默忽略（引擎只读 ProviderPreset 的字段），
    却照样明文写进 ``run.json`` 的 ``config_snapshot``。异常消息只许点名字段，
    不许回显值 —— 把密钥抄进消息等于换个地方泄漏。
    """
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"providers": {"openai": {"api_key": YAML_SECRET}}}})
    with pytest.raises(ValueError) as ei:
        Settings.load()
    message = str(ei.value)
    assert "api_key" in message and "疑似密钥字段" in message
    assert YAML_SECRET not in message


def test_yaml_nested_secret_field_is_rejected(monkeypatch, tmp_path):
    """密钥字段藏在 ``capabilities`` 里也一样被拒：校验是**按 schema 递归**的。

    只查顶层等于把承诺的深度交给「猜密钥会写在哪一层」—— ``capabilities`` 今天
    是唯一能嵌套的分支，但它照样能把 ``api_key`` 原样送进 ``run.json``。
    """
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"providers": {
            "openai": {"capabilities": {"api_key": YAML_SECRET}}}}})
    with pytest.raises(ValueError) as ei:
        Settings.load()
    message = str(ei.value)
    assert "capabilities.api_key" in message and "疑似密钥字段" in message
    assert YAML_SECRET not in message


def test_yaml_nested_unknown_field_is_rejected(monkeypatch, tmp_path):
    """嵌套层的未知字段同样不得静默忽略（与顶层同一条规则）。"""
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"providers": {
            "openai": {"capabilities": {"supports_json_schemas": True}}}}})
    with pytest.raises(ValueError) as ei:
        Settings.load()
    assert "capabilities.supports_json_schemas" in str(ei.value)
    assert "未知字段" in str(ei.value)


def test_yaml_unknown_override_field_is_rejected(monkeypatch, tmp_path):
    """未知字段过去被静默忽略：写错了什么都不会发生，操作者却以为生效了。"""
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"providers": {"openai": {"base_ur": "https://x"}}}})
    with pytest.raises(ValueError, match="未知字段"):
        Settings.load()


def test_yaml_unknown_provider_is_rejected(monkeypatch, tmp_path):
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"providers": {"openai2": {"implementation": "mock"}}}})
    with pytest.raises(ValueError, match="未知 provider"):
        Settings.load()


def test_yaml_known_override_fields_still_load(monkeypatch, tmp_path):
    """READ 的合法覆盖不受影响：implementation / base_url / capabilities。"""
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"providers": {
            "ollama": {"base_url": "http://localhost:11435/v1"},
            "doubao": {"capabilities": {"supports_json_schema": False}},
        }}})
    settings = Settings.load()
    assert settings.provider_overrides["ollama"]["base_url"] == (
        "http://localhost:11435/v1")


def test_config_snapshot_redacts_secret_looking_override_values():
    """第二层防御：即便密钥字段混进了 Settings，快照里也只留 ``***``。

    ``model_copy`` 绕过校验构造（模拟修复之前写下的 / 从别处注入的配置），
    快照是**文件**，写出去就收不回来了。
    """
    settings = Settings.load().model_copy(update={
        "provider_overrides": {
            "openai": {"api_key": YAML_SECRET, "implementation": "mock"}}})
    snapshot = settings.config_snapshot()
    assert snapshot["provider_overrides"]["openai"]["api_key"] == "***"
    assert snapshot["provider_overrides"]["openai"]["implementation"] == "mock"
    assert YAML_SECRET not in str(snapshot)
    # 真的落盘一遍也一样（run.json 的 config_snapshot 就是它）
    assert YAML_SECRET not in yaml.safe_dump(snapshot, allow_unicode=True)


def test_config_snapshot_redacts_nested_secret_values():
    """脱敏必须递归：藏在 ``capabilities`` 里的密钥同样一个字符都不许落盘。"""
    nested = {"capabilities": {"api_key": YAML_SECRET, "max_output_tokens": 111}}
    settings = Settings.load().model_copy(
        update={"provider_overrides": {"openai": nested}})
    snapshot = settings.config_snapshot()
    assert snapshot["provider_overrides"]["openai"]["capabilities"]["api_key"] == "***"
    assert (
        snapshot["provider_overrides"]["openai"]["capabilities"]["max_output_tokens"]
        == 111
    )
    assert YAML_SECRET not in str(snapshot)
    assert YAML_SECRET not in yaml.safe_dump(snapshot, allow_unicode=True)


def test_redact_overrides_keeps_other_fields_and_non_mapping_values():
    assert redact_overrides({"openai": {"token": "t", "base_url": "u"}}) == {
        "openai": {"token": "***", "base_url": "u"}}
    # 嵌套一层同样处理，且不改动非密钥字段
    assert redact_overrides(
        {"openai": {"capabilities": {"secret": "s", "max_output_tokens": 7}}}
    ) == {"openai": {"capabilities": {"secret": "***", "max_output_tokens": 7}}}
    # 能力位里名字含 token 的**合法**字段不得被误伤（按名字一刀切会抹掉它们）
    assert redact_overrides(
        {"openai": {"capabilities": {"max_context_tokens": 8192}}}
    ) == {"openai": {"capabilities": {"max_context_tokens": 8192}}}
    # 未知字段的子树没有 schema 可依：继续按名字递归找密钥
    assert redact_overrides({"openai": {"bogus": {"api_key": "x", "n": 1}}}) == {
        "openai": {"bogus": {"api_key": "***", "n": 1}}}
    assert redact_overrides(None) is None
    assert redact_overrides(["x"]) == ["x"]


def test_suite_is_isolated_from_a_developer_env_file(tmp_path, monkeypatch):
    """套件不得受开发者本机 ``.env`` 影响。

    没有 ``conftest._isolate_from_developer_env`` 时，``load_dotenv()`` 会就地
    改写 ``os.environ``，把 ``.env`` 泄漏给同进程的每一个测试 —— 于是用户照
    README「快速开始」第 2 步 ``cp .env.example .env`` 之后，``test_defaults``
    立刻变红。测试结果不该取决于开发者本机的私有配置。
    """
    (tmp_path / ".env").write_text(
        "AGENTCREW_DEFAULT_MODEL=deepseek:deepseek-chat\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert "AGENTCREW_DEFAULT_MODEL" not in os.environ
    Settings.load()
    assert "AGENTCREW_DEFAULT_MODEL" not in os.environ, (
        "load_dotenv() 把 .env 泄漏进了 os.environ：测试之间会互相污染，"
        "开发者本机的 .env 会改变套件结果"
    )


def test_real_env_vars_still_override_defaults_under_isolation(monkeypatch):
    """隔离夹具只挡住「``.env`` 文件被自动加载」，真实环境变量照常生效。

    ``api_key_for()`` 走的正是 ``os.environ``，这条路径不能一起被挡住。
    （注意不要试图在测试里换 ``chdir`` 来喂 ``.env``：``load_dotenv()`` 不带
    参数时按**调用方文件位置**向上搜索，不看 CWD。）
    """
    monkeypatch.setenv("AGENTCREW_MAX_CONCURRENCY", "9")
    assert Settings.load().max_concurrency == 9


# ── A1：custom_providers ────────────────────────────────────────────────


def test_custom_provider_loads_from_yaml(monkeypatch, tmp_path):
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"custom_providers": {
            "myrelay": {
                "implementation": "openai_compat",
                "base_url": "http://localhost:9/v1",
                "api_key_env": "MYRELAY_KEY",
                "capabilities": {"supports_json_object": True},
            }
        }}})
    s = Settings.load()
    assert "myrelay" in s.custom_providers
    assert s.custom_providers["myrelay"]["base_url"] == "http://localhost:9/v1"


def test_custom_provider_duplicate_builtin_rejected(monkeypatch, tmp_path):
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"custom_providers": {
            "openai": {
                "implementation": "openai_compat",
                "base_url": "http://x",
                "api_key_env": "X",
                "capabilities": {},
            }
        }}})
    with pytest.raises(ValueError, match="重名"):
        Settings.load()


def test_custom_provider_colon_name_rejected(monkeypatch, tmp_path):
    """回归：provider 名含冒号将无法被别名寻址，加载时即拒绝。"""
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"custom_providers": {
            "my:relay": {
                "implementation": "openai_compat",
                "base_url": "http://x",
                "api_key_env": "X",
                "capabilities": {},
            }
        }}})
    with pytest.raises(ValueError, match="冒号"):
        Settings.load()


def test_custom_provider_empty_name_rejected(monkeypatch, tmp_path):
    """回归：空 provider 名无法被别名寻址，加载时即拒绝。"""
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"custom_providers": {
            "": {
                "implementation": "openai_compat",
                "base_url": "http://x",
                "api_key_env": "X",
                "capabilities": {},
            }
        }}})
    with pytest.raises(ValueError, match="不能为空"):
        Settings.load()


def test_custom_provider_secret_field_rejected(monkeypatch, tmp_path):
    """custom_providers 里写 api_key 值必须报错（密钥绝不落盘）。"""
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"custom_providers": {
            "myrelay": {
                "implementation": "openai_compat",
                "base_url": "http://x",
                "api_key_env": "X",
                "api_key": YAML_SECRET,
                "capabilities": {},
            }
        }}})
    with pytest.raises(ValueError) as ei:
        Settings.load()
    assert "api_key" in str(ei.value)
    assert YAML_SECRET not in str(ei.value)


def test_custom_provider_unknown_field_rejected(monkeypatch, tmp_path):
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"custom_providers": {
            "myrelay": {
                "implementation": "openai_compat",
                "base_url": "http://x",
                "api_key_env": "X",
                "capabilities": {},
                "bogus_field": 1,
            }
        }}})
    with pytest.raises(ValueError, match="未知字段"):
        Settings.load()


def test_custom_provider_missing_required_field_rejected(monkeypatch, tmp_path):
    """缺 implementation / base_url / api_key_env / capabilities 之一应报错。"""
    _yaml_config(monkeypatch, tmp_path, {
        "models": {"custom_providers": {
            "myrelay": {
                "base_url": "http://x",
                "api_key_env": "X",
                "capabilities": {},
            }
        }}})
    with pytest.raises(ValueError, match="custom_providers"):
        Settings.load()


def test_custom_provider_snapshot_redacted(monkeypatch, tmp_path):
    """config_snapshot 对 custom_providers 脱敏（模拟旧 run.json 注入密钥）。"""
    s = Settings.load().model_copy(update={
        "custom_providers": {
            "myrelay": {
                "implementation": "openai_compat",
                "base_url": "http://x",
                "api_key_env": "X",
                "capabilities": {},
                "api_key": YAML_SECRET,
            }
        }
    })
    snap = s.config_snapshot()
    assert snap["custom_providers"]["myrelay"]["api_key"] == "***"
    assert YAML_SECRET not in str(snap)


def test_custom_provider_env_json(monkeypatch):
    monkeypatch.setenv(
        "AGENTCREW_CUSTOM_PROVIDERS",
        '{"myrelay":{"implementation":"openai_compat",'
        '"base_url":"http://h/v1","api_key_env":"MYKEY","capabilities":{}}}',
    )
    s = Settings.load()
    assert s.custom_providers["myrelay"]["base_url"] == "http://h/v1"


def test_custom_provider_api_key_for(monkeypatch):
    monkeypatch.setenv("MYRELAY_KEY", "sk-relay-123")
    s = Settings.load().model_copy(update={
        "custom_providers": {
            "myrelay": {
                "implementation": "openai_compat",
                "base_url": "http://x",
                "api_key_env": "MYRELAY_KEY",
                "capabilities": {},
            }
        }
    })
    assert s.api_key_for("myrelay") == "sk-relay-123"
    monkeypatch.delenv("MYRELAY_KEY", raising=False)
    assert s.api_key_for("myrelay") is None


def test_builtin_preset_api_key_env_resolves(monkeypatch):
    """A2 回归：新增内置平台（openrouter 等）的 api_key_env 必须从预设解析。

    修复前 ``api_key_for`` 只查硬编码的 ``_PROVIDER_KEY_ENV``（原始 6 家），
    openrouter/siliconflow/moonshot/zhipu/groq/together 即便设了环境变量也返回
    None，工厂误报「缺少 API Key」。这里钉住每个新预设都能正确取到 Key。
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-1")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-sf-2")
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-ms-3")
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-zp-4")
    monkeypatch.setenv("GROQ_API_KEY", "sk-gq-5")
    monkeypatch.setenv("TOGETHER_API_KEY", "sk-tg-6")
    s = Settings.load()
    assert s.api_key_for("openrouter") == "sk-or-1"
    assert s.api_key_for("siliconflow") == "sk-sf-2"
    assert s.api_key_for("moonshot") == "sk-ms-3"
    assert s.api_key_for("zhipu") == "sk-zp-4"
    assert s.api_key_for("groq") == "sk-gq-5"
    assert s.api_key_for("together") == "sk-tg-6"
    # 未设置的新预设仍为 None
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert s.api_key_for("openrouter") is None


# ── A6：config_version ─────────────────────────────────────────────────


def test_config_version_too_high_raises(monkeypatch, tmp_path):
    _yaml_config(monkeypatch, tmp_path, {"config_version": 99})
    with pytest.raises(ValueError, match="高于当前支持的 v1"):
        Settings.load()


def test_config_version_default_is_1(monkeypatch, tmp_path):
    _yaml_config(monkeypatch, tmp_path, {})
    assert Settings.load().config_version == 1


# ── 依赖清单一致性（R42：pyproject ↔ requirements.txt ↔ uv.lock）──────────


def _pyproject_dependency_names() -> set[str]:
    """从 pyproject.toml 的 ``[project] dependencies`` 里取包名（正则，不依赖 tomllib）。"""
    import re

    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"dependencies = \[(.*?)\]", text, re.S)
    assert block, "pyproject.toml 里找不到 [project] dependencies 块"
    return {name for name in re.findall(r'"([A-Za-z0-9_.-]+)', block.group(1))}


def _requirements_runtime_names() -> set[str]:
    """requirements.txt 里「# 测试」标记之前的运行时包名。"""
    text = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    runtime = text.split("# 测试", 1)[0]
    names: set[str] = set()
    for line in runtime.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        names.add(stripped.split(">=", 1)[0].split("==", 1)[0].split("<", 1)[0].strip())
    return names


def test_runtime_deps_match_between_pyproject_and_requirements():
    """两处声明必须同一份清单：pip 读 requirements、uv 读 pyproject，不一致 CI 必红。"""
    assert _pyproject_dependency_names() == _requirements_runtime_names()


def test_uv_lock_exists():
    """uv.lock 已生成且提交（Windows CI 的 `uv lock --check` 依赖它）。"""
    lock = Path(__file__).resolve().parents[1] / "uv.lock"
    assert lock.is_file() and lock.stat().st_size > 1000
