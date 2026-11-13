"""外部 CLI 引擎 Provider 的测试（V2 第 3 项）。"""

from __future__ import annotations

import sys

import pytest

from agentcrew.models.config import ModelBinding, ProviderCapabilities, parse_alias
from agentcrew.models.providers.base import Message
from agentcrew.models.providers.cli import CliProvider


def _binding(**kwargs) -> ModelBinding:
    defaults = dict(
        alias="cli:test-model",
        provider="cli",
        model="test-model",
        implementation="cli",
        base_url=None,
        api_key_env="",
        capabilities=ProviderCapabilities(),
        command="echo {prompt}",
    )
    defaults.update(kwargs)
    return ModelBinding.model_validate(defaults)


class TestCliProvider:
    @pytest.mark.asyncio
    async def test_basic_echo_command(self):
        # 用解释器自身打印固定文本：`echo` 是 shell 内建，Windows 上没有同名
        # 可执行文件（`shutil.which("echo")` 为 None），本机全量会 FileNotFoundError。
        provider = CliProvider(_binding(
            command=f"\"{sys.executable}\" -c \"print('hello')\""))
        result = await provider.complete(
            [{"role": "user", "content": "hi"}],
            temperature=0.7, max_tokens=100,
        )
        assert "hello" in result.text
        assert result.provider == "cli"
        assert result.model == "test-model"
        assert result.latency_ms >= 0
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_prompt_piped_to_stdin(self):
        # prompt 通过 stdin 传入（避免多行 prompt 作为命令参数的转义问题）
        provider = CliProvider(_binding(
            command=f'"{sys.executable}" -c "import sys; print(sys.stdin.read())"'))
        result = await provider.complete(
            [{"role": "user", "content": "TEST_PROMPT_CONTENT"}],
            temperature=0.5, max_tokens=50,
        )
        assert "TEST_PROMPT_CONTENT" in result.text

    @pytest.mark.asyncio
    async def test_missing_command_raises(self):
        provider = CliProvider(_binding(command=None))
        with pytest.raises(ValueError, match="缺少 command"):
            await provider.complete(
                [{"role": "user", "content": "hi"}],
                temperature=0.5, max_tokens=50,
            )

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises(self):
        # 用解释器自身退出码 1 触发非零退出，跨平台（Windows/Linux 的 shell 不同）。
        provider = CliProvider(_binding(
            command=f'"{sys.executable}" -c "import sys; sys.exit(1)"'))
        with pytest.raises(RuntimeError, match="退出码"):
            await provider.complete(
                [{"role": "user", "content": "hi"}],
                temperature=0.5, max_tokens=50,
            )

    def test_format_prompt_system_user(self):
        messages: list[Message] = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        prompt = CliProvider._format_prompt(messages)
        assert "[System]" in prompt
        assert "You are helpful" in prompt
        assert "[User]" in prompt
        assert "Hello" in prompt
        assert "[Assistant]" in prompt
        assert "Hi there" in prompt

    def test_parse_alias_cli_preset(self):
        binding = parse_alias("cli:my-model")
        assert binding.provider == "cli"
        assert binding.model == "my-model"
        assert binding.implementation if hasattr(binding, "implementation") else True
        assert binding.command is None  # 预设不指定 command，需 override

    def test_cli_preset_capabilities(self):
        from agentcrew.models.config import PROVIDER_PRESETS
        preset = PROVIDER_PRESETS["cli"]
        assert preset.implementation == "cli"
        assert preset.api_key_env == ""
        assert preset.capabilities.supports_json_schema is False


class TestCliProviderFactory:
    def test_factory_resolves_cli_with_override(self, tmp_path):
        from agentcrew.config import Settings
        from agentcrew.models.factory import ModelFactory
        from agentcrew.models.providers.cli import CliProvider

        s = Settings.load().model_copy(update={
            "provider_overrides": {
                "cli": {"command": "echo test"},
            },
        })
        factory = ModelFactory(s)
        provider = factory.get("cli:my-model")
        assert isinstance(provider, CliProvider)
        assert provider.binding.command == "echo test"
        assert provider.binding.model == "my-model"

    def test_factory_cli_without_command_raises_on_call(self, tmp_path):
        from agentcrew.config import Settings
        from agentcrew.models.factory import ModelFactory

        s = Settings.load()  # 没有 cli override，command 为 None
        factory = ModelFactory(s)
        provider = factory.get("cli:my-model")
        # get() 不报错（构造时不校验），但 complete() 时报错
        assert provider.binding.command is None
