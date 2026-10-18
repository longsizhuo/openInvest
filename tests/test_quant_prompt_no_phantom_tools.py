"""B3 契约：Quant 的 prompt 不得命令它调用它根本没有的工具。

事故：quant.md 写"不要不调——一个负责的分析师会去查多周期对照"，但 debate.py 对 Quant
传 search_enabled=False（无 tools schema）。模型于是在输出里"假装调用"——整段输出只有
一行调用语句、或编造工具返回 JSON（含"假设为 1.05"的成交量比与不存在的历史 SIGNAL）。
多周期对照其实已作为 `# 市场数据` 块喂给了它。

钉 core.committee.debate._create_agent（run_committee 在该命名空间解析工厂）。不联网。
"""
import openinvest.core.committee.debate as debate
from openinvest.capabilities.tools import TOOL_IMPL


class _Agent:
    last_tool_calls: list = []

    def run(self, ctx):
        return "SIGNAL: neutral\nSTRENGTH: 5\nONE_LINER: stub\nVERDICT: HOLD\nCONFIDENCE: 0.5\n"

    def tool_call_summary(self):
        return ""


def test_quant_agents_have_no_tools_and_prompts_name_none(monkeypatch):
    created = []

    def fake_create_agent(system_prompt, *, role="unknown", **kw):
        created.append((role, system_prompt, kw.get("search_enabled", True)))
        return _Agent()

    monkeypatch.setattr(debate, "_create_agent", fake_create_agent)
    debate.run_committee(
        asset={"symbol": "TEST.X", "display_name": "Test"},
        market_data="(stub market data)", macro_view="(stub macro)",
        portfolio_summary="(stub portfolio)",
        persist_to_memory=False, max_debate_rounds=2,
    )

    quants = [(p, s) for (r, p, s) in created if r == "quant"]
    assert len(quants) == 2, "Round 1 + Round 2 各一个 Quant agent"
    for prompt, search_enabled in quants:
        assert search_enabled is False, "Quant 无工具；若改为开工具，请同步改 quant.md 的措辞"
        for tool in TOOL_IMPL:
            assert f"{tool}(" not in prompt, f"Quant prompt 不得给无工具的 agent 写调用 {tool}(...)"
        assert "不要不调" not in prompt

    opening = quants[0][0]
    assert "# 市场数据" in opening and "不得声称" in opening and "不得编造" in opening
