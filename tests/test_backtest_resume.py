"""run_one_day 断点续跑（resume）契约测试。

resume=True（默认）：已写出 <symbol>.md 的日期/资产跳过（不跑 LLM），断点续跑用。
resume=False：无条件跑（run_walk_forward 用）——它每次从零重建 simulator，必须拿到
每天真实 verdict 回放成交，跳过会让指标静默算在残缺成交集上。
"""
from __future__ import annotations


def test_resume_true_skips_when_md_exists(monkeypatch, tmp_path):
    """resume=True + 已存在 <symbol>.md → 返回 {skipped:True}，不跑委员会（无 LLM）。"""
    import core.memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")

    from core.committee.persist import safe_symbol
    d = "2024-03-15"
    bt = tmp_path / "memory" / ".backtest" / d
    bt.mkdir(parents=True)
    (bt / f"{safe_symbol('510300.SS')}.md").write_text("dummy", encoding="utf-8")

    from scripts.backtest_committee import run_one_day
    out = run_one_day(d, ["510300.SS"], resume=True)
    assert out["verdicts"]["510300.SS"] == {"skipped": True}


def test_safe_symbol_single_source_matches_persist():
    """backtest 续跑探测的文件名口径 == _persist 写文件名口径（单一可信源，防漂移）。"""
    from core.committee.persist import safe_symbol
    assert safe_symbol("510300.SS") == "510300_SS"
    assert safe_symbol("GC=F") == "GC_F"
    assert safe_symbol("NDQ.AX") == "NDQ_AX"
