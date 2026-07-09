"""Pydantic 请求/响应模型 — 从 web_api.py 拆分（定义不变）。"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field



# ============ Pydantic 响应模型 ============

class HealthResponse(BaseModel):
    """/api/health 响应"""
    ok: bool = True
    service: str = "invest-web-api"
    timestamp: str


class CashSummary(BaseModel):
    """现金部分"""
    cny: float = Field(..., description="CNY 现金")
    aud: float = Field(..., description="AUD 现金")


class GoldHolding(BaseModel):
    """黄金持仓 + 实时估值"""
    grams: float = Field(..., description="持仓克数")
    avg_cost_cny_per_gram: float = Field(..., description="加权均价 CNY/g")
    spot_cny_per_gram: Optional[float] = Field(None, description="实时现货价 CNY/g（yfinance）")
    bank_cny_per_gram: Optional[float] = Field(None, description="渠道参考克价（含点差，渠道由 strategy/INVEST_GOLD_CHANNEL 决定）")
    offset_pct: float = Field(0.0, description="渠道点差（spot → 实际买入克价的溢价）")
    market_value_cny: Optional[float] = Field(None, description="持仓现值 CNY")
    pnl_cny: Optional[float] = Field(None, description="浮盈 CNY")
    is_stale: bool = Field(False, description="价格来自 DB 兜底（yfinance 不可用）")


class NDQHolding(BaseModel):
    """NDQ.AX 持仓 + 实时行情"""
    shares: float = Field(..., description="持仓股数")
    last_price_aud: Optional[float] = Field(None, description="最新价 AUD")
    prev_close_aud: Optional[float] = Field(None, description="前收 AUD")
    day_change_pct: Optional[float] = Field(None, description="日变化 %")
    last_updated: Optional[str] = Field(None, description="行情日期 YYYY-MM-DD")


class PortfolioResponse(BaseModel):
    """/api/portfolio 响应：完整持仓快照"""
    cash: CashSummary
    gold: GoldHolding
    ndq: NDQHolding


class TargetAsset(BaseModel):
    """strategy.md 中的单个目标资产"""
    symbol: str
    display_name: Optional[str] = None
    channel: Optional[str] = None
    max_single_invest_cny: float = 0
    price_offset_pct: Optional[float] = None
    sell_fee_pct: Optional[float] = None


class StrategyResponse(BaseModel):
    """/api/strategy 响应"""
    target_allocation_stock: float
    target_allocation_cash: float
    target_assets: List[TargetAsset]


class HistoryRow(BaseModel):
    """单笔交易记录（兼容历史字段变化）"""
    # frontmatter 历史上字段会增减，开 extra=allow 兜底
    model_config = ConfigDict(extra="allow")

    ts: Optional[str] = None
    ts_origin: Optional[str] = None
    action: Optional[str] = None
    symbol: Optional[str] = None
    units: Optional[float] = None
    price_per_unit: Optional[float] = None
    total_amount: Optional[float] = None
    currency: Optional[str] = None
    channel: Optional[str] = None
    source: Optional[str] = None


class HistoryResponse(BaseModel):
    """/api/history 响应"""
    count: int
    rows: List[HistoryRow]


class DailyEntry(BaseModel):
    """单天 daily 日志（完整 markdown）"""
    date: str
    content: str


class DailyResponse(BaseModel):
    """/api/daily 响应"""
    count: int
    entries: List[DailyEntry]


# ============ v2 通用持仓 ============

class HoldingQuote(BaseModel):
    """单个 holding 的实时行情（通用化 v2 端点用）"""
    price: Optional[float] = None
    currency: Optional[str] = None
    unit: Optional[str] = None
    last_updated: Optional[str] = None
    is_stale: bool = False
    extra: Optional[Dict[str, Any]] = None


class HoldingV2(BaseModel):
    """单个 v2 holding（含静态字段 + 实时 quote + 计算的 P&L）"""
    symbol: str
    kind: str
    units: float
    unit_label: str
    avg_cost: float
    cost_currency: str
    channel: Optional[str] = None
    display_name: Optional[str] = None
    yfinance_proxy: Optional[str] = None
    proxy_kind: str = "direct"
    price_offset_pct: Optional[float] = None
    sell_fee_pct: Optional[float] = None
    is_tracking_only: bool = False
    quote: Optional[HoldingQuote] = None
    market_value: Optional[float] = None     # cost_currency 计价
    pnl: Optional[float] = None              # cost_currency 计价


class HoldingsListResponse(BaseModel):
    """GET /api/holdings 响应"""
    cash: Dict[str, float]
    holdings: List[HoldingV2]


# ============ 多币种总市值折算（v3 补充）============

class TotalValueBreakdownItem(BaseModel):
    """单项资产 / 现金 在折算币种下的金额"""
    label: str
    kind: str   # "cash" | "holding"
    amount_local: float
    currency_local: str
    amount_in_base: Optional[float] = None
    fx_rate: Optional[float] = None
    note: Optional[str] = None


class TotalValueResponse(BaseModel):
    """完整资产折算到指定币种"""
    base_currency: str
    cash_total: float
    holdings_total: float
    grand_total: float
    breakdown: List[TotalValueBreakdownItem]
    fx_rates: Dict[str, Optional[float]]


# ============ User profile ============

class UserProfileResponse(BaseModel):
    """GET /api/user 返回 user.md frontmatter 全部字段"""
    display_name: Optional[str] = None
    risk_tolerance: Optional[str] = None
    user_email: Optional[str] = None


# ============ v2 通用 holdings CRUD ============

class HoldingCreateRequest(BaseModel):
    """POST /api/holdings body — 新增持仓"""
    symbol: str = Field(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._=^:\-]+$")
    kind: Literal["equity", "etf", "metal", "crypto", "bond", "fund", "other"] = "equity"
    units: float = Field(default=0.0, ge=0)
    unit_label: str = Field(default="股", max_length=8)
    avg_cost: float = Field(default=0.0, ge=0)
    cost_currency: str = Field(..., pattern=r"^[A-Za-z]{3,5}$")
    channel: Optional[str] = Field(None, max_length=64)
    display_name: Optional[str] = Field(None, max_length=128)
    yfinance_proxy: Optional[str] = Field(None, max_length=32)
    proxy_kind: Literal["direct", "gold_cny_per_gram", "fx_pair"] = "direct"
    is_tracking_only: bool = False


class HoldingPatchRequest(BaseModel):
    """PUT /api/holdings/{symbol} body — 部分字段更新"""
    kind: Optional[Literal["equity", "etf", "metal", "crypto", "bond", "fund", "other"]] = None
    units: Optional[float] = Field(None, ge=0)
    unit_label: Optional[str] = Field(None, max_length=8)
    avg_cost: Optional[float] = Field(None, ge=0)
    cost_currency: Optional[str] = Field(None, pattern=r"^[A-Za-z]{3,5}$")
    channel: Optional[str] = None
    display_name: Optional[str] = None
    yfinance_proxy: Optional[str] = None
    proxy_kind: Optional[Literal["direct", "gold_cny_per_gram", "fx_pair"]] = None
    is_tracking_only: Optional[bool] = None


class HoldingsImportRequest(BaseModel):
    """POST /api/holdings/import body — 自由文本/CSV 持仓描述 → 结构化持仓"""
    content: str = Field(..., min_length=1, max_length=20000, description="自然语言或 CSV 持仓描述")
    commit: bool = Field(default=False, description="false=只预览解析结果不落盘；true=非破坏写入（只加新 symbol、cash 只填当前为 0 的币种）")


class HoldingsImportResponse(BaseModel):
    """POST /api/holdings/import 返回 — parsed 预览 +（commit 时）写入 summary"""
    parsed: Dict[str, Any] = Field(..., description="LLM 解析出的 {cash, holdings}")
    committed: bool = Field(..., description="是否已落盘")
    summary: Optional[Dict[str, Any]] = Field(None, description="commit 时的 {added_holdings, skipped_holdings, cash_set, cash_skipped}")


# 注：v2 通用 cash CRUD（/api/cash/{currency}/deposit|withdraw）放在文件末尾，
# 因为它依赖 WriteResponse 定义（在 PR 2 区域）

# ============ yfinance Search 端点 ============

class SymbolSearchResult(BaseModel):
    """单个搜索命中"""
    symbol: str
    shortname: Optional[str] = None
    longname: Optional[str] = None
    exchange: Optional[str] = None
    quote_type: Optional[str] = None


class SymbolSearchResponse(BaseModel):
    count: int
    results: List[SymbolSearchResult]


# ============================================================
# PR 2: 写操作 + 委员会异步
# ============================================================
# 设计：
# - 所有写端点都走 PortfolioManager.with_portfolio_tx()（fcntl + 原子写）
#
#   不重新发明轮子（保证 NapCat 与 Web 写入语义一致，包括 history 字段）
# - 委员会触发是长任务（~6 min），用 asyncio.run_in_executor 跑同步入口，
#   状态落盘到 memory/.committee/<task_id>/status.json，前端 SWR 轮询查


# ===== 写操作请求模型 =====

class DepositRequest(BaseModel):
    """POST /api/deposit body"""
    currency: Literal["cny", "aud"] = Field("cny", description="cny=人民币 / aud=澳元（NDQ 子弹）")
    amount: float = Field(..., gt=0, description="正数金额")


class WithdrawRequest(BaseModel):
    """POST /api/withdraw body"""
    currency: Literal["cny", "aud"] = Field("cny", description="cny=人民币 / aud=澳元")
    amount: float = Field(..., gt=0, description="正数金额")


class GoldTradeRequest(BaseModel):
    """POST /api/gold/buy 和 /api/gold/sell 共用 body"""
    grams: float = Field(..., gt=0, description="买入/卖出克数")
    price_per_gram: float = Field(..., gt=0, description="单价 CNY/g")


class GoldSetRequest(BaseModel):
    """POST /api/gold/set body — 直接覆盖克数（不计流水，仅校正用）"""
    grams: float = Field(..., ge=0, description="目标克数（≥0）")


class GoldOffsetRequest(BaseModel):
    """POST /api/gold/offset body — 报当日实际买入克价，反推渠道点差"""
    bank_price: float = Field(..., gt=0, description="当日实际买入克价 CNY/g（任何银行/纸黄金渠道都通用）")


class WriteResponse(BaseModel):
    """写操作统一响应：返回新写入字段 + history 是否记录"""
    ok: bool = True
    cash_cny: Optional[float] = None
    aud_cash: Optional[float] = None
    gold_grams: Optional[float] = None
    gold_avg_cost_cny_per_gram: Optional[float] = None
    history_appended: bool = False
    message: str


class CommitteeRunRequest(BaseModel):
    """POST /api/committee/run body

    v3 升级（2026-05-06）：
    - symbols 空 = 跑 strategy.target_assets 全部（多资产并行）
    - symbols 给了 = 跑指定的（单 / 多资产并行）
    - 与旧 daily_report.run() 区别：默认 max_debate_rounds=4 真讨论，不发邮件
    - cron 自动跑仍走 daily_report.run()（max=1 串行 + 邮件，节省成本）
    """
    note: Optional[str] = Field(None, description="可选备注")
    symbols: Optional[List[str]] = Field(
        default=None,
        description="资产列表；None = strategy.target_assets 全部",
    )
    max_debate_rounds: int = Field(
        default=4, ge=1, le=8,
        description="cross-challenge 上限。1=旧行为；4=真讨论（推荐）",
    )
    event_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "事件层（event_watch）触发委员会时传入的事件 id 列表。仅用于审计 "
            "（写 meta.json）+ 让 Macro 看到这些事件的结构化 brief；不影响 verdict "
            "解析逻辑。其他 caller 不需要传。"
        ),
    )


class CommitteeRunResponse(BaseModel):
    """触发后立即返回"""
    task_id: str
    status: Literal["queued"]
    started_at: str
    poll_url: str  # 前端轮询路径


class CommitteeStatusResponse(BaseModel):
    """GET /api/committee/{task_id} 响应"""
    model_config = ConfigDict(extra="allow")

    task_id: str
    status: Literal["queued", "running", "done", "error"]
    started_at: str
    ended_at: Optional[str] = None
    note: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # v3 live 模式新增字段（旧 /run 端点不写）
    phase: Optional[str] = None                          # 当前 stage 名（round_1_done / cio_done 等）
    last_event: Optional[Dict[str, Any]] = None
    events: Optional[List[Dict[str, Any]]] = None        # 最近 100 条 progress 事件
    symbol: Optional[str] = None                         # live 模式跑的是哪个资产
    max_debate_rounds: Optional[int] = None


# ============ 事件感知层 (ADR-006) — Events Tab 用 ============

class EventItem(BaseModel):
    """单条事件（GUI Events Tab 渲染）"""
    event_id: str
    one_line_claim: str
    event_type: str
    stance: str       # risk / opportunity / neutral
    severity: str     # low / mid / high
    affected_symbols: List[str] = Field(default_factory=list)
    entities: List[str] = Field(default_factory=list)
    ts: str           # ISO timestamp
    committee_task_id: Optional[str] = None


class EventsRecentResponse(BaseModel):
    hours: int
    counts: Dict[str, int]                  # {low, mid, high, total}
    items: List[EventItem]


class EventCheckResponse(BaseModel):
    """POST /api/events/check —— 手动跑一次 event_watch"""
    status: str
    fetched: int
    new_events: int
    triggered: int
    duration_ms: int


# ===== 请求模型 =====

class AllocationsRequest(BaseModel):
    """PUT /api/strategy/allocations body"""
    target_allocation_stock: float = Field(..., ge=0, le=1)
    target_allocation_cash: float = Field(..., ge=0, le=1)


class TargetAssetCreate(BaseModel):
    """POST /api/strategy/asset body"""
    symbol: str = Field(..., min_length=1)
    display_name: Optional[str] = None
    channel: Optional[str] = None
    max_single_invest_cny: float = Field(..., ge=0, le=1_000_000)
    price_offset_pct: Optional[float] = Field(None, ge=-0.1, le=0.1)
    sell_fee_pct: Optional[float] = Field(None, ge=0, le=0.05)
    # 允许扩展字段（旧 md 用 currency/market/note 等，前端可能想顺便改）
    extra: Optional[Dict[str, Any]] = None


class TargetAssetPatch(BaseModel):
    """PUT /api/strategy/asset/{symbol} body — 全字段可选，仅更新提供的字段"""
    display_name: Optional[str] = None
    channel: Optional[str] = None
    max_single_invest_cny: Optional[float] = Field(None, ge=0, le=1_000_000)
    price_offset_pct: Optional[float] = Field(None, ge=-0.1, le=0.1)
    sell_fee_pct: Optional[float] = Field(None, ge=0, le=0.05)


class StrategyWriteResponse(BaseModel):
    """策略写操作统一响应"""
    ok: bool = True
    target_allocation_stock: float
    target_allocation_cash: float
    target_assets: List[Dict[str, Any]]
    message: str


# ============================================================
# v2 通用 cash CRUD（任意币种） — 放在文件末尾因为依赖 WriteResponse
# ============================================================

class CashWriteRequest(BaseModel):
    """POST /api/cash/{currency}/deposit | withdraw body"""
    amount: float = Field(..., gt=0)


# ============================================================
# 系统 / 原理可视化端点（让 GUI 能看到所有静默 cron 的内部状态）
# ============================================================

class JobStatus(BaseModel):
    name: str
    description: str
    schedule: str
    timezone: str
    enabled: bool
    next_run_time: Optional[str] = None


class JobsStatusResponse(BaseModel):
    jobs: List[JobStatus]


class InsightItem(BaseModel):
    slug: str
    metadata: Dict[str, Any]
    body: str


class InsightsResponse(BaseModel):
    count: int
    items: List[InsightItem]


class RegimeResponse(BaseModel):
    symbol: str
    regime: str
    reason: str
    inputs: Dict[str, Any]
    strategy_hint: str
    brief: str


class DreamEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str
    phase: str


class DreamsStateResponse(BaseModel):
    short_term: Optional[Dict[str, Any]] = None
    candidates: Optional[Dict[str, Any]] = None
    recent_events: List[DreamEvent]


class PnLHistoryPoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str
    total_pnl_pct: Optional[float] = None


class PnLHistoryResponse(BaseModel):
    count: int
    points: List[PnLHistoryPoint]


class CommitteeSessionSummary(BaseModel):
    date: str
    symbol: str
    verdict: Optional[str] = None
    confidence: Optional[float] = None
    dominant_view: Optional[str] = None
    suggested_alloc_cny: Optional[float] = None
    file_path: str


class CommitteeSessionsResponse(BaseModel):
    count: int
    sessions: List[CommitteeSessionSummary]


class CommitteeSessionDetail(BaseModel):
    date: str
    symbol: str
    content: str


class FreshInsightItem(BaseModel):
    """新鲜出炉的 Dreaming insight，给 GUI toast 用（PM-3 留存杠杆）"""
    slug: str = Field(..., description="insight 文件名（不含 .md）")
    title: str = Field(..., description="一句话总结，供 toast 直接展示")
    hit_rate: Optional[float] = Field(None, description="该模式历史命中率 0-1")
    sample_count: Optional[int] = Field(None, description="支持样本数")
    asset: Optional[str] = Field(None, description="资产 symbol（如适用）")
    written_at: str = Field(..., description="insight 文件 mtime ISO")


class FreshInsightsResponse(BaseModel):
    count: int = Field(..., description="返回的 fresh insight 条数")
    items: List[FreshInsightItem] = Field(..., description="按写入时间倒序")


class ReengagementAlert(BaseModel):
    """主动 nudge 用户回来的事件（PM-3 留存漏洞 #3 修复）"""
    kind: str = Field(..., description="alert 类型：volatile / high_confidence_buy / stale_decision")
    asset: Optional[str] = Field(None, description="资产 symbol")
    message: str = Field(..., description="给用户看的一句话")
    severity: str = Field(..., description="info / warn / urgent")
    detected_at: str = Field(..., description="检测时间 ISO")


class ReengagementResponse(BaseModel):
    count: int
    alerts: List[ReengagementAlert]


class OutperformEvent(BaseModel):
    """openInvest 跑赢某个基准的"可分享瞬间" event（jobs/pnl_snapshot 写入）"""
    ts: str = Field(..., description="snapshot 时间戳 ISO")
    benchmark: str = Field(..., description="基准名，如 余额宝 / 沪深300 / Wealthfront")
    user_pct: float = Field(..., description="openInvest 实盘累计涨幅 %")
    bench_pct: float = Field(..., description="基准累计涨幅 %")
    diff_pct: float = Field(..., description="跑赢幅度 % (user - bench)")
    label: str = Field(..., description="拼好的可分享文案")


class OutperformEventsResponse(BaseModel):
    count: int = Field(..., description="返回的事件数")
    events: List[OutperformEvent] = Field(..., description="按时间倒序")


# ============ LLM telemetry 端点（v3 透明化 D1）============

class LlmUsageRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str
    agent_role: str
    asset: Optional[str] = None
    round: Optional[str] = None
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    cost_cny: float = 0.0
    tool_calls: int = 0
    iteration: int = 0
    ok: bool = True
    error: Optional[str] = None


class LlmUsageResponse(BaseModel):
    count: int
    records: List[LlmUsageRecord]


class LlmRoleStats(BaseModel):
    calls: int
    input_tokens: int
    output_tokens: int
    cost_cny: float
    avg_latency_ms: int


class LlmSummaryResponse(BaseModel):
    total_calls: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_cny: float
    by_role: Dict[str, LlmRoleStats]


# ============ 数据源健康 + 基准对标（v3 透明化 C11/C8）============

class DataSourceHealth(BaseModel):
    name: str
    description: str
    last_success_at: Optional[str] = None
    is_stale: bool = False
    sample_value: Optional[Any] = None
    error: Optional[str] = None


class DataSourcesHealthResponse(BaseModel):
    sources: List[DataSourceHealth]


# ============ Verdict Review 端点（v3 透明化 B4 — marketing 核心信任）============

class VerdictReviewItem(BaseModel):
    """单条 verdict review 记录（实际 schema 字段类型多样，宽松接受）"""
    model_config = ConfigDict(extra="allow")

    date: str
    asset: Optional[str] = None
    verdict: Optional[str] = None
    confidence: Optional[float] = None
    expected_direction: Optional[str] = None
    # 实际 macro_shock 是 dict {detected, drivers}，不是 bool
    macro_shock: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    actual_returns: Optional[Dict[str, float]] = None
    hits: Optional[Dict[str, bool]] = None


class VerdictReviewDataResponse(BaseModel):
    count: int
    items: List[VerdictReviewItem]


class VerdictReviewSummary(BaseModel):
    """命中率汇总"""
    total: int
    by_window: Dict[str, Dict[str, Any]]      # { "1d": {n, hit_rate}, "7d": ..., "30d": ... }
    by_verdict: Dict[str, Dict[str, Any]]     # { "BUY": {n, avg_conf, 1d_hit, ...}, ... }
    directional_only_hit_rate: Optional[float] = None  # 剔除 HOLD 后真实 alpha
    has_report_md: bool


class VerdictReviewReportResponse(BaseModel):
    """docs/verdict_accuracy.md 完整内容"""
    exists: bool
    generated_at: Optional[str] = None
    content: Optional[str] = None


# ============ Tool Calls Audit 端点（v3 透明化 A8/D2）============

class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str
    agent_role: str
    asset: Optional[str] = None
    round: Optional[str] = None
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result_preview: str = ""
    latency_ms: int = 0
    iteration: int = 0


class ToolCallsResponse(BaseModel):
    count: int
    records: List[ToolCallRecord]


# ============ Regime 规则 + 4 角色 prompt 暴露（v3 透明化 B6/A9）============

class AgentPromptInfo(BaseModel):
    """单个 agent 角色的 prompt 配置"""
    role: str
    label: str                    # 中文显示名
    description: str              # 一句话职责
    prompt_opening: Optional[str] = None     # Round 1 prompt（quant / risk）
    prompt_rebuttal: Optional[str] = None    # Round 2 prompt（quant / risk）
    prompt_full: Optional[str] = None        # macro / cio 单 prompt
    temperature: float = 0.2
    enable_tools: bool = True
    notes: List[str] = Field(default_factory=list)


class RegimeRulesResponse(BaseModel):
    """Regime 判定硬规则 + 4 角色 prompt 全集（marketing 主战场）"""
    regime_thresholds: Dict[str, float]
    regime_types: List[str]
    regime_priority: List[str]
    verdict_options: List[str]
    sanity_checks: List[str]
    agents: List[AgentPromptInfo]
    tools: List[Dict[str, Any]]   # 5 个可调 tool


# ============================================================
# GUI 静态文件挂载（如果 static/ 已 sync）
# ============================================================
# ============ CommSec 手动导入端点（Task #38）============
# 替代旧 cron 自动模式：cron 模式 IMAP 临时失败会静默丢成交。改成
# 用户主动触发：先 /preview 看拉到了什么，确认后再 /apply 写入

class CommsecPreviewResponse(BaseModel):
    """GET /api/commsec/preview"""
    ok: bool = True
    lookback_days: int
    new_trades: List[Dict[str, Any]] = Field(default_factory=list)
    skipped_count: int = 0
    error: Optional[str] = None


class CommsecApplyRequest(BaseModel):
    lookback_days: int = Field(default=180, ge=1, le=365)


class CommsecApplyResponse(BaseModel):
    ok: bool = True
    written: int = 0
    skipped: int = 0
    errors: List[str] = Field(default_factory=list)


class RecordTradeRequest(BaseModel):
    """POST /api/trades/record body"""
    symbol: str = Field(..., min_length=1, max_length=32,
                        description="标的代码，如 NDQ.AX / GC=F")
    direction: Literal["BUY", "SELL"] = Field(..., description="方向：BUY 或 SELL")
    units: float = Field(..., gt=0, description="数量（股数 / 克数）")
    price: Optional[float] = Field(None, gt=0,
                                   description="每单位价格；None 表示市价")
    cost_currency: str = Field("CNY", pattern=r"^[A-Za-z]{3,5}$",
                               description="计价货币，默认 CNY")
    verdict_id: Optional[str] = Field(None, max_length=256,
                                      description='关联决议 decision_id："<date>/<symbol>" 如 '
                                                  '"2026-07-03/GC=F"（可选；历史 transcript '
                                                  '路径写法仍兼容）')
    note: Optional[str] = Field(None, max_length=512, description="备注（可选）")
    intended_date: Optional[str] = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description=(
            "计划成交日期，ISO 格式 YYYY-MM-DD，可空。"
            "None 表示「现在记录、现在打算执行」；"
            "填具体日期表示计划在该日成交（金融审计用，与 ts 独立）。"
        ),
    )


class TradeRecord(BaseModel):
    """单笔 trade 记录（返回给前端）"""
    id: int
    ts: str                          # 记录意向时刻（UTC ISO 8601 时间戳，自动生成）
    verdict_id: Optional[str] = None
    symbol: str
    direction: str
    units: float
    price: Optional[float] = None
    cost_currency: str
    note: Optional[str] = None
    status: str
    intended_date: Optional[str] = None  # 计划成交日期（ISO YYYY-MM-DD，可空；None=立即执行）


class TradesListResponse(BaseModel):
    """GET /api/trades 响应"""
    count: int
    trades: List[TradeRecord]


class DecisionIntervention(BaseModel):
    """决议被确定性规则改写的摘要（源 interventions.jsonl）"""
    rule: Optional[str] = None
    rule_family: Optional[str] = None
    original_verdict: Optional[str] = None
    original_alloc: Optional[float] = None


class DecisionMatchedTrade(BaseModel):
    """自动匹配到的成交（源 trades.db）"""
    id: int
    ts: str
    direction: str
    units: float
    price: Optional[float] = None
    status: Optional[str] = None


class DecisionOutcome(BaseModel):
    """事后结果（源 verdict_review.jsonl，cron 回填）"""
    actual_returns: Optional[Dict[str, float]] = None
    hits: Optional[Dict[str, bool]] = None
    macro_shock: Optional[bool] = None


class DecisionExecution(BaseModel):
    """用户执行/拒绝声明（源 executions.jsonl）"""
    decision_id: str
    executed: bool
    reason: Optional[str] = None
    trade_ids: Optional[List[int]] = None
    recorded_at: Optional[str] = None


class DecisionRecord(BaseModel):
    """统一决策视图单条（issue #133 Decision 9，读时 join 五份账本）"""
    decision_id: str                 # "<date>/<symbol>"，同 trades.verdict_id 口径
    date: str
    symbol: str
    verdict: str
    confidence: float
    alloc_cny: Optional[float] = None
    intervention: Optional[DecisionIntervention] = None
    executed: Optional[bool] = None  # None=未知（HOLD 或无声明无匹配依据）
    execution: Optional[DecisionExecution] = None
    matched_trades: List[DecisionMatchedTrade] = []
    outcome: Optional[DecisionOutcome] = None


class DecisionsSummary(BaseModel):
    """采纳率汇总"""
    total: int
    directional: int
    executed: int
    not_executed: int
    unknown: int
    overridden_by_rule: int
    with_reason: int
    adoption_rate: Optional[float] = None


class DecisionsResponse(BaseModel):
    """GET /api/decisions 响应"""
    count: int
    summary: DecisionsSummary
    decisions: List[DecisionRecord]


class RecordExecutionRequest(BaseModel):
    """POST /api/decisions/execution body —— 宿主 Agent 回写执行/拒绝"""
    decision_id: str = Field(..., max_length=256, description='"<date>/<symbol>"')
    executed: bool
    reason: Optional[str] = Field(None, max_length=2000,
                                  description="未执行原因 / 执行备注（宿主 Agent 采集）")
    trade_ids: Optional[List[int]] = None


class SkillWhatIfRequest(BaseModel):
    """POST /api/skill/what_if body —— 字段与 CLI what_if 参数一一对应"""
    symbol: Optional[str] = None
    pct: Optional[float] = None
    price: Optional[float] = None
    gold_price: Optional[float] = None
    gold_pct: Optional[float] = None
    ndq_price: Optional[float] = None
    ndq_pct: Optional[float] = None
    audcny: Optional[float] = None


class SkillBuyRequest(BaseModel):
    """POST /api/skill/buy body —— 字段与 CLI buy 参数一一对应"""
    symbol: str
    units: float
    price: float
    currency: str = "CNY"
    kind: Literal["equity", "etf", "metal", "crypto", "bond", "fund", "other"] = "equity"
    unit_label: str = "股"


class SkillSellRequest(BaseModel):
    """POST /api/skill/sell body"""
    symbol: str
    units: float
    price: float


class SkillCashRequest(BaseModel):
    """POST /api/skill/deposit|withdraw body"""
    currency: str
    amount: float


class SkillDeleteHoldingRequest(BaseModel):
    """POST /api/skill/delete_holding body"""
    symbol: str
    force: bool = False


class CommitteePrepareRequest(BaseModel):
    """POST /api/committee/prepare body"""
    symbol: str


class CommitteeSaveRequest(BaseModel):
    """POST /api/committee/save body"""
    symbol: str
    transcript: str = Field(..., description="6 段 '=== ROLE ===' 分隔的 transcript 全文")


# ============ config-via-API（ADR-017）============

class ConfigItem(BaseModel):
    """GET/PUT /api/config 中的单条白名单配置项"""
    key: str = Field(..., description="dotted config key，如 verdict.concentration_lens_enabled")
    value: Any = Field(..., description="当前生效值（bool 或 enum 字符串）")
    overridden: bool = Field(..., description="是否被持久 API override（区别于 env/yaml/默认）")
    type: str = Field(..., description="bool | enum")
    label: str
    help: str
    choices: Optional[List[str]] = Field(default=None, description="enum 时的可选值")


class ConfigResponse(BaseModel):
    """GET/PUT/DELETE /api/config 响应：白名单全部配置项的当前生效视图"""
    items: List[ConfigItem]


class ConfigUpdateRequest(BaseModel):
    """PUT /api/config body：设一条白名单 override"""
    key: str = Field(..., description="必须 ∈ 白名单（API_SETTABLE）")
    value: Any = Field(..., description="bool 或 enum 字符串；后端按白名单 spec 校验")


__all__ = [
    "HealthResponse",
    "CashSummary",
    "GoldHolding",
    "NDQHolding",
    "PortfolioResponse",
    "TargetAsset",
    "StrategyResponse",
    "HistoryRow",
    "HistoryResponse",
    "DailyEntry",
    "DailyResponse",
    "HoldingQuote",
    "HoldingV2",
    "HoldingsListResponse",
    "TotalValueBreakdownItem",
    "TotalValueResponse",
    "UserProfileResponse",
    "HoldingCreateRequest",
    "HoldingPatchRequest",
    "SymbolSearchResult",
    "SymbolSearchResponse",
    "DepositRequest",
    "WithdrawRequest",
    "GoldTradeRequest",
    "GoldSetRequest",
    "GoldOffsetRequest",
    "WriteResponse",
    "CommitteeRunRequest",
    "CommitteeRunResponse",
    "CommitteeStatusResponse",
    "EventItem",
    "EventsRecentResponse",
    "EventCheckResponse",
    "AllocationsRequest",
    "TargetAssetCreate",
    "TargetAssetPatch",
    "StrategyWriteResponse",
    "CashWriteRequest",
    "JobStatus",
    "JobsStatusResponse",
    "InsightItem",
    "InsightsResponse",
    "RegimeResponse",
    "DreamEvent",
    "DreamsStateResponse",
    "PnLHistoryPoint",
    "PnLHistoryResponse",
    "CommitteeSessionSummary",
    "CommitteeSessionsResponse",
    "CommitteeSessionDetail",
    "FreshInsightItem",
    "FreshInsightsResponse",
    "ReengagementAlert",
    "ReengagementResponse",
    "OutperformEvent",
    "OutperformEventsResponse",
    "LlmUsageRecord",
    "LlmUsageResponse",
    "LlmRoleStats",
    "LlmSummaryResponse",
    "DataSourceHealth",
    "DataSourcesHealthResponse",
    "VerdictReviewItem",
    "VerdictReviewDataResponse",
    "VerdictReviewSummary",
    "VerdictReviewReportResponse",
    "ToolCallRecord",
    "ToolCallsResponse",
    "AgentPromptInfo",
    "RegimeRulesResponse",
    "CommsecPreviewResponse",
    "CommsecApplyRequest",
    "CommsecApplyResponse",
    "RecordTradeRequest",
    "TradeRecord",
    "TradesListResponse",
    "DecisionIntervention",
    "DecisionMatchedTrade",
    "DecisionOutcome",
    "DecisionExecution",
    "DecisionRecord",
    "DecisionsSummary",
    "DecisionsResponse",
    "RecordExecutionRequest",
    "SkillWhatIfRequest",
    "SkillBuyRequest",
    "SkillSellRequest",
    "SkillCashRequest",
    "SkillDeleteHoldingRequest",
    "CommitteePrepareRequest",
    "CommitteeSaveRequest",
    "ConfigItem",
    "ConfigResponse",
    "ConfigUpdateRequest",
]
