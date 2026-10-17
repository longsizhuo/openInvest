"""基准币种第 1 步（委员会层）：AUD 画像下字段名 / 阈值 / 截断上限 / 持仓摘要都按 AUD；
CNY 默认画像逐字不变由 base=CNY 的既有测试 + 模板无残留占位符守。"""
from __future__ import annotations

import re

import pytest

from openinvest.core.config import set_config_override
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager

AUD_PER_CNY = 1 / 4.7


@pytest.fixture(autouse=True)
def _offline_fx(monkeypatch):
    from openinvest.utils import fx
    rates = {("AUD", "CNY"): 4.7, ("CNY", "AUD"): AUD_PER_CNY}
    monkeypatch.setattr(fx, "get_fx_rate",
                        lambda q, b="CNY", as_of_date=None: 1.0 if q == b else rates.get((q, b)))


def _aud():
    set_config_override({"portfolio": {"base_currency": "aud"}})


def _prompts(json_mode=False):
    from openinvest.capabilities.committee.cio import build_cio_prompt
    from openinvest.capabilities.committee.risk_officer import build_risk_officer_prompt
    asset = {"symbol": "NDQ.AX", "display_name": "NDQ"}
    return {
        "cio": build_cio_prompt(asset, json_mode=json_mode),
        "risk": build_risk_officer_prompt(asset, "opening"),
        "risk_rebuttal": build_risk_officer_prompt(asset, "rebuttal"),
    }


def test_thresholds_convert_from_cny_with_two_significant_digits():
    from openinvest.utils.base_currency import alloc_ceiling, base_currency, from_cny, money
    assert from_cny(1000) == 1000 and alloc_ceiling() == 100000 and money(1234.5) == "¥1,234"
    _aud()
    assert base_currency() == "AUD"                      # 配置大小写不敏感
    assert (from_cny(1000), from_cny(5000), alloc_ceiling()) == (210, 1100, 21000)
    assert money(1234.5) == "1,234 AUD"


def test_missing_fx_keeps_cny_number(monkeypatch):
    from openinvest.utils import fx
    from openinvest.utils.base_currency import from_cny
    monkeypatch.setattr(fx, "get_fx_rate", lambda *a, **k: None)
    set_config_override({"portfolio": {"base_currency": "JPY"}})
    assert from_cny(5000) == 5000


@pytest.mark.parametrize("lens", [False, True])
def test_aud_prompts_use_aud_fields_and_converted_thresholds(lens):
    set_config_override({"portfolio": {"base_currency": "AUD"},
                         "verdict": {"concentration_lens_enabled": lens}})
    p = _prompts()
    for text in p.values():
        assert "{{" not in text
        assert not re.search(r"(SUGGESTED_ALLOC|DRY_POWDER|WORST_CASE_LOSS)_CNY|[a-z]_cny\b", text)
    assert "SUGGESTED_ALLOC_AUD: <" in p["cio"]
    assert "first_tranche_aud:" in p["cio"] and "worst_case_pnl_aud:" in p["cio"]
    assert "人民币" not in p["cio"]
    assert "|SUGGESTED_ALLOC_AUD| > 21000" in p["cio"]
    assert "DRY_POWDER_AUD < 1100" in p["cio"]
    assert "DRY_POWDER_AUD < 210" in p["risk"] and "DRY_POWDER_AUD < 210" in p["risk_rebuttal"]
    assert "WORST_CASE_LOSS_AUD_AT_-20" in p["risk"] and "如 430 AUD" in p["risk"]
    json_cio = _prompts(json_mode=True)["cio"]
    assert '"suggested_alloc_aud"' in json_cio and "suggested_alloc_cny" not in json_cio


def test_cny_prompts_render_every_placeholder():
    for text in _prompts(json_mode=True).values():
        assert "{{" not in text
    assert "|SUGGESTED_ALLOC_CNY| > 100000" in _prompts()["cio"]


def test_parser_reads_any_currency_and_clamps_at_given_ceiling():
    from openinvest.core.committee.cio_parse import parse_cio_memo
    out = parse_cio_memo("VERDICT: ACCUMULATE\nCONFIDENCE: 0.6\nSUGGESTED_ALLOC_AUD: 30000\n",
                         alloc_ceiling=21000)
    assert out["alloc_cny"] == 21000 and out["_original_alloc"] == 30000
    out = parse_cio_memo("", fields={"verdict": "ACCUMULATE", "confidence": 0.6,
                                     "suggested_alloc_aud": 4321})
    assert out["alloc_cny"] == 4321
    # 老存档（CNY 字段）照常解析
    assert parse_cio_memo("VERDICT: HOLD\nSUGGESTED_ALLOC_CNY: -500\n")["alloc_cny"] == -500


def test_aud_summary_counts_aud_cash_and_values_in_aud(tmp_path):
    _aud()
    from openinvest.utils.fx import total_portfolio_value
    from openinvest.utils.portfolio_summary import portfolio_summary_text
    store = MemoryStore(tmp_path / "memory")
    store.write("user", "user", {"risk_tolerance": "Balanced"}, "")
    store.write("strategy", "strategy", {"target_assets": []}, "")
    store.write("portfolio", "state", {"schema_version": 2, "cash": {"AUD": 3000.0, "CNY": 470.0},
                                       "holdings": [{"symbol": "NDQ.AX", "units": 10, "avg_cost": 50.0,
                                                     "cost_currency": "AUD", "unit_label": "股"}]}, "")
    pm = PortfolioManager(store)
    prices = {"NDQ.AX": 60.0}
    total, _ = total_portfolio_value(pm, prices, "AUD")
    text = portfolio_summary_text(pm, total, prices)
    assert "总资产估算: 3,700 AUD" in text          # 3000 + 470/4.7 + 600
    assert "  - AUD 现金: 3,000 AUD" in text
    assert "可投子弹 (dry_powder): 3,100 AUD（含外币现金按汇率折算）" in text
    assert "  - CNY 现金: 470 CNY" in text
    assert "AUD 市值 600 AUD" in text and "损失 60 AUD / 120 AUD / 210 AUD" in text
    assert "¥" not in text


def test_ledger_reads_allocation_label_currency():
    from openinvest.core.decision_ledger import parse_committee_text
    head = "**Verdict**: HOLD (confidence 0.50)\n**Symbol**: NDQ.AX\n"
    aud = parse_committee_text(head + "**Suggested allocation AUD**: 1200\n")
    assert (aud["alloc_cny"], aud["alloc_currency"]) == (1200.0, "AUD")
    old = parse_committee_text(head + "**Suggested allocation CNY**: -3,000\n")
    assert (old["alloc_cny"], old["alloc_currency"]) == (-3000.0, "CNY")


def test_display_sites_label_base_currency_and_cny_is_unchanged():
    from openinvest.jobs.daily_report_builder import plain_verdict_summary
    v = {"verdict": "ACCUMULATE", "alloc_cny": 1200}
    assert "¥1,200" in plain_verdict_summary(v, None)
    _aud()
    out = plain_verdict_summary(v, None)
    assert "AUD 1,200" in out and "¥" not in out


def test_aggressiveness_scales_with_converted_ceiling(monkeypatch):
    from openinvest.core.committee.cio_parse import parse_cio_memo
    monkeypatch.setenv("INVEST_ALLOC_AGGRESSIVENESS", "0.1")
    cny = parse_cio_memo("VERDICT: ACCUMULATE\nCONFIDENCE: 0.6\nSUGGESTED_ALLOC_CNY: 99999\n")
    aud = parse_cio_memo("VERDICT: ACCUMULATE\nCONFIDENCE: 0.6\nSUGGESTED_ALLOC_AUD: 99999\n",
                         alloc_ceiling=21000)                  # 21000/100000 的比例
    assert cny["alloc_cny"] == 10000 and aud["alloc_cny"] == 2100


def test_label_roundtrip_through_session_list_parser():
    from openinvest.connectors.web_api.routers.committee_sessions import _parse_committee_header
    text = ("**Verdict**: HOLD (confidence 0.50)\n**Dominant view**: quant\n"
            "**Suggested allocation AUD**: 1200\n")
    assert _parse_committee_header(text)[3] == 1200


def test_load_skill_skips_fx_lookup_when_no_placeholder(monkeypatch):
    import openinvest.utils.base_currency as bc
    calls = []
    monkeypatch.setattr(bc, "prompt_vars", lambda: calls.append(1) or {})
    from openinvest.capabilities.loader import load_skill
    load_skill("macro_strategist")          # 模板里没有 {{ 占位符 -> 不该算币种变量
    assert calls == []


def test_single_invest_cap_floors_in_non_cny_base():
    """硬上限换基准币种向下取整（两位有效数字可能向上舍，钳出来会略高于人民币上限）；CNY 逐位相等"""
    import math
    from openinvest.utils.base_currency import single_invest_cap
    assert single_invest_cap({"max_single_invest_cny": 15000}, {}, "CNY") == 15000
    _aud()
    exact = 15000 * AUD_PER_CNY
    got = single_invest_cap({"max_single_invest_cny": 15000}, {})
    assert got == math.floor(exact) and got <= exact
