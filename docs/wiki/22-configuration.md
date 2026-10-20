---
type: wiki-chapter
title: 配置参考（LLM Provider + Runtime Overrides）
tags: [config, env, llm-provider, runtime-overrides, adr-017]
intent: LLM provider .env 配置契约与运行时可调参数的单一参考页（从 README Configuration 章节迁入）
documents:
  endpoints: []
  config_keys:
    - LLM_API_KEY
    - LLM_BASE_URL
    - LLM_MODEL
    - EQUIBLES_API_KEY
    - verdict.concentration_lens_enabled
    - verdict.risk_profile
    - verdict.gold_defense_dca_enabled
    - dca.auto_dca_enabled
    - dca.auto_dca_amount_cny
    - autopilot.enabled
    - autopilot.commission_bps
    - autopilot.min_order_cny
    - ibkr.enabled
    - ibkr.dry_run
    - ibkr.account
    - ibkr.max_order_usd
    - ibkr.max_day_usd
    - ibkr.min_order_usd
    - ibkr.max_batches_per_day
    - ibkr.port
    - ibkr.notify_cmd
    - fund.confirm_lag_days
  symbols: []
---

# 配置参考（LLM Provider + Runtime Overrides）

> 本页是 README「Configuration」章节的完整版（2026-07 从 README 迁入）。
> 机制设计依据见 [ADR-017](adr/017-config-via-api.md)（config-via-API 白名单）；
> 端点与白名单实现细节见 [06-api.md](06-api.md)。

## LLM Provider 配置（.env）

系统默认采用 DeepSeek 端点，支持任何标准 OpenAI 兼容 API。替换 provider 时按 `.env` 契约映射以下变量（务必确保 `LLM_MODEL` 是目标 provider 的官方真实模型 ID，不可只改 URL）：

```env
# === 选项 A: DeepSeek (默认) ===
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# === 选项 B: 通义千问 (Aliyun DashScope 兼容模式) ===
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-max

# === 选项 C: 智谱 AI (GLM OpenAI 兼容端点) ===
LLM_API_KEY=xxxxxxxxxxxxxxxx.xxxxxxxxx
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v1
LLM_MODEL=glm-4-flash
```

## 可选数据源：Equibles（A 股 / 港股基本面）

行情和估值默认走 Yahoo Finance（yfinance），不用配置。持有 A 股 / 港股个股时，可以另接 [Equibles](https://equibles.com)，给委员会的估值事实块补上**最近一期利润表**和**最近一次业绩电话会摘要**。它不依赖 Yahoo，所以 Yahoo 限流（429）时这部分照样能拿到。

1. 去 [equibles.com](https://equibles.com/dashboard/apikeys) 免费注册，在控制台建一个 API key（免费档每天 100 次请求，00:00 UTC 重置）。
2. 写进 `.env`：

```env
EQUIBLES_API_KEY=eq_xxxxxxxxxxxxxxxx
```

- 只对 `.HK` / `.SS` / `.SZ` 代码生效（自动换成 Equibles 的 `XHKG:00700` / `XSHG:600519` / `XSHE:000001` 写法），每个标的每次委员会用 2 次请求；美股、ETF 基金、黄金等不受影响。
- 每个用户用自己的 key，不要共用：开源项目里每人各有一份免费额度。
- 没配 key、额度用完或网络不通时，这一段直接跳过，委员会照常运行。
- 电话会摘要是 Equibles 生成的 AI 摘要，委员会会把它当成观点，不当成事实。
- 数据旁边会带 `Data: Equibles - https://equibles.com` 署名（Equibles 条款要求），转述这些数字时请保留来源。

## Tunable Runtime Overrides（运行时可调参数）

依据 [ADR-017](adr/017-config-via-api.md)，以下 Tunable 变量在 CLI / REST API / `.env` 通道具备一致的覆盖优先级，持久化于 `memory/.state/config_overrides.json`：

| Config Key | 类型（默认值） | 行为 |
|---|---|---|
| `portfolio.benchmark_symbol` | `str` (`"510300.SS"`) | 账户业绩（`status.performance` / `openinvest performance` / 日报"账户业绩"段）对比的基准 yfinance symbol。默认沪深 300 ETF（指数 `000300.SS` 在 Yahoo 只回 1 根 bar）；持有跟踪它的 ETF 时自相关，换一个不持有的市场（`^GSPC` / `VAS.AX`）；空串 = 不比。env：`INVEST_PORTFOLIO_BENCHMARK_SYMBOL` |
| `verdict.concentration_lens_enabled` | `bool` (`false`) | **持仓集中度过滤器**。开启时对过度集中的资产触发告警 / TRIM；关闭（默认）后委员会看不到持仓占比，集中度不再触发 TRIM（波动率与估值风控仍生效，压力测试改用绝对损失金额）。见 [ADR-019](adr/019-remove-solvency-concentration-override.md)、[ADR-020](adr/020-concentration-lens-default-off.md) |
| `verdict.risk_profile` | `str` (`"steady"`) | 风险偏好：`steady`（稳健）/ `aggressive`（下行阶段允许更大规模买入）。 |
| `verdict.gold_defense_dca_enabled` | `bool` (`true`) | 黄金防御机制。高波动骤增阶段把单次大额加仓拆分为多期 DCA。 |
| `dca.auto_dca_enabled` | `bool` (`false`) | 全自动定投决策总开关。每个交易日记一笔；仅适用北京 15:30 前收盘的市场（A 股 / 亚太），休市日不记，见 [ADR-018](adr/018-dca-dip-reserve.md) |
| `dca.auto_dca_amount_cny` | `float` (`0.0`) | 每期自动定投的基准人民币配置额度。见 [ADR-018](adr/018-dca-dip-reserve.md) |
| `autopilot.enabled` | `bool` (`false`) | 全自动模拟账户总开关（`jobs/paper_autopilot`）。开启后该 INVEST_HOME 的每条 ACCUMULATE / BUY / TRIM / SELL 裁决按当日收盘价自动记账成交；不下真单。**只应在独立的模拟 INVEST_HOME 开**，见 [23-paper-autopilot.md](23-paper-autopilot.md) |
| `autopilot.commission_bps` | `float` (`3.0`) | 模拟成交的买卖双边手续费，万分之几；作为 `fee` 活动另记一行 |
| `autopilot.min_order_cny` | `float` (`500.0`) | 折算后小于它的模拟单不下（记 `skip too_small`）；SELL 清仓不受限 |
| `ibkr.enabled` | `bool` (`false`) | 盈透实盘 autopilot 总开关（`jobs/ibkr_autopilot`）。**下真单**；只应在独立的盈透 INVEST_HOME 开，见 [24-ibkr-autopilot.md](24-ibkr-autopilot.md)。**`ibkr.*` 全部故意不进 API 白名单**（MCP / web API / agent 的 config set 改不了真单开关）：只认 `INVEST_IBKR_*` 环境变量（如 cron 行里 `INVEST_IBKR_DRY_RUN=false`）或该 home 的 `memory/.state/config_overrides.json`（优先级高于 env）。金额键非有限 / ≤0 / 最小单大于单笔上限 → submit 一单不下 |
| `ibkr.dry_run` | `bool` (`true`) | true = 只 `whatIfOrder` 试算（盈透校验合约 / 权限 / 佣金），不下单、不换汇 |
| `ibkr.account` | `str` (`""`) | 盈透账户号（`U…` 实盘 / `DU…` 模拟）；网关登录的账户里没有它就什么都不做 |
| `ibkr.max_order_usd` | `float` (`100.0`) | 单笔订单金额硬上限（USD） |
| `ibkr.max_day_usd` | `float` (`150.0`) | 当日累计（已挂 + 已成交）硬上限（USD） |
| `ibkr.min_order_usd` | `float` (`20.0`) | 小于它的单不下（整笔清仓例外；每单佣金最低约 USD 0.35） |
| `ibkr.max_batches_per_day` | `int` (`4`) | 每天最多几批订单（第 1 批之后都是追加）：防失控 / 被外部内容诱导反复下单的刹车，运营者可调高；<1 → submit 一单不下 |
| `ibkr.port` | `int` (`4001`) | 本机 IB Gateway 的 API 端口（只连 127.0.0.1）。同一台机器同时跑实盘和模拟盘网关时，模拟盘的 home 设成另一个端口（如 4002） |
| `ibkr.notify_cmd` | `str` (`""`) | 通知（名单 / 下单 / 停手 / 对不上 / 失败）改走这条命令：按空格切词直接执行（不经 shell），标题 + 空行 + 正文从 stdin 进，60 秒超时。例：`hermes send --to discord:<你> --quiet`（Hermes 会把消息同步进该私聊会话，可直接回复追问）。空、正文 ≥ 15000 字（Hermes 发 Discord 最多拆 8 条，会截断），或命令失败 / 超时 → 照旧发邮件；失败日志只记异常类型和退出码（命令行 / stderr 可能带 token，不记；要看原因手动重跑那条命令） |
| `fund.confirm_lag_days` | `dict` (`{default: 1, qdii: 2, fof: 2}`) | 场外基金两段式（`place_fund_order`）下单后第几个交易日确认份额：预计确认日 = 定价日 T + lag，按基金类型取（QDII / FOF 从东方财富基金概况推断，取不到按 `default` 并在订单里标 `lag_source=default`）。改 `core/config/defaults.yaml` 的 `fund:` 段，只写想改的键（与内置默认深合并，如 `fund: {confirm_lag_days: {qdii: 3}}`）；dict 型，暂不进 API 白名单（ADR-017）、env 无效。见 [ADR-027](adr/027-ledger-activity-types.md) D5 |

### 运行时覆盖示例（以关闭集中度 Lens 为例）

```bash
# 途径 1: CLI
uvx openinvest config --set verdict.concentration_lens_enabled false

# 途径 2: REST API（deprecated surface —— 仅 remote hub 模式）
curl -X PUT http://localhost:8765/api/config -d '{"key":"verdict.concentration_lens_enabled","value":false}'
```

> 白名单 `API_SETTABLE`（`core/config/_loader.py`）只放用户安全的行为开关；机密与部署引导仍只走 env，`locked.py` 永不暴露。
