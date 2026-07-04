"""独立快崩防御测试 — VIX 哨兵 / ATR 飙升 → 确定性买侧降级。

背景（2026-06 消融结论）：MA120 regime 看不见快速崩盘（COVID 全程 uptrend），
原 crash 锁因双条件（ATR + 30d 回撤确认）永不触发。防御必须独立于 regime、
只取快腿，且确定性后处理（CIO SKILL 的降级规则从 prompt 搬进代码强制）。
只拦买侧（BUY/ACCUMULATE），不强制卖出——卖出判断留给委员会。
"""
from __future__ import annotations

import pytest

from openinvest.core.committee import atr_defense_from_text, parse_cio_memo
from openinvest.core.config import reset_config


@pytest.fixture(autouse=True)
def _reset_config():
    reset_config()
    yield
    reset_config()


def _memo(verdict: str, alloc: int = 5000) -> str:
    return (
        f"VERDICT: {verdict}\nCONFIDENCE: 0.6\nDOMINANT_VIEW: quant\n"
        f"SUGGESTED_ALLOC_CNY: {alloc}"
    )


# ---------- parse_cio_memo 确定性降级 ----------

def test_defense_downgrades_buy_to_accumulate():
    r = parse_cio_memo(_memo("BUY"), defense_flag_on=True)
    assert r["verdict"] == "ACCUMULATE"
    assert r["_original_verdict"] == "BUY"
    assert r["_defense_downgrade"] == "buy_to_accumulate"
    # BUY→ACCUMULATE 只降一级，alloc 保留（Sanity 2 已 clamp）
    assert r["alloc_cny"] == 5000


def test_defense_downgrades_accumulate_to_hold_and_zeroes_alloc():
    r = parse_cio_memo(_memo("ACCUMULATE"), defense_flag_on=True)
    assert r["verdict"] == "HOLD"
    assert r["_defense_downgrade"] == "accumulate_to_hold"
    assert r["alloc_cny"] == 0
    assert r["_original_alloc"] == 5000


def test_defense_leaves_hold_trim_sell_untouched():
    """防御只拦加仓，不强制卖出（VIX 飙升常在恐慌底部，强制卖=高买低卖）"""
    for v in ("HOLD", "TRIM", "SELL"):
        r = parse_cio_memo(_memo(v, alloc=0), defense_flag_on=True)
        assert r["verdict"] == v, v
        assert "_defense_downgrade" not in r, v


def test_no_defense_no_downgrade():
    r = parse_cio_memo(_memo("ACCUMULATE"), defense_flag_on=False)
    assert r["verdict"] == "ACCUMULATE"
    assert "_defense_downgrade" not in r


def test_defense_stacks_with_sanity1_overconfident_buy():
    """纵深：BUY(conf 0.99) 先被 Sanity 1 降 ACCUMULATE，防御再降 HOLD"""
    memo = "VERDICT: BUY\nCONFIDENCE: 0.99\nDOMINANT_VIEW: quant\nSUGGESTED_ALLOC_CNY: 5000"
    r = parse_cio_memo(memo, defense_flag_on=True)
    assert r["verdict"] == "HOLD"
    assert r["_original_verdict"] == "BUY"  # 审计链保留最早原值
    assert r["_defense_downgrade"] == "accumulate_to_hold"
    assert r["alloc_cny"] == 0


# ---------- 黄金防御分批 DCA（2026-06-13 裁决，wiki18 §5）----------
# defense_dca 非 None = 黄金且启用 → 防御触发时不全拦，放行一批(×1/3) or 按 spacing/quota 拦。

def test_gold_dca_tranche_releases_one_third():
    """放行批：BUY/ACCUMULATE 都改 ACCUMULATE 且 alloc=意图×fraction，不再全拦"""
    gate = {"allow": True, "fraction": 0.3333, "reason": "first", "tranche_idx": 1}
    r = parse_cio_memo(_memo("BUY", alloc=9000), defense_flag_on=True, defense_dca=gate)
    assert r["verdict"] == "ACCUMULATE"
    assert r["_original_verdict"] == "BUY"
    assert r["_defense_dca"] == "tranche"
    assert r["_defense_dca_tranche_idx"] == 1
    assert r["_original_alloc"] == 9000
    assert r["alloc_cny"] == round(9000 * 0.3333)   # 放行 1/3，不是全拦
    assert "_defense_downgrade" not in r            # 走 DCA 分支，不走旧全拦


def test_gold_dca_blocked_holds_and_zeroes_alloc():
    """暂拦批（spacing/quota 未满）：HOLD + alloc 0，但留痕原值供踏空账本回算"""
    gate = {"allow": False, "fraction": 0.3333, "reason": "spacing", "tranche_idx": 2}
    r = parse_cio_memo(_memo("ACCUMULATE", alloc=9000), defense_flag_on=True, defense_dca=gate)
    assert r["verdict"] == "HOLD"
    assert r["alloc_cny"] == 0
    assert r["_defense_dca"] == "blocked_spacing"
    assert r["_original_alloc"] == 9000
    assert "_defense_downgrade" not in r


def test_gold_dca_none_falls_back_to_full_block():
    """defense_dca=None（非黄金/未启用）→ 旧全拦行为完全不变"""
    r = parse_cio_memo(_memo("ACCUMULATE", alloc=5000), defense_flag_on=True, defense_dca=None)
    assert r["verdict"] == "HOLD"
    assert r["_defense_downgrade"] == "accumulate_to_hold"
    assert "_defense_dca" not in r


def test_gold_dca_only_applies_when_defense_fires():
    """defense_flag_on=False 时，即便传了 gate 也不动裁决（闸只在防御触发时生效）"""
    gate = {"allow": True, "fraction": 0.3333, "reason": "first", "tranche_idx": 1}
    r = parse_cio_memo(_memo("BUY", alloc=9000), defense_flag_on=False, defense_dca=gate)
    assert r["verdict"] == "BUY"
    assert "_defense_dca" not in r
    assert r["alloc_cny"] == 9000


def test_gold_dca_leaves_sell_side_untouched():
    """分批只管买侧；HOLD/TRIM/SELL 不受 gate 影响"""
    gate = {"allow": True, "fraction": 0.3333, "reason": "first", "tranche_idx": 1}
    for v in ("HOLD", "TRIM", "SELL"):
        r = parse_cio_memo(_memo(v, alloc=0), defense_flag_on=True, defense_dca=gate)
        assert r["verdict"] == v, v
        assert "_defense_dca" not in r, v


# ---------- atr_defense_from_text（coordinator transcript 路径） ----------
# 通用口径（无 per-asset 数字）：波动突变比 atr_spike_ratio（当日 ATR% / 自身
# 近 1 年滚动中位）≥ sentiment.atr_defense_spike_ratio（默认 2.0=波动翻倍）

BRIEF_TRIGGERED = (
    "REGIME: uptrend\n"
    "REASON: MA20 高于 MA120 4%（快崩中 MA 滞后仍显示 uptrend）\n"
    "INPUTS: ma20=100.0000, ma120=96.0000, atr_pct=6.2000, "
    "atr_spike_ratio=2.5000, price_quantile_2y=0.9000\n"
    "THRESHOLDS: trend_ma_spread_pct=3.00, crash_atr_pct_min=5.00\n"
    "STRATEGY_HINT: ..."
)


def test_atr_defense_from_text_triggers():
    """突变比 2.5 ≥ 默认线 2.0 → 触发"""
    assert atr_defense_from_text(BRIEF_TRIGGERED) is True


def test_atr_defense_from_text_below_threshold():
    calm = BRIEF_TRIGGERED.replace("atr_spike_ratio=2.5000", "atr_spike_ratio=1.1000")
    assert atr_defense_from_text(calm) is False


def test_atr_defense_from_text_threshold_from_config():
    """防御线走 config（sentiment.atr_defense_spike_ratio），抬线后同文本不触发"""
    from openinvest.core.config import set_config_override
    set_config_override({"sentiment": {"atr_defense_spike_ratio": 3.0}})
    assert atr_defense_from_text(BRIEF_TRIGGERED) is False  # 2.5 < 3.0


def test_atr_defense_from_text_missing_graceful():
    assert atr_defense_from_text("") is False
    assert atr_defense_from_text("no regime data") is False
    # 老 transcript 只有 atr_pct 没有 atr_spike_ratio → 不触发（graceful）
    assert atr_defense_from_text("INPUTS: atr_pct=9.0000") is False
    # atr_spike_ratio=None（样本不足）打进 INPUTS 行 → 正则不匹配数字 → 不触发
    assert atr_defense_from_text("INPUTS: atr_spike_ratio=None") is False


# ---------- 防御线与 crash 分类解耦（通用口径下结构性成立） ----------

def test_defense_spike_ratio_decoupled_from_crash_classification():
    """调 sentiment.atr_defense_spike_ratio 不影响 classify_regime"""
    from openinvest.core.config import set_config_override
    from openinvest.core.regime import classify_regime

    metrics = {
        "ma20": 100.0, "ma120": 96.0, "atr_pct": 2.0, "atr_spike_ratio": 2.4,
        "price_quantile_2y": 0.5, "return_30d": -0.05,
        "rebound_off_30d_low": None,
    }
    before = classify_regime(metrics, symbol="NDQ.AX")["regime"]
    set_config_override({"sentiment": {"atr_defense_spike_ratio": 1.0}})
    after = classify_regime(metrics, symbol="NDQ.AX")["regime"]
    assert after == before, "防御线居然影响了 regime 分类 — 解耦被破坏"
    assert after != "crash"


def test_atr_spike_ratio_metric_computed():
    """compute_metrics 输出 atr_spike_ratio：波动翻倍场景 ratio 显著 >1"""
    import numpy as np
    import pandas as pd
    from openinvest.utils.market_metrics import compute_metrics

    n = 400
    rng = np.random.default_rng(7)
    # 前 370 天平静（日波动 ~0.5%），最后 30 天波动放大 4 倍（快崩场景）
    rets = np.concatenate([
        rng.normal(0, 0.005, n - 30), rng.normal(0, 0.02, 30),
    ])
    close = pd.Series(100 * np.cumprod(1 + rets))
    df = pd.DataFrame({"Close": close})
    m = compute_metrics(df)
    assert m["atr_spike_ratio"] is not None
    assert m["atr_spike_ratio"] >= 2.0, f"波动 4 倍场景 ratio 应 ≥2，实际 {m['atr_spike_ratio']:.2f}"

    # 样本不足（<120) → None（graceful）
    m_small = compute_metrics(df.iloc[:60])
    assert m_small["atr_spike_ratio"] is None
