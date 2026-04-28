"""持仓与策略门面 - 基于 MemoryStore（OpenClaw 风格 markdown 持久化）

职责重新切分（相比旧版）：
- 只负责"读 memory + 计算用户状态 + 记录交易"
- 工资入账（process_income）已迁出 → jobs/payday_check.py
- 文件 IO 统一走 MemoryStore（带文件锁）
- 不再直接持有 user_profile.json
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, List, Optional

from core.memory_store import MemoryStore, _DocTx


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

        # 多资产策略下从 target_assets 各 cap 取 max 当兜底（每资产独立 cap，
        # 调用方按当前操作的资产单独取更准）。旧字段 strategy.max_single_invest_cny /
        # strategy.target_asset 已废弃，保留 .get() 仅为单资产旧 memory 的兼容兜底。
        target_assets = list(self.strategy.get("target_assets", []) or [])
        if target_assets:
            max_single = max(
                float(t.get("max_single_invest_cny", 0) or 0) for t in target_assets
            ) or 10000.0
            primary_asset = str(target_assets[0].get("symbol", "NDQ.AX"))
        else:
            max_single = float(self.strategy.get("max_single_invest_cny", 10000))
            primary_asset = str(self.strategy.get("target_asset", "NDQ.AX"))

        # 持仓市值（粗算 AUD->CNY，黄金估值由调用方负责）
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
            target_asset=primary_asset,
            max_single_invest_cny=max_single,
            user_name=str(self.user.get("display_name", "Anonymous")),
        )

    def get_processed_emails(self) -> List[str]:
        return list(self.store.state_get("processed_emails", []))

    # ---------- 写 ----------
    #
    # 所有 portfolio 修改都走 store.transaction("portfolio") 单锁闭包，避免
    # TOCTOU/Lost Update：之前是先 update_fields 再 _refresh_portfolio_body
    # 两次独立锁，中间另一进程能插入造成丢更新。
    #
    # 外部调用方（NapCat bot 等）通过 with_portfolio_tx() 也能拿到同样的安全
    # 闭包，退出 with 自动重渲染 body + atomic 写入。

    @contextmanager
    def with_portfolio_tx(self) -> Iterator[_DocTx]:
        """对外暴露的 portfolio RMW 闭包。

        用法：
            with pm.with_portfolio_tx() as p:
                p["cash_cny"] = float(p.get("cash_cny", 0)) + amount
                # p["ndq_shares"] = ...
            # 退出 with 自动：1) 重渲染 body 2) atomic write
            pm._reload()  # 让 pm.portfolio 视图也跟上

        想保证多字段联动写不会被并发进程踩，所有改动必须放在同一个 with 里——
        分开两次 with 又退化成 TOCTOU 窗口。
        """
        with self.store.transaction("portfolio") as p:
            yield p
            p.set_body(_render_portfolio_body(p))

    def update_after_invest(self, invest_cny: float) -> None:
        """daily_report 在用户实际买入后调用（目前是手动操作，先留接口）"""
        with self.with_portfolio_tx() as p:
            p["cash_cny"] = float(p.get("cash_cny", 0)) - invest_cny
        self._reload()

    def record_external_trade(self, trade: dict) -> None:
        """从 CommSec 邮件解析出的成交回报 → 更新 portfolio + 历史 + 已处理邮件"""
        symbol = str(trade.get("symbol", ""))
        units = float(trade.get("units", 0))
        action = str(trade.get("action", "")).lower()
        amount = float(trade.get("total_amount", 0))
        currency = str(trade.get("currency", "AUD"))
        is_ndq = "NDQ" in symbol

        # portfolio 改动 + body 重渲染在单一锁内完成
        with self.with_portfolio_tx() as p:
            ndq_shares = float(p.get("ndq_shares", 0))
            aud_cash = float(p.get("aud_cash", 0))
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
            p["ndq_shares"] = ndq_shares
            p["aud_cash"] = aud_cash

        # 历史 jsonl 是 append-only 独立文件，自带锁，不需要在 portfolio 锁内
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
        with self.with_portfolio_tx() as p:
            p["cash_cny"] = float(p.get("cash_cny", 0)) + net_income_cny
            new_cash = float(p["cash_cny"])

        # user.md 是另一份文件，自己一次单锁 update 即可
        self.store.update_fields("user", last_payday=payday_label)
        self._reload()
        print(f"💰 [Payday {payday_label}] 净收入 ¥{net_income_cny:,.0f} 已入账，"
              f"现金余额 ¥{new_cash:,.2f}")

    # ---------- 内部 ----------

    def _reload(self) -> None:
        """写入后重新读，保证下一次访问看到最新数据"""
        self.user = self.store.read("user")  # type: ignore[assignment]
        self.strategy = self.store.read("strategy")  # type: ignore[assignment]
        self.portfolio = self.store.read("portfolio")  # type: ignore[assignment]


def _render_portfolio_body(p) -> str:
    """根据 portfolio frontmatter 重渲染 body。

    给 transaction 闭包用：在锁内拿着 _DocTx 直接算出最新 body，避免之前
    "update_fields → 释放锁 → _refresh_portfolio_body 再拿锁" 的两段式（中
    间会被插入）。

    p 可以是 _DocTx 或 MemoryDoc，只要支持 .get(key, default) 即可。
    """
    cash_cny = float(p.get("cash_cny", 0))
    aud_cash = float(p.get("aud_cash", 0))
    ndq_shares = float(p.get("ndq_shares", 0))
    gold_grams = float(p.get("gold_grams", 0) or 0)
    gold_avg_cost = float(p.get("gold_avg_cost_cny_per_gram", 0) or 0)
    gold_line = (
        f"- **黄金持仓 (浙商积存金)**: {gold_grams:.4f} 克"
        + (f"，均价 ¥{gold_avg_cost:.2f}/克" if gold_avg_cost else "")
    ) if gold_grams else "- **黄金持仓 (浙商积存金)**: 0"
    return f"""# 当前持仓

- **CNY 现金**: ¥{cash_cny:,.2f}
- **AUD 现金**: ${aud_cash:,.2f}
- **NDQ.AX 持仓**: {ndq_shares} 股
{gold_line}

## 说明

此文件由 daily_report / commsec_sync / payday_check / napcat_bot 自动更新。
不要手动编辑——如需调整，请走 jobs/manual_adjust.py 或 NapCat /cmd 命令。
"""
