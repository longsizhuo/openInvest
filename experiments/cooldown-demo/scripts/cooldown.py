"""冷静期演示：委员会照常辩论，对外只给正反理由纪要，不给裁决。

生产代码零改动：这里调 run_committee_session 跑完整委员会（CIO 照跑，裁决在进程内
丢弃），再让书记员把 Macro / Quant / Risk 的辩论整理成正反理由，过逐字闸才返回。
对外出口只有 debate_summary() 的返回值——公开演示的服务只能暴露它，不能暴露
openinvest 的 MCP / web_api（那些出口都带裁决）。

用法（必须顾问模式 + 独立 INVEST_HOME，否则 CIO 裁决会落进真实账本）：
    INVEST_HOME=~/openinvest-demo INVEST_ADVISORY_MODE=1 \
        uv run python experiments/cooldown-demo/scripts/cooldown.py 510300.SS
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List

from openinvest.core.committee.agent_io import AGENT_UNAVAILABLE_MARKER, _ask, _create_agent
from openinvest.core.committee.debate import _format_debate_history
from openinvest.utils.advisory import is_advisory_mode

SCRIBE_PROMPT = Path(__file__).with_name("scribe.md").read_text(encoding="utf-8")
DISCLAIMER = "\n\n---\n以上是多个 AI 角色辩论的整理，不构成投资建议。要不要做、做多少，由你自己决定。"

# 逐字闸：书记员 prompt 是第一道，这里是第二道。命中任意一条 = 纪要里有结论性表述。
VERDICT_LANGUAGE = re.compile(
    "|".join([
        # 裁决枚举；中文紧挨英文时 \b 不成立（汉字也是 \w），所以只看两侧是不是 ASCII 字母
        r"(?<![A-Za-z])(?:STRONG[_ ]?BUY|BUY|SELL|HOLD|ACCUMULATE|TRIM)(?![A-Za-z])",
        r"(?:建议|应该|应当|可以考虑|可考虑|不妨|适合|值得)[^。；;，,\n]{0,6}?"
        r"(?:买|卖|加仓|减仓|建仓|清仓|持有|止损|止盈|抄底|观望|入场|离场|配置|定投|赎回|申购|上车)",
        r"仓位|首笔|目标价|止损[价位线点]|止盈[价位线点]|买回[价点]|[买卖]点|入场点|胜率",
        # 同一分句里数字和支撑/阻力挨得近就算价位（实跑漏过"4.30 被其视为下方支撑"）
        r"(?:支撑|阻力)[^。；;，,\n]{0,12}\d|\d[^。；;，,\n]{0,12}(?:支撑|阻力)",
        r"[¥￥]\s*\d|\d[\d,]*(?:\.\d+)?\s*(?:万?元|块钱)",
        r"\d+(?:\.\d+)?\s*%\s*的?(?:概率|可能性|机会)|概率\s*(?:约|为|是|有|高达|只有|仅)?\s*\d",
        r"(?<![A-Za-z])(?:should|recommend(?:ed|s)?|consider)\s+"
        r"(?:buy|sell|add|trim|hold|buying|selling|adding|trimming|holding)(?![A-Za-z])",
    ]),
    re.IGNORECASE,
)

# 闸查的串必须就是用户看到的串（parser differential）：纪要按 Markdown 渲染，
# `建*议*买入` 渲染出来是"建议买入"，零宽字符 / 全角字母 / HTML 实体 / 繁体同理。
# 顾问模式下群友可加新闻源，新闻经 Macro 进书记员上下文，是现实的注入入口。
_TRAD_TO_SIMP = str.maketrans(
    "議應該當慮適買賣倉減損場離觀贖購車筆標價點勝撐萬塊錢機會約為達僅線",
    "议应该当虑适买卖仓减损场离观赎购车笔标价点胜撑万块钱机会约为达仅线",
)
# 西里尔/希腊字母里长得像拉丁字母的（ВUY、НОLD），渲染视图里折回拉丁
_CONFUSABLES = str.maketrans(dict(pair for pair in (
    "АA ВB ЕE КK МM НH ОO РP СC ТT ХX УY аa еe оo рp сc уy хx ѕs іi јj "
    "ΑA ΒB ΕE ΗH ΙI ΚK ΜM ΝN ΟO ΡP ΤT ΥY ΧX οo νv"
).split()))
# 纪要里没有任何正当理由出现 HTML 标签/注释、HTML 实体、格式控制字符（零宽/双向覆盖/软连字符）
_SUSPICIOUS_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z!][^>]*>|&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z]+);")


def normalize(text: str) -> str:
    """闸查的规范形：NFKC（全角→半角）+ 繁→简（闸关键词涉及的字）。

    只用来查、不返回：折叠只会让查的串更"标准"、更容易命中，人读原文和读规范形是
    同一个意思，所以返回原文不构成渲染差异；返回规范形反而会把中文标点改成半角。"""
    return unicodedata.normalize("NFKC", text or "").translate(_TRAD_TO_SIMP)


def _rendered_view(t: str) -> str:
    """模拟 Markdown 渲染后人读到的字串：链接只留文字，去掉组合附加符、同形字折回拉丁，
    再删空白和所有 Markdown 语法符（强调/代码/转义/链接括号/标题/引用/表格/列表）。
    只用来查，不返回——删得狠只会多拦（多一次重写），不会漏拦。"""
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)     # 行内链接 / 图片
    t = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", t)       # 引用式链接
    t = "".join(c for c in t if unicodedata.category(c) not in ("Mn", "Me"))
    return re.sub(r"[\s\\*_~`\[\]()!#>|+\-]+", "", t.translate(_CONFUSABLES))


def find_verdict_language(text: str) -> List[str]:
    """纪要里命中闸的片段（去重排序）；空列表 = 放行。

    查两个视图：规范形本身，以及 _rendered_view 模拟的渲染结果。
    HTML 标签/实体、格式控制字符直接算命中。
    ponytail: 关键词闸的上限是同义改写；要更稳就加 LLM 二审，或让前端按纯文本渲染
    """
    t = normalize(text)
    hits = {m.group(0) for view in (t, _rendered_view(t)) for m in VERDICT_LANGUAGE.finditer(view)}
    if _SUSPICIOUS_MARKUP.search(t) or any(unicodedata.category(c) == "Cf" for c in t):
        hits.add("<可疑标记>")
    return sorted(hits)


def summarize(symbol: str, display_name: str, brief: str,
              ask: Callable[[str, str], str] | None = None) -> Dict[str, Any]:
    """书记员整理辩论。命中闸 → 带着命中片段重写一次；还命中或 LLM 不可用 → 整份拦下
    （fail closed，不做局部删改——删掉半句的纪要比没有更误导）。"""
    system = SCRIBE_PROMPT.replace("{{asset_name}}", display_name).replace("{{asset_symbol}}", symbol)
    ask = ask or (lambda sys_prompt, ctx: _ask(_create_agent(
        sys_prompt, search_enabled=False, temperature=0.2, role="scribe", asset=symbol,
        round_label="scribe"), ctx))
    hits: List[str] = []
    for _ in (1, 2):
        ctx = brief
        if hits:
            ctx += ("\n\n=== 上一版被服务端拦下 ===\n出现了禁止的表述：" + "、".join(hits)
                    + "。重写：只保留理由，不要任何结论、金额、仓位、价格目标、概率数字，"
                    + "不要 HTML 或特殊控制字符。")
        summary = ask(system, ctx)
        hits = find_verdict_language(summary)
        if not hits and AGENT_UNAVAILABLE_MARKER not in summary:
            return {"symbol": symbol, "debate_summary": summary.strip() + DISCLAIMER}
    return {"status": "error", "error": "辩论纪要未通过无裁决检查，已拦截"}


def debate_summary(symbol: str, max_debate_rounds: int = 1) -> Dict[str, Any]:
    """跑一次委员会，只返回书记员纪要。返回值是白名单构造的，不含 verdict / 金额 / 置信度。"""
    if not is_advisory_mode():
        raise RuntimeError(
            "冷静期演示只在 INVEST_ADVISORY_MODE=1 + 独立 INVEST_HOME 下跑"
            "（否则 CIO 裁决和路径快照会落进真实账本）"
        )
    from openinvest.core.committee_runner import run_committee_session

    out = run_committee_session(symbols=[symbol], max_debate_rounds=max_debate_rounds)
    res = (out.get("asset_committees") or {}).get(symbol) or {}
    report = res.get("report") if isinstance(res, dict) else None
    if report is None:
        return {"status": "error", "error": (res.get("error") if isinstance(res, dict) else None)
                or "committee failed"}
    # 书记员只看三个角色的辩论 + 确定性事实（to_cio_brief 不含 CIO memo）
    brief = report.to_cio_brief()
    debate = res.get("debate") or {}
    quant, risk = debate.get("quant_history") or [], debate.get("risk_history") or []
    if len(quant) > 1:
        brief += "\n\n=== 完整辩论历史（含所有 cross-challenge 轮）===\n" + _format_debate_history(quant, risk)
    name = report.asset.get("display_name") or symbol
    return summarize(symbol, name, brief)


if __name__ == "__main__":
    print(json.dumps(debate_summary(sys.argv[1]), ensure_ascii=False, indent=2))
