from agentcrew.models.pricing import MODEL_PRICING, estimate_cost


def test_known_model_costs_money():
    assert estimate_cost("gpt-4o-mini", 1_000_000, 0) > 0


def test_unknown_model_costs_zero_but_does_not_raise():
    assert estimate_cost("nobody-knows-this", 1_000_000, 1_000_000) == 0.0


def test_cost_scales_linearly():
    a = estimate_cost("gpt-4o-mini", 1000, 0)
    b = estimate_cost("gpt-4o-mini", 2000, 0)
    assert abs(b - 2 * a) < 1e-9


def test_prefix_matching_covers_versioned_names():
    assert estimate_cost("gpt-4o-mini-2024-07-18", 1000, 0) > 0


def test_longest_prefix_wins():
    # gpt-4o-mini 同时命中 "gpt-4o" 与 "gpt-4o-mini"，必须取最长前缀的单价。
    assert estimate_cost("gpt-4o-mini-2024-07-18", 1_000_000, 0) == 0.15


def test_pricing_table_is_non_empty():
    assert MODEL_PRICING
