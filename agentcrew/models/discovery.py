"""模型发现服务（A3）：按需拉取 provider 的 ``/models`` 端点。

设计要点：

- **不引入新依赖**：用标准库 ``urllib.request`` 在 ``asyncio.to_thread`` 里跑。
- **OpenAI 兼容**：``GET {base_url}/models``，``Authorization: Bearer <key>``。
- **URL 候选回退**：从 ``base_url`` 派生多个候选 URL，依次尝试，第一个成功的返回。
- **anthropic 单独适配**：``GET https://api.anthropic.com/v1/models``，
  header ``x-api-key`` + ``anthropic-version: 2023-06-01``。
- **错误分类**：``network`` / ``auth`` / ``not_found`` / ``parse`` / ``timeout``，
  抛 :class:`ModelDiscoveryError`，含 ``kind`` 字段和可读消息，不栈崩溃。
- 结果按模型名字母序去重排序。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from agentcrew.models.config import ModelBinding

#: 单次请求超时（秒）。
DISCOVERY_TIMEOUT_SECS = 10.0

#: anthropic API 版本头。
_ANTHROPIC_VERSION = "2023-06-01"


class ModelDiscoveryError(Exception):
    """模型发现失败，含分类 ``kind`` 字段。

    ``kind`` ∈ ``network`` / ``auth`` / ``not_found`` / ``parse`` / ``timeout``。
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def candidate_urls(binding: ModelBinding) -> list[str]:
    """从 ``base_url``（或 ``models_url`` 覆写）派生 ``/models`` 候选 URL。

    - 若 ``binding.models_url`` 显式指定，只试它；
    - 否则：``{base_url}/models``；若 base_url 不以 ``/v1`` 结尾，再试
      ``{base_url}/v1/models``。
    去重保序。
    """
    if binding.models_url:
        return [binding.models_url]
    base = (binding.base_url or "").rstrip("/")
    if not base:
        return []
    candidates = [f"{base}/models"]
    if not base.endswith("/v1"):
        candidates.append(f"{base}/v1/models")
    seen: set[str] = set()
    result: list[str] = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _build_request(url: str, binding: ModelBinding, api_key: str) -> urllib.request.Request:
    """按 provider 类型构造 HTTP 请求（header 不同）。"""
    headers = {"Accept": "application/json"}
    if binding.implementation == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = _ANTHROPIC_VERSION
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return urllib.request.Request(url, headers=headers, method="GET")


def _parse_models(body: str) -> list[str]:
    """从 ``/models`` 响应体提取模型名列表。

    OpenAI / Anthropic 兼容格式：``{"data": [{"id": "..."}, ...]}``。
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ModelDiscoveryError(
            f"响应不是合法 JSON：{exc}", kind="parse") from exc
    if not isinstance(data, dict):
        raise ModelDiscoveryError("响应不是 JSON 对象", kind="parse")
    items = data.get("data")
    if not isinstance(items, list):
        raise ModelDiscoveryError("响应缺少 data 列表", kind="parse")
    names: list[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            names.append(item["id"])
    return names


def _fetch_one_sync(url: str, binding: ModelBinding, api_key: str) -> list[str]:
    """同步拉取单个 URL（在 ``asyncio.to_thread`` 里调用）。"""
    req = _build_request(url, binding, api_key)
    try:
        with urllib.request.urlopen(
            req, timeout=DISCOVERY_TIMEOUT_SECS,
        ) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ModelDiscoveryError(
                f"鉴权失败（HTTP {exc.code}），请检查 API Key", kind="auth"
            ) from exc
        if exc.code == 404:
            raise ModelDiscoveryError(
                f"模型列表端点不存在（HTTP 404）：{url}", kind="not_found"
            ) from exc
        raise ModelDiscoveryError(
            f"HTTP {exc.code}：{url}", kind="network") from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason).lower()
        if "timed out" in reason or "timeout" in reason:
            raise ModelDiscoveryError("请求超时", kind="timeout") from exc
        raise ModelDiscoveryError(f"网络错误：{exc.reason}", kind="network") from exc
    return _parse_models(body)


async def fetch_models(
    binding: ModelBinding, *, api_key: str = ""
) -> list[str]:
    """按需拉取 provider 的模型列表，按字母序去重排序。

    依次尝试 :func:`candidate_urls` 返回的候选 URL，第一个成功的返回；
    全部失败时抛出最后一个 :class:`ModelDiscoveryError`。
    """
    urls = candidate_urls(binding)
    if not urls:
        raise ModelDiscoveryError(
            "该 provider 没有 base_url，无法发现模型", kind="not_found")
    last_error: ModelDiscoveryError | None = None
    for url in urls:
        try:
            names = await asyncio.to_thread(
                _fetch_one_sync, url, binding, api_key)
        except ModelDiscoveryError as exc:
            last_error = exc
            # 鉴权失败 / 解析错误不回退下一个 URL（换 URL 也不会好）
            if exc.kind in ("auth", "parse"):
                raise
            continue
        # 成功：去重排序返回
        return sorted(set(names))
    assert last_error is not None
    raise last_error
