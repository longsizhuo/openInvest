"""P1 端到端契约：模拟 provider 不支持 DeepSeek JSON Output（json 模式吐非法 JSON）→
run_committee 必须【优雅回退】文本模式重问一次、regex 解析出 verdict，不崩、不丢裁决。
对照：provider 支持时只建一次 CIO、不双调用。

钉 core.committee.debate._create_agent —— run_committee 在该命名空间解析工厂
（façade patch 无效，见 CLAUDE.md）。不真调 LLM。"""
import core.committee.debate as debate


class _StubAgent:
    def __init__(self, role, response_format):
        self.role = role
        self.response_format = response_format
        self.last_tool_calls = []

    def run(self, ctx):
        if self.role != "cio":
            return "SIGNAL: neutral\nONE_LINER: stub analyst\nSTRENGTH: weak"
        if self.response_format:
            # json 模式：模拟 provider 不支持 response_format → 吐非法 JSON（json.loads 失败）
            return "抱歉我不支持 json_object 这不是合法 JSON {verdict: HOLD"
        # 文本回退路径：合法 VERDICT 文本，regex 能解析
        return ("VERDICT: HOLD\nCONFIDENCE: 0.5\nDOMINANT_VIEW: risk\n"
                "SUGGESTED_ALLOC_CNY: 0\nTRIM_REASON: N/A\n")

    def tool_call_summary(self):
        return ""


class _JsonOkAgent(_StubAgent):
    def run(self, ctx):
        if self.role != "cio":
            return "SIGNAL: neutral\nONE_LINER: stub\nSTRENGTH: weak"
        return ('{"verdict":"ACCUMULATE","confidence":0.6,"dominant_view":"quant",'
                '"suggested_alloc_cny":3000,"trim_reason":null,'
                '"memo":"完整投行级分析 prose（应进 cio_memo 展示）"}')


def _run(monkeypatch, json_supported):
    created = []

    def fake_create_agent(system_prompt, *, role="unknown", response_format=None, **kw):
        created.append((role, bool(response_format)))
        cls = _JsonOkAgent if json_supported else _StubAgent
        return cls(role, response_format)

    monkeypatch.setattr(debate, "_create_agent", fake_create_agent)
    monkeypatch.setenv("INVEST_FORCE_JSON_OUTPUT", "1")   # 强制走 json 尝试
    monkeypatch.delenv("INVEST_CIO_THINKING", raising=False)

    res = debate.run_committee(
        asset={"symbol": "GC=F", "display_name": "Gold"},
        market_data="(stub market data)", macro_view="(stub macro)",
        portfolio_summary="(stub portfolio)",
        persist_to_memory=False, max_debate_rounds=1,
    )
    return res, created


def test_unsupported_provider_graceful_fallback(monkeypatch):
    res, created = _run(monkeypatch, json_supported=False)
    cio_calls = [rf for (role, rf) in created if role == "cio"]
    # CIO 被建两次：先 json(response_format=True)，非法 JSON → 文本回退(False)
    assert cio_calls == [True, False], f"应 json→文本回退两次，实际 {cio_calls}"
    # 回退后 regex 解析出裁决，不是 UNCLEAR
    assert res["verdict"]["verdict"] == "HOLD"
    # cio_memo 是文本备忘（含 VERDICT），不是那串非法 JSON
    assert "VERDICT" in res["report"].cio_memo


def test_supported_provider_no_double_call(monkeypatch):
    res, created = _run(monkeypatch, json_supported=True)
    cio_calls = [rf for (role, rf) in created if role == "cio"]
    assert cio_calls == [True], f"支持时只建一次 CIO，实际 {cio_calls}"
    assert res["verdict"]["verdict"] == "ACCUMULATE"
    assert res["verdict"]["alloc_cny"] == 3000
    # prose memo 被提取展示（不是裸 JSON）
    assert "prose" in res["report"].cio_memo
    assert not res["report"].cio_memo.lstrip().startswith("{")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
