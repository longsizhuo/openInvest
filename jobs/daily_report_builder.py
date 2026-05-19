"""每日投资报告"构建器" - 纯函数，零 IO 副作用

ADR-005：从 daily_report.py 拆分出"给定数据 → 组装 markdown"的纯函数层，
让调度/采集与报告渲染解耦，并且让单测不需要 mock yfinance/LLM 就能跑。

职责（本文件）：
- format_staleness_warning()：给定 label + age_days，生成告警字符串
- assemble_full_report()：给定所有委员会结果 + 辅助数据，组装最终 markdown 报告
- classify_asset_freshness()：stateless 辅助函数（age_days → fresh/stale/very_stale）
- build_gemini_prompt()：给定所有投资结果 + wealth_view + event_brief，组装 Gemini 第二意见 prompt

re-export（向后兼容，2026-05-19）：
- portfolio_summary_text()：搬到 utils/portfolio_summary.py 以便 core/ service layer
  也能用（不破坏分层契约）。外部 `from jobs.daily_report_builder import
  portfolio_summary_text` 仍可用。

不在本文件里的：
- 价格拉取（get_history_data / get_gold_snapshot）
- LLM 调用（run_committee / run_macro_view）
- 邮件发送（send_gmail_notification）
- cron 触发 / 熔断判断（日期/阈值/跳过逻辑仍在 daily_report.py）
- MemoryStore / dream_event 写入（IO 副作用）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.portfolio_manager import PortfolioManager
# re-export 给 jobs/daily_report.py + scripts/skill.py:cmd_prepare_committee 用
# （保留旧 import path 的向后兼容）
from utils.portfolio_summary import portfolio_summary_text  # noqa: F401

# ============ 常量（可被 daily_report.py 的 env 读取覆盖后传入，无默认值依赖） ============

STALE_LABEL_SOFT = "stale"       # 软警告（超阈值但在熔断阈值内）
STALE_LABEL_HARD = "very_stale"  # 硬熔断
STALE_LABEL_MISSING = "missing"  # 完全没价
STALE_LABEL_FRESH = "fresh"      # 新鲜


# ============ 1. 辅助：Staleness 分类 ============

def classify_asset_freshness(
    price: Optional[float],
    age_days: Optional[int],
    stale_threshold_days: int = 3,
    hard_abort_days: int = 7,
) -> str:
    """把 (price, age_days) 映射到 freshness 标签（stateless，易于单测）

    Args:
        price: 当前价，None 表示完全没拿到
        age_days: 数据距今天数，None 表示不知道（按 fresh 处理）
        stale_threshold_days: 超过几天认为"有点旧"（软警告）
        hard_abort_days: 超过几天认为"太旧，不可信"（硬熔断）
    """
    if price is None:
        return STALE_LABEL_MISSING
    if age_days is None or age_days < stale_threshold_days:
        return STALE_LABEL_FRESH
    if age_days < hard_abort_days:
        return STALE_LABEL_SOFT
    return STALE_LABEL_HARD


# ============ 2. 辅助：告警文本 ============

def format_staleness_warning(label: str, age_days: Optional[int], stale_threshold_days: int = 3) -> str:
    """给 portfolio_summary 用的陈旧警告字符串，age_days >= 阈值才输出。

    LLM 看到这段会知道当前估值用的是 N 天前的价，不要假装是今天的市场。
    返回空字符串表示无需警告。
    """
    if age_days is None or age_days < stale_threshold_days:
        return ""
    return (
        f"\n⚠️ **{label} 价格数据陈旧 {age_days} 天** —— 今日 scraper / yfinance "
        f"未能更新行情，估值基于 {age_days} 天前的收盘价。请在结论里明确标注"
        f"\"基于陈旧数据\"，不要假设当前价仍接近此值。"
    )


# ============ 3. portfolio_summary_text 已搬至 utils/portfolio_summary.py ============
# 2026-05-19: 为支持 core/committee_runner.py service layer 调用（不破坏分层契约），
# 函数实体搬到 utils/，本文件顶部 re-export 保持向后兼容。


# ============ 4. Gemini 第二意见 prompt 组装 ============

def build_gemini_prompt(
    portfolio_summary: str,
    macro_view: str,
    cio_memos_combined: str,
    gold_snapshot_text: str,
    friction_report: str,
    wealth_view: str = "",
    event_brief: str = "",
) -> str:
    """组装 Gemini 第二意见 prompt（纯函数，零 IO）

    修复 2026-05-16 漂移：原来 Gemini prompt 是 daily_report.py 里的硬编码 f-string，
    wealth_view 和 event_brief 均未注入，导致 Gemini 做独立 challenge 时
    看不到真实流动性上下文和近期事件，等价于没有这两层信息。

    # 事件层 和 # 用户真实流动性 两个 section 仅在非空时插入，避免出现空标题。
    tests/test_committee_contract.py 有 SENTINEL 断言保护此处。

    Args:
        portfolio_summary: 用户上下文文本（现金/持仓/浮盈等）
        macro_view: Macro Strategist 宏观分析文本
        cio_memos_combined: 所有资产 CIO 备忘拼接文本
        gold_snapshot_text: 黄金现货快照文本
        friction_report: 换汇摩擦成本报告
        wealth_view: WealthContextOfficer 真实流动性视图（可空）
        event_brief: 跨资产 event RAG 召回的近期事件上下文（可空）

    Returns:
        发给 Gemini CLI 的完整 prompt 字符串
    """
    # 仅非空时插入各可选 section，避免出现空标题干扰 Gemini
    wealth_section = (
        f"\n# 用户真实流动性 (WealthContextOfficer)\n{wealth_view}\n"
        if wealth_view.strip()
        else ""
    )
    event_section = (
        f"\n# 事件层（近期 RAG 召回）\n{event_brief}\n"
        if event_brief.strip()
        else ""
    )

    return (
        "今日 Investment Committee 给出以下决策（每个资产 4 角色 + CIO 综合）：\n"
        "\n"
        "# 用户上下文\n"
        f"{portfolio_summary}\n"
        f"{wealth_section}"
        "\n"
        "# 宏观环境\n"
        f"{macro_view}\n"
        f"{event_section}"
        "\n"
        "# 各资产 CIO 备忘\n"
        f"{cio_memos_combined}\n"
        "\n"
        "# 黄金现货\n"
        f"{gold_snapshot_text}\n"
        "\n"
        "# 摩擦成本\n"
        f"{friction_report}\n"
        "\n"
        "请用搜索工具验证最新汇率/价格，对委员会的决策做独立 challenge。\n"
        "**必须中文回答，控制在 300 字以内**。给一个总结性的「我同意 / 我反对」判断。\n"
    )


# ============ 5. 核心：完整报告拼接 ============

def assemble_full_report(
    today: str,
    macro_view: str,
    gold_snapshot_text: str,
    friction_report: str,
    target_assets: List[Dict[str, Any]],
    asset_committees: Dict[str, Dict[str, Any]],
    skipped_assets: set,
    total_assets_cny: float,
    final_decision_gemini: str,
    wealth_context_view: str = "",
) -> str:
    """给定所有委员会结果 + 辅助数据，组装最终 markdown 报告

    这是纯函数：输入什么就输出什么，没有任何 IO 调用。
    测试时直接传 fixture 数据即可，不需要 mock LLM / yfinance。

    Args:
        today: 日期字符串（YYYY-MM-DD）
        macro_view: Macro Strategist 的宏观分析文本
        gold_snapshot_text: 黄金现货快照文本
        friction_report: 摩擦成本报告文本
        target_assets: strategy.target_assets 列表
        asset_committees: {symbol: {"verdict": ..., "report": ...}} 各资产委员会结果
        skipped_assets: 被跳过的资产 symbol 集合（数据缺失）
        total_assets_cny: 总资产 CNY 估算（已计算好）
        final_decision_gemini: Gemini 第二意见文本

    Returns:
        完整 markdown 报告字符串
    """
    # 各资产委员会区块
    active_assets = [a for a in target_assets if a["symbol"] not in skipped_assets
                     and a["symbol"] in asset_committees]

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
        for idx, a in enumerate(active_assets)
    ]) if active_assets else "_（所有资产数据不可用，跳过委员会）_"

    n = len(active_assets)  # 活跃资产数，用于后续章节编号

    # WealthContextOfficer 章节：仅当 wealth_view 非空时插入，避免无内容时还出现空 section
    # 防漂移：assemble_full_report 必须把 wealth_view 渲染进邮件正文，否则 Risk Officer
    # 用到的"家族真实资金/流动性"信息只进 transcript 不进用户邮箱。tests/test_committee_contract.py
    # 有 SENTINEL 断言保护此处。
    wealth_section = (
        f"\n## 1.5. 真实流动性视图 (WealthContextOfficer)\n{wealth_context_view}\n\n---\n"
        if wealth_context_view.strip()
        else ""
    )

    return f"""
# 投资委员会日报 ({today})

## 1. 宏观环境 (跨资产共享)
{macro_view}

---
{wealth_section}
## 黄金现货快照
```
{gold_snapshot_text}
```

---

{asset_section}

---

## {n+2}. 摩擦成本 (CNY → AUD 换汇)
```
{friction_report}
```

---

## {n+3}. Gemini 第二意见 (独立 challenge)
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
