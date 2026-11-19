"""CLI 共享辅助：配置装配、闸门解析、数值校验、输出与 resume 提示。

从 ``main.py`` 拆出（R66f）；各命令模块共用，禁止反向 import ``main``。"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from agentcrew.config import GateMode, Settings, redact_overrides
from agentcrew.crew.crew import (
    EXIT_CODES,
)
from agentcrew.gate.cli_prompter import CliPrompter
from agentcrew.gate.deferred import DeferredPrompter
from agentcrew.gate.gatekeeper import NullPrompter, Prompter
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.checkpoint import (
    RunRecord,
)

#: 含人工卡点的 gate 模式（与 ``Gatekeeper`` 的 ``_PLAN_GATES`` / ``_DELIVERY_GATES`` 同源）。
_HUMAN_GATES: frozenset[str] = frozenset({"both", "plan", "delivery"})


#: ``resume`` 时参与「结果可比性」比对的配置键（spec §13.3 第 3 步：防换模型后结果不可比）。
#: 不比对 ``output_dir`` / ``gate`` / ``log_level`` 之类的运行环境字段：它们变了
#: 不影响产物含义，比进去只会逼用户无脑加 ``--force``。
_COMPARABLE_KEYS = (
    "default_model",
    "role_bindings",
    "provider_overrides",
    "context_budget_tokens",
    "mock",
)


#: CLI ``--image`` 的上限与白名单（与 API 侧 validate_images 同口径）。
_IMAGE_MAX_BYTES = 2_600_000


_IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}


def _load_cli_images(paths: list[str] | None) -> tuple[list[dict], str | None]:
    """读取 ``--image`` 指定的文件并转成 Crew 的 images 形状。

    返回 ``(images, 错误文案)``；错误文案非 None 时调用方 ``_fail``。
    """
    import base64 as _base64

    if not paths:
        return [], None
    if len(paths) > 4:
        return [], "--image 最多 4 张"
    images: list[dict] = []
    for raw_path in paths:
        path = Path(raw_path)
        mime = _IMAGE_MIME_BY_SUFFIX.get(path.suffix.lower())
        if mime is None:
            return [], (
                f"--image {raw_path}：不支持的格式 {path.suffix!r}"
                "（可用 png / jpg / webp / gif）")
        try:
            data = path.read_bytes()
        except OSError as exc:
            return [], f"--image {raw_path}：读取失败（{exc}）"
        if len(data) > _IMAGE_MAX_BYTES:
            return [], (f"--image {raw_path}：单张上限 2.6 MB，"
                        f"实际 {len(data) / 1e6:.1f} MB")
        images.append({
            "mime": mime,
            "data": _base64.b64encode(data).decode("ascii"),
        })
    return images, None


def _apply_sample(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """按 ``--sample`` 切换 mock 夹具并返回样例目标。

    样例是一份**离线回放**：目标与 ``plan_todo.json`` / ``responses/`` 强绑定，
    用真实 provider 跑就跑成了另一个项目。因此这里顺手把 ``mock`` 打开，
    而不是让用户先跑一次才发现「任务与夹具对不上」。

    返回 ``(goal, error)``：未给 ``--sample`` 时是 ``(None, None)``；
    给了但登记表里没有（或缺夹具）时是 ``(None, 错误文案)``，由调用方转成退出码 3。
    """
    name = getattr(args, "sample", None)
    if not name:
        return None, None
    try:
        from examples.samples import activate
    except ImportError as exc:  # 发行包可能不含 examples/，此时样例不可用
        return None, f"--sample 需要仓库内的 examples/samples：{exc}"
    try:
        spec = activate(name)
    except (KeyError, FileNotFoundError) as exc:
        # KeyError 的 str() 带外层引号，这里统一剥掉，和 `samples` 的错误口径一致
        return None, f"样例不可用：{str(exc).strip(chr(39) + chr(34))}"
    args.mock = True
    return spec.goal, None


def _settings_from(args: argparse.Namespace) -> Settings:
    """把 CLI 覆盖合并进五级配置（spec §17.1）。

    只写「命令行真的给了」的键：``store_true`` 的 ``False`` 缺省不能进 overrides，
    否则会把 ``AGENTCREW_MOCK=true`` 之类的环境变量反过来盖掉。
    """
    overrides: dict[str, Any] = {}
    if getattr(args, "mock", False):
        overrides["mock"] = True
    if getattr(args, "plan_only", False):
        overrides["plan_only"] = True
    if getattr(args, "debug_step", False):
        overrides["debug_step"] = True   # R20：store_true 的 False 缺省不进 overrides
    gate = _effective_gate(
        getattr(args, "gate", None), auto=getattr(args, "auto", False))
    if gate is not None:
        overrides["gate"] = gate
    if getattr(args, "max_concurrency", None) is not None:
        overrides["max_concurrency"] = args.max_concurrency
    if getattr(args, "default_model", None) is not None:
        overrides["default_model"] = args.default_model
    if getattr(args, "context_budget", None) is not None:
        overrides["context_budget_tokens"] = args.context_budget
    if getattr(args, "max_cost", None) is not None:
        overrides["max_cost_usd"] = args.max_cost
    if getattr(args, "max_llm_calls", None) is not None:
        overrides["max_llm_calls"] = args.max_llm_calls
    if getattr(args, "output_dir", None):
        overrides["output_dir"] = Path(args.output_dir)
    if getattr(args, "approval_mode", None):
        overrides["approval_mode"] = args.approval_mode
    return Settings.load(**overrides)


def _effective_gate(gate: GateMode | None, *, auto: bool) -> GateMode | None:
    """``--gate`` / ``--auto`` → 传给配置层的 gate 覆盖值（``None`` = 不覆盖）。

    ``--auto`` 与 ``--gate`` 同时给出时 ``--auto`` 胜出：它本身就是「等价于
    ``--gate none``」的缩写，两者写出不同的值属于自相矛盾的调用。
    """
    if auto:
        return "none"
    return gate


def _resolve_gate(
    gate: GateMode,
    *,
    interactive: bool,
    console: Console | None = None,
    catalog: RoleCatalog | None = None,
    approval_mode: str = "interactive",
) -> tuple[GateMode, Prompter, bool]:
    """gate 模式 → ``(生效的 gate, prompter, 是否降级)``。

    人工卡点 + 无 TTY 时**显式降级**为 ``none`` 并返回 ``downgraded=True``，由调用方
    打一行说明。本函数绝不返回「gate 仍含人工卡点 + NullPrompter」的组合：那正是
    Task 27 的构造期守卫要拦的错位（``Gatekeeper`` 会为此抛 ``ValueError``，因为
    「声称有人工卡点、实际全自动放行」比没有闸门更危险）。

    ``catalog`` 透传给 :class:`CliPrompter`：计划卡点的 ``[e] 编辑后执行`` 要用
    它校验编辑结果，自定义角色才不会被误判为未注册。
    """
    # R4：deferred 模式把「问人」外包给审批单，不需要 TTY，也不降级为全自动。
    # run 会在卡点挂起，人用 `approve` 在另一个进程裁决后 `resume` 继续。
    if approval_mode == "deferred" and gate in _HUMAN_GATES:
        return gate, DeferredPrompter(), False
    if gate not in _HUMAN_GATES:
        return gate, NullPrompter(), False
    if interactive:
        return gate, CliPrompter(console=console, catalog=catalog), False
    return "none", NullPrompter(), True


def _stdin_isatty() -> bool:
    """stdin 是否真终端；取不到（pytest / 重定向 / 无 stdin）一律当非 TTY。"""
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _invalid_numbers(args: argparse.Namespace) -> str | None:
    """数值开关的校验（argparse 只管类型，不管取值）；非法时返回错误消息。

    ``--max-cost`` 特别要挡 ``nan`` / ``inf``：预算判定是
    ``total_cost_usd > max_cost_usd``，与 NaN 比较恒为 False，配了上限等于没配；
    inf 同理。负值 / 负调用上限会让第一次 LLM 调用就以「预算耗尽」失败，用户
    看不出是自己把上限写成了负数。引擎侧负上限的既有语义由测试钉住，这里只
    在 CLI 入口拦住非法输入。
    """
    cmd = getattr(args, "command", "run")
    concurrency = getattr(args, "max_concurrency", None)
    if concurrency is not None and concurrency < 1:
        return f"{cmd}：--max-concurrency 必须 >= 1。"
    budget = getattr(args, "context_budget", None)
    if budget is not None and budget < 1:
        return f"{cmd}：--context-budget 必须 >= 1。"
    cost = getattr(args, "max_cost", None)
    if cost is not None and (not math.isfinite(cost) or cost < 0):
        return f"{cmd}：--max-cost 必须是 >= 0 的有限数。"
    calls = getattr(args, "max_llm_calls", None)
    if calls is not None and calls < 0:
        return f"{cmd}：--max-llm-calls 必须 >= 0。"
    # 2026-11-18 审计 P2-1：``--plan-only`` 的「与 --auto / --gate 互斥」此前
    # 只写在帮助文案里，实际静默接受（exit 0）：--auto / --gate 被忽略，用户以为
    # 进了全自动或交互闸门，resume 执行时却走配置默认 gate。参数之间的矛盾必须
    # 在开跑前说清楚，而不是靠用户读 help。
    if getattr(args, "plan_only", False):
        conflicts = [
            flag for flag, given in (
                ("--auto", getattr(args, "auto", False)),
                ("--gate", getattr(args, "gate", None)),
            ) if given
        ]
        if conflicts:
            return (f"{cmd}：--plan-only 与 {' / '.join(conflicts)} 互斥"
                    "（只出计划，不派发执行，闸门无从生效）。")
    return None


def _config_diff(snapshot: dict[str, Any], settings: Settings) -> list[str]:
    """配置快照 vs 当前配置的差异行（空列表 = 可比，spec §13.3 第 3 步）。

    两侧都过 :func:`~agentcrew.config.redact_overrides`：这条差异行会**打印到
    stderr**（还常常被重定向进 CI 日志），而 ``provider_overrides`` 是自由形状
    的用户输入 —— 旧 run.json 里可能留着本次修复之前写下的明文密钥。
    """
    current = settings.config_snapshot()
    return [
        f"{key}: {_redacted(snapshot.get(key))!r} -> "
        f"{_redacted(current.get(key))!r}"
        for key in _COMPARABLE_KEYS
        if _redacted(snapshot.get(key)) != _redacted(current.get(key))
    ]


def _redacted(value: Any) -> Any:
    """映射值先抹掉疑似密钥再比较/打印；其余类型原样返回。"""
    return redact_overrides(value) if isinstance(value, dict) else value


#: 差异键 → 能把 ``resume`` 拉回首次运行状态的 CLI 开关（``{value}`` 取快照里的值）。
#: 只列**真的存在开关**的键：``role_bindings`` / ``provider_overrides`` 只能改配置
#: 文件，硬编一条命令只会让人白敲一遍。
_RESUME_FLAG_TEMPLATES: dict[str, str] = {
    "default_model": "--default-model {value}",
    "context_budget_tokens": "--context-budget {value}",
}


def _sample_names_for(goal: str) -> list[str]:
    """目标文本与哪些预设样例逐字一致（按登记顺序）；无匹配返回空列表。

    样例 run 的夹具目录是进程内状态（``DEFAULT_FIXTURES_DIR``），不写进
    ``run.json``。所以「续跑一个样例 run」必须重新 ``--sample``：只加 ``--mock``
    能让配置比对通过，夹具却停在默认目录，回放出来的产物属于另一个项目
    （实测：logstat 的 run 被回放成 Todo 的 ``backend/app/*.py``，然后因
    「产出未在契约中声明的文件」失败）。

    返回**列表**而不是单个名字：``linear`` 与 ``revision`` 是同一句目标、同一份
    计划与产物，只有评审夹具不同——单看 ``run.json`` 无法分辨当时用的是哪个，
    调用方必须把这份不确定说出来，不能假装知道。
    """
    try:
        from examples.samples import SAMPLES
    except ImportError:      # 发行包可能不含 examples/
        return []
    return [spec.name for spec in SAMPLES.values() if spec.goal == goal]


def _resume_hint_notes(record: RunRecord) -> list[str]:
    """续跑提示里那些「命令本身说不清」的补充说明（无则空列表）。"""
    snapshot = record.config_snapshot or {}
    if not snapshot.get("mock"):
        return []
    names = _sample_names_for(record.project_goal)
    if len(names) < 2:
        return []
    return [
        "目标与多个样例一致（" + "、".join(names) + "）：它们共享同一句目标，"
        "只有评审夹具不同，请按首次运行所选的那个填 --sample。"
    ]


def _resume_hint(record: RunRecord, settings: Settings) -> str:
    """「照这条命令敲就能续跑」的命令串（补齐快照里与当前配置不同的开关）。

    ``resume`` 的拒绝文案过去只说「加 --force」——那等于劝用户放弃可比性；而
    正确动作往往是「带上一模一样的开关」。两个调用方：``resume`` 的失败提示与
    ``rewind`` 结尾的「下一步」。后者尤其重要：它过去打印一条裸命令，对任何
    ``--sample`` / ``--mock`` 起出来的 run 都是**必然失败**的下一步。
    """
    snapshot = record.config_snapshot or {}
    flags: list[str] = []
    if snapshot.get("mock"):
        samples = _sample_names_for(record.project_goal)
        # ``--sample`` 自带 ``--mock``，两者同时给反而重复。多个样例同目标时取
        # 第一个（能跑通），不确定性由 :func:`_resume_hint_notes` 单独说。
        flags.append(f"--sample {samples[0]}" if samples else "--mock")
    current = settings.config_snapshot()
    for key, template in _RESUME_FLAG_TEMPLATES.items():
        value = snapshot.get(key)
        if value in (None, ""):
            continue
        if _redacted(value) != _redacted(current.get(key)):
            flags.append(template.format(value=value))
    command = f"python main.py resume {record.run_id}"
    return f"{command} {' '.join(flags)}" if flags else command


def _run_dir(settings: Settings, run_id: str) -> Path:
    """定位 run 目录，并拦住任何会拼出 ``output_dir`` 之外的名字。

    ``CheckpointStore.__init__`` 只检查「目录存在」，不校验名字；``cancel`` 会往
    ``run.json`` 写，所以路径必须在进 I/O 之前就收进 output_dir 内（与
    ``checkpoint._assert_deletable`` 同一条边界）。
    """
    root = settings.output_dir.resolve()
    resolved = (settings.output_dir / run_id).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"非法 run_id: {run_id!r}（必须位于 {root} 之下）")
    return resolved


def _out(console: Console, text: str) -> None:
    """打印一行长文本；``soft_wrap`` 关掉 rich 的折行，路径 / run_id 不被截断。"""
    console.print(Text(text), soft_wrap=True)


def _fail(message: str) -> int:
    """错误一律走 stderr + 配置类退出码（spec §7.4 的「配置错误」）。"""
    print(message, file=sys.stderr)
    return EXIT_CODES["config"]

