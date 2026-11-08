"""2026-11-08 安全止血与可信基线回归（对应《开源对标调研与完善方案》P0/P1）。

分五组，每组只断言**行为**，不锁实现细节：

1. 路径规范化（P0-1）：编码变体不再造成「授权端匹配不上、分发端解出来」；
2. SSE 分桶（P0-2）：A run 的事件不进 B run 的连接；
3. 认证默认（P0-3）：非回环地址无密钥拒绝启动，回环地址显式警告后放行；
4. CLI 引擎（P1）：argv 模板执行，注入串只作字面参数；
5. 默认关闭项（P1）：``run_python`` 默认不注册；配置错误在加载期报错（P1-12）；
6. 看板 XSS（P1-2）：渲染不再拼 HTML + CSP 头到位。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcrew.api.paths import PathRejected, canonical_path, canonical_target
from agentcrew.api.rbac import KeyStore, MultiKeyAuth
from agentcrew.api.server import ApiServer, EventBus
from agentcrew.config import Settings

# ── 1. 路径规范化 ────────────────────────────────────────────────────────


def test_encoded_slash_is_rejected():
    """``%2f`` 解码后段边界会变——不能「尽力修好」，只能拒。"""
    with pytest.raises(PathRejected):
        canonical_path("/runs%2fx/status")
    with pytest.raises(PathRejected):
        canonical_path("/keys%2F..%2Fruns")


def test_encoded_backslash_and_nul_are_rejected():
    with pytest.raises(PathRejected):
        canonical_path("/a%5cb")
    with pytest.raises(PathRejected):
        canonical_path("/a%00b")


@pytest.mark.parametrize("raw", ["/a/%0a", "/a/%7f", "/a/%0d%0a"])
def test_encoded_control_chars_are_rejected(raw):
    with pytest.raises(PathRejected):
        canonical_path(raw)


@pytest.mark.parametrize("raw", ["/a/../../etc", "/..", "/runs/./x", "/%2e%2e/runs"])
def test_traversal_segments_are_rejected(raw):
    with pytest.raises(PathRejected):
        canonical_path(raw)


def test_encoded_route_is_decoded_to_one_canonical_form():
    """``/%6beys`` 与 ``/keys`` 必须收敛到同一个字符串（绕过的根因）。"""
    assert canonical_path("/%6beys") == "/keys"
    assert canonical_path("/keys/") == "/keys"
    assert canonical_path("//keys//") == "/keys"
    assert canonical_path("/%256beys") == "/keys"      # 双层编码
    assert canonical_path("/") == "/"


def test_non_utf8_escape_is_rejected():
    with pytest.raises(PathRejected):
        canonical_path("/%E4")     # 半个多字节字符


def test_too_long_and_absolute_paths_are_rejected():
    with pytest.raises(PathRejected):
        canonical_path("/" + "a" * 4096)
    with pytest.raises(PathRejected):
        canonical_path("http://evil/keys")


def test_canonical_target_keeps_query_verbatim():
    path, query = canonical_target("/runs/%41/ledger?event=a%20b&limit=5")
    assert path == "/runs/A/ledger"
    assert query == "event=a%20b&limit=5"


# ── 2. 授权/分发一致（HTTP 级）──────────────────────────────────────────


async def _http(port: int, method: str, target: str,
                body: dict | None = None, key: str | None = None
                ) -> tuple[int, dict, str]:
    """发一条原始请求（**target 原样发送**，不做任何编码校正）。"""
    import asyncio

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [f"{method} {target} HTTP/1.1", "Host: localhost",
               f"Content-Length: {len(payload)}", "Connection: close"]
    if key:
        headers.append(f"Authorization: Bearer {key}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode() + payload)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, asyncio.CancelledError):
        pass
    head, _, body_bytes = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    try:
        data = json.loads(body_bytes.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        data = {}
    return status, data, body_bytes.decode("utf-8", errors="replace")


@pytest.fixture
async def viewer_server(tmp_path: Path):
    store = KeyStore(tmp_path / "_auth" / "keys.json")
    _, viewer_key = store.add(name="alice", role="viewer")
    _, admin_key = store.add(name="root", role="admin")
    srv = ApiServer(port=0, output_dir=tmp_path,
                    auth=MultiKeyAuth(store, required=True))
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        yield {"port": port, "viewer": viewer_key, "admin": admin_key,
               "store": store, "tmp": tmp_path}
    finally:
        await srv.stop()


@pytest.mark.parametrize("target", ["/keys", "/%6beys", "/KEYS".lower() + "", ])
async def test_viewer_cannot_create_key_through_any_encoding(viewer_server, target):
    """原绕过：``/%6beys`` 授权端匹配不到 → 放行；分发端解成 /keys → 建密钥。"""
    status, data, _ = await _http(viewer_server["port"], "POST", target,
                                  {"name": "evil", "role": "admin"},
                                  key=viewer_server["viewer"])
    assert status == 403, data
    assert "keys:write" in data["error"]


@pytest.mark.parametrize("target", ["/keys?x=1", "/keys/", "//keys"])
async def test_key_route_aliases_all_need_keys_write(viewer_server, target):
    status, _, _ = await _http(viewer_server["port"], "GET", target,
                               key=viewer_server["viewer"])
    assert status == 403


async def test_admin_still_reaches_decoded_route(viewer_server):
    """规范化不能把正常路径也挡掉：admin 走 ``/%6beys`` 应正常列密钥。"""
    status, data, _ = await _http(viewer_server["port"], "GET", "/%6beys",
                                  key=viewer_server["admin"])
    assert status == 200, data
    assert {item["name"] for item in data["keys"]} >= {"alice", "root"}


@pytest.mark.parametrize("target", ["/runs/%2e%2e/status", "/runs/a%00b/status",
                                    "/a%5cb", "/runs/x%2f..%2fkeys"])
async def test_illegal_paths_are_400_not_500(viewer_server, target):
    status, data, _ = await _http(viewer_server["port"], "GET", target,
                                  key=viewer_server["viewer"])
    assert status == 400, data
    assert "非法请求路径" in data["error"]


async def test_illegal_path_rejected_before_auth(viewer_server):
    """路径非法时不再进认证分支：400 先于 401，避免用状态码做探测。"""
    status, _, _ = await _http(viewer_server["port"], "GET", "/a%00b")
    assert status == 400


# ── 3. SSE 分桶 ──────────────────────────────────────────────────────────


def test_event_bus_isolates_runs():
    bus = EventBus()
    qa = bus.subscribe("R-A")
    qb = bus.subscribe("R-B")
    bus.publish("task_transition", {"run_id": "R-A", "task_id": "T-1"})
    assert "R-A" in qa.get_nowait()
    assert qb.empty(), "B run 的连接不得收到 A run 的事件"


def test_event_bus_wildcard_still_receives_everything():
    """库层/测试用的通配订阅行为不变（无 run_id 声明 = 全收）。"""
    bus = EventBus()
    q = bus.subscribe()
    bus.publish("x", {"run_id": "R-A"})
    bus.publish("y", {"run_id": "R-B"})
    assert "R-A" in q.get_nowait()
    assert "R-B" in q.get_nowait()


def test_event_bus_global_events_reach_scoped_subscribers():
    """没有 run_id 的服务级事件仍应广播（否则连接会「静默失联」）。"""
    bus = EventBus()
    q = bus.subscribe("R-A")
    bus.publish("server_notice", {"text": "restarting"})
    assert "server_notice" in q.get_nowait()


def test_event_bus_unsubscribe_cleans_scope():
    bus = EventBus()
    q = bus.subscribe("R-A")
    bus.unsubscribe(q)
    assert bus.subscriber_count == 0
    bus.publish("x", {"run_id": "R-A"})     # 不抛异常即可
    assert q.empty()


async def test_sse_stream_only_sees_own_run(tmp_path: Path):
    """端到端：连 A 的 SSE，推 B 的事件，A 收不到。"""
    import asyncio

    srv = ApiServer(port=0, output_dir=tmp_path)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /runs/R-A/events HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        first = await asyncio.wait_for(reader.read(4096), timeout=10)
        assert b"connected" in first

        srv.on_event("run_started", {"run_id": "R-B", "goal": "B 的秘密目标"})
        srv.on_event("run_started", {"run_id": "R-A", "goal": "A 的目标"})

        chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
        text = chunk.decode("utf-8", errors="replace")
        assert "A 的目标" in text
        assert "B 的秘密目标" not in text
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, asyncio.CancelledError):
            pass
    finally:
        await srv.stop()


# ── 4. serve：认证默认 / 非回环强制 ─────────────────────────────────────


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("[::1]", True),
    ("0.0.0.0", False), ("::", False), ("", False), ("10.0.0.5", False),
    ("example.com", False),
])
def test_loopback_detection(host, expected):
    import main

    assert main._is_loopback_host(host) is expected


def test_serve_refuses_non_loopback_without_keys(tmp_path: Path, capsys):
    """``serve --host 0.0.0.0`` 无密钥 → 退 3（配置错误），不是「先跑起来」。"""
    import main

    args = SimpleNamespace(host="0.0.0.0", port=18799, api_key=None,
                           output_dir=str(tmp_path), config=None)
    assert main._cmd_serve(args) == 3
    captured = capsys.readouterr()
    assert "没有配置任何密钥" in captured.err


def test_serve_refuses_loopback_without_keys_when_auth_on(tmp_path: Path):
    """回环地址也一样 fail-closed：开了认证却没密钥 ≠ 匿名放行。

    这条守的是一个真实踩过的坑——早先的实现对回环「警告后放行」，于是
    ``AGENTCREW_API_AUTH_ENABLED=1`` 的用例不是退 3，而是把进程挂在启动后的
    等待上（整批测试超时）。
    """
    import main

    args = SimpleNamespace(host="127.0.0.1", port=18799, api_key=None,
                           output_dir=str(tmp_path), config=None)
    assert main._cmd_serve(args) == 3


def test_serve_refuses_anonymous_bind_when_auth_disabled(tmp_path: Path):
    """显式关认证也不能顺手对外：非回环地址照样退 3。"""
    import main

    monkeypatch_env = {"AGENTCREW_API_AUTH_ENABLED": "false"}
    import os

    saved = {key: os.environ.get(key) for key in monkeypatch_env}
    os.environ.update(monkeypatch_env)
    try:
        args = SimpleNamespace(host="0.0.0.0", port=18799, api_key=None,
                               output_dir=str(tmp_path), config=None)
        assert main._cmd_serve(args) == 3
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_auth_default_is_on():
    assert Settings.load().api_auth_enabled is True


# ── 5. 配置快照：mcp_servers 的 Bearer 头不能进 run.json ──────────────────


def test_config_snapshot_redacts_mcp_bearer_headers():
    """方案 §5.2 点名要复核的一处：`headers.Authorization: Bearer ...`。

    结论：已有 ``_SECRET_KEY_RE``（含 ``auth``）覆盖 ``Authorization``，无需改
    代码——但把它锁进测试，避免以后有人把正则收窄成只认 ``key/token`` 又漏回来。
    """
    settings = Settings.model_validate({"mcp_servers": {"gh": {
        "command": ["npx", "server"],
        "headers": {"Authorization": "Bearer sk-live-1", "X-Trace": "ok"},
        "env": {"GH_TOKEN": "ghp_x"},
    }}})
    snapshot = settings.config_snapshot()["mcp_servers"]["gh"]
    assert snapshot["headers"]["Authorization"] == "***"
    assert snapshot["headers"]["X-Trace"] == "ok"
    assert snapshot["env"]["GH_TOKEN"] == "***"


# ── 6. CLI 引擎：argv 模板，不经 shell ──────────────────────────────────


def _cli_binding(command: str, model: str = "m"):
    from agentcrew.models.config import ModelBinding, ProviderCapabilities

    return ModelBinding.model_validate(dict(
        alias="cli:x", provider="cli", model=model, implementation="cli",
        base_url=None, api_key_env="", capabilities=ProviderCapabilities(),
        command=command,
    ))


def test_cli_argv_keeps_injection_as_literal_argument():
    from agentcrew.models.providers.cli import CliProvider

    provider = CliProvider(_cli_binding("echo {model}", model="a; rm -rf / #"))
    argv = provider._build_argv(model="a; rm -rf / #", prompt="p",
                                max_tokens=10, temperature=0.0)
    assert argv == ["echo", "a; rm -rf / #"], "分号必须只是字面参数"


def test_cli_argv_does_not_expand_shell_substitution():
    from agentcrew.models.providers.cli import CliProvider

    provider = CliProvider(_cli_binding("echo $(id)"))
    argv = provider._build_argv(model="m", prompt="p", max_tokens=1,
                               temperature=0.0)
    assert argv == ["echo", "$(id)"]


async def test_cli_provider_runs_without_shell():
    """真机验证：命令行里的分号不会被执行成第二条命令。"""
    from agentcrew.models.providers.base import Message
    from agentcrew.models.providers.cli import CliProvider

    provider = CliProvider(_cli_binding(f'"{sys.executable}" -c "import sys;'
                                        f"print('OK:' + sys.argv[1])\" {{model}}",
                                        model="a; echo INJECTED"))
    messages: list[Message] = [{"role": "user", "content": "hi"}]
    result = await provider.complete(messages, temperature=0.0, max_tokens=8)
    assert "OK:a; echo INJECTED" in result.text
    assert "INJECTED\n" not in result.text.replace("OK:a; echo INJECTED", "")


def test_cli_bad_placeholder_reports_cleanly():
    from agentcrew.models.providers.cli import CliProvider

    provider = CliProvider(_cli_binding("echo {nope}"))
    with pytest.raises(ValueError, match="占位符非法"):
        provider._build_argv(model="m", prompt="p", max_tokens=1, temperature=0)


def test_cli_unbalanced_quote_reports_cleanly():
    from agentcrew.models.providers.cli import CliProvider

    provider = CliProvider(_cli_binding('echo "未闭合'))
    with pytest.raises(ValueError, match="引号不配对"):
        provider._build_argv(model="m", prompt="p", max_tokens=1, temperature=0)


# ── 7. run_python 默认关闭 / 配置期校验 ────────────────────────────────


def test_code_exec_defaults_off():
    assert Settings.load().code_exec_enabled is False


def test_code_exec_env_switch(monkeypatch):
    monkeypatch.setenv("AGENTCREW_CODE_EXEC_ENABLED", "true")
    assert Settings.load().code_exec_enabled is True


def test_tool_registry_omits_run_python_by_default(tmp_path: Path, monkeypatch):
    from agentcrew.crew.crew import Crew
    from agentcrew.runtime.ledger import Ledger

    monkeypatch.setenv("AGENTCREW_TOOLS_ENABLED", "true")
    monkeypatch.delenv("AGENTCREW_CODE_EXEC_ENABLED", raising=False)
    settings = Settings.load(output_dir=tmp_path)
    crew = Crew(settings)
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="R")
    checkpoint = SimpleNamespace(run_dir=tmp_path / "run")
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)
    registry = crew._build_tool_registry(checkpoint, ledger)
    assert registry is not None
    assert "run_python" not in [tool.name for tool in registry.list_tools()]

    monkeypatch.setenv("AGENTCREW_CODE_EXEC_ENABLED", "true")
    on_settings = Settings.load(output_dir=tmp_path)
    on_registry = Crew(on_settings)._build_tool_registry(checkpoint, ledger)
    assert "run_python" in [tool.name for tool in on_registry.list_tools()]


@pytest.mark.parametrize("payload", [{"max_concurrency": 0},
                                     {"context_budget_tokens": 0},
                                     {"plan_max_attempts": 0},
                                     {"mock_latency_ms": -1},
                                     {"review_score_threshold": 1.5},
                                     {"review_score_threshold": 0}])
def test_invalid_config_fails_at_load(payload):
    """P1-12：非法配置在加载期报错（带字段名），不留运行时兜底。"""
    with pytest.raises(ValueError) as exc:
        Settings.model_validate(payload)
    assert next(iter(payload)) in str(exc.value)


# ── 8. run overrides 单点校验（P1-11）───────────────────────────────────


def test_run_options_single_validator():
    from agentcrew.api import run_options
    from agentcrew.api.server import _validate_run_options as legacy

    assert run_options.validate_run_options({"mock": True}) is None
    assert legacy({"mock": True}) is None          # 兼容别名指向同一实现
    assert "不支持的键" in legacy({"nope": 1})
    assert "必须是布尔值" in legacy({"mock": "false"})
    assert "必须 >= 1" in legacy({"max_concurrency": 0})


def test_apply_run_options_does_not_mutate_on_error():
    from agentcrew.api import apply_run_options

    base = Settings.load()
    same, error = apply_run_options(base, {"unknown_key": 1})
    assert error is not None and same is base
    changed, error = apply_run_options(base, {"mock": True})
    assert error is None and changed.mock is True and base.mock is False


def test_sdk_option_keys_stay_within_whitelist():
    """SDK 文档里举例的键必须仍在白名单内（防止文档与实现漂移）。"""
    from agentcrew.api import ALLOWED_RUN_OPTIONS

    sdk_source = (Path(__file__).resolve().parents[1]
                  / "sdk" / "agentcrew_sdk" / "client.py").read_text(
                      encoding="utf-8")
    for key in re.findall(r"``([a-z_]+)``", sdk_source):
        if key in {"mock", "default_model", "max_concurrency"}:
            assert key in ALLOWED_RUN_OPTIONS


# ── 9. 看板 XSS + CSP ───────────────────────────────────────────────────


def test_dashboard_does_not_interpolate_goal_into_innerhtml():
    from agentcrew.api.dashboard import DASHBOARD_HTML

    assert "innerHTML = `" not in DASHBOARD_HTML, "仍有模板字符串拼 HTML"
    # run 卡片必须建节点 + textContent，而不是拼字符串
    assert "el(\"div\", \"goal\", r.goal || \"\")" in DASHBOARD_HTML
    assert "function el(tag, className, text)" in DASHBOARD_HTML


def test_dashboard_escapes_flow_props_values():
    from agentcrew.api.dashboard import DASHBOARD_HTML

    # 属性面板改用 DOM 赋值：不再把 task.title 拼进 value="..."
    assert 'value="${(task.title' not in DASHBOARD_HTML
    assert 'node.value = value' in DASHBOARD_HTML


async def test_html_response_carries_csp(tmp_path: Path):
    import asyncio

    srv = ApiServer(port=0, output_dir=tmp_path)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(65536), timeout=10)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, asyncio.CancelledError):
            pass
        head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        assert "Content-Security-Policy" in head
        assert "connect-src 'self'" in head
        assert "X-Content-Type-Options: nosniff" in head
    finally:
        await srv.stop()
