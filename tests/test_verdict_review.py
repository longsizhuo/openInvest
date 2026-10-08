"""verdict_review 数据质量回归测试

守三个曾导致学习信号脏掉的 bug：
- A: committee 文件名转义（GC=F→GC_F）无法反推 → 真实 symbol 解析 + holdings 兜底
- B: 黄金事后收益用原始 GC=F USD/oz 而非 CNY/克 → proxy-aware return
- C: forward window 未到期就被算（df[date<=target].iloc[-1] 塌缩成今日）→ 成熟度过滤
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from openinvest.jobs import verdict_review as vr


def _df(rows):
    """造一个 index=日期、含 Close 列的 DataFrame（rows=[(date_str, close)]）"""
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({"Close": [r[1] for r in rows]}, index=idx)


# ---------- Bug C: 成熟度过滤 ----------

def test_window_return_immature_window_returns_none(monkeypatch):
    """目标日落在未来 → 不产出（绝不拿今天收盘冒充 N 天后）"""
    today = datetime.now().date()
    decision = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    # 只有决议日附近的数据，没有 D+30 的数据
    series = _df([
        ((today - timedelta(days=2)).strftime("%Y-%m-%d"), 100.0),
        ((today - timedelta(days=1)).strftime("%Y-%m-%d"), 101.0),
        (today.strftime("%Y-%m-%d"), 102.0),
    ])
    monkeypatch.setattr(vr, "_closes", lambda s: series)
    # 30d 窗口未成熟 → None
    assert vr._window_return("NDQ.AX", {"proxy_kind": "direct"}, decision, 30) is None
    # 1d 窗口已成熟 → 有值
    assert vr._window_return("NDQ.AX", {"proxy_kind": "direct"}, decision, 1) is not None


def test_window_return_direct_native(monkeypatch):
    """direct 资产用原生币种比值"""
    series = _df([
        ("2026-04-01", 100.0),
        ("2026-04-08", 110.0),  # +10% 在 7d
    ])
    monkeypatch.setattr(vr, "_closes", lambda s: series)
    ret = vr._window_return("NDQ.AX", {"proxy_kind": "direct"}, "2026-04-01", 7)
    assert ret == pytest.approx(0.10, abs=1e-6)


# ---------- Bug B: 黄金 CNY/克 ----------

def test_window_return_gold_uses_cny_per_gram(monkeypatch):
    """gold_cny_per_gram：收益 = (GC=F × USDCNY) 比值，不是裸 USD/oz。

    构造：GC=F 涨 10%（4000→4400），但 USDCNY 跌 5%（7.0→6.65，人民币升值）。
    用户真实 CNY/克收益 = 1.10 × 0.95 - 1 = +4.5%，而非裸 USD/oz 的 +10%。
    """
    gc = _df([("2026-04-01", 4000.0), ("2026-05-01", 4400.0)])
    fx = _df([("2026-04-01", 7.0), ("2026-05-01", 6.65)])

    def fake_closes(sym):
        return fx if sym == "USDCNY=X" else gc

    monkeypatch.setattr(vr, "_closes", fake_closes)
    ret = vr._window_return("GC=F", {"proxy_kind": "gold_cny_per_gram"}, "2026-04-01", 30)
    assert ret == pytest.approx(1.10 * 0.95 - 1.0, abs=1e-4)  # +4.5%
    # 关键：绝不等于裸 USD/oz 的 +10%
    assert abs(ret - 0.10) > 0.04


# ---------- Bug A: symbol 反转义 ----------

def test_parse_symbol_line(tmp_path):
    """新文件的 **Symbol** 行被解析出来"""
    f = tmp_path / "GC_F.md"
    f.write_text(
        "# Committee: 伦敦金\n\n**Date**: 2026-05-13\n"
        "**Symbol**: GC=F\n**Verdict**: HOLD (confidence 0.65)\n",
        encoding="utf-8",
    )
    parsed = vr._parse_committee_file(f)
    assert parsed["symbol"] == "GC=F"
    assert parsed["verdict"] == "HOLD"


def test_review_one_resolves_via_holdings_map(tmp_path, monkeypatch):
    """旧文件（无 **Symbol** 行）靠 holdings 转义名映射拿回真实 symbol + proxy_kind"""
    date_dir = tmp_path / "2026-05-13"
    date_dir.mkdir()
    (date_dir / "GC_F.md").write_text(
        "# Committee: 伦敦金\n\n**Date**: 2026-05-13\n"
        "**Verdict**: TRIM (confidence 0.78)\n", encoding="utf-8",
    )
    gold_holding = {"symbol": "GC=F", "proxy_kind": "gold_cny_per_gram"}
    resolver = {"GC_F": gold_holding}

    captured = {}

    def fake_window_return(symbol, holding, decision_date, window):
        captured["symbol"] = symbol
        captured["proxy_kind"] = (holding or {}).get("proxy_kind")
        return -0.01

    monkeypatch.setattr(vr, "_window_return", fake_window_return)
    monkeypatch.setattr(vr, "_detect_macro_shock", lambda *a, **k: {"detected": False, "drivers": []})

    rv = vr.review_one(date_dir, "GC_F", resolver)
    assert rv is not None
    assert rv.asset == "GC=F"                          # 不是转义名 GC_F
    assert captured["symbol"] == "GC=F"                # 拉行情用真实 symbol
    assert captured["proxy_kind"] == "gold_cny_per_gram"  # proxy 信息透传给收益计算


# ---------- 污染/holdout 强制分桶 ----------

def _rv(date: str, verdict: str, hit: bool) -> "vr.VerdictReview":
    return vr.VerdictReview(
        date=date, asset="NDQ.AX", verdict=verdict, confidence=0.7,
        expected_direction=vr.EXPECTED_DIRECTION.get(verdict, "flat"),
        macro_at_decision={}, hits={"1d": hit, "7d": hit, "30d": hit},
        contaminated=date <= vr.CONTAMINATION_CUTOFF,
    )


def test_summarize_buckets_never_merge():
    """holdout 与 contaminated 分两桶，summarize 不产出任何跨桶 union 命中率。"""
    reviews = [_rv("2024-03-01", "BUY", True),   # contaminated (≤ cutoff)
               _rv("2025-06-02", "BUY", False)]  # holdout (> cutoff)
    s = vr.summarize(reviews)
    assert set(s) == {"total", "weekend_dup_excluded", "cutoff", "holdout", "contaminated"}
    assert s["total"] == 2
    assert s["holdout"]["n"] == 1 and s["contaminated"]["n"] == 1
    # 绝不存在合并成一个数的顶层命中率
    assert "by_window" not in s and "hit_rate" not in s
    assert s["contaminated"]["note"] == "含记忆穿越,非业绩"


def test_summarize_holdout_sub30_suppresses_rates():
    """holdout n<30 → 红线 #2：只留样本量，不出命中率数字；contaminated 桶不抑制。"""
    reviews = [_rv("2025-06-02", "BUY", True) for _ in range(5)] + \
              [_rv("2024-06-03", "BUY", True) for _ in range(5)]
    s = vr.summarize(reviews)
    assert s["holdout"]["rates_suppressed_sub30"] is True
    assert "hit_rate" not in s["holdout"]["by_window"].get("7d", {})  # 命中率被抑制
    assert s["holdout"]["by_window"]["7d"]["n"] == 5                  # 样本量仍保留
    # contaminated 桶从不抑制（本就标注非业绩）
    assert s["contaminated"]["rates_suppressed_sub30"] is False
    assert "hit_rate" in s["contaminated"]["by_window"]["7d"]


def test_summarize_excludes_weekend_dups():
    """D8：周末休市资产的周末决议（基准=周五收盘）不进命中率，只计数；加密周末照算。"""
    sat, mon = "2025-06-07", "2025-06-09"
    reviews = [_rv(mon, "BUY", True)] * 30 + [_rv(sat, "BUY", False)] * 10
    s = vr.summarize(reviews)
    assert s["total"] == 40 and s["weekend_dup_excluded"] == 10
    assert s["holdout"]["n"] == 30 and s["holdout"]["by_window"]["7d"]["hit_rate"] == 1.0
    btc = vr.VerdictReview(date=sat, asset="BTC-USD", verdict="HOLD", confidence=0.5,
                           expected_direction="flat", macro_at_decision={})
    assert vr.summarize([btc])["weekend_dup_excluded"] == 0


def test_window_return_past_side_guard(monkeypatch):
    """issue #179 P1-A⑤：决议日早于数据窗口首行 → None（跳过），
    绝不静默锚到窗口第一根算出错误收益。"""
    df = _df([("2026-06-01", 10.0), ("2026-06-02", 11.0), ("2026-06-03", 12.0)])
    monkeypatch.setattr(vr, "_closes", lambda s: df)
    direct = {"proxy_kind": "direct"}
    # 窗口内正常锚定：base=06-02 收盘 11 → target=06-03 收盘 12
    assert vr._window_return("X", direct, "2026-06-02", 1) == pytest.approx(12 / 11 - 1)
    # 未来侧（原有护栏）
    assert vr._window_return("X", direct, "2026-06-03", 30) is None
    # 过去侧：2025 年的决议日不得锚到 2026-06-01
    assert vr._window_return("X", direct, "2025-01-01", 1) is None
    # 非正 base（原 start<=0 护栏，迁入 forward_return 后仍成立）→ None 不除零
    monkeypatch.setattr(vr, "_closes", lambda s: _df([("2026-06-01", 0.0), ("2026-06-02", 1.0)]))
    assert vr._window_return("X", direct, "2026-06-01", 1) is None


def test_window_return_weekend_base_is_last_close_on_or_before(monkeypatch):
    """#234-2：hit-rate base 与概率表/path_review 同口径——base = 决议日**当日或之前**
    最后收盘（单一可信源 forward_return）。周六决议的 base 是周五收盘，周一跳空计入；
    旧口径（当日或之后首根=周一）会把跳空排除在外，1d 窗更塌成 base==target 恒 0。"""
    from openinvest.core.regime_probability import forward_return

    df = _df([
        ("2026-04-02", 99.0),   # 周四
        ("2026-04-03", 100.0),  # 周五
        ("2026-04-06", 110.0),  # 周一：跳空 +10%
        ("2026-04-13", 120.0),  # 下周一
    ])
    monkeypatch.setattr(vr, "_closes", lambda s: df)
    direct = {"proxy_kind": "direct"}
    sat = "2026-04-04"
    # 1d：target=周日→周一 110；base=周五 100 → +10%（旧口径 base=周一 → 0）
    assert vr._window_return("X", direct, sat, 1) == pytest.approx(0.10)
    # 7d：target=04-11(周六)→04-13 120；base=周五 100 → +20%（旧口径 120/110-1）
    assert vr._window_return("X", direct, sat, 7) == pytest.approx(0.20)
    # 与 path_review/概率表共用的 forward_return 逐位一致
    for w in (1, 7):
        assert vr._window_return("X", direct, sat, w) == forward_return("X", sat, w, closes=df["Close"])
    # 交易日决议：两口径本就重合，行为不变
    assert vr._window_return("X", direct, "2026-04-03", 1) == pytest.approx(0.10)

    # 黄金 CNY/克代理：两腿同口径各自锚定
    fx = _df([("2026-04-03", 7.0), ("2026-04-06", 7.0 * 1.02)])
    monkeypatch.setattr(vr, "_closes", lambda s: fx if s == "USDCNY=X" else df)
    ret = vr._window_return("GC=F", {"proxy_kind": "gold_cny_per_gram"}, sat, 1)
    assert ret == pytest.approx(1.10 * 1.02 - 1.0)
