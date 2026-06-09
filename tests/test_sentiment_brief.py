"""单测：市场情绪表盘（utils/sentiment.py）

红线验证：
1. VIX 分位是确定性主信号，纯算术正确
2. CNN 不可达时 graceful 跳过，VIX 分位照常输出（绝不单点故障）
3. INDEP_DEFENSE_FLAG 在 VIX 高位置 on（独立于 regime 的快速崩盘哨兵）
4. VIX 都拿不到 → 整块降级 ""（保持 graceful loader 契约）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _fake_vix_df(values):
    return pd.DataFrame(
        {"Close": values},
        index=pd.date_range("2024-01-01", periods=len(values)),
    )


def test_vix_high_percentile_triggers_extreme_fear_and_defense_flag(monkeypatch):
    """VIX 当前=近2年最高 → 分位 100% → extreme_fear + INDEP_DEFENSE_FLAG=on"""
    import utils.exchange_fee as ef
    # 升序，最后一个最大 → 当前在最高位
    monkeypatch.setattr(ef, "get_history_data",
                        lambda *a, **k: _fake_vix_df(list(range(10, 60))))
    from utils.sentiment import build_sentiment_brief

    brief = build_sentiment_brief(cnn_enabled=False)
    assert "FEAR_GREED_GAUGE:" in brief
    assert "extreme_fear" in brief
    assert "分位 100%" in brief
    assert "INDEP_DEFENSE_FLAG: on" in brief


def test_vix_low_percentile_is_greed_and_defense_off(monkeypatch):
    """VIX 当前=近2年最低 → 分位低 → greed/extreme_greed + 防御 off"""
    import utils.exchange_fee as ef
    # 降序，最后一个最小 → 当前在最低位
    monkeypatch.setattr(ef, "get_history_data",
                        lambda *a, **k: _fake_vix_df(list(range(60, 10, -1))))
    from utils.sentiment import build_sentiment_brief

    brief = build_sentiment_brief(cnn_enabled=False)
    assert "extreme_greed" in brief or "greed" in brief
    assert "INDEP_DEFENSE_FLAG: off" in brief


def test_cnn_unreachable_does_not_break_vix(monkeypatch):
    """CNN 抓取失败 → graceful 跳过 CNN 行，VIX 分位照常输出（绝不单点故障）"""
    import utils.exchange_fee as ef
    import utils.sentiment as st
    monkeypatch.setattr(ef, "get_history_data",
                        lambda *a, **k: _fake_vix_df(list(range(10, 60))))
    # 模拟 CNN 端点挂掉
    monkeypatch.setattr(st, "fetch_cnn_fear_greed", lambda *a, **k: None)

    brief = build = st.build_sentiment_brief(cnn_enabled=True)
    assert "FEAR_GREED_GAUGE:" in brief, "CNN 挂了 VIX 分位仍必须输出"
    assert "CNN_FNG:" not in brief, "CNN 不可达时不应出现 CNN 行"


def test_cnn_reachable_appends_line(monkeypatch):
    """CNN 可达 → 附加 CNN_FNG 行（锦上添花）"""
    import utils.exchange_fee as ef
    import utils.sentiment as st
    monkeypatch.setattr(ef, "get_history_data",
                        lambda *a, **k: _fake_vix_df(list(range(10, 60))))
    monkeypatch.setattr(st, "fetch_cnn_fear_greed", lambda *a, **k: (25, "extreme_fear"))

    brief = st.build_sentiment_brief(cnn_enabled=True)
    assert "CNN_FNG: 25 (extreme_fear)" in brief


def test_event_stance_aggregation(monkeypatch):
    """event_brief 非空 → 数 risk/opportunity 标签给净情绪行（纯计数，无新 IO）"""
    import utils.exchange_fee as ef
    monkeypatch.setattr(ef, "get_history_data",
                        lambda *a, **k: _fake_vix_df([20] * 40))
    from utils.sentiment import build_sentiment_brief

    event_brief = (
        "[2026-05-13] [risk/high] [NDQ.AX]\nbad news\n\n"
        "[2026-05-12] [risk/mid] [GC=F]\nmore risk\n\n"
        "[2026-05-11] [opportunity/mid] [GC=F]\ngood news\n"
    )
    brief = build_sentiment_brief(event_brief, cnn_enabled=False)
    assert "EVENT_STANCE: net risk (risk=2 opportunity=1" in brief


def test_vix_unavailable_degrades_to_empty(monkeypatch):
    """VIX 都拿不到（空 df）→ 整块返回 ""（graceful loader 契约）"""
    import utils.exchange_fee as ef
    monkeypatch.setattr(ef, "get_history_data", lambda *a, **k: pd.DataFrame())
    from utils.sentiment import build_sentiment_brief

    assert build_sentiment_brief(cnn_enabled=True) == ""


def test_vix_fetch_exception_degrades_to_empty(monkeypatch):
    """VIX 拉取抛异常 → graceful 返回 ""（不阻断 committee）"""
    import utils.exchange_fee as ef

    def boom(*a, **k):
        raise RuntimeError("yfinance down")
    monkeypatch.setattr(ef, "get_history_data", boom)
    from utils.sentiment import build_sentiment_brief

    assert build_sentiment_brief() == ""


def test_fetch_cnn_never_raises(monkeypatch):
    """fetch_cnn_fear_greed 在 urlopen 抛任意异常时返回 None，绝不抛"""
    import utils.sentiment as st

    def boom(*a, **k):
        raise OSError("DNS fail")
    monkeypatch.setattr(st.urllib.request, "urlopen", boom)
    assert st.fetch_cnn_fear_greed(timeout=1) is None
