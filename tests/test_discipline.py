"""services.discipline 自测:渲染逻辑(纯函数)+ 空台账优雅 + summary smoke。
跑:uv run pytest tests/test_discipline.py -q"""
from services.discipline import discipline_summary, render_discipline_md


def test_render_inaction_and_interventions():
    s = {
        "inaction": {"total_verdicts": 152, "by_verdict": {"HOLD": 85},
                     "hold": 85, "hold_rate": 0.559},
        "interventions": {
            "total": 24, "windows": [30, 60, 90], "caveat": "正=拦错 负=拦对",
            "by_family": {
                "buy_defense": {"n": 10, "settled_30d": 5, "settled_60d": 3, "settled_90d": 0,
                                "sum_pnl_30d": 1200.0, "sum_pnl_60d": -500.0, "sum_pnl_90d": 0.0},
                "trim_blocked": {"n": 14, "settled_30d": 0, "settled_60d": 0, "settled_90d": 0,
                                 "sum_pnl_30d": 0.0, "sum_pnl_60d": 0.0, "sum_pnl_90d": 0.0},
            }}}
    md = render_discipline_md(s)
    assert "56%" in md and "HOLD 85/152" in md          # 不作为率
    assert "拦加仓" in md and "10 次" in md
    assert "60d 合计 ¥-500" in md                         # 取最长已结算窗(90d 未结算→落 60d)
    assert "拦减仓" in md and "未到结算窗" in md          # trim_blocked 全未结算
    assert "非 alpha" in md                              # ADR-023 定位措辞


def test_empty_graceful():
    s = {"inaction": {"total_verdicts": 0, "by_verdict": {}, "hold": 0, "hold_rate": None},
         "interventions": {"total": 0, "by_family": {}, "windows": [30, 60, 90]}}
    md = render_discipline_md(s)
    assert "暂无" in md                                  # 空台账不崩、出人话


def test_summary_smoke():
    s = discipline_summary()                             # 真实数据(CI 空 memory → 全 0,仍合法)
    assert set(s) == {"inaction", "interventions"}
    assert "hold_rate" in s["inaction"]
    assert "total" in s["interventions"] and "by_family" in s["interventions"]


if __name__ == "__main__":
    test_render_inaction_and_interventions()
    test_empty_graceful()
    test_summary_smoke()
    print("discipline self-checks passed")
