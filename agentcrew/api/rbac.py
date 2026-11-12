"""R25：多用户与 RBAC——密钥库、角色/作用域、主体解析与路由授权表。

设计取舍（与 E-3 的单密钥 ``APIKeyAuth`` 保持兼容）：

1. **密钥不落明文**：只存 ``scrypt(salt)`` 哈希；明文只在创建时打印一次。
   文件权限尽力收紧到 0600，且绝不进入 ledger / 事件 / 日志。
2. **角色 = 作用域集合**，可附加 ``scopes`` 做收窄或扩张；``custom`` 角色必须
   显式给 ``scopes``。每个路由声明所需作用域（:func:`required_scope`），
   中间件统一校验——授权规则集中在一处，不让各 handler 各判各的。
3. **run 归属**：API 创建的 run 在 run 目录写 ``owner.json``（key_id + name）；
   非 admin 只能看/操作 ``run_ids`` 白名单里或自己创建的 run。没有 owner.json
   的 run（CLI 建的）视作 ``local`` 所有，非 admin 不可见——宁可少给，不多给。
4. **审计**：审批裁决的 ``decided_by`` 一律取主体名，不再接受自由文本（legacy
   单密钥模式除外）；拒绝访问写 ``api_forbidden`` 事件（含主体与所需作用域，
   不含密钥）。

主体通过 :class:`contextvars.ContextVar` 传给 handler：连接各自跑在独立
asyncio task 里，ContextVar 的拷贝语义正好保证并发连接之间不串。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: 角色/作用域的规范名。新增作用域必须同时进 :data:`ALL_SCOPES` 与
#: :data:`ROLE_SCOPES`（admin 自动获得全部），并由 :func:`required_scope`
#: 在路由上显式引用——否则等于默认放行。
SCOPE_RUNS_READ = "runs:read"
SCOPE_RUNS_WRITE = "runs:write"
SCOPE_RUNS_ADMIN = "runs:admin"
SCOPE_APPROVALS_WRITE = "approvals:write"
SCOPE_TOOLS_READ = "tools:read"
SCOPE_TOOLS_TEST = "tools:test"
SCOPE_CONFIG_READ = "config:read"
SCOPE_KEYS_READ = "keys:read"
SCOPE_KEYS_WRITE = "keys:write"
#: C8：入站 Webhook 触发（HMAC 认证，不持 API Key 的外部系统用）。
SCOPE_WEBHOOK_TRIGGER = "webhook:trigger"

ALL_SCOPES: frozenset[str] = frozenset({
    SCOPE_RUNS_READ, SCOPE_RUNS_WRITE, SCOPE_RUNS_ADMIN,
    SCOPE_APPROVALS_WRITE, SCOPE_TOOLS_READ, SCOPE_TOOLS_TEST,
    SCOPE_CONFIG_READ, SCOPE_KEYS_READ, SCOPE_KEYS_WRITE,
    SCOPE_WEBHOOK_TRIGGER,
})

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "admin": ALL_SCOPES,
    "operator": frozenset({
        SCOPE_RUNS_READ, SCOPE_RUNS_WRITE, SCOPE_APPROVALS_WRITE,
        SCOPE_TOOLS_READ, SCOPE_TOOLS_TEST,
    }),
    "viewer": frozenset({SCOPE_RUNS_READ, SCOPE_TOOLS_READ}),
}

#: 密钥库目录名（相对 ``output_dir``）；``clean`` 只扫 run 目录，不会碰它。
AUTH_DIR_NAME = "_auth"
KEYS_FILE_NAME = "keys.json"

_KEY_PREFIX = "ac_"


class Principal(BaseModel):
    """一次已认证请求的主体。"""

    model_config = ConfigDict(frozen=True)

    name: str
    """身份名（审计与归属用）；legacy 单密钥为 ``legacy-key``。"""

    role: str
    scopes: frozenset[str]
    key_id: str = ""
    run_ids: frozenset[str] = frozenset()
    """可访问的 run 白名单；空集 = 仅自己创建的 run（admin 除外）。"""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin" or SCOPE_RUNS_ADMIN in self.scopes

    def allows(self, scope: str | None) -> bool:
        return scope is None or scope in self.scopes

    def can_see_run(self, owner: str | None) -> bool:
        """按归属判定可见性。``owner`` 为 owner.json 里的 ``name``（可空）。"""
        if self.is_admin:
            return True
        if self.run_ids:
            return owner is not None and (owner in self.run_ids
                                          or owner == self.name)
        return owner is not None and owner == self.name


class KeyRecord(BaseModel):
    """密钥库中的一条记录（**没有明文**）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    role: str
    scopes: list[str] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    key_hash: str
    salt: str
    created_at: str
    expires_at: str | None = None
    revoked_at: str | None = None
    last_used_at: str | None = None
    note: str = ""

    def effective_scopes(self) -> frozenset[str]:
        base = ROLE_SCOPES.get(self.role, frozenset())
        extra = {scope for scope in self.scopes if scope in ALL_SCOPES}
        return frozenset(base | extra)

    def to_principal(self) -> Principal:
        return Principal(
            name=self.name, role=self.role, scopes=self.effective_scopes(),
            key_id=self.id, run_ids=frozenset(self.run_ids))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hash_key(key: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.scrypt(key.encode("utf-8"), salt=salt,
                            n=2 ** 14, r=8, p=1, dklen=32)
    return digest.hex()


def default_key_store(output_dir: Path) -> KeyStore:
    return KeyStore(Path(output_dir) / AUTH_DIR_NAME / KEYS_FILE_NAME)


class KeyStore:
    """``keys.json`` 的读写与校验。原子写；缺文件视为空库。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._records: list[KeyRecord] | None = None
        self._mtime: float | None = None
        self._persist_after: dict[str, datetime] = {}

    # ── 读写 ──────────────────────────────────────────────────────────

    def load(self) -> list[KeyRecord]:
        """读库（按 mtime 失效）：CLI 新建 / 吊销的密钥无需重启服务即生效。"""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if self._records is not None and mtime == self._mtime:
            return list(self._records)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {"keys": []}
        records: list[KeyRecord] = []
        for item in raw.get("keys", []) if isinstance(raw, dict) else []:
            try:
                records.append(KeyRecord(**item))
            except (TypeError, ValueError):
                continue   # 坏记录跳过：密钥库不因一行损坏整体不可用
        self._records = records
        self._mtime = mtime
        return list(records)

    def save(self, records: Iterable[KeyRecord] | None = None) -> None:
        if records is not None:
            self._records = list(records)
        payload = {"keys": [record.model_dump(mode="json")
                            for record in self._records or []]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass   # Windows 上 chmod 只是尽力而为
        tmp.replace(self.path)
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None

    # ── 管理 ──────────────────────────────────────────────────────────

    def add(
        self,
        *,
        name: str,
        role: str = "operator",
        scopes: Iterable[str] = (),
        run_ids: Iterable[str] = (),
        expires_at: str | None = None,
        note: str = "",
    ) -> tuple[KeyRecord, str]:
        """新建密钥，返回 ``(记录, 明文)``——**明文只在这里出现一次**。"""
        name = name.strip()
        if not name:
            raise ValueError("密钥名不能为空")
        if role not in ROLE_SCOPES and role != "custom":
            raise ValueError(f"未知角色：{role!r}（可选 {', '.join(ROLE_SCOPES)}、custom）")
        unknown = {scope for scope in scopes if scope not in ALL_SCOPES}
        if unknown:
            raise ValueError(f"未知作用域：{', '.join(sorted(unknown))}")
        if role == "custom" and not list(scopes):
            raise ValueError("custom 角色必须显式给 --scope")
        # B-17：expires_at 落库前归一化为 naive UTC——带时区的 ISO 串此前会被
        # 原样存下，verify 里与 naive ``now`` 比较抛 TypeError、全站 500。
        normalized_expiry: str | None = None
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at)
                if expiry.tzinfo is not None:
                    expiry = expiry.astimezone(
                        timezone.utc).replace(tzinfo=None)
                normalized_expiry = expiry.isoformat(sep="T")
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"expires_at 不是合法时间：{expires_at!r}") from exc
        plaintext = _KEY_PREFIX + secrets.token_urlsafe(24)
        salt = secrets.token_hex(16)
        record = KeyRecord(
            id="k_" + secrets.token_hex(6),
            name=name, role=role, scopes=sorted(set(scopes)),
            run_ids=[str(item) for item in run_ids],
            key_hash=_hash_key(plaintext, salt), salt=salt,
            created_at=_now(), expires_at=normalized_expiry, note=note)
        records = self.load()
        if any(item.name == name for item in records):
            raise ValueError(f"密钥名已存在：{name!r}")
        records.append(record)
        self.save(records)
        return record, plaintext

    def revoke(self, ident: str) -> KeyRecord:
        records = self.load()
        for record in records:
            if ident in (record.id, record.name):
                if record.revoked_at is None:
                    record.revoked_at = _now()
                    self.save(records)
                return record
        raise ValueError(f"密钥不存在：{ident!r}")

    def find(self, ident: str) -> KeyRecord | None:
        for record in self.load():
            if ident in (record.id, record.name):
                return record
        return None

    # ── 认证 ──────────────────────────────────────────────────────────

    def verify(self, presented: str) -> Principal | None:
        """比对明文 → 主体；吊销 / 过期 / 不匹配一律 ``None``。"""
        if not presented:
            return None
        now = datetime.now()
        for record in self.load():
            if record.revoked_at is not None:
                continue
            if record.expires_at:
                try:
                    expiry = datetime.fromisoformat(record.expires_at)
                    # B-17：带时区的 ISO 串与 naive ``now`` 比较会抛 TypeError——
                    # 此前只 except ValueError，一条带 ``+08:00`` 的记录会让
                    # 全站所有请求 500。这里归一化为 naive UTC 再比。
                    if expiry.tzinfo is not None:
                        expiry = expiry.astimezone(
                            timezone.utc).replace(tzinfo=None)
                    if expiry < now:
                        continue
                except (ValueError, TypeError):
                    continue   # 时间是坏值 → 保守地不认这条
            if not secrets.compare_digest(
                    _hash_key(presented, record.salt), record.key_hash):
                continue
            self._touch(record)
            return record.to_principal()
        return None

    def _touch(self, record: KeyRecord) -> None:
        """记录 last_used_at；同一密钥最多每分钟落盘一次，避免每请求写文件。"""
        now = datetime.now()
        record.last_used_at = now.isoformat(timespec="seconds")
        last = self._persist_after.get(record.id)
        if last is not None and (now - last).total_seconds() < 60:
            return
        self._persist_after[record.id] = now
        try:
            self.save()
        except OSError:
            pass   # 审计字段落盘失败不影响认证


class MultiKeyAuth:
    """RBAC 认证器：多密钥 + legacy 单密钥（隐含 admin）+ 关闭。

    与 :class:`~agentcrew.api.auth.APIKeyAuth` 同形（``enabled`` /
    ``authenticate``），并额外提供 :meth:`principal` 返回完整主体。
    """

    def __init__(
        self,
        store: KeyStore | None = None,
        *,
        legacy_keys: Iterable[str] = (),
        required: bool = False,
    ) -> None:
        self.store = store
        self._legacy = tuple(key.strip() for key in legacy_keys if key.strip())
        self._required = required

    @property
    def enabled(self) -> bool:
        if self._required or self._legacy:
            return True
        store = self.store
        if store is None:
            return False
        return any(record.revoked_at is None for record in store.load())

    def principal(self, headers: dict[str, str]) -> Principal | None:
        """请求头 → 主体；``enabled`` 为假时返回匿名 admin（不启用=不拦截）。"""
        if not self.enabled:
            return Principal(name="anonymous", role="admin",
                             scopes=ALL_SCOPES)
        presented = extract_key(headers)
        if presented is None:
            return None
        if presented in self._legacy:
            return Principal(name="legacy-key", role="admin",
                             scopes=ALL_SCOPES, key_id="legacy")
        if self.store is not None:
            return self.store.verify(presented)
        return None

    def authenticate(self, headers: dict[str, str]) -> bool:
        return self.principal(headers) is not None


def extract_key(headers: dict[str, str]) -> str | None:
    """``Authorization: Bearer`` 或 ``X-API-Key``（与 E-3 同一约定）。"""
    authorization = headers.get("authorization", "")
    if authorization[:7].lower() == "bearer ":
        token = authorization[7:].strip()
        return token or None
    api_key = headers.get("x-api-key", "").strip()
    return api_key or None


# ── 路由 → 作用域 ──────────────────────────────────────────────────────

#: 路径模板 → 所需作用域。**未列入的路径默认要求登录但不要额外作用域**；
#: 新增端点时必须显式决定（契约守卫与 RBAC 守卫各自盯着自己的那一半）。
_ROUTE_SCOPES: tuple[tuple[str, str, str], ...] = (
    # (method, 正则, scope)
    ("GET", r"^/runs$", SCOPE_RUNS_READ),
    ("POST", r"^/runs$", SCOPE_RUNS_WRITE),
    ("GET", r"^/runs/[^/]+/status$", SCOPE_RUNS_READ),
    ("GET", r"^/runs/[^/]+/events$", SCOPE_RUNS_READ),
    ("GET", r"^/runs/[^/]+/ledger$", SCOPE_RUNS_READ),
    ("GET", r"^/runs/[^/]+/stats$", SCOPE_RUNS_READ),
    ("GET", r"^/runs/[^/]+/model$", SCOPE_RUNS_READ),
    ("GET", r"^/runs/[^/]+/approvals$", SCOPE_RUNS_READ),
    ("POST", r"^/runs/[^/]+/approvals$", SCOPE_APPROVALS_WRITE),
    ("POST", r"^/runs/[^/]+/cancel$", SCOPE_RUNS_WRITE),
    ("POST", r"^/runs/[^/]+/resume$", SCOPE_RUNS_WRITE),
    ("POST", r"^/runs/[^/]+/events$", SCOPE_RUNS_WRITE),
    ("POST", r"^/runs/[^/]+/agents$", SCOPE_RUNS_ADMIN),
    # B-21：GET 版此前漏登记 → required_scope 返回 None → Principal.allows(None)
    # 恒 True，读执行体绑定矩阵绕过了 RBAC。与 POST 同属 run 级只读信息。
    ("GET", r"^/runs/[^/]+/agents$", SCOPE_RUNS_READ),
    ("PUT", r"^/runs/[^/]+/model$", SCOPE_RUNS_ADMIN),
    ("POST", r"^/runs/[^/]+/tasks/[^/]+/agent$", SCOPE_RUNS_ADMIN),
    ("DELETE", r"^/runs/[^/]+$", SCOPE_RUNS_ADMIN),
    ("GET", r"^/providers(/.*)?$", SCOPE_CONFIG_READ),
    ("POST", r"^/providers/[^/]+/test$", SCOPE_CONFIG_READ),
    ("GET", r"^/tools$", SCOPE_TOOLS_READ),
    ("POST", r"^/tools/[^/]+/test$", SCOPE_TOOLS_TEST),
    # R26：流程编排——流程是 run 的定义，复用 runs 作用域（读/写/删）
    ("GET", r"^/flows$", SCOPE_RUNS_READ),
    ("POST", r"^/flows$", SCOPE_RUNS_WRITE),
    ("GET", r"^/flows/[^/]+$", SCOPE_RUNS_READ),
    ("POST", r"^/flows/validate$", SCOPE_RUNS_READ),
    ("DELETE", r"^/flows/[^/]+$", SCOPE_RUNS_ADMIN),
    ("GET", r"^/keys$", SCOPE_KEYS_READ),
    ("POST", r"^/keys$", SCOPE_KEYS_WRITE),
    ("DELETE", r"^/keys/[^/]+$", SCOPE_KEYS_WRITE),
    # C8：入站触发——认证方式是 HMAC（见 ``_webhook_trigger``），不走 API Key
    # 链；登记作用域是为了让 required_scope 有明确答案，custom 角色可显式持有。
    ("POST", r"^/webhooks/trigger$", SCOPE_WEBHOOK_TRIGGER),
)

_COMPILED = tuple((method, re.compile(pattern), scope)
                  for method, pattern, scope in _ROUTE_SCOPES)


def required_scope(method: str, path: str) -> str | None:
    """方法 + 路径（已去尾斜杠）→ 所需作用域；``None`` = 登录即可。"""
    for route_method, pattern, scope in _COMPILED:
        if route_method == method.upper() and pattern.match(path):
            return scope
    return None


#: 当前连接的主体（中间件写、handler 读）。每个连接是独立 task，ContextVar
#: 的拷贝语义保证并发请求之间不串。
_CURRENT: ContextVar[Principal | None] = ContextVar("agentcrew_principal", default=None)


def set_current(principal: Principal | None) -> None:
    _CURRENT.set(principal)


def current_principal() -> Principal:
    return _CURRENT.get() or Principal(
        name="anonymous", role="admin", scopes=ALL_SCOPES)


# ── run 归属 ──────────────────────────────────────────────────────────

OWNER_FILE_NAME = "owner.json"


def write_owner(run_dir: Path, principal: Principal) -> None:
    """API 创建 run 后写所有者（key_id + name），供后续可见性判定。"""
    if principal.name in ("anonymous", "legacy-key"):
        return   # 未启用 RBAC / 单密钥模式不需要归属标记
    payload = {
        "key_id": principal.key_id, "name": principal.name,
        "role": principal.role, "created_at": _now(),
    }
    try:
        (Path(run_dir) / OWNER_FILE_NAME).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass   # 归属标记是尽力而为：写失败时按「无主」处理（非 admin 不可见）


def read_owner(run_dir: Path) -> str | None:
    """读 run 的所有者名；没有 / 坏文件返回 ``None``（视作 local）。"""
    try:
        raw = json.loads((Path(run_dir) / OWNER_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = raw.get("name") if isinstance(raw, dict) else None
    return str(name) if name else None


def describe_scopes() -> list[dict[str, Any]]:
    """作用域与角色清单（给 CLI / 看板展示）。"""
    return [
        {"scope": scope, "roles": [role for role, scopes in ROLE_SCOPES.items()
                                   if scope in scopes]}
        for scope in sorted(ALL_SCOPES)
    ]
