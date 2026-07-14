"""每日投资报告"构建器" - 纯函数，零 IO 副作用

ADR-005：从 daily_report.py 拆分出"给定数据 → 组装 markdown"的纯函数层，
让调度/采集与报告渲染解耦，并且让单测不需要 mock yfinance/LLM 就能跑。

职责（本文件）：
- format_staleness_warning()：给定 label + age_days，生成告警字符串
- assemble_full_report()：给定所有委员会结果 + 辅助数据，组装最终 markdown 报告
- classify_asset_freshness()：stateless 辅助函数（age_days → fresh/stale/very_stale）
- build_gemini_prompt()：给定所有投资结果 + event_brief，组装 Gemini 第二意见 prompt

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

# re-export 给 jobs/daily_report.py + scripts/skill.py:cmd_prepare_committee 用
# （保留旧 import path 的向后兼容）
from openinvest.utils.portfolio_summary import portfolio_summary_text  # noqa: F401

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
    event_brief: str = "",
) -> str:
    """组装 Gemini 第二意见 prompt（纯函数，零 IO）

    修复 2026-05-16 漂移：原来 Gemini prompt 是 daily_report.py 里的硬编码 f-string，
    event_brief 未注入，导致 Gemini 做独立 challenge 时看不到近期事件。

    # 事件层 section 仅在非空时插入，避免出现空标题。
    tests/test_committee_contract.py 有 SENTINEL 断言保护此处。

    Args:
        portfolio_summary: 用户上下文文本（现金/持仓/浮盈等）
        macro_view: Macro Strategist 宏观分析文本
        cio_memos_combined: 所有资产 CIO 备忘拼接文本
        gold_snapshot_text: 黄金现货快照文本
        friction_report: 换汇摩擦成本报告
        event_brief: 跨资产 event RAG 召回的近期事件上下文（可空）

    Returns:
        发给 Gemini CLI 的完整 prompt 字符串
    """
    # 仅非空时插入 event section，避免出现空标题干扰 Gemini
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


# ============ 5. 人话摘要（确定性，零 LLM） ============

_VERDICT_PLAIN = {
    "HOLD": "继续持有，不买也不卖",
    "ACCUMULATE": "建议小额加仓",
    "BUY": "建议买入",
    "TRIM": "建议减掉一部分仓位",
    "SELL": "建议清仓",
}


# chat 变体专用：裁决 emoji 徽章。BUY/ACCUMULATE 都是"买"但分两档深浅绿，
# 5 级配色比纯文字在 Discord 上更快扫完（2026-07-14，daily-invest-report
# 渲染优化——聊天平台没有大号标题，靠 emoji 做视觉锚点）。
_VERDICT_EMOJI = {
    "BUY": "🟢",
    "ACCUMULATE": "🟩",
    "HOLD": "🟡",
    "TRIM": "🟠",
    "SELL": "🔴",
}


def build_tldr_block(
    active_assets: List[Dict[str, Any]],
    asset_committees: Dict[str, Dict[str, Any]],
) -> str:
    """chat 变体专用：逐资产一行摘要（emoji 裁决徽章 + 置信度 + 建议金额），
    整块置顶在报告最前面。

    动机：Hermes/OpenClaw cron 把完整报告转发到 Discord，Discord 单条消息
    2000 字符上限会把多资产报告切成 ~5 条消息（分页标记 "(1/5)" 等），结论
    埋在中间某条里。这个摘要块保证第一条消息就有全部裁决，不用翻到最后。

    纯格式化，复用调用方已算好的 verdict dict，零新计算 / 零 LLM。
    """
    if not active_assets:
        return ""
    lines = ["**📊 今日速览**\n"]
    for a in active_assets:
        sym = a["symbol"]
        v = asset_committees[sym]["verdict"]
        emoji = _VERDICT_EMOJI.get(v["verdict"], "⚪")
        lines.append(
            f"{emoji} **{a.get('display_name', sym)}** ({sym}): "
            f"{v['verdict']} · 置信度 {v['confidence']:.0%} · "
            f"建议 ¥{v['alloc_cny']:,.0f}"
        )
    return "\n".join(lines) + "\n"


def plain_verdict_summary(verdict: Dict[str, Any],
                          path_profile: Optional[Dict[str, Any]]) -> str:
    """给小白看的一句话结论——从 verdict + 路径分布确定性生成，零 LLM 调用。

    专家细节（CIO 备忘/analyst 原文）保留在后面的段落，这行只翻译"该干嘛 +
    历史上类似行情接下来大概怎么走"。
    """
    vd = str(verdict.get("verdict", ""))
    act = _VERDICT_PLAIN.get(vd, vd)
    alloc = verdict.get("alloc_cny", 0) or 0
    if vd in ("ACCUMULATE", "BUY") and alloc:
        act += f" ¥{alloc:,.0f}"
    st = ((path_profile or {}).get("windows") or {}).get("30d")
    tail = ""
    if st and all(k in st for k in ("p_below", "downside_pct", "median_pct")):
        tail = (
            f"。历史上类似行情，30 天后比今天更便宜的概率约 "
            f"{st['p_below'] * 100:.0f}%；运气差的情形（最差 1/5）大约 "
            f"{st['downside_pct']:+.1f}%，一般情形 {st['median_pct']:+.1f}%"
        )
    return f"**一句话**: {act}{tail}。\n\n"


# ============ 5b. 翻译官（人话解读，LLM 版；上面的确定性版是 graceful 回落） ============

TRANSLATOR_SYSTEM_PROMPT = """你是 AI 投资委员会的"翻译官"，负责把委员会结论翻译给完全不懂投资的用户。

铁律：
1. **只许引用输入里出现的数字/价格/百分比，禁止自己计算、换算或编造任何数字**
2. 每个资产输出一段 4-8 句的大白话，要做到三件事：
   a. 先说该干嘛（买/卖/不动，多少钱）
   b. 交代关键概率数字的出身和含义——用"历史上 N 个类似的日子里…""5 次里有 4 次…"
      这种口吻；"悲观情形"要说明它不是最坏情况，是最差的 1/5 的分界线
   c. 如果输入里有"CIO 原始结论被防御规则改掉"的留痕，必须解释系统真正在担心什么；
      如果数字之间看似矛盾（比如概率偏多但结论偏空），必须调和解释，不许装看不见
3. 不堆术语；必须用术语时随手用半句话解释
4. 数据里标了"⚠样本不足"的，要提醒用户该数字只能看方向、别当真到个位数

输出格式（严格遵守，不要输出任何其他内容）：
每个资产先输出单独一行 `@@<symbol>`，从下一行开始是这个资产的解读正文。"""


def build_translator_prompt(asset_inputs: List[Dict[str, Any]]) -> str:
    """翻译官 user prompt（纯函数，零 IO）。

    asset_inputs 每项: {symbol, display_name, verdict_line, defense_note,
    path_lines: List[str], cio_memo}
    """
    blocks: List[str] = []
    for x in asset_inputs:
        defense = f"\n防御规则留痕: {x['defense_note']}" if x.get("defense_note") else ""
        path = "\n".join(x.get("path_lines") or []) or "（路径分布不可用）"
        blocks.append(
            f"## {x['symbol']}（{x.get('display_name', x['symbol'])}）\n"
            f"最终裁决: {x['verdict_line']}{defense}\n"
            f"历史路径分布（与 CIO 看到的同一份）:\n{path}\n"
            f"CIO 备忘原文:\n{x.get('cio_memo', '')}"
        )
    return (
        "把下面每个资产的委员会结论翻译成大白话（输出格式见 system 指令）：\n\n"
        + "\n\n---\n\n".join(blocks)
    )


def parse_translator_output(text: str) -> Dict[str, str]:
    """解析翻译官输出：@@SYM 行分段 → {symbol: 正文}。格式不符返回 {}（回落确定性版）。"""
    out: Dict[str, str] = {}
    sym: Optional[str] = None
    buf: List[str] = []
    for ln in (text or "").splitlines():
        if ln.strip().startswith("@@"):
            if sym and "".join(buf).strip():
                out[sym] = "\n".join(buf).strip()
            sym = ln.strip().lstrip("@").strip()
            buf = []
        elif sym is not None:
            buf.append(ln)
    if sym and "".join(buf).strip():
        out[sym] = "\n".join(buf).strip()
    return out


# 术语表（静态，放邮件末尾——小白查表，专家直接跳过）
_GLOSSARY = """## 术语表（看不懂的词在这里查）

- **裁决**: 委员会结论。HOLD=持有不动；ACCUMULATE=小额加仓；BUY=买入；TRIM=减一部分；SELL=清仓
- **置信度**: 委员会对这个结论的把握（0-1）。0.5 上下=分歧大，0.8+=很有把握
- **主导方**: 这次结论主要听谁的——macro=宏观面 / quant=技术面 / risk=风控
- **regime**: 系统对当前行情的粗分类（uptrend 上行 / downtrend 下行 / crash 急跌 / recovery 修复 / range 震荡）
- **n 与"重叠窗口独立≈k"**: 历史样本量。相邻交易日的窗口高度重叠，真实独立样本只有 ≈k 个；带 ⚠样本不足 的行别太当真
- **跌破现价概率**: 历史上同类行情里，N 天后价格低于今天的占比
- **悲观情形(20分位)**: 历史同类行情里最差的 1/5 大约跌到哪（不是最坏情况）
- **路径形状**: 未来 90 天怎么走的历史占比——先跌后涨 / 直接涨 / 冲高回落 / 一路收跌
- **INDEP_DEFENSE_FLAG**: 快崩哨兵。VIX（恐慌指数）或波动率异常飙升时=on，系统会自动拒绝加仓建议
- **集中度**: 单一资产占总资产比例。过高=鸡蛋在一个篮子
- **子弹 / 可投**: 当前现金里可以用来加仓的部分
- **报价币种**: 行情价用的是交易所报价币种（GC=F 是美元/盎司，与积存金的 ¥/克不同；换算看"黄金现货快照"节）
"""


# ============ 6. 核心：完整报告拼接 ============

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
    plain_summaries: Optional[Dict[str, str]] = None,
    discipline_md: str = "",
    *,
    render_target: str = "email",
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
        plain_summaries: {symbol: 翻译官人话解读}（LLM 版，entry 层生成；
            缺失/失败时该资产回落确定性 plain_verdict_summary 一句话）
        render_target: "email"（默认，分析师长文走 `<div class="analyst"
            markdown="1">` 卡片——notifier 用 md_in_html 扩展转成带样式的 HTML）
            | "chat"（宿主 agent cron 投递 Discord/Weixin/QQ 等聊天平台用；
            这些平台不解析原生 HTML，"email" 变体的 `<div>` 会以字面文本泄露——
            见 2026-07-14 Hermes cron 渲染事故）：同样内容不裹 HTML，纯 markdown。

    Returns:
        完整 markdown 报告字符串
    """
    # 各资产委员会区块
    active_assets = [a for a in target_assets if a["symbol"] not in skipped_assets
                     and a["symbol"] in asset_committees]

    def _asset_block(idx: int, a: dict) -> str:
        sym = a["symbol"]
        c = asset_committees[sym]
        v = c["verdict"]
        # 小白友好：人话解读。优先翻译官（LLM，能调和矛盾/解释防御拦截），
        # 该资产缺失/失败 → 回落确定性一句话（零 LLM）
        translated = (plain_summaries or {}).get(sym, "").strip()
        plain_block = (
            f"**人话解读**: {translated}\n\n" if translated
            else plain_verdict_summary(v, c.get("path_profile"))
        )
        lines = [
            f"## {idx+2}. {a.get('display_name', sym)} ({sym})\n\n",
            f"**裁决**: {v['verdict']} | 置信度 {v['confidence']:.2f} | "
            f"主导方 {v['dominant_view']} | 建议金额 ¥{v['alloc_cny']}\n\n",
            plain_block,
        ]
        # TRIM 路径化：展示买回点 + 预期路径（让用户能判断"卖出是赚是亏"）
        if v.get("verdict") == "TRIM":
            rp = v.get("reentry_price")
            rp_txt = f"¥{rp:,.2f}" if rp is not None else "未给出"
            cond = v.get("reentry_condition") or "未给出"
            path = v.get("expected_path") or "未给出"
            lines.append(
                f"**减仓路径**: 预期路径 {path} → 买回点 {rp_txt}"
                f"（触发条件：{cond}）| 不达买回点则继续持有\n\n"
            )
        # 被 Sanity check 5 从 TRIM 降级成 HOLD 的，留痕给用户看为什么没减
        elif v.get("_original_verdict") == "TRIM" and v.get("_sanity5_reason"):
            why = ("没给买回点" if v["_sanity5_reason"] == "reentry_missing"
                   else "买回点不低于现价（卖了高价接回 = 纯亏）")
            lines.append(f"**减仓被否**: CIO 想 TRIM，但{why} → 系统降级 HOLD\n\n")
        # 被 Sanity check 4（集中度 lens 已关）从 TRIM(concentration) 拦成 HOLD 的：
        # 你已声明本池为单资产 / 刻意集中 / 全可投资金，集中度不作为减仓理由。措辞明确
        # 留痕"CIO 想减仓 → 因 lens 关被拦"，不再用旧的"兜底充足/集中度不构成真实风险"
        # （那是已移除的 solvency 自动兜底口径）。CIO 给的卖出后路径预期仍摆出供对照。
        elif (v.get("_original_verdict") == "TRIM"
              and v.get("_original_trim_reason") == "concentration"
              and v.get("_concentration_lens") == "disabled"):
            _path = v.get("expected_path")
            _orig_alloc = v.get("_original_alloc", v.get("alloc_cny", 0))
            lines.append(
                f"**减仓未执行（集中度 lens 已关）**: CIO 想因集中度减仓（建议金额 "
                f"¥{_orig_alloc}），但你已关闭集中度 lens（本池视为刻意集中 / 全可投资金）"
                "→ 系统维持 HOLD。"
                + (f"系统对未来路径的预期仍供对照：{_path}"
                   "（系统预期偏空，你若有不同判断自行权衡）" if _path else "")
                + "\n\n"
            )
        # 买侧被快崩防御改写（VIX/ATR 哨兵），含黄金分批 DCA。翻译官路径已用
        # defense_note 渲染；这里是无翻译官时的确定性兜底，保证决策维度不丢
        #（守"邮件必须渲染决策维度"红线）。
        elif v.get("_original_verdict") in ("BUY", "ACCUMULATE") and (
            v.get("_defense_dca") or v.get("_defense_downgrade")
        ):
            _oa = v.get("_original_alloc", v.get("alloc_cny", 0)) or 0
            _fa = v.get("alloc_cny", 0) or 0
            if v.get("_defense_dca") == "tranche":
                lines.append(
                    f"**防御分批**: CIO 想 {v['_original_verdict']}（¥{_oa:,.0f}），高 VIX/ATR "
                    f"防御期黄金买入改强制分批 DCA，本次只放行约 1/3（第 "
                    f"{v.get('_defense_dca_tranche_idx', 1)} 批）→ {v['verdict']} ¥{_fa:,.0f}\n\n"
                )
            elif str(v.get("_defense_dca") or "").startswith("blocked"):
                lines.append(
                    f"**防御分批**: CIO 想 {v['_original_verdict']}（¥{_oa:,.0f}），高 VIX/ATR "
                    "防御期黄金买入分批、本批未满间隔/配额 → 暂缓持有（等下一批）\n\n"
                )
            else:
                lines.append(
                    f"**防御降级**: CIO 想 {v['_original_verdict']}（¥{_oa:,.0f}），VIX/ATR "
                    f"快崩哨兵触发 → 降级 {v['verdict']} ¥{_fa:,.0f}\n\n"
                )

        # 概率分布（regime 概率表）
        prob = c.get("regime_probability")
        if prob is not None:
            lines.append(f"**历史参考**: {prob.summary_line()}\n\n")
        # 路径概率（多窗分布 + 形状 + 时序）——与 CIO 决策时看到的同一份文本。
        # 只取 "- " 数据行：掐头（给 LLM 的标题行）去尾（TRIM 指令行），用户只看数据。
        path_ref = c.get("path_reference") or ""
        path_lines = [ln for ln in path_ref.splitlines() if ln.startswith("- ")]
        if path_lines:
            lines.append(
                "**路径概率**（该 regime 历史 forward 分布，CIO 决策依据）:\n\n"
                + "\n".join(path_lines) + "\n\n"
            )
        # 分析师长文走 .analyst 卡片（md_in_html）而非 ``` 代码块——**仅 email**。
        # 历史（2026-06-12）：曾用 <details> 折叠，但 python-markdown 默认不解析
        # 原生 HTML 块内部 markdown → **粗体**/换行全坏，于是改塞 ``` 代码块；副作用是
        # LLM 原文（含 ** 标记）以灰色等宽块原样泄露、极难阅读。
        # 2026-06-20：notifier 开 md_in_html 扩展 + `markdown="1"` 容器，
        # 卡片内 markdown 正常解析（粗体/换行/列表都对），版式可读且不泄露原文。
        # 2026-07-14：这套 HTML 卡片只对"markdown → HTML 邮件"管线有意义——
        # Hermes/OpenClaw cron 把同一份 full_report 原样转发给 Discord/Weixin/QQ，
        # 这些平台不解析原生 HTML，`<div class="analyst" markdown="1">` 以字面
        # 文本泄露给用户（见 daily-invest-report 2026-07-14 实况）。chat 变体
        # 去掉 HTML 包裹，内容不变——纯 markdown 到哪个平台都至少能读。
        if render_target == "chat":
            lines.extend([
                "### CIO 备忘\n\n",
                f'{c["report"].cio_memo}\n\n',
                "### 分析师意见（专家区）\n\n",
                "**Quant（技术面）**\n\n",
                f'{c["report"].quant_view}\n\n',
                "**Risk Officer（风控）**\n\n",
                f'{c["report"].risk_view}\n',
            ])
        else:
            lines.extend([
                "### CIO 备忘\n\n",
                f'<div class="analyst" markdown="1">\n\n{c["report"].cio_memo}\n\n</div>\n\n',
                "### 分析师意见（专家区）\n\n",
                "**Quant（技术面）**\n\n",
                f'<div class="analyst" markdown="1">\n\n{c["report"].quant_view}\n\n</div>\n\n',
                "**Risk Officer（风控）**\n\n",
                f'<div class="analyst" markdown="1">\n\n{c["report"].risk_view}\n\n</div>\n',
            ])
        return "".join(lines)

    asset_section = "\n\n---\n\n".join([
        _asset_block(idx, a) for idx, a in enumerate(active_assets)
    ]) if active_assets else "_（所有资产数据不可用，跳过委员会）_"

    n = len(active_assets)  # 活跃资产数，用于后续章节编号

    # 纪律台账(ADR-023:委员会可证价值=不作为+拦冲动,非 alpha)。entry 层算好传入,
    # 本纯函数只渲染。空则不出 section(无数据时不留空壳)。
    discipline_section = (
        f"\n{discipline_md}\n\n---\n" if discipline_md.strip() else ""
    )

    # chat 变体置顶速览（见 build_tldr_block 动机）；email 不加——邮件本来就
    # 一次性看全文，标题+目录结构已经够用，不需要重复摘要。
    tldr_section = (
        f"\n{build_tldr_block(active_assets, asset_committees)}\n---\n"
        if render_target == "chat" else ""
    )

    return f"""
# 投资委员会日报 ({today})
{tldr_section}
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
{discipline_section}
## {n+2}. 摩擦成本 (CNY → AUD 换汇)
```
{friction_report}
```

---

## {n+3}. Gemini 第二意见 (独立 challenge)
{final_decision_gemini}

---

{_GLOSSARY}
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
