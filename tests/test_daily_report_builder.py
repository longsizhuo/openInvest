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

from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
from jobs.daily_report_builder import (
    assemble_full_report,
    classify_asset_freshness,
    format_staleness_warning,
    portfolio_summary_text,
)


# ============ Fixture 辅助 ============

def _make_pm(tmp_path, cash=None, holdings=None, exchange_buffer_cny=0.0) -> PortfolioManager:
    """创建一个带完整 memory 的 PortfolioManager（v2 结构）"""
    store = MemoryStore(tmp_path / "memory")
    cash = cash if cash is not None else {"CNY": 50000.0, "AUD": 500.0}
    holdings = holdings if holdings is not None else []

    store.write("user", "user", {
        "display_name": "TestUser",
        "risk_tolerance": "Balanced",
        "exchange_buffer_cny": exchange_buffer_cny,
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

    def test_exchange_buffer_subtracted(self, tmp_path):
        """dry_powder = cash_cny - exchange_buffer_cny"""
        pm = _make_pm(tmp_path, cash={"CNY": 30000.0}, holdings=[],
                       exchange_buffer_cny=10000.0)
        text = portfolio_summary_text(pm, total_assets_cny=30000.0, current_prices={})
        assert "20,000" in text  # dry_powder = 30000 - 10000

    def test_risk_level_shown(self, tmp_path):
        """风险偏好显示正确"""
        pm = _make_pm(tmp_path)
        text = portfolio_summary_text(pm, total_assets_cny=50000.0, current_prices={})
        assert "Balanced" in text


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

    def test_sanity4_downgrade_shows_system_path_expectation(self):
        """Sanity4 降级（concentration）→ 展示"系统路径预期 vs 持有决定"，措辞非减仓计划"""
        report = self._report_with_verdict({
            "verdict": "HOLD", "confidence": 0.75, "dominant_view": "risk",
            "alloc_cny": -20000, "_original_verdict": "TRIM",
            "_original_trim_reason": "concentration",
            "reentry_price": 938.0,
            "expected_path": "range_bound 历史30天跌破现价概率100%，中位→¥947、20分位→¥938",
        })
        assert "系统路径预期" in report
        assert "自行权衡" in report
        assert "¥947" in report and "¥938" in report
        # 措辞区分：不是减仓计划
        assert "减仓路径" not in report
        assert "减仓被否" not in report
