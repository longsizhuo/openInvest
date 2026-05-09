"""每日投资报告 - Investment Committee 模式（4 角色）

替代旧的 main.py + bull/bear/judge/manager 多步骤管线。

流程：
1. Macro Strategist 跑 1 次（跨资产共享）
2. 对每个资产跑 Quant + Risk Officer + CIO（4 角色，但 Macro 是外部传入）
3. 直接拼报告发邮件 — 不再有 manager 综合层（CIO 已经综合）

LLM 调用次数: 1 (macro) + 3 * N (asset committee)
对比旧版: 1 (macro) + 5 * N (debate) + 1 (manager) → 新版省 token
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from dotenv import load_dotenv

from core.committee import run_committee, run_macro_view
from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
from db.market_store import MarketStore
from services.notifier import EmailDeliveryError, send_gmail_notification
from utils.exchange_fee import (
    analyze_multi_timeframe,
    get_cost_report,
    get_history_data,
    get_macro_data,
)
from utils.gold_price import format_gold_report, get_gold_snapshot

load_dotenv()

# 数据陈旧阈值：DB 最新日期距今超过这个天数，仍然能跑但要在 LLM 上下文里
# 显式标注"数据陈旧 N 天"，让 LLM 不要在过期价上面编今天的策略。
STALE_THRESHOLD_DAYS = int(os.getenv("INVEST_PRICE_STALE_DAYS", "3"))

# 硬熔断阈值：超过这个天数的数据**禁止参与决策**。
# 阈值不同的层次：
#   STALE_THRESHOLD_DAYS (3) — 软警告，注入 LLM 上下文里说"数据有点旧"
#   STALE_HARD_ABORT_DAYS (7) — 硬熔断，所有资产都旧到这程度 → daily_report 整个跳过
# 设 7 天因为周末 + 节假日最多 4-5 天没数据是正常的；超过一周是数据源真的挂了。
STALE_HARD_ABORT_DAYS = int(os.getenv("INVEST_HARD_ABORT_STALE_DAYS", "7"))

_MARKET_STORE = MarketStore()


def _get_last_close(
    symbol: str, label: str
) -> Tuple[Optional[float], Optional[int]]:
    """返回 (close_price, age_days)。

    age_days: 0=今天的价、N=N 天前的价、None=完全没数据。
    price=None 时调用方必须显式判空，绝不能用 0 兜底——0 进入估值算式
    会让 NDQ 总值变 0，Risk Officer 看到"集中度爆表"建议清仓，全是数据
    缺失导致的虚假信号。
    """
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    if df.empty:
        print(f"⚠️ {label} 数据缺失: {symbol}")
        return None, None

    price = float(df["Close"].iloc[-1])

    # 算 staleness：DB 最新日期 vs. 今天
    latest_date_str = _MARKET_STORE.get_latest_date(symbol)
    if latest_date_str:
        try:
            latest = datetime.strptime(latest_date_str, "%Y-%m-%d").date()
            age_days = (datetime.now().date() - latest).days
        except Exception:
            age_days = None
    else:
        age_days = None
    return price, age_days


def _format_staleness(label: str, age_days: Optional[int]) -> str:
    """给 portfolio_summary 用的陈旧警告字符串，age_days >= 阈值才输出。
    LLM 看到这段会知道当前估值用的是 N 天前的价，不要假装是今天的市场。"""
    if age_days is None or age_days < STALE_THRESHOLD_DAYS:
        return ""
    return (
        f"\n⚠️ **{label} 价格数据陈旧 {age_days} 天** —— 今日 scraper / yfinance "
        f"未能更新行情，估值基于 {age_days} 天前的收盘价。请在结论里明确标注"
        f"\"基于陈旧数据\"，不要假设当前价仍接近此值。"
    )


def _gather_relevant_insights(store: MemoryStore, asset: Dict[str, Any]) -> str:
    insights_dir = store.root / "insights"
    if not insights_dir.exists():
        return ""
    sym = asset.get("symbol", "").lower().replace("=", "_")
    matches = []
    for f in sorted(insights_dir.glob("*.md")):
        if sym in f.stem.lower() or any(
            tok in f.stem.lower() for tok in ["gold", "ndq"] if tok in sym
        ):
            matches.append(f"## {f.stem}\n{f.read_text(encoding='utf-8')[:600]}")
    return "\n\n".join(matches)


def _portfolio_summary(
    pm: PortfolioManager,
    total_assets_cny: float,
    current_prices: Dict[str, float],
) -> str:
    """详细的用户上下文，给 Risk Officer 压力测试用 (含当前市价 + 浮盈)

    v3 通用化：动态遍历用户实际 holdings，不再写死 NDQ.AX/GC=F。fork 用户
    持仓 510300.SS / AAPL / BTC-USD 等任何 yfinance symbol 都能正确显示。

    current_prices: dict[symbol, price] —— 每个资产当前市价（per asset 币种）
                    黄金特殊：传 'GC=F' → bank_cny_per_gram（含点差，与 cost_currency 一致）
    """
    cash_cny = pm.cash_amount("CNY")
    aud_cash = pm.cash_amount("AUD")
    buffer_cny = float(pm.user.get("exchange_buffer_cny", 0))
    risk_level = str(pm.user.get("risk_tolerance", "Balanced"))
    dry_powder = max(0.0, cash_cny - buffer_cny)

    # 现金部分（多币种通用）
    lines = [
        f"用户风险偏好: {risk_level}",
        f"总资产估算: ¥{total_assets_cny:,.0f}",
        f"  - CNY 现金: ¥{cash_cny:,.0f} (其中应急金 ¥{buffer_cny:,} 不可投)",
        f"  - 可投子弹 (dry_powder): ¥{dry_powder:,.0f}",
    ]
    if aud_cash > 0:
        lines.append(f"  - AUD 现金: ${aud_cash:,.0f}")

    # 持仓部分：遍历实际 holdings，按 unit_label / cost_currency 通用化展示
    real_holdings = [
        h for h in pm.holdings
        if not h.get("is_tracking_only") and float(h.get("units", 0) or 0) > 0
    ]
    if not real_holdings:
        lines.append("  - **当前无实仓持仓**（onboarding 后请通过 GUI/NapCat 添加）")

    for h in real_holdings:
        sym = str(h.get("symbol", ""))
        units = float(h.get("units", 0) or 0)
        cost = float(h.get("avg_cost", 0) or 0)
        unit_label = str(h.get("unit_label", "份"))
        ccy = str(h.get("cost_currency", "CNY"))
        display = h.get("display_name") or sym
        channel = h.get("channel") or ""
        channel_str = f" ({channel})" if channel else ""

        cur = current_prices.get(sym)
        if cur is None or cost <= 0:
            # 缺价 / 无成本时仅显示持仓量
            lines.append(
                f"  - **{display}** ({sym}){channel_str}: "
                f"{units:.4f} {unit_label}, 均价 {cost:.2f} {ccy}/{unit_label}",
            )
            continue

        pnl_pct = ((cur / cost) - 1) * 100
        pnl_local = (cur - cost) * units
        ccy_symbol = "¥" if ccy == "CNY" else ("$" if ccy in ("USD", "AUD") else "")
        lines.append(
            f"  - **{display}** ({sym}){channel_str}: "
            f"{units:.4f} {unit_label}, "
            f"均价 {ccy_symbol}{cost:.2f}, "
            f"现价 {ccy_symbol}{cur:.2f}, "
            f"浮盈 {pnl_pct:+.2f}% (≈ {ccy_symbol}{pnl_local:+,.2f} {ccy})",
        )

    return "\n".join(lines) + "\n"


def _run_gemini_cli_review(prompt: str) -> str:
    print("🤖 [Gemini CLI] 正在生成第二意见...")
    # PATH 上找 gemini，避免硬编码 nvm 路径（每升级 node 版本就失效）
    import shutil
    gemini_cmd = shutil.which("gemini")
    if not gemini_cmd:
        return "Skipped: gemini CLI 不在 PATH"
    try:
        result = subprocess.run(
            [gemini_cmd], input=prompt,
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout.strip()
    except FileNotFoundError:
        return "Skipped: gemini CLI 不可用"
    except Exception as e:
        return f"Skipped: {e}"


# ----------------------------------------------------------------------

def run() -> Dict[str, Any]:
    pm = PortfolioManager()
    store = pm.store
    today = datetime.now().strftime("%Y-%m-%d")

    target_assets = list(pm.strategy.get("target_assets", []))
    if not target_assets:
        return {"status": "skipped", "reason": "no_target_assets"}

    # 估值用的"主资产价格"专门给 NDQ 持仓估值用，不能依赖 target_assets 顺序。
    # 价格 None = 完全没数据（DB + scraper + yfinance 全失败），上层显式跳过该
    # 资产委员会而不是用 0 兜底；age_days = 数据陈旧多少天，超阈值要在 LLM 上下文
    # 里告警。
    data_warnings: list[str] = []   # 累积价格陈旧/缺失的警告，注入 portfolio_summary
    skipped_assets: set[str] = set()  # 完全没价的资产 → 跳过该资产 committee

    # 硬熔断：每个资产的"数据可信度"打标，最后用来判断是否所有资产都崩了
    # status: "fresh" | "stale" (软警告内) | "very_stale" (硬熔断阈值外) | "missing"
    asset_freshness: Dict[str, str] = {}

    def _classify_freshness(price: Optional[float], age_days: Optional[int]) -> str:
        if price is None:
            return "missing"
        if age_days is None or age_days < STALE_THRESHOLD_DAYS:
            return "fresh"
        if age_days < STALE_HARD_ABORT_DAYS:
            return "stale"
        return "very_stale"

    # 通用价格采集 (B3): 遍历 target_assets，按 kind 分发取价
    #   kind=etf/stock/fund/bond → yfinance close（_get_last_close）
    #   kind=metal              → 由下方 get_gold_snapshot 单独处理
    #   未识别 kind             → 也走 yfinance close（兜底）
    # 之前是写死 NDQ.AX 一个分支，fork 用户持有 510300.SS / AAPL 直接被静默跳过
    current_prices: Dict[str, float] = {}     # symbol → 当前价（per asset 币种）
    current_price: Optional[float] = None      # 兼容旧 get_user_status 的 NDQ 价（AUD）

    for asset_cfg in target_assets:
        sym = str(asset_cfg.get("symbol", ""))
        kind = str(asset_cfg.get("kind", ""))
        if not sym or kind == "metal":
            # 金属下面单独处理（gold_price.py 含点差反推）
            continue
        price, age = _get_last_close(sym, sym)
        asset_freshness[sym] = _classify_freshness(price, age)
        if price is None:
            print(f"⛔ {sym} 价格获取完全失败，跳过 committee")
            store.dream_event({"phase": "price_fetch_failed", "symbol": sym, "date": today})
            skipped_assets.add(sym)
            continue
        current_prices[sym] = price
        stale_msg = _format_staleness(sym, age)
        if stale_msg:
            data_warnings.append(stale_msg)
            store.dream_event({
                "phase": "price_stale", "symbol": sym, "age_days": age, "date": today,
            })
        # NDQ.AX 特殊：旧 get_user_status 仍按 NDQ AUD 价 + AUDCNY 折算
        # （单一"主资产"概念，v3 完全去掉时一起拆）
        if sym == "NDQ.AX":
            current_price = price

    rate_price, rate_age = _get_last_close("AUDCNY=X", "汇率")
    if rate_price is None:
        # 汇率拿不到比较罕见但仍要兜底——AUDCNY 的历史均值约 4.7 当作 sentinel
        # 避免直接抛异常让 daily_report 整体挂掉，但要明确告警 LLM
        print("⚠️ AUDCNY=X 完全失败，使用历史均值 4.7 兜底")
        store.dream_event({"phase": "price_fetch_failed", "symbol": "AUDCNY=X", "date": today})
        current_rate = 4.7
        data_warnings.append(
            "\n⚠️ **AUDCNY 汇率今日无法获取，使用历史均值 4.7 兜底**。汇率敏感的 AUD 估值"
            "可能偏差 ±5%，请勿据此做换汇决策。"
        )
    else:
        current_rate = rate_price
        stale_msg = _format_staleness("AUDCNY=X 汇率", rate_age)
        if stale_msg:
            data_warnings.append(stale_msg)
            store.dream_event({"phase": "price_stale", "symbol": "AUDCNY=X",
                               "age_days": rate_age, "date": today})

    # 计算总资产估算（给 Risk Officer 用）—— NDQ 跳过时不算它的市值
    user_status = pm.get_user_status(current_price, current_rate)
    # 从 strategy.target_assets[gold] 拿 price_offset_pct，让估值与用户成本同口径
    # （audit financial C1: 之前 offset_pct=0.0 + spot_cny_per_gram 让浮盈系统性
    # 偏低 1-1.5%）
    gold_offset = 0.0
    for a in target_assets:
        if a.get("symbol") == "GC=F":
            gold_offset = float(a.get("price_offset_pct", 0.0) or 0.0)
            break
    snap = get_gold_snapshot(offset_pct=gold_offset)
    if snap is None:
        asset_freshness["GC=F"] = "missing"
        store.dream_event({"phase": "price_fetch_failed", "symbol": "GC=F", "date": today})
        # yfinance + DB 兜底全失败时跳过黄金 committee
        skipped_assets.add("GC=F")
        gold_now = 0.0
        data_warnings.append(
            "\n⚠️ **黄金现货今日无法获取**（yfinance 实时 + DB 兜底全失败），"
            "本次跳过黄金 committee 分析。"
        )
    else:
        gold_now = snap.bank_cny_per_gram  # 含浙商点差的克价，与用户成本同口径
        # snap.is_stale 表示走了 DB 兜底，没有具体 age_days；保守按 stale 标
        # （硬熔断关心的是"全部都崩"，单 stale 不会触发；想拿到准确 age_days
        # 需要扩 GoldSnapshot dataclass，过度工程暂跳过）
        asset_freshness["GC=F"] = "stale" if snap.is_stale else "fresh"
        if snap.is_stale:
            # DB 兜底返回的是陈旧数据，告知 LLM 不要假装是今天的市场
            store.dream_event({"phase": "gold_price_stale_fallback", "date": today})
            data_warnings.append(
                "\n⚠️ **黄金价格来自 DB 兜底（非实时）**：yfinance 今日不可达，"
                "估值用最近一次成功拉取的价格。请在结论里明确标注'基于陈旧数据'。"
            )

    # ================================================================
    # 硬熔断 (P0-6)：所有 target_asset 价格全废 → daily_report 整个跳过
    #
    # 触发条件：所有 asset 的 freshness 都是 "missing" 或 "very_stale"
    #          （即没有任何可信价格做决策）
    # 行为：
    #   - 不跑委员会（不烧 LLM token）
    #   - 不发邮件（避免发"一份基于垃圾数据的 verdict"出去）
    #   - 不写 daily/<date>.md verdict（避免污染历史）
    #   - 写 dream_event 留 audit trail
    #   - 返回 status="aborted_stale_data" 让 cron / scheduler 知道
    #
    # 不触发的情况（即使数据有问题）：
    #   - 部分资产 fresh + 部分 stale → 跑（跳过 stale 资产单独处理）
    #   - 全 stale 但都在 STALE_HARD_ABORT_DAYS 阈值内 → 跑（LLM 看到 warning）
    # ================================================================
    rated_assets = list(asset_freshness.keys())
    deadly = {"missing", "very_stale"}
    if rated_assets and all(asset_freshness[s] in deadly for s in rated_assets):
        msg = (
            f"所有 {len(rated_assets)} 个目标资产数据全废："
            + ", ".join(f"{s}={asset_freshness[s]}" for s in rated_assets)
        )
        print(f"⛔ STALE DATA HARD ABORT: {msg}")
        store.dream_event({
            "phase": "daily_report_aborted_stale",
            "reason": "all_assets_unusable",
            "asset_freshness": dict(asset_freshness),
            "abort_threshold_days": STALE_HARD_ABORT_DAYS,
            "date": today,
        })
        return {
            "status": "aborted",
            "reason": "stale_data_hard_abort",
            "asset_freshness": dict(asset_freshness),
            "abort_threshold_days": STALE_HARD_ABORT_DAYS,
            "skipped_assets": sorted(skipped_assets),
            "date": today,
            "next_step": (
                f"所有数据源都过时超 {STALE_HARD_ABORT_DAYS} 天或全失败。"
                "检查 yfinance 网络 / DB 是否被定期更新。强制跑（绕过熔断）："
                "INVEST_HARD_ABORT_STALE_DAYS=999 重跑。"
            ),
        }

    # B3 通用化总资产估算：遍历 holdings 按 kind/cost_currency 算
    # gold_now 仍是 GC=F 黄金的"含点差克价"，单独传进 current_prices
    if "GC=F" not in skipped_assets and gold_now > 0:
        current_prices["GC=F"] = gold_now

    total_assets_cny = user_status.cash_cny + user_status.cash_aud * current_rate
    for h in pm.holdings:
        if h.get("is_tracking_only"):
            continue
        sym = str(h.get("symbol", ""))
        if sym in skipped_assets:
            continue
        units = float(h.get("units", 0) or 0)
        if units <= 0:
            continue
        ccy = str(h.get("cost_currency", "CNY"))
        cur = current_prices.get(sym)
        if cur is None:
            continue
        # 折算到 CNY: CNY 直接加；AUD 走 AUDCNY 汇率；其他币种暂不折（v2 行为）
        if ccy == "CNY":
            total_assets_cny += units * cur
        elif ccy == "AUD":
            total_assets_cny += units * cur * current_rate
        # USD/EUR 等暂不折算（已知缺口，v3 将引入 utils/fx 模块）

    portfolio_summary = _portfolio_summary(pm, total_assets_cny, current_prices)
    if data_warnings:
        portfolio_summary += "\n\n=== 数据可信度告警 ===" + "".join(data_warnings)

    has_non_cny = any(a.get("currency", "CNY") != "CNY" for a in target_assets)

    # 摩擦成本（NDQ 才有，黄金没有）
    if has_non_cny:
        friction_report = get_cost_report(
            invest_cny=user_status.disposable_for_invest,
            spot_rate=current_rate,
        )
    else:
        friction_report = "N/A (本期无需换汇)"

    # 1) Macro Strategist 跑一次（跨资产共享）
    print("🌍 Macro Strategist (1 次)...")
    macro_data_report = get_macro_data()
    macro_view = run_macro_view(macro_data_report)
    print(f"  Macro: {macro_view[:120]}")

    # 2) 对每个资产跑 committee（数据完全缺失的资产直接跳过，不让 LLM 在 0 价上瞎编）
    asset_committees: Dict[str, Dict[str, Any]] = {}
    for asset in target_assets:
        sym = asset["symbol"]
        if sym in skipped_assets:
            print(f"⏭️  Skip committee for {sym}（价格数据缺失）")
            continue
        print(f"\n⚖️ Committee for {sym}...")
        # 算 metrics + regime 一次，给 analyze_multi_timeframe（人类可读 brief）
        # 和 format_regime_brief（Quant prompt 用的 REGIME 上下文）共用
        from core.regime import format_regime_brief
        from utils.market_metrics import compute_metrics

        df_asset = get_history_data(sym, "2y")
        metrics = compute_metrics(df_asset)
        market_data = analyze_multi_timeframe(
            df_asset,
            f"{asset.get('display_name', sym)} ({sym})",
        )
        regime_brief = format_regime_brief(metrics)
        prior = _gather_relevant_insights(store, asset)
        result = run_committee(
            asset=asset,
            market_data=market_data,
            macro_view=macro_view,
            portfolio_summary=portfolio_summary,
            prior_insights=prior,
            regime_brief=regime_brief,
        )
        asset_committees[sym] = result
        v = result["verdict"]
        print(
            f"  ⚖️  {sym}: {v['verdict']} "
            f"(conf {v['confidence']:.2f}, dom {v['dominant_view']}, "
            f"alloc ¥{v['alloc_cny']})"
        )

    # 3) Gemini 第二意见（综合所有资产 verdicts）
    cio_memos_combined = "\n\n".join([
        f"### {a.get('display_name', a['symbol'])} ({a['symbol']})\n"
        f"{asset_committees[a['symbol']]['report'].cio_memo}"
        for a in target_assets
    ])
    gold_snapshot_text = format_gold_report(snap) if snap else "黄金数据获取失败"
    gemini_prompt = f"""
今日 Investment Committee 给出以下决策（每个资产 4 角色 + CIO 综合）：

# 用户上下文
{portfolio_summary}

# 宏观环境
{macro_view}

# 各资产 CIO 备忘
{cio_memos_combined}

# 黄金现货
{gold_snapshot_text}

# 摩擦成本
{friction_report}

请用搜索工具验证最新汇率/价格，对委员会的决策做独立 challenge。
**必须中文回答，控制在 300 字以内**。给一个总结性的"我同意 / 我反对"判断。
"""
    final_decision_gemini = _run_gemini_cli_review(gemini_prompt)

    # 4) 拼报告
    asset_section = "\n\n---\n\n".join([
        f"## {idx+2}. {a.get('display_name', a['symbol'])} ({a['symbol']})\n\n"
        f"**裁决**: {asset_committees[a['symbol']]['verdict']['verdict']} | "
        f"置信度 {asset_committees[a['symbol']]['verdict']['confidence']:.2f} | "
        f"主导方 {asset_committees[a['symbol']]['verdict']['dominant_view']} | "
        f"建议金额 ¥{asset_committees[a['symbol']]['verdict']['alloc_cny']}\n\n"
        f"### CIO 备忘\n```\n{asset_committees[a['symbol']]['report'].cio_memo}\n```\n\n"
        f"<details><summary>📜 三个 analyst 详细意见</summary>\n\n"
        f"**Quant**:\n{asset_committees[a['symbol']]['report'].quant_view}\n\n"
        f"**Risk Officer**:\n{asset_committees[a['symbol']]['report'].risk_view}\n\n"
        f"</details>"
        for idx, a in enumerate(target_assets)
    ])

    full_report = f"""
# 投资委员会日报 ({today})

## 1. 宏观环境 (跨资产共享)
{macro_view}

---

## 黄金现货快照
```
{gold_snapshot_text}
```

---

{asset_section}

---

## {len(target_assets)+2}. 摩擦成本 (CNY → AUD 换汇)
```
{friction_report}
```

---

## {len(target_assets)+3}. Gemini 第二意见 (独立 challenge)
{final_decision_gemini}

---

*用户当前总资产估算: ¥{total_assets_cny:,.0f}*
*Generated by Investment Committee — Quant / Macro / Risk Officer / CIO*

---

### ⚠️ 风险提示与免责声明

- 本报告由 LLM 生成，**不构成任何投资建议**。LLM 可能误读数据、过度自信、漏看
  重要信息或基于陈旧/错误数据编造结论。
- 系统**不自动下单**，所有决策需人工复核后自行执行。
- 数据样本量过小（近 60 天），任何"跑赢/跑输基准"的结论在统计意义上**不显著**，
  不代表长期表现。
- 投资有风险，过往业绩不预示未来。损失自负。
"""

    # 5) Append 给 Dreaming（被跳过的资产标 N/A）
    daily_block = f"**委员会摘要**\n\n- 宏观: {macro_view[:200]}\n\n**资产裁决**:"
    for a in target_assets:
        sym = a["symbol"]
        if sym in skipped_assets:
            daily_block += f"\n- {a.get('display_name', sym)} ({sym}): SKIPPED（数据缺失）"
            continue
        v = asset_committees[sym]["verdict"]
        daily_block += (
            f"\n- {a.get('display_name', sym)} ({sym}): "
            f"{v['verdict']} (conf {v['confidence']:.2f}, alloc ¥{v['alloc_cny']})"
        )
    store.append_daily("committee_report", daily_block, date=today)
    store.append_daily(
        "market_snapshot",
        f"```\n{macro_data_report}\n\n{gold_snapshot_text}\n\n{friction_report}\n```",
        date=today,
    )

    # 6) 发邮件
    # committee 结果已经持久化到 .committee/<date>/ 和 daily/<date>.md，邮件
    # 失败不应该让整个 job 状态变成 failed —— 但必须能在 return value 和审计日志
    # 里看到 email 失败这件事，让外部监控（看 dream_event）能告警。
    email_status: Dict[str, Any] = {"sent": False, "receiver": "", "error": None}
    try:
        receiver = send_gmail_notification(full_report)
        email_status = {
            "sent": bool(receiver),
            "receiver": receiver,
            "error": None,
            "skipped": not receiver,  # 凭据缺失等于故意 skip
        }
    except EmailDeliveryError as e:
        email_status = {"sent": False, "receiver": "", "error": str(e), "skipped": False}
        print(f"⛔ Email delivery failed (committee 已落盘，job 仍标 success): {e}")
        store.dream_event({
            "phase": "email_delivery_failed",
            "date": today,
            "error": str(e),
        })

    return {
        "status": "success" if not skipped_assets else "degraded",
        "date": today,
        "assets": [a["symbol"] for a in target_assets],
        "skipped_assets": sorted(skipped_assets),
        "data_warnings": data_warnings,
        "email": email_status,
        "verdicts": {
            sym: {
                "verdict": r["verdict"]["verdict"],
                "confidence": r["verdict"]["confidence"],
                "alloc_cny": r["verdict"]["alloc_cny"],
            }
            for sym, r in asset_committees.items()
        },
    }


if __name__ == "__main__":
    print(run())
