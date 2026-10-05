"""零依赖的 token 估算（spec §11.2）。"""


def estimate_tokens(text: str) -> int:
    """保守启发式估算：``max(len(text) // 3, len(text.split()))``；空串返回 0。

    预算判定用估算值（偏悲观），实际消耗以 API 返回的 usage 为准。
    """
    if not text:
        return 0
    return max(len(text) // 3, len(text.split()))
