"""冷静期演示的闸与出口。不在 CI 里（CI 只跑 tests/），改这里的代码前后手动跑：

    uv run pytest experiments/cooldown-demo/scripts/ -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import cooldown


@pytest.mark.parametrize("text", [
    "建议分批买入",
    "宏观角色裁决HOLD",                 # 汉字紧挨英文，\b 失效的情形
    "accumulate on dips",
    "首笔 ¥2,700",
    "仓位上限 10%",
    "支撑3.90，阻力4.30",
    "价格下方有 4.30 支撑参考",         # 实跑 DeepSeek 时漏过的语序
    "价格 4.30 被其视为下方支撑",       # 同上，数字和支撑之间隔了几个字
    "MA120 4.76 为上方阻力",
    "期末低于现价概率 48%",
    "有 60% 的概率反弹",
    "适合长期持有",
    "可以考虑逢低加仓",
    "投入 2000 元",
    "You should buy now",
    # 绕过手法：闸查的必须是渲染后用户看到的东西
    "建*议*买入",                       # Markdown 强调拆词
    "建 议 买 入",
    "[建](https://x.co)议买入",
    "[建][1]议买入\n\n[1]: https://x.co",  # 引用式链接
    "建\\议买入",                       # Markdown 转义
    "| 建 | 议买入 |",                   # 表格
    "建議買入",                         # 繁体
    "ＢＵＹ",                           # 全角
    "B​UY",                       # 零宽字符
    "建̸议买入",                   # 组合附加符
    "ВUY",                        # 西里尔 В 冒充 B
    "建<!-- -->议买入",                 # HTML 注释
    "&#24314;议买入",                   # HTML 实体
])
def test_gate_blocks_conclusions(text):
    assert cooldown.find_verdict_language(text)


@pytest.mark.parametrize("text", [
    "Quant 指出近两年价格分位 3%，RSI 40.6，处于低位",
    "值得注意的是，买方力量偏弱",
    "利率应该会继续上行，对成长股估值是压力",
    "美债规模 36万亿",
    "the household budget",
    "Risk Officer 提醒：这只基金过去两年最大回撤 -48%",
])
def test_gate_passes_plain_analysis(text):
    assert cooldown.find_verdict_language(text) == []


def _asker(replies):
    seen = []

    def ask(system, ctx):
        seen.append(ctx)
        return replies.pop(0)
    return ask, seen


def test_rewrites_once_then_passes():
    ask, seen = _asker(["- 建议分批买入", "## 支持的理由\n- 价格分位 3%，RSI 40（低位）"])
    out = cooldown.summarize("X", "示例", "brief", ask=ask)
    assert set(out) == {"symbol", "debate_summary"}
    assert "价格分位 3%，RSI 40（低位）" in out["debate_summary"]   # 原文返回，中文标点不被改成半角
    assert "不构成投资建议" in out["debate_summary"]
    assert "建议分批买" in seen[1]                          # 命中片段喂回去重写


def test_fails_closed_after_second_hit():
    ask, _ = _asker(["- 建议买入", "- 首笔 ¥2,700"])
    assert "拦截" in cooldown.summarize("X", "示例", "brief", ask=ask)["error"]


def test_unavailable_llm_is_blocked():
    m = cooldown.AGENT_UNAVAILABLE_MARKER
    ask, _ = _asker([f"{m} reason=x", f"{m} reason=x"])
    assert "error" in cooldown.summarize("X", "示例", "brief", ask=ask)


def test_refuses_outside_advisory_mode(monkeypatch):
    monkeypatch.delenv("INVEST_ADVISORY_MODE", raising=False)
    with pytest.raises(RuntimeError, match="INVEST_ADVISORY_MODE"):
        cooldown.debate_summary("GC=F")


def test_output_is_whitelisted_and_scribe_never_sees_cio(monkeypatch):
    """委员会结果里挂着 verdict / CIO memo / path_reference（概率、买回点原文），
    出口只能有书记员纪要，书记员的输入里也不能有 CIO 的东西。"""
    monkeypatch.setenv("INVEST_ADVISORY_MODE", "1")
    report = SimpleNamespace(
        asset={"symbol": "GC=F", "display_name": "黄金"},
        cio_memo="【裁决结论】ACCUMULATE 首笔 ¥2,700",
        to_cio_brief=lambda: "=== MACRO ===\n利率上行",
    )
    monkeypatch.setattr("openinvest.core.committee_runner.run_committee_session",
                        lambda **kw: {"asset_committees": {"GC=F": {
                            "verdict": {"verdict": "ACCUMULATE", "alloc_cny": 2700},
                            "report": report,
                            "debate": {"quant_history": ["q1"], "risk_history": ["r1"]},
                            "path_reference": "期末低于现价概率 48%",
                        }}})
    seen = []
    monkeypatch.setattr(cooldown, "_create_agent", lambda sys_prompt, **kw: sys_prompt)
    monkeypatch.setattr(cooldown, "_ask", lambda agent, ctx: seen.append(ctx) or "## 反对的理由\n- 利率上行")
    out = cooldown.debate_summary("GC=F")
    assert set(out) == {"symbol", "debate_summary"}
    assert "ACCUMULATE" not in seen[0] and "2,700" not in seen[0]
