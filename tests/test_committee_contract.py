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


def test_skill_cmd_run_committee_passes_loaded_wealth_view(monkeypatch, tmp_path):
    """同上契约，但测 scripts.skill.cmd_run_committee Direct 路径（agent CLI）。

    Direct 路径跟 daily_report 是两个独立 entry，都接 run_committee，都得通过
    shared loader。两个 entry 都要测才能挡住漂移。
    """
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _seed_minimal_memory(memory_dir)

    from core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", memory_dir)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-fake-key")  # cmd_run_committee guard

    SENTINEL = "TEST_SENTINEL_SKILL_xyz456"
    # cmd_run_committee 在函数内做 `from core.committee import load_wealth_context_view`，
    # 每次执行重新查 core.committee.load_wealth_context_view —— patch 那里
    monkeypatch.setattr(
        "core.committee.load_wealth_context_view",
        lambda: SENTINEL,
    )

    captured_kwargs_list: list[dict] = []

    def fake_run_committee(**kwargs):
        captured_kwargs_list.append(kwargs)
        return {
            "verdict": {
                "verdict": "HOLD", "confidence": 0.5, "alloc_cny": 0,
                "dominant_view": "macro",
                "raw": "VERDICT: HOLD\nCONFIDENCE: 0.5",
            },
            "report": None,
        }

    monkeypatch.setattr("core.committee.run_committee", fake_run_committee)
    monkeypatch.setattr("core.committee.run_macro_view", lambda *a, **kw: "MOCK_MACRO")

    import pandas as pd
    fake_df = pd.DataFrame(
        {"Close": [100.0, 101.0, 102.0, 103.0, 104.0]},
        index=pd.date_range("2024-05-10", periods=5),
    )
    # cmd_run_committee 通过 utils.exchange_fee 拉数据，patch 那里
    monkeypatch.setattr("utils.exchange_fee.get_history_data", lambda *a, **kw: fake_df)
    monkeypatch.setattr("utils.exchange_fee.get_macro_data", lambda: "MOCK")
    monkeypatch.setattr("utils.exchange_fee.analyze_multi_timeframe",
                        lambda *a, **kw: "MOCK_MARKET")

    # 跑 cmd_run_committee("TEST.AX")
    import argparse
    args = argparse.Namespace(symbol="TEST.AX", force=False, max_rounds=1)
    from scripts import skill
    try:
        skill.cmd_run_committee(args)
    except SystemExit:
        pass  # cmd_run_committee 可能 sys.exit；只要 run_committee 被调了就够
    except Exception as e:
        if not captured_kwargs_list:
            raise AssertionError(
                f"cmd_run_committee 跑挂前 run_committee 没被调用。原始错误: {e}"
            ) from e

    assert captured_kwargs_list, (
        "cmd_run_committee 没调到 run_committee — Direct 路径流程没走通"
    )
    for kwargs in captured_kwargs_list:
        actual = kwargs.get("wealth_context_view")
        assert actual == SENTINEL, (
            f"❌ scripts.skill.cmd_run_committee 没把 loader 结果传给 run_committee!\n"
            f"   期望 {SENTINEL!r}, 实际 {actual!r}"
        )


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
