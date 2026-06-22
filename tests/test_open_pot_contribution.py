"""open-pot 月度补充模型（ADR-018 future work）：wealth_context 加 monthly_contribution_cny

让 WealthContextOfficer 把 portfolio cash 当「开口池」（每月从工资/外部补充 ¥X）而非
封闭快照：低现金更不算流动性风险、30% 现金目标软化（不必为达标囤现金）。
**铁律不变**：加仓上限仍 = 当前 portfolio cash（月度补充是未来流量，不是当下可投）。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.wealth_context_officer import PROMPT_WEALTH_CONTEXT_OFFICER
from connectors.web_api.models import WealthContextRequest


class TestWealthContextRequestModel:
    """PUT body 模型接受 monthly_contribution_cny（≥0，可选）"""

    def test_accepts_monthly_contribution(self):
        req = WealthContextRequest(monthly_contribution_cny=10000)
        assert req.monthly_contribution_cny == 10000

    def test_rejects_negative(self):
        with pytest.raises(ValidationError):
            WealthContextRequest(monthly_contribution_cny=-1)

    def test_optional_defaults_none(self):
        req = WealthContextRequest(emergency_buffer_cny=4_000_000)
        assert req.monthly_contribution_cny is None


class TestPromptSentinel:
    """SENTINEL：开口池指引 + 铁律必须留在 prompt 里（防 drift 掉，feedback_invest_drift_defense）"""

    def test_prompt_documents_monthly_contribution_field(self):
        assert "monthly_contribution_cny" in PROMPT_WEALTH_CONTEXT_OFFICER

    def test_prompt_has_monthly_output_field(self):
        assert "MONTHLY_CONTRIBUTION_CNY" in PROMPT_WEALTH_CONTEXT_OFFICER

    def test_prompt_keeps_investable_iron_law(self):
        """开口池软化现金目标，但加仓上限仍 = portfolio cash（铁律不能被开口池冲掉）"""
        assert "INVESTABLE_CASH_CNY" in PROMPT_WEALTH_CONTEXT_OFFICER
