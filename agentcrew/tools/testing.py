"""执行验证工具 ``run_tests``（C3）：在沙箱里跑 pytest，把真实结果回灌给模型。

**为什么这是关键一步。** C3 之前，系统从不执行自己生成的东西：G1 只做静态
校验（语法 / 危险导入 / 占位符），G2 让评审模型「看代码打分」。也就是说，一份
语法正确但一跑就崩的代码，可以一路绿灯交付。Devin / OpenHands 的核心循环恰好
相反——写代码、跑测试、看报错、改。这个工具补的就是「跑」这一环。

三条边界：

- **在哪跑由后端决定**：默认 ``local``（与 ``run_python`` 同一条进程边界），
  显式 ``sandbox_backend=docker`` 时进容器（禁网、非 root、资源上限）。工具本身
  不判断后端，只拿 ``backend.python`` 拼 argv。
- **只跑工作区内的路径**：``path`` 走
  :func:`~agentcrew.tools.builtins.resolve_in_workdir`，越界 / 经符号链接一律
  拒绝；**不接受任意 pytest 参数**——``--rootdir=/etc`` 这类参数会把边界绕过去，
  所以本版只开放 ``path`` 与 ``timeout``。
- **输出截断**：pytest 的完整输出可能几万行，回灌只保留尾部
  ``max_output_chars``（报错信息在尾部），并显式标注省略了多少字符。

不写 ``.pytest_cache``（``-p no:cacheprovider``）：run 目录是要长期保留、可
``resume`` 的，测试跑一次就在里面留一堆缓存，`diff` / 拷贝都会莫名其妙。

本模块位于 ``tools`` 层，不依赖 ``runtime`` / ``crew`` / ``agents``。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from pydantic import Field

from agentcrew.tools.builtins import resolve_in_workdir
from agentcrew.tools.sandbox import ExecutionBackend, LocalBackend
from agentcrew.tools.tool import Tool

__all__ = ["RunTestsTool"]

#: 默认与最大的测试执行时限（秒）。CI 里跑整套测试可能更久，但那是运维脚本的
#: 事；Agent 在任务里跑的应该是「这个任务自己的用例」，分钟级足够。
DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 300.0

#: 回灌给模型的输出上限（字符，保留尾部）。
DEFAULT_OUTPUT_CHARS = 4000


class RunTestsTool(Tool):
    """在沙箱内运行 pytest，返回通过/失败摘要与失败输出尾部。"""

    name: str = "run_tests"
    description: str = (
        "在项目目录内运行 pytest，返回测试结果（通过/失败）与失败输出。"
        "path 指定测试文件或目录（默认 '.'）。用于写完代码后验证它真的能跑，"
        "而不是只看语法。失败时把报错原文带回。"
    )
    input_schema: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "path": {
                "type": "string", "maxLength": 300,
                "description": "项目内相对路径：测试文件或目录，默认 '.'",
            },
            "timeout": {
                "type": "number",
                "description": f"超时秒数，默认 {int(DEFAULT_TIMEOUT)}，"
                               f"上限 {int(MAX_TIMEOUT)}",
            },
        },
    })
    workdir: Path
    """执行根目录（构造期注入；测试路径不得越出这里）。"""
    backend: Any = None
    """C2 执行后端；``None`` = local（与引入后端前一致）。"""
    default_timeout: float = DEFAULT_TIMEOUT
    max_output_chars: int = DEFAULT_OUTPUT_CHARS

    @property
    def _backend(self) -> ExecutionBackend:
        backend = self.backend
        if backend is None:
            return LocalBackend()
        return backend

    async def run(self, path: str = ".", timeout: float | None = None,
                  **kwargs: Any) -> str:
        if not isinstance(path, str):
            return "错误：path 必须是字符串"
        target = resolve_in_workdir(self.workdir, path)
        if target is None:
            return f"错误：路径越界或非法：{path!r}（只允许项目目录内的相对路径）"
        if not target.exists():
            return f"错误：测试路径不存在：{path}"
        if timeout is None:
            limit = self.default_timeout
        else:
            try:
                limit = float(timeout)
            except (TypeError, ValueError):
                return "错误：timeout 必须是数值（秒）"
            if not math.isfinite(limit) or not 0 < limit <= MAX_TIMEOUT:
                return f"错误：timeout 必须落在 (0, {MAX_TIMEOUT:g}] 秒内"
        relative = "." if target == self.workdir else target.relative_to(
            self.workdir).as_posix()
        backend = self._backend
        argv = [
            backend.python, "-m", "pytest", "-q", "--no-header",
            "-p", "no:cacheprovider", relative,
        ]
        result = await backend.run(argv, workdir=self.workdir, timeout=limit)
        if result.timed_out:
            return f"错误：测试执行超时（>{limit:g}s），已终止"
        output = _combine(result.stdout, result.stderr).strip()
        # 解释器/容器都没起来（不是测试失败，是环境问题）：明确说出来。
        if result.exit_code < 0:
            return f"错误：{output or '无法启动测试进程'}"
        missing_pytest = "No module named pytest" in output
        body = _tail(output, self.max_output_chars) if output else "（无输出）"
        verdict = "通过" if result.exit_code == 0 else "失败"
        header = f"pytest {verdict}（exit_code={result.exit_code}，路径={relative}）"
        if missing_pytest:
            header += (
                "\n提示：当前执行环境没有安装 pytest；"
                "docker 后端请把 pytest 打进 sandbox_image"
            )
        return f"{header}\n{body}"


def _combine(stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout.strip():
        parts.append(stdout.rstrip())
    if stderr.strip():
        parts.append(stderr.rstrip())
    return "\n".join(parts)


def _tail(text: str, limit: int) -> str:
    """保留尾部：pytest 的失败摘要与断言详情都在最后。"""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"…（前面省略 {omitted} 字符）\n" + text[-limit:]
