"""Dreaming verdict-outcome 换源回归测试（2026-05-26）

锁住：light_sleep 从 verdict_review.jsonl 读、rem_sleep 按 (asset, verdict, regime)
聚合、宏观突变样本免责剔除、1d 窗口不参与（只看 7d/30d）。
"""
from __future__ import annotations

import json

import pytest

from core.memory_store import MemoryStore
from jobs import dreaming


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = MemoryStore(tmp_path / "memory")
    # regime 固定，避免测试拉网络
    monkeypatch.setattr(dreaming, "_decision_regime", lambda asset, date: "range_bound")
    return s


def _write_reviews(store: MemoryStore, rows):
    path = store.root / ".dreams" / "verdict_review.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")


def _review(asset, verdict, hit7, ret7, *, shock=False, date="2026-05-20"):
    # directions = 原始市场方向（verdict 无关），用 ret 符号近似（测试用）
    d = "up" if ret7 > 0 else ("down" if ret7 < 0 else "flat")
    return {
        "date": date, "asset": asset, "verdict": verdict, "confidence": 0.7,
        "macro_at_decision": {}, "actual_returns": {"1d": ret7, "7d": ret7, "30d": ret7},
        "hits": {"1d": True, "7d": hit7, "30d": hit7},
        "directions": {"1d": d, "7d": d, "30d": d},
        "macro_shock": {"detected": shock, "drivers": []}, "source": "live",
    }


def test_light_sleep_reads_verdict_review(store):
    _write_reviews(store, [_review("NDQ.AX", "HOLD", True, 0.01)])
    signals = dreaming.light_sleep(store)
    assert len(signals) == 1
    assert signals[0]["verdict"] == "HOLD"
    assert signals[0]["asset"] == "NDQ.AX"
    assert "regime" in signals[0]


def test_light_sleep_skips_unmatured(store):
    """没有 hits（窗口全未成熟）的样本不进信号流"""
    row = _review("GC=F", "TRIM", True, -0.02)
    row["hits"] = {}  # 无任何成熟窗口
    _write_reviews(store, [row])
    assert dreaming.light_sleep(store) == []


def test_rem_macro_shock_no_longer_excluded(store):
    """2026-05-27：macro_shock 免责已退役。shock 样本现在照常进模式学习桶。"""
    rows = [_review("NDQ.AX", "HOLD", True, 0.01) for _ in range(3)]
    rows += [_review("NDQ.AX", "HOLD", True, 0.01, shock=True) for _ in range(3)]
    _write_reviews(store, rows)
    signals = dreaming.light_sleep(store)
    candidates = dreaming.rem_sleep(store, signals)
    # macro_shock 不再剔除 → 6 条全进桶（旧行为是只进非 shock 的 3 条）
    hold = [c for c in candidates if c["verdict"] == "HOLD" and c["window_days"] == 7]
    assert hold and hold[0]["count"] == 6


def test_rem_only_7d_30d_not_1d(store):
    rows = [_review("NDQ.AX", "HOLD", True, 0.01) for _ in range(3)]
    _write_reviews(store, rows)
    candidates = dreaming.rem_sleep(store, dreaming.light_sleep(store))
    assert candidates  # 有候选
    assert all(c["window_days"] in (7, 30) for c in candidates)
    assert not any(c["window_days"] == 1 for c in candidates)


def test_score_verdict_ignores_abs_return(store):
    """verdict 路径评分只看命中率+样本，不被 abs(avg_return) 反向奖励"""
    # 命中率低但 avg_return 绝对值大（坏 HOLD）→ reliable 路径不该靠收益项刷分
    c_bad = {"verdict": "HOLD", "hit_rate": 0.2, "count": 10, "avg_return_pct": 12.0, "kind": "reliable"}
    c_good = {"verdict": "HOLD", "hit_rate": 0.9, "count": 10, "avg_return_pct": 0.5, "kind": "reliable"}
    assert dreaming._score(c_bad) < dreaming._score(c_good)
    # 旧 action 路径仍用老公式（不带 verdict 键）
    c_legacy = {"action": "bought", "hit_rate": 0.8, "count": 5, "avg_return_pct": 3.0}
    assert dreaming._score(c_legacy) > 0


def test_caution_no_downside_is_rejected_by_lift(store):
    """单向上涨 regime（市场全涨、base_down=0）里 HOLD 踏空 → 仍标 caution，
    但 lift-based 评分判为基率假象 → score=0（不固化）。这是 2026-05-27 修正的核心。"""
    # 9 条 HOLD：全没命中且市场涨 3% → missed_up=100%、base_down=0（全 up，无下行）
    rows = [_review("NDQ.AX", "HOLD", False, 0.03) for _ in range(9)]
    _write_reviews(store, rows)
    candidates = dreaming.rem_sleep(store, dreaming.light_sleep(store))
    hold7 = next(c for c in candidates if c["verdict"] == "HOLD" and c["window_days"] == 7)
    assert hold7["kind"] == "caution"          # 标签仍是 caution
    assert hold7["missed_up_rate"] == 1.0
    assert hold7.get("base_down", 0) == 0.0    # 该 regime 无真实下行
    assert dreaming._score(hold7) == 0.0       # lift-based 拒绝：踏空=单向基率假象


def test_caution_lift_based_scoring_unit():
    """_score 的 lift-based caution 直测：只有'真有下行 + HOLD 比基率更踏空'才得分。"""
    base = {"verdict": "HOLD", "kind": "caution", "count": 20, "hit_rate": 0.2}
    # (a) 无下行(base_down=0) → 拒绝
    assert dreaming._score({**base, "missed_up_rate": 0.9, "base_up": 0.9, "base_down": 0.0}) == 0.0
    # (b) 有下行但 HOLD 没比基率更踏空(lift<=0) → 拒绝
    assert dreaming._score({**base, "missed_up_rate": 0.50, "base_up": 0.55, "base_down": 0.30}) == 0.0
    # (c) 有真实下行 + 正 lift → 接受、得分随 lift 升高
    s = dreaming._score({**base, "missed_up_rate": 0.70, "base_up": 0.50, "base_down": 0.30})
    assert s > 0.0
    # (d) 缺基率数据 → 安全休眠 0
    assert dreaming._score({**base, "missed_up_rate": 0.9}) == 0.0


def test_hold_avoided_down_not_punished(store):
    """HOLD 没命中但市场跌（躲过下跌）→ 不算踏空，不标 caution"""
    rows = [_review("GC=F", "HOLD", False, -0.04) for _ in range(5)]
    _write_reviews(store, rows)
    candidates = dreaming.rem_sleep(store, dreaming.light_sleep(store))
    hold7 = next(c for c in candidates if c["verdict"] == "HOLD" and c["window_days"] == 7)
    assert hold7["missed_up_rate"] == 0.0       # 跌的不算踏空
    assert hold7["avoided_down_rate"] == 1.0
    assert hold7["kind"] == "reliable"          # 没踏空 → 不告诫


# ---------- crash 样本免责（B 组）----------

def _crash_review(asset, verdict, hit7, ret7, date="2026-05-20"):
    """带 regime_at_decision=crash 标记的样本（review 时已留痕）"""
    row = _review(asset, verdict, hit7, ret7, date=date)
    row["regime_at_decision"] = "crash"
    return row


def test_crash_sample_kept_in_jsonl_but_exempt_from_rem(store):
    """B5：decision_date regime=crash 的样本进了 verdict_review.jsonl + light_sleep 信号流
    （留痕），但不出现在 rem_sleep 候选桶里（免责，不进模式学习）。"""
    # 3 条 crash HOLD（应被剔除）+ 3 条正常 HOLD（应进桶）
    rows = [_crash_review("NDQ.AX", "HOLD", True, 0.01) for _ in range(3)]
    rows += [_review("NDQ.AX", "HOLD", True, 0.01) for _ in range(3)]
    _write_reviews(store, rows)

    # 留痕：6 条都进 light_sleep 信号流，crash 的打了 regime_crash=True
    signals = dreaming.light_sleep(store)
    assert len(signals) == 6
    crash_signals = [s for s in signals if s["regime_crash"]]
    assert len(crash_signals) == 3
    assert all(s["regime"] == ["crash"] for s in crash_signals)

    # 免责：rem_sleep 候选只来自 3 条非 crash 样本
    candidates = dreaming.rem_sleep(store, signals)
    hold7 = [c for c in candidates if c["verdict"] == "HOLD" and c["window_days"] == 7]
    assert hold7 and hold7[0]["count"] == 3, "crash 样本不应进 rem 桶"
    # 不存在以 crash 为 regime 的候选桶
    assert not any("crash" in (c.get("regime") or "") for c in candidates)


def test_verdict_review_serializes_regime_marker():
    """VerdictReview.regime_at_decision 会随 asdict 落进 jsonl（下游可读可排除）"""
    from dataclasses import asdict
    from jobs.verdict_review import VerdictReview
    rv = VerdictReview(
        date="2026-05-20", asset="NDQ.AX", verdict="HOLD", confidence=0.7,
        expected_direction="flat", macro_at_decision={}, regime_at_decision="crash",
    )
    d = asdict(rv)
    assert d["regime_at_decision"] == "crash"
