from agentcrew.utils.tokens import estimate_tokens


def test_empty_text_is_zero():
    assert estimate_tokens("") == 0


def test_estimate_is_conservative_for_chinese():
    # 中文按 len//3 估算；100 个汉字应落在 30..100 之间
    assert 30 <= estimate_tokens("测" * 100) <= 100


def test_estimate_scales_with_length():
    assert estimate_tokens("a" * 300) > estimate_tokens("a" * 30)
