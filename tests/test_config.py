import os

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
