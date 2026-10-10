"""流式检测（V5 P2）：发一个 ``stream=true`` 的最小补全请求，验证 SSE 流可正常返回。

设计要点：

- **不引入新依赖**：标准库 ``urllib.request`` + ``asyncio.to_thread``。
- **OpenAI 兼容**：``POST {base_url}/chat/completions``，body 含
  ``{"model", "messages": [...], "max_tokens": 1, "stream": true}``，
  header ``Authorization: Bearer <key>``。
- **anthropic**：``POST {base_url}/v1/messages``，header ``x-api-key`` +
  ``anthropic-version: 2023-06-01``。
- **SSE 解析**：逐行读取，遇到第一个以 ``"data:"`` 开头的行即判定成功
  （含 ``data: [DONE]``）；不等流结束。
- **计时**：从发出请求到收到第一个 data 帧的毫秒数。
- **错误分类**：``network`` / ``auth`` / ``timeout`` / ``parse``
  （响应不是 SSE 格式）。结果以 dict 返回，不抛异常。
- **mock/cli**：离线实现，直接返回 ``ok=True, latency_ms=0``，不真实请求。
- **密钥不落盘**：``api_key`` 只从参数读取、只放进请求 header，不存任何字段。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Any

from agentcrew.models.config import ModelBinding

#: anthropic API 版本头（与 discovery.py 保持一致）。
_ANTHROPIC_VERSION = "2023-06-01"


def _build_stream_request(
    binding: ModelBinding, api_key: str
) -> urllib.request.Request:
    """按 provider 类型构造流式补全 POST 请求。"""
    model = binding.model
    if binding.implementation == "anthropic":
        url = f"{(binding.base_url or '').rstrip('/')}/v1/messages"
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 1,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
    else:
        url = f"{(binding.base_url or '').rstrip('/')}/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": True,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {api_key}",
        }
    data = json.dumps(body).encode("utf-8")
    return urllib.request.Request(url, data=data, headers=headers, method="POST")


def _ok(latency_ms: float) -> dict[str, Any]:
    return {"ok": True, "latency_ms": latency_ms, "error": None}


def _fail(kind: str, message: str) -> dict[str, Any]:
    return {"ok": False, "latency_ms": None, "error": f"{kind}: {message}"}


def _check_streaming_sync(
    binding: ModelBinding, *, api_key: str, timeout_secs: float
) -> dict[str, Any]:
    """同步实现（在 ``asyncio.to_thread`` 中调用）。"""
    req = _build_stream_request(binding, api_key)
    start = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_secs)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _fail("auth", f"HTTP {exc.code}")
        return _fail("network", f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        reason = str(exc.reason).lower()
        if "timed out" in reason or "timeout" in reason:
            return _fail("timeout", "请求超时")
        return _fail("network", str(exc.reason))
    except Exception as exc:  # noqa: BLE001 —— 兜住所有同步网络异常
        return _fail("network", f"{type(exc).__name__}: {exc}")

    try:
        with resp:
            while True:
                line = resp.readline()
                if not line:
                    return _fail(
                        "parse", "响应流结束但未收到 SSE data 帧")
                text = line.decode("utf-8", errors="replace").strip()
                if text.startswith("data:"):
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    return _ok(elapsed_ms)
    except Exception as exc:  # noqa: BLE001
        reason = str(exc).lower()
        if "timed out" in reason or "timeout" in reason:
            return _fail("timeout", "读取超时")
        return _fail("network", f"{type(exc).__name__}: {exc}")
    # 理论不可达：循环要么 return 要么异常
    return _fail("parse", "响应未包含 SSE data 帧")


async def check_streaming(
    binding: ModelBinding,
    *,
    api_key: str = "",
    timeout_secs: float = 10.0,
) -> dict[str, Any]:
    """发一个 ``stream=true`` 的最小补全请求，验证 SSE 流能正常返回。

    返回 ``{"ok": bool, "latency_ms": float|None, "error": str|None}``。

    - 收到第一个以 ``data:`` 开头的行即判定 ``ok=True``（含 ``data: [DONE]``）；
    - 超时 / 连接错误 / HTTP 4xx/5xx / 非 SSE 响应 → ``ok=False``，
      ``error`` 前缀标明分类（``network`` / ``auth`` / ``timeout`` / ``parse``）；
    - ``implementation`` 为 ``mock`` 或 ``cli`` 时直接返回
      ``ok=True, latency_ms=0``（离线实现，不真实请求）。
    """
    if binding.implementation in ("mock", "cli"):
        return {"ok": True, "latency_ms": 0.0, "error": None}
    return await asyncio.to_thread(
        _check_streaming_sync,
        binding,
        api_key=api_key,
        timeout_secs=timeout_secs,
    )
