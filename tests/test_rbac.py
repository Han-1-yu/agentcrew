"""R25：多用户与 RBAC——密钥库、作用域授权、run 归属与审计身份。

分四层验证：
1. 密钥库：明文不落盘、scrypt 校验、吊销/过期失效、跨实例（CLI 建 / 服务读）生效；
2. 路由作用域表：viewer 读得到、写不了；operator 能审批；admin 能管密钥；
3. run 归属：非 admin 只看自己的（或白名单里的）run，不可见一律 404；
4. 审计：审批裁决的 decided_by 取密钥身份，不再接受自由文本。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from agentcrew.api.rbac import (
    ALL_SCOPES,
    ROLE_SCOPES,
    KeyStore,
    MultiKeyAuth,
    Principal,
    read_owner,
    required_scope,
)
from agentcrew.api.server import ApiServer
from agentcrew.runtime.approvals import ApprovalStore
from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
from agentcrew.task.task import Task
from agentcrew.task.task_board import TaskBoard

# ── 密钥库 ────────────────────────────────────────────────────────────────


def test_add_returns_plaintext_once_and_stores_only_hash(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    record, plaintext = store.add(name="alice", role="operator")
    assert plaintext.startswith("ac_")
    raw = json.loads((tmp_path / "keys.json").read_text(encoding="utf-8"))
    dumped = json.dumps(raw, ensure_ascii=False)
    assert plaintext not in dumped            # 明文绝不落盘
    assert record.key_hash and record.key_hash != plaintext
    assert store.verify(plaintext) is not None
    assert store.verify("ac_wrong") is None


def test_principal_carries_role_scopes_and_identity(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    record, plaintext = store.add(name="bob", role="viewer")
    principal = store.verify(plaintext)
    assert principal is not None
    assert principal.name == "bob" and principal.key_id == record.id
    assert principal.scopes == ROLE_SCOPES["viewer"]
    assert principal.allows("runs:read")
    assert not principal.allows("runs:write")


def test_revoked_and_expired_keys_fail_closed(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    _, plain = store.add(name="carol", role="viewer")
    store.revoke("carol")
    assert store.verify(plain) is None

    expired = (datetime.now() - timedelta(days=1)).isoformat()
    _, plain2 = store.add(name="dave", role="viewer", expires_at=expired)
    assert store.verify(plain2) is None


def test_duplicate_name_and_bad_role_rejected(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    store.add(name="erin", role="viewer")
    with pytest.raises(ValueError, match="已存在"):
        store.add(name="erin", role="viewer")
    with pytest.raises(ValueError, match="未知角色"):
        store.add(name="frank", role="superuser")
    with pytest.raises(ValueError, match="custom"):
        store.add(name="gina", role="custom")


def test_store_reloads_when_file_changes(tmp_path):
    """CLI 在建密钥，服务端另一个实例必须立刻看到（mtime 失效）。"""
    path = tmp_path / "keys.json"
    server_side = KeyStore(path)
    assert server_side.load() == []
    cli_side = KeyStore(path)
    _, plaintext = cli_side.add(name="henry", role="viewer")
    assert any(record.name == "henry" for record in server_side.load())
    assert server_side.verify(plaintext) is not None


# ── 作用域表 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path,scope", [
    ("GET", "/runs", "runs:read"),
    ("POST", "/runs", "runs:write"),
    ("DELETE", "/runs/R-1", "runs:admin"),
    ("POST", "/runs/R-1/approvals", "approvals:write"),
    ("POST", "/runs/R-1/agents", "runs:admin"),
    ("GET", "/keys", "keys:read"),
    ("POST", "/keys", "keys:write"),
    ("GET", "/tools", "tools:read"),
    ("POST", "/tools/read_file/test", "tools:test"),
    ("GET", "/health", None),
])
def test_required_scope_table(method, path, scope):
    assert required_scope(method, path) == scope


def test_role_scope_matrix():
    assert ROLE_SCOPES["viewer"] == frozenset({"runs:read", "tools:read"})
    assert "approvals:write" in ROLE_SCOPES["operator"]
    assert "keys:write" not in ROLE_SCOPES["operator"]
    assert ROLE_SCOPES["admin"] == ALL_SCOPES


# ── API 集成 ──────────────────────────────────────────────────────────────


async def _request(port: int, method: str, path: str,
                   body: dict | None = None,
                   key: str | None = None) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [f"{method} {path} HTTP/1.1", "Host: localhost",
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
    head, _, body_text = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    try:
        return status, json.loads(body_text.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return status, {}


async def _make_run_dir(output_dir, run_id: str, owner: str | None):
    store = CheckpointStore.create(
        output_dir, project_goal="rbac", config_snapshot={}, run_id=run_id)
    store.save_tasks(TaskBoard([
        Task(id="T-1", title="t", description="d", role="backend_dev",
             acceptance_criteria=["a"], deliverables=[]),
    ]))
    record = store.load_record()
    record.status = RunStatus.COMPLETED
    store.save_record(record)
    if owner is not None:
        (store.run_dir / "owner.json").write_text(
            json.dumps({"key_id": f"k_{owner}", "name": owner}),
            encoding="utf-8")
    return store.run_dir


@pytest.fixture
async def rbac_server(tmp_path):
    store = KeyStore(tmp_path / "_auth" / "keys.json")
    admin, admin_key = store.add(name="root", role="admin")
    viewer, viewer_key = store.add(name="alice", role="viewer")
    operator, operator_key = store.add(name="bob", role="operator")
    auth = MultiKeyAuth(store, required=True)
    srv = ApiServer(port=0, output_dir=tmp_path, auth=auth)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        yield {
            "port": port, "store": store, "srv": srv, "tmp": tmp_path,
            "admin": (admin, admin_key),
            "viewer": (viewer, viewer_key),
            "operator": (operator, operator_key),
        }
    finally:
        await srv.stop()


async def test_anonymous_is_rejected(rbac_server):
    status, _ = await _request(rbac_server["port"], "GET", "/runs")
    assert status == 401


async def test_viewer_can_read_but_not_write(rbac_server):
    port = rbac_server["port"]
    status, data = await _request(port, "GET", "/runs",
                                  key=rbac_server["viewer"][1])
    assert status == 200 and data["runs"] == []
    status, data = await _request(
        port, "POST", "/runs", {"goal": "做一个 Todo 应用"},
        key=rbac_server["viewer"][1])
    assert status == 403 and "runs:write" in data["error"]
    status, _ = await _request(port, "GET", "/keys",
                               key=rbac_server["viewer"][1])
    assert status == 403


async def test_operator_can_approve_but_not_manage_keys(rbac_server):
    port = rbac_server["port"]
    status, _ = await _request(
        port, "POST", "/runs/R-none/approvals",
        {"request_id": "AP-1", "decision": {"action": "approve"}},
        key=rbac_server["operator"][1])
    assert status == 404          # 路由放行，只是资源不存在
    status, _ = await _request(port, "POST", "/keys", {"name": "x"},
                               key=rbac_server["operator"][1])
    assert status == 403


async def test_admin_lists_keys_without_hashes(rbac_server):
    status, data = await _request(rbac_server["port"], "GET", "/keys",
                                  key=rbac_server["admin"][1])
    assert status == 200
    names = {item["name"] for item in data["keys"]}
    assert {"root", "alice", "bob"} <= names
    for item in data["keys"]:
        assert "key_hash" not in item and "salt" not in item


async def test_admin_creates_key_and_it_works(rbac_server):
    port = rbac_server["port"]
    status, data = await _request(
        port, "POST", "/keys",
        {"name": "newbie", "role": "viewer"}, key=rbac_server["admin"][1])
    assert status == 201
    plaintext = data["key"]
    assert plaintext.startswith("ac_") and "key_hash" not in data["record"]
    status, _ = await _request(port, "GET", "/runs", key=plaintext)
    assert status == 200


async def test_run_visibility_is_owner_scoped(rbac_server):
    tmp = rbac_server["tmp"]
    await _make_run_dir(tmp, "20260101-000000-aaaa", owner="alice")
    await _make_run_dir(tmp, "20260101-000001-bbbb", owner="bob")
    await _make_run_dir(tmp, "20260101-000002-cccc", owner=None)  # CLI 建的

    port = rbac_server["port"]
    status, data = await _request(port, "GET", "/runs",
                                  key=rbac_server["viewer"][1])
    ids = {item["run_id"] for item in data["runs"]}
    assert ids == {"20260101-000000-aaaa"}

    status, _ = await _request(
        port, "GET", "/runs/20260101-000001-bbbb/status",
        key=rbac_server["viewer"][1])
    assert status == 404          # 别人的 run：404 不泄露存在性

    status, data = await _request(port, "GET", "/runs",
                                  key=rbac_server["admin"][1])
    assert len(data["runs"]) == 3


def test_write_owner_marks_run_then_visibility_applies(tmp_path):
    """write_owner 落盘 owner.json；非 admin 立刻按归属可见。"""
    from agentcrew.api.rbac import write_owner

    run_dir = tmp_path / "20260101-000003-dddd"
    run_dir.mkdir()
    alice = Principal(name="alice", role="viewer", scopes=ROLE_SCOPES["viewer"],
                      key_id="k_alice")
    write_owner(run_dir, alice)
    assert read_owner(run_dir) == "alice"
    assert alice.can_see_run(read_owner(run_dir)) is True
    bob = Principal(name="bob", role="viewer", scopes=ROLE_SCOPES["viewer"])
    assert bob.can_see_run(read_owner(run_dir)) is False
    # 匿名 / legacy 不写归属（保持单密钥部署行为）
    legacy = Principal(name="legacy-key", role="admin", scopes=ALL_SCOPES,
                       key_id="legacy")
    other = tmp_path / "20260101-000004-eeee"
    other.mkdir()
    write_owner(other, legacy)
    assert read_owner(other) is None


async def test_decided_by_uses_key_identity(rbac_server):
    """审计身份取密钥名：body 里写什么都覆盖不掉。"""
    tmp = rbac_server["tmp"]
    run_dir = await _make_run_dir(tmp, "20260101-000010-eeee", owner="root")
    store = ApprovalStore(run_dir)
    request = store.create("plan", context={"task_ids": ["T-1"]})

    port = rbac_server["port"]
    status, data = await _request(
        port, "POST", "/runs/20260101-000010-eeee/approvals",
        {"request_id": request.id,
         "decision": {"action": "approve"},
         "decided_by": "冒名顶替"},
        key=rbac_server["admin"][1])
    assert status == 200
    assert data["decided_by"] == "root"


async def test_legacy_single_key_still_admin(tmp_path):
    """legacy api_keys 等价 admin：单密钥部署零改动，但 /keys 不可用（无库）。"""
    auth = MultiKeyAuth(None, legacy_keys=["legacy-secret"], required=True)
    srv = ApiServer(port=0, output_dir=tmp_path, auth=auth)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        status, _ = await _request(port, "GET", "/runs", key="legacy-secret")
        assert status == 200
        status, _ = await _request(port, "GET", "/keys", key="legacy-secret")
        assert status == 503     # 没配密钥库
        status, _ = await _request(port, "GET", "/runs", key="wrong")
        assert status == 401
    finally:
        await srv.stop()


def test_principal_can_see_run_matrix():
    viewer_own = Principal(name="a", role="viewer", scopes=ROLE_SCOPES["viewer"])
    assert viewer_own.can_see_run("a") is True
    assert viewer_own.can_see_run("b") is False
    assert viewer_own.can_see_run(None) is False
    scoped = Principal(name="a", role="viewer", scopes=ROLE_SCOPES["viewer"],
                       run_ids=frozenset({"20260101-000000-x"}))
    assert scoped.can_see_run("20260101-000000-x") is True
    admin = Principal(name="r", role="admin", scopes=ALL_SCOPES)
    assert admin.can_see_run(None) is True
