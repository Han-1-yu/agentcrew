"""模型计价表（spec §12.4）。

单位：USD / 1M tokens ``(输入单价, 输出单价)``。查表用**最长前缀匹配**，
因此 ``gpt-4o-mini-2024-07-18`` 会命中 ``gpt-4o-mini`` 而不是 ``gpt-4o``。
未知模型按 0 计但**照常记录 token 数** —— 估算成本绝不阻塞调用。
"""

MODEL_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    # Anthropic
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus": (15.00, 75.00),
    "claude-haiku": (1.00, 5.00),
    # DeepSeek
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算一次调用成本（USD）。

    取 ``MODEL_PRICING`` 中匹配 ``model`` 的**最长**前缀；无匹配返回 ``0.0``
    （从不抛异常，调用方仍会把 token 数记入 ledger）。
    """
    prefix = max((p for p in MODEL_PRICING if model.startswith(p)), key=len, default=None)
    if prefix is None:
        return 0.0
    in_price, out_price = MODEL_PRICING[prefix]
    return prompt_tokens / 1e6 * in_price + completion_tokens / 1e6 * out_price
