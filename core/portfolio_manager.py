import json
import os
from dataclasses import dataclass
from datetime import datetime

PROFILE_PATH = "user_profile.json"


@dataclass
class UserStatus:
    cash_cny: float
    cash_aud: float
    disposable_for_invest: float
    risk_level: str
    portfolio_value: float
    is_payday: bool


def _load_profile():
    if not os.path.exists(PROFILE_PATH):
        raise FileNotFoundError(f"找不到 {PROFILE_PATH}，请先创建配置。")
    with open(PROFILE_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


class PortfolioManager:
    def __init__(self):
        self.profile = _load_profile()

    def _save_profile(self):
        with open(PROFILE_PATH, 'w', encoding='utf-8') as f:
            json.dump(self.profile, f, indent=2, ensure_ascii=False)

    def process_income(self):
        """
        检查是否需要'发工资'。
        逻辑：简单的按月检测。实际生产中可以根据具体日期。
        """
        last_run = datetime.strptime(self.profile.get("last_run_date", "2025-12-20"), "%Y-%m-%d")
        today = datetime.now()

        income_added = False
        # 如果月份不同，且今天是1号以后（简化逻辑，实际可按需调整）
        if today.month != last_run.month:
            income = self.profile["monthly_income_cny"]
            expense = self.profile["monthly_expenses_cny"]
            net_income = income - expense

            self.profile["current_assets"]["cash_cny"] += net_income
            print(f"💰 [Payday] 检测到新月份，已自动存入净收入: ¥{net_income}")
            income_added = True

        # 更新运行时间
        self.profile["last_run_date"] = today.strftime("%Y-%m-%d")
        self._save_profile()
        return income_added

    def get_user_status(self, current_stock_price: float, exchange_rate: float) -> UserStatus:
        assets = self.profile["current_assets"]
        strategy = self.profile["investment_strategy"]

        # 1. 计算当前持仓市值 (转为 CNY)
        stock_val_aud = assets["ndq_shares"] * current_stock_price
        stock_val_cny = stock_val_aud * exchange_rate  # 粗略估算

        total_portfolio = stock_val_cny + assets["cash_cny"] + (assets["aud_cash"] * exchange_rate)

        # 2. 计算本期“可支配投资资金”
        # 逻辑：当前现金 - 必须留的周转金 (exchange_buffer)
        available_cash = assets["cash_cny"] - self.profile["exchange_buffer_cny"]
        if available_cash < 0: available_cash = 0

        # 3. 基础投资额度 (Base Cap)
        # 即使非常有钱，单次也不超过设置的上限，防止梭哈风险
        invest_cap = strategy["max_single_invest_cny"]
        disposable = min(available_cash, invest_cap)

        return UserStatus(
            cash_cny=assets["cash_cny"],
            cash_aud=assets.get("aud_cash", 0.0),
            disposable_for_invest=disposable,
            risk_level=self.profile["risk_tolerance"],
            portfolio_value=total_portfolio,
            is_payday=False  # 由 process_income 外部控制打印
        )

    def update_after_invest(self, invest_cny: float):
        self.profile["current_assets"]["cash_cny"] -= invest_cny
        self._save_profile()