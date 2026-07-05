# 工具清单（CLI 子命令 + Web API 端点）

> 本文件是**工具文档**——SKILL.md 只管 workflow（issue #133 Decision 6：
> Tool Usage 交给 MCP schema 自动发现，Skill 收缩为编排协议）。
>
> - **MCP 用户**（Claude Code plugin / codex mcp）：14 个工具带 schema 自动发现，
>   通常不用读本文件；MCP 没覆盖的长尾端点（trades/config/events/...）才来查表
> - **CLI/REST agent**（Gemini / Cursor / 脚本）：本文件是完整参考

## 子命令一览

| 命令 | 路径 | 用在 | 返回 |
|------|------|------|------|
| `doctor` | 通用 | 必跑第一步 | JSON，`status: "ready"` 或 `"needs_setup"` |
| `init [--from-stdin] [--force]` | 通用 | **不在本 skill 用**，首次安装走 `invest-setup` skill | — |
| `status` | 通用 | 看持仓 | 现金 + holdings + 实时价 + P&L |
| `strategy` | 通用 | 看策略 | target_assets + Dreaming insights |
| `history [-n N]` | 通用 | 看流水 | 最近 N 笔交易 + 委员会决议 |
| `live_prices` | 通用 | 背景行情 | VIX / TNX / USDCNY / AUDCNY / NDQ / GC=F |
| `discipline` | 通用 | "委员会拦了什么/纪律如何" | 不作为率(HOLD 占比) + 拦截冲动操作次数 + 反事实省/费钱(只读零 LLM，对齐 ADR-023)。等价 `GET /api/discipline` |
| `decisions [--days N]` | 通用 | "我听了几次建议/哪些没执行" | 决议↔干预↔执行↔结果 join + 采纳率(只读零 LLM)。等价 `GET /api/decisions`（issue #133 Decision 9）|
| `record_execution DECISION_ID [--rejected] [--reason "..."]` | 通用 写 | 用户说"我没买/我买了"时回写 | 幂等追加 executions.jsonl。**用户拒绝建议时主动问一句原因再记**（Reason Loop 采集端在你这里）。等价 `POST /api/decisions/execution` |
| `what_if [--symbol X --pct N \| --gold-pct N \| --ndq-pct N]` | 通用 | "X 跌 Y% 我亏多少" | 算术情景，无 LLM |
| `correlate --symbols A,B[,C...] [--period 6mo] [--with-llm]` | "btw" 附带 | 用户**顺嘴问**"A 跟 B 像不像"（不写入 memory/.committee，纯查询返回）| pairwise 相关矩阵 + sector + macro 关联 |
| `prepare_committee SYM` | Coordinator | 拿 brief 给 4 subagent | brief JSON + 6 段 prompts |
| `save_committee SYM` | Coordinator | 落盘 transcript | stdin 4 段输出 → markdown |
| `run_committee SYM [--force]` | Direct | 一键完整委员会 | verdict JSON + CIO memo |
| `deposit -c CCY -a N` | 通用 写 | 存入现金（任意币种） | JSON 新余额 |
| `withdraw -c CCY -a N` | 通用 写 | 取出现金，余额不足报错 | JSON 新余额 |
| `buy --symbol S --units N --price P [-c CCY] [--kind etf/equity/...]` | 通用 写 | 加仓 / 建仓（加权平均成本） | JSON action + 估算成本 |
| `sell --symbol S --units N --price P` | 通用 写 | 减仓（按 holding cost_currency 还现金） | JSON 剩余 units |
| `delete_holding --symbol S [--force]` | 通用 写 | 删除持仓行（units 必须 0 或 --force） | JSON 已删 |
| `import [--file F \| --text T] [--commit]` | 通用 读/写 | 自由文本/CSV 持仓描述 → LLM 解析成结构化持仓（券商持仓粘贴、批量录入）。默认只预览；`--commit` 非破坏写入（只加新 symbol、cash 只填当前为 0 的币种，重复导入幂等）。等价 POST /api/holdings/import | JSON `{parsed, committed, summary?}` |
| `config [--set KEY VALUE] [--clear KEY]` | 通用 读/写 | 读/改可经 API 配置的白名单参数（concentration_lens / **cash_opportunity_cost_rule**（机会成本规则，默认 OFF，ADR-024）/ risk_profile / gold_defense_dca / dreaming.llm_verify / **dca.auto_dca_enabled / dca.auto_dca_amount_cny**——自动定投开关与金额，ADR-018 / **event.watch_schedule**——event_watch 扫描窗口 crontab（按 Asia/Shanghai 解释，默认北京 8:00-次日 2:30；scheduler ≤10 分钟自动拾取，改完无需重启）/ **event.sentinel_enabled / event.sentinel_atr_mult / event.sentinel_cooldown_min / event.sentinel_schedule**——价格异动哨兵（垂直线检测，先报警邮件后触发委员会，ADR-025）：总开关 / 触发倍数(×日ATR，默认 0.8) / 同symbol同方向冷却分钟数(默认 120) / 扫描窗口 crontab(默认 5 分钟一次)，另有 event.*/staleness.* 若干，全量见 GET /api/config）。无参=读全部。等价 GET/PUT /api/config（ADR-017）| JSON 全部生效值 |

**子命令名是封闭集合 —— 上表之外的命令都不存在**。看到自己想调
`get_committee_context` / `analyze_asset` / `pull_brief` 这种名字时，停下，
回上表对照 —— 你大概率是在脑补不存在的命令名，应该选 `prepare_committee` 或
`run_committee`。

输出都是 JSON。**始终从 JSON 引用数字**，不从 `memory/*.md` markdown 读
（markdown body 是 frontmatter 的渲染产物，可能略滞后）。

### 从券商 App **截图**导入持仓（你来 OCR，后端零依赖）

用户发券商持仓**截图**时：**你自己读图**（你有视觉能力），把每行转成
`symbol/数量/成本/币种/渠道` 的文字，再走 `import --text "..."`（或 POST
/api/holdings/import）。后端 `import` 的 LLM 只解析**文字**——不要把图片塞给它
（DeepSeek/多数 chat 模型不收图，会报错）。即：截图 → 你转文字 → import，
比让后端 OCR 更准、且不挑后端模型。先 `--text`（不带 `--commit`）让用户核对预览，
确认后再 `--commit` 非破坏写入。

## Web API 写操作端点（agent 也能调）

**产品哲学**：agent（你）拥有 openInvest 全部功能。**优先走 CLI 子命令 / MCP 工具**；
只有 CLI/MCP 没覆盖的长尾操作才 curl 下面端点（默认 :8765）。Web API 已标记
deprecated（GUI 退役，存量端点服务 remote hub 模式，不再新增端点）。

用户说"记一笔交易"/"我打算买 X"/"标记成交"/"加新资产"时调这些：

| 端点 | 用在 | body 简例 |
|------|------|-----------|
| `GET /api/user` | **分析战况前必读**，拿 wealth_context（家族 backup / 账户性质 / 应急金）—— 决定怎么解释集中度 + 低现金 | — |
| `PUT /api/user/wealth_context` | 用户改家族 backup / 账户性质 / **月度补充额（开口池）**等（用户口述后 agent 代填）| `{emergency_buffer_cny?, family_backup_available?, account_purpose?, lifestyle_notes?, monthly_contribution_cny?}` |
| `POST /api/trades/record` | **记一笔意向交易**（不连真实支付，只内部账本）| `{symbol, direction: "BUY"\|"SELL", units, price?, intended_date?, note?}` |
| `GET /api/trades?limit=N` | 看最近 N 笔意向 / 已成交 | — |
| `PATCH /api/trades/{id}/status` | **标记成交**（status: "executed"）→ 自动同步 portfolio.md（更新 holdings + 扣 cash）| `{status: "executed"}` |
| `POST /api/holdings` | 新增 yfinance 跟踪资产（不下单，只录入持仓数据）| `{symbol, kind, units, avg_cost, cost_currency, channel?}` |
| `POST /api/holdings/import` | 自由文本/CSV 持仓描述 → LLM 解析（券商持仓粘贴、批量录入）。`commit:false` 只预览不落盘；`commit:true` 非破坏写入（只加新 symbol、cash 只填当前为 0 的币种）。需后端 LLM key | `{content, commit?}` |
| `PUT /api/holdings/{symbol}` | 改持仓字段 | `{units?, avg_cost?, channel?}` |
| `POST /api/deposit` / `/api/withdraw` | 调 cash 现金 | `{currency: "CNY"\|"AUD"\|..., amount}` |
| `POST /api/gold/buy` / `/sell` | 黄金买卖（含 sell_fee 自动算）| `{grams, price_per_gram}` |
| `POST /api/strategy/asset` | 加 target_assets 条目 | `{symbol, channel?, max_single_invest_cny}` |
| `GET /api/events/recent?hours=24&min_severity=low&limit=50` | 列最近 N 小时事件层感知的新闻（ADR-006）。debug / "系统现在感知到什么" | — |
| `GET /api/discipline` | 委员会纪律台账：不作为率(HOLD 占比) + 拦截冲动操作次数 + 反事实损益（对齐 ADR-023，agent 展示"它拦了什么"）| — |
| `GET /api/decisions?days=90` | 统一决策视图：决议↔干预↔执行↔结果 join + 采纳率（issue #133 Decision 9）| — |
| `POST /api/decisions/execution` | 回写用户对某决议的执行/拒绝+原因（幂等，ADR-016）| `{decision_id: "2026-07-03/GC=F", executed: false, reason?: "..."}` |
| `POST /api/events/check` | 手动跑一次 event_watch（拉新闻 + 归一化 + 入库 + 命中触发委员会）。同步 30-90s | — |
| `GET /api/config` | 看可经 API 配置的白名单参数当前生效值（+ 是否被 override + 元信息）| — |
| `PUT /api/config` | 改一条白名单 override（落盘持久、跨进程共读，优先级高于 env；ADR-017）| `{key, value}`，如 `{"key":"verdict.concentration_lens_enabled","value":false}` |
| `DELETE /api/config/{key}` | 删一条 override 回退默认 | — |

**典型流程**：用户说"我打算..."/"刚买了 X"/"我的持仓多了 Y" → 用
`POST /api/trades/record`（带 intended_date 区分计划 vs 已成交）→ 真实成交后用
`PATCH .../status executed`，后端会**自动更新 portfolio.md**（加权均价 + cash 扣减），
不需要你再调别的接口。

**完整 OpenAPI**：`http://127.0.0.1:8765/openapi.json` 查所有端点 + Pydantic schema。

