"""模型层异常层次（spec §11.2 / §12.3 / P0-2 / P0-3）。

异常集中在一个**零依赖**模块里，而不是散落在 ``ModelFactory`` 所在的实现模块：
调用方（``Crew`` / ``Scheduler`` / CLI / API）需要按类型分流退出码，若异常定义
在 2000 行的实现模块里，任何一处 import 异常都会把整条 Provider 装配链拖进来，
也让「异常层次」这件事难以一眼看清。

层次（越往下越具体）::

    RuntimeError
    ├── ModelFactoryError            配置错误（缺 Key / 非法别名 / implementation 非法）
    │   ├── ContextBudgetError       上下文预算超出模型窗口（退出码 3）
    │   └── BudgetExhaustedError     成本 / 调用次数硬上限（退出码 1）
    └── ProviderUnavailableError     故障转移别名全部不可用（P0-2 的聚合结论）
"""

from __future__ import annotations

__all__ = [
    "SUGGESTED_BUDGET_HEADROOM",
    "BudgetExhaustedError",
    "ContextBudgetError",
    "ModelFactoryError",
    "ProviderUnavailableError",
]

#: 建议预算相对窗口上限预留的头部空间（spec §11.2 的建议值公式）。
SUGGESTED_BUDGET_HEADROOM = 1024


class ModelFactoryError(RuntimeError):
    """Provider 构造失败（缺 API Key、implementation 非法等）——属配置错误。"""


class ContextBudgetError(ModelFactoryError):
    """上下文预算 + 最大输出超出模型窗口（spec §11.2，退出码 3）。"""

    def __init__(
        self, alias: str, budget: int, window: int, suggested_budget: int
    ) -> None:
        self.alias = alias
        self.budget = budget
        self.window = window
        self.suggested_budget = suggested_budget
        super().__init__(
            f"上下文预算 {budget} tokens 对模型窗口过大：alias={alias!r} 的 "
            f"max_context_tokens={window}，建议把 AGENTCREW_CONTEXT_BUDGET 调低到 "
            f"{suggested_budget}（= 窗口 - max_output_tokens - "
            f"{SUGGESTED_BUDGET_HEADROOM}）")


class ProviderUnavailableError(RuntimeError):
    """Provider 不可用错误标记类（P0-2 故障转移）。

    503/429/连接超时/读取超时等瞬时故障在 Provider 内部重试耗尽后，以
    :class:`~agentcrew.utils.retry.TransientError`（openai_compat / anthropic）
    或 :class:`TimeoutError` / :class:`ConnectionError`（cli）传播。工厂的
    故障转移循环捕获这些类型，依次尝试备用别名；全部备用耗尽时把最后一次
    错误包装成 ``ProviderUnavailableError`` 抛出，便于调用方区分「模型不可用」
    与「执行失败」。
    """


class BudgetExhaustedError(ModelFactoryError):
    """成本预算耗尽（P0-3）：累计成本或 LLM 调用次数超出硬上限。

    ``_account()`` 在每次记账后检查；超限即抛，Crew 捕获后优雅收尾
    （等在跑任务完成，不再派发新任务），记 ``budget_exhausted`` 事件，退出码 1。
    携带当前累计值与上限，便于日志定位。
    """

    def __init__(self, *, kind: str, current: float, limit: float) -> None:
        self.kind = kind
        self.current = current
        self.limit = limit
        super().__init__(
            f"预算耗尽：{kind} 累计 {current} 已达上限 {limit}")
