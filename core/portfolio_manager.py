"""持仓与策略门面 - 基于 MemoryStore（OpenClaw 风格 markdown 持久化）

职责重新切分（相比旧版）：
- 只负责"读 memory + 计算用户状态 + 记录交易"
- 工资入账（process_income）已迁出 → jobs/payday_check.py
- 文件 IO 统一走 MemoryStore（带文件锁）
- 不再直接持有 user_profile.json
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from core.memory_store import MemoryStore


@dataclass
class UserStatus:
    cash_cny: float
    cash_aud: float
    disposable_for_invest: float
    risk_level: str
    portfolio_value: float
    target_asset: str
    max_single_invest_cny: float
    user_name: str
    user_email: Optional[str] = None  # 预留，目前从 .env 拿


class PortfolioManager:
    """所有数据通过 MemoryStore 读写；调用方不用关心文件布局。"""

    def __init__(self, store: Optional[MemoryStore] = None):
        self.store = store or MemoryStore()

        user_doc = self.store.read("user")
        strategy_doc = self.store.read("strategy")
        portfolio_doc = self.store.read("portfolio")

        if not (user_doc and strategy_doc and portfolio_doc):
            raise FileNotFoundError(
                "memory/user.md / strategy.md / portfolio.md 缺失。"
                "请先跑 `python scripts/migrate_profile.py` 迁移旧的 user_profile.json"
            )

        self.user = user_doc
        self.strategy = strategy_doc
        self.portfolio = portfolio_doc

    # ---------- 读 ----------

    def get_user_status(self, current_stock_price: float, exchange_rate: float) -> UserStatus:
        cash_cny = float(self.portfolio.get("cash_cny", 0))
        aud_cash = float(self.portfolio.get("aud_cash", 0))
        ndq_shares = float(self.portfolio.get("ndq_shares", 0))
        exchange_buffer = float(self.user.get("exchange_buffer_cny", 0))
        max_single = float(self.strategy.get("max_single_invest_cny", 10000))
        target_asset = str(self.strategy.get("target_asset", "NDQ.AX"))

        # 持仓市值（粗算 AUD->CNY）
        stock_val_cny = ndq_shares * current_stock_price * exchange_rate
        total_portfolio = stock_val_cny + cash_cny + aud_cash * exchange_rate

        # 本期可投资金 = 现金 - 周转金，封顶 max_single
        available = max(0.0, cash_cny - exchange_buffer)
        disposable = min(available, max_single)

        return UserStatus(
            cash_cny=cash_cny,
            cash_aud=aud_cash,
            disposable_for_invest=disposable,
            risk_level=str(self.user.get("risk_tolerance", "Balanced")),
            portfolio_value=total_portfolio,
            target_asset=target_asset,
            max_single_invest_cny=max_single,
            user_name=str(self.user.get("display_name", "Anonymous")),
        )

    def get_processed_emails(self) -> List[str]:
        return list(self.store.state_get("processed_emails", []))

    # ---------- 写 ----------

    def update_after_invest(self, invest_cny: float) -> None:
        """daily_report 在用户实际买入后调用（目前是手动操作，先留接口）"""
        new_cash = float(self.portfolio.get("cash_cny", 0)) - invest_cny
        self.store.update_fields("portfolio", cash_cny=new_cash)
        self._refresh_portfolio_body()
        self._reload()

    def record_external_trade(self, trade: dict) -> None:
        """从 CommSec 邮件解析出的成交回报 → 更新 portfolio + 历史 + 已处理邮件"""
        symbol = str(trade.get("symbol", ""))
        units = float(trade.get("units", 0))
        action = str(trade.get("action", "")).lower()
        amount = float(trade.get("total_amount", 0))
        currency = str(trade.get("currency", "AUD"))

        is_ndq = "NDQ" in symbol
        ndq_shares = float(self.portfolio.get("ndq_shares", 0))
        aud_cash = float(self.portfolio.get("aud_cash", 0))

        if action == "bought":
            if is_ndq:
                ndq_shares += units
            if currency == "AUD":
                aud_cash -= amount
        elif action == "sold":
            if is_ndq:
                ndq_shares = max(0.0, ndq_shares - units)
            if currency == "AUD":
                aud_cash += amount

        self.store.update_fields(
            "portfolio",
            ndq_shares=ndq_shares,
            aud_cash=aud_cash,
        )
        self._refresh_portfolio_body()
        self.store.append_history(trade)

        email_id = trade.get("email_id")
        if email_id:
            processed = self.get_processed_emails()
            if email_id not in processed:
                processed.append(email_id)
                self.store.state_set("processed_emails", processed)

        self._reload()
        print(
            f"💾 已记录外部交易: {action} {units} {symbol} "
            f"(成本: ${amount:.2f} {currency})"
        )

    def add_income(self, net_income_cny: float, payday_label: str) -> None:
        """payday_check job 调用 - 把月度净收入加进 cash_cny，并更新 last_payday"""
        new_cash = float(self.portfolio.get("cash_cny", 0)) + net_income_cny
        self.store.update_fields("portfolio", cash_cny=new_cash)
        self._refresh_portfolio_body()

        self.store.update_fields("user", last_payday=payday_label)
        self._reload()
        print(f"💰 [Payday {payday_label}] 净收入 ¥{net_income_cny:,.0f} 已入账，"
              f"现金余额 ¥{new_cash:,.2f}")

    # ---------- 内部 ----------

    def _refresh_portfolio_body(self) -> None:
        """用最新 frontmatter 数据重写 portfolio.md 的 body（保持自然语言部分新鲜）"""
        doc = self.store.read("portfolio")
        if doc is None:
            return
        cash_cny = float(doc.get("cash_cny", 0))
        aud_cash = float(doc.get("aud_cash", 0))
        ndq_shares = float(doc.get("ndq_shares", 0))
        body = f"""# 当前持仓

- **CNY 现金**: ¥{cash_cny:,.2f}
- **AUD 现金**: ${aud_cash:,.2f}
- **NDQ.AX 持仓**: {ndq_shares} 股

## 说明

此文件由 daily_report / commsec_sync / payday_check 三个 job 自动更新。
不要手动编辑——如需调整，请走 jobs/manual_adjust.py。
"""
        # 保留 frontmatter 业务字段
        meta = {k: v for k, v in doc.metadata.items()
                if k not in {"name", "type", "updated"}}
        self.store.write("portfolio", "state", meta, body)

    def _reload(self) -> None:
        """写入后重新读，保证下一次访问看到最新数据"""
        self.user = self.store.read("user")  # type: ignore[assignment]
        self.strategy = self.store.read("strategy")  # type: ignore[assignment]
        self.portfolio = self.store.read("portfolio")  # type: ignore[assignment]
