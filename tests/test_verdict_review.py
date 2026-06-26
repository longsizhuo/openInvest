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

from jobs import verdict_review as vr


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
               _rv("2025-06-01", "BUY", False)]  # holdout (> cutoff)
    s = vr.summarize(reviews)
    assert set(s) == {"total", "cutoff", "holdout", "contaminated"}
    assert s["total"] == 2
    assert s["holdout"]["n"] == 1 and s["contaminated"]["n"] == 1
    # 绝不存在合并成一个数的顶层命中率
    assert "by_window" not in s and "hit_rate" not in s
    assert s["contaminated"]["note"] == "含记忆穿越,非业绩"


def test_summarize_holdout_sub30_suppresses_rates():
    """holdout n<30 → 红线 #2：只留样本量，不出命中率数字；contaminated 桶不抑制。"""
    reviews = [_rv("2025-06-01", "BUY", True) for _ in range(5)] + \
              [_rv("2024-06-01", "BUY", True) for _ in range(5)]
    s = vr.summarize(reviews)
    assert s["holdout"]["rates_suppressed_sub30"] is True
    assert "hit_rate" not in s["holdout"]["by_window"].get("7d", {})  # 命中率被抑制
    assert s["holdout"]["by_window"]["7d"]["n"] == 5                  # 样本量仍保留
    # contaminated 桶从不抑制（本就标注非业绩）
    assert s["contaminated"]["rates_suppressed_sub30"] is False
    assert "hit_rate" in s["contaminated"]["by_window"]["7d"]
