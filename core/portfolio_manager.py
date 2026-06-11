"""持仓与策略门面 - 基于 MemoryStore（OpenClaw 风格 markdown 持久化）

v1 → v2 重构（2026-05-06）：
- 持仓从「扁平字段」(cash_cny/aud_cash/ndq_shares/gold_*) 改成「cash dict + holdings list」
- 支持任意币种现金 + 任意 yfinance symbol 持仓
- 旧字段在数据层由 scripts/migrate_portfolio_to_holdings.py 一次性迁移；本类不再读

职责：
- 只负责"读 memory + 计算用户状态 + 记录交易"
- 文件 IO 统一走 MemoryStore（带文件锁）
- 工资入账 / 委员会触发等业务逻辑见 jobs/
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

from core.memory_store import MemoryStore, _DocTx
from core.schemas import validate_portfolio

log = logging.getLogger(__name__)


# ============ 数据模型 ============

@dataclass
class UserStatus:
    """get_user_status() 返回值（用于 daily_report 等场景）"""
    cash_cny: float                     # CNY 现金（兼容字段：v2 内部从 cash["CNY"] 取）
    cash_aud: float                     # AUD 现金
    disposable_for_invest: float        # 本期可投 CNY = max(0, cash_cny - exchange_buffer)；有正 cap 时封顶
    risk_level: str
    portfolio_value: float              # 总市值（CNY 折算粗算）
    target_asset: str                   # 主资产 symbol（target_assets[0]）
    max_single_invest_cny: float        # 正数=单次上限；0=不设限（target_assets 全 0/缺失）
    user_name: str
    user_email: Optional[str] = None
    holdings_count: int = 0             # v2 新增：持仓资产数量


# ============ HoldingsView：持仓数组的可读写视图 ============

class HoldingsView:
    """包装 portfolio.holdings list，提供 find / upsert / remove 等便利方法

    所有写操作都直接修改 underlying list（in-place），调用方用 with_portfolio_tx 包住保证锁安全
    """

    def __init__(self, raw: List[Dict[str, Any]]):
        self._raw = raw

    def __iter__(self):
        return iter(self._raw)

    def __len__(self):
        return len(self._raw)

    def __getitem__(self, idx):
        return self._raw[idx]

    def all(self) -> List[Dict[str, Any]]:
        """返回 list 副本（只读快照）"""
        return list(self._raw)

    def find(self, symbol: str) -> Optional[Dict[str, Any]]:
        """按 symbol 找单个 holding；找不到返回 None"""
        return next((h for h in self._raw if h.get("symbol") == symbol), None)

    def upsert(self, symbol: str, **fields) -> Dict[str, Any]:
        """存在则 update 字段，不存在则插入新 holding。返回最终的 dict"""
        h = self.find(symbol)
        if h is None:
            h = {"symbol": symbol, **fields}
            self._raw.append(h)
        else:
            h.update(fields)
        return h

    def remove(self, symbol: str) -> bool:
        """删除 symbol；返回是否真删了"""
        before = len(self._raw)
        self._raw[:] = [h for h in self._raw if h.get("symbol") != symbol]
        return len(self._raw) < before


# ============ PortfolioManager ============

class PortfolioManager:
    """持仓门面（v2 通用化）

    属性：
    - `pm.cash` (Dict[str, float]) — 任意币种现金
    - `pm.holdings` (HoldingsView) — 任意 yfinance symbol 持仓
    - `pm.user` / `pm.strategy` (MemoryDoc) — 不动
    """

    def __init__(self, store: Optional[MemoryStore] = None):
        self.store = store or MemoryStore()

        user_doc = self.store.read("user")
        strategy_doc = self.store.read("strategy")
        portfolio_doc = self.store.read("portfolio")

        if not (user_doc and strategy_doc and portfolio_doc):
            raise FileNotFoundError(
                "memory/user.md / strategy.md / portfolio.md 缺失。"
                "首次使用请跑 `python -m scripts.skill init` 初始化",
            )

        self.user = user_doc
        self.strategy = strategy_doc
        self.portfolio = portfolio_doc

    # ---------- v2 接口：cash + holdings ----------
    # v1 fallback 已于 2026-05-10 正式退场（见 docs/wiki/adr/004-v1-fallback-retirement.md）
    # 迁移工具：scripts/migrate_portfolio_to_holdings.py

    @property
    def cash(self) -> Dict[str, float]:
        """任意币种现金视图：pm.cash["CNY"] / pm.cash["AUD"] 等

        v2 only：portfolio.md 必须已经是 v2 格式（schema_version=2）。
        未迁移的旧数据请先跑 scripts/migrate_portfolio_to_holdings.py。
        """
        return dict(self.portfolio.get("cash") or {})

    @property
    def holdings(self) -> HoldingsView:
        """持仓数组视图：pm.holdings.find('NDQ.AX') 等

        v2 only：从 portfolio.md 的 holdings 列表读取。
        未迁移的旧数据请先跑 scripts/migrate_portfolio_to_holdings.py。
        """
        raw = list(self.portfolio.get("holdings") or [])
        return HoldingsView(raw)

    def cash_amount(self, currency: str) -> float:
        """快捷读：pm.cash_amount('CNY') → 0.0 if 不存在"""
        return float(self.cash.get(currency.upper(), 0) or 0)

    def find_holding(self, symbol: str) -> Optional[Dict[str, Any]]:
        """快捷读：pm.find_holding('NDQ.AX')"""
        return self.holdings.find(symbol)

    # ---------- 读：用户状态聚合 ----------

    def get_user_status(
        self,
        current_prices: Optional[Dict[str, float]],
        exchange_rate: Optional[float] = None,
    ) -> UserStatus:
        """汇总用户状态 → daily_report / committee 用

        Args:
            current_prices: {symbol: 当前价 in cost_currency} dict。任一资产的当前价
                拉不到，**不要在 dict 里塞 0**——直接 omit key（用 cost 兜底）或
                设为 None（剔除该 holding 不进总市值，避免 Risk Officer 把 0 误读成
                "集中度爆表，建议清仓"）。传 None 表示全部资产没拉到价。
            exchange_rate: **已 deprecated** (2026-05-19 A2 fix)。历史上是 AUD→CNY
                汇率的单参数硬编码，假设用户只持 AUD。现在内部走 utils.fx.to_base
                自取所有币种汇率（USD/EUR/HKD/AUD/...）。若显式传入则仍作为 AUD→CNY
                的覆盖值用（保留给 test_portfolio_manager.py 的 deterministic 测试），
                不传则自动调 utils.fx.get_fx_rate("AUD", "CNY")。

        v2 通用化：之前是写死 NDQ.AX 一个分支接 current_stock_price，fork 用户持
        AAPL/510300 完全按 cost 兜底估值，市值偏差大。改成 dict 让所有 holding
        都能用最新价。
        2026-05-19 (A1/A2 修复)：之前 portfolio_value 只支持 CNY/AUD 两条分支，
        USD/HKD/EUR 等持仓被漏算 → Risk Officer 看到的总市值偏低 → 集中度判断错。
        现在用 utils.fx.to_base 折算所有币种。
        """
        from utils.fx import get_fx_rate, to_base
        cash_cny = self.cash_amount("CNY")
        cash_aud = self.cash_amount("AUD")
        exchange_buffer = float(self.user.get("exchange_buffer_cny", 0) or 0)
        prices = dict(current_prices or {})

        target_assets = list(self.strategy.get("target_assets", []) or [])
        if target_assets:
            # 单次上限语义（2026-06-12）：0/缺失 = 该资产不设限；
            # 全部不设限 → max_single=0 哨兵，disposable 不钳（金额交委员会裁量）
            caps = [float(t.get("max_single_invest_cny", 0) or 0) for t in target_assets]
            positive_caps = [c for c in caps if c > 0]
            max_single = max(positive_caps) if positive_caps else 0.0
            primary_asset = str(target_assets[0].get("symbol", ""))
        else:
            # 旧 v1 单资产 strategy 路径：保留 10000 默认，不引入新语义
            max_single = float(self.strategy.get("max_single_invest_cny", 10000) or 10000)
            primary_asset = str(self.strategy.get("target_asset", ""))

        # 总市值聚合：cash 多币种（不只是 CNY/AUD）+ holdings 用 to_base 折算
        # 显式传入 exchange_rate 时用它作 AUD→CNY 覆盖（保留 deterministic 测试兼容）；
        # 否则走 utils.fx 自动拉。
        def _aud_to_cny_rate() -> Optional[float]:
            if exchange_rate is not None:
                return float(exchange_rate)
            return get_fx_rate("AUD", "CNY")

        def _ccy_to_cny(amount: float, ccy: str) -> Optional[float]:
            """折算到 CNY；AUD 优先用 exchange_rate 覆盖，其他走 to_base"""
            if amount == 0:
                return 0.0
            if ccy == "CNY":
                return amount
            if ccy == "AUD" and exchange_rate is not None:
                return amount * float(exchange_rate)
            return to_base(ccy, amount, "CNY")

        portfolio_value = cash_cny
        # 多币种 cash 全部折算（不只 CNY/AUD）
        for ccy, amt in self.cash.items():
            ccy_u = str(ccy).upper()
            if ccy_u == "CNY":
                continue   # 已经计入 cash_cny
            converted = _ccy_to_cny(float(amt or 0), ccy_u)
            if converted is not None:
                portfolio_value += converted
            # 拉不到汇率的币种静默跳过（不引入 0 兜底）

        for h in self.holdings:
            if h.get("is_tracking_only"):
                continue   # 追踪仓不计入资产
            sym = str(h.get("symbol", ""))
            units = float(h.get("units", 0) or 0)
            avg = float(h.get("avg_cost", 0) or 0)
            ccy = str(h.get("cost_currency", "CNY"))
            # 取价：dict 里 explicit None = "拉不到，剔除"；缺 key = "用 cost 兜底"
            if sym in prices:
                price = prices[sym]
                if price is None:
                    continue   # 该资产剔除（不用 0，防 Risk 误判清仓）
            else:
                price = avg
            value_local = units * price
            value_cny = _ccy_to_cny(value_local, ccy)
            if value_cny is not None:
                portfolio_value += value_cny
            # 拉不到汇率的 holding 静默跳过（缺口告警在 daily_report 那层做）

        available = max(0.0, cash_cny - exchange_buffer)
        disposable = min(available, max_single) if max_single > 0 else available

        return UserStatus(
            cash_cny=cash_cny,
            cash_aud=cash_aud,
            disposable_for_invest=disposable,
            risk_level=str(self.user.get("risk_tolerance", "Balanced")),
            portfolio_value=portfolio_value,
            target_asset=primary_asset,
            max_single_invest_cny=max_single,
            user_name=str(self.user.get("display_name", "Anonymous")),
            holdings_count=len([h for h in self.holdings if not h.get("is_tracking_only")]),
        )

    def get_processed_emails(self) -> List[str]:
        return list(self.store.state_get("processed_emails", []))

    # ---------- 写：单锁 RMW 闭包 ----------

    @contextmanager
    def with_portfolio_tx(self) -> Iterator[_DocTx]:
        """对外暴露的 portfolio RMW 闭包

        用法：
            with pm.with_portfolio_tx() as p:
                # 改 cash
                cash = dict(p.get("cash") or {})
                cash["CNY"] = cash.get("CNY", 0) + amount
                p["cash"] = cash
                # 改 holdings（list of dict）
                holdings = list(p.get("holdings") or [])
                # ... 找/改/插入 ...
                p["holdings"] = holdings
                # schema_version 必须保留
                p["schema_version"] = 2
            # 退出 with 自动: 1) schema validate 2) body 重渲染 3) atomic write
            pm._reload()  # 刷新 self.portfolio 视图

        commit-on-success：with 块内抛异常 → 整个写不会落盘（已改的 metadata 丢弃）。
        """
        with self.store.transaction("portfolio") as p:
            # 进 with 前自动 v1→v2 fallback（让调用方直接拿到 cash + holdings）
            _ensure_v2_inplace(p)
            yield p
            # commit 前：清理 v1 旧字段 + 标记 schema_version=2 + 校验 + 渲染 body
            for k in ("cash_cny", "aud_cash", "ndq_shares",
                      "ndq_avg_cost_aud_per_share",
                      "gold_grams", "gold_avg_cost_cny_per_gram"):
                p.metadata.pop(k, None)
            p["schema_version"] = 2
            try:
                validate_portfolio(dict(p.metadata))
            except Exception as e:
                log.error(f"with_portfolio_tx schema validate failed: {e}")
                raise
            p.set_body(_render_portfolio_body_v2(p))

    def update_after_invest(self, invest_cny: float) -> None:
        """daily_report 在用户实际买入后调用（手动操作时留接口）"""
        with self.with_portfolio_tx() as p:
            cash = dict(p.get("cash") or {})
            cash["CNY"] = float(cash.get("CNY", 0) or 0) - invest_cny
            p["cash"] = cash
        self._reload()

    def record_external_trade(self, trade: dict) -> None:
        """从 CommSec 邮件解析出的成交回报 → 更新 holdings + cash + history + processed_emails

        v2 通用化：trade dict 含 symbol/units/total_amount/currency/action/email_id/...
        upsert holding by symbol；cash[currency] 扣减/增加。
        """
        symbol = str(trade.get("symbol", "")).strip()
        if not symbol:
            log.warning(f"record_external_trade: 空 symbol，跳过 {trade}")
            return

        units = float(trade.get("units", 0) or 0)
        action = str(trade.get("action", "")).lower()
        amount = float(trade.get("total_amount", 0) or 0)
        currency = str(trade.get("currency", "")).strip().upper() or "AUD"
        kind = trade.get("kind") or _guess_kind_from_symbol(symbol)

        with self.with_portfolio_tx() as p:
            holdings = list(p.get("holdings") or [])
            cash = dict(p.get("cash") or {})

            # 找现有 holding
            target = next((h for h in holdings if h.get("symbol") == symbol), None)
            cur_units = float(target.get("units", 0) or 0) if target else 0.0
            cur_avg = float(target.get("avg_cost", 0) or 0) if target else 0.0

            if action == "bought":
                new_units = cur_units + units
                # 加权均价（cur_units==0 时退化为本次价）
                new_avg = (
                    (cur_avg * cur_units + amount) / new_units if new_units else (amount / units if units else 0)
                )
                if target:
                    target["units"] = new_units
                    target["avg_cost"] = round(new_avg, 4)
                else:
                    holdings.append({
                        "symbol": symbol,
                        "kind": kind,
                        "units": new_units,
                        "unit_label": "股" if kind in ("equity", "etf") else "share",
                        "avg_cost": round(new_avg, 4),
                        "cost_currency": currency,
                        "channel": trade.get("channel"),
                    })
                cash[currency] = float(cash.get(currency, 0) or 0) - amount
            elif action == "sold":
                if target:
                    target["units"] = max(0.0, cur_units - units)
                cash[currency] = float(cash.get(currency, 0) or 0) + amount

            p["holdings"] = holdings
            p["cash"] = cash

        # processed_emails 在 transaction 外（独立 state 文件）
        email_id = trade.get("email_id")
        if email_id:
            processed = self.get_processed_emails()
            if email_id not in processed:
                processed.append(email_id)
                self.store.state_set("processed_emails", processed)

        # history.jsonl 也在 transaction 外（独立 append-only 锁）
        self.store.append_history(trade)
        self._reload()

    def deposit_cash(
        self, currency: str, amount: float, *, source: str = "skill_cli",
    ) -> Dict[str, Any]:
        """存入现金（任意币种）。CLI deposit 与 /api/skill/deposit 共用，
        非法参数抛 ValueError（消息与原 CLI 输出一致）"""
        ccy = currency.upper()
        amount = float(amount)
        if amount <= 0:
            raise ValueError("amount 必须 > 0")
        if not (3 <= len(ccy) <= 5) or not ccy.isalpha():
            raise ValueError(f"非法币种 {ccy}")
        with self.with_portfolio_tx() as p:
            cash = dict(p.get("cash") or {})
            new_balance = float(cash.get(ccy, 0) or 0) + amount
            cash[ccy] = round(new_balance, 2)
            p["cash"] = cash
        self._reload()
        self.store.append_history({
            "ts_origin": _now_iso_local(), "action": "deposit",
            "symbol": ccy, "units": amount, "currency": ccy, "source": source,
        })
        return {
            "status": "ok", "currency": ccy, "amount_deposited": amount,
            "new_balance": new_balance, "cny_total": self.cash_amount("CNY"),
        }

    def withdraw_cash(
        self, currency: str, amount: float, *, source: str = "skill_cli",
    ) -> Dict[str, Any]:
        """取出现金（任意币种）。余额不足抛 ValueError（余额检查在 fcntl 锁内，防 TOCTOU）"""
        ccy = currency.upper()
        amount = float(amount)
        if amount <= 0:
            raise ValueError("amount 必须 > 0")
        with self.with_portfolio_tx() as p:
            cash = dict(p.get("cash") or {})
            current = float(cash.get(ccy, 0) or 0)
            if current < amount:
                raise ValueError(f"{ccy} 余额 {current} < 取出 {amount}")
            cash[ccy] = round(current - amount, 2)
            p["cash"] = cash
        self._reload()
        self.store.append_history({
            "ts_origin": _now_iso_local(), "action": "withdraw",
            "symbol": ccy, "units": -amount, "currency": ccy, "source": source,
        })
        return {
            "status": "ok", "currency": ccy, "amount_withdrawn": amount,
            "new_balance": current - amount, "cny_total": self.cash_amount("CNY"),
        }

    def delete_holding(
        self, symbol: str, *, force: bool = False, source: str = "skill_cli",
    ) -> Dict[str, Any]:
        """删除持仓行（units 必须为 0，否则拒绝；force=True 强删）+ 记 history"""
        with self.with_portfolio_tx() as p:
            holdings = list(p.get("holdings") or [])
            target = next((h for h in holdings if h.get("symbol") == symbol), None)
            if target is None:
                raise ValueError(f"symbol {symbol} 不在持仓")
            units = float(target.get("units", 0) or 0)
            is_tracking = bool(target.get("is_tracking_only", False))
            if not is_tracking and units > 0 and not force:
                raise ValueError(
                    f"{symbol} 持仓 {units} > 0，请先 sell 或加 --force 强删"
                )
            p["holdings"] = [h for h in holdings if h.get("symbol") != symbol]
        self._reload()
        self.store.append_history({
            "ts_origin": _now_iso_local(), "action": "delete_holding",
            "symbol": symbol, "source": source,
        })
        return {"status": "ok", "deleted": symbol}

    def buy(
        self,
        symbol: str,
        units: float,
        price: float,
        *,
        currency: str = "CNY",
        kind: str = "equity",
        unit_label: str = "股",
        source: str = "skill_cli",
    ) -> Dict[str, Any]:
        """加仓（已有 symbol 增加 units + 加权平均成本；新 symbol 直接建仓）

        同步扣现金（保证账本一致：买 X 元股 = 扣 X 元现金）+ 记 history。
        CLI (scripts/skill.py:cmd_buy) 与 Web API (/api/skill/buy) 共用——
        units/price 非法抛 ValueError，由调用方转 CLI error JSON / HTTP 400。
        """
        if units <= 0 or price <= 0:
            raise ValueError("units / price 必须 > 0")
        ccy = currency.upper()
        cost_cny = units * price  # 简化：fork 用户若用非 CNY 自己换算后再传 price=CNY 价
        with self.with_portfolio_tx() as p:
            holdings = list(p.get("holdings") or [])
            existing = next((h for h in holdings if h.get("symbol") == symbol), None)
            if existing:
                # 加权平均成本
                old_units = float(existing.get("units", 0) or 0)
                old_avg = float(existing.get("avg_cost", 0) or 0)
                new_units = old_units + units
                new_avg = (old_units * old_avg + units * price) / new_units
                existing["units"] = round(new_units, 6)
                existing["avg_cost"] = round(new_avg, 6)
                action_kind = "add"
            else:
                holdings.append({
                    "symbol": symbol, "kind": kind, "units": units, "avg_cost": price,
                    "unit_label": unit_label, "cost_currency": ccy, "proxy_kind": "direct",
                })
                action_kind = "new"
            p["holdings"] = holdings

            cash = dict(p.get("cash") or {})
            cash[ccy] = round(float(cash.get(ccy, 0) or 0) - units * price, 2)
            p["cash"] = cash
        self._reload()
        self.store.append_history({
            "ts_origin": _now_iso_local(), "action": "buy",
            "symbol": symbol, "units": units, "price": price,
            "currency": ccy, "source": source,
        })
        return {
            "status": "ok", "action": action_kind, "symbol": symbol,
            "units_added": units, "price": price, "currency": ccy,
            "cost_cny_estimate": cost_cny,
        }

    def sell(
        self,
        symbol: str,
        units: float,
        price: float,
        *,
        source: str = "skill_cli",
    ) -> Dict[str, Any]:
        """减仓（units 减少，cost_avg 不变；卖出按 holding 的 cost_currency 还现金）

        units 减完后保留为 0（删行走 delete_holding）。symbol 不在持仓 / 持仓不足
        抛 ValueError，由调用方转 CLI error JSON / HTTP 400。
        """
        if units <= 0 or price <= 0:
            raise ValueError("units / price 必须 > 0")
        with self.with_portfolio_tx() as p:
            holdings = list(p.get("holdings") or [])
            target = next((h for h in holdings if h.get("symbol") == symbol), None)
            if target is None:
                raise ValueError(f"symbol {symbol} 不在持仓里")
            old_units = float(target.get("units", 0) or 0)
            if old_units < units:
                raise ValueError(f"{symbol} 持仓 {old_units} < 卖出 {units}")
            target["units"] = round(old_units - units, 6)
            p["holdings"] = holdings

            # 卖出收回现金（cost_currency = target 的 cost_currency）
            ccy = str(target.get("cost_currency", "CNY")).upper()
            cash = dict(p.get("cash") or {})
            cash[ccy] = round(float(cash.get(ccy, 0) or 0) + units * price, 2)
            p["cash"] = cash
        self._reload()
        self.store.append_history({
            "ts_origin": _now_iso_local(), "action": "sell",
            "symbol": symbol, "units": units, "price": price,
            "currency": ccy, "source": source,
        })
        return {
            "status": "ok", "symbol": symbol, "units_sold": units, "price": price,
            "proceeds_cny_estimate": units * price,  # 简化估计
            "remaining_units": old_units - units,
        }

    def add_income(self, net_income_cny: float, payday_label: str) -> None:
        """payday_check job 调用 - CNY 月度净收入入账"""
        with self.with_portfolio_tx() as p:
            cash = dict(p.get("cash") or {})
            cash["CNY"] = float(cash.get("CNY", 0) or 0) + net_income_cny
            p["cash"] = cash
            new_cash = cash["CNY"]

        self.store.update_fields("user", last_payday=payday_label)
        self._reload()
        log.info(
            f"💰 [Payday {payday_label}] 净收入 ¥{net_income_cny:,.0f} 已入账，"
            f"现金余额 ¥{new_cash:,.2f}",
        )

    # ---------- 内部 ----------

    def _reload(self) -> None:
        """写入后重新读，保证下一次访问看到最新数据"""
        self.user = self.store.read("user")  # type: ignore[assignment]
        self.strategy = self.store.read("strategy")  # type: ignore[assignment]
        self.portfolio = self.store.read("portfolio")  # type: ignore[assignment]


# ============ 工具函数 ============

def _now_iso_local() -> str:
    """本地时区 ISO 时间戳——history.jsonl 的 ts_origin 口径（与 scripts/skill.py 一致）"""
    from datetime import timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ensure_v2_inplace(p) -> None:
    """transaction 入口处确保 cash / holdings 字段已就位（v2 only）

    v1 fallback 已于 2026-05-10 正式退场。
    调用方需确保 portfolio.md 已通过 scripts/migrate_portfolio_to_holdings.py 迁移。
    此函数保留为空实现是为了兼容 with_portfolio_tx 的调用位置，后续可彻底删除。
    """
    # v2 数据直接读取，无需任何转换
    pass


def _guess_kind_from_symbol(symbol: str) -> str:
    """根据 symbol 启发式猜测 kind（CommSec 邮件 trade dict 没显式给 kind 时用）"""
    s = symbol.upper()
    if s.endswith(".AX") or s.endswith(".HK") or "." in s:
        return "equity"
    if "-USD" in s or s in ("BTC", "ETH"):
        return "crypto"
    if s in ("GC=F", "SI=F", "GOLD"):
        return "metal"
    return "equity"


def _render_portfolio_body_v2(p) -> str:
    """v2 portfolio.md body 渲染。p 是 _DocTx（也支持 MemoryDoc）"""
    cash = dict(p.get("cash") or {})
    holdings = list(p.get("holdings") or [])

    lines = ["# 当前持仓", ""]

    if cash:
        lines.append("## 现金")
        for ccy, amt in sorted(cash.items()):
            lines.append(f"- **{ccy}**: {amt:,.2f}")
        lines.append("")

    real_holdings = [h for h in holdings if not h.get("is_tracking_only")]
    tracking_holdings = [h for h in holdings if h.get("is_tracking_only")]

    if real_holdings:
        lines.append("## 持仓")
        for h in real_holdings:
            unit = h.get("unit_label", "share")
            label = h.get("display_name") or h["symbol"]
            avg = float(h.get("avg_cost", 0) or 0)
            ccy = h.get("cost_currency", "")
            line = f"- **{label}** (`{h['symbol']}`): {h.get('units', 0)} {unit}"
            if avg:
                line += f"，均价 {avg:.2f} {ccy}/{unit}"
            channel = h.get("channel")
            if channel:
                line += f"（渠道 {channel}）"
            lines.append(line)
        lines.append("")

    if tracking_holdings:
        lines.append("## 追踪仓（仅观察，不计 P&L）")
        for h in tracking_holdings:
            label = h.get("display_name") or h["symbol"]
            lines.append(f"- 🔍 **{label}** (`{h['symbol']}`)")
        lines.append("")

    if not real_holdings and not tracking_holdings:
        lines.extend(["（暂无持仓）", ""])

    lines.extend([
        "## 说明",
        "",
        f"_schema_version: {p.get('schema_version', 2)}_  此文件由 daily_report / commsec_sync / payday_check / web_api / napcat_bot 自动更新；不要手动编辑。",
    ])
    return "\n".join(lines) + "\n"
