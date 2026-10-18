"""数据层 schema —— md frontmatter 的运行时类型约束（v2: 通用化）

v1 → v2 重构（2026-05-06）：
- portfolio.md 从「扁平写死字段」(cash_cny/aud_cash/ndq_shares/gold_*) 改成
  「通用 cash dict + holdings 数组」，支持任意币种和任意 yfinance symbol。
- schema_version 字段标记，迁移脚本 scripts/migrate_portfolio_to_holdings.py 跑一次。

设计目标：
- **写入前强校验**：写 portfolio/strategy/user.md 都先过 schema，越界字段立即 fail
- **读取容忍**：extra="allow" + 默认值，老数据不 break
- **不锁死物理存储**：md + frontmatter 仍是 source of truth，schema 只是写门口检查站
- **HTTP API 解耦**：connectors/web_api.py 的响应模型可以另写
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ============ strategy.md ============

class SymbolAlerts(BaseModel):
    """target_assets[].alerts：用户给单个标的设的报警线（C8，jobs/price_sentinel 每个 tick 判一次）。

    price_* 与 status 的 current_price 同口径（持仓按 cost_currency；黄金积存金 = CNY/克；
    只跟踪未持仓 = 行情源原价，如 GC=F 为 USD/oz）。*_pct 是**百分点**（3 = 3%），
    与 target_pct 的 0~1 小数不同。extra=forbid：拼错的键（price_blow）直接报错，不会静默永不触发。
    """
    model_config = ConfigDict(extra="forbid")

    price_above: Optional[float] = Field(None, gt=0, allow_inf_nan=False, description="现价 ≥ 该价 时报警")
    price_below: Optional[float] = Field(None, gt=0, allow_inf_nan=False, description="现价 ≤ 该价 时报警")
    day_drop_pct: Optional[float] = Field(None, gt=0, le=100, description="最新日线跌幅（相对前收）≥ 该百分点 时报警，每个交易日只报一次")
    day_rise_pct: Optional[float] = Field(None, gt=0, le=1000, description="最新日线涨幅（相对前收）≥ 该百分点 时报警，每个交易日只报一次")
    drawdown_from_cost_pct: Optional[float] = Field(None, gt=0, le=100, description="持仓浮亏（相对成本）≥ 该百分点 时报警；需持有且有成本")

    @model_validator(mode="after")
    def _price_band_ordered(self) -> "SymbolAlerts":
        if self.price_above is not None and self.price_below is not None and self.price_below >= self.price_above:
            raise ValueError(f"price_below ({self.price_below}) 必须小于 price_above ({self.price_above})，否则两条线永远同时触发")
        return self


class TargetAsset(BaseModel):
    """strategy.target_assets[] 中的单个资产配置（决策层 schema）"""
    model_config = ConfigDict(extra="allow")

    symbol: str = Field(..., min_length=1, description="标的代码（如 NDQ.AX, GC=F）")
    display_name: Optional[str] = Field(None, description="人类可读名")
    channel: Optional[str] = Field(None, description="交易渠道")
    max_single_invest_cny: float = Field(..., ge=0, le=1_000_000)
    price_offset_pct: Optional[float] = Field(None, ge=-0.1, le=0.1)
    sell_fee_pct: Optional[float] = Field(None, ge=0, le=0.05)
    alerts: Optional[SymbolAlerts] = Field(None, description="每标的报警线（价格线 / 日涨跌 / 成本回撤），哨兵按它触发")
    # target_pct（占总资产的 0~1 小数，rebalance_plan 读）刻意不在这里声明：手写的 strategy.md 里
    # 可能有 `target_pct: 30` 这类历史写法，声明 ge/le 会让每一次无关的 strategy 写入都被整份校验拒绝。
    # 范围 + Σ ≤ 股票目标由 services/strategy_write 只对「本次写入新改的值」校验（#449）。

    @field_validator("symbol")
    @classmethod
    def _strip_symbol(cls, v: str) -> str:
        return v.strip()


class StrategyData(BaseModel):
    """strategy.md frontmatter（去掉 name/type/updated 这些 MemoryStore 元字段）"""
    model_config = ConfigDict(extra="allow")

    target_allocation_stock: float = Field(..., ge=0, le=1)
    target_allocation_cash: float = Field(..., ge=0, le=1)
    target_assets: List[TargetAsset] = Field(..., min_length=1)

    # 旧字段兼容（v1 单资产时代）
    max_single_invest_cny: Optional[float] = None
    target_asset: Optional[str] = None

    @model_validator(mode="after")
    def _allocations_sum_to_one(self) -> "StrategyData":
        s = self.target_allocation_stock + self.target_allocation_cash
        if not (0.99 <= s <= 1.01):
            raise ValueError(
                f"target_allocation_stock + target_allocation_cash 必须 ≈ 1.0，"
                f"当前 = {s}（{self.target_allocation_stock} + {self.target_allocation_cash}）",
            )
        return self

    @model_validator(mode="after")
    def _no_duplicate_symbols(self) -> "StrategyData":
        symbols = [a.symbol for a in self.target_assets]
        dups = {s for s in symbols if symbols.count(s) > 1}
        if dups:
            raise ValueError(f"target_assets 有重复 symbol: {sorted(dups)}")
        return self


# ============ portfolio.md (v2 通用化结构) ============

# yfinance 支持的资产类型（PM 评审：合并 equity 和 equity_etf）
HoldingKind = Literal["equity", "etf", "metal", "crypto", "bond", "fund", "other"]

# 行情代理类型：
# - direct: 直接用 symbol 拉 yfinance（绝大多数 yfinance 支持的资产）
# - gold_cny_per_gram: GC=F + USDCNY=X 反推克价（浙商积存金这类 CNY 计价但 yfinance 没有的）
# - fx_pair: 货币对（如果有需要）
ProxyKind = Literal["direct", "gold_cny_per_gram", "fx_pair", "eastmoney_fund"]


class Holding(BaseModel):
    """单个持仓 —— 通用化后支持任意 yfinance symbol"""
    model_config = ConfigDict(extra="allow")

    symbol: str = Field(
        ...,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9._=^:\-]+$",
        description="yfinance symbol（AAPL, NDQ.AX, GC=F, BTC-USD 等）",
    )
    kind: HoldingKind = Field(..., description="资产类型")
    units: float = Field(default=0.0, ge=0, description="持仓数量；追踪仓时 = 0")
    unit_label: str = Field(default="share", max_length=8, description="单位（股/克/oz）")
    avg_cost: float = Field(default=0.0, ge=0, description="加权均价（cost_currency 计价）")
    cost_currency: str = Field(
        ...,
        pattern=r"^[A-Z]{3,5}$",
        description="均价计价币种 ISO 4217 三/四字母",
    )
    channel: Optional[str] = Field(None, max_length=64, description="交易渠道")
    display_name: Optional[str] = Field(None, max_length=128, description="人类可读名")

    # 行情接入（utils/quotes.py 用）
    yfinance_proxy: Optional[str] = Field(
        None,
        max_length=32,
        description="如果 symbol 不能直接拉行情，用此代理（浙商金 → GC=F）",
    )
    proxy_kind: ProxyKind = Field(default="direct", description="行情代理逻辑")

    # 渠道修正
    price_offset_pct: Optional[float] = Field(None, ge=-0.1, le=0.1)
    sell_fee_pct: Optional[float] = Field(None, ge=0, le=0.05)

    # 追踪仓：仅观察不持有，跑委员会但不算 P&L
    is_tracking_only: bool = Field(default=False, description="True = 虚拟跟踪，不影响 P&L")

    @field_validator("symbol", mode="before")
    @classmethod
    def _strip_symbol(cls, v: Any) -> str:
        return str(v).strip() if v is not None else v

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_legacy_kind(cls, v: Any) -> Any:
        """Accept legacy/importer ``stock`` values and persist them as ``equity``."""
        if isinstance(v, str) and v.strip().lower() == "stock":
            return "equity"
        return v

    @field_validator("cost_currency", mode="before")
    @classmethod
    def _uppercase_currency(cls, v: Any) -> str:
        """先 normalize 再让 pattern 校验"""
        if not isinstance(v, str):
            return v
        return v.strip().upper()


class PortfolioData(BaseModel):
    """portfolio.md frontmatter v2 —— 通用化结构

    v1 的扁平字段（cash_cny/aud_cash/ndq_shares/gold_grams/gold_avg_cost_cny_per_gram/
    ndq_avg_cost_aud_per_share）由 scripts/migrate_portfolio_to_holdings.py 一次性转换；
    v2 后这些字段在写入端不应再出现（CI grep 会防回归）。
    """
    model_config = ConfigDict(extra="allow")

    cash: Dict[str, float] = Field(
        default_factory=dict,
        description="任意币种现金 {CNY: 1234.56, AUD: -100, USD: 0}；允许负数",
    )
    holdings: List[Holding] = Field(
        default_factory=list,
        description="任意 yfinance symbol 持仓",
    )
    schema_version: int = Field(default=2, ge=2, le=2, description="迁移脚本看这个字段决定是否 noop")

    @field_validator("cash", mode="before")
    @classmethod
    def _normalize_currency_keys(cls, v: Any) -> Dict[str, float]:
        """币种 key 统一大写 + 去空白；amount 转 float"""
        if not isinstance(v, dict):
            return {}
        normalized = {}
        for k, amt in v.items():
            ccy = str(k).strip().upper()
            if not ccy:
                continue
            normalized[ccy] = float(amt)
        return normalized

    @model_validator(mode="after")
    def _no_dup_symbols(self) -> "PortfolioData":
        syms = [h.symbol for h in self.holdings]
        dups = {s for s in syms if syms.count(s) > 1}
        if dups:
            raise ValueError(f"holdings 含重复 symbol: {sorted(dups)}")
        return self


# ============ user.md ============

class UserData(BaseModel):
    """user.md frontmatter（用户偏好）"""
    model_config = ConfigDict(extra="allow")

    display_name: Optional[str] = None
    risk_tolerance: Optional[str] = Field(
        default="Balanced",
        pattern="^(Conservative|Balanced|Aggressive)$",
    )
    user_email: Optional[str] = None


# ============ portfolio_history.jsonl —— 统一 Activity（ADR-027 D2）============

# 历史动作（bought/sold/dca_reconcile_*/manual_override）在解析层归一，无法识别的行 = "unknown"（保留、计数、不丢）。
# PR2 起新写入还有 dividend / interest / fee / tax / split / return_of_capital（schema_version=2 记录，效果由写入方算好）；
# 场外基金两段式（fund_order_*）是 PR3 的事。
ActivityAction = Literal[
    "buy", "sell", "deposit", "withdraw", "gift", "delete_holding", "correction",
    "dividend", "interest", "fee", "tax", "split", "return_of_capital", "unknown",
]


class Activity(BaseModel):
    """账本流水的统一读形状：``calc.ledger_replay.replay_ledger`` 把老/新记录折叠成它，读方只认这里的字段。

    符号约定（读方只认 ``cash_effect`` / ``units_effect``，不再自己判 action 的正负）：
    - ``cash_effect``：对 ``currency`` 现金的净影响，流入正、流出负；external_funding 的买卖不动现金 = 0
    - ``units_effect``：对 ``symbol`` 持仓数量的净影响（correction / delete_holding 是从前序状态算出的增量）
    - ``amount``：老记录原样的 ``total_amount``（缺省 ``|units| × price``）；deposit/withdraw 为 ``|金额|``
    - ``external``：外部现金流（deposit / withdraw / gift / external_funding 买卖）；外部流水金额用
      ``calc.ledger_replay.external_flow(a)`` 取，不要自己拼。dividend / interest / fee / tax / return_of_capital
      是内部流（进 NAV、不进 TWR 分母），split 无现金效应
    - dividend：``amount`` = 税前总额，``tax_withheld`` = 预扣税，``cash_effect`` = 税后净额；fee / tax 的 ``amount`` 是
      正的支出额，``cash_effect`` 为负；split：``split_ratio`` = 新/旧，``units_effect`` = 拆股增加的数量
    """
    # forbid：replay 少写/多写一个字段立即炸，不会悄悄丢信息
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2   # 不叫 `schema`：与 BaseModel.schema() 同名会告警；老记录无此字段 = 1
    action: ActivityAction
    symbol: Optional[str] = None     # deposit/withdraw 沿用老记录：symbol = 币种代码
    units: Optional[float] = None    # 记录里的数量原样（correction 为修正后的绝对数量）；deposit/withdraw 为 None
    price: Optional[float] = None    # price 或老记录的 price_per_unit
    amount: Optional[float] = None
    currency: str = "CNY"
    fee: Optional[float] = None
    tax_withheld: Optional[float] = None
    cash_effect: float = 0.0
    units_effect: float = 0.0
    split_ratio: Optional[float] = None
    external: bool = False
    trade_date: Optional[str] = None  # YYYY-MM-DD；None = 记录没有可用日期（不进回放，计入 unknown 计数）
    settle_date: Optional[str] = None
    nav_date: Optional[str] = None
    source: Optional[str] = None
    idempotency_key: Optional[str] = None
    note: Optional[str] = None
    unparsed: bool = False            # 数值字段解析失败：action 分类保留、效果全 0，不进回放（脏行不挡读方）
    correction: bool = False          # 持仓修正（manual_override / delete_holding）：无现金对手，现金流按当日市价补算
    ts: Optional[str] = None          # 记录时间戳原样（ts_origin 优先，否则 ts）——决议↔成交按它做时间窗
    seq: int = -1                     # 原记录在输入列表里的下标（"history:<seq>" 引用）
    raw: Dict[str, Any] = Field(default_factory=dict, repr=False)   # 原记录（浅拷贝），溯源用，读方不要解析它

    @property
    def replayable(self) -> bool:
        """能进回放（产生 cash/units 效果）：已识别的 action + 有日期 + 数值可解析。"""
        return self.action != "unknown" and self.trade_date is not None and not self.unparsed


# ============ helper：把 metadata dict 校验后转回 dict（写 md 用） ============

def validate_strategy(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return _validated_dict(StrategyData, metadata)


def validate_portfolio(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return _validated_dict(PortfolioData, metadata)


def validate_user(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return _validated_dict(UserData, metadata)


def _validated_dict(model_cls: type[BaseModel], metadata: Dict[str, Any]) -> Dict[str, Any]:
    """通用 helper：去掉 MemoryStore 元字段后跑 model 校验，返回 dict"""
    cleaned = {k: v for k, v in metadata.items() if k not in {"name", "type", "updated"}}
    instance = model_cls.model_validate(cleaned)
    return instance.model_dump(exclude_none=True)
