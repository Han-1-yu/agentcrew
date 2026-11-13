"""图像生成工具 ``generate_image``（R61）：可插拔提供方 + 离线可测，默认关闭。

补齐「豆包管图画生成」这类愿景的最后一块：Agent 能在 ReAct 循环里调用本工具产图，
图片以**字节**写进 run 工作区，再经二进制产物通道（``ArtifactStore.commit_bytes`` +
``kind="image"``）成为受契约保护、可预览的交付物。

四条边界（与 ``search.py`` 同口径）：

- **密钥绝不落盘**：只从环境变量读（``ARK_API_KEY`` / ``OPENAI_API_KEY``，或通用
  ``AGENTCREW_IMAGE_API_KEY``）。配置里只有一个提供方名字。
- **默认关闭**：``image_provider`` 为空时不注册 ``generate_image``——不配就没有这个
  工具，模型看不见，也不会在提示词里被承诺一个跑不了的能力。
- **离线可测**：:class:`MockImageProvider` 回放固定 PNG 字节，单测与评测不碰网络。
- **不放大 SSRF / 写盘面**：端点写死公网 https、不跟随重定向；落盘路径先过
  ``safe_relpath`` 且限定在工作区内，模型给的路径不能逃逸。

本模块位于 ``tools`` 层，不依赖 ``runtime`` / ``crew`` / ``agents``。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from pydantic import Field

from agentcrew.tools.network import build_no_redirect_opener
from agentcrew.tools.tool import Tool
from agentcrew.utils.files import safe_relpath

__all__ = [
    "ArkSeedreamProvider",
    "GenerateImageTool",
    "ImageProvider",
    "ImageResult",
    "MockImageProvider",
    "OpenAIImageProvider",
    "build_image_provider",
]

#: 通用图像密钥环境变量（优先于提供方专属变量）。
GENERIC_KEY_ENV = "AGENTCREW_IMAGE_API_KEY"

#: 提供方 → 专属密钥环境变量。
PROVIDER_KEY_ENV: dict[str, str] = {
    "ark": "ARK_API_KEY",
    "openai": "OPENAI_API_KEY",
}

#: 单张响应字节硬上限（防提供方返回超大 payload 撑爆内存/磁盘）。
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: 离线夹具：1x1 PNG（合法可解码），供 mock 与单测用。
_MOCK_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)


@dataclass(frozen=True)
class ImageResult:
    """一次图像生成的产物字节 + 元数据。``cost_usd`` 未知为 ``None``。"""

    data: bytes
    mime: str = "image/png"
    cost_usd: float | None = None


class ImageProvider(ABC):
    """图像提供方接口：一段提示词 → 一张图（字节）。"""

    name: str = ""

    @abstractmethod
    async def generate(
        self, prompt: str, *, size: str, timeout: float
    ) -> ImageResult:
        """执行一次生成。网络/解析失败抛异常，由工具收敛成错误文本。"""

    @property
    def api_key(self) -> str:
        """本提供方密钥（只从环境变量读，绝不进配置/日志/快照）。"""
        for env_name in (GENERIC_KEY_ENV, PROVIDER_KEY_ENV.get(self.name, "")):
            if env_name:
                value = os.environ.get(env_name, "").strip()
                if value:
                    return value
        return ""

    def key_env_hint(self) -> str:
        specific = PROVIDER_KEY_ENV.get(self.name, "")
        if specific:
            return f"{specific}（或 {GENERIC_KEY_ENV}）"
        return GENERIC_KEY_ENV

    def require_key(self) -> str:
        key = self.api_key
        if not key:
            raise ValueError(
                f"图像提供方 {self.name} 缺少 API Key：请设置 "
                f"{self.key_env_hint()} 环境变量")
        return key


def _post_json(url: str, *, payload: Mapping[str, Any], key: str,
               timeout: float) -> dict[str, Any]:
    """同步 POST 一次并解析 JSON（在 ``to_thread`` 里跑，不阻塞事件循环）。"""
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AgentCrew-ImageGen/1.0",
        },
    )
    with build_no_redirect_opener().open(request, timeout=timeout) as response:
        body = response.read(_MAX_IMAGE_BYTES + 1)
    if len(body) > _MAX_IMAGE_BYTES:
        raise ValueError(f"图像接口响应超过 {_MAX_IMAGE_BYTES} 字节上限")
    parsed = json.loads(body.decode("utf-8", errors="replace"))
    if not isinstance(parsed, dict):
        raise ValueError("图像接口返回的 JSON 不是对象")
    return parsed


def _extract_b64(parsed: Mapping[str, Any]) -> ImageResult:
    """OpenAI 兼容 ``images/generations`` 形状：``data[0].b64_json``。"""
    data = parsed.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("图像接口没有返回 data 列表")
    first = data[0]
    if not isinstance(first, Mapping):
        raise ValueError("图像接口 data[0] 不是对象")
    b64 = first.get("b64_json")
    if not isinstance(b64, str) or not b64:
        raise ValueError("图像接口未返回 b64_json（请确认 response_format=b64_json）")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"图像 b64_json 解码失败：{exc}") from None
    if not raw:
        raise ValueError("图像接口返回了空图片")
    return ImageResult(data=raw, mime="image/png")


class ArkSeedreamProvider(ImageProvider):
    """豆包 / 火山方舟 Seedream（OpenAI 兼容 ``images/generations``）。"""

    name = "ark"
    endpoint = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    model = "doubao-seedream-4-0-250828"

    async def generate(self, prompt: str, *, size: str, timeout: float) -> ImageResult:
        key = self.require_key()
        parsed = await asyncio.to_thread(
            _post_json, self.endpoint, key=key, timeout=timeout,
            payload={"model": self.model, "prompt": prompt, "size": size,
                     "response_format": "b64_json"},
        )
        return _extract_b64(parsed)


class OpenAIImageProvider(ImageProvider):
    """OpenAI 图像生成（``gpt-image-1``，OpenAI 兼容 ``images/generations``）。"""

    name = "openai"
    endpoint = "https://api.openai.com/v1/images/generations"
    model = "gpt-image-1"

    async def generate(self, prompt: str, *, size: str, timeout: float) -> ImageResult:
        key = self.require_key()
        parsed = await asyncio.to_thread(
            _post_json, self.endpoint, key=key, timeout=timeout,
            payload={"model": self.model, "prompt": prompt, "size": size},
        )
        return _extract_b64(parsed)


class MockImageProvider(ImageProvider):
    """离线夹具：返回固定 PNG 字节，供单测与评测端到端用（不碰网络）。"""

    name = "mock"

    def __init__(self, data: bytes | None = None) -> None:
        self.data = data if data is not None else base64.b64decode(_MOCK_PNG_B64)
        self.calls: list[str] = []

    async def generate(self, prompt: str, *, size: str, timeout: float) -> ImageResult:
        self.calls.append(prompt)
        return ImageResult(data=self.data, mime="image/png")


def build_image_provider(name: str) -> ImageProvider:
    """按名字造提供方；空串表示关闭，未知名字报错（不静默降级成 mock）。"""
    normalized = (name or "").strip().lower()
    if normalized in ("ark", "seedream", "doubao"):
        return ArkSeedreamProvider()
    if normalized == "openai":
        return OpenAIImageProvider()
    if normalized == "mock":
        return MockImageProvider()
    raise ValueError(
        f"未知 image_provider：{name!r}（可选 ark / openai / mock）")


class GenerateImageTool(Tool):
    """按提示词生成一张图片并写进工作区，返回落盘相对路径与元数据。"""

    name: str = "generate_image"
    description: str = (
        "按文字提示生成一张图片（PNG），保存到工作区指定相对路径，返回该路径。"
        "用于配图、海报、插画等图像交付物；路径须在本任务契约声明的交付路径内。"
    )
    input_schema: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string", "minLength": 1, "maxLength": 2000,
                "description": "图像内容的文字描述",
            },
            "path": {
                "type": "string", "minLength": 1,
                "description": "工作区内相对保存路径（如 assets/cover.png）",
            },
            "size": {
                "type": "string",
                "description": "尺寸（如 1024x1024），默认 1024x1024",
            },
        },
        "required": ["prompt", "path"],
    })
    #: 构造期注入（对模型不可见）。
    provider: Any = None
    workdir: Any = None
    timeout_secs: float = 60.0
    default_size: str = "1024x1024"
    ledger: Any = None

    async def run(self, prompt: str = "", path: str = "", size: str = "",
                  **kwargs: Any) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            return "错误：prompt 必须是非空字符串"
        if len(prompt) > 2000:
            return "错误：prompt 不能超过 2000 字符"
        if not isinstance(path, str) or not path.strip():
            return "错误：path 必须是非空字符串"
        try:
            rel = safe_relpath(path.strip())
        except (ValueError, OSError) as exc:
            return f"错误：path 不安全：{exc}"
        if self.provider is None:
            return "错误：generate_image 未配置图像提供方（image_provider）"
        if self.workdir is None:
            return "错误：generate_image 未注入工作区"
        try:
            result = await self.provider.generate(
                prompt.strip(), size=(size or self.default_size),
                timeout=self.timeout_secs)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            return f"错误：图像生成失败：{exc}"
        except Exception as exc:  # noqa: BLE001 —— 工具不把异常冒泡给 Agent
            return f"错误：图像生成失败：{type(exc).__name__}: {exc}"
        target = Path(self.workdir) / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(result.data)
        except OSError as exc:
            return f"错误：图片落盘失败：{exc}"
        if self.ledger is not None:
            self.ledger.emit(
                "image_generated", path=rel.as_posix(),
                size_bytes=len(result.data), mime=result.mime,
                provider=getattr(self.provider, "name", ""),
                cost_usd=result.cost_usd,
            )
        return (f"图像已生成：{rel.as_posix()}（{result.mime}，"
                f"{len(result.data)} 字节）")
