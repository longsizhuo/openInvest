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


def test_vix_thresholds_come_from_config(monkeypatch):
    """阈值走 config (sentiment 节)：抬高 vix_defense_quantile 后同一数据哨兵转 off。

    守"magic number 必须在 core/config 统一维护"——有人把阈值改回模块常量本测会红。
    """
    from core.config import reset_config, set_config_override
    import utils.exchange_fee as ef
    from utils.sentiment import build_sentiment_brief

    # 当前分位 ≈ 90%（50 个点里排第 45）
    values = list(range(10, 60))
    values[-1] = 54.5
    monkeypatch.setattr(ef, "get_history_data", lambda *a, **k: _fake_vix_df(values))

    reset_config()
    try:
        brief = build_sentiment_brief(cnn_enabled=False)
        assert "INDEP_DEFENSE_FLAG: on" in brief  # 默认 0.85 < 90% → on

        set_config_override({"sentiment": {"vix_defense_quantile": 0.99,
                                           "vix_extreme_fear_q": 0.95}})
        brief2 = build_sentiment_brief(cnn_enabled=False)
        assert "INDEP_DEFENSE_FLAG: off" in brief2   # 抬线后同数据 → off
        assert "extreme_fear" not in brief2          # 分档线同样生效 → fear
        assert "fear" in brief2
    finally:
        reset_config()


# ---------- EVENT_STANCE 升级（默认=纯计数等价；加权/衰减经验证前禁用） ----------

def _stance_cfg(**kw):
    from core.config import set_config_override
    set_config_override({"sentiment": kw})


@pytest.fixture(autouse=True)
def _reset_cfg():
    from core.config import reset_config
    reset_config()
    yield
    reset_config()


BRIEF_3 = (
    "[2026-05-13T14:32:00+00:00] [risk/high] [FAKE.AX, NVDA] (sources: reuters)\n"
    "bad news\n\n"
    "[2026-05-12T08:00:00Z] [risk/mid] [OTHER=F]\nmore risk\n\n"
    "[2026-05-11T08:00:00Z] [opportunity/mid] [OTHER=F]\ngood news\n"
)


def test_event_stance_default_config_identical_to_legacy_counting():
    """等价性守门：默认配置（等权+无衰减+band 0）输出与旧纯计数版逐字一致。

    谁要改 defaults.yaml 的 event_stance_* 而没过 eval_event_stance.py 验证，
    本测试红给他看。
    """
    from utils.sentiment import _event_stance_line

    line = _event_stance_line(BRIEF_3)
    # 旧实现的逐字输出
    assert line == "EVENT_STANCE: net risk (risk=2 opportunity=1, 来自近期事件层)"


def test_event_stance_severity_weight_overrides_count():
    """开加权（w 1/2/3）后：1 条 risk/high 压过 2 条 opportunity/low"""
    from utils.sentiment import _event_stance_line

    _stance_cfg(event_stance_w_low=1.0, event_stance_w_mid=2.0, event_stance_w_high=3.0)
    brief = (
        "[2026-05-13T00:00:00Z] [risk/high] [A]\nx\n\n"
        "[2026-05-12T00:00:00Z] [opportunity/low] [A]\nx\n\n"
        "[2026-05-11T00:00:00Z] [opportunity/low] [A]\nx\n"
    )
    line = _event_stance_line(brief)
    # score = -3 + 1 + 1 = -1 → net risk（纯计数会给 opportunity）
    assert "net risk" in line
    assert "score=-1.0" in line  # 加权开启后显示 score


def test_event_stance_decay_flips_net(monkeypatch):
    """开衰减后：3 条旧 opportunity 衰减殆尽，1 条新 risk 主导"""
    from datetime import datetime, timezone
    import utils.sentiment as st

    fresh = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    brief = (
        f"[2026-06-10T11:00:00+00:00] [risk/mid] [A]\nfresh risk\n\n"
        f"[2026-05-25T00:00:00+00:00] [opportunity/mid] [A]\nold\n\n"
        f"[2026-05-24T00:00:00+00:00] [opportunity/mid] [A]\nold\n\n"
        f"[2026-05-23T00:00:00+00:00] [opportunity/mid] [A]\nold\n"
    )
    entries = st._parse_event_brief_entries(brief)

    # 默认（无衰减）：3 opp vs 1 risk → net opportunity
    score, risk, opp = st._stance_score(entries, now=fresh)
    assert (risk, opp) == (1, 3) and score > 0

    # half_life=24h：16 天前的 opportunity 衰减到 ~0 → net risk
    _stance_cfg(event_stance_half_life_hours=24.0)
    score2, _, _ = st._stance_score(entries, now=fresh)
    assert score2 < 0


def test_event_stance_unparseable_ts_no_crash():
    """ts 烂掉（空/非 ISO）不抛异常，按无衰减计"""
    from utils.sentiment import _event_stance_line

    _stance_cfg(event_stance_half_life_hours=24.0)  # 衰减开着也不能炸
    brief = (
        "[] [risk/mid] [A]\nno ts\n\n"
        "[not-a-date] [opportunity/low] [B]\nbad ts\n"
    )
    line = _event_stance_line(brief)
    assert line is not None and "risk=1 opportunity=1" in line


def test_event_stance_nonstandard_text_falls_back_to_counting():
    """非标准 override 文本（无完整头行）→ 退化旧纯计数，不丢行为"""
    from utils.sentiment import _event_stance_line

    line = _event_stance_line("自由文本提到 [risk/high] 和 [risk/mid] 没有头行结构")
    assert line == "EVENT_STANCE: net risk (risk=2 opportunity=0, 来自近期事件层)"


def test_event_stance_line_for_symbol_filters():
    """per-asset 行：只统计 [syms] 含该 symbol 的事件；任意虚构 ticker 通用"""
    from utils.sentiment import event_stance_line_for_symbol

    line_fake = event_stance_line_for_symbol(BRIEF_3, "FAKE.AX")
    assert line_fake is not None
    assert line_fake.startswith("EVENT_STANCE(FAKE.AX): net risk")
    assert "risk=1 opportunity=0" in line_fake

    line_other = event_stance_line_for_symbol(BRIEF_3, "OTHER=F")
    assert "net neutral" in line_other  # 1 risk vs 1 opportunity 打平
    assert "risk=1 opportunity=1" in line_other


def test_event_stance_line_for_symbol_no_match_returns_none():
    from utils.sentiment import event_stance_line_for_symbol

    assert event_stance_line_for_symbol(BRIEF_3, "MISSING.SYM") is None
    assert event_stance_line_for_symbol("", "FAKE.AX") is None
    assert event_stance_line_for_symbol(BRIEF_3, "") is None
