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

import logging
import math
import os
import subprocess
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# 三路径统一架构（2026-05-16）：daily_report 不再直接 import core.committee 原语,
# 全部走 core.committee_runner 的 service layer (run_committee_session)
# 防漂移契约: tests/test_committee_contract.py:test_run_committee_session_*
from core.portfolio_manager import PortfolioManager
from db.market_store import MarketStore
from jobs.daily_report_builder import (
    TRANSLATOR_SYSTEM_PROMPT,
    assemble_full_report,
    build_gemini_prompt,
    build_translator_prompt,
    classify_asset_freshness,
    format_staleness_warning,
    parse_translator_output,
    portfolio_summary_text,
)
from services.notifier import EmailDeliveryError, send_gmail_notification
from utils.exchange_fee import (
    analyze_multi_timeframe,
    get_cost_report,
    get_history_data,
    get_macro_data,
)
from utils.gold_price import format_gold_report, get_gold_snapshot

load_dotenv()

# 数据陈旧阈值与硬熔断通过 load_config() 动态加载以支持运行时配置，env/默认只作 bootstrap

def get_stale_threshold_days() -> int:
    from core.config import load_config
    return load_config().staleness.price_stale_days

def get_stale_hard_abort_days() -> int:
    from core.config import load_config
    return load_config().staleness.hard_abort_stale_days

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
        log.warning("%s 数据缺失: %s", label, symbol)
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
    """告警文本委托给 daily_report_builder（见 ADR-005）"""
    return format_staleness_warning(label, age_days, get_stale_threshold_days())


# _gather_relevant_insights 已迁移到 core/committee_runner.py:load_prior_insights
# (2026-05-16 三路径统一; 三 entry 不再各自实现一份)


def _portfolio_summary(
    pm: PortfolioManager,
    total_assets_cny: float,
    current_prices: Dict[str, float],
    *,
    backup_cny: float = 0.0,
) -> str:
    """用户上下文文本，委托给 daily_report_builder（见 ADR-005）"""
    return portfolio_summary_text(pm, total_assets_cny, current_prices, backup_cny=backup_cny)


def _run_gemini_cli_review(prompt: str) -> str:
    log.info("[Gemini CLI] 正在生成第二意见...")
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
        # 委托给 builder 的 stateless 版本（同参数传入）
        return classify_asset_freshness(
            price, age_days, get_stale_threshold_days(), get_stale_hard_abort_days()
        )

    # 通用价格采集 (B3): 遍历 target_assets，按 kind 分发取价
    #   kind=etf/stock/fund/bond → yfinance close（_get_last_close）
    #   kind=metal              → 由下方 get_gold_snapshot 单独处理
    #   未识别 kind             → 也走 yfinance close（兜底）
    # 之前是写死 NDQ.AX 一个分支，fork 用户持有 510300.SS / AAPL 直接被静默跳过
    current_prices: Dict[str, float] = {}     # symbol → 当前价（per asset 币种）

    for asset_cfg in target_assets:
        sym = str(asset_cfg.get("symbol", ""))
        kind = str(asset_cfg.get("kind", ""))
        if not sym or kind == "metal":
            # 金属下面单独处理（gold_price.py 含点差反推）
            continue
        price, age = _get_last_close(sym, sym)
        asset_freshness[sym] = _classify_freshness(price, age)
        if price is None:
            log.error("%s 价格获取完全失败，跳过 committee", sym)
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

    rate_price, rate_age = _get_last_close("AUDCNY=X", "汇率")
    if rate_price is None:
        # 汇率拿不到比较罕见但仍要兜底——AUDCNY 的历史均值约 4.7 当作 sentinel
        # 避免直接抛异常让 daily_report 整体挂掉，但要明确告警 LLM
        #
        # 2026-05-19 (A4 修复)：4.7 是 **AUD 专用** 历史均值。当前 fork 用户都
        # 以 NDQ.AX(AUD) 为主要非 CNY 持仓，这条兜底只对他们生效。fork 用户主要
        # 持有 USD/EUR 等其他币种时，他们的 cost_currency 走 utils.fx.to_base 自动
        # 取汇率（USDCNY=X / EURCNY=X），跟这条 fallback 完全无关；total_assets_cny
        # 那段已经用 to_base 而不是 current_rate 折算非 AUD 持仓。
        # 不要把 4.7 推广成任意币种兜底——其他币种没汇率应直接 fail-loud 标 stale。
        log.warning("AUDCNY=X 完全失败，使用历史均值 4.7 兜底（AUD-only fallback）")
        store.dream_event({"phase": "price_fetch_failed", "symbol": "AUDCNY=X", "date": today})
        current_rate = 4.7
        data_warnings.append(
            "\n⚠️ **AUDCNY 汇率今日无法获取，使用历史均值 4.7 兜底（仅作 AUD 资产估值，"
            "其他币种走 utils.fx 自动汇率）**。汇率敏感的 AUD 估值可能偏差 ±5%，"
            "请勿据此做换汇决策。"
        )
    else:
        current_rate = rate_price
        stale_msg = _format_staleness("AUDCNY=X 汇率", rate_age)
        if stale_msg:
            data_warnings.append(stale_msg)
            store.dream_event({"phase": "price_stale", "symbol": "AUDCNY=X",
                               "age_days": rate_age, "date": today})

    # 计算总资产估算（给 Risk Officer 用）—— NDQ 跳过时不算它的市值
    user_status = pm.get_user_status(current_prices, current_rate)
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
        hard_abort_days = get_stale_hard_abort_days()
        msg = (
            f"所有 {len(rated_assets)} 个目标资产数据全废："
            + ", ".join(f"{s}={asset_freshness[s]}" for s in rated_assets)
        )
        log.error("STALE DATA HARD ABORT: %s", msg)
        store.dream_event({
            "phase": "daily_report_aborted_stale",
            "reason": "all_assets_unusable",
            "asset_freshness": dict(asset_freshness),
            "abort_threshold_days": hard_abort_days,
            "date": today,
        })
        return {
            "status": "aborted",
            "reason": "stale_data_hard_abort",
            "asset_freshness": dict(asset_freshness),
            "abort_threshold_days": hard_abort_days,
            "skipped_assets": sorted(skipped_assets),
            "date": today,
            "next_step": (
                f"所有数据源都过时超 {hard_abort_days} 天或全失败。"
                "检查 yfinance 网络 / DB 是否被定期更新。强制跑（绕过熔断）："
                "INVEST_HARD_ABORT_STALE_DAYS=999 重跑。"
            ),
        }

    # B3 通用化总资产估算：遍历 holdings 按 kind/cost_currency 算
    # gold_now 仍是 GC=F 黄金的"含点差克价"，单独传进 current_prices
    if "GC=F" not in skipped_assets and gold_now > 0:
        current_prices["GC=F"] = gold_now

    # 2026-05-19: 用 utils.fx.to_base 替代硬编码 AUD 折算。支持任意币种持仓
    # (USD/EUR/JPY/HKD 等)。之前 USD/EUR 持仓被漏算导致 total_assets_cny 偏低 →
    # Risk Officer 算集中度虚高。
    # 2026-05-19 (A3 续修): cash 也通用化遍历，之前只加 AUD，fork 用户的 USD/EUR
    # 现金被漏算。注意这里不能直接用 utils.fx.total_portfolio_value_cny，因为这一段
    # 需要按 skipped_assets 精确剔除（cron 路径的 staleness 兜底语义）。
    from utils.fx import to_base
    total_assets_cny = user_status.cash_cny
    for ccy, amt in pm.cash.items():
        if str(ccy).upper() == "CNY" or not amt:
            continue  # CNY 已计入；空余额不算
        # live valuation: as_of_date intentionally not threaded (no historical caller).
        # For backtest/historical use, thread as_of_date like utils.fx.to_base (see PR#53 fix(fx)).
        ccy_in_cny = to_base(str(ccy).upper(), float(amt), "CNY")
        if ccy_in_cny is not None and math.isfinite(ccy_in_cny):
            total_assets_cny += ccy_in_cny
        else:
            data_warnings.append(
                f"\n⚠️ **{ccy} 现金折算 CNY 失败**（汇率拉不到），"
                f"total_assets_cny 漏算这部分。"
            )
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
        # NaN 价（当日 close=NULL 被读成 float('nan')）等同缺价：排除并告警，
        # 绝不 total_assets_cny += NaN（否则总资产 NaN 污染整个集中度风控）。
        if cur is None or not math.isfinite(cur):
            data_warnings.append(
                f"\n⚠️ **{sym} 实时价不可用**（NaN/缺），已从总资产排除，"
                f"集中度会偏高，请勿据此做大额操作。"
            )
            continue
        local_value = units * cur
        # live valuation: as_of_date intentionally not threaded (no historical caller).
        # For backtest/historical use, thread as_of_date like utils.fx.to_base (see PR#53 fix(fx)).
        value_cny = to_base(ccy, local_value, "CNY")
        if value_cny is None or not math.isfinite(value_cny):
            data_warnings.append(
                f"\n⚠️ **{sym} 持仓折算 CNY 失败**（{ccy}→CNY 汇率拉不到），"
                f"total_assets_cny 漏算这部分，集中度会偏高，请勿做大额操作。"
            )
            continue
        total_assets_cny += value_cny

    # backup_cny（off-portfolio 兜底）走单一可信源 loader（service layer re-export）
    from core.committee_runner import load_backup_cny
    _backup_cny = load_backup_cny(pm)
    portfolio_summary = _portfolio_summary(pm, total_assets_cny, current_prices, backup_cny=_backup_cny)
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

    # 三路径统一架构：所有跨资产 prep (macro/wealth/event_brief multi 召回/regime/
    # market_data/prior_insights) 全部委托给 run_committee_session 一处实现，跟
    # Skill/Web 对齐。本函数只负责 cron-specific 后置: staleness 熔断、邮件渲染、
    # Gemini 第二意见、Dreaming append_daily、邮件发送。
    # 修复 2026-05-16 漂移: 此前 daily_report 自己手搓 multi-asset loop, 跟 Web 重复
    # 实现; wealth_view / event_brief 漂移都在这条路径上出过事故
    target_symbols_to_run = [
        a["symbol"] for a in target_assets if a["symbol"] not in skipped_assets
    ]

    asset_committees: Dict[str, Dict[str, Any]] = {}
    macro_view = ""
    wealth_view = ""
    event_brief = ""

    if not target_symbols_to_run:
        log.error("所有 target_assets 都因 staleness 被 skip; session 不跑")
    else:
        from core.committee_runner import run_committee_session

        log.info("run_committee_session 跑 %d 资产...", len(target_symbols_to_run))
        session = run_committee_session(
            symbols=target_symbols_to_run,
            max_debate_rounds=4,  # 三路径统一 4 轮 (cron 成本 ~¥2.5/月, 用户已确认)
            portfolio_summary_override=portfolio_summary,  # 含 total_assets_cny + data_warnings
        )
        asset_committees = session["asset_committees"]
        macro_view = session["macro_view"]
        wealth_view = session["wealth_view"]
        event_brief = session["event_brief"]

        # 单资产 session 失败 → 合并到 skipped_assets, 防 cio_memos_combined /
        # assemble_full_report 拿不到 report.cio_memo 抛 AttributeError
        for sym, err in session.get("errors", {}).items():
            log.warning("%s 在 session 内失败: %s; 加入 skipped_assets", sym, err)
            skipped_assets.add(sym)
            store.dream_event({"phase": "committee_failed", "symbol": sym,
                               "error": err, "date": today})

        for sym, result in asset_committees.items():
            if sym in skipped_assets:
                continue
            v = result["verdict"]
            log.info(
                "  %s: %s (conf %.2f, dom %s, alloc ¥%s)",
                sym, v["verdict"], v["confidence"], v["dominant_view"], v["alloc_cny"],
            )

    # 3) Gemini 第二意见（综合所有资产 verdicts）
    cio_memos_combined = "\n\n".join([
        f"### {a.get('display_name', a['symbol'])} ({a['symbol']})\n"
        f"{asset_committees[a['symbol']]['report'].cio_memo}"
        for a in target_assets
        if a["symbol"] not in skipped_assets and a["symbol"] in asset_committees
    ])
    gold_snapshot_text = format_gold_report(snap) if snap else "黄金数据获取失败"
    # 2026-05-19 修复：market_snapshot 持久化段（line 455）引用 macro_data_report
    # 但之前从未赋值，NameError。补一行从 get_macro_data() 拉，给 daily/<date>.md
    # 的 market_snapshot section 用（跟 cron 邮件的 macro view 是同源数据）。
    macro_data_report = get_macro_data()
    # 修复 2026-05-16 漂移：Gemini prompt 委托给 builder 纯函数，
    # wealth_view 和 event_brief 注入 Gemini，让独立 challenge 看到完整上下文
    gemini_prompt = build_gemini_prompt(
        portfolio_summary=portfolio_summary,
        macro_view=macro_view,
        cio_memos_combined=cio_memos_combined,
        gold_snapshot_text=gold_snapshot_text,
        friction_report=friction_report,
        wealth_view=wealth_view,
        event_brief=event_brief,
    )
    final_decision_gemini = _run_gemini_cli_review(gemini_prompt)

    # 3.5) 翻译官：人话解读（一次 LLM 调用；任何失败 graceful 回落
    # builder 内的确定性一句话，邮件照发）
    plain_summaries: Dict[str, str] = {}
    try:
        t_inputs = []
        for a in target_assets:
            sym = a["symbol"]
            if sym in skipped_assets or sym not in asset_committees:
                continue
            r = asset_committees[sym]
            v = r["verdict"]
            defense_note = ""
            if v.get("_original_verdict") and v["_original_verdict"] != v["verdict"]:
                _dca = v.get("_defense_dca")
                if _dca == "tranche":
                    reason = (f"高 VIX/ATR 防御期，黄金买入改强制分批 DCA，本次只放行"
                              f"约 1/3（第 {v.get('_defense_dca_tranche_idx', 1)} 批）")
                elif _dca and str(_dca).startswith("blocked"):
                    reason = "高 VIX/ATR 防御期，黄金买入分批，本批未满间隔/配额暂缓（等下一批）"
                else:
                    reason = (v.get("_defense_downgrade") and "VIX/ATR 快崩哨兵拦截买入") or \
                             (v.get("_original_trim_reason") == "concentration"
                              and v.get("_concentration_lens") == "disabled"
                              and "集中度 lens 已按 config 关闭，不因超配减仓") or \
                             (v.get("_sanity5_reason") and "TRIM 没给合格买回点被否") or "防御规则"
                defense_note = (f"CIO 原始结论 {v['_original_verdict']}"
                                f"（建议金额 ¥{v.get('_original_alloc', v['alloc_cny'])}），"
                                f"{reason}，最终改为 {v['verdict']}")
            path_lines = [ln for ln in (r.get("path_reference") or "").splitlines()
                          if ln.startswith("- ")]
            t_inputs.append({
                "symbol": sym,
                "display_name": a.get("display_name", sym),
                "verdict_line": (f"{v['verdict']}，置信度 {v['confidence']:.2f}，"
                                 f"建议金额 ¥{v['alloc_cny']}"),
                "defense_note": defense_note,
                "path_lines": path_lines,
                "cio_memo": r["report"].cio_memo,
            })
        if t_inputs:
            from capabilities.sdk_agent import SDKAgent
            translator = SDKAgent(
                system_prompt=TRANSLATOR_SYSTEM_PROMPT,
                enable_tools=False, temperature=0.3,
            )
            raw = translator.run(build_translator_prompt(t_inputs))
            plain_summaries = parse_translator_output(raw)
            log.info("翻译官人话解读: %d/%d 资产", len(plain_summaries), len(t_inputs))
    except Exception as e:  # noqa: BLE001  翻译官失败不阻断邮件
        log.warning("翻译官失败 graceful 回落确定性一句话: %s", e)

    # 4) 拼报告（委托给 builder 的纯函数）
    # 纪律台账(只读聚合,零 LLM;失败不阻断报告)
    try:
        from services.discipline import render_discipline_md
        discipline_md = render_discipline_md()
    except Exception as e:  # noqa: BLE001
        log.warning(f"纪律台账渲染失败 graceful: {e}")
        discipline_md = ""

    full_report = assemble_full_report(
        today=today,
        macro_view=macro_view,
        gold_snapshot_text=gold_snapshot_text,
        friction_report=friction_report,
        target_assets=target_assets,
        asset_committees=asset_committees,
        skipped_assets=skipped_assets,
        total_assets_cny=total_assets_cny,
        final_decision_gemini=final_decision_gemini,
        wealth_context_view=wealth_view,
        plain_summaries=plain_summaries,
        discipline_md=discipline_md,
    )

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
        log.error("Email delivery failed (committee 已落盘，job 仍标 success): %s", e)
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


def _report_exit_code(result: Dict[str, Any]) -> int:
    """退出码契约：邮件真发出 → 0；其余(发送失败/无收件人/no_target_assets 早返回)→ 1。
    自托管(GitHub Actions)唯一反馈通道是邮件 —— 没发出就必须红勾，否则委员会跑了、token
    烧了、收件箱空、用户零感知(配错 Gmail 应用密码会每天静默烧)。"""
    return 0 if (result.get("email") or {}).get("sent") is True else 1


if __name__ == "__main__":
    # 仅影响 `python -m jobs.daily_report`(CLI/Actions)；APScheduler cron 走 run() 不受影响。
    import json as _json
    import sys as _sys
    _r = run()
    print(_json.dumps(_r, ensure_ascii=False, default=str))
    _sys.exit(_report_exit_code(_r))
