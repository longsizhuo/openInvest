"""契约：Risk Officer 的 prompt 不得命令它调用它根本没有的工具（#476，同 Quant 的 #475）。

事故同款：risk_officer.md 写"你有工具可调用：query_dreaming_insights / get_recent_committee_verdicts"，
但 debate.py 对 Risk 传 search_enabled=False（无 tools schema），遥测 0 次工具调用。
这两份数据其实已作为 `# 用户当前持仓` / `# 长期行为模式 (Dreaming)` 块喂进了 user message。

钉 core.committee.debate._create_agent（run_committee 在该命名空间解析工厂）。不联网。
"""
import openinvest.core.committee.debate as debate
from openinvest.capabilities.tools import TOOL_IMPL


class _Agent:
    last_tool_calls: list = []

    def __init__(self, role, inputs):
        self.role, self.inputs = role, inputs

    def run(self, ctx):
        self.inputs.append((self.role, ctx))
        return "SIGNAL: neutral\nSTRENGTH: 5\nONE_LINER: stub\nVERDICT: HOLD\nCONFIDENCE: 0.5\n"

    def tool_call_summary(self):
        return ""


def test_risk_agents_have_no_tools_and_prompts_name_none(monkeypatch):
    created, inputs = [], []

    def fake_create_agent(system_prompt, *, role="unknown", **kw):
        created.append((role, system_prompt, kw.get("search_enabled", True)))
        return _Agent(role, inputs)

    monkeypatch.setattr(debate, "_create_agent", fake_create_agent)
    debate.run_committee(
        asset={"symbol": "TEST.X", "display_name": "Test"},
        market_data="(stub market data)", macro_view="(stub macro)",
        portfolio_summary="(stub portfolio)",
        persist_to_memory=False, max_debate_rounds=2,
    )

    risks = [(p, s) for (r, p, s) in created if r == "risk"]
    assert len(risks) == 2, "Round 1 + Round 2 各一个 Risk agent"
    for prompt, search_enabled in risks:
        assert search_enabled is False, "Risk 无工具；若改为开工具，请同步改 risk_officer.md 的措辞"
        for tool in TOOL_IMPL:
            assert f"{tool}(" not in prompt, f"Risk prompt 不得给无工具的 agent 写调用 {tool}(...)"
        assert "你有工具可调用" not in prompt

    # prompt 指向的块标题必须真是 Round 1 喂给 Risk 的块标题（防标题改了 prompt 没跟）
    opening = risks[0][0]
    risk_r1_input = next(ctx for (role, ctx) in inputs if role == "risk")
    for heading in ("# 用户当前持仓", "# 长期行为模式 (Dreaming)"):
        assert heading in risk_r1_input
    assert "不编造" in opening and "账户承受" in opening and "当前小幅反弹不能抹去" in opening
