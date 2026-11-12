"""G1 执行校验档（C3）：把「产物能不能跑」变成可自动判定的门禁结论。

**它解决的是什么问题。** G1 既有六类检查全部是静态的（契约、语法、占位符、
必需符号），G2 是「评审模型看代码打分」。两者都答不出一个最基本的问题：
**这份代码跑起来对不对？** 一份语法正确、符号齐全、评审也挑不出毛病的实现，
可以在 ``pytest`` 下第一个断言就崩——而系统此前从不执行自己生成的东西。

做法刻意做得最窄：

- **只在 G1 已经放行之后**才跑（结构都不对时没必要启容器）；
- **只跑任务自己交付的测试**：把该任务的产物原样铺进
  ``<run_dir>/verify/<task_id>/``，再用 pytest 跑目录下的 ``test_*.py`` /
  ``*_test.py``；一条测试都没有就直接跳过（记 ``skipped`` 原因，不假装通过）；
- **失败并入 G1 issue**：复用 ``run_static`` 的同一组字段与同一条返工通道
  （``REWORK`` → 重新执行），**不新增第二条返工路径**——多一条路径就多一套
  额度、审计与收敛语义要维护；
- **默认关闭**：``gate_config.exec_verify=true`` 才生效。原因是它会把「跑测试」
  带进每一条交付路径：时间成本真实存在（每条任务一次解释器/容器启动），而且
  测试本身可能是错的。

执行环境与 ``run_python`` / ``run_tests`` 共用同一个
:class:`~agentcrew.tools.sandbox.ExecutionBackend`：选了 docker 就进容器跑，
没有 docker 时装配期就报错，不会静默退回「在宿主上跑模型写的测试」。
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentcrew.artifacts.store import ArtifactStore
from agentcrew.task.task import Task
from agentcrew.tools.sandbox import ExecutionBackend, SandboxError

__all__ = ["ExecutionVerifier", "TEST_FILE_PATTERNS"]

#: 被视为「测试文件」的命名（pytest 默认口径的子集，够用且可预测）。
TEST_FILE_PATTERNS: tuple[str, ...] = ("test_*.py", "*_test.py")

#: 单个产物最多复制多少字节进校验目录（超出的不复制：跑不动就别跑）。
_MAX_ARTIFACT_BYTES = 256 * 1024

#: 回灌进 issue 的输出长度上限（issue 会进返工上下文，不能把几万行塞进去）。
_MAX_ISSUE_CHARS = 1200


class ExecutionVerifier:
    """在一个隔离目录里跑任务交付的测试，把失败转成 G1 可用的 issue/fix_hint。"""

    def __init__(
        self,
        *,
        backend: ExecutionBackend,
        workspace_root: Path,
        ledger: Any | None = None,
        timeout: float = 120.0,
        max_output_chars: int = _MAX_ISSUE_CHARS,
    ) -> None:
        self._backend = backend
        self._root = Path(workspace_root)
        self._ledger = ledger
        self._timeout = float(timeout)
        self._max_chars = int(max_output_chars)

    @property
    def backend_name(self) -> str:
        return self._backend.name

    async def check(
        self, task: Task, store: ArtifactStore, *, round_no: int = 1
    ) -> list[tuple[str, str]]:
        """跑一次执行校验；返回 ``[(issue, fix_hint)]``，空列表表示通过或跳过。"""
        started = time.perf_counter()
        scratch = self._root / task.id
        test_files = self._materialize(task, store, scratch)
        if not test_files:
            self._emit(
                task, round_no=round_no, status="skipped", reason="no_tests",
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            return []
        argv = [
            self._backend.python, "-m", "pytest", "-q", "--no-header",
            "-p", "no:cacheprovider", ".",
        ]
        try:
            result = await self._backend.run(
                argv, workdir=scratch, timeout=self._timeout)
        except SandboxError as exc:
            # 环境问题（docker 消失 / 装配错误）：按失败处理并显式说明，
            # 不做「跑不了就算过」的静默放行。
            self._emit(
                task, round_no=round_no, status="error", reason=str(exc),
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            return [(
                f"执行校验无法运行：{exc}",
                "修复执行后端（sandbox_backend / docker 可用性）后重试",
            )]
        output = _combine(result.stdout, result.stderr)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        self._emit(
            task, round_no=round_no,
            status="timeout" if result.timed_out else (
                "passed" if result.exit_code == 0 else "failed"),
            exit_code=result.exit_code,
            test_files=len(test_files),
            backend=result.backend,
            duration_ms=duration_ms,
            output=_tail(output, 2000),
        )
        if result.timed_out:
            return [(
                f"执行校验超时（>{self._timeout:g}s）：测试可能死循环或依赖外部服务",
                "让测试在无网络、无外部依赖的环境下可跑完，或缩小测试范围",
            )]
        if result.exit_code == 0:
            return []
        summary = _failure_summary(output)
        # issue 只放「一句话结论」，细节进 fix_hint：返工方读的是 fix_hints，
        # 而 issue 会被终端报告与跨层聚合单独渲染，塞进整段 traceback 很难读。
        detail = _tail(output, self._max_chars) if output.strip() else "（无输出）"
        return [(
            f"执行校验失败（pytest exit_code={result.exit_code}）：{summary}",
            f"修正实现或测试使 pytest 通过；失败详情：\n{detail}",
        )]

    # ---- 内部 ----

    def _materialize(
        self, task: Task, store: ArtifactStore, scratch: Path
    ) -> list[Path]:
        """把任务的产物铺进临时目录，返回其中的测试文件列表。"""
        produced = {record.path for record in store.by_task(task.id)}
        if not produced:
            return []
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, exist_ok=True)
        for spec in task.deliverables:
            if spec.path not in produced:
                continue
            try:
                text = store.read(spec.path)
            except (OSError, UnicodeDecodeError):
                continue
            if len(text.encode("utf-8", errors="replace")) > _MAX_ARTIFACT_BYTES:
                continue
            target = scratch / spec.path
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            except OSError:
                continue
        return sorted(
            path for pattern in TEST_FILE_PATTERNS
            for path in scratch.rglob(pattern) if path.is_file()
        )

    def _emit(self, task: Task, **payload: Any) -> None:
        if self._ledger is None:
            return
        self._ledger.emit("g1_exec_check", task_id=task.id, **payload)


def _combine(stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout.strip():
        parts.append(stdout.rstrip())
    if stderr.strip():
        parts.append(stderr.rstrip())
    return "\n".join(parts)


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"…（省略 {len(text) - limit} 字符）\n" + text[-limit:]


def _failure_summary(output: str) -> str:
    """从 pytest 输出里挑一句最能说明问题的：优先 failed 统计行，其次最后一行。"""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "（测试没有任何输出）"
    for line in reversed(lines):
        if "failed" in line or "error" in line.lower():
            return _tail(line, 300)
    return _tail(lines[-1], 300)


def _dedup(items: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    kept: list[tuple[str, str]] = []
    for issue, hint in items:
        if issue in seen:
            continue
        seen.add(issue)
        kept.append((issue, hint))
    return kept


async def run_exec_checks(
    verifier: ExecutionVerifier | None,
    task: Task,
    store: ArtifactStore,
    *,
    round_no: int,
) -> list[tuple[str, str]]:
    """门禁侧的统一入口：``verifier`` 为空时直接返回空列表（默认关闭）。"""
    if verifier is None:
        return []
    findings = await verifier.check(task, store, round_no=round_no)
    return _dedup(findings)
