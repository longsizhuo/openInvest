"""estimate_cost_cny 缓存命中计价（P2/P4）：命中 token 按 input_cache_hit 价，
其余按未命中价；不传 cache_hit 时全按未命中（保守上界，旧行为）。"""
from openinvest.core.llm_telemetry import estimate_cost_cny


def test_cache_hit_cheaper_than_miss():
    # v4-flash: miss ¥1/M, hit ¥0.02/M, output ¥2/M
    full_miss = estimate_cost_cny("deepseek-v4-flash", 1_000_000, 0)
    all_hit = estimate_cost_cny("deepseek-v4-flash", 1_000_000, 0, cache_hit_tokens=1_000_000)
    assert full_miss == 1.0
    assert all_hit == 0.02
    assert all_hit < full_miss  # 命中省 50×


def test_partial_hit_split():
    # 60 万命中 + 40 万未命中
    c = estimate_cost_cny("deepseek-v4-flash", 1_000_000, 500_000, cache_hit_tokens=600_000)
    expected = 400_000/1e6*1.0 + 600_000/1e6*0.02 + 500_000/1e6*2.0
    assert abs(c - round(expected, 6)) < 1e-9


def test_default_no_cache_is_upper_bound():
    # 不传 cache_hit → 全按未命中（旧行为不回归）
    assert estimate_cost_cny("deepseek-v4-flash", 1_000_000, 0) == 1.0


def test_cache_hit_clamped_to_input():
    # 命中数 > 输入数 → clamp，不会算出负的未命中
    c = estimate_cost_cny("deepseek-v4-flash", 1000, 0, cache_hit_tokens=999999)
    assert c == round(1000/1e6*0.02, 6)


def test_v4_pro_official_price():
    # 官方页：v4-pro 未命中输入 ¥3/M、输出 ¥6/M、命中 ¥0.025/M
    assert estimate_cost_cny("deepseek-v4-pro", 1_000_000, 0) == 3.0
    assert estimate_cost_cny("deepseek-v4-pro", 0, 1_000_000) == 6.0
    assert estimate_cost_cny("deepseek-v4-pro", 1_000_000, 0, cache_hit_tokens=1_000_000) == 0.025


if __name__ == "__main__":
    test_cache_hit_cheaper_than_miss()
    test_partial_hit_split()
    test_default_no_cache_is_upper_bound()
    test_cache_hit_clamped_to_input()
    test_v4_pro_official_price()
    print("ok")
