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
import math
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

from openinvest.core.memory_store import MemoryStore, _DocTx
from openinvest.core.schemas import validate_portfolio

log = logging.getLogger(__name__)


# ============ 数据模型 ============

@dataclass
class UserStatus:
    """get_user_status() 返回值（用于 daily_report 等场景）"""
    cash_cny: float                     # CNY 现金（兼容字段：v2 内部从 cash["CNY"] 取）
    cash_aud: float                     # AUD 现金
    disposable_for_invest: float        # 本期可投 CNY = 所有币种现金折算成 CNY（不小于 0）；有正 cap 时封顶
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

class MemoryNotInitialized(FileNotFoundError):
    """数据目录还没 init（user/strategy/portfolio 缺）。仍是 FileNotFoundError，老的 except 照常接住；
    CLI 据此输出结构化 JSON 而不是一段 traceback。"""


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
            raise MemoryNotInitialized(
                "memory/user.md / strategy.md / portfolio.md 缺失。"
                "首次使用请跑 `openinvest init` 初始化",
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
        from openinvest.utils.fx import get_fx_rate, to_base
        cash_cny = self.cash_amount("CNY")
        cash_aud = self.cash_amount("AUD")
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
            # live valuation: as_of_date intentionally not threaded (no historical caller).
            # For backtest/historical use, thread as_of_date like utils.fx.to_base (see PR#53 fix(fx)).
            return get_fx_rate("AUD", "CNY")

        def _ccy_to_cny(amount: float, ccy: str) -> Optional[float]:
            """折算到 CNY；AUD 优先用 exchange_rate 覆盖，其他走 to_base"""
            if amount == 0:
                return 0.0
            if ccy == "CNY":
                return amount
            if ccy == "AUD" and exchange_rate is not None:
                return amount * float(exchange_rate)
            # live valuation: as_of_date intentionally not threaded (no historical caller).
            # For backtest/historical use, thread as_of_date like utils.fx.to_base (see PR#53 fix(fx)).
            return to_base(ccy, amount, "CNY")

        # ponytail: portfolio_value 没有调用方（只有测试读），保留自己的粗算而不并进
        # fx.total_portfolio_value；有人要用它时先换成那个唯一实现。
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

        # 可投 = 所有币种现金折算成 CNY（基准币种方案 A，2026-10）；与持仓摘要同一实现 fx.dry_powder
        from openinvest.utils.fx import dry_powder
        available, _missing = dry_powder(
            self, "CNY", rate_override={"AUD": float(exchange_rate)} if exchange_rate is not None else None)
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
    def _ledger_tx(self, idempotency_key: Optional[str]) -> Iterator[_DocTx]:
        """with_portfolio_tx + 幂等闸（ADR-016）：buy/sell/deposit/withdraw 共用。

        key 记在 portfolio 文档自身的 `ledger_idempotency_keys`（与 trades 同步的
        `synced_trade_ids` 同款）：check + append 与 cash/holdings 改动在**同一把锁、同一次
        atomic write** 落盘——并发 / HTTP 超时重发 / agent 重跑同 key 只有一个赢家，其余抛
        _DuplicateKey 中止事务（commit-on-success，不落盘）；块内业务异常（余额不足等）或
        commit 失败 / 中途被 kill 时 key 和账本一起不落盘，不存在"key 烧掉了但账没记"的
        丢单窗口（独立 .state 文件的 state_claim 做不到：两个文件两次写）。不传 key 行为不变。
        """
        with self.with_portfolio_tx() as p:
            if idempotency_key:
                seen = list(p.get(_LEDGER_KEYS) or [])
                if idempotency_key in seen:
                    raise _DuplicateKey(idempotency_key)
                # ponytail: 只留最近 N 个 key（同 synced_trade_ids）；更老的 key 重放回到无键行为
                p[_LEDGER_KEYS] = (seen + [idempotency_key])[-_LEDGER_KEYS_KEEP:]
            yield p

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
            # v1 文件直接拒绝（commit 会 pop 掉 v1 字段 = 现金永久丢失）
            _ensure_v2_inplace(p, self.store)
            yield p
            # commit 前：清理 v1 旧字段 + 标记 schema_version=2 + 校验 + 渲染 body
            for k in _V1_KEYS:
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

        # ---- 幂等闸：动账本之前先原子 claim email_id ----
        # processed_emails 与 portfolio 是两个独立文件、各有各的锁，无法塞进单一事务。
        # 退而求其次：在改 cash/holdings *之前* 原子 claim（check+append 同锁），apply
        # 失败再 unclaim。关掉两个真实漏洞——
        #   (1) 并发 commsec_apply：两请求同 email_id 只有一个 claim 成功，另一个早退
        #   (2) 成功后崩溃/重试：email_id 已 claim → 早退，不会二次记账
        # 残余风险（非全闭环）：claim 写盘后、portfolio-tx 提交前被 SIGKILL/OOM →
        # email_id 永久 claimed 但账本未改，该笔成交会被静默跳过，需人工
        # state_unclaim("processed_emails", email_id) 才能重放。旧行为（崩在 tx 提交
        # 之后 = 双重记账真金白银）更糟，故接受此窄窗口权衡。
        email_id = trade.get("email_id")
        if email_id and not self.store.state_claim("processed_emails", email_id):
            log.info(f"record_external_trade: email_id={email_id} 已处理，跳过（幂等）")
            return

        try:
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
                    # 有意设计：sold 无持仓仍记 cash。CommSec = 真实已结算成交，钱真的
                    # 到账了；不记反而让现金低估真实余额（fork 用户半路接入、买入早于
                    # lookback 都会触发）。与 web 手工路径 _sync_trade_to_portfolio 的
                    # _SkipSync 故意语义不同——勿合并成共用底层函数。
                    if target:
                        target["units"] = max(0.0, cur_units - units)
                    cash[currency] = float(cash.get(currency, 0) or 0) + amount

                p["holdings"] = holdings
                p["cash"] = cash
        except Exception:
            # 账本没改成 → 撤销 claim，让下次 sync 能重试这封邮件
            # （避免 claim-but-not-applied 把真实成交静默丢掉）
            if email_id:
                self.store.state_unclaim("processed_emails", email_id)
            raise

        # history.jsonl 在 transaction 外（独立 append-only 锁）
        self.store.append_history(trade)
        self._reload()

    def deposit_cash(
        self, currency: str, amount: float, *, source: str = "skill_cli",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """存入现金（任意币种）。CLI deposit 与 /api/skill/deposit 共用，
        非法参数抛 ValueError（消息与原 CLI 输出一致）。
        idempotency_key：同 key 第二次返回 {"status": "duplicate"} 不动账本（ADR-016）。"""
        ccy = currency.upper()
        amount = float(amount)
        if amount <= 0:
            raise ValueError("amount 必须 > 0")
        if not (3 <= len(ccy) <= 5) or not ccy.isalpha():
            raise ValueError(f"非法币种 {ccy}")
        try:
            with self._ledger_tx(idempotency_key) as p:
                cash = dict(p.get("cash") or {})
                new_balance = float(cash.get(ccy, 0) or 0) + amount
                cash[ccy] = round(new_balance, 2)
                p["cash"] = cash
        except _DuplicateKey:
            return _duplicate(idempotency_key)
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
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """取出现金（任意币种）。余额不足抛 ValueError（余额检查在 fcntl 锁内，防 TOCTOU）。
        idempotency_key 语义同 deposit_cash。"""
        ccy = currency.upper()
        amount = float(amount)
        if amount <= 0:
            raise ValueError("amount 必须 > 0")
        try:
            with self._ledger_tx(idempotency_key) as p:
                cash = dict(p.get("cash") or {})
                current = float(cash.get(ccy, 0) or 0)
                if current < amount:
                    raise ValueError(f"{ccy} 余额 {current} < 取出 {amount}")
                cash[ccy] = round(current - amount, 2)
                p["cash"] = cash
        except _DuplicateKey:
            return _duplicate(idempotency_key)
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
            in_transit = [o for o in _pending_of(target) if o.get("status") == "placed"]
            if in_transit:   # 现金已扣、份额未到：删行 = 在途金额从账上凭空消失，force 也不放行
                raise ValueError(
                    f"{symbol} 有 {len(in_transit)} 笔在途申购单未确认（共 {sum(float(o.get('amount') or 0) for o in in_transit):,.2f}）："
                    f"先 confirm_fund_order 或 cancel_fund_order，否则这笔钱会从账上消失")
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
        currency: Optional[str] = None,
        kind: str = "equity",
        unit_label: str = "股",
        source: str = "skill_cli",
        source_type: str = "cash_deduct",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """加仓（已有 symbol 增加 units + 加权平均成本；新 symbol 直接建仓）

        同步扣现金（保证账本一致：买 X 元股 = 扣 X 元现金）+ 记 history。
        CLI (scripts/skill.py:cmd_buy) 与 Web API (/api/skill/buy) 共用——
        units/price 非法抛 ValueError，由调用方转 CLI error JSON / HTTP 400。

        资金来源 source_type（子弹池模型，见 ADR-018）：
        - "cash_deduct"（默认）：从 portfolio cash 扣对应币种金额（账本一致，现有行为）
        - "external_funding"：外部新钱（工资/银行卡，如自动定投）建仓，**portfolio cash 不动**。
          日定投的钱不来自子弹池现金，扣它会错误消耗抄底弹药（¥30k 子弹 10 个月归零）。

        currency：price 的币种。加仓已有持仓时不传 = 沿用该持仓的计价币种；传了但和
        持仓币种不一致直接报错——否则会扣错币种的现金，并把两种币种的价格平均进同一个
        成本（2026-10 盘点：漏写币种给澳元持仓加仓，扣人民币、成本被人民币价拉偏）。
        新建仓不传 = CNY。

        黄金（GC=F）：只接受 CNY / 克（currency=CNY、unit_label=克，price = 人民币/克），
        其余 ValueError 并给正确写法；合法记法自动落 kind=metal + proxy_kind=gold_cny_per_gram（#443）。

        idempotency_key：同 key 第二次返回 {"status": "duplicate"} 不动账本（ADR-016，
        远端 HTTP 超时重发 / agent 重跑同一笔）。record_existing_position 也经此路径。
        """
        if units <= 0 or price <= 0:
            raise ValueError("units / price 必须 > 0")
        if source_type not in ("cash_deduct", "external_funding"):
            raise ValueError(
                f"非法 source_type {source_type!r}（只接受 cash_deduct / external_funding）"
            )
        if symbol.strip().upper() == _GOLD_SYMBOL:
            symbol = _GOLD_SYMBOL   # 'gc=f' 与已有 'GC=F' 行是同一个标的：别因大小写新建出第二行
        cost_cny = units * price  # 金额单位 = ccy（字段名是历史遗留）
        fund_warning = _fund_buy_warning(self, symbol, kind) if source_type == "cash_deduct" else None
        try:
            with self._ledger_tx(idempotency_key) as p:
                holdings = list(p.get("holdings") or [])
                existing = next((h for h in holdings if h.get("symbol") == symbol), None)
                held = str(existing.get("cost_currency") or "CNY").upper() if existing else None
                ccy = (currency or held or "CNY").upper()
                gold = gold_booking(symbol, ccy, unit_label, held)   # 非 GC=F → None
                if not gold and held and ccy != held:
                    raise ValueError(
                        f"{symbol} 持仓按 {held} 计价，这笔买入写的是 {ccy}；"
                        f"请按 {held} 价格记账（currency 填 {held} 或不填）"
                    )
                if existing:
                    # 加权平均成本
                    old_units = float(existing.get("units", 0) or 0)
                    old_avg = float(existing.get("avg_cost", 0) or 0)
                    new_units = old_units + units
                    new_avg = (old_units * old_avg + units * price) / new_units
                    existing["units"] = round(new_units, 6)
                    existing["avg_cost"] = round(new_avg, 6)
                    if gold:
                        existing.update(gold)   # 顺手修复被旧版本记成 proxy_kind=direct 的黄金行
                    action_kind = "add"
                else:
                    holdings.append({
                        "symbol": symbol, "kind": kind, "units": units, "avg_cost": price,
                        "unit_label": unit_label, "cost_currency": ccy, "proxy_kind": "direct",
                        **(gold or {}),
                    })
                    action_kind = "new"
                p["holdings"] = holdings

                # 资金来源决定是否扣现金：external_funding = 外部新钱，子弹池现金不动
                if source_type == "cash_deduct":
                    cash = dict(p.get("cash") or {})
                    current = float(cash.get(ccy, 0) or 0)
                    # 防穿透：主动建仓不得把子弹池现金扣成负数（与 withdraw_cash 对称，
                    # 锁内校验防 TOCTOU）。钱不来自子弹池的合法场景走 source_type=
                    # 'external_funding'（不动 cash）。存储层仍容忍负现金——那是给
                    # record_external_trade 券商真实结算回灌用的，与主动 buy 无关。
                    if current < cost_cny:
                        raise ValueError(
                            f"{ccy} 现金 {current} < 买入成本 {cost_cny}"
                            f"（子弹不足；外部新钱建仓请用 source_type='external_funding'）"
                        )
                    cash[ccy] = round(current - cost_cny, 2)
                    p["cash"] = cash
        except _DuplicateKey:
            return _duplicate(idempotency_key)
        self._reload()
        self.store.append_history({
            "ts_origin": _now_iso_local(), "action": "buy",
            "symbol": symbol, "units": units, "price": price,
            "currency": ccy, "source": source, "funding_source": source_type,
        })
        out: Dict[str, Any] = {
            "status": "ok", "action": action_kind, "symbol": symbol,
            "units_added": units, "price": price, "currency": ccy,
            "cost_cny_estimate": cost_cny, "funding_source": source_type,
        }
        if fund_warning:
            log.warning(fund_warning)
            out["warning"] = fund_warning
        return out

    def sell(
        self,
        symbol: str,
        units: float,
        price: float,
        *,
        source: str = "skill_cli",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """减仓（units 减少，cost_avg 不变；卖出按 holding 的 cost_currency 还现金）

        units 减完后保留为 0（删行走 delete_holding）。symbol 不在持仓 / 持仓不足
        抛 ValueError，由调用方转 CLI error JSON / HTTP 400。idempotency_key 语义同 buy。
        """
        if units <= 0 or price <= 0:
            raise ValueError("units / price 必须 > 0")
        try:
            with self._ledger_tx(idempotency_key) as p:
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
        except _DuplicateKey:
            return _duplicate(idempotency_key)
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

    # ---------- ADR-027 PR2：分红 / 利息 / 费用 / 税 / 拆股 / 退回本金 ----------
    # 全部走 _ledger_tx（fcntl 锁 + atomic write + 可选 idempotency_key，同 buy）：快照（cash / units / avg_cost）
    # 在事务里落下，流水行（schema_version=2，cash_effect / units_effect / external 写入方算好）在事务后追加。
    # 效果表见 docs/wiki/adr/027-ledger-activity-types.md D4；读方只认流水里的 cash_effect / units_effect / external。

    def _require_known_symbol(self, symbol: str) -> bool:
        """symbol 必须是当前持仓，或流水里出现过（卖光 / 删行的标的合法）；都不是 → ValueError（拦手滑写错的 symbol：
        账本是追加式的，写错的分红 / 拆股没有"撤销"）。返回当前是否还持有。"""
        if self.find_holding(symbol) is not None:
            return True
        if any(str(r.get("symbol") or "") == symbol for r in self.store.read_history()):
            return False
        raise ValueError(f"{symbol} 既不在当前持仓、账本里也没有它的记录；请核对 symbol（大小写 / 后缀）")

    def record_dividend(
        self,
        symbol: str,
        gross: float,
        currency: str,
        tax_withheld: float = 0.0,
        reinvest: bool = False,
        trade_date: Optional[str] = None,
        note: Optional[str] = None,
        *,
        reinvest_price: Optional[float] = None,
        source: str = "skill_cli",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记一笔分红：现金 +（gross − tax_withheld），持仓数量 / 成本不变；不是外部现金流（进 NAV 不进 TWR 分母）。

        gross = 税前总额（券商对账单的"分红总额"）；tax_withheld = 已被预扣的税；``currency`` = 现金到账币种。
        ``trade_date`` = 到账日（YYYY-MM-DD，缺省今天）——账户业绩按它在 NAV 里入账，补记历史分红请传真实日期。
        reinvest=True（DRIP）：同一事务里再追加一笔 buy（source=drip，external=False，用掉这笔净额），
        单价 = ``reinvest_price``，缺省取行情库当日原始收盘价（``backfill_raw_close`` 回填的 close_raw，往前最多 7 天）；
        持仓必须存在且 ``currency`` 必须等于该持仓的计价币种。
        symbol 已无持仓但账本里有它（例如除息后卖掉、派息日才到账）允许记；完全没见过的 symbol 抛 ValueError（防写错）。
        idempotency_key 语义同 buy（ADR-016）。非法参数抛 ValueError。"""
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol 必填")
        still_held = self._require_known_symbol(symbol)
        ccy = _clean_ccy(currency)
        gross, tax = float(gross), float(tax_withheld or 0)
        if not gross > 0:
            raise ValueError("gross 必须 > 0")
        if not 0 <= tax <= gross:
            raise ValueError("tax_withheld 必须在 0..gross 之间")
        day = _activity_day(trade_date)
        net = round(gross - tax, 2)
        price = None
        if reinvest:
            price = float(reinvest_price) if reinvest_price else _stored_raw_close(self, symbol, day)
            if not price or price <= 0:
                raise ValueError(
                    f"reinvest 需要单价：行情库没有 {symbol} 在 {day} 前 7 天内的原始收盘价。"
                    f"传 reinvest_price，或先跑 `openinvest backfill_raw_close --symbols {symbol}`")
        reinvested = None
        try:
            with self._ledger_tx(idempotency_key) as p:
                cash = dict(p.get("cash") or {})
                cash[ccy] = round(float(cash.get(ccy, 0) or 0) + net, 2)
                holdings = list(p.get("holdings") or [])
                held = next((h for h in holdings if h.get("symbol") == symbol), None)
                if reinvest:
                    held_ccy = str(held.get("cost_currency") or "CNY").upper() if held else None
                    if held is None or float(held.get("units", 0) or 0) <= 0:
                        raise ValueError(f"{symbol} 当前没有持仓，不能再投资（reinvest）")
                    if ccy != held_ccy:
                        raise ValueError(f"{symbol} 持仓按 {held_ccy} 计价，再投资的分红币种必须同为 {held_ccy}（当前 {ccy}）")
                    units = round(net / price, 6)
                    if units <= 0:
                        raise ValueError("分红净额太小，再投资买不到 0.000001 份")
                    cost = units * price
                    old_units, old_avg = float(held.get("units", 0) or 0), float(held.get("avg_cost", 0) or 0)
                    held["units"] = round(old_units + units, 6)
                    held["avg_cost"] = round((old_units * old_avg + cost) / (old_units + units), 6)
                    cash[ccy] = round(cash[ccy] - cost, 2)
                    p["holdings"] = holdings
                    reinvested = {"units": units, "price": price, "cost": round(cost, 2)}
                p["cash"] = cash
        except _DuplicateKey:
            return _duplicate(idempotency_key)
        self._reload()
        self.store.append_history(_activity_row(
            "dividend", currency=ccy, day=day, source=source, key=idempotency_key, note=note,
            symbol=symbol, amount=gross, tax_withheld=tax or None, cash_effect=net, units_effect=0.0,
            reinvest=bool(reinvest)))
        if reinvested:
            cost = reinvested["units"] * reinvested["price"]
            self.store.append_history({
                "ts_origin": _now_iso_local(), "schema_version": 2, "action": "buy", "symbol": symbol,
                "units": reinvested["units"], "price": reinvested["price"], "currency": ccy,
                "source": "drip", "funding_source": "cash_deduct", "external": False,
                "cash_effect": -cost, "units_effect": reinvested["units"], "trade_date": day,
                **({"idempotency_key": f"{idempotency_key}#drip"} if idempotency_key else {}),
                "note": f"DRIP: reinvested {symbol} dividend",
            })
        out: Dict[str, Any] = {
            "status": "ok", "action": "dividend", "symbol": symbol, "gross": gross, "tax_withheld": tax,
            "net_cash": net, "currency": ccy, "trade_date": day, "reinvested": reinvested,
            "new_balance": self.cash_amount(ccy),
        }
        if not still_held:
            out["note"] = f"{symbol} 当前已无持仓（账本里有它的记录）：只记了分红现金"
        return out

    def record_interest(
        self,
        amount: float,
        currency: str,
        symbol: Optional[str] = None,
        trade_date: Optional[str] = None,
        note: Optional[str] = None,
        *,
        source: str = "skill_cli",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记一笔利息（货币基金 / 现金账户利息）：现金 +amount，内部流。symbol 可空（账户级）。"""
        ccy = _clean_ccy(currency)
        amount = float(amount)
        if not amount > 0:
            raise ValueError("amount 必须 > 0")
        day = _activity_day(trade_date)
        try:
            with self._ledger_tx(idempotency_key) as p:
                cash = dict(p.get("cash") or {})
                cash[ccy] = round(float(cash.get(ccy, 0) or 0) + amount, 2)
                p["cash"] = cash
        except _DuplicateKey:
            return _duplicate(idempotency_key)
        self._reload()
        self.store.append_history(_activity_row(
            "interest", currency=ccy, day=day, source=source, key=idempotency_key, note=note,
            symbol=(symbol or "").strip() or None, amount=amount, cash_effect=amount, units_effect=0.0))
        return {"status": "ok", "action": "interest", "amount": amount, "currency": ccy, "trade_date": day,
                "new_balance": self.cash_amount(ccy)}

    def record_fee(
        self,
        amount: float,
        currency: str,
        symbol: Optional[str] = None,
        kind: str = "fee",
        trade_date: Optional[str] = None,
        note: Optional[str] = None,
        *,
        source: str = "skill_cli",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记一笔费用（kind="fee"：账户管理费 / 汇款费 / 单独收的佣金）或税（kind="tax"：红利税补缴等）：
        现金 −amount，内部流（是账户的成本，进 NAV）。symbol 可空（账户级）。
        买卖自带的手续费不用单记——buy / sell 已按成交额入账。余额不足抛 ValueError（同 withdraw）。"""
        if kind not in ("fee", "tax"):
            raise ValueError("kind 只接受 fee / tax")
        ccy = _clean_ccy(currency)
        amount = float(amount)
        if not amount > 0:
            raise ValueError("amount 必须 > 0")
        day = _activity_day(trade_date)
        try:
            with self._ledger_tx(idempotency_key) as p:
                cash = dict(p.get("cash") or {})
                current = float(cash.get(ccy, 0) or 0)
                if current < amount:
                    raise ValueError(f"{ccy} 现金 {current} < {kind} {amount}（先 deposit 补足现金，或核对金额）")
                cash[ccy] = round(current - amount, 2)
                p["cash"] = cash
        except _DuplicateKey:
            return _duplicate(idempotency_key)
        self._reload()
        self.store.append_history(_activity_row(
            kind, currency=ccy, day=day, source=source, key=idempotency_key, note=note,
            symbol=(symbol or "").strip() or None, amount=amount, cash_effect=-amount, units_effect=0.0))
        return {"status": "ok", "action": kind, "amount": amount, "currency": ccy, "trade_date": day,
                "new_balance": self.cash_amount(ccy)}

    def record_split(
        self,
        symbol: str,
        ratio: float,
        trade_date: Optional[str] = None,
        note: Optional[str] = None,
        *,
        ledger_only: bool = False,
        source: str = "skill_cli",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记一次拆股 / 并股：units × ratio，avg_cost ÷ ratio，现金不变（市值不变）。
        ratio = 新 / 旧：10 拆 1 股变 10 股写 10，10 合 1 写 0.1。``trade_date`` = 除权日（账户业绩把拆股前的买卖数量
        换算成拆股后口径要靠它，补记历史拆股请传真实日期）。symbol 已清仓但账本里有它也记（快照无变化，只记流水）；
        完全没见过的 symbol 抛 ValueError（防写错）。

        快照是**当前**份数，整体 × ratio 只在"快照里没有混进拆股后口径的份数"时才对。所以账本里已有该标的在
        ``trade_date`` 当天或之后的买卖 / 修正（它们是按拆股后口径记的）→ 拒绝并指路：确认快照份数本来就是拆股后的
        （例如你已手动改过），传 ``ledger_only=True``——只补流水、不动快照（账户业绩照样据此换算拆股前的数量）。
        检查在幂等闸之后、事务之内：已入账的同 key 重试仍返回 duplicate，被拒的调用不烧 key。"""
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol 必填")
        self._require_known_symbol(symbol)
        ratio = float(ratio)
        if not ratio > 0 or ratio == 1:
            raise ValueError("ratio 必须 > 0 且 ≠ 1（新 / 旧：10 拆 1 写 10，10 合 1 写 0.1）")
        day = _activity_day(trade_date)
        units_effect, held_ccy, found = 0.0, "CNY", False
        try:
            with self._ledger_tx(idempotency_key) as p:
                holdings = list(p.get("holdings") or [])
                held = next((h for h in holdings if h.get("symbol") == symbol), None)
                if held is not None:
                    found, held_ccy = True, str(held.get("cost_currency") or "CNY").upper()
                if not ledger_only:
                    # 只有会动到快照份数时才需要这道检查（已清仓 / 没有持仓行 → 快照本来就不变）
                    later = self._units_changes_since(symbol, day) if held is not None and float(held.get("units", 0) or 0) else []
                    if later:
                        raise ValueError(
                            f"{symbol} 在 {day} 当天或之后已有 {len(later)} 笔份数变动（{later[0]} 等）：它们是按拆股后口径记的，"
                            f"快照份数已混着拆股后的部分，整体 × {ratio:g} 会凭空多出份数，账本没有撤销。"
                            f"如果快照份数本来就是拆股后的，加 ledger_only（CLI --ledger-only）只补流水不动快照；"
                            f"否则先核对份数，或把除权日改成最后一笔份数变动之后")
                    if held is not None:
                        old_units, old_avg = float(held.get("units", 0) or 0), float(held.get("avg_cost", 0) or 0)
                        held["units"] = round(old_units * ratio, 6)
                        held["avg_cost"] = round(old_avg / ratio, 6)
                        units_effect = held["units"] - old_units
                        p["holdings"] = holdings
        except _DuplicateKey:
            return _duplicate(idempotency_key)
        self._reload()
        self.store.append_history(_activity_row(
            "split", currency=held_ccy, day=day, source=source, key=idempotency_key, note=note,
            symbol=symbol, split_ratio=ratio, cash_effect=0.0, units_effect=units_effect,
            ledger_only=True if ledger_only else None))
        out: Dict[str, Any] = {"status": "ok", "action": "split", "symbol": symbol, "ratio": ratio,
                               "trade_date": day, "units_effect": units_effect,
                               # 拆股后 Yahoo 会把全部历史重新按新口径给价；行情库 2 年以前的老行不会被增量刷新碰到
                               "follow_up": f"拆股后跑一次 `openinvest backfill_raw_close --symbols {symbol}`，"
                                            f"把行情库里 2 年以前的 close_raw 刷成拆股后口径（账户成立日早于 2 年前才有影响）"}
        if ledger_only:
            out["note"] = "ledger_only：只记了流水，快照份数 / 成本没动"
        elif found:
            h = self.find_holding(symbol) or {}
            out.update(units=h.get("units"), avg_cost=h.get("avg_cost"))
        else:
            out["note"] = f"{symbol} 当前已无持仓：快照没有变化，只记了流水（账户业绩据此换算拆股前的数量）"
        return out

    def _units_changes_since(self, symbol: str, day: str) -> List[str]:
        """账本里该标的在 ``day``（含）之后改变过份数的活动，形如 ``"2026-07-01 buy"``（split 自己不算：
        它们是乘法，后面再补记更早的拆股时，快照里已有的后拆股效应不冲突）。"""
        from openinvest.calc.ledger_replay import replay_ledger
        acts, _ = replay_ledger(self.store.read_history(), alias={})
        return [f"{a.trade_date} {a.action}" for a in acts
                if a.replayable and a.symbol == symbol and a.action != "split"
                and (a.trade_date or "") >= day and a.units_effect != 0]

    def record_return_of_capital(
        self,
        symbol: str,
        amount: float,
        currency: str,
        trade_date: Optional[str] = None,
        note: Optional[str] = None,
        *,
        source: str = "skill_cli",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记一笔退回本金（return of capital，如部分 ETF / REIT 分配）：现金 +amount，avg_cost −= amount / 持仓量
        （成本降低，不是收益），数量不变。持仓必须存在，``currency`` 必须等于它的计价币种（否则成本会混币种）。
        降到成本为 0 为止，超出部分按资本利得处理（返回 ``excess_over_cost``）。"""
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("symbol 必填")
        ccy = _clean_ccy(currency)
        amount = float(amount)
        if not amount > 0:
            raise ValueError("amount 必须 > 0")
        day = _activity_day(trade_date)
        excess = 0.0
        try:
            with self._ledger_tx(idempotency_key) as p:
                holdings = list(p.get("holdings") or [])
                held = next((h for h in holdings if h.get("symbol") == symbol), None)
                units = float(held.get("units", 0) or 0) if held else 0.0
                if held is None or units <= 0:
                    raise ValueError(f"{symbol} 当前没有持仓，无法降低成本")
                held_ccy = str(held.get("cost_currency") or "CNY").upper()
                if ccy != held_ccy:
                    raise ValueError(f"{symbol} 持仓按 {held_ccy} 计价，退回本金币种必须同为 {held_ccy}（当前 {ccy}）")
                old_avg = float(held.get("avg_cost", 0) or 0)
                new_avg = old_avg - amount / units
                if new_avg < 0:
                    excess, new_avg = round(-new_avg * units, 2), 0.0
                held["avg_cost"] = round(new_avg, 6)
                p["holdings"] = holdings
                cash = dict(p.get("cash") or {})
                cash[ccy] = round(float(cash.get(ccy, 0) or 0) + amount, 2)
                p["cash"] = cash
        except _DuplicateKey:
            return _duplicate(idempotency_key)
        self._reload()
        self.store.append_history(_activity_row(
            "return_of_capital", currency=ccy, day=day, source=source, key=idempotency_key, note=note,
            symbol=symbol, amount=amount, cash_effect=amount, units_effect=0.0))
        out: Dict[str, Any] = {"status": "ok", "action": "return_of_capital", "symbol": symbol, "amount": amount,
                               "currency": ccy, "trade_date": day,
                               "avg_cost": (self.find_holding(symbol) or {}).get("avg_cost")}
        if excess:
            out["excess_over_cost"] = excess
        return out

    # ---------- ADR-027 PR3：场外基金两段式（下单扣现金记在途 → 确认日按净值到账）----------
    # 订单存在基金持仓行的 ``extra.pending_orders[]``（与现金同一次 atomic write）：placed → confirmed | cancelled。
    # 状态转换是 CAS（检查与写入在同一个 with_portfolio_tx 里，ADR-016 #127 同款）：手动 confirm 与 fund_confirm job 并发
    # 只有一个生效，另一个读到非 placed 就原样返回 already_confirmed / cancelled，不改任何东西。
    # 在途金额对所有估值方可见的唯一读口是 :meth:`pending_fund_orders`（utils.fx.total_portfolio_value 经它按面值计入）。

    def pending_fund_orders(self) -> List[Dict[str, Any]]:
        """所有 ``status=placed`` 的在途申购单（浅拷贝，每条带 ``symbol``）。估值 / status / 业绩读在途的**唯一**入口——
        不允许任何调用方自己去读 ``extra.pending_orders``。"""
        return [{**o, "symbol": h.get("symbol")} for h in self.holdings for o in _pending_of(h)
                if o.get("status") == "placed"]

    def place_fund_order(
        self,
        symbol: str,
        amount: float,
        currency: str = "CNY",
        order_ts: Any = None,
        note: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        source: str = "skill_cli",
    ) -> Dict[str, Any]:
        """场外基金申购第一段：现金 −amount，订单以 ``placed`` 记在基金持仓行的 ``extra.pending_orders``（没有这只基金就建一行
        ``kind=fund`` ``units=0``），流水记 ``fund_order_placed``（现金 → 在途，不是外部现金流）。份额等确认日按净值到账
        （:meth:`confirm_fund_order` / ``fund_confirm`` job）。总资产在这一步前后不变——在途金额按面值计入。

        - ``symbol``：``FUND:<六位代码>``（也认裸六位 / ``.OF``）；``currency`` 只接受该基金持仓的计价币种（场外基金 = CNY）
        - ``order_ts``：下单时刻（datetime 或 ISO 字符串，缺省现在；naive 按北京时间）。15:00 前按当日净值、之后按下一交易日
          （定价日 ``nav_date``）；预计确认日 = 定价日 + lag 个交易日，lag 按基金类型取 ``fund.confirm_lag_days``，类型从东方财富
          基金概况推断（QDII / FOF），取不到按 default 并标 ``lag_source=default``；交易日历只有周一到周五（``calendar=weekday_only``）
        - 基金概况在锁外联网取；取不到不挡下单
        - idempotency_key 语义同 buy（ADR-016）。非法参数 / 现金不足抛 ValueError。"""
        import uuid

        from openinvest.calc import fund_rules as fr
        from openinvest.core.config import load_config
        from openinvest.utils.eastmoney_fund import canonical_fund_symbol, extract_fund_code, fetch_fund_profile
        from openinvest.utils.quotes import is_eastmoney_fund

        raw = str(symbol or "").strip()
        code = extract_fund_code(raw)
        # 带 .SS/.SZ 后缀的六位码是交易所标的（如 510300.SS 沪深300 ETF），不是场外基金；
        # 只有已有 kind=fund 的老行（历史误标 123456.SZ）才认
        legacy_row = any(x.get("symbol") == raw and is_eastmoney_fund(x) for x in self.holdings)
        if code is None or (raw.upper().endswith((".SS", ".SZ")) and not legacy_row):
            raise ValueError(f"{symbol!r} 不是场外基金代码（写 FUND:<六位代码>）；沪深交易所的 ETF / 股票请用 buy")
        sym = f"FUND:{code}"
        ccy = _clean_ccy(currency)
        amount = round(float(amount), 2)
        if not amount > 0:
            raise ValueError("amount 必须 > 0")
        ts = _parse_order_ts(order_ts)
        profile = fetch_fund_profile(sym)            # 联网：必须在锁外
        kind = fr.classify_fund_kind(*profile.values()) if profile else "default"
        lag = fr.resolve_lag_days(kind if profile else None, load_config().fund.confirm_lag_days)
        order = {
            "order_id": uuid.uuid4().hex, "amount": amount, "currency": ccy,
            "order_date": ts.date().isoformat(), "order_ts": ts.isoformat(timespec="seconds"),
            "expected_confirm_date": fr.expected_confirm_date(ts, lag).isoformat(),
            "nav_date": fr.pricing_date(ts).isoformat(), "status": "placed",
            "calendar": fr.CALENDAR_WEEKDAY_ONLY, "fund_kind": kind, "lag_days": lag,
            "lag_source": "profile" if profile else "default",
            **({"note": note} if note else {}),
        }
        try:
            with self._ledger_tx(idempotency_key) as p:
                holdings = list(p.get("holdings") or [])
                h = next((x for x in holdings if canonical_fund_symbol(str(x.get("symbol") or "")) == sym
                          and (str(x.get("symbol") or "").upper().startswith("FUND:") or is_eastmoney_fund(x))), None)
                held_ccy = str(h.get("cost_currency") or "CNY").upper() if h else "CNY"
                if ccy != held_ccy:
                    raise ValueError(f"场外基金按人民币净值计价：{sym} 的申购币种必须是 {held_ccy}（当前 {ccy}）")
                cash = dict(p.get("cash") or {})
                current = float(cash.get(ccy, 0) or 0)
                if current < amount:
                    raise ValueError(f"{ccy} 现金 {current} < 申购金额 {amount}（子弹不足）")
                cash[ccy] = round(current - amount, 2)
                if h is None:
                    h = {"symbol": sym, "kind": "fund", "units": 0.0, "unit_label": "份", "avg_cost": 0.0,
                         "cost_currency": "CNY", "proxy_kind": "eastmoney_fund"}
                    holdings.append(h)
                else:
                    h.update(kind="fund", proxy_kind="eastmoney_fund")   # symbol 是 FUND: 的行一定是场外基金口径
                    if h.get("is_tracking_only"):
                        h["is_tracking_only"] = False                    # 钱已经进去了，不再是追踪仓
                booked = str(h["symbol"])   # 老行可能是 123456.SZ 这类误标写法：流水 / 返回都用持仓行自己的 symbol，回放才对得上
                h["extra"] = {**(h.get("extra") or {}), _PENDING_KEY: _keep_recent(_pending_of(h) + [order])}
                p["holdings"] = holdings
                p["cash"] = cash
        except _DuplicateKey:
            return _duplicate(idempotency_key)
        self._reload()
        self.store.append_history(_activity_row(
            "fund_order_placed", currency=ccy, day=order["order_date"], source=source, key=idempotency_key, note=note,
            symbol=booked, amount=amount, cash_effect=-amount, units_effect=0.0, settle_date=order["expected_confirm_date"],
            nav_date=order["nav_date"], order_id=order["order_id"], calendar=order["calendar"], fund_kind=kind,
            lag_days=lag, lag_source=order["lag_source"]))
        return {
            "status": "ok", "action": "fund_order_placed", "symbol": booked, **{
                k: order[k] for k in ("order_id", "amount", "currency", "order_date", "nav_date",
                                      "expected_confirm_date", "calendar", "fund_kind", "lag_days", "lag_source")},
            "new_balance": self.cash_amount(ccy),
            "note": ("预计确认日按周一到周五估计（没有 A 股节假日表）"
                     + ("；基金类型没取到，按默认延迟估计" if not profile else "")
                     + "。份额由 fund_confirm job 在定价日净值公布后自动确认，也可手动 confirm_fund_order。"),
        }

    def confirm_fund_order(
        self,
        order_id: str,
        nav: Optional[float] = None,
        fee: float = 0.0,
        *,
        confirm_date: Optional[str] = None,
        source: str = "skill_cli",
    ) -> Dict[str, Any]:
        """场外基金申购第二段：把在途单按净值换成份额。``units += (amount − fee) / nav``，``avg_cost`` 加权（成本含申购费），
        订单 → ``confirmed``，流水记 ``fund_order_confirmed``（在途 → 份额，现金不动）。

        - ``nav`` 缺省：取东方财富历史净值里订单定价日（``nav_date``）的单位净值；定价日净值还没公布 → ``{"status": "pending_nav"}``
          什么都不改（job 下一次再试）；定价日休市 / 无净值而更晚的净值已公布 → 顺延用之后第一个净值（返回里标 ``nav_date_planned``）
        - ``fee``：申购费（从订单金额里扣，不另扣现金）
        - ``confirm_date``：确认动作的发生日（缺省今天，北京时间；账户业绩按它把份额记进 NAV）
        - **CAS**：单子已确认 → ``already_confirmed``、已取消 → ``cancelled``，原样返回、不改任何东西、不报错；
          订单不存在抛 ValueError。联网取净值在锁外，状态检查与写入在同一个 with_portfolio_tx 里。"""
        from openinvest.calc import fund_rules as fr
        from openinvest.utils.eastmoney_fund import fetch_fund_nav_history

        fee = float(fee or 0)
        if not math.isfinite(fee) or fee < 0:
            raise ValueError("fee 必须 ≥ 0")
        if nav is not None:
            nav = float(nav)
            if not (math.isfinite(nav) and nav > 0):
                raise ValueError("nav 必须 > 0")
        day = _activity_day(confirm_date) if confirm_date else datetime.now(fr.CN_TZ).date().isoformat()
        self._reload()   # 别的进程（job / 另一个 agent）可能刚写过
        sym, order = self._locate_order(order_id)
        if order.get("status") != "placed":
            return _terminal_order_result(sym, order)
        planned, used_date = str(order.get("nav_date") or ""), str(order.get("nav_date") or "")
        if nav is None:
            history = fetch_fund_nav_history(sym)       # 联网：必须在锁外；未复权的真实单位净值
            picked = fr.pick_nav(history or [], planned)
            if picked is None:
                return {"status": "pending_nav", "order_id": order_id, "symbol": sym, "nav_date": planned,
                        "reason": "nav_not_published" if history else "nav_fetch_failed",
                        "note": f"{planned} 的单位净值还没公布" if history else "净值接口取不到，稍后重试或手填 nav"}
            used_date, nav = picked
        try:
            with self.with_portfolio_tx() as p:
                holdings = list(p.get("holdings") or [])
                h, o = _find_order(holdings, order_id)
                if o is None:
                    raise ValueError(f"找不到在途单 {order_id}")
                if o.get("status") != "placed":         # CAS：别人先到了
                    raise _NoChange(_terminal_order_result(str(h.get("symbol")), o))
                amount, ccy = float(o["amount"]), str(o.get("currency") or "CNY").upper()
                if fee >= amount:
                    raise ValueError(f"fee {fee} 必须小于申购金额 {amount}")
                units = round((amount - fee) / nav, 6)
                if not units > 0:
                    raise ValueError("申购金额太小，换不到 0.000001 份")
                old_units, old_avg = float(h.get("units", 0) or 0), float(h.get("avg_cost", 0) or 0)
                new_units = old_units + units
                h["units"] = round(new_units, 6)
                h["avg_cost"] = round((old_units * old_avg + amount) / new_units, 6)
                o.update(status="confirmed", confirmed_on=day, nav=nav, nav_date=used_date, units=units,
                         **({"fee": fee} if fee else {}))
                if used_date != planned:
                    o["nav_date_planned"] = planned
                h["extra"] = {**(h.get("extra") or {}), _PENDING_KEY: _keep_recent(_pending_of(h))}
                p["holdings"] = holdings
                expected, units_now, avg_now = o.get("expected_confirm_date"), h["units"], h["avg_cost"]
        except _NoChange as nc:
            return nc.result
        self._reload()
        self.store.append_history(_activity_row(
            "fund_order_confirmed", currency=ccy, day=day, source=source, key=None, note=None,
            symbol=sym, amount=amount, fee=fee or None, price=nav, units=units, cash_effect=0.0, units_effect=units,
            settle_date=expected, nav_date=used_date, order_id=order_id))
        out: Dict[str, Any] = {
            "status": "ok", "action": "fund_order_confirmed", "order_id": order_id, "symbol": sym, "amount": amount,
            "fee": fee, "nav": nav, "nav_date": used_date, "units": units, "total_units": units_now,
            "avg_cost": avg_now, "confirm_date": day,
        }
        if used_date != planned:
            out["nav_date_planned"] = planned
            out["note"] = f"{planned} 没有净值（休市 / 节假日），按之后第一个已公布的净值日 {used_date} 确认"
        return out

    def cancel_fund_order(self, order_id: str, *, cancel_date: Optional[str] = None,
                          source: str = "skill_cli") -> Dict[str, Any]:
        """撤销在途申购单：只有 ``placed`` 能撤，现金 +amount（在途退回），流水记 ``fund_order_cancelled``。
        已确认抛 ValueError（份额已到账，要卖请走 sell）；已撤销 → ``{"status": "cancelled"}`` 原样返回（幂等）。"""
        from openinvest.calc import fund_rules as fr
        day = _activity_day(cancel_date) if cancel_date else datetime.now(fr.CN_TZ).date().isoformat()
        self._reload()
        sym, order = self._locate_order(order_id)
        if order.get("status") == "cancelled":
            return _terminal_order_result(sym, order)
        try:
            with self.with_portfolio_tx() as p:
                holdings = list(p.get("holdings") or [])
                h, o = _find_order(holdings, order_id)
                if o is None:
                    raise ValueError(f"找不到在途单 {order_id}")
                if o.get("status") == "cancelled":
                    raise _NoChange(_terminal_order_result(sym, o))
                if o.get("status") != "placed":
                    raise ValueError(f"在途单 {order_id} 已确认（{o.get('units')} 份已到账），不能撤销；要卖出请用 sell")
                amount, ccy = float(o["amount"]), str(o.get("currency") or "CNY").upper()
                o.update(status="cancelled", cancelled_on=day)
                h["extra"] = {**(h.get("extra") or {}), _PENDING_KEY: _keep_recent(_pending_of(h))}
                cash = dict(p.get("cash") or {})
                cash[ccy] = round(float(cash.get(ccy, 0) or 0) + amount, 2)
                p["holdings"] = holdings
                p["cash"] = cash
        except _NoChange as nc:
            return nc.result
        self._reload()
        self.store.append_history(_activity_row(
            "fund_order_cancelled", currency=ccy, day=day, source=source, key=None, note=None,
            symbol=sym, amount=amount, cash_effect=amount, units_effect=0.0, order_id=order_id))
        return {"status": "ok", "action": "fund_order_cancelled", "order_id": order_id, "symbol": sym,
                "amount": amount, "currency": ccy, "new_balance": self.cash_amount(ccy)}

    def _locate_order(self, order_id: str) -> Tuple[str, Dict[str, Any]]:
        """(基金 symbol, 订单 dict)——读当前快照视图；找不到抛 ValueError。"""
        h, o = _find_order(list(self.portfolio.get("holdings") or []), order_id)
        if o is None:
            raise ValueError(f"找不到在途单 {order_id}（status 的 pending_fund_orders 里有当前在途单；已终结的单子只保留最近 {_KEEP_TERMINAL} 条）")
        return str(h.get("symbol")), o

    # ---------- 内部 ----------

    def _reload(self) -> None:
        """写入后重新读，保证下一次访问看到最新数据"""
        self.user = self.store.read("user")  # type: ignore[assignment]
        self.strategy = self.store.read("strategy")  # type: ignore[assignment]
        self.portfolio = self.store.read("portfolio")  # type: ignore[assignment]


# ============ 工具函数 ============

# 黄金（GC=F）只有一种合法记法：CNY / 克。全仓估值（status / what_if / committee / daily_report）
# 都把 GC=F 当 CNY/克（GC=F USD/oz ÷ 31.1035 × USDCNY 反推）；账里记成 USD/oz 会被拿去跟克价比，
# 假浮亏 −78%（#443）；记成 CNY/克却没带 proxy_kind 又会被当 USD/oz 拉，假浮盈 +375%。
_GOLD_SYMBOL = "GC=F"
_GOLD_FIELDS = {"kind": "metal", "unit_label": "克", "proxy_kind": "gold_cny_per_gram",
                "yfinance_proxy": _GOLD_SYMBOL}
# "股" = buy 的默认 unit_label（调用方没指定单位），按克处理；oz / 盎司 / ounce 等显式其他单位一律拒收。
_GRAM_LABELS = {"克", "g", "gram", "grams", "股"}


def gold_booking(symbol: str, ccy: str, unit_label: str, held: Optional[str]) -> Optional[Dict[str, str]]:
    """GC=F 的入口校验（buy / record_existing_position 共用）：非 GC=F 返回 None；
    合法（CNY + 克）返回要落账的 kind / unit_label / proxy_kind 字段；其余抛 ValueError 并给正确写法。

    选「拒绝」而不是自动换算 USD/oz → CNY/克：换算要猜汇率，且 USD 买入扣的是 USD 现金、
    CNY 记账扣的是 CNY 现金，悄悄改币种就是悄悄记错账（#402：不编汇率）；拒绝 + 示例让调用方重发一次就对了。
    """
    if symbol.strip().upper() != _GOLD_SYMBOL:
        return None
    how = (f"黄金必须按「人民币 / 克」记账：price 填 CNY/克、currency=CNY、unit_label=克，"
           f"如 buy {symbol} --units 5 --price 880 -c CNY --kind metal --unit-label 克"
           f"（MCP: symbol='{symbol}', units=5, price=880, currency='CNY', kind='metal', unit_label='克'）；"
           f"美元/盎司报价先折成 CNY/克（USD/oz ÷ 31.1035 × USDCNY）")
    if held and held != "CNY":
        raise ValueError(
            f"{symbol} 现有持仓按 {held} 记账（旧版本遗留的错误口径，会被估成假浮亏）。"
            f"先 delete_holding --symbol {symbol} --force 删掉，再按 CNY/克重新记。{how}")
    if ccy != "CNY":
        raise ValueError(f"{symbol} 不接受 {ccy} 计价。{how}")
    if unit_label.strip().lower() not in _GRAM_LABELS:
        raise ValueError(f"{symbol} 不接受单位 {unit_label!r}。{how}")
    return dict(_GOLD_FIELDS)


# 账本幂等闸的去重键表：portfolio.md frontmatter 的 list 字段（ADR-016），与账本同一次 atomic write。
# ponytail: 只记"见过的 key"不缓存上次响应；重试在秒~分钟内发生，留 500 个绰绰有余。
_LEDGER_KEYS = "ledger_idempotency_keys"
_LEDGER_KEYS_KEEP = 500


class _DuplicateKey(Exception):
    """同 idempotency_key 已入账 —— 在 portfolio 锁内抛出，中止事务（不提交）。"""


def _duplicate(key: Optional[str]) -> Dict[str, Any]:
    return {
        "status": "duplicate", "idempotency_key": key,
        "note": "同 idempotency_key 的这笔已入账过，本次未重复记账（ADR-016）。key 只用于重试"
                "完全相同的请求；若这是另一笔（换了标的 / 数量 / 金额），请换一个新 key 重发。",
    }


def _clean_ccy(currency: str) -> str:
    ccy = str(currency or "").strip().upper()
    if not (3 <= len(ccy) <= 5) or not ccy.isalpha():
        raise ValueError(f"非法币种 {ccy}")
    return ccy


def _activity_day(trade_date: Optional[str]) -> str:
    """活动发生日 YYYY-MM-DD（本地日期）；缺省今天，格式不对抛 ValueError。"""
    if trade_date is None or str(trade_date).strip() == "":
        return datetime.now().date().isoformat()
    try:
        return date.fromisoformat(str(trade_date).strip()).isoformat()
    except ValueError:
        raise ValueError(f"trade_date 必须是 YYYY-MM-DD: {trade_date!r}") from None


def _activity_row(action: str, *, currency: str, day: str, source: str, key: Optional[str], note: Optional[str],
                  **fields: Any) -> Dict[str, Any]:
    """ADR-027 D2 的新流水记录：schema_version=2，cash_effect / units_effect / external 由写入方算好。
    这些活动都是内部流（external=False）；None 字段不落盘。"""
    row: Dict[str, Any] = {
        "ts_origin": _now_iso_local(), "schema_version": 2, "action": action, "currency": currency,
        "trade_date": day, "source": source, "external": False,
        **{k: v for k, v in fields.items() if v is not None},
        **({"idempotency_key": key} if key else {}), **({"note": note} if note else {}),
    }
    return row


def _stored_raw_close(pm: "PortfolioManager", symbol: str, day: str) -> Optional[float]:
    """DRIP 缺省单价：行情库该标的 ``day`` 当天（往前最多 7 天）的原始收盘价（close_raw）。库里没有 → None。"""
    from openinvest.db.market_store import MarketStore
    held = pm.find_holding(symbol) or {}
    store = MarketStore()
    try:
        return store.get_raw_close_on(str(held.get("yfinance_proxy") or symbol), day)
    finally:
        store.conn.close()


_PENDING_KEY = "pending_orders"
_KEEP_TERMINAL = 20   # 每个基金行保留最近 N 条已终结（confirmed / cancelled）的订单：CAS 要靠它回答 already_confirmed
_TERMINAL_STATUS = {"confirmed": "already_confirmed", "cancelled": "cancelled"}


class _NoChange(Exception):
    """CAS 读到订单已不是 placed —— 在锁内抛出，中止事务（不提交、不重写文件），``result`` 原样返回给调用方。"""
    def __init__(self, result: Dict[str, Any]):
        super().__init__(result.get("status"))
        self.result = result


def _pending_of(h: Dict[str, Any]) -> List[Dict[str, Any]]:
    """基金持仓行的订单列表（所有状态；``holding.extra.pending_orders``）。"""
    return [o for o in ((h.get("extra") or {}).get(_PENDING_KEY) or []) if isinstance(o, dict)]


def _keep_recent(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """在途单全留，已终结的只留最近 N 条（订单是追加的，后面的更新）。"""
    live = [o for o in orders if o.get("status") == "placed"]
    done = [o for o in orders if o.get("status") != "placed"][-_KEEP_TERMINAL:]
    keep = {id(o) for o in live + done}
    return [o for o in orders if id(o) in keep]


def _find_order(holdings: List[Dict[str, Any]], order_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    for h in holdings:
        for o in _pending_of(h):
            if o.get("order_id") == order_id:
                return h, o
    return None, None


def _terminal_order_result(symbol: str, o: Dict[str, Any]) -> Dict[str, Any]:
    """CAS 失败（订单已终结）时原样返回的结果：不改账，调用方据 status 判断。"""
    status = _TERMINAL_STATUS.get(str(o.get("status")), str(o.get("status")))
    out: Dict[str, Any] = {"status": status, "order_id": o.get("order_id"), "symbol": symbol, "amount": o.get("amount")}
    if status == "already_confirmed":
        out.update(units=o.get("units"), nav=o.get("nav"), nav_date=o.get("nav_date"), confirmed_on=o.get("confirmed_on"),
                   note="这笔单已确认过，本次未重复入账")
    else:
        out.update(cancelled_on=o.get("cancelled_on"), note="这笔单已撤销，本次未改账")
    return out


def _parse_order_ts(v: Any) -> datetime:
    """下单时刻 → 带北京时区的 datetime。None = 现在；datetime / ISO 字符串（naive 按北京时间，只有日期 = 当日 00:00）。"""
    from openinvest.calc import fund_rules as fr
    if v is None or v == "":
        return datetime.now(fr.CN_TZ)
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.strip())
        except ValueError:
            raise ValueError(f"order_ts 必须是 ISO 时间（如 2026-10-19T14:30:00，按北京时间）: {v!r}") from None
    if not isinstance(v, datetime):
        raise ValueError(f"order_ts 必须是 datetime 或 ISO 字符串: {v!r}")
    return fr.to_cn(v)


def _fund_buy_warning(pm: "PortfolioManager", symbol: str, kind: str) -> Optional[str]:
    """buy 记场外基金（现金扣款）时的提醒：这样记等于"下单即成交"，下单到确认之间总资产会假跌——建议改用两段式。不拒绝。"""
    from openinvest.utils.eastmoney_fund import extract_fund_code
    from openinvest.utils.quotes import is_eastmoney_fund
    held = pm.find_holding(symbol)
    if (held is not None and is_eastmoney_fund(held)) or (
            extract_fund_code(symbol) is not None and (str(kind).lower() == "fund" or symbol.upper().startswith("FUND:"))):
        return (f"{symbol} 是场外基金：buy 按「下单即成交」记账，但实际是 T+1 左右按净值确认份额，"
                f"中间总资产会假跌一笔。建议改用 place_fund_order（下单扣现金记在途）+ confirm_fund_order（确认日按净值到账，"
                f"fund_confirm job 会自动确认）。本次已按 buy 记账")
    return None


def _now_iso_local() -> str:
    """本地时区 ISO 时间戳——history.jsonl 的 ts_origin 口径（与 scripts/skill.py 一致）"""
    from datetime import timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


_V1_KEYS = ("cash_cny", "aud_cash", "ndq_shares", "ndq_avg_cost_aud_per_share",
            "gold_grams", "gold_avg_cost_cny_per_gram")


def is_v1_portfolio(meta) -> bool:
    """v1 扁平格式 = 带 v1 字段、且没有 v2 的 cash{}。

    schema_version 缺省不算证据：QUICK_START 的手写 v2 模板就不写它——只看它会把
    合法 v2 文件判成 v1，转换脚本再把它的 cash 清空。doctor、写入拒绝、转换脚本共用这一个判据。
    """
    try:
        if int(meta.get("schema_version") or 1) >= 2:
            return False
    except (TypeError, ValueError):
        pass
    return any(k in meta for k in _V1_KEYS) and not isinstance(meta.get("cash"), dict)


def v1_migrate_command(store: MemoryStore) -> str:
    """把 v1 扁平 portfolio.md 转成 v2 的确切命令（doctor 的 portfolio_schema 提示与写入拒绝共用）"""
    import shlex
    import sys
    return (
        f"INVEST_HOME={shlex.quote(str(store.root.parent))} "
        f"{shlex.quote(sys.executable)} -m openinvest.migrate_portfolio_to_holdings"
    )


def _ensure_v2_inplace(p, store: MemoryStore) -> None:
    """transaction 入口：v1 扁平 portfolio.md（#191 修复前的 init 写的）拒绝写入。

    with_portfolio_tx commit 时 pop 掉 v1 字段并盖 schema_version=2——在 v1 文件上任何
    写（deposit / buy --existing-position 不查现金所以会成功）都会把 cash_cny/aud_cash
    永久抹掉，还让 doctor 的 portfolio_schema 检查和转换脚本都以为已是 v2。
    转换走 openinvest.migrate_portfolio_to_holdings（先备份）。
    """
    if is_v1_portfolio(p.metadata):
        raise ValueError(
            "portfolio.md 还是 v1 旧格式（现金在 cash_cny/aud_cash，status 显示 0）；"
            "直接写会丢掉这些字段，已拒绝，文件未改动。先跑 "
            f"`{v1_migrate_command(store)}` 转成 v2（先自动备份），再重试。"
        )


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

    in_transit = [(h, o) for h in holdings for o in _pending_of(h) if o.get("status") == "placed"]
    if in_transit:
        lines.append("## 在途申购（现金已扣、份额待确认；总资产按面值计入）")
        for h, o in in_transit:
            lines.append(f"- `{h['symbol']}`: {float(o.get('amount') or 0):,.2f} {o.get('currency', '')}，"
                         f"下单 {o.get('order_date')}，预计 {o.get('expected_confirm_date')} 确认")
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
        f"_schema_version: {p.get('schema_version', 2)}_  此文件由 daily_report / commsec_sync / web_api 自动更新；不要手动编辑。",
    ])
    return "\n".join(lines) + "\n"
