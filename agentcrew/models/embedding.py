"""R27：embedding 目标的解析——把 ``embedding_model`` 别名翻成 (模型, 密钥, 端点)。

放在 models 包而不是 knowledge 包，是分层守卫的要求：knowledge 不许依赖 models。
知识层只接收 ``(model, api_key, base_url)`` 三元组，解析与 provider 目录留在这一层。
"""

from __future__ import annotations

import os
from typing import Any


def embedding_target(settings: Any) -> tuple[str, str, str | None] | None:
    """``embedding_model`` → ``(model, api_key, base_url)``；无模型名返回 ``None``。

    解析失败（未知 provider 等）也返回 ``None``：调用方据此降级为本地哈希向量，
    不让一次检索把 run 拖死。
    """
    alias = str(getattr(settings, "embedding_model", "") or "").strip()
    if not alias:
        return None
    try:
        from agentcrew.models.config import parse_alias

        binding = parse_alias(
            alias,
            custom=getattr(settings, "model_providers", None) or None,
            overrides=getattr(settings, "model_overrides", None) or None)
    except Exception:  # noqa: BLE001 —— 解析失败一律降级
        return None
    api_key = os.environ.get(binding.api_key_env, "") if binding.api_key_env else ""
    # embedding_base_url 优先于 provider 预设：OpenAI 兼容但不在预设列表里的端点
    # （自建 vLLM / 公司网关）不必为此新增一个 model_providers 条目。
    configured = str(
        getattr(settings, "embedding_base_url", "") or "").strip()
    return binding.model, api_key, configured or binding.base_url or None
