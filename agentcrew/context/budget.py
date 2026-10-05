"""Token 预算：上下文组装的「试装」账本（spec §11.2）。

预算判定用 :func:`~agentcrew.utils.tokens.estimate_tokens` 的保守估算。语义是
**试装**：装得下才占用，装不下原样退回，后续更小的项因此仍有机会（spec §11.2
的六级降级顺序要求「跳过装不下的项，不中断后续项」）。
"""

from agentcrew.utils.tokens import estimate_tokens


class TokenBudget:
    """一次上下文包组装的共享预算。

    不变量：``used`` 永不超 ``limit``，``remaining`` 永不为负；被拒绝的
    :meth:`try_consume` 不改变 ``used``。``limit=0`` 时拒绝一切。
    """

    def __init__(self, limit: int) -> None:
        if limit < 0:
            raise ValueError(f"预算上限不能为负: {limit}")
        self._limit = limit
        self._used = 0

    @property
    def limit(self) -> int:
        """预算上限（token）。"""
        return self._limit

    @property
    def used(self) -> int:
        """已占用（token）。"""
        return self._used

    @property
    def remaining(self) -> int:
        """剩余额度（token），永不为负。"""
        return max(0, self._limit - self._used)

    def try_consume(self, text: str) -> bool:
        """试装一段文本：装得下就占用并返回 ``True``，否则返回 ``False`` 且不改 ``used``。"""
        return self.try_consume_tokens(estimate_tokens(text))

    def try_consume_tokens(self, tokens: int) -> bool:
        """按 token 数试装，语义与 :meth:`try_consume` 相同。

        供「按上限预留」的项使用：上游摘要按 spec §11.2 的 400 tok 截断上限计费，
        与摘要实际长度无关；按上限而非实际占用，预算就永远够容纳该摘要的截断版本。
        """
        if self._used + tokens <= self._limit:
            self._used += tokens
            return True
        return False
