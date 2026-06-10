"""Contract tests — 防 cross-entry 漂移

历史教训：2026-05-15 wealth_context_view 漂移事故 — run_committee 接受了参数，
Risk Officer prompt 也读了，但 daily_report / scripts.skill 没人在调用链上算
view 传进去 → user.md 的 wealth_context 三个月没进过 production 委员会。

本文件守住**真实行为契约**，不是字符模式：
1. graceful loader 必须在异常时返回空字符串
2. entry 调用 run_committee 时必须**真的把 loader 结果**传给它

第 2 条用 sentinel 字符串通过 monkeypatch 锚定 —— 如果有人偷懒写
`wealth_context_view=""` 硬编码 / 漏调 loader，sentinel 不会出现在 captured
kwargs 里，test 立刻红。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================================
# 契约 1: load_wealth_context_view graceful 退化空字符串
# ============================================================================
# 不是"为通过而写"——这是 CLAUDE.md 分层契约段明确写的"shared loader 必须
# graceful 退化"。memory 文件缺/损坏时不能让 committee 整个挂掉。

def test_load_wealth_context_view_graceful_on_missing_memory(monkeypatch, tmp_path):
    """memory 文件缺失时 load_wealth_context_view 返回 ""，不抛异常"""
    from core import memory_store as ms

    empty_dir = tmp_path / "empty_memory"
    empty_dir.mkdir()
    monkeypatch.setattr(ms, "MEMORY_ROOT", empty_dir)

    from core.committee import load_wealth_context_view
    result = load_wealth_context_view()
    assert result == "", (
        "load_wealth_context_view 在 memory 缺失时应 graceful 退化空字符串，"
        f"实际返回: {result!r}"
    )


# ============================================================================
# 契约 2: daily_report 真把 loader 结果传给 run_committee
# ============================================================================
# 这是**真行为测试**，不是 AST 扫字符模式：
# - 用 SENTINEL 字符串通过 monkeypatch 锚定 load_wealth_context_view 输出
# - 抓 run_committee 收到的 kwargs
# - 验证 captured wealth_context_view == SENTINEL
#
# 漂移会被抓:
# - "硬编码空" (wealth_context_view=""): captured != SENTINEL → 红
# - "漏调 loader" (没传这个 kwarg / 用其他变量): captured != SENTINEL → 红
# - "用错的字段名" (传成 wealth_view=...): kwarg 不存在 → 红

def _seed_minimal_memory(memory_dir: Path, with_wealth_context: bool = False) -> None:
    """生成 daily_report.run() 能跑起来的最小 memory 结构"""
    wealth_block = ""
    if with_wealth_context:
        wealth_block = (
            "wealth_context:\n"
            "  emergency_buffer_cny: 4000000\n"
            "  family_backup_available: true\n"
            "  account_purpose: 零花钱账户\n"
        )
    (memory_dir / "user.md").write_text(
        f"""---
name: user
type: profile
schema_version: 1
display_name: Test
risk_tolerance: Balanced
exchange_buffer_cny: 0
{wealth_block}---
""",
    )
    (memory_dir / "strategy.md").write_text(
        """---
name: strategy
type: strategy
schema_version: 1
target_assets:
  - symbol: TEST.AX
    display_name: Test Asset
    channel: direct
    max_single_invest_cny: 10000
updated: '2024-05-15T00:00:00+00:00'
---
""",
    )
    (memory_dir / "portfolio.md").write_text(
        """---
schema_version: 2
cash:
  CNY: 100000.0
holdings: []
name: portfolio
type: state
updated: '2024-05-15T00:00:00+00:00'
---
""",
    )
    (memory_dir / "MEMORY.md").write_text("# Test\n")
    (memory_dir / "portfolio_history.jsonl").write_text("")
    state_dir = memory_dir / ".state"
    state_dir.mkdir()
    (state_dir / "processed_emails.json").write_text("[]")


# 已删: test_daily_report_passes_loaded_wealth_view_to_run_committee
# 历史: 守 daily_report 直接调 run_committee 时传 wealth_view 的契约
# 2026-05-16 三路径统一架构后, daily_report 改走 run_committee_session,
# 不再直调 run_committee. 该契约由 test_run_committee_session_passes_all_shared_inputs
# 统一守护. 原代码可经 `git log -p tests/test_committee_contract.py` 查阅.


# 已删: test_skill_cmd_run_committee_passes_loaded_wealth_view
# 历史: 守 skill.cmd_run_committee 直接调 run_committee 传 wealth_view 的契约
# 2026-05-16 三路径统一架构后, skill 改走 run_committee_session 不再直调
# run_committee, 该契约由 test_run_committee_session_passes_all_shared_inputs
# 统一守护. 原代码可经 `git log -p tests/test_committee_contract.py` 查阅.


# ============================================================================
# 契约 3: 邮件 render 路径 — assemble_full_report 必须把 wealth_view 渲染进正文
# ============================================================================
# 历史教训（2026-05-15 第二次漂移事故）：load_wealth_context_view 修好了，
# daily_report.run() 也把它传给了 run_committee（Risk Officer 用得上）—— 但
# assemble_full_report 邮件模板里**没有 wealth section**，导致 LLM 用了视图却
# 看不到结果进用户邮箱。本测验证 SENTINEL 字符串确实出现在最终邮件 markdown。

def test_assemble_full_report_renders_wealth_section_when_view_nonempty():
    """非空 wealth_context_view 必须出现在邮件 markdown 正文（而不是只进 transcript）"""
    from jobs.daily_report_builder import assemble_full_report

    SENTINEL = "WEALTH_SECTION_SENTINEL_abc123"

    md = assemble_full_report(
        today="2026-05-16",
        macro_view="mock macro view",
        gold_snapshot_text="mock gold snapshot",
        friction_report="mock friction",
        target_assets=[],
        asset_committees={},
        skipped_assets=set(),
        total_assets_cny=100000.0,
        final_decision_gemini="mock gemini",
        wealth_context_view=SENTINEL,
    )

    assert SENTINEL in md, (
        "❌ wealth_context_view 没渲染进邮件正文！\n"
        "   assemble_full_report 收了 wealth_context_view 参数但邮件模板里没插 section，\n"
        "   导致 WealthContextOfficer 视图只进 transcript / Risk Officer prompt，\n"
        "   用户邮件看不到。检查 jobs/daily_report_builder.py 的 wealth_section 插入逻辑。"
    )


def test_assemble_full_report_omits_wealth_section_when_view_empty():
    """空字符串（fork 用户没填 wealth_context）→ 不应出现空 section 标题"""
    from jobs.daily_report_builder import assemble_full_report

    md = assemble_full_report(
        today="2026-05-16",
        macro_view="mock macro view",
        gold_snapshot_text="mock gold snapshot",
        friction_report="mock friction",
        target_assets=[],
        asset_committees={},
        skipped_assets=set(),
        total_assets_cny=100000.0,
        final_decision_gemini="mock gemini",
        wealth_context_view="",
    )

    assert "WealthContextOfficer" not in md, (
        "空 wealth_context_view 不应渲染 section 标题（避免空 section 干扰用户）"
    )


# ============================================================================
# 契约 4: Gemini prompt 必须包含 wealth_view 和 event_brief
# ============================================================================
# 2026-05-16 漂移事故：Gemini prompt 是 daily_report.py 里的硬编码 f-string，
# wealth_view 和 event_brief 均未注入，导致 Gemini 独立 challenge 时看不到
# 真实流动性上下文和近期事件层，等价于这两层信息对 Gemini 不可见。
#
# 修复方案：把 prompt 组装抽到 build_gemini_prompt() 纯函数（daily_report_builder.py），
# 接受 wealth_view 和 event_brief 参数，在 prompt 里加对应 section（非空时插入）。
#
# 4 个 SENTINEL 测试守卫：
# a. test_gemini_prompt_includes_wealth_view — wealth_view SENTINEL 出现在 prompt
# b. test_gemini_prompt_includes_event_brief — event_brief SENTINEL 出现在 prompt
# c. test_gemini_prompt_omits_empty_sections — 两者为空时不出现对应空 section 标题
# d. test_daily_report_passes_event_brief_to_run_committee_and_macro
#    — monkeypatch resolve_event_brief_multi 返 SENTINEL，抓 run_macro_view + run_committee kwargs

def test_gemini_prompt_includes_wealth_view():
    """build_gemini_prompt 必须把 wealth_view 渲染进 prompt 正文

    任何形式的硬编码 / 漏传都会让 SENTINEL 不出现在 prompt 里，测试立即红。
    """
    from jobs.daily_report_builder import build_gemini_prompt

    SENTINEL = "WEALTH_VIEW_SENTINEL_gemini_abc"

    prompt = build_gemini_prompt(
        portfolio_summary="mock portfolio",
        macro_view="mock macro",
        cio_memos_combined="mock cio",
        gold_snapshot_text="mock gold",
        friction_report="mock friction",
        wealth_view=SENTINEL,
        event_brief="",
    )

    assert SENTINEL in prompt, (
        "❌ build_gemini_prompt 没把 wealth_view 渲染进 prompt！\n"
        "   Gemini 独立 challenge 时看不到用户真实流动性上下文。\n"
        "   检查 jobs/daily_report_builder.py build_gemini_prompt() 的 wealth_section 插入逻辑。"
    )


def test_gemini_prompt_includes_event_brief():
    """build_gemini_prompt 必须把 event_brief 渲染进 prompt 正文

    任何形式的硬编码 / 漏传都会让 SENTINEL 不出现在 prompt 里，测试立即红。
    """
    from jobs.daily_report_builder import build_gemini_prompt

    SENTINEL = "EVENT_BRIEF_SENTINEL_gemini_xyz"

    prompt = build_gemini_prompt(
        portfolio_summary="mock portfolio",
        macro_view="mock macro",
        cio_memos_combined="mock cio",
        gold_snapshot_text="mock gold",
        friction_report="mock friction",
        wealth_view="",
        event_brief=SENTINEL,
    )

    assert SENTINEL in prompt, (
        "❌ build_gemini_prompt 没把 event_brief 渲染进 prompt！\n"
        "   Gemini 独立 challenge 时看不到近期 RAG 召回的事件层上下文。\n"
        "   检查 jobs/daily_report_builder.py build_gemini_prompt() 的 event_section 插入逻辑。"
    )


def test_gemini_prompt_omits_empty_sections():
    """wealth_view="" 和 event_brief="" 时不出现对应空 section 标题

    避免 Gemini 看到"# 用户真实流动性 (WealthContextOfficer)\n\n"这种无内容的空 section，
    会让 Gemini 产生"为什么这里是空的"的困惑。
    """
    from jobs.daily_report_builder import build_gemini_prompt

    prompt = build_gemini_prompt(
        portfolio_summary="mock portfolio",
        macro_view="mock macro",
        cio_memos_combined="mock cio",
        gold_snapshot_text="mock gold",
        friction_report="mock friction",
        wealth_view="",
        event_brief="",
    )

    # 空时不应出现 section 标题
    assert "WealthContextOfficer" not in prompt, (
        "空 wealth_view 不应在 Gemini prompt 里出现 WealthContextOfficer section 标题"
    )
    assert "事件层" not in prompt, (
        "空 event_brief 不应在 Gemini prompt 里出现事件层 section 标题"
    )


# 已删: test_daily_report_passes_event_brief_to_run_committee_and_macro
# 历史: 守 daily_report 直接调 run_macro_view + run_committee 时传 event_brief 的契约
# 2026-05-16 三路径统一架构后, daily_report 改走 run_committee_session,
# 不再自己 multi 召回 + 注入 macro + 注入 committee. 这些都在 session 内一处完成,
# 由 test_run_committee_session_passes_all_shared_inputs (event_brief 部分) 统一守护.
# 原代码可经 `git log -p tests/test_committee_contract.py` 查阅.


# ============================================================================
# 契约 5: run_committee_session 三路径统一架构核心防漂
# ============================================================================
# 历史教训（2026-05-16）: 三路径（Skill/Web/Cron）各自调原语 run_committee，导致
# 同样的"加跨 entry 参数"动作要在 3 处分别改，漏一处即漂移（连续 4 次事故）。
# 抽出 run_committee_session 作为单一可信源后，本契约守护它：
# - 任何 entry 调 session，最终 run_committee 必然收到 shared inputs（wealth +
#   event_brief + macro_view）
# - 单资产失败不阻断其他资产
# - event_brief 三选一优先级（override > event_ids > multi_recall）严格执行

def test_run_committee_session_passes_all_shared_inputs_to_run_committee(
    monkeypatch, tmp_path,
):
    """3 个 SENTINEL（wealth/event/macro）必须全部到达 run_committee kwargs."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _seed_minimal_memory(memory_dir)

    from core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", memory_dir)

    SENTINEL_W = "WEALTH_SENTINEL_session_abc"
    SENTINEL_E = "EVENT_SENTINEL_session_def"
    SENTINEL_M = "MACRO_SENTINEL_session_ghi"

    # 锚定 3 个 loader 的输出
    monkeypatch.setattr("core.committee_runner.load_wealth_context_view",
                        lambda: SENTINEL_W)
    monkeypatch.setattr("core.committee_runner.resolve_event_brief_multi",
                        lambda syms: SENTINEL_E)
    monkeypatch.setattr("core.committee_runner.run_macro_view",
                        lambda *a, **kw: SENTINEL_M)
    monkeypatch.setattr("core.committee_runner.get_macro_data", lambda: "MOCK")
    monkeypatch.setattr("core.committee_runner.load_prior_insights",
                        lambda *a, **kw: "")

    # mock 行情，让 run_committee_for_symbol 能跑到 run_committee
    import pandas as pd
    fake_df = pd.DataFrame(
        {"Close": [100.0, 101.0, 102.0, 103.0, 104.0]},
        index=pd.date_range("2024-05-10", periods=5),
    )
    monkeypatch.setattr("core.committee_runner.get_history_data",
                        lambda *a, **kw: fake_df)
    monkeypatch.setattr("core.committee_runner.analyze_multi_timeframe",
                        lambda *a, **kw: "MOCK_MARKET_DATA")

    captured: list[dict] = []

    def fake_run_committee(*args, **kwargs):
        captured.append(kwargs)
        return {
            "verdict": {"verdict": "HOLD", "confidence": 0.5,
                        "alloc_cny": 0, "dominant_view": "macro",
                        "raw": "VERDICT: HOLD\nCONFIDENCE: 0.5"},
            "report": None,
        }

    monkeypatch.setattr("core.committee_runner.run_committee", fake_run_committee)

    from core.committee_runner import run_committee_session
    result = run_committee_session(symbols=["TEST.AX"], max_debate_rounds=1)

    assert captured, "run_committee 未被调用 — session dispatch 失败"
    kw = captured[0]
    assert kw.get("wealth_context_view") == SENTINEL_W, (
        f"wealth_view 漂移: 期望 {SENTINEL_W!r}, 实际 {kw.get('wealth_context_view')!r}"
    )
    assert kw.get("macro_view") == SENTINEL_M, (
        f"macro_view 漂移: 期望 {SENTINEL_M!r}, 实际 {kw.get('macro_view')!r}"
    )

    # event_brief 通过 run_committee_for_symbol 流到 _resolve_event_brief（接受 override）
    # 然后 service layer 没把它直接传给 run_committee（macro 在 prompt 里），
    # 我们改测 session 返回值
    assert result["wealth_view"] == SENTINEL_W
    assert result["event_brief"] == SENTINEL_E
    assert result["macro_view"] == SENTINEL_M


def test_run_committee_session_continues_on_single_asset_error(
    monkeypatch, tmp_path,
):
    """单资产抛异常 → errors map 含失败 symbol + 其他资产正常返回."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _seed_minimal_memory(memory_dir)

    from core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", memory_dir)

    monkeypatch.setattr("core.committee_runner.load_wealth_context_view", lambda: "")
    monkeypatch.setattr("core.committee_runner.resolve_event_brief_multi",
                        lambda syms: "")
    monkeypatch.setattr("core.committee_runner.run_macro_view",
                        lambda *a, **kw: "MOCK_MACRO")
    monkeypatch.setattr("core.committee_runner.get_macro_data", lambda: "MOCK")

    # 一个资产成功一个抛异常
    def fake_run_committee_for_symbol(symbol, **kw):
        if symbol == "BAD.SYM":
            raise RuntimeError("simulated 行情失败")
        return {
            "verdict": {"verdict": "HOLD", "confidence": 0.5,
                        "alloc_cny": 0, "dominant_view": "macro",
                        "raw": "VERDICT: HOLD"},
            "report": None,
        }

    monkeypatch.setattr("core.committee_runner.run_committee_for_symbol",
                        fake_run_committee_for_symbol)

    from core.committee_runner import run_committee_session
    result = run_committee_session(
        symbols=["GOOD.AX", "BAD.SYM"], max_debate_rounds=1,
    )

    assert "BAD.SYM" in result["errors"]
    assert "simulated 行情失败" in result["errors"]["BAD.SYM"]
    assert result["asset_committees"]["GOOD.AX"].get("verdict") is not None, (
        "其他资产应正常返回 verdict，session 不能因单资产失败阻断"
    )
    assert "GOOD.AX" not in result["errors"]


def test_run_committee_session_event_brief_override_takes_priority(
    monkeypatch, tmp_path,
):
    """event_brief_override > event_ids > resolve_event_brief_multi（严格）."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _seed_minimal_memory(memory_dir)

    from core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", memory_dir)

    SENTINEL_OVERRIDE = "OVERRIDE_BRIEF_xxx"
    SENTINEL_MULTI = "MULTI_RECALL_BRIEF_yyy"

    monkeypatch.setattr("core.committee_runner.load_wealth_context_view", lambda: "")
    monkeypatch.setattr("core.committee_runner.resolve_event_brief_multi",
                        lambda syms: SENTINEL_MULTI)  # 不应被调
    monkeypatch.setattr("core.committee_runner.run_macro_view",
                        lambda *a, **kw: "M")
    monkeypatch.setattr("core.committee_runner.get_macro_data", lambda: "MOCK")
    monkeypatch.setattr(
        "core.committee_runner.run_committee_for_symbol",
        lambda sym, **kw: {"verdict": {"verdict": "HOLD", "confidence": 0.5,
                                       "alloc_cny": 0, "dominant_view": "macro",
                                       "raw": ""}, "report": None},
    )

    from core.committee_runner import run_committee_session
    result = run_committee_session(
        symbols=["TEST.AX"],
        event_brief_override=SENTINEL_OVERRIDE,
        max_debate_rounds=1,
    )

    assert result["event_brief"] == SENTINEL_OVERRIDE, (
        "event_brief_override 必须优先于 multi_recall（严格优先级）"
    )
    assert result["audit"]["event_brief_source"] == "override"


def test_run_committee_session_event_ids_translates_via_event_store(
    monkeypatch, tmp_path,
):
    """event_ids=["ev_1"] → store.get_event + format_event_brief → 注入下游."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _seed_minimal_memory(memory_dir)

    from core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", memory_dir)

    monkeypatch.setattr("core.committee_runner.load_wealth_context_view", lambda: "")
    monkeypatch.setattr("core.committee_runner.run_macro_view",
                        lambda *a, **kw: "M")
    monkeypatch.setattr("core.committee_runner.get_macro_data", lambda: "MOCK")

    # multi_recall 不应被调（event_ids 优先于它）
    multi_called = []
    monkeypatch.setattr("core.committee_runner.resolve_event_brief_multi",
                        lambda syms: (multi_called.append(syms), "SHOULD_NOT_BE_USED")[1])

    # 锚定 EventStore: 仅"ev_1" 反查得到
    class FakeStore:
        vec_loaded = False
        def get_event(self, eid):
            if eid == "ev_1":
                return {"event_id": eid, "ts": "2026-05-15", "stance": "risk",
                        "severity": "high", "affected_symbols": ["TEST.AX"],
                        "one_line_claim": "Fake event for test", "entities": []}
            return None
        def get_sources(self, eid):
            return [{"src_name": "reuters", "url": "http://x/y"}]
    monkeypatch.setattr("core.committee_runner._get_event_store",
                        lambda: FakeStore())

    monkeypatch.setattr(
        "core.committee_runner.run_committee_for_symbol",
        lambda sym, **kw: {"verdict": {"verdict": "HOLD", "confidence": 0.5,
                                       "alloc_cny": 0, "dominant_view": "macro",
                                       "raw": ""}, "report": None},
    )

    from core.committee_runner import run_committee_session
    result = run_committee_session(
        symbols=["TEST.AX"],
        event_ids=["ev_1"],
        max_debate_rounds=1,
    )

    assert "Fake event for test" in result["event_brief"], (
        "event_ids 没翻译成 brief 注入下游"
    )
    assert result["audit"]["event_brief_source"] == "event_ids"
    assert multi_called == [], (
        "event_ids 已传时不应再调 resolve_event_brief_multi（优先级被破）"
    )


# ============================================================================
# 契约 6: Risk Officer 集中度 SENTINEL 覆写（2026-05-20 NDQ.AX 漂移修复）
# ============================================================================
# 历史教训：portfolio_summary 字面写"**集中度 33.6%**"喂给 Risk Officer，但
# DeepSeek 仍偶发 hallucinate 编成 70.2%（前一日同 prompt 输出 33.4% 正确）。
# CIO 据此误喊 TRIM ¥15,000。修复方案：service layer 在 Risk Officer 输出后
# 用 portfolio_summary 字面值强制覆写 CONCENTRATION_PCT 行。
#
# 本测验证 helper 真把脏数字改回，不是只在 prompt 里讲讲。


def test_extract_concentration_from_summary_picks_correct_asset():
    """portfolio_summary 含多个 asset 时按 (SYM) 锚定，不混淆"""
    from core.committee import _extract_concentration_from_summary

    summary = (
        "用户风险偏好: Aggressive\n"
        "总资产估算: ¥220,371\n"
        "  - **BetaShares Nasdaq 100 ETF** (NDQ.AX) (CommSec): 256.0000 股, "
        "均价 $53.86, 现价 $59.82, 浮盈 +11.06%, "
        "**集中度 33.6%** (CNY 市值 ¥74,060 / 总资产 ¥220,371)\n"
        "  - **伦敦金 (浙商积存金)** (GC=F) (浙商积存金): 133.8842 克, "
        "均价 ¥1008.34, 现价 ¥976.97, 浮盈 -3.11%, "
        "**集中度 59.4%** (CNY 市值 ¥130,800 / 总资产 ¥220,371)\n"
    )

    assert _extract_concentration_from_summary(summary, "NDQ.AX") == 33.6
    assert _extract_concentration_from_summary(summary, "GC=F") == 59.4
    # 不在 summary 里的 asset
    assert _extract_concentration_from_summary(summary, "ASIA.AX") is None
    # 空输入容忍
    assert _extract_concentration_from_summary("", "NDQ.AX") is None
    assert _extract_concentration_from_summary(summary, "") is None


def test_override_concentration_rewrites_hallucinated_value():
    """LLM 输出 70.2% 但真实 33.6% → 强制改回 33.6%"""
    from core.committee import _override_concentration_in_risk_output

    risk_output = (
        "SIGNAL: high_risk\n"
        "STRENGTH: 8\n"
        "CONCENTRATION_PCT: 70.2%\n"
        "DRY_POWDER_CNY: ¥0\n"
        "PNL_PCT: +11.06%\n"
        "ONE_LINER: NDQ集中度70%远超上限...\n"
    )

    fixed = _override_concentration_in_risk_output(risk_output, 33.6)

    assert "CONCENTRATION_PCT: 33.6%" in fixed, (
        "覆写失败: LLM hallucinate 70.2% 没被改回真实 33.6%。\n"
        f"实际输出:\n{fixed}"
    )
    assert "70.2" not in fixed.split("ONE_LINER")[0], (
        "覆写不完整: CONCENTRATION_PCT 行之前仍存留 70.2"
    )
    # ONE_LINER 里的描述性 "70%" 不动（那是 LLM 的措辞，CIO 看得到原文）
    assert "NDQ集中度70%" in fixed


def test_override_concentration_noop_when_within_tolerance():
    """LLM 输出 33.4% 真实 33.6% → 0.2% 容差内不动（正常浮动）"""
    from core.committee import _override_concentration_in_risk_output

    risk_output = "CONCENTRATION_PCT: 33.4%\nSIGNAL: ok\n"
    fixed = _override_concentration_in_risk_output(risk_output, 33.6)
    assert "CONCENTRATION_PCT: 33.4%" in fixed, (
        "0.2% 容差内不应覆写（避免把正常 rounding 改没）"
    )


def test_override_concentration_noop_when_true_pct_none():
    """portfolio_summary 没给数字（None）→ 不动 LLM 输出"""
    from core.committee import _override_concentration_in_risk_output

    risk_output = "CONCENTRATION_PCT: 70.2%\n"
    fixed = _override_concentration_in_risk_output(risk_output, None)
    assert fixed == risk_output, "true_pct=None 时不应做任何修改"


def test_override_concentration_noop_when_field_missing():
    """LLM 完全没输出该字段 → 不凭空注入（避免脏数据）"""
    from core.committee import _override_concentration_in_risk_output

    risk_output = "SIGNAL: ok\nSTRENGTH: 3\nONE_LINER: 无风险\n"
    fixed = _override_concentration_in_risk_output(risk_output, 33.6)
    assert fixed == risk_output, (
        "LLM 没输出 CONCENTRATION_PCT 时不应凭空注入字段"
    )


# ============================================================================
# 契约 7: 新维度（sentiment_brief / valuation_brief）— 确定性事实块防漂
# ============================================================================
# 对齐 TradingAgents 补的基本面 + 情绪维度。做成确定性 shared 块（像 wealth_view /
# event_brief），必须真的从 loader 流到 run_committee，否则就是"算了但没人看"的漂移。
#
# - graceful: 两个 loader 任何失败都退化 ""（不阻断 committee）
# - SENTINEL: session→for_symbol→run_committee 链路真的把 loader 结果传到 run_committee
# - 渲染: to_cio_brief 把两段事实渲染进 CIO 输入（CIO 必须能看到）


def test_load_sentiment_brief_graceful_on_failure(monkeypatch):
    """build_sentiment_brief 抛异常 → load_sentiment_brief 退化 ""，不抛"""
    import utils.sentiment as st

    def boom(*a, **k):
        raise RuntimeError("VIX source down")
    monkeypatch.setattr(st, "build_sentiment_brief", boom)

    from core.committee_runner import load_sentiment_brief
    assert load_sentiment_brief("") == ""


def test_load_valuation_brief_graceful_on_failure(monkeypatch):
    """build_valuation_brief 抛异常 → load_valuation_brief 退化 ""，不抛"""
    import utils.valuation as val

    def boom(*a, **k):
        raise RuntimeError("yfinance .info down")
    monkeypatch.setattr(val, "build_valuation_brief", boom)

    from core.committee_runner import load_valuation_brief
    assert load_valuation_brief("NDQ.AX", 0.5) == ""


def test_run_committee_session_passes_sentiment_and_valuation_to_run_committee(
    monkeypatch, tmp_path,
):
    """SENTINEL: sentiment_brief（session 共享）+ valuation_brief（per-asset）必须
    全部到达 run_committee kwargs。

    漂移会被抓：
    - session 算了 sentiment 但没传 for_symbol → captured.sentiment_brief != SENTINEL
    - for_symbol 没调 load_valuation_brief / 没传 run_committee → != SENTINEL
    """
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _seed_minimal_memory(memory_dir)

    from core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", memory_dir)

    SENTINEL_SENT = "SENTIMENT_SENTINEL_xyz"
    SENTINEL_VAL = "VALUATION_SENTINEL_abc"

    # 锚定新 loader 的输出
    monkeypatch.setattr("core.committee_runner.load_sentiment_brief",
                        lambda *a, **k: SENTINEL_SENT)
    monkeypatch.setattr("core.committee_runner.load_valuation_brief",
                        lambda *a, **k: SENTINEL_VAL)
    # 其余 shared loader / 数据全 mock，让链路能跑到 run_committee
    monkeypatch.setattr("core.committee_runner.load_wealth_context_view", lambda: "")
    monkeypatch.setattr("core.committee_runner.resolve_event_brief_multi",
                        lambda syms: "")
    monkeypatch.setattr("core.committee_runner.run_macro_view",
                        lambda *a, **kw: "MOCK_MACRO")
    monkeypatch.setattr("core.committee_runner.get_macro_data", lambda: "MOCK")
    monkeypatch.setattr("core.committee_runner.load_prior_insights",
                        lambda *a, **kw: "")

    import pandas as pd
    fake_df = pd.DataFrame(
        {"Close": [100.0, 101.0, 102.0, 103.0, 104.0]},
        index=pd.date_range("2024-05-10", periods=5),
    )
    monkeypatch.setattr("core.committee_runner.get_history_data",
                        lambda *a, **kw: fake_df)
    monkeypatch.setattr("core.committee_runner.analyze_multi_timeframe",
                        lambda *a, **kw: "MOCK_MARKET_DATA")

    captured: list[dict] = []

    def fake_run_committee(*args, **kwargs):
        captured.append(kwargs)
        return {
            "verdict": {"verdict": "HOLD", "confidence": 0.5,
                        "alloc_cny": 0, "dominant_view": "macro",
                        "raw": "VERDICT: HOLD"},
            "report": None,
        }

    monkeypatch.setattr("core.committee_runner.run_committee", fake_run_committee)

    from core.committee_runner import run_committee_session
    result = run_committee_session(symbols=["TEST.AX"], max_debate_rounds=1)

    assert captured, "run_committee 未被调用 — session dispatch 失败"
    kw = captured[0]
    assert kw.get("sentiment_brief") == SENTINEL_SENT, (
        f"sentiment_brief 漂移: 期望 {SENTINEL_SENT!r}, 实际 {kw.get('sentiment_brief')!r}\n"
        "  → session 共享 sentiment 没流到 run_committee（检查 _run_one 是否传了 sentiment_brief）"
    )
    assert kw.get("valuation_brief") == SENTINEL_VAL, (
        f"valuation_brief 漂移: 期望 {SENTINEL_VAL!r}, 实际 {kw.get('valuation_brief')!r}\n"
        "  → for_symbol 没调 load_valuation_brief 或没传 run_committee"
    )
    assert result["sentiment_brief"] == SENTINEL_SENT
    assert result["audit"]["sentiment_brief_attached"] is True


def test_to_cio_brief_renders_sentiment_and_valuation():
    """to_cio_brief 必须把 sentiment_brief + valuation_brief 渲染进 CIO 输入"""
    from core.committee import CommitteeReport

    SENTINEL_SENT = "SENT_CIO_SENTINEL_111"
    SENTINEL_VAL = "VAL_CIO_SENTINEL_222"

    report = CommitteeReport(
        asset={"symbol": "NDQ.AX", "display_name": "Test"},
        macro_view="m", quant_view="q", risk_view="r",
        sentiment_brief=SENTINEL_SENT, valuation_brief=SENTINEL_VAL,
    )
    brief = report.to_cio_brief()
    assert SENTINEL_SENT in brief, "sentiment_brief 没渲染进 CIO 输入"
    assert SENTINEL_VAL in brief, "valuation_brief 没渲染进 CIO 输入"


def test_to_cio_brief_omits_empty_new_dimensions():
    """空 sentiment/valuation → 不出现对应 section 标题（避免空 section 干扰）"""
    from core.committee import CommitteeReport

    report = CommitteeReport(
        asset={"symbol": "GC=F", "display_name": "Gold"},
        macro_view="m", quant_view="q", risk_view="r",
        sentiment_brief="", valuation_brief="",
    )
    brief = report.to_cio_brief()
    assert "VALUATION" not in brief
    assert "MARKET SENTIMENT" not in brief


def test_run_committee_injects_sentiment_and_valuation_into_agents(monkeypatch):
    """端到端：run_committee 把 sentiment_brief 注入 Quant 输入，sentiment+valuation 注入 CIO。

    SENTINEL 守卫：未来有人删了 quant_input/cio_brief 的注入，本测立即红。
    """
    from core import committee as cmt

    SENT = "SENT_E2E_SENTINEL_777"
    VAL = "VAL_E2E_SENTINEL_888"
    captured: dict[str, str] = {}

    class RecordingAgent:
        def __init__(self, role):
            self._role = role
        def run(self, ctx):
            # 记录每个角色最后看到的 context
            captured[self._role] = ctx
            if self._role == "quant":
                return "REGIME: uptrend\nSIGNAL: neutral\nSTRENGTH: 4\nONE_LINER: x\n"
            if self._role == "risk":
                return "SIGNAL: ok\nSTRENGTH: 3\nONE_LINER: x\n"
            if self._role == "cio":
                return (
                    "VERDICT: HOLD\nCONFIDENCE: 0.5\nDOMINANT_VIEW: macro\n"
                    "SUGGESTED_ALLOC_CNY: 0\nTRIM_REASON: N/A\nREENTRY_PRICE: N/A\n"
                    "REENTRY_CONDITION: N/A\nEXPECTED_PATH: N/A\n"
                    "EXECUTION_PLAN:\n  mode: none\n  first_tranche_cny: 0\n  add_levels:\n    - N/A\n"
                    "RISK_PLAN:\n  stop_loss_trigger: N/A\n  what_if_wrong:\n    "
                    "worst_case_pnl_cny: 0\n    recovery_estimate: N/A\n"
                    "PERSONAL_NOTE:\n  - N/A\n  - N/A\n  - N/A\n"
                )
            return ""

    monkeypatch.setattr(cmt, "_create_agent",
                        lambda _p, **kw: RecordingAgent(kw.get("role")))
    monkeypatch.setattr(cmt, "_persist", lambda *a, **kw: None)

    cmt.run_committee(
        asset={"symbol": "NDQ.AX", "display_name": "Test"},
        market_data="fake market",
        macro_view="fake macro",
        portfolio_summary="用户风险偏好: Balanced\n",
        regime_brief="REGIME: uptrend",
        sentiment_brief=SENT,
        valuation_brief=VAL,
        max_debate_rounds=1,
    )

    assert SENT in captured.get("quant", ""), "sentiment_brief 没注入 Quant 输入"
    assert VAL in captured.get("quant", ""), "valuation_brief 没注入 Quant 输入"
    assert SENT in captured.get("cio", ""), "sentiment_brief 没注入 CIO 输入"
    assert VAL in captured.get("cio", ""), "valuation_brief 没注入 CIO 输入"


def test_run_committee_overrides_risk_concentration_end_to_end(monkeypatch):
    """端到端：mock LLM 让 Risk Officer 故意编 70.2%，验证最终 risk_view 是 33.6%

    这是真正的 SENTINEL 守卫——如果未来有人删了 _override 调用，本测会红。
    """
    from core import committee as cmt

    # 构造典型 portfolio_summary（与 utils.portfolio_summary 输出格式一致）
    fake_summary = (
        "用户风险偏好: Aggressive\n"
        "总资产估算: ¥220,371\n"
        "  - **BetaShares Nasdaq 100 ETF** (NDQ.AX) (CommSec): 256 股, "
        "均价 $53.86, 现价 $59.82, 浮盈 +11.06%, "
        "**集中度 33.6%** (CNY 市值 ¥74,060 / 总资产 ¥220,371)\n"
    )

    # mock _create_agent → 返回一个会输出固定字符串的假 agent
    # _ask() 走 agent.run(context)，所以 FakeAgent 必须实现 .run()
    class FakeAgent:
        def __init__(self, fixed_reply: str):
            self._reply = fixed_reply
        def run(self, _ctx: str) -> str:
            return self._reply

    HALLUCINATED_RISK = (
        "SIGNAL: high_risk\n"
        "STRENGTH: 8\n"
        "CONCENTRATION_PCT: 70.2%\n"  # ← LLM 编的
        "DRY_POWDER_CNY: ¥0\n"
        "PNL_PCT: +11.06%\n"
        "ONE_LINER: 集中度过高\n"
    )
    FAKE_QUANT = (
        "REGIME: uptrend\n"
        "SIGNAL: neutral\n"
        "STRENGTH: 4\n"
        "ONE_LINER: 等待回调\n"
    )
    FAKE_CIO = (
        "VERDICT: HOLD\n"
        "CONFIDENCE: 0.5\n"
        "DOMINANT_VIEW: risk\n"
        "SUGGESTED_ALLOC_CNY: 0\n"
        "EXECUTION_PLAN:\n  mode: none\n  first_tranche_cny: 0\n  add_levels:\n    - N/A\n"
        "RISK_PLAN:\n  stop_loss_trigger: N/A\n  what_if_wrong:\n    "
        "worst_case_pnl_cny: 0\n    recovery_estimate: N/A\n"
        "PERSONAL_NOTE:\n  - N/A\n  - N/A\n  - N/A\n"
    )

    def fake_create_agent(_prompt, **kwargs):
        role = kwargs.get("role")
        if role == "risk":
            return FakeAgent(HALLUCINATED_RISK)
        if role == "quant":
            return FakeAgent(FAKE_QUANT)
        if role == "cio":
            return FakeAgent(FAKE_CIO)
        return FakeAgent("")

    monkeypatch.setattr(cmt, "_create_agent", fake_create_agent)
    # 跳过 persist（写文件 + DB）—— 单测不关心
    monkeypatch.setattr(cmt, "_persist", lambda *a, **kw: None)

    result = cmt.run_committee(
        asset={"symbol": "NDQ.AX", "display_name": "BetaShares Nasdaq 100 ETF"},
        market_data="fake market",
        macro_view="fake macro",
        portfolio_summary=fake_summary,
        prior_insights="",
        regime_brief="REGIME: uptrend",
        wealth_context_view="",
        max_debate_rounds=1,
    )

    # 关键断言：risk_view 应该被覆写
    risk_view = result.get("report").risk_view if result.get("report") else ""
    assert "CONCENTRATION_PCT: 33.6%" in risk_view, (
        "❌ SENTINEL 覆写失效！\n"
        f"   LLM hallucinate 输出 70.2%，但 service layer 没覆写回 33.6%。\n"
        f"   实际 risk_view:\n{risk_view}\n"
        "   检查 core/committee.py 的 _override_concentration_in_risk_output 调用是否还在。"
    )
    assert "CONCENTRATION_PCT: 70.2%" not in risk_view, (
        "覆写不彻底，hallucinated 70.2% 仍在 risk_view"
    )


# ---------------------------------------------------------------------------
# 契约 8: risk_profile 风险档 — run_committee 真把 regime/防御哨兵传进 parse_cio_memo
# ---------------------------------------------------------------------------
# 2026-06 拆 regime 方向锁后，uptrend 杠杆保留为显式 config 开关（默认 steady）。
# 本契约守：aggressive 档下 run_committee 端到端真的会把 HOLD 升级 ACCUMULATE
# （即 regime_brief 标签 + sentiment_brief 防御哨兵真的接进了 parse_cio_memo）。

def _make_hold_cio_agent_factory(captured: dict):
    class FakeAgent:
        def __init__(self, role):
            self._role = role

        def run(self, ctx):
            captured[self._role] = ctx
            if self._role == "quant":
                return "REGIME: uptrend\nSIGNAL: neutral\nSTRENGTH: 4\nONE_LINER: x\n"
            if self._role == "risk":
                return "SIGNAL: ok\nSTRENGTH: 3\nONE_LINER: x\n"
            if self._role == "cio":
                return (
                    "VERDICT: HOLD\nCONFIDENCE: 0.5\nDOMINANT_VIEW: macro\n"
                    "SUGGESTED_ALLOC_CNY: 0\n"
                )
            return ""
    return lambda _p, **kw: FakeAgent(kw.get("role"))


def test_run_committee_applies_aggressive_risk_profile(monkeypatch):
    """aggressive + uptrend regime_brief → 最终 verdict 升级 ACCUMULATE（端到端）"""
    from core import committee as cmt
    from core.config import reset_config, set_config_override

    reset_config()
    set_config_override({"verdict": {"risk_profile": "aggressive"}})
    try:
        captured: dict = {}
        monkeypatch.setattr(cmt, "_create_agent", _make_hold_cio_agent_factory(captured))
        monkeypatch.setattr(cmt, "_persist", lambda *a, **kw: None)
        result = cmt.run_committee(
            asset={"symbol": "NDQ.AX", "display_name": "Test"},
            market_data="fake market",
            macro_view="fake macro",
            portfolio_summary="用户风险偏好: Balanced\n",
            regime_brief="REGIME: uptrend\nREASON: x\nSTRATEGY_HINT: x",
            max_debate_rounds=1,
        )
        assert result["verdict"]["verdict"] == "ACCUMULATE", (
            "aggressive 档下 uptrend+HOLD 没升级 — regime 标签没接进 parse_cio_memo？"
        )
        assert result["verdict"]["_risk_profile_applied"] == (
            "aggressive_uptrend_hold_to_accumulate"
        )
    finally:
        reset_config()


def test_run_committee_defense_flag_blocks_aggressive(monkeypatch):
    """sentiment_brief 含 INDEP_DEFENSE_FLAG: on → aggressive 杠杆被哨兵拦下（端到端）"""
    from core import committee as cmt
    from core.config import reset_config, set_config_override

    reset_config()
    set_config_override({"verdict": {"risk_profile": "aggressive"}})
    try:
        captured: dict = {}
        monkeypatch.setattr(cmt, "_create_agent", _make_hold_cio_agent_factory(captured))
        monkeypatch.setattr(cmt, "_persist", lambda *a, **kw: None)
        result = cmt.run_committee(
            asset={"symbol": "NDQ.AX", "display_name": "Test"},
            market_data="fake market",
            macro_view="fake macro",
            portfolio_summary="用户风险偏好: Balanced\n",
            regime_brief="REGIME: uptrend\nREASON: x\nSTRATEGY_HINT: x",
            sentiment_brief=(
                "FEAR_GREED_GAUGE: VIX=35.0 (近2年分位 99%) → extreme_fear\n"
                "INDEP_DEFENSE_FLAG: on  # 快崩哨兵"
            ),
            max_debate_rounds=1,
        )
        assert result["verdict"]["verdict"] == "HOLD", (
            "防御哨兵 on 时 aggressive 杠杆必须被拦下 — defense_flag_on 没接进 parse_cio_memo？"
        )
        assert "_risk_profile_applied" not in result["verdict"]
    finally:
        reset_config()


# ---------------------------------------------------------------------------
# 契约 9: 独立快崩防御 — ATR 腿（service layer 算）+ 确定性买侧降级（端到端）
# ---------------------------------------------------------------------------
# MA120 regime 看不见快崩（COVID 全程 uptrend），crash 锁双条件永不触发。
# 防御 = VIX 哨兵（市场级, sentiment_brief）OR ATR 飙升（资产级, metrics），
# 独立于 regime，确定性降级 BUY→ACCUMULATE / ACCUMULATE→HOLD。

def _setup_session_mocks(monkeypatch, tmp_path, *, atr_spike_ratio: float):
    """复用契约 5/7 的 mock 骨架，metrics.atr_spike_ratio 可控
    （通用防御线 sentiment.atr_defense_spike_ratio=2.0）"""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _seed_minimal_memory(memory_dir)
    from core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", memory_dir)

    monkeypatch.setattr("core.committee_runner.load_wealth_context_view", lambda: "")
    monkeypatch.setattr("core.committee_runner.resolve_event_brief_multi", lambda syms: "")
    monkeypatch.setattr("core.committee_runner.run_macro_view", lambda *a, **kw: "MOCK_MACRO")
    monkeypatch.setattr("core.committee_runner.get_macro_data", lambda: "MOCK")
    monkeypatch.setattr("core.committee_runner.load_prior_insights", lambda *a, **kw: "")
    monkeypatch.setattr("core.committee_runner.load_sentiment_brief", lambda *a, **k: "")
    monkeypatch.setattr("core.committee_runner.load_valuation_brief", lambda *a, **k: "")

    import pandas as pd
    fake_df = pd.DataFrame(
        {"Close": [100.0, 101.0, 102.0, 103.0, 104.0]},
        index=pd.date_range("2024-05-10", periods=5),
    )
    monkeypatch.setattr("core.committee_runner.get_history_data", lambda *a, **kw: fake_df)
    monkeypatch.setattr("core.committee_runner.analyze_multi_timeframe",
                        lambda *a, **kw: "MOCK_MARKET_DATA")
    # metrics.atr_spike_ratio 可控 → ATR 腿确定性可测（通用线 2.0，无 per-asset）
    monkeypatch.setattr(
        "core.committee_runner.compute_metrics",
        lambda df: {
            "ma20": 100.0, "ma120": 96.0, "atr_pct": 1.5,
            "atr_spike_ratio": atr_spike_ratio,
            "price_quantile_2y": 0.9, "return_30d": -0.05,
            "rebound_off_30d_low": None, "current_price": 104.0,
        },
    )

    captured: list[dict] = []

    def fake_run_committee(*args, **kwargs):
        captured.append(kwargs)
        return {
            "verdict": {"verdict": "HOLD", "confidence": 0.5,
                        "alloc_cny": 0, "dominant_view": "macro",
                        "raw": "VERDICT: HOLD"},
            "report": None,
        }

    monkeypatch.setattr("core.committee_runner.run_committee", fake_run_committee)
    return captured


def test_service_layer_computes_atr_defense_leg(monkeypatch, tmp_path):
    """突变比 2.5 ≥ 通用线 2.0 → run_committee 收到 atr_defense_on=True"""
    captured = _setup_session_mocks(monkeypatch, tmp_path, atr_spike_ratio=2.5)
    from core.committee_runner import run_committee_session
    run_committee_session(symbols=["TEST.AX"], max_debate_rounds=1)
    assert captured, "run_committee 未被调用"
    assert captured[0].get("atr_defense_on") is True, (
        "ATR 腿没接进 run_committee — 检查 run_committee_for_symbol 的 atr_defense_on 计算"
    )


def test_service_layer_atr_defense_off_when_calm(monkeypatch, tmp_path):
    """突变比 1.1 < 通用线 2.0 → atr_defense_on=False（防御不乱触发）"""
    captured = _setup_session_mocks(monkeypatch, tmp_path, atr_spike_ratio=1.1)
    from core.committee_runner import run_committee_session
    run_committee_session(symbols=["TEST.AX"], max_debate_rounds=1)
    assert captured, "run_committee 未被调用"
    assert captured[0].get("atr_defense_on") is False


def test_run_committee_atr_defense_downgrades_accumulate(monkeypatch):
    """端到端：atr_defense_on=True + CIO 给 ACCUMULATE → 最终 HOLD（确定性降级）"""
    from core import committee as cmt

    class FakeAgent:
        def __init__(self, role):
            self._role = role

        def run(self, ctx):
            if self._role == "quant":
                return "REGIME: uptrend\nSIGNAL: bullish\nSTRENGTH: 6\nONE_LINER: x\n"
            if self._role == "risk":
                return "SIGNAL: ok\nSTRENGTH: 3\nONE_LINER: x\n"
            if self._role == "cio":
                return (
                    "VERDICT: ACCUMULATE\nCONFIDENCE: 0.6\nDOMINANT_VIEW: quant\n"
                    "SUGGESTED_ALLOC_CNY: 5000\n"
                )
            return ""

    monkeypatch.setattr(cmt, "_create_agent", lambda _p, **kw: FakeAgent(kw.get("role")))
    monkeypatch.setattr(cmt, "_persist", lambda *a, **kw: None)
    result = cmt.run_committee(
        asset={"symbol": "NDQ.AX", "display_name": "Test"},
        market_data="fake market",
        macro_view="fake macro",
        portfolio_summary="用户风险偏好: Balanced\n",
        regime_brief="REGIME: uptrend\nREASON: MA 滞后\nSTRATEGY_HINT: x",
        atr_defense_on=True,
        max_debate_rounds=1,
    )
    v = result["verdict"]
    assert v["verdict"] == "HOLD", (
        "ATR 防御没降级 ACCUMULATE — atr_defense_on 没接进 parse_cio_memo？"
    )
    assert v["_defense_downgrade"] == "accumulate_to_hold"
    assert v["alloc_cny"] == 0
