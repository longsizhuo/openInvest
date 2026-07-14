"""jobs/daily_report_builder.py 单元测试（纯函数，零 IO）

覆盖：
- classify_asset_freshness() 各场景
- format_staleness_warning() 各场景
- portfolio_summary_text() 持仓/现金/浮盈显示
- assemble_full_report() 报告结构
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import sys

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.jobs.daily_report_builder import (
    assemble_full_report,
    build_tldr_block,
    classify_asset_freshness,
    format_staleness_warning,
    portfolio_summary_text,
)


# ============ Fixture 辅助 ============

def _make_pm(tmp_path, cash=None, holdings=None) -> PortfolioManager:
    """创建一个带完整 memory 的 PortfolioManager（v2 结构）"""
    store = MemoryStore(tmp_path / "memory")
    cash = cash if cash is not None else {"CNY": 50000.0, "AUD": 500.0}
    holdings = holdings if holdings is not None else []

    store.write("user", "user", {
        "display_name": "TestUser",
        "risk_tolerance": "Balanced",
    }, "")
    store.write("strategy", "strategy", {
        "target_allocation_stock": 0.7,
        "target_allocation_cash": 0.3,
        "target_assets": [{"symbol": "NDQ.AX", "max_single_invest_cny": 10000.0}],
    }, "")
    store.write("portfolio", "state", {
        "schema_version": 2,
        "cash": cash,
        "holdings": holdings,
    }, "")
    return PortfolioManager(store)


# ============ 任务 3a：classify_asset_freshness ============

class TestClassifyAssetFreshness:
    def test_none_price_is_missing(self):
        assert classify_asset_freshness(None, 0) == "missing"

    def test_fresh_no_age(self):
        """price 有值，age_days=None → fresh"""
        assert classify_asset_freshness(100.0, None) == "fresh"

    def test_fresh_within_threshold(self):
        """age_days=2 < 默认 threshold=3 → fresh"""
        assert classify_asset_freshness(100.0, 2) == "fresh"

    def test_stale_between_thresholds(self):
        """3 <= age_days < 7 → stale"""
        assert classify_asset_freshness(100.0, 4) == "stale"
        assert classify_asset_freshness(100.0, 3) == "stale"

    def test_very_stale_at_hard_abort(self):
        """age_days >= 7 → very_stale"""
        assert classify_asset_freshness(100.0, 7) == "very_stale"
        assert classify_asset_freshness(100.0, 30) == "very_stale"

    def test_custom_thresholds(self):
        """自定义阈值参数有效"""
        assert classify_asset_freshness(100.0, 5, stale_threshold_days=6, hard_abort_days=10) == "fresh"
        assert classify_asset_freshness(100.0, 7, stale_threshold_days=6, hard_abort_days=10) == "stale"
        assert classify_asset_freshness(100.0, 11, stale_threshold_days=6, hard_abort_days=10) == "very_stale"


# ============ 任务 3b：format_staleness_warning ============

class TestFormatStalenessWarning:
    def test_no_warning_below_threshold(self):
        """age_days < threshold → 空字符串"""
        assert format_staleness_warning("NDQ.AX", 2) == ""

    def test_no_warning_none_age(self):
        assert format_staleness_warning("NDQ.AX", None) == ""

    def test_warning_above_threshold(self):
        """age_days >= threshold → 包含 label 和天数的告警"""
        msg = format_staleness_warning("NDQ.AX 价格", 5)
        assert "NDQ.AX 价格" in msg
        assert "5 天" in msg
        assert "陈旧" in msg

    def test_warning_at_threshold(self):
        msg = format_staleness_warning("汇率", 3)
        assert "3 天" in msg

    def test_custom_threshold(self):
        """自定义 stale_threshold_days 参数有效"""
        # 默认 threshold=3，age=4 会告警；custom threshold=10，age=4 不告警
        assert format_staleness_warning("X", 4, stale_threshold_days=10) == ""


# ============ 任务 3c：portfolio_summary_text ============

class TestPortfolioSummaryText:
    def test_cash_only_no_holdings(self, tmp_path):
        """纯现金，无持仓时显示'无实仓持仓'"""
        pm = _make_pm(tmp_path, cash={"CNY": 20000.0}, holdings=[])
        text = portfolio_summary_text(pm, total_assets_cny=20000.0, current_prices={})
        assert "20,000" in text
        assert "当前无实仓持仓" in text

    def test_aud_cash_shown_when_positive(self, tmp_path):
        """AUD 余额 > 0 时显示"""
        pm = _make_pm(tmp_path, cash={"CNY": 10000.0, "AUD": 300.0}, holdings=[])
        text = portfolio_summary_text(pm, total_assets_cny=11500.0, current_prices={})
        assert "AUD" in text
        assert "300" in text

    def test_holding_with_price_shows_pnl(self, tmp_path):
        """有现价时显示浮盈信息"""
        holdings = [{
            "symbol": "NDQ.AX", "kind": "etf", "units": 100.0,
            "unit_label": "股", "avg_cost": 50.0, "cost_currency": "AUD",
            "display_name": "BetaShares Nasdaq 100",
        }]
        pm = _make_pm(tmp_path, cash={"CNY": 0.0}, holdings=holdings)
        text = portfolio_summary_text(pm, total_assets_cny=25000.0,
                                       current_prices={"NDQ.AX": 55.0})
        assert "浮盈" in text
        assert "10.00%" in text  # (55-50)/50 = 10%
        assert "BetaShares" in text

    def test_holding_without_price_shows_avg_only(self, tmp_path):
        """无现价时只显示持仓量和均价，不显示浮盈"""
        holdings = [{
            "symbol": "NDQ.AX", "kind": "etf", "units": 100.0,
            "unit_label": "股", "avg_cost": 50.0, "cost_currency": "AUD",
        }]
        pm = _make_pm(tmp_path, cash={"CNY": 0.0}, holdings=holdings)
        text = portfolio_summary_text(pm, total_assets_cny=25000.0, current_prices={})
        assert "浮盈" not in text
        assert "50.00" in text  # avg_cost 显示

    def test_tracking_only_excluded(self, tmp_path):
        """is_tracking_only 持仓不出现在 summary 里"""
        holdings = [
            {"symbol": "NDQ.AX", "kind": "etf", "units": 50.0, "cost_currency": "AUD"},
            {"symbol": "TSLA", "kind": "equity", "units": 0.0, "cost_currency": "USD",
             "is_tracking_only": True},
        ]
        pm = _make_pm(tmp_path, cash={"CNY": 1000.0}, holdings=holdings)
        text = portfolio_summary_text(pm, total_assets_cny=10000.0, current_prices={})
        # TSLA is_tracking_only=True 但 units=0，不出现在 real_holdings
        assert "TSLA" not in text

    def test_dry_powder_equals_cash(self, tmp_path):
        """dry_powder = cash_cny（无 buffer 减项后）"""
        pm = _make_pm(tmp_path, cash={"CNY": 30000.0}, holdings=[])
        text = portfolio_summary_text(pm, total_assets_cny=30000.0, current_prices={})
        assert "30,000" in text

    def test_risk_level_shown(self, tmp_path):
        """风险偏好显示正确"""
        pm = _make_pm(tmp_path)
        text = portfolio_summary_text(pm, total_assets_cny=50000.0, current_prices={})
        assert "Balanced" in text

    def test_concentration_not_zeroed_when_total_unknown(self, tmp_path):
        """total_assets_cny=NaN（上游某腿不可解析）时，集中度不得伪造成 0.0%。

        根因回归：单个持仓 current_price=NaN 污染 total → total=NaN → renderer 走
        `total>0=False` else 分支，把每个 holding 的集中度静默写成 0.0%。这会让
        Risk Officer 据假 0% 决策（关闭集中度风控）。修复后应输出可见降级标记，
        促人工复核而非沉默归零。
        """
        from openinvest.core.config import set_config_override
        set_config_override({"verdict": {"concentration_lens_enabled": True}})  # 测集中度计算→显式开 lens
        holdings = [
            {"symbol": "NDQ.AX", "kind": "etf", "units": 100.0, "unit_label": "股",
             "avg_cost": 50.0, "cost_currency": "AUD", "display_name": "Nasdaq100"},
            {"symbol": "510300.SS", "kind": "etf", "units": 1000.0, "unit_label": "份",
             "avg_cost": 4.0, "cost_currency": "CNY", "display_name": "沪深300"},
        ]
        pm = _make_pm(tmp_path, cash={"CNY": 0.0}, holdings=holdings)
        text = portfolio_summary_text(
            pm, total_assets_cny=float("nan"),
            current_prices={"NDQ.AX": 55.0, "510300.SS": 4.5},
        )
        # 不得出现伪造的 0.0% 集中度
        assert "集中度 0.0%" not in text
        # 必须有可见降级标记
        assert ("总资产不可用" in text) or ("暂不可计算" in text)

    def test_concentration_correct_when_one_holding_priced_other_missing(self, tmp_path):
        """传入已修正的合法 total（只含可解析腿），缺价 holding 只显示均价不被归零，
        有价 holding 显示正确非零集中度。
        """
        from openinvest.core.config import set_config_override
        set_config_override({"verdict": {"concentration_lens_enabled": True}})  # 测集中度计算→显式开 lens
        holdings = [
            # 黄金：1 单位 * 134 CNY = 134 CNY 市值
            {"symbol": "GOLD", "kind": "commodity", "units": 1.0, "unit_label": "克",
             "avg_cost": 100.0, "cost_currency": "CNY", "display_name": "黄金"},
            # 缺价 holding（current_prices 不含它）
            {"symbol": "510300.SS", "kind": "etf", "units": 1000.0, "unit_label": "份",
             "avg_cost": 4.0, "cost_currency": "CNY", "display_name": "沪深300"},
        ]
        pm = _make_pm(tmp_path, cash={"CNY": 100.0}, holdings=holdings)
        # 合法 total（已剔除缺价腿）：cash 100 + 黄金 134 = 234；黄金占比 134/234 ≈ 57.3%
        text = portfolio_summary_text(
            pm, total_assets_cny=234.0,
            current_prices={"GOLD": 134.0},  # 510300.SS 缺价
        )
        # 有价持仓显示正确非零集中度（不再被 NaN 静默归零）
        assert "集中度 57.3%" in text
        # 缺价持仓只显示均价行（无浮盈、无集中度数字）
        assert "510300.SS" in text
        # 缺价腿那一行不应出现伪造 0.0% 集中度
        assert "集中度 0.0%" not in text

    def test_concentration_hidden_when_lens_off(self, tmp_path):
        """集中度 lens 关闭(默认,ADR-020)时 portfolio_summary 完全不渲染集中度。
        这是 cron/session/Direct 共用的单一源 helper,关一处即三路径全关。"""
        holdings = [
            {"symbol": "GOLD", "kind": "commodity", "units": 1.0, "unit_label": "克",
             "avg_cost": 100.0, "cost_currency": "CNY", "display_name": "黄金"},
        ]
        pm = _make_pm(tmp_path, cash={"CNY": 100.0}, holdings=holdings)
        # 默认 config = lens OFF（#93/ADR-020），不显式 override
        text = portfolio_summary_text(
            pm, total_assets_cny=234.0, current_prices={"GOLD": 134.0},
        )
        assert "集中度" not in text, "lens OFF 时集中度不应出现在 portfolio_summary"
        assert "黄金" in text and "浮盈" in text  # 其余持仓信息仍在


# ============ 任务 3d：assemble_full_report ============

@dataclass
class _FakeReport:
    """模拟 CommitteeReport dataclass（只需要 cio_memo/quant_view/risk_view）"""
    cio_memo: str = "CIO 备忘内容"
    quant_view: str = "Quant 分析"
    risk_view: str = "Risk 分析"


class TestAssembleFullReport:
    def _make_committees(self, symbols):
        """构造虚假的委员会结果 dict"""
        return {
            sym: {
                "verdict": {
                    "verdict": "ACCUMULATE",
                    "confidence": 0.75,
                    "dominant_view": "Quant",
                    "alloc_cny": 5000,
                },
                "report": _FakeReport(
                    cio_memo=f"{sym} CIO memo",
                    quant_view=f"{sym} Quant",
                    risk_view=f"{sym} Risk",
                ),
            }
            for sym in symbols
        }

    def test_basic_report_structure(self):
        """报告包含日期、宏观、免责声明等关键结构"""
        report = assemble_full_report(
            today="2026-05-10",
            macro_view="宏观稳定",
            gold_snapshot_text="黄金 650.00 CNY/g",
            friction_report="摩擦成本 1.5%",
            target_assets=[{"symbol": "NDQ.AX", "display_name": "BetaShares Nasdaq 100"}],
            asset_committees=self._make_committees(["NDQ.AX"]),
            skipped_assets=set(),
            total_assets_cny=100000.0,
            final_decision_gemini="我同意，继续持有",
        )
        assert "2026-05-10" in report
        assert "宏观稳定" in report
        assert "BetaShares Nasdaq 100" in report
        assert "免责声明" in report
        assert "100,000" in report  # 总资产

    def test_skipped_assets_not_in_report(self):
        """被跳过的资产不出现在报告正文委员会区块"""
        report = assemble_full_report(
            today="2026-05-10",
            macro_view="宏观分析",
            gold_snapshot_text="黄金",
            friction_report="摩擦",
            target_assets=[
                {"symbol": "NDQ.AX", "display_name": "NDQ"},
                {"symbol": "GC=F", "display_name": "黄金"},
            ],
            asset_committees=self._make_committees(["NDQ.AX"]),  # GC=F 没有委员会结果
            skipped_assets={"GC=F"},
            total_assets_cny=50000.0,
            final_decision_gemini="同意",
        )
        # 跳过的 GC=F 不在报告里；NDQ.AX 委员会结果应在报告中
        assert "NDQ.AX" in report
        assert "NDQ.AX CIO memo" in report  # _FakeReport 用 symbol 生成 cio_memo

    def test_path_reference_rendered_data_lines_only(self):
        """path_reference 的 "- " 数据行渲染进邮件；LLM 标题行和 TRIM 指令行不渲染"""
        committees = self._make_committees(["NDQ.AX"])
        committees["NDQ.AX"]["path_reference"] = (
            "# 路径参考（regime=uptrend 历史 forward 路径分布）：\n"
            "- 现价: ¥60.00\n"
            "- 30d (n=1423): 跌破现价概率 43%、中位 +1.4%\n"
            "- 90d 路径形状: 先跌后涨 49% / 直接涨 25%\n"
            "（若要 TRIM，REENTRY_PRICE 必须低于现价）"
        )
        report = assemble_full_report(
            today="2026-05-10",
            macro_view="",
            gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX"}],
            asset_committees=committees,
            skipped_assets=set(),
            total_assets_cny=0.0,
            final_decision_gemini="",
        )
        assert "路径概率" in report
        assert "跌破现价概率 43%" in report
        assert "先跌后涨 49%" in report
        assert "# 路径参考" not in report          # LLM 标题行掐头
        assert "REENTRY_PRICE 必须低于现价" not in report  # TRIM 指令行去尾

    def test_plain_summary_rendered_from_profile(self):
        """一句话人话摘要：verdict + 30d 路径分布确定性生成"""
        committees = self._make_committees(["NDQ.AX"])
        committees["NDQ.AX"]["verdict"]["verdict"] = "HOLD"
        committees["NDQ.AX"]["path_profile"] = {
            "windows": {"30d": {"p_below": 0.42, "downside_pct": -3.0,
                                "median_pct": 1.1}},
        }
        report = assemble_full_report(
            today="2026-05-10", macro_view="", gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX"}],
            asset_committees=committees,
            skipped_assets=set(), total_assets_cny=0.0,
            final_decision_gemini="",
        )
        assert "一句话" in report
        assert "继续持有，不买也不卖" in report
        assert "更便宜的概率约 42%" in report
        assert "-3.0%" in report

    def test_plain_summary_without_profile_still_has_action(self):
        """无 path_profile（概率表不可用）→ 人话行只有动作没有概率尾巴"""
        report = assemble_full_report(
            today="2026-05-10", macro_view="", gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX"}],
            asset_committees=self._make_committees(["NDQ.AX"]),  # ACCUMULATE 5000
            skipped_assets=set(), total_assets_cny=0.0,
            final_decision_gemini="",
        )
        assert "一句话" in report
        assert "建议小额加仓 ¥5,000" in report
        assert "更便宜的概率" not in report

    def test_analyst_views_rendered_as_md_cards(self):
        """analyst 原文走 .analyst md_in_html 卡片（正常排版），
        不再用 <details>，也不塞进 ``` 代码块（会导致 ** 原文泄露、灰块难读）。"""
        report = assemble_full_report(
            today="2026-05-10", macro_view="", gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX"}],
            asset_committees=self._make_committees(["NDQ.AX"]),
            skipped_assets=set(), total_assets_cny=0.0,
            final_decision_gemini="",
        )
        assert "<details>" not in report
        # 卡片容器 + markdown="1" 让 notifier 的 md_in_html 解析内部 markdown
        assert 'class="analyst"' in report
        assert 'markdown="1"' in report
        # 分析师原文不再被 ``` 围栏包裹（围栏只留给等宽数据块）
        assert "```\nNDQ.AX Quant" not in report
        assert "NDQ.AX Quant" in report
        assert "NDQ.AX Risk" in report
        assert "分析师意见" in report

    def test_chat_render_target_has_no_html(self):
        """render_target="chat"：Discord/Weixin/QQ 等聊天平台不解析原生 HTML，
        <div class="analyst" markdown="1"> 会以字面文本泄露给用户（2026-07-14
        Hermes cron 渲染事故）。chat 变体同样内容，不裹 HTML。"""
        report = assemble_full_report(
            today="2026-05-10", macro_view="", gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX"}],
            asset_committees=self._make_committees(["NDQ.AX"]),
            skipped_assets=set(), total_assets_cny=0.0,
            final_decision_gemini="",
            render_target="chat",
        )
        assert "<div" not in report
        assert "</div>" not in report
        assert 'markdown="1"' not in report
        # 内容本身不丢——只是不裹 HTML
        assert "NDQ.AX Quant" in report
        assert "NDQ.AX Risk" in report
        assert "分析师意见" in report

    def test_email_render_target_is_default(self):
        """不传 render_target 时行为不变（向后兼容——现存 email 调用方零改动）。"""
        report = assemble_full_report(
            today="2026-05-10", macro_view="", gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX"}],
            asset_committees=self._make_committees(["NDQ.AX"]),
            skipped_assets=set(), total_assets_cny=0.0,
            final_decision_gemini="",
        )
        assert 'class="analyst"' in report

    def test_chat_render_target_has_tldr_at_top(self):
        """chat 变体标题正下方就是逐资产速览（emoji 徽章 + 裁决 + 置信度 +
        建议金额）——Discord 2000 字符/条会把长报告切成多条消息，第一条必须
        自带结论，不能让用户翻到最后一条才看到 verdict（2026-07-14 渲染优化）。"""
        report = assemble_full_report(
            today="2026-05-10", macro_view="宏观正文", gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX", "display_name": "纳指ETF"}],
            asset_committees=self._make_committees(["NDQ.AX"]),
            skipped_assets=set(), total_assets_cny=0.0,
            final_decision_gemini="",
            render_target="chat",
        )
        title_pos = report.index("投资委员会日报")
        tldr_pos = report.index("今日速览")
        macro_pos = report.index("宏观正文")
        assert title_pos < tldr_pos < macro_pos  # 速览夹在标题和正文之间
        assert "🟩" in report  # ACCUMULATE 徽章（_make_committees fixture 用的裁决）
        assert "纳指ETF" in report and "NDQ.AX" in report
        assert "置信度 75%" in report

    def test_email_render_target_has_no_tldr(self):
        """email 变体不加速览块——邮件本来就一次性看全文，不需要重复摘要。"""
        report = assemble_full_report(
            today="2026-05-10", macro_view="", gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX"}],
            asset_committees=self._make_committees(["NDQ.AX"]),
            skipped_assets=set(), total_assets_cny=0.0,
            final_decision_gemini="",
            render_target="email",
        )
        assert "今日速览" not in report

    def test_glossary_rendered(self):
        """术语表固定渲染在报告尾部（小白查表，专家跳过）"""
        report = assemble_full_report(
            today="2026-05-10", macro_view="", gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX"}],
            asset_committees=self._make_committees(["NDQ.AX"]),
            skipped_assets=set(), total_assets_cny=0.0,
            final_decision_gemini="",
        )
        assert "术语表" in report
        assert "跌破现价概率" in report

    def test_path_reference_absent_no_section(self):
        """没有 path_reference（如概率表不可用）→ 不出现路径概率空段落"""
        report = assemble_full_report(
            today="2026-05-10",
            macro_view="",
            gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX"}],
            asset_committees=self._make_committees(["NDQ.AX"]),
            skipped_assets=set(),
            total_assets_cny=0.0,
            final_decision_gemini="",
        )
        assert "路径概率" not in report

    def test_gemini_opinion_included(self):
        """Gemini 第二意见包含在报告里"""
        opinion = "Gemini 认为：应该减仓"
        report = assemble_full_report(
            today="2026-05-10",
            macro_view="",
            gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX"}],
            asset_committees=self._make_committees(["NDQ.AX"]),
            skipped_assets=set(),
            total_assets_cny=0.0,
            final_decision_gemini=opinion,
        )
        assert opinion in report

    def test_all_skipped_shows_placeholder(self):
        """所有资产被跳过时，报告有占位文字而非崩溃"""
        report = assemble_full_report(
            today="2026-05-10",
            macro_view="无数据",
            gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "NDQ.AX"}, {"symbol": "GC=F"}],
            asset_committees={},  # 空
            skipped_assets={"NDQ.AX", "GC=F"},
            total_assets_cny=0.0,
            final_decision_gemini="无法判断",
        )
        # 不应崩溃；应有占位文字
        assert "2026-05-10" in report

    # ---------- TRIM 路径化展示三分支 ----------

    def _report_with_verdict(self, verdict: dict) -> str:
        return assemble_full_report(
            today="2026-05-10",
            macro_view="",
            gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "GC=F", "display_name": "黄金"}],
            asset_committees={
                "GC=F": {"verdict": verdict, "report": _FakeReport(cio_memo="memo")}
            },
            skipped_assets=set(),
            total_assets_cny=100000.0,
            final_decision_gemini="",
        )

    def test_trim_shows_reentry_plan(self):
        """verdict=TRIM → 展示减仓路径 + 买回点"""
        report = self._report_with_verdict({
            "verdict": "TRIM", "confidence": 0.8, "dominant_view": "risk",
            "alloc_cny": -5000, "reentry_price": 950.0,
            "reentry_condition": "跌至 ¥950 且 RSI<40", "expected_path": "30d 55% 跌破现价",
        })
        assert "减仓路径" in report
        assert "¥950" in report

    def test_sanity5_downgrade_shows_rejected(self):
        """Sanity5 降级（买回点不低于现价）→ 展示减仓被否"""
        report = self._report_with_verdict({
            "verdict": "HOLD", "confidence": 0.6, "dominant_view": "quant",
            "alloc_cny": 0, "_original_verdict": "TRIM",
            "_sanity5_reason": "reentry_not_below_current",
        })
        assert "减仓被否" in report

    def test_concentration_lens_off_downgrade_shows_lens_note(self):
        """集中度 lens 关 → 拦下 concentration-TRIM：措辞明确"减仓未执行 / lens 已关"
        + CIO 原始减仓金额留痕，非"减仓计划"，也非旧的"兜底充足 / 系统路径预期"口径；
        CIO 的路径预期仍供对照。"""
        report = self._report_with_verdict({
            "verdict": "HOLD", "confidence": 0.40, "dominant_view": "risk",
            "alloc_cny": 0, "_original_verdict": "TRIM",
            "_original_trim_reason": "concentration",
            "_concentration_lens": "disabled",
            "_original_alloc": -20000,
            "reentry_price": 938.0,
            "expected_path": "range_bound 历史30天跌破现价概率100%，中位→¥947、20分位→¥938",
        })
        assert "减仓未执行" in report and "集中度 lens 已关" in report
        assert "¥-20000" in report  # CIO 原始减仓金额留痕
        assert "自行权衡" in report
        assert "¥947" in report and "¥938" in report
        # 措辞区分：不是减仓计划（TRIM 执行），也不是 Sanity5 的"减仓被否"
        assert "减仓路径" not in report
        assert "减仓被否" not in report
        # 已移除的 solvency 自动兜底口径不应再出现
        assert "兜底充足" not in report
        assert "系统路径预期" not in report


class TestBuildTldrBlock:
    """build_tldr_block()：纯格式化，逐资产一行摘要"""

    def _committees(self, verdict="HOLD", confidence=0.65, alloc_cny=0):
        return {
            "GC=F": {"verdict": {
                "verdict": verdict, "confidence": confidence,
                "dominant_view": "risk", "alloc_cny": alloc_cny,
            }},
        }

    def test_empty_assets_returns_empty_string(self):
        assert build_tldr_block([], {}) == ""

    def test_each_verdict_gets_distinct_emoji(self):
        for verdict, emoji in [
            ("BUY", "🟢"), ("ACCUMULATE", "🟩"), ("HOLD", "🟡"),
            ("TRIM", "🟠"), ("SELL", "🔴"),
        ]:
            block = build_tldr_block(
                [{"symbol": "GC=F"}], self._committees(verdict=verdict),
            )
            assert emoji in block, f"{verdict} 应该带 {emoji}"

    def test_unknown_verdict_falls_back_to_neutral_emoji(self):
        block = build_tldr_block(
            [{"symbol": "GC=F"}], self._committees(verdict="WEIRD"),
        )
        assert "⚪" in block

    def test_line_includes_confidence_and_alloc(self):
        block = build_tldr_block(
            [{"symbol": "GC=F", "display_name": "伦敦金"}],
            self._committees(verdict="TRIM", confidence=0.8, alloc_cny=3000),
        )
        assert "伦敦金" in block and "GC=F" in block
        assert "置信度 80%" in block
        assert "¥3,000" in block


# ============ 翻译官（人话解读 LLM 版） ============

class TestTranslator:
    def test_parse_well_formed_output(self):
        from openinvest.jobs.daily_report_builder import parse_translator_output
        raw = (
            "@@GC=F\n继续持有。历史上 408 个类似日子里……\n第二句。\n"
            "@@0700.HK\n建议小额加仓 ¥2,500。\n"
        )
        out = parse_translator_output(raw)
        assert set(out) == {"GC=F", "0700.HK"}
        assert "408 个类似日子" in out["GC=F"]
        assert "第二句" in out["GC=F"]

    def test_parse_garbage_returns_empty(self):
        from openinvest.jobs.daily_report_builder import parse_translator_output
        assert parse_translator_output("我不会遵守格式，直接说：持有吧") == {}
        assert parse_translator_output("") == {}
        assert parse_translator_output("@@GC=F\n   \n") == {}  # 有头无正文

    def test_prompt_contains_data_and_defense_note(self):
        from openinvest.jobs.daily_report_builder import build_translator_prompt
        prompt = build_translator_prompt([{
            "symbol": "GC=F", "display_name": "伦敦金",
            "verdict_line": "HOLD，置信度 0.65，建议金额 ¥0",
            "defense_note": "CIO 原始结论 TRIM（建议金额 ¥-30000），想因集中度减仓被拦",
            "path_lines": ["- 30d: 跌破现价概率 42%"],
            "cio_memo": "VERDICT: HOLD",
        }])
        assert "GC=F" in prompt and "伦敦金" in prompt
        assert "跌破现价概率 42%" in prompt
        assert "集中度减仓被拦" in prompt

    def test_assemble_prefers_translator_over_deterministic(self):
        committees = {
            "GC=F": {
                "verdict": {"verdict": "HOLD", "confidence": 0.65,
                            "dominant_view": "risk", "alloc_cny": 0},
                "report": _FakeReport(cio_memo="m", quant_view="q", risk_view="r"),
                "path_profile": {"windows": {"30d": {
                    "p_below": 0.42, "downside_pct": -3.0, "median_pct": 1.1}}},
            },
        }
        report = assemble_full_report(
            today="2026-06-12", macro_view="", gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "GC=F", "display_name": "伦敦金"}],
            asset_committees=committees,
            skipped_assets=set(), total_assets_cny=0.0,
            final_decision_gemini="",
            plain_summaries={"GC=F": "翻译官说：拿住别动，系统其实在担心你篮子太满。"},
        )
        assert "人话解读" in report
        assert "篮子太满" in report
        assert "一句话" not in report   # 翻译官命中时确定性版不再渲染

    def test_assemble_falls_back_per_asset(self):
        """翻译官只给了部分资产 → 缺的资产回落确定性一句话"""
        committees = {
            "GC=F": {
                "verdict": {"verdict": "HOLD", "confidence": 0.65,
                            "dominant_view": "risk", "alloc_cny": 0},
                "report": _FakeReport(cio_memo="m", quant_view="q", risk_view="r"),
            },
        }
        report = assemble_full_report(
            today="2026-06-12", macro_view="", gold_snapshot_text="",
            friction_report="",
            target_assets=[{"symbol": "GC=F"}],
            asset_committees=committees,
            skipped_assets=set(), total_assets_cny=0.0,
            final_decision_gemini="",
            plain_summaries={"OTHER": "无关资产"},
        )
        assert "一句话" in report
        assert "继续持有，不买也不卖" in report
