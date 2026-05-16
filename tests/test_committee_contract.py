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


def test_daily_report_passes_loaded_wealth_view_to_run_committee(monkeypatch, tmp_path):
    """**核心防漂移契约** — daily_report 必须真把 load_wealth_context_view()
    的结果传给 run_committee 的 wealth_context_view= 参数。

    用 SENTINEL 字符串锚定。任何形式的硬编码 / 漏调 / 字段名错都会让 captured
    不等于 SENTINEL，test 红。
    """
    # 1. seed memory
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _seed_minimal_memory(memory_dir)

    from core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", memory_dir)

    # 2. monkeypatch shared loader 返回 SENTINEL（验证 entry 真的把 loader 结果用了）
    SENTINEL = "TEST_SENTINEL_WEALTH_VIEW_xyz789"
    monkeypatch.setattr(
        "jobs.daily_report.load_wealth_context_view",
        lambda: SENTINEL,
    )

    # 3. monkeypatch run_committee 抓 kwargs（也避开真 LLM 调用）
    captured_kwargs_list: list[dict] = []

    def fake_run_committee(**kwargs):
        captured_kwargs_list.append(kwargs)
        return {
            "verdict": {
                "verdict": "HOLD",
                "confidence": 0.5,
                "alloc_cny": 0,
                "dominant_view": "macro",
                "raw": "VERDICT: HOLD\nCONFIDENCE: 0.5",
            },
            "report": None,
        }

    monkeypatch.setattr("jobs.daily_report.run_committee", fake_run_committee)
    monkeypatch.setattr("jobs.daily_report.run_macro_view", lambda *a, **kw: "MOCK_MACRO")

    # 4. mock 重 IO（yfinance / 邮件 / 价格快照），让 daily_report 能跑到 committee loop
    import pandas as pd
    fake_df = pd.DataFrame(
        {"Close": [100.0, 101.0, 102.0, 103.0, 104.0]},
        index=pd.date_range("2024-05-10", periods=5),
    )
    monkeypatch.setattr("jobs.daily_report.get_history_data", lambda *a, **kw: fake_df)
    monkeypatch.setattr("jobs.daily_report.get_macro_data", lambda: "MOCK_DATA")
    monkeypatch.setattr("jobs.daily_report.analyze_multi_timeframe",
                        lambda *a, **kw: "MOCK_MARKET_DATA")
    # 邮件 send 不能真跑（没 SMTP 凭据），mock 掉
    monkeypatch.setattr("jobs.daily_report.send_gmail_notification",
                        lambda *a, **kw: None)

    # 5. 跑 daily_report.run()
    from jobs import daily_report
    try:
        daily_report.run()
    except Exception as e:
        # 邮件 send / 落盘等可能因测试环境抛错，但只要 run_committee 被调过了我们就有数据
        if not captured_kwargs_list:
            raise AssertionError(
                f"daily_report.run() 跑挂前 run_committee 没被调用。"
                f"无法验证 wealth_context_view 是否传对。原始错误: {e}"
            ) from e

    # 6. 真正的契约：每次调 run_committee 都必须传 SENTINEL
    assert captured_kwargs_list, (
        "run_committee 没被调用 — daily_report 流程没走到 committee loop。"
        "可能 target_assets 为空 / 数据 prep 阶段失败。"
    )
    for i, kwargs in enumerate(captured_kwargs_list):
        actual = kwargs.get("wealth_context_view")
        assert actual == SENTINEL, (
            f"❌ 第 {i+1} 次 run_committee 调用没拿到 loader 结果！\n"
            f"   期望: {SENTINEL!r}（load_wealth_context_view 返回的 sentinel）\n"
            f"   实际 wealth_context_view = {actual!r}\n"
            f"\n"
            f"   可能的漂移：\n"
            f"   - daily_report 写 `wealth_context_view=\"\"` 硬编码\n"
            f"   - 漏调 load_wealth_context_view() 直接传空字符串\n"
            f"   - 字段名拼错（如 wealth_view= 而非 wealth_context_view=）"
        )


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


def test_daily_report_passes_event_brief_to_run_committee_and_macro(monkeypatch, tmp_path):
    """**核心防漂移契约** — daily_report cron 路径必须真把跨资产 event_brief
    同时注入 run_macro_view(event_brief=...) 和每个 run_committee(..., event_brief=...)。

    用 SENTINEL 字符串通过 monkeypatch resolve_event_brief_multi 锚定。
    任何形式的漏传 / 硬编码 / 字段名错都会让 captured != SENTINEL，测试红。
    """
    # 1. seed memory
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _seed_minimal_memory(memory_dir)

    from core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", memory_dir)

    # 2. monkeypatch resolve_event_brief_multi 返回 SENTINEL（验证 entry 真的把结果用了）
    SENTINEL = "EVENT_BRIEF_SENTINEL_daily_report_cron_789"
    monkeypatch.setattr(
        "jobs.daily_report.resolve_event_brief_multi",
        lambda symbols: SENTINEL,
    )

    # 3. 抓 run_macro_view 收到的 event_brief kwarg
    captured_macro_kwargs: list[dict] = []

    def fake_run_macro_view(*args, **kwargs):
        captured_macro_kwargs.append(kwargs)
        return "MOCK_MACRO"

    monkeypatch.setattr("jobs.daily_report.run_macro_view", fake_run_macro_view)

    # 4. 抓 run_committee 收到的 event_brief kwarg
    captured_committee_kwargs: list[dict] = []

    def fake_run_committee(**kwargs):
        captured_committee_kwargs.append(kwargs)
        return {
            "verdict": {
                "verdict": "HOLD",
                "confidence": 0.5,
                "alloc_cny": 0,
                "dominant_view": "macro",
                "raw": "VERDICT: HOLD\nCONFIDENCE: 0.5",
            },
            "report": None,
        }

    monkeypatch.setattr("jobs.daily_report.run_committee", fake_run_committee)

    # 5. mock 重 IO（yfinance / 邮件 / 价格快照），让 daily_report 能跑到 committee loop
    import pandas as pd
    fake_df = pd.DataFrame(
        {"Close": [100.0, 101.0, 102.0, 103.0, 104.0]},
        index=pd.date_range("2024-05-10", periods=5),
    )
    monkeypatch.setattr("jobs.daily_report.get_history_data", lambda *a, **kw: fake_df)
    monkeypatch.setattr("jobs.daily_report.get_macro_data", lambda: "MOCK_DATA")
    monkeypatch.setattr("jobs.daily_report.analyze_multi_timeframe",
                        lambda *a, **kw: "MOCK_MARKET_DATA")
    monkeypatch.setattr("jobs.daily_report.send_gmail_notification",
                        lambda *a, **kw: None)
    # wealth_view 也 mock 掉，避免 load_wealth_context_view 读真实 memory 路径
    monkeypatch.setattr(
        "jobs.daily_report.load_wealth_context_view",
        lambda: "MOCK_WEALTH_VIEW",
    )

    # 6. 跑 daily_report.run()
    from jobs import daily_report
    try:
        daily_report.run()
    except Exception as e:
        # 邮件 send / 落盘等可能因测试环境抛错，但只要两边 mock 被调过了就有数据
        if not captured_macro_kwargs and not captured_committee_kwargs:
            raise AssertionError(
                f"daily_report.run() 跑挂前 run_macro_view 和 run_committee 都没被调用。"
                f"无法验证 event_brief 是否传对。原始错误: {e}"
            ) from e

    # 7. 契约 A：run_macro_view 必须收到 event_brief=SENTINEL
    assert captured_macro_kwargs, (
        "run_macro_view 没被调用 — daily_report 流程没走到 macro 阶段。"
        "可能 target_assets 为空 / 数据 prep 阶段提前退出。"
    )
    macro_event_brief = captured_macro_kwargs[0].get("event_brief")
    assert macro_event_brief == SENTINEL, (
        f"❌ run_macro_view 没收到 resolve_event_brief_multi 的结果！\n"
        f"   期望: {SENTINEL!r}（resolve_event_brief_multi 返回的 sentinel）\n"
        f"   实际 event_brief = {macro_event_brief!r}\n"
        f"\n"
        f"   可能的漂移：\n"
        f"   - daily_report 没把 event_brief 变量传给 run_macro_view\n"
        f"   - 传了但用了不同的变量名（如传了另一个空字符串）\n"
        f"   - resolve_event_brief_multi 调用在 run_macro_view 调用之后"
    )

    # 8. 契约 B：每次 run_committee 调用都必须收到 event_brief=SENTINEL
    assert captured_committee_kwargs, (
        "run_committee 没被调用 — daily_report 流程没走到 committee loop。"
        "可能 target_assets 为空 / 数据 prep 阶段失败。"
    )
    for i, kwargs in enumerate(captured_committee_kwargs):
        actual = kwargs.get("event_brief")
        assert actual == SENTINEL, (
            f"❌ 第 {i+1} 次 run_committee 调用没收到 resolve_event_brief_multi 的结果！\n"
            f"   期望: {SENTINEL!r}（resolve_event_brief_multi 返回的 sentinel）\n"
            f"   实际 event_brief = {actual!r}\n"
            f"\n"
            f"   可能的漂移：\n"
            f"   - daily_report committee for-loop 没传 event_brief= kwarg\n"
            f"   - 传了但用了硬编码空字符串\n"
            f"   - resolve_event_brief_multi 的结果没存到 event_brief 变量"
        )


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
