"""holdings_import —— 自由文本 / CSV 持仓描述 → 结构化 v2 持仓（单一可信源）。

被三个 entry 复用，避免 prompt / 解析逻辑漂移：
  - onboarding：scripts/skill_cmds/lifecycle_cmds.py:cmd_init（re-export 本模块的
    _HOLDINGS_PARSE_SYSTEM_PROMPT / _parse_holdings_with_llm，保持历史 monkeypatch 命中）
  - Web API：connectors/web_api/routers/holdings_write.py:POST /api/holdings/import
  - CLI：scripts/skill.py `import` 子命令

设计：parse 只读不写（返回预览给用户确认）；commit 走 with_portfolio_tx 且**非破坏**
（只加 portfolio 里还没有的 symbol、cash 只填当前为 0 的币种），重复导入幂等、不覆盖已有数据。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

_HOLDINGS_PARSE_SYSTEM_PROMPT = """你是金融数据解析助手。把用户的自然语言持仓描述解析成严格 JSON。

输出 schema（无 markdown 无解释）：
{
  "cash": {"<currency_code>": <number>},
  "holdings": [
    {
      "symbol": "<yfinance ticker>",
      "kind": "<stock|etf|fund|metal|crypto|bond|other>",
      "units": <number>,
      "unit_label": "<股|份|克|个|盎司>",
      "avg_cost": <number>,
      "market_value": <number, optional>,
      "pnl": <number, optional>,
      "cost_currency": "<currency_code>",
      "channel": "<券商/银行渠道，没说就 '未指定'>",
      "display_name": "<易读名>"
    }
  ]
}

Symbol 映射规则：
- 沪市股票/ETF: 6 位代码 + .SS  (510300 → 510300.SS, 600519 → 600519.SS)
- 深市: 6 位 + .SZ
- 港股: 5 位 + .HK
- 美股: 直接 ticker (AAPL, TSLA)
- 澳股: ticker + .AX (NDQ.AX)
- 加密: 大写 + -USD (BTC-USD, ETH-USD)
- 黄金/纸黄金/积存金: GC=F (浙商/工行/招行积存金都映射到 GC=F，渠道写银行名)
- 中国场外公募基金: FUND:<六位基金代码>（例如 162201 → FUND:162201），kind=fund；
  绝对不要追加 .SS/.SZ。用户只给“持有金额/持仓收益”而没给份额和成本时，
  原样输出 market_value 和 pnl，units/avg_cost 先填 0，后端会按最新单位净值换算。
- 货币基金/余额宝/朝朝宝/银行理财: 不放 holdings，并入 cash

币种规则：
- 用户没说币种 → CNY
- 美元/USD → USD; 澳元/AUD → AUD; 港元/HKD → HKD

数值规则：
- 用户没说均价 → avg_cost: 0
- 用户没说渠道 → channel: "未指定"
- 缺字段就用合理默认，不要抛错

输入可能是自然语言，也可能是 CSV / 表格粘贴 —— 都按上面规则解析。
只输出 JSON 对象本身。"""


def _parse_holdings_with_llm(
    description: str,
    api_key: str,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """调 LLM（默认 DeepSeek，可换千问/智谱）把"510300 ETF 3000股 4.2元 + 余额宝 5万"
    这种自然语言转成 v2 持仓 JSON。

    返回 {"cash": {...}, "holdings": [...]}.
    出错时抛异常，让调用方决定回退策略（onboarding 不阻塞、API 转 502）。

    base_url / model 不传 → 走 utils.llm 默认值（LLM_*，fallback DEEPSEEK_*）
    """
    from openai import OpenAI

    from openinvest.utils.llm import get_llm_config_safe, needs_thinking_disabled
    _ak, _bu, _m, _p = get_llm_config_safe()
    base_url = base_url or _bu
    model = model or _m

    client = OpenAI(api_key=api_key, base_url=base_url)
    # DeepSeek v4 默认 thinking 模式，关掉走旧 chat 行为；千问/智谱不需要
    extra_body = {}
    if needs_thinking_disabled(model):
        extra_body["thinking"] = {"type": "disabled"}
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _HOLDINGS_PARSE_SYSTEM_PROMPT},
            {"role": "user", "content": description},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
        extra_body=extra_body or None,
    )
    raw = resp.choices[0].message.content or "{}"
    parsed = json.loads(raw)
    # 兜底归一化：保证两个顶层 key 都在
    parsed.setdefault("cash", {})
    parsed.setdefault("holdings", [])
    return enrich_fund_holdings(parsed)


def parse_holdings(content: str) -> Dict[str, Any]:
    """freeform 文本 / CSV → {cash, holdings} 预览。Key 从 utils.llm 配置解析。

    无 LLM key → ValueError（调用方转 400 / 报错，不静默吞）。
    """
    from openinvest.utils.llm import get_llm_config_safe
    api_key, base_url, model, _ = get_llm_config_safe()
    if not api_key:
        raise ValueError("未配置 LLM_API_KEY / DEEPSEEK_API_KEY，无法解析持仓文本")
    return _parse_holdings_with_llm(content, api_key=api_key, base_url=base_url, model=model)


_KIND_MAP = {"stock": "equity"}  # parser 出 "stock"，HoldingV2 schema 要 "equity"
_VALID_KINDS = {"equity", "etf", "metal", "crypto", "bond", "fund", "other"}


def _first_number(h: Dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = h.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def enrich_fund_holdings(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """规范化场外基金 symbol，并由“持有金额 + P&L + 最新净值”反推份额/均价。

    只改变 ``kind=fund`` 的条目。净值源失败时保留 0 份，调用方预览可以明确看到
    未能转换，绝不编造份额。
    """
    from openinvest.utils.eastmoney_fund import (
        canonical_fund_symbol,
        fetch_fund_nav,
    )

    out = dict(parsed)
    enriched: List[Dict[str, Any]] = []
    for raw in parsed.get("holdings") or []:
        h = dict(raw)
        kind = _KIND_MAP.get(str(h.get("kind") or "").lower(), str(h.get("kind") or "").lower())
        if kind != "fund":
            enriched.append(h)
            continue

        h["symbol"] = canonical_fund_symbol(str(h.get("symbol") or ""))
        h["proxy_kind"] = "eastmoney_fund"
        h["cost_currency"] = "CNY"
        h["unit_label"] = str(h.get("unit_label") or "份")

        units = _first_number(h, "units")
        avg_cost = _first_number(h, "avg_cost")
        market_value = _first_number(
            h, "market_value", "current_value", "holding_amount", "market_value_cny",
        )
        pnl = _first_number(h, "pnl", "pnl_cny", "holding_pnl")
        if units <= 0 and market_value > 0:
            snap = fetch_fund_nav(h["symbol"])
            if snap is not None and snap.nav > 0:
                units = market_value / snap.nav
                cost_basis = market_value - pnl
                avg_cost = cost_basis / units if cost_basis > 0 else 0.0
                h["units"] = round(units, 6)
                h["avg_cost"] = round(avg_cost, 6)
                h["nav_at_import"] = snap.nav
                h["nav_date_at_import"] = snap.nav_date
                h["market_value_at_import"] = round(market_value, 2)
                h["pnl_at_import"] = round(pnl, 2)
        enriched.append(h)
    out["holdings"] = enriched
    return out


def _normalize_holding(h: Dict[str, Any]) -> Dict[str, Any]:
    """parser 输出 → portfolio 存储 shape（对齐 HoldingCreateRequest 字段 + kind 映射）。"""
    sym = str(h.get("symbol") or "").strip()
    kind = _KIND_MAP.get(str(h.get("kind") or "").lower(), str(h.get("kind") or "").lower())
    if kind not in _VALID_KINDS:
        kind = "other"
    if kind == "fund":
        from openinvest.utils.eastmoney_fund import canonical_fund_symbol
        sym = canonical_fund_symbol(sym)
    return {
        "symbol": sym,
        "kind": kind,
        "units": float(h.get("units") or 0),
        "unit_label": str(h.get("unit_label") or ("份" if kind == "fund" else "股")),
        "avg_cost": float(h.get("avg_cost") or 0),
        "cost_currency": str(h.get("cost_currency") or "CNY").upper(),
        "channel": str(h.get("channel") or "未指定"),
        "display_name": str(h.get("display_name") or sym),
        "proxy_kind": str(h.get("proxy_kind") or ("eastmoney_fund" if kind == "fund" else "direct")),
    }


def commit_parsed(pm: Any, parsed: Dict[str, Any]) -> Dict[str, Any]:
    """把 parse 结果**非破坏**写入 portfolio：
      - holdings：只加 portfolio 里**还没有**的 symbol；已存在的跳过并报告（不覆盖 units/均价）
      - cash：每个币种只在当前余额为 0 时填入；已有余额的跳过并报告（不覆盖）
    重复导入因此幂等，绝不撕裂已有真实数据。返回 summary 给调用方展示。
    """
    holdings_in = parsed.get("holdings") or []
    cash_in = parsed.get("cash") or {}
    added: List[str] = []
    skipped: List[str] = []
    cash_set: Dict[str, float] = {}
    cash_skipped: Dict[str, float] = {}

    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        existing = {str(h.get("symbol")) for h in holdings}
        for h in holdings_in:
            norm = _normalize_holding(h)
            if not norm["symbol"]:
                continue
            if norm["symbol"] in existing:
                skipped.append(norm["symbol"])
                continue
            holdings.append(norm)
            existing.add(norm["symbol"])
            added.append(norm["symbol"])
        p["holdings"] = holdings

        cash = dict(p.get("cash") or {})
        for ccy, amt in cash_in.items():
            c = str(ccy).upper()
            if float(cash.get(c, 0) or 0) > 0:
                cash_skipped[c] = float(amt or 0)
            else:
                cash[c] = round(float(amt or 0), 2)
                cash_set[c] = cash[c]
        p["cash"] = cash

    pm._reload()
    return {
        "added_holdings": added,
        "skipped_holdings": skipped,
        "cash_set": cash_set,
        "cash_skipped": cash_skipped,
    }
