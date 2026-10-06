"""OpenAI 兼容 Provider 的行为测试（spec §12.1 / §17.6）。

brief 用例原样保留；在其之上补四件事：

- ``slept``（autouse）把 ``asyncio.sleep`` 换成记录器，沿用 ``test_retry.py`` 的
  约定 —— brief 要求 ``attempts=4`` / ``base_delay=1.0``，若真睡，两个耗尽重试
  的用例合计要等约 14 秒；真实等待时长本身已由 ``test_retry.py`` 覆盖。
- 服务端 ``Retry-After`` 必须经由 ``retry_async`` 的 ``retry_after`` 钩子覆盖自算
  退避（spec §17.6 的 carry-forward），brief 用例未覆盖。
- ``Retry-After`` 的 300 秒上限与截断上报（控制器 FINDING 1）：不设上限等于把
  停摆本进程的能力交给远端；截断经 ``on_retry`` 同一通道留痕。
- 连接/传输/网络类错误（``ConnectError``/``TransportError``/``NetworkError``）
  必须判为瞬时并重试（控制器 FINDING 2）。

生产参数（4 次尝试 + 指数退避 + ``on_retry`` 记账）用记录器钉死，零真实等待。
"""

import asyncio
import sys

import pytest

from agentcrew.models.config import parse_alias
from agentcrew.models.providers.openai_compat import (
    MAX_RETRY_AFTER_SECONDS,
    RETRY_AFTER_CLAMPED_ATTR,
    OpenAICompatProvider,
    classify_http_error,
    retry_after_seconds,
)
from agentcrew.utils.retry import PermanentError, TransientError


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """记录 ``asyncio.sleep`` 的时长而不真睡（返回时长列表）。"""
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


class FakeResp:
    def __init__(self, text, pt=10, ct=5):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]
        self.usage = type("U", (), {"prompt_tokens": pt, "completion_tokens": ct})()

class FakeClient:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error
        self.calls = []
        self.chat = type("Chat", (), {"completions": self})()
    async def create(self, **kw):
        self.calls.append(kw)
        if self._error:
            raise self._error
        return self._result

async def test_happy_path_returns_text_and_usage():
    c = FakeClient(FakeResp("hello"))
    p = OpenAICompatProvider(parse_alias("openai:gpt-4o-mini"), api_key="k", client=c)
    r = await p.complete([{"role": "user", "content": "hi"}],
                         temperature=0.2, max_tokens=100)
    assert r.text == "hello" and r.prompt_tokens == 10 and r.completion_tokens == 5

async def test_passes_temperature_and_max_tokens():
    c = FakeClient(FakeResp("x"))
    p = OpenAICompatProvider(parse_alias("openai:gpt-4o-mini"), api_key="k", client=c)
    await p.complete([{"role": "user", "content": "hi"}], temperature=0.7, max_tokens=42)
    assert c.calls[0]["temperature"] == 0.7 and c.calls[0]["max_tokens"] == 42

async def test_response_format_forwarded_when_provided():
    c = FakeClient(FakeResp("{}"))
    p = OpenAICompatProvider(parse_alias("openai:gpt-4o-mini"), api_key="k", client=c)
    await p.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=10,
                     response_format={"type": "json_object"})
    assert c.calls[0]["response_format"] == {"type": "json_object"}

async def test_response_format_omitted_when_none():
    c = FakeClient(FakeResp("x"))
    p = OpenAICompatProvider(parse_alias("openai:gpt-4o-mini"), api_key="k", client=c)
    await p.complete([{"role": "user", "content": "hi"}], temperature=0, max_tokens=10)
    assert "response_format" not in c.calls[0]

async def test_system_merged_into_user_when_unsupported():
    c = FakeClient(FakeResp("x"))
    b = parse_alias("openai:m", overrides={"openai": {
        "capabilities": {"supports_system_role": False}}})
    p = OpenAICompatProvider(b, api_key="k", client=c)
    await p.complete([{"role": "system", "content": "SYS"},
                      {"role": "user", "content": "USR"}], temperature=0, max_tokens=10)
    msgs = c.calls[0]["messages"]
    assert len(msgs) == 1 and "SYS" in msgs[0]["content"] and "USR" in msgs[0]["content"]

@pytest.mark.parametrize("status,expected", [
    (429, TransientError), (500, TransientError), (502, TransientError),
    (503, TransientError), (504, TransientError),
    (400, PermanentError), (401, PermanentError), (403, PermanentError),
    (404, PermanentError), (422, PermanentError),
])
def test_classify_http_status(status, expected):
    e = Exception(f"Error code: {status} - something")
    assert classify_http_error(e) is expected

def test_classify_timeout_is_transient():
    assert classify_http_error(TimeoutError("timed out")) is TransientError

def test_classify_unknown_is_permanent():
    assert classify_http_error(ValueError("weird")) is PermanentError

async def test_transient_error_is_retried():
    c = FakeClient(error=Exception("Error code: 429 - rate limited"))
    p = OpenAICompatProvider(parse_alias("openai:m"), api_key="k", client=c)
    with pytest.raises(TransientError):
        await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert len(c.calls) > 1

async def test_permanent_error_not_retried():
    c = FakeClient(error=Exception("Error code: 401 - bad key"))
    p = OpenAICompatProvider(parse_alias("openai:m"), api_key="k", client=c)
    with pytest.raises(PermanentError):
        await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert len(c.calls) == 1


# ── 以下为 brief 之外的补强用例 ──────────────────────────────────────────────

async def test_production_retry_config_is_four_attempts_with_backoff(slept):
    """attempts=4 / base_delay=1.0 / on_retry 记账：用记录器代替真实等待。"""
    c = FakeClient(error=Exception("Error code: 503 - overloaded"))
    seen: list[tuple[int, str, float]] = []
    p = OpenAICompatProvider(
        parse_alias("openai:m"), api_key="k", client=c,
        on_retry=lambda n, e, d: seen.append((n, type(e).__name__, d)))
    with pytest.raises(TransientError):
        await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert len(c.calls) == 4
    assert [n for n, _, _ in seen] == [1, 2, 3]
    assert {name for _, name, _ in seen} == {"TransientError"}
    assert len(slept) == 3
    assert 0.7 <= slept[0] <= 1.3      # 1.0 × 2^0，±30% 抖动
    assert 1.4 <= slept[1] <= 2.6      # 1.0 × 2^1
    assert 2.8 <= slept[2] <= 5.2      # 1.0 × 2^2
    assert [d for _, _, d in seen] == slept


async def test_retry_after_header_overrides_computed_backoff(slept):
    """服务端 Retry-After 覆盖自算退避（spec §17.6）：0 秒不得回落成 1 秒。"""
    err = Exception("Error code: 429 - rate limited")
    err.response = type("R", (), {"headers": {"Retry-After": "0"}})()
    c = FakeClient(error=err)
    delays: list[float] = []
    p = OpenAICompatProvider(parse_alias("openai:m"), api_key="k", client=c,
                             on_retry=lambda n, e, d: delays.append(d))
    with pytest.raises(TransientError):
        await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert len(c.calls) == 4
    assert delays == [0.0, 0.0, 0.0]
    assert slept == []


def test_retry_after_reads_seconds_from_response_headers():
    err = Exception("boom")
    err.response = type("R", (), {"headers": {"Retry-After": "2.5"}})()
    assert retry_after_seconds(err) == 2.5


def test_retry_after_accepts_lowercase_header_key():
    """httpx.Headers 的键是小写（其 get 不区分大小写）；普通 dict 需要兜底。"""
    err = Exception("boom")
    err.response = type("R", (), {"headers": {"retry-after": "3"}})()
    assert retry_after_seconds(err) == 3.0


def test_retry_after_follows_cause_chain_to_sdk_exception():
    """引擎只看到类型化错误；原始 SDK 异常挂在 __cause__ 上，必须能找到。"""
    sdk = Exception("Error code: 429 - rate limited")
    sdk.response = type("R", (), {"headers": {"Retry-After": "7"}})()
    typed = TransientError(str(sdk))
    typed.__cause__ = sdk
    assert retry_after_seconds(typed) == 7.0


def test_retry_after_absent_or_unparseable_is_none():
    """缺 response / 缺 headers / 值不可解析 / 负值：一律 None，绝不抛异常。"""
    bare = Exception("boom")
    with_headers = Exception("boom")
    with_headers.response = type("R", (), {"headers": {"Retry-After": "soon"}})()
    negative = Exception("boom")
    negative.response = type("R", (), {"headers": {"Retry-After": "-1"}})()
    no_headers = Exception("boom")
    no_headers.response = object()
    assert retry_after_seconds(bare) is None
    assert retry_after_seconds(no_headers) is None
    assert retry_after_seconds(with_headers) is None
    assert retry_after_seconds(negative) is None


# ── 控制器 FINDING 1：Retry-After 上限（不允许远端停摆本进程） ──────────────

def test_retry_after_above_ceiling_is_clamped():
    err = Exception("Error code: 429 - rate limited")
    err.response = type("R", (), {"headers": {"Retry-After": "86400"}})()
    assert retry_after_seconds(err) == MAX_RETRY_AFTER_SECONDS


@pytest.mark.parametrize("raw,expected", [("0", 0.0), ("2.5", 2.5), ("300", 300.0)])
def test_retry_after_at_or_below_ceiling_passes_through(raw, expected):
    """服务端指令在合理范围内保持权威：不截断、不留痕、值原样返回。"""
    err = Exception("Error code: 429 - rate limited")
    err.response = type("R", (), {"headers": {"Retry-After": raw}})()
    assert retry_after_seconds(err) == expected
    assert not hasattr(err, RETRY_AFTER_CLAMPED_ATTR)


async def test_retry_after_clamp_is_reported_through_on_retry(slept):
    """截断必须经 on_retry 通道可见（同一对象留痕），而不是静默等 300 秒。"""
    err = Exception("Error code: 429 - rate limited")
    err.response = type("R", (), {"headers": {"Retry-After": "86400"}})()
    c = FakeClient(error=err)
    seen: list[tuple[int, Exception, float]] = []
    p = OpenAICompatProvider(parse_alias("openai:m"), api_key="k", client=c,
                             on_retry=lambda n, e, d: seen.append((n, e, d)))
    with pytest.raises(TransientError):
        await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert len(c.calls) == 4
    assert [d for _, _, d in seen] == [MAX_RETRY_AFTER_SECONDS] * 3
    assert slept == [MAX_RETRY_AFTER_SECONDS] * 3      # 记录器：零真实等待
    for _, exc, _ in seen:
        assert getattr(exc, RETRY_AFTER_CLAMPED_ATTR, None) == 86400.0
        # add_note / __notes__ 是 PEP 678（Python 3.11+），3.10 上不设置
        if sys.version_info >= (3, 11):
            notes = getattr(exc, "__notes__", [])
            assert any("86400" in note and "已截断" in note for note in notes)


# ── 控制器 FINDING 2：连接/传输/网络类错误必须重试 ──────────────────────────

@pytest.mark.parametrize("name", ["ConnectError", "TransportError", "NetworkError",
                                  "TimeoutError", "ConnectionError"])
def test_classify_network_failure_type_names_are_transient(name):
    """真实 SDK 的连接失败叫 ``ConnectError``（Connective 而非 Connection）。"""
    exc = type(name, (Exception,), {})("boom")
    assert classify_http_error(exc) is TransientError


async def test_connect_error_is_retried_as_transient(slept):
    """掉线是最该重试的故障：绝不能因类型名不匹配而判为永久错误。"""
    err = type("ConnectError", (Exception,), {})("connection dropped")
    c = FakeClient(error=err)
    p = OpenAICompatProvider(parse_alias("openai:m"), api_key="k", client=c)
    with pytest.raises(TransientError):
        await p.complete([{"role": "user", "content": "x"}], temperature=0, max_tokens=10)
    assert len(c.calls) == 4
