"""D11 P2 舰队试跑契约：T2 CONFIDENCE 定义只进舰队 T2 臂，live CIO prompt 逐字节不变。

钉 core.committee.debate._create_agent 抓 CIO 实际收到的 system prompt（同
test_cio_json_fallback_e2e 的做法），不真调 LLM。"""
import hashlib

import openinvest.core.committee.debate as debate
from openinvest.capabilities.committee.cio import build_cio_prompt
from scripts import backtest_committee as bc

ASSET = {"symbol": "QQQ", "display_name": "QQQ"}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _cio_prompts(monkeypatch):
    """跑一次 stub 委员会，返回 CIO agent 收到的 system prompt 列表。"""
    seen = []

    class _Stub:
        last_tool_calls = []

        def __init__(self, role):
            self.role = role

        def run(self, ctx):
            if self.role == "cio":
                return "VERDICT: HOLD\nCONFIDENCE: 0.5\nDOMINANT_VIEW: risk\nSUGGESTED_ALLOC_CNY: 0\n"
            return "SIGNAL: neutral\nONE_LINER: stub\nSTRENGTH: weak"

        def tool_call_summary(self):
            return ""

    def fake_create_agent(system_prompt, *, role="unknown", **kw):
        if role == "cio":
            seen.append(system_prompt)
        return _Stub(role)

    monkeypatch.setattr(debate, "_create_agent", fake_create_agent)
    # 关 JSON 模式 → CIO 只建一次、走文本 prompt（JSON 变体由 test_t2_variant_adds_definition_and_drops_085_rule 覆盖）
    monkeypatch.setattr("openinvest.utils.llm.supports_json_output", lambda: False)
    monkeypatch.delenv("INVEST_CIO_THINKING", raising=False)
    debate.run_committee(asset=ASSET, market_data="(stub)", macro_view="(stub)",
                         portfolio_summary="(stub)", persist_to_memory=False,
                         max_debate_rounds=1)
    return seen


def test_t2_variant_adds_definition_and_drops_085_rule():
    for json_mode in (False, True):
        live = build_cio_prompt(ASSET, json_mode=json_mode)
        t2 = bc.build_cio_prompt_t2(ASSET, json_mode=json_mode)
        assert "confidence ≥ 0.85" in live and "CONFIDENCE 的定义" not in live
        assert "0.85" not in t2
        assert "CONFIDENCE 的定义" in t2 and "30 个日历天" in t2 and "正常波动带" in t2
        # 变体只动这两处：删掉定义块 + 还原 0.85 行 → 与 live 逐字节相同
        start = t2.index("**📏 CONFIDENCE 的定义")
        end = t2.index(bc._CIO_T2_ANCHOR)
        restored = (t2[:start] + t2[end:]).replace(
            "1. **三方一致**: 按一致方向给 verdict", bc._CIO_085_RULE)
        assert restored == live


def test_live_cio_prompt_unchanged_and_t2_arm_scoped(monkeypatch):
    before = _sha(build_cio_prompt(ASSET))

    live_seen = _cio_prompts(monkeypatch)
    with bc._t2_arm():
        t2_seen = _cio_prompts(monkeypatch)
    after_seen = _cio_prompts(monkeypatch)

    # live（不进开关）CIO 拿到的就是 build_cio_prompt 原样；开关退出后还原
    assert [_sha(p) for p in live_seen] == [before]
    assert [_sha(p) for p in after_seen] == [before]
    assert debate.build_cio_prompt is build_cio_prompt
    assert _sha(build_cio_prompt(ASSET)) == before
    # 开关内 run_committee 真的用了变体（patch 目标钉对了命名空间）
    assert len(t2_seen) == 1 and "CONFIDENCE 的定义" in t2_seen[0] and "0.85" not in t2_seen[0]


def test_t2_arm_flag_requires_prospective(monkeypatch):
    import pytest
    monkeypatch.setattr("sys.argv", ["backtest_committee", "--t2-confidence-arm", "--days", "1"])
    with pytest.raises(SystemExit, match="--prospective"):
        bc.main()
