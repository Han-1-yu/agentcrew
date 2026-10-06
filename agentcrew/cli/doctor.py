"""``doctor`` 子命令：配置 / 环境 / 依赖 / Provider 连通性的健康报告。

设计要点：

- **纯函数优先**：:func:`run_diagnostics` 收一份 :class:`~agentcrew.config.Settings`，
  吐出结构化的 :class:`DoctorReport`；Provider 连通性通过注入的 ``health_check``
  协程做（缺省用 :class:`~agentcrew.models.factory.ModelFactory` 的实现），测试里
  换一个假协程即可，不必真发网络请求。
- **warn 不阻断、fail 才阻断**：:attr:`DoctorReport.ok` 只看有没有 ``fail``；有
  ``warn`` 时整体仍 ``ok=True``（警告是「能用但不理想」，不该挡住用户跑 run）。
  这与 CLI 退出码一一对应：``ok`` → 0，有 ``fail`` → 3（``EXIT_CODES.config``）。
- **不静默**：每一条检查都落进 ``checks``，哪怕是 pass —— 用户要的是「我到底
  哪儿配好了、哪儿没配」，而不是一个孤零零的红绿。
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from agentcrew.config import Settings
from agentcrew.models.config import UnknownProviderError, parse_alias
from agentcrew.models.factory import HealthRecord, ModelFactory

#: 检查项状态：pass / warn / fail。warn 不阻断退出码，fail 阻断。
STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


class DoctorCheck(BaseModel):
    """单条诊断结果。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: str  # pass / warn / fail
    message: str


class DoctorReport(BaseModel):
    """整份诊断报告：``ok`` = 没有任何 ``fail``（warn 不计入）。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    checks: list[DoctorCheck]


#: 核心依赖：导入失败即 fail（引擎本来就 import 它们，缺了根本跑不起来）。
_REQUIRED_DEPS: tuple[tuple[str, str], ...] = (
    ("pydantic", "pydantic"),
    ("yaml", "pyyaml"),
    ("dotenv", "python-dotenv"),
    ("rich", "rich"),
)

#: 可选依赖：导入失败只 warn（没有也能跑核心流程）。
_OPTIONAL_DEPS: tuple[tuple[str, str], ...] = (
    ("aiohttp", "aiohttp"),
)

#: 不持钥的 provider（本地 / 离线 / CLI），缺 Key 不算错。
_KEYLESS_PROVIDERS: frozenset[str] = frozenset({"mock", "ollama", "cli"})


def _collect_aliases(settings: Settings) -> list[str]:
    """收集配置里出现的全部模型别名（默认 / 角色绑定 / 分层 / 故障转移）。

    去重保序：连通性探测与 Key 检查都按这份清单走。故障转移项可能是字符串
    或 ``{"alias": ...}`` 权重格式（P3-10），两种都认。
    """
    aliases: list[str] = []

    def add(value: Any) -> None:
        alias = str(value).strip() if value is not None else ""
        if alias and alias not in aliases:
            aliases.append(alias)

    add(settings.default_model)
    for alias in settings.role_bindings.values():
        add(alias)
    for alias in settings.model_tiers.values():
        add(alias)
    for backups in settings.model_failover.values():
        for entry in backups:
            if isinstance(entry, str):
                add(entry)
            elif isinstance(entry, dict):
                add(entry.get("alias"))
    return aliases


def _check_python_version() -> DoctorCheck:
    """Python >= 3.10（README 的运行时要求）。"""
    if sys.version_info >= (3, 10):
        return DoctorCheck(
            name="python",
            status=STATUS_PASS,
            message=f"Python {sys.version_info.major}.{sys.version_info.minor}."
                    f"{sys.version_info.micro}",
        )
    return DoctorCheck(
        name="python",
        status=STATUS_FAIL,
        message=(
            f"Python {sys.version_info.major}.{sys.version_info.minor} 过低："
            "AgentCrew 需要 >= 3.10"
        ),
    )


def _check_dependencies() -> DoctorCheck:
    """核心依赖可导入（fail）；可选依赖缺失只 warn。"""
    missing_required: list[str] = []
    missing_optional: list[str] = []
    for module, package in _REQUIRED_DEPS:
        try:
            importlib.import_module(module)
        except ImportError:
            missing_required.append(package)
    for module, package in _OPTIONAL_DEPS:
        try:
            importlib.import_module(module)
        except ImportError:
            missing_optional.append(package)
    if missing_required:
        return DoctorCheck(
            name="dependencies",
            status=STATUS_FAIL,
            message=f"缺少核心依赖：{', '.join(missing_required)}（pip install 它们）",
        )
    if missing_optional:
        return DoctorCheck(
            name="dependencies",
            status=STATUS_WARN,
            message=(
                "核心依赖齐备；可选依赖缺失（不影响核心流程）："
                + ", ".join(missing_optional)
            ),
        )
    return DoctorCheck(
        name="dependencies",
        status=STATUS_PASS,
        message="核心依赖均可导入",
    )


def _check_api_keys(settings: Settings) -> DoctorCheck:
    """至少一个「真要发请求」的 provider 配了 Key；mock 模式豁免。"""
    if settings.mock:
        return DoctorCheck(
            name="api_keys",
            status=STATUS_PASS,
            message="mock 模式：所有别名解析为 MockProvider，无需 API Key",
        )
    missing: list[str] = []
    for alias in _collect_aliases(settings):
        try:
            binding = parse_alias(alias, overrides=settings.provider_overrides)
        except (ValueError, UnknownProviderError):
            continue  # 别名非法由 config 检查项报告
        if binding.provider in _KEYLESS_PROVIDERS:
            continue
        if settings.api_key_for(binding.provider) is None:
            env = binding.api_key_env or "?"
            if env not in missing:
                missing.append(env)
    if not missing:
        return DoctorCheck(
            name="api_keys",
            status=STATUS_PASS,
            message="已为引用到的 provider 配置 API Key",
        )
    return DoctorCheck(
        name="api_keys",
        status=STATUS_FAIL,
        message=(
            "非 mock 模式但以下环境变量未设置："
            + ", ".join(missing)
            + "（至少配一个 provider 的 Key，或加 --mock 离线运行）"
        ),
    )


def _check_config(settings: Settings) -> DoctorCheck:
    """default_model 别名可解析；mcp_servers 形状正确。"""
    problems: list[str] = []
    try:
        parse_alias(settings.default_model, overrides=settings.provider_overrides)
    except (ValueError, UnknownProviderError) as exc:
        problems.append(f"default_model 别名非法：{exc}")
    if not isinstance(settings.mcp_servers, dict):
        problems.append("mcp_servers 必须是映射")
    if problems:
        return DoctorCheck(
            name="config", status=STATUS_FAIL, message="；".join(problems))
    return DoctorCheck(
        name="config",
        status=STATUS_PASS,
        message=(
            f"default_model={settings.default_model!r} 可解析；"
            f"mcp_servers {len(settings.mcp_servers)} 项"
        ),
    )


def _check_output_dir(settings: Settings) -> DoctorCheck:
    """output_dir 存在且可写；不存在则尝试创建。"""
    out = settings.output_dir
    if out.exists():
        if not out.is_dir():
            return DoctorCheck(
                name="output_dir",
                status=STATUS_FAIL,
                message=f"output_dir 已存在但不是目录：{out}",
            )
        if not os.access(out, os.W_OK):
            return DoctorCheck(
                name="output_dir",
                status=STATUS_FAIL,
                message=f"output_dir 不可写：{out}",
            )
        return DoctorCheck(
            name="output_dir",
            status=STATUS_PASS,
            message=f"output_dir 存在且可写：{out}",
        )
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DoctorCheck(
            name="output_dir",
            status=STATUS_FAIL,
            message=f"无法创建 output_dir {out}：{exc}",
        )
    return DoctorCheck(
        name="output_dir",
        status=STATUS_PASS,
        message=f"output_dir 已创建：{out}",
    )


def _health_to_check(alias: str, record: HealthRecord) -> DoctorCheck:
    """把一次健康探测结果映射成检查项。"""
    if record.status == "healthy":
        latency = f"{record.latency_ms:.0f}ms" if record.latency_ms is not None else "—"
        return DoctorCheck(
            name=f"provider:{alias}",
            status=STATUS_PASS,
            message=f"healthy（{latency}）",
        )
    if record.status == "degraded":
        latency = f"{record.latency_ms:.0f}ms" if record.latency_ms is not None else "—"
        return DoctorCheck(
            name=f"provider:{alias}",
            status=STATUS_WARN,
            message=f"degraded（{latency}，偏慢）",
        )
    detail = record.error or "未知错误"
    return DoctorCheck(
        name=f"provider:{alias}",
        status=STATUS_FAIL,
        message=f"unavailable：{detail}",
    )


async def run_diagnostics(
    settings: Settings,
    *,
    health_check: Callable[[str], Awaitable[HealthRecord]] | None = None,
) -> DoctorReport:
    """跑全部检查并汇成报告；``health_check`` 缺省用 ModelFactory 的实现。

    ``health_check`` 注入点：测试传一个假协程，避免真发网络请求；生产路径
    下缺省时构造 :class:`ModelFactory` 并逐别名探测（P2-11 的同一套
    healthy / degraded / unavailable 三级结论）。
    """
    checks: list[DoctorCheck] = [
        _check_python_version(),
        _check_dependencies(),
        _check_api_keys(settings),
        _check_config(settings),
        _check_output_dir(settings),
    ]

    aliases = _collect_aliases(settings)
    if health_check is None:
        factory = ModelFactory(settings)
        health_check = factory.health_check
    for alias in aliases:
        try:
            record = await health_check(alias)
        except Exception as exc:  # noqa: BLE001 —— 诊断命令绝不能因探测崩掉
            checks.append(
                DoctorCheck(
                    name=f"provider:{alias}",
                    status=STATUS_FAIL,
                    message=f"unavailable：{type(exc).__name__}: {exc}",
                )
            )
            continue
        checks.append(_health_to_check(alias, record))

    ok = not any(c.status == STATUS_FAIL for c in checks)
    return DoctorReport(ok=ok, checks=checks)


def print_report(report: DoctorReport) -> None:
    """把报告格式化打印到终端（✓/⚠/✗）。"""
    symbol = {STATUS_PASS: "✓", STATUS_WARN: "⚠", STATUS_FAIL: "✗"}
    for check in report.checks:
        print(f"{symbol.get(check.status, '?')} {check.name}: {check.message}")
    verdict = "通过" if report.ok else "存在失败项"
    print(f"\n总体：{verdict}（{len(report.checks)} 项检查）")
