# 故障排查（doctor 全绿但还出错时读）

## `status` 成功但实时价是 0 或缺失

**原因**：yfinance 被限速，或资产所在市场闭市 + DB cache fallback 没建。

看返回的 quote——每个都有 `is_stale` flag。`true` 表示价格来自本地 DB cache
（`db/market_data.db`），不是实时。

**修法**：告诉用户"实时数据源不可达，用的是缓存数据（X 天前）。建议过几分钟再试，
或检查 `db/market_data.db` 是否被定期更新"。

## `prepare_committee X` 返回 `{"error": "asset X not in strategy.target_assets"}`

`prepare_committee` 只对 `strategy.target_assets` 里的资产工作。用户想分析没追踪的
symbol：

1. 先通过 Web GUI（Strategy 页）或 `POST /api/strategy/asset` 加进 `target_assets`
   ——见 `references/adding-assets.md`
2. 或通过 `POST /api/holdings` 加成追踪仓（`is_tracking_only: true`）——效果一样，
   不动 strategy

委员会不管是否持有都能分析，但需要 `target_assets` 配置（cap / fee / channel 信息）。

## Worker（`Agent` 调用）报错 "no such tool"

你不在 Claude Code 里（或者当前 context 没有 `Agent` 工具）。Skill 模式需要
orchestrator 能 spawn worker。

**降级方案**：单对话 6 角色输出。读 brief 里 `prompts.{macro_strategist, quant_round1,
risk_round1, quant_round2_after_risk, risk_round2_after_quant, cio}`，然后你内联写
6 段（你自己扮演所有角色）。同样的 `=== MACRO ===` / `=== QUANT_R1 ===` / ...
分隔符。`save_committee` 两种格式都接受。

降级后失去真正的 context 隔离（信息会在你的单 context 里渗透）但至少能出 verdict。

## `save_committee` 拒绝输入

最常见原因：
- 缺 6 个 section header 之一（`=== MACRO ===` 等）
- header 拼写错
- CIO 段是空的（你忘写了）

parser 严格因为存盘的文件被 Dreaming 和 Web GUI "决议归档" tab 消费。检查
6 段都在再重发。

## 同日检查说有 verdict 但你没跑过

看是谁写的：

```bash
head -5 "$INVEST_HOME/memory/.committee/$(date +%F)/<SYMBOL>.md"
```

frontmatter 有 `Provider: claude (skill mode)` 或 `Provider: deepseek`。
如果是 `deepseek`，cron `daily_report` 已经跑过 + 写过 verdict——你应该读那个
拿给用户。只在用户明确想要 Claude 视角再重跑。

## doctor 全绿但用户报 "GUI 里看不到我的数据"

用户开了 GUI 但后端没接上。检查：

1. `ps aux | grep uvicorn` —— `connectors.web_api` 在 :8765 跑吗？
2. `curl http://127.0.0.1:8765/api/health` —— 200 吗？
3. 如果 GUI 是 `invest.<域名>`，Caddy 把 `/api/*` 反代到 :8765 了吗？

让用户：
- 直接跑 `~/.claude/skills/invest/scripts/run.sh gui`（前台 uvicorn，Ctrl+C 退出），或
- 走 systemd（生产）：[docs/wiki/08-deployment.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/08-deployment.md)

## `.env` 里有 DeepSeek key 但 `daily_report` 还报 401

Key 多半拼错或被吊销。直接测：

```bash
curl -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  https://api.deepseek.com/v1/models
```

200 = key 有效，401 = key 失效。让用户去 https://platform.deepseek.com/api_keys
重发一个。

## 更深的故障

[docs/wiki/09-troubleshooting.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/09-troubleshooting.md)
（项目仓库里）有 10 类症状 → 修法的完整目录。
