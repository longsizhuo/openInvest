"""D10 P1：裁决旁展示的同类决议查表（只改展示层；样本 = live + 前瞻纸面舰队）。"""
import copy
import json
import os
from datetime import datetime

import pandas as pd

from openinvest.jobs.review_calc import (
    VerdictReview,
    build_confidence_lookup,
    confidence_display,
    merge_confidence_lookup,
)


def _rv(verdict, hit, *, date="2026-06-01", conf=0.6, source="live", flat=True,
        asset="SPY", matured=True):
    r = VerdictReview(date=date, asset=asset, verdict=verdict, confidence=conf,
                      expected_direction="", macro_at_decision={}, source=source)
    if matured:
        r.hits["30d"] = hit
        r.directions["30d"] = "flat" if flat else "up"
    return r


def test_lookup_pool_filters_source_mix_and_n30_suppression():
    live = (
        [_rv("HOLD", True)] * 24 + [_rv("HOLD", False, flat=False)] * 8      # 32
        + [_rv("HOLD", True, conf=0.4)] * 4               # 低分行照样进样本（只在展示时打标）
        + [_rv("HOLD", True, date="2026-06-06")] * 50     # 周六非加密：不进
        + [_rv("HOLD", True, date="2026-06-06", asset="BTC-USD")] * 2   # 加密周末：进
        + [_rv("HOLD", True, source="backtest")] * 50     # live 列表里混进的非 live：不进
        + [_rv("HOLD", True, matured=False)] * 50         # 未成熟：不进
        + [_rv("ACCUMULATE", False, flat=False)] * 20
    )
    fleet = (
        [_rv("HOLD", False, source="backtest", flat=False)] * 10
        + [_rv("HOLD", True, source="backtest", date="2026-06-07")] * 9   # 周日非加密：不进
        + [_rv("ACCUMULATE", True, source="backtest", flat=False)] * 9    # 20+9=29 → 不给数
    )
    lk = build_confidence_lookup(live, fleet)
    assert lk["by_verdict"]["HOLD"] == {"n": 48, "rate": round(30 / 48, 3),
                                        "n_live": 38, "n_fleet": 10}
    assert lk["by_verdict"]["ACCUMULATE"] == {"n": 29, "rate": None, "n_live": 20, "n_fleet": 9}
    assert lk["market_flat"] == {"n": 77, "rate": round(30 / 77, 3), "n_live": 58, "n_fleet": 19}
    # 只给舰队（默认表生成器的用法）：n_live 恒 0
    assert build_confidence_lookup([], fleet)["by_verdict"]["HOLD"]["n_live"] == 0


LOCAL = {"by_verdict": {"HOLD": {"n": 136, "rate": 0.713, "n_live": 100, "n_fleet": 36},
                        "ACCUMULATE": {"n": 40, "rate": 0.2, "n_live": 40, "n_fleet": 0},
                        "TRIM": {"n": 18, "rate": None, "n_live": 18, "n_fleet": 0},
                        "BUY": {"n": 3, "rate": None, "n_live": 3, "n_fleet": 0}},
         "market_flat": {"n": 181, "rate": 0.691}}
DEFAULT = {"by_verdict": {"HOLD": {"n": 900, "rate": 0.75, "n_live": 0, "n_fleet": 900},
                          "BUY": {"n": 80, "rate": 0.64, "n_live": 0, "n_fleet": 80},
                          "SELL": {"n": 5, "rate": None, "n_live": 0, "n_fleet": 5}},
           "market_flat": {"n": 980, "rate": 0.73}}
LK = merge_confidence_lookup(LOCAL, DEFAULT)


def test_merge_prefers_local_n30_else_bundled_default():
    bv = LK["by_verdict"]
    assert bv["HOLD"]["from"] == "local" and bv["HOLD"]["market_flat"]["rate"] == 0.691
    assert bv["ACCUMULATE"]["from"] == "local"
    assert bv["BUY"]["from"] == "default" and bv["BUY"]["market_flat"]["rate"] == 0.73
    assert bv["TRIM"] == {"n": 18, "rate": None, "from": "local"}     # 两边都不够：留本机 n
    assert bv["SELL"] == {"n": 0, "rate": None, "from": "local"}
    assert merge_confidence_lookup(None, DEFAULT)["by_verdict"]["HOLD"]["from"] == "default"
    assert merge_confidence_lookup(None, None) is None


def test_display_wording_and_source_note():
    hold = {"verdict": "HOLD", "confidence": 0.6}
    assert confidence_display(hold, LK) == (
        "同类 HOLD 之后 30 天涨跌留在正常波动带内的比例 71%"
        "（n=136；同期市场横盘基率 69%；含纸面舰队样本）（自报 0.60）")
    assert "命中率" not in confidence_display(hold, LK)
    assert confidence_display({"verdict": "ACCUMULATE", "confidence": 0.62}, LK,
                              with_raw=False) == "同类 ACCUMULATE 30 天后方向判对的比例 20%（n=40；本机样本）"
    assert confidence_display({"verdict": "BUY", "confidence": 0.7}, LK,
                              with_raw=False) == "同类 BUY 30 天后方向判对的比例 64%（n=80；默认表）"
    only_default = merge_confidence_lookup(None, DEFAULT)
    assert confidence_display(hold, only_default, with_raw=False) == (
        "同类 HOLD 之后 30 天涨跌留在正常波动带内的比例 75%（n=900；同期市场横盘基率 73%；默认表）")
    assert confidence_display({"verdict": "TRIM", "confidence": 0.7}, LK).startswith("样本不足（n=18）")
    assert confidence_display({"verdict": "SELL", "confidence": 0.7}, None).startswith("样本不足（n=0）")


def test_forced_or_low_rows_get_tag_not_lookup():
    tag = "输入缺失/强制 HOLD，不查表"
    for v in (
        {"verdict": "HOLD", "confidence": 0.4},                        # Sanity 3/4 封顶
        {"verdict": "ACCUMULATE", "confidence": 0.35},                 # 自报低分
        {"verdict": "HOLD", "confidence": 0.7, "_original_verdict": "TRIM"},          # Sanity 5
        {"verdict": "HOLD", "confidence": 0.65, "_original_verdict": "ACCUMULATE",
         "_defense_downgrade": "accumulate_to_hold"},                  # 防御拦买
        {"verdict": "HOLD", "confidence": 0.4, "_original_confidence_unavailable": 0.7},
        {"verdict": "UNCLEAR", "confidence": 0.0},
    ):
        assert confidence_display(v, LK).startswith(tag), v
    # BUY→ACCUMULATE 降级不是强制 HOLD：照常查表
    assert confidence_display({"verdict": "ACCUMULATE", "confidence": 0.6,
                               "_original_verdict": "BUY"}, LK).startswith("同类 ACCUMULATE")


def test_display_never_touches_the_verdict_dict():
    v = {"verdict": "HOLD", "confidence": 0.6, "alloc_cny": 0, "_original_verdict": "TRIM"}
    before = copy.deepcopy(v)
    confidence_display(v, LK)
    assert v == before


def test_bundled_default_is_fleet_only_aggregate():
    """随包默认表：只用舰队（n_live 恒 0），只有聚合数，没有标的（CI 红线 grep 同一份）。"""
    from openinvest.jobs.verdict_review import DEFAULT_LOOKUP_PATH
    data = json.loads(DEFAULT_LOOKUP_PATH.read_text(encoding="utf-8"))
    assert data["source"] == "fleet"
    cells = list(data["by_verdict"].values()) + [data["market_flat"]]
    assert cells and all(c["n_live"] == 0 and c["n"] == c["n_fleet"] for c in cells)
    assert set(data) <= {"source", "window", "min_n", "by_verdict", "market_flat", "generated_on"}


def test_daily_verdict_review_refreshes_lookup(tmp_path, monkeypatch):
    """verdict_review.run() 的尾巴写出查表，展示端读得回来；坏文件 → 退回默认表，不抛。"""
    from openinvest.core.memory_store import MemoryStore
    from openinvest.jobs import verdict_review as vr
    monkeypatch.setattr(vr, "MemoryStore", lambda: MemoryStore(tmp_path))
    monkeypatch.setattr(vr, "review_all", lambda **k: [_rv("HOLD", True)] * 30)
    monkeypatch.setattr(vr, "write_report", lambda *a: tmp_path / "r.md")
    monkeypatch.setattr(vr, "write_jsonl", lambda *a: tmp_path / "r.jsonl")
    dflt = tmp_path / "default.json"
    dflt.write_text(json.dumps(DEFAULT))
    monkeypatch.setattr(vr, "DEFAULT_LOOKUP_PATH", dflt)
    assert vr.load_confidence_lookup()["by_verdict"]["HOLD"]["from"] == "default"
    vr.run()
    lk = vr.load_confidence_lookup()["by_verdict"]["HOLD"]
    assert (lk["from"], lk["n"], lk["rate"], lk["n_live"], lk["n_fleet"]) == ("local", 30, 1.0, 30, 0)
    (tmp_path / ".dreams" / "confidence_lookup.json").write_text("{bad")
    assert vr.load_confidence_lookup()["by_verdict"]["HOLD"]["from"] == "default"
    (tmp_path / ".dreams" / "confidence_lookup.json").write_text('{"by_verdict": []}')
    assert vr.load_confidence_lookup()["by_verdict"]["HOLD"]["from"] == "default"
    dflt.unlink()
    assert vr.load_confidence_lookup() is None


def _write_md(root, sub, day, sym, verdict, *, written=None):
    d = root / sub / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sym}.md"
    p.write_text(f"# Committee: {sym}\n**Symbol**: {sym}\n**Verdict**: {verdict} (confidence 0.60)\n\n"
                 '## Macro Context Snapshot\n```json\n{"vix": 15.0}\n```\n', encoding="utf-8")
    ts = (written or datetime.fromisoformat(day + "T22:30:00")).timestamp()
    os.utime(p, (ts, ts))


def test_fleet_rows_join_lookup_offline(monkeypatch, tmp_path):
    """舰队只收前瞻那部分（≥FLEET_START、决议日当天/次日写出），T2 臂和事后补跑的回测不进；
    标签只读 market_data.db，零网络（同 test_run_is_live_only_and_never_touches_network 的拦法）；
    舰队不套本机持仓映射。"""
    import socket

    import yfinance as yf

    from openinvest.core import memory_store as ms
    from openinvest.db import market_store as mstore
    from openinvest.jobs import verdict_review as vr
    from openinvest.utils import exchange_fee as ef

    mem = tmp_path / "memory"
    monkeypatch.setattr(ms, "MEMORY_ROOT", mem)
    monkeypatch.setattr(vr, "ROOT", tmp_path)
    monkeypatch.setattr(mstore, "DB_PATH", str(tmp_path / "market_data.db"))
    monkeypatch.setattr(vr, "_STORE", None)
    _write_md(mem, ".committee", "2026-08-03", "AAPL", "HOLD")                     # live
    _write_md(mem, ".backtest", "2026-08-03", "AAPL", "HOLD")                      # 舰队 ✓
    _write_md(mem, ".backtest", "2026-08-04", "MSFT", "ACCUMULATE",
              written=datetime.fromisoformat("2026-08-05T23:00:00"))               # 次日写出 ✓
    _write_md(mem, ".backtest", "2026-08-05", "MSFT", "ACCUMULATE",
              written=datetime.fromisoformat("2026-09-20T10:00:00"))               # 事后补跑 ✗
    _write_md(mem, ".backtest", "2026-08-08", "AAPL", "HOLD")                      # 周六非加密 ✗（查表剔）
    _write_md(mem, ".backtest", "2026-07-20", "AAPL", "HOLD")                      # 历史回填 ✗
    _write_md(mem, ".backtest_t2conf", "2026-08-03", "MSFT", "ACCUMULATE")         # T2 试跑臂 ✗

    store = mstore.MarketStore()
    for i, day in enumerate(pd.bdate_range("2025-06-01", "2026-09-30")):
        for sym, base in [("AAPL", 100.0), ("MSFT", 200.0), ("^VIX", 15.0),
                          ("^TNX", 4.0), ("USDCNY=X", 7.0)]:
            c = base * (1 + 0.001 * i)
            store.save_generic_price(sym, day.strftime("%Y-%m-%d"), c,
                                     high=c * 1.01, low=c * 0.99, volume=1e6)

    calls = []

    def _net(*a, **k):
        calls.append(a[:2])
        raise RuntimeError("network attempted during verdict_review")

    monkeypatch.setattr(ef, "get_history_data", _net)
    monkeypatch.setattr(yf, "Ticker", _net)
    monkeypatch.setattr(yf, "download", _net)
    monkeypatch.setattr(socket.socket, "connect", _net)

    def _no_holdings_for_fleet():
        raise AssertionError("舰队不该套本机持仓映射")

    monkeypatch.setattr(vr, "_build_symbol_resolver", _no_holdings_for_fleet)
    fleet = vr.review_fleet()
    assert sorted((r.date, r.asset) for r in fleet) == [
        ("2026-08-03", "AAPL"), ("2026-08-04", "MSFT"), ("2026-08-08", "AAPL")]
    assert all("30d" in r.hits for r in fleet), "标签应从库里算出"

    monkeypatch.setattr(vr, "_build_symbol_resolver", lambda: {})
    assert vr.run()["status"] == "ok"
    assert calls == [], f"verdict_review 不许触网，实际尝试 {calls}"
    lk = json.loads((mem / ".dreams" / "confidence_lookup.json").read_text())
    assert lk["by_verdict"]["HOLD"] == {"n": 2, "rate": None, "n_live": 1, "n_fleet": 1}
    assert lk["by_verdict"]["ACCUMULATE"] == {"n": 1, "rate": None, "n_live": 0, "n_fleet": 1}
    rows = (mem / ".dreams" / "verdict_review.jsonl").read_text().splitlines()
    assert len(rows) == 1, "舰队行只进查表，不进 jsonl"


def test_local_cli_cached_run_committee_carries_lookup(monkeypatch, capfd, tmp_path):
    """本地 CLI 同日 cache 命中也给 confidence_lookup（与 fresh 路径 / MCP / 远端同款）"""
    import argparse
    from openinvest.jobs import verdict_review as vr
    from openinvest.skill_cmds import committee_cmds as cc

    class FakePM:
        strategy = {"target_assets": [{"symbol": "AAPL", "target_pct": 1.0}]}

    monkeypatch.setenv("LLM_API_KEY", "test-fake-key")
    monkeypatch.setattr(cc, "ROOT", tmp_path)
    monkeypatch.setattr("openinvest.core.portfolio_manager.PortfolioManager", lambda: FakePM())
    monkeypatch.setattr(vr, "load_confidence_lookup", lambda: LK)
    md = tmp_path / "memory" / ".committee" / datetime.now().strftime("%Y-%m-%d") / "AAPL.md"
    md.parent.mkdir(parents=True)
    md.write_text("# Committee: AAPL\n**Verdict**: HOLD (confidence 0.62)\n", encoding="utf-8")

    cc.cmd_run_committee(argparse.Namespace(symbol="AAPL", force=False, max_rounds=1))
    out = json.loads(capfd.readouterr().out)
    assert out["status"] == "cached"
    assert out["confidence_lookup"] == confidence_display(
        {"verdict": "HOLD", "confidence": 0.62}, LK, with_raw=False)
