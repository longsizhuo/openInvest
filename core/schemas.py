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

class TargetAsset(BaseModel):
    """strategy.target_assets[] 中的单个资产配置（决策层 schema）"""
    model_config = ConfigDict(extra="allow")

    symbol: str = Field(..., min_length=1, description="标的代码（如 NDQ.AX, GC=F）")
    display_name: Optional[str] = Field(None, description="人类可读名")
    channel: Optional[str] = Field(None, description="交易渠道")
    max_single_invest_cny: float = Field(..., ge=0, le=1_000_000)
    price_offset_pct: Optional[float] = Field(None, ge=-0.1, le=0.1)
    sell_fee_pct: Optional[float] = Field(None, ge=0, le=0.05)

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
ProxyKind = Literal["direct", "gold_cny_per_gram", "fx_pair"]


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
    exchange_buffer_cny: float = Field(default=0.0, ge=0)
    last_payday: Optional[str] = None
    user_email: Optional[str] = None


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
