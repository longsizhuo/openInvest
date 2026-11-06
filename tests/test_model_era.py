"""模型分桶（model_era）：换模型（#483 deepseek-v4-flash → claude-sonnet-5-5）后新旧账分开算。

守：model_era 口径（Direct / Coordinator / 老存档按日期）、holdout 内按模型分子桶 + n<30 只给 n、
查表只用同模型样本（不够退默认表并注明）、公开 JSON 的 by_model 抑制（含互补抑制）、四臂报告标模型。
不联网、不调 LLM。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

@pytest.fixture(autouse=True)
def _qualified_synthetic_math_inputs(monkeypatch):
    monkeypatch.setattr("openinvest.core.evidence_eligibility.Eligibility.allows", lambda *a, **k: True)

from openinvest.jobs import review_calc as rc
from scripts.export_accuracy import MIN_SAMPLE_FOR_PUBLIC, build_summary

PRE = "direct:deepseek-v4-flash"  # synthetic fixtures explicitly record their model
SONNET_H = {"schema": 1, "model": "claude-sonnet-5-5"}
SONNET = "direct:claude-sonnet-5-5"


# ---------- model_era 口径 ----------

@pytest.mark.parametrize("health,date,want", [
    ({"schema": 1, "model": "claude-sonnet-5-5"}, "2026-10-19", "direct:claude-sonnet-5-5"),
    ({"schema": 1, "model": "deepseek-v4-flash"}, "2026-10-14", "direct:deepseek-v4-flash"),
    ({"schema": 1}, "2026-10-19", "direct:unknown"),                       # 有块没 model：不猜
    ({"entry": "coordinator", "provider": "hermes", "model": "claude-haiku-5-5"}, "2026-10-20",
     "hermes:claude-haiku-5-5"),
    ({"entry": "coordinator", "provider": "claude", "model": "unknown"}, "2026-10-20", "claude:unknown"),
    ({"entry": "coordinator", "provider": "Hermes"}, "2026-10-20", "hermes:unknown"),   # 老 Coordinator 行没 model
    ({}, "2026-07-01", "unknown"),
    ({}, "2026-10-18", "unknown"),
    ({}, "2026-10-19", "unknown"),                                         # cutoff 之后没块 → 不猜
    (None, "2026-10-19", "unknown"),
])
def test_model_era_of(health, date, want):
    assert rc.model_era_of(health, date) == want


def test_review_derives_model_era_and_keeps_explicit_one():
    r = rc.VerdictReview(date="2026-10-20", asset="X", verdict="HOLD", confidence=0.6,
                         expected_direction="flat", macro_at_decision={}, input_health=dict(SONNET_H))
    assert r.model_era == SONNET
    assert rc.VerdictReview(date="2026-07-01", asset="X", verdict="HOLD", confidence=0.6,
                            expected_direction="flat", macro_at_decision={}).model_era == "unknown"
    e = rc.VerdictReview(date="2026-07-01", asset="X", verdict="HOLD", confidence=0.6,
                         expected_direction="flat", macro_at_decision={}, model_era="custom:m")
    assert e.model_era == "custom:m"
    assert json.loads(json.dumps(__import__("dataclasses").asdict(r)))["model_era"] == SONNET   # 落 jsonl


def test_model_key_ignores_prefix_suffix_and_colons_in_config_name():
    assert rc.era_model(PRE) == "deepseek-v4-flash" == rc.era_model("direct:deepseek-v4-flash")
    assert rc.era_model("hermes:claude-haiku-5-5") == "claude-haiku-5-5"
    assert rc.era_model("unknown") == "unknown"
    assert rc.model_key("Claude-Sonnet-5-5 ") == "claude-sonnet-5-5"
    assert rc.model_key("vendor/model:free") == "vendor/model:free"       # config 原名自带冒号不能被当 era 拆


# ---------- holdout 内按模型分子桶 ----------

def _rv(*, health=None, day="2026-09-01", verdict="HOLD", hit=True, mkt="flat", conf=0.6,
        matured=True, source="live"):
    r = rc.VerdictReview(date=day, asset="X", verdict=verdict, confidence=conf,
                         expected_direction=rc.EXPECTED_DIRECTION.get(verdict, rc.UNCLEAR_DIRECTION),
                         macro_at_decision={}, input_health=({"schema":1,"model":"deepseek-v4-flash"} if health is None else health), source=source)
    if matured:
        r.hits["30d"], r.directions["30d"] = hit, mkt
        r.actual_returns["30d"] = 0.0
    return r


def test_summarize_splits_holdout_by_model_and_suppresses_small_subbucket():
    old = [_rv(hit=i % 4 != 0) for i in range(40)]                       # pre-health deepseek，40 条 30d 已成熟
    new = [_rv(health=SONNET_H, day="2026-10-20", matured=False) for _ in range(35)] \
        + [_rv(health=SONNET_H, day="2026-10-20", hit=False) for _ in range(2)]   # Sonnet：总行 37、成熟 2
    s = rc.summarize_verdict_reviews(old + new)
    bm = s["holdout"]["by_model"]
    assert set(bm) == {PRE, SONNET}
    d, n = bm[PRE], bm[SONNET]
    assert d["n"] == 40 and d["by_window"]["30d"]["hit_rate"] == 0.75   # 30/40
    assert d["calibration"]["n"] == 40 and d["neutrality"]["n"] == 40
    # 总行 37 ≥ 30 但 30d 成熟只有 2：闸按成熟数，只给 n
    assert n == {"n": 37, "n_matured_30d": 2, "rates_suppressed_sub30": True,
                 "live_count": 37, "backtest_count": 0}
    # 全局 holdout 数字不变（仍是全部模型合并）
    assert s["holdout"]["n"] == 77 and s["holdout"]["by_window"]["30d"]["n"] == 42


def test_report_has_by_model_section_with_only_n_for_small_subbucket(tmp_path, monkeypatch):
    from openinvest.jobs import verdict_review as vr
    monkeypatch.setattr(vr, "ROOT", tmp_path)
    rows = [_rv(hit=i % 2 == 0) for i in range(40)] + [_rv(health=SONNET_H, day="2026-10-20", hit=True)]
    md = vr.write_report(rows, vr.summarize(rows)).read_text(encoding="utf-8")
    sec = md.split("## 按模型", 1)[1].split("## 置信度校准", 1)[0]
    assert f"| {PRE} | 40 | 40 | 50.0% |" in sec
    assert f"| {SONNET} | 1 | 1 | n<30 只给 n | — |" in sec
    assert "按模型 · " + PRE in sec and "按模型 · " + SONNET not in sec      # 小子桶没有详细分节
    assert "· 全部模型合并" in md                                           # 全局校准 / 中性度标明是合并口径


def test_single_era_report_does_not_duplicate_holdout():
    lines = "\n".join(rc._model_lines(rc.summarize_by_model([_rv() for _ in range(40)])))
    assert "只有一个 model_era" in lines and "按模型 · " not in lines


# ---------- 置信度查表：只用同模型样本 ----------

def _lk_rows(n, health=None, day="2026-06-01", **kw):
    return [_rv(health=health, day=day, **kw) for _ in range(n)]


def test_lookup_only_counts_same_model_rows():
    live = _lk_rows(40) + _lk_rows(5, SONNET_H, "2026-10-20")
    fleet = _lk_rows(10, {"schema": 1, "model": "deepseek-v4-flash"}, "2026-10-20", source="backtest")
    sonnet = rc.build_confidence_lookup(live, fleet, model="claude-sonnet-5-5")
    assert sonnet["model"] == "claude-sonnet-5-5"
    assert sonnet["by_verdict"]["HOLD"] == {"n": 5, "rate": None, "n_live": 5, "n_fleet": 0}   # 不够 30 → 无数
    ds = rc.build_confidence_lookup(live, fleet, model="deepseek-v4-flash")
    # pre-health + 有块的 DeepSeek 舰队行同模型；Sonnet 行不进
    assert ds["by_verdict"]["HOLD"] == {"n": 50, "rate": 1.0, "n_live": 40, "n_fleet": 10}
    assert "model" not in rc.build_confidence_lookup(live, fleet)                              # 不传 = 不过滤
    assert rc.build_confidence_lookup(live, fleet)["by_verdict"]["HOLD"]["n"] == 55
    # Coordinator 行：模型名对得上才算（host 没传 = unknown，永远不匹配）
    coord = _lk_rows(30, {"entry": "coordinator", "provider": "hermes", "model": "claude-sonnet-5-5"}, "2026-10-20") \
        + _lk_rows(30, {"entry": "coordinator", "provider": "claude", "model": "unknown"}, "2026-10-20")
    assert rc.build_confidence_lookup(coord, model="claude-sonnet-5-5")["by_verdict"]["HOLD"]["n"] == 30


DEFAULT = {"source": "fleet", "model": "deepseek-v4-flash", "by_verdict": {"HOLD": {
    "n": 900, "rate": 0.75, "n_live": 0, "n_fleet": 900}}, "market_flat": {"n": 900, "rate": 0.73}}


def test_insufficient_current_model_falls_back_to_default_and_says_so():
    local = rc.build_confidence_lookup(_lk_rows(40) + _lk_rows(5, SONNET_H, "2026-10-20"),
                                       model="claude-sonnet-5-5")
    merged = rc.merge_confidence_lookup(local, DEFAULT)
    assert merged["by_verdict"]["HOLD"]["from"] == "default"
    txt = rc.confidence_display({"verdict": "HOLD", "confidence": 0.62}, merged, with_raw=False)
    assert "默认表，deepseek-v4-flash 舰队样本，当前 claude-sonnet-5-5 样本不足" in txt and "n=900" in txt
    # 当前模型就是默认表的模型：照旧只写"默认表"
    same = rc.merge_confidence_lookup({**local, "model": "deepseek-v4-flash"}, DEFAULT)
    assert rc.confidence_display({"verdict": "HOLD", "confidence": 0.62}, same, with_raw=False).endswith("；默认表）")
    # 本机同模型样本够 30 → 用本机，不退默认表
    ok = rc.merge_confidence_lookup(
        rc.build_confidence_lookup(_lk_rows(35, SONNET_H, "2026-10-20"), model="claude-sonnet-5-5"), DEFAULT)
    assert ok["by_verdict"]["HOLD"]["from"] == "local" and ok["by_verdict"]["HOLD"]["n"] == 35


def test_daily_run_writes_lookup_for_the_configured_model(tmp_path, monkeypatch):
    from openinvest.core.memory_store import MemoryStore
    from openinvest.jobs import verdict_review as vr
    monkeypatch.setattr(vr, "MemoryStore", lambda: MemoryStore(tmp_path))
    monkeypatch.setattr(vr, "review_all", lambda **k: _lk_rows(40))          # 40 条 pre-health DeepSeek
    monkeypatch.setattr(vr, "write_report", lambda *a: tmp_path / "r.md")
    monkeypatch.setattr(vr, "write_jsonl", lambda *a: tmp_path / "r.jsonl")
    monkeypatch.setattr(vr, "review_fleet", lambda: [])
    dflt = tmp_path / "default.json"
    dflt.write_text(json.dumps(DEFAULT))
    monkeypatch.setattr(vr, "DEFAULT_LOOKUP_PATH", dflt)
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5-5")
    vr.run()
    saved = json.loads((tmp_path / ".dreams" / "confidence_lookup.json").read_text(encoding="utf-8"))
    assert saved["model"] == "claude-sonnet-5-5" and saved["by_verdict"] == {}   # 一条同模型样本都没有
    absent = vr.load_confidence_lookup()
    assert absent is None or absent["by_verdict"].get("HOLD", {}).get("n", 0) == 0
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")                    # 换回 DeepSeek → 40 条够 30
    vr.run()
    assert vr.load_confidence_lookup()["by_verdict"]["HOLD"]["from"] == "local"


# ---------- 公开 JSON：by_model ----------

def _row(*, era=None, health=None, day="2026-08-03", hit=True, matured=True, conf=0.6):
    r = {"date": day, "asset": "X", "source": "live", "expected_direction": "flat", "confidence": conf,
         "hits": {"30d": hit} if matured else {}, "directions": {"30d": "flat"} if matured else {},
         "actual_returns": {"30d": 0.0} if matured else {},
         "input_health": ({"schema":1,"model":"deepseek-v4-flash"} if health is None else health)}
    if era:
        r["model_era"] = era
    return r


def _export(tmp_path: Path, rows) -> dict:
    p = tmp_path / "v.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return build_summary(p)["windows"]["all"]


def test_public_by_model_splits_and_derives_era_for_old_rows(tmp_path):
    rows = [_row(hit=i % 2 == 0) for i in range(40)] \
        + [_row(health=SONNET_H, day="2026-10-20", hit=i % 3 == 0) for i in range(36)]
    w = _export(tmp_path, rows)                      # 老行没有 model_era 键：按 input_health + date 重算
    assert set(w["by_model"]) == {PRE, SONNET}
    assert w["by_model"][PRE]["sample_size"] == 40 and w["by_model"][PRE]["direction_hit_rate"] == 0.5
    assert w["by_model"][SONNET]["sample_size"] == 36 and w["by_model"][SONNET]["direction_hit_rate"] == 0.3333
    assert w["sample_size"] == 76                    # 窗口合计口径不变
    blob = json.dumps(w)
    assert '"asset"' not in blob and '"symbol"' not in blob and "X" not in w["by_model"]   # 红线


def test_public_by_model_small_model_suppressed_and_complement_masked(tmp_path):
    rows = [_row(hit=i % 2 == 0) for i in range(40)] \
        + [_row(health=SONNET_H, day="2026-10-20", hit=False) for _ in range(3)]
    w = _export(tmp_path, rows)
    small, big = w["by_model"][SONNET], w["by_model"][PRE]
    assert small["sample_size"] == 3 and small["direction_hit_rate"] is None
    assert small["by_direction"]["hold"]["total"] == 3 and small["by_direction"]["hold"]["hit"] is None
    # 窗口合计公开（43 条）：若大模型的数字也公开，相减就能还原那 3 条 → 互补抑制：大模型只留计数
    assert w["direction_hit_rate"] is not None
    assert big["sample_size"] == 40 and big["direction_hit_rate"] is None
    assert big["by_direction"]["hold"]["total"] == 40
    assert all(big["by_direction"]["hold"][k] is None for k in ("hit", "rate", "lift", "ci95", "base_rate"))
    assert big["calibration"]["n"] == 40 and big["calibration"]["brier"] is None
    assert big["base_rate"]["n"] == 40 and big["base_rate"]["flat"] is None


def test_public_by_model_no_mask_when_no_mature_rows_or_enough_rows(tmp_path):
    # 新模型 30d 还没成熟：sample_size 0，没有可还原的东西 → 旧模型数字照出
    rows = [_row(hit=True) for _ in range(40)] + [_row(health=SONNET_H, day="2026-10-20", matured=False)] * 4
    w = _export(tmp_path, rows)
    assert w["by_model"][SONNET]["sample_size"] == 0
    assert w["by_model"][PRE]["direction_hit_rate"] == 1.0
    # 新模型够 30：两边都出数字
    rows = [_row(hit=True) for _ in range(40)] + [_row(health=SONNET_H, day="2026-10-20", hit=False)] * MIN_SAMPLE_FOR_PUBLIC
    w = _export(tmp_path, rows)
    assert w["by_model"][SONNET]["direction_hit_rate"] == 0.0 and w["by_model"][PRE]["direction_hit_rate"] == 1.0


def test_public_by_model_prefers_stored_model_era_and_windows_each_get_it(tmp_path):
    now = datetime.now(timezone.utc)
    d = now - timedelta(days=65)
    while d.weekday() >= 5:   # 周末决议对休市资产按周五重复剔除——落到周六/日样本会整个消失（周一跑必红）
        d += timedelta(days=1)
    day = d.strftime("%Y-%m-%d")
    rows = [_row(era="hermes:claude-haiku-5-5", day=day, hit=True) for _ in range(31)]
    p = tmp_path / "v.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    ws = build_summary(p)["windows"]
    assert all("by_model" in w for w in ws.values())
    assert set(ws["all"]["by_model"]) == {"hermes:claude-haiku-5-5"}


# ---------- 四臂报告标模型 ----------

def test_arm_report_labels_models_and_warns_on_mixed_models():
    ctl = [_rv(health={"schema": 1, "model": "deepseek-v4-flash"}, day="2026-10-12") for _ in range(2)]
    arm_same = {"sealed": [_rv(health={"schema": 1, "model": "deepseek-v4-flash", "arm": "sealed_n3"},
                               day="2026-10-12")]}
    txt = "\n".join(rc.arms_report_lines(arm_same, ctl))
    assert "模型（model_era × 行数）" in txt and "direct:deepseek-v4-flash × 2" in txt
    assert "不止一个模型" not in txt
    arm_mixed = {"sealed": [_rv(health={"schema": 1, "model": "claude-sonnet-5-5", "arm": "sealed_n3"},
                                day="2026-10-12")]}
    mixed = "\n".join(rc.arms_report_lines(arm_mixed, ctl))
    assert "direct:claude-sonnet-5-5 × 1" in mixed and "不止一个模型" in mixed
    assert "claude-sonnet-5-5" in mixed.split("不止一个模型", 1)[1].split("\n", 1)[0]


def test_arm_report_rows_without_recorded_model_are_not_a_second_model():
    """舰队对照臂 / T2 臂 transcript 在记录模型之前是 direct:unknown：不能因此报“不止一个模型”，只提示有 N 行没记录。"""
    ctl = [_rv(health={"schema": 1}, day="2026-10-12") for _ in range(2)]      # 块里没有 model
    arm = {"sealed": [_rv(health={"schema": 1, "model": "deepseek-v4-flash", "arm": "sealed_n3"}, day="2026-10-12")]}
    txt = "\n".join(rc.arms_report_lines(arm, ctl))
    assert "direct:unknown × 2" in txt and "不止一个模型" not in txt
    assert "另有 2 行没有记录模型" in txt


# ---------- 评审 #497：方向桶 / 命中总数 / 校准 / 市场基率的跨模型减法通道 ----------

def _mk(model_health, day, *, bull=0, bull_hits=0, hold=0, hold_hits=0, bear=0, bear_hits=0):
    """合成一个模型的成熟行：看涨 bull 条（命中 bull_hits）、HOLD hold 条、看跌 bear 条。"""
    rows = []
    for direction, n, k in (("up", bull, bull_hits), ("flat", hold, hold_hits), ("down", bear, bear_hits)):
        for i in range(n):
            hit = i < k
            mkt = {"up": "up" if hit else "flat", "flat": "flat" if hit else "up",
                   "down": "down" if hit else "flat"}[direction]
            r = _row(health=model_health, day=day, hit=hit, conf=0.6)
            r["expected_direction"] = direction
            r["directions"] = {"30d": mkt}
            r["actual_returns"] = {"30d": 0.02 if mkt == "up" else (-0.02 if mkt == "down" else 0.0)}
            rows.append(r)
    return rows


def test_public_by_model_closes_direction_bucket_subtraction(tmp_path):
    """评审复现：A 35 看涨(18 中)+30 HOLD；B 30 HOLD+5 看涨(4 中)。窗口 bullish.hit=22 公开，A 的 bullish 也公开，
    则 22−18=4 还原 B 的 5 条。现在被藏桶所在的量族在其余模型里也藏掉。"""
    rows = _mk(None, "2026-08-03", bull=35, bull_hits=18, hold=30, hold_hits=20) \
        + _mk(SONNET_H, "2026-10-20", bull=5, bull_hits=4, hold=30, hold_hits=20)
    w = _export(tmp_path, rows)
    a, b = w["by_model"][PRE], w["by_model"][SONNET]
    assert w["by_direction"]["bullish"]["hit"] == 22 and w["by_direction"]["bullish"]["total"] == 40   # 窗口公开
    assert b["by_direction"]["bullish"]["total"] == 5 and b["by_direction"]["bullish"]["hit"] is None   # B 的小桶被抑制
    # A 的 bullish 族也藏（total 计数保留）→ 窗口 − A 还原不出 B
    assert a["by_direction"]["bullish"]["total"] == 35
    assert all(a["by_direction"]["bullish"][k] is None for k in ("hit", "rate", "lift", "ci95"))
    # B 整体 ≥30 但因小桶被互补抑制，direction_hit_rate=None；窗口公开 → A 的也藏，否则 窗口−A 得 B 的命中总数，再减 B 的 HOLD 桶得小桶命中
    assert w["direction_hit_rate"] is not None
    assert a["direction_hit_rate"] is None and b["direction_hit_rate"] is None
    # 校准同理（Brier×n 跨模型可加）
    assert w["calibration"]["brier"] is None  # raw confidence is not a declared probability
    assert a["calibration"]["brier"] is None and b["calibration"]["brier"] is None
    # 两边都 ≥30 的 HOLD 桶不受牵连，照出
    assert a["by_direction"]["hold"]["hit"] == 20 and b["by_direction"]["hold"]["hit"] == 20


def _recoverable(window):
    """独立于实现的攻击者视角：某个量族里恰好一个模型有数据却被藏、窗口和其余有数据的模型都公开 → 可由减法还原。"""
    from scripts.export_accuracy import _families, _get
    leaks = []
    for name, paths, count_path in _families():
        hidden = [m for m, a in window["by_model"].items()
                  if int(_get(a, count_path) or 0) > 0 and _get(a, paths[0]) is None]
        others_public = all(_get(a, paths[0]) is not None for m, a in window["by_model"].items()
                            if m not in hidden and int(_get(a, count_path) or 0) > 0)
        if len(hidden) == 1 and _get(window, paths[0]) is not None and others_public \
                and len([m for m in window["by_model"] if int(_get(window["by_model"][m], count_path) or 0) > 0]) > 1:
            leaks.append((name, hidden[0]))
    return leaks


@pytest.mark.parametrize("a_bull,b_bull,b_hold,b_bear", [
    (35, 5, 30, 0), (35, 29, 31, 0), (30, 1, 40, 0), (40, 0, 3, 0), (45, 30, 10, 0),
    (35, 4, 30, 6), (31, 31, 31, 31), (60, 3, 1, 1), (33, 0, 35, 2),
])
def test_public_by_model_no_single_model_is_recoverable_by_subtraction(tmp_path, a_bull, b_bull, b_hold, b_bear):
    rows = _mk(None, "2026-08-03", bull=a_bull, bull_hits=a_bull // 2, hold=40, hold_hits=25, bear=33, bear_hits=20) \
        + _mk(SONNET_H, "2026-10-20", bull=b_bull, bull_hits=b_bull // 2, hold=b_hold, hold_hits=b_hold // 2,
              bear=b_bear, bear_hits=b_bear // 2)
    w = _export(tmp_path, rows)
    assert _recoverable(w) == []


def test_small_window_does_not_leak_hold_hit_through_bucket_base_rate(tmp_path):
    """方向桶里的 base_rate（同尺子的市场标签占比）在样本只有几行时 = HOLD 命中率：样本 <30 也抹掉。"""
    w = _export(tmp_path, _mk(SONNET_H, "2026-10-20", hold=4, hold_hits=3))
    for scope in (w, w["by_model"][SONNET]):
        assert scope["by_direction"]["hold"]["rate"] is None and scope["by_direction"]["hold"]["base_rate"] is None


# ---------- 评审 #497：Input Health 上线前的 Coordinator 存档不能按日期算成 DeepSeek ----------

def test_old_coordinator_archive_without_health_is_not_deepseek(tmp_path, monkeypatch):
    from openinvest.core.decision_ledger import parse_committee_text
    skill = ("# Committee: X\n\n**Date**: 2026-09-01\n**Symbol**: X\n**Provider**: claude (skill mode)\n"
             "**Verdict**: HOLD (confidence 0.60)\n")
    direct = "# Committee: X\n**Symbol**: X\n**Verdict**: HOLD (confidence 0.60)\n"
    assert parse_committee_text(skill)["provider"] == "claude"
    assert parse_committee_text(direct)["provider"] is None
    assert rc.model_era_of({}, "2026-09-01", "claude") == "claude:unknown"
    assert rc.model_era_of({}, "2026-09-01", None) == "unknown"
    # 端到端：review_one 把 Provider 行带进 model_era
    from openinvest.jobs import verdict_review as vr
    d = tmp_path / "2026-09-01"
    d.mkdir()
    (d / "X.md").write_text(skill, encoding="utf-8")
    (d / "Y.md").write_text(direct.replace("**Symbol**: X", "**Symbol**: Y"), encoding="utf-8")
    monkeypatch.setattr(vr, "_window_return", lambda *a, **k: 0.0)
    monkeypatch.setattr(vr, "_detect_macro_shock", lambda *a, **k: {"detected": False, "drivers": []})
    monkeypatch.setattr(vr, "_decision_regime", lambda *a, **k: None)
    monkeypatch.setattr(vr, "_atr_pct_cached", lambda *a, **k: 2.0)
    assert vr.review_one(d, "X", {}).model_era == "claude:unknown"
    assert vr.review_one(d, "Y", {}).model_era == "unknown"


# ---------- 安全评审 #497：掩码要跨族（direction_hit_rate 被掩 ⇒ 校准也掩） ----------

def test_masked_hit_rate_also_masks_calibration_and_keeps_n(tmp_path):
    """两个模型子桶、其中一个 n<30 被掩：另一个的 direction_hit_rate 与 calibration 数字都为 null，n 保留。"""
    rows = _mk(None, "2026-08-03", hold=40, hold_hits=25) + _mk(SONNET_H, "2026-10-20", hold=3, hold_hits=2)
    w = _export(tmp_path, rows)
    a, b = w["by_model"][PRE], w["by_model"][SONNET]
    assert a["direction_hit_rate"] is None and b["direction_hit_rate"] is None
    for m in (a, b):
        assert all(m["calibration"][k] is None for k in ("ece", "brier", "base_rate_brier"))
    assert a["calibration"]["n"] == 40 and b["calibration"]["n"] == 3 and a["sample_size"] == 40


def test_calibration_must_follow_hit_rate_even_when_window_calibration_is_not_public(tmp_path):
    """校准族自己不会触发掩码的情形：被藏模型 B 的 5 条全是 confidence=0.0（不进校准、cal.n=0）→ 窗口校准因
    0<n_excluded<30 本身就是 null、校准族没有“有数据被藏”的模型。但 direction_hit_rate 族照样掩掉 A 的命中率；
    若 A 的校准还留着，base_rate_brier=p̄(1−p̄) 把 A 的命中总数还原出来，再 窗口命中总数 − A = B 的命中。"""
    rows = _mk(None, "2026-08-03", hold=60, hold_hits=40)
    rows += [{**r, "confidence": 0.0} for r in _mk(SONNET_H, "2026-10-20", hold=5, hold_hits=4)]
    w = _export(tmp_path, rows)
    a, b = w["by_model"][PRE], w["by_model"][SONNET]
    assert w["direction_hit_rate"] is not None and w["calibration"]["brier"] is None   # 前提成立
    assert b["direction_hit_rate"] is None
    assert a["direction_hit_rate"] is None, "hit_rate 族：窗口公开、B 被藏 → A 也藏"
    assert all(a["calibration"][k] is None for k in ("ece", "brier", "base_rate_brier")), "A 的校准必须跟着藏"
    assert a["calibration"]["n"] == 60                                                  # 计数保留


def test_published_numbers_never_pin_down_a_masked_models_hit_rate(tmp_path):
    """攻击者视角：对每个模型，若它的 direction_hit_rate 是 None，其余模型公开的数字里不能再藏着它的命中率
    （校准的 base_rate_brier 反推 p̄；方向桶命中之和 = 命中总数）。"""
    for hold_b in (1, 5, 29):
        rows = _mk(None, "2026-08-03", hold=60, hold_hits=40, bull=35, bull_hits=18) \
            + _mk(SONNET_H, "2026-10-20", hold=hold_b, hold_hits=hold_b // 2)
        w = _export(tmp_path, rows)
        for era, m in w["by_model"].items():
            if m["direction_hit_rate"] is None:
                assert all(m["calibration"][k] is None for k in ("ece", "brier", "base_rate_brier")), era
