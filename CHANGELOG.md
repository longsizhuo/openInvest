# Changelog

## [0.32.1](https://github.com/longsizhuo/openInvest/compare/v0.32.0...v0.32.1) (2026-07-21)


### Bug Fixes

* **cli:** strip mcp subcommand token from argv before dispatching to mcp_server ([0513d27](https://github.com/longsizhuo/openInvest/commit/0513d27928f03c8247fc358fa50b4b63a7493d9e))

## [0.32.0](https://github.com/longsizhuo/openInvest/compare/v0.31.7...v0.32.0) (2026-07-20)


### Features

* add INVEST_ADVISORY_MODE for guest/advisory committee access ([9aa75af](https://github.com/longsizhuo/openInvest/commit/9aa75af91295879528adf13346fb20c246887431))
* INVEST_ADVISORY_MODE — 顾问模式群聊部署 ([05b8fec](https://github.com/longsizhuo/openInvest/commit/05b8fec010e40a5becd68d9268d62a53c1eb5c22))


### Bug Fixes

* **mcp:** close advisory-mode isolation gaps, tighten allowed tools ([82ac796](https://github.com/longsizhuo/openInvest/commit/82ac796af074382f30a3a321312f6db8cbc1a224))
* **mcp:** restore params and docstrings broken by advisory-mode guard ([22193c7](https://github.com/longsizhuo/openInvest/commit/22193c7eb6a3d6c8daab3db95897f4167d8de547))

## [0.31.7](https://github.com/longsizhuo/openInvest/compare/v0.31.6...v0.31.7) (2026-07-18)


### Bug Fixes

* **cr:** 依次修复 CR issue 第二批——8 处（[#231](https://github.com/longsizhuo/openInvest/issues/231)-[#234](https://github.com/longsizhuo/openInvest/issues/234) 中数据实锤与小 diff 项） ([0f42a68](https://github.com/longsizhuo/openInvest/commit/0f42a68dffb49975189460a787adfc146978009d))

## [0.31.6](https://github.com/longsizhuo/openInvest/compare/v0.31.5...v0.31.6) (2026-07-17)


### Bug Fixes

* **committee:** Sanity5 force-HOLD 漏归零 alloc + provider 硬编码 + insight slug 漂移 ([8629ede](https://github.com/longsizhuo/openInvest/commit/8629ede762be0c45d7f1788239e31fda94a17dc0))
* **infra:** sentinel_schedule env 强转 + insights UTC 存储 + Docker 泄漏账本 ([c2a74ff](https://github.com/longsizhuo/openInvest/commit/c2a74ff3a62953f7d9685d294aa4b06ed0a056d3))
* **ledger:** 堵住 PATCH 交易状态的双记账窗口（ADR-016 红线，CR 双 agent 命中） ([33a1595](https://github.com/longsizhuo/openInvest/commit/33a1595e2937ed77e25327e4eb4aa317257121c9))
* **valuation:** 0.0 价污染 total + what_if 缺 symbol 静默 + 2Y 窗高估 + pnl 标签 ([444423f](https://github.com/longsizhuo/openInvest/commit/444423fa32aa4c6544678dec8fd000806cf1d5ef))

## [0.31.5](https://github.com/longsizhuo/openInvest/compare/v0.31.4...v0.31.5) (2026-07-17)


### Bug Fixes

* **plugin:** logo 换回真实猫头鹰品牌图 + 发布脚本补 ClawHub 分类 ([0beba28](https://github.com/longsizhuo/openInvest/commit/0beba28fc1cba7d625547cc5789543012ee9da16))

## [0.31.4](https://github.com/longsizhuo/openInvest/compare/v0.31.3...v0.31.4) (2026-07-17)


### Bug Fixes

* **mcp:** 全部工具参数补 schema 级 description + 数值边界约束 ([a5acbf6](https://github.com/longsizhuo/openInvest/commit/a5acbf6843db188323e52771eb1377a48ff5e97e))

## [0.31.3](https://github.com/longsizhuo/openInvest/compare/v0.31.2...v0.31.3) (2026-07-17)


### Bug Fixes

* **mcp:** 18 个工具 description 全量英文化重写——用途/时机/Args/Returns 齐备 ([dcf2699](https://github.com/longsizhuo/openInvest/commit/dcf2699799ff39393a5528b0eaeafaf9edcf0fe0))

## [0.31.2](https://github.com/longsizhuo/openInvest/compare/v0.31.1...v0.31.2) (2026-07-17)


### Docs

* **glama:** README（en/zh）加 Glama 评分徽章 + 补 glama.json maintainer 声明 ([1b4fa83](https://github.com/longsizhuo/openInvest/commit/1b4fa83e7292740b7b8a916bb1ea7ed15a5a3a9a))

## [0.31.1](https://github.com/longsizhuo/openInvest/compare/v0.31.0...v0.31.1) (2026-07-17)


### Docs

* **readme:** add Research & Falsification section, slim config, merge philosophy ([dd03af8](https://github.com/longsizhuo/openInvest/commit/dd03af8ff238f942f8254ad0e5483302e8bde54c))
* **readme:** add Research & Falsification section, slim config, merge philosophy ([c41aec6](https://github.com/longsizhuo/openInvest/commit/c41aec6938d6fe454ed566ca6021b486f5ee537f))

## [0.31.0](https://github.com/longsizhuo/openInvest/compare/v0.30.5...v0.31.0) (2026-07-16)


### Features

* **invest-setup:** SKILL.md 与 README 英文化 + 修存量 YAML 解析错误 ([6e1badd](https://github.com/longsizhuo/openInvest/commit/6e1badd014a40c170b5448a72d566ae0af43edce))
* **skill:** SKILL.md 与全部 references 英文化——正文英文 + 双语触发短语 ([83b6e59](https://github.com/longsizhuo/openInvest/commit/83b6e59a4cca8ecdb65214c4381b04faa336f87f))

## [0.30.5](https://github.com/longsizhuo/openInvest/compare/v0.30.4...v0.30.5) (2026-07-16)


### Docs

* **agents:** OpenClaw 安装改一键 clawhub 命令 + 澄清两个 OpenClaw ([449c0df](https://github.com/longsizhuo/openInvest/commit/449c0dff26c516cb41907c3d2f30ab1039b77b17))

## [0.30.4](https://github.com/longsizhuo/openInvest/compare/v0.30.3...v0.30.4) (2026-07-15)


### Bug Fixes

* **event-watch:** 宏观关键词轮换，别只靠单条 Fed 兜底查询 ([41e2565](https://github.com/longsizhuo/openInvest/commit/41e25655a91dc1cd2c9388f4122eb45650fafd57))

## [0.30.3](https://github.com/longsizhuo/openInvest/compare/v0.30.2...v0.30.3) (2026-07-15)


### Docs

* **research:** 固化 issue [#210](https://github.com/longsizhuo/openInvest/issues/210) 事件研究脚本进 experiments/ ([50aac1d](https://github.com/longsizhuo/openInvest/commit/50aac1de3d240bf439af078fdd2a61be91f5c585))

## [0.30.2](https://github.com/longsizhuo/openInvest/compare/v0.30.1...v0.30.2) (2026-07-15)


### Bug Fixes

* **sentiment:** opportunity stance 别再暗示方向性利好 ([4040f71](https://github.com/longsizhuo/openInvest/commit/4040f719615ccf1915f937884ef9fcb8da4d4ebe))

## [0.30.1](https://github.com/longsizhuo/openInvest/compare/v0.30.0...v0.30.1) (2026-07-15)


### Bug Fixes

* **pnl:** outperform feed 静默失效——get_all_series 补 start_date + series.key ([f20e3e6](https://github.com/longsizhuo/openInvest/commit/f20e3e61a41848a6be757e9c94078dabedbd8072)), closes [#197](https://github.com/longsizhuo/openInvest/issues/197)

## [0.30.0](https://github.com/longsizhuo/openInvest/compare/v0.29.0...v0.30.0) (2026-07-15)


### Features

* **news:** searxng 元搜索接入 event_watch 新闻源 ([1089fe9](https://github.com/longsizhuo/openInvest/commit/1089fe90883cbc32a4594fe7a1405619b01e480f))
* **news:** searxng 元搜索接入 event_watch 新闻源（第 5 源） ([fab57ec](https://github.com/longsizhuo/openInvest/commit/fab57ecee011e18d1173b3d09d5987a99e8af702))
* **skill:** 新闻投喂补「情报哨兵」定时主动喂料指引 ([6123b9e](https://github.com/longsizhuo/openInvest/commit/6123b9e15101df3b44a86f01eaf8c6b41b070474))


### Docs

* **news:** searxng 模块补定位说明——与特定 agent 无关、本身即引擎抽象层 ([cb4eb04](https://github.com/longsizhuo/openInvest/commit/cb4eb047179056df63e4b870b009dd0f8def5ed5))
* **wiki:** 21 章情报哨兵——三层情报架构与 agent 定时喂料标准配方 ([ae5b527](https://github.com/longsizhuo/openInvest/commit/ae5b5274ddfe493e6f97e63cf2dfeb2fd9a943fe))
* 情报哨兵产品化——SKILL.md 指引 + wiki 21 章 ([97989d8](https://github.com/longsizhuo/openInvest/commit/97989d813cd205f2558766e35a1d596f11790b83))

## [0.29.0](https://github.com/longsizhuo/openInvest/compare/v0.28.1...v0.29.0) (2026-07-14)


### Features

* **daily-report:** chat cron 渲染优化——去 HTML div 包裹 + 置顶速览 ([ffbc6f1](https://github.com/longsizhuo/openInvest/commit/ffbc6f1b8447110ccc4a86079a69be938a5b206f))
* **services:** 事件/verdict 报警实时推送 Discord DM ([a2851e4](https://github.com/longsizhuo/openInvest/commit/a2851e4bc1db036529a32a50991afeed89f20c66))
* **services:** 事件/verdict 报警实时推送 Discord DM（经 ChatBot 转发） ([d0b558b](https://github.com/longsizhuo/openInvest/commit/d0b558b05b100f938c0a0e4df3d75fe32bf02ccb))


### Bug Fixes

* **committee:** save_committee 落盘 Provider 标注可配置，不再硬编码 claude ([2138d2f](https://github.com/longsizhuo/openInvest/commit/2138d2f41f1428502b01c37484c6223b2d7928bd))
* **committee:** SDKAgent 缺 LLM key 时抛清晰可操作错误，不再裸抛 OpenAIError ([c387f51](https://github.com/longsizhuo/openInvest/commit/c387f5117ecc4f2111a75166be5aa14d70a74a53))
* **daily-report:** 第二意见 CLI 从 gemini 迁移到 agy（原环境已切换命令） ([012e2c9](https://github.com/longsizhuo/openInvest/commit/012e2c912f16b81d92113bbd66b464049d23b76f))


### Docs

* **skill:** Hermes Coordinator 协议 + 路由表改按交互/无人值守分轴 ([27d06ee](https://github.com/longsizhuo/openInvest/commit/27d06ee63b23e645210339a905b0fde6475f6dbb))
* wiki 同步——Coordinator 路由改按交互/无人值守分轴，cron 教程改 provider-agnostic key ([a7748c7](https://github.com/longsizhuo/openInvest/commit/a7748c7e84a001a5857786b08d6fd84d87cbbb8c))

## [0.28.1](https://github.com/longsizhuo/openInvest/compare/v0.28.0...v0.28.1) (2026-07-12)


### Docs

* **wiki:** schema_source 锚点跟随 calc 搬迁——OKF frontmatter lint 修红 ([2b55367](https://github.com/longsizhuo/openInvest/commit/2b55367761f3fda39599c71102fa7fbf43d46c6a))

## [0.28.0](https://github.com/longsizhuo/openInvest/compare/v0.27.0...v0.28.0) (2026-07-12)


### Features

* **events:** 事件账本 ingested_by 溯源字段——坏 verdict 可反查坏输入是谁喂的 ([71b31cb](https://github.com/longsizhuo/openInvest/commit/71b31cb126e8e010445b55e57f05c71c556b803d))


### Refactor

* **calc:** core/benchmarks 纯核 → calc/series（BenchmarkSeries/to_pct_series/_generate_constant_apr） ([c21bf66](https://github.com/longsizhuo/openInvest/commit/c21bf66c6c5effa583e5ee70018f3e024c0ba2e5))
* **calc:** core/regime_probability 纯核 → calc/regime_probability ([d1602d9](https://github.com/longsizhuo/openInvest/commit/d1602d9064a7bf2581a852527fd05d394adf0ae5))
* **calc:** sentiment/gold 纯核 → calc/sentiment + calc/gold ([383218e](https://github.com/longsizhuo/openInvest/commit/383218e1e8c0af16349aafcc91796d04e9b382e1))
* **calc:** utils/exchange_fee 拆 calc/transaction_costs + calc/timeframe_analysis ([20eed24](https://github.com/longsizhuo/openInvest/commit/20eed24f58467206cc0c31338837f0b07a686b04))
* **calc:** 新建 openinvest.calc 计算层——regime/market_metrics/strategy_metrics/backtest_reward/symbols 五整模块搬迁 ([3e11882](https://github.com/longsizhuo/openInvest/commit/3e118824482310ae96f4f6eed768fec5d885261f))
* **core:** debate_calc + intervention_rules + event_format + decision_calc 拆分；daily_report_builder 删死 import ([e465783](https://github.com/longsizhuo/openInvest/commit/e4657834ec62198a14b2ca2b49222fa247251b35))
* **jobs:** pnl_snapshot 渲染纯核 → jobs/pnl_render ([f8e5561](https://github.com/longsizhuo/openInvest/commit/f8e5561381772d3cbf69af0d963705b1f29b2118))
* **jobs:** verdict/path review 统计纯核 → jobs/review_calc；dreaming 聚合纯核 → jobs/dreaming_calc ([f1da2fd](https://github.com/longsizhuo/openInvest/commit/f1da2fdfae8345b0c8dfadb46e35a91da6662b64))


### Docs

* **adr:** ADR-026 决策核心三层纯度契约 + wiki/CLAUDE.md 同步 ([028d64e](https://github.com/longsizhuo/openInvest/commit/028d64e655f566af5262f6391223752ededc0fb0))

## [0.27.0](https://github.com/longsizhuo/openInvest/compare/v0.26.2...v0.27.0) (2026-07-12)


### Features

* **cli:** daily_report 子命令——邮件同款日报走 stdout，宿主 agent cron 原样投递 ([cd75be6](https://github.com/longsizhuo/openInvest/commit/cd75be6220fad5905430d9a82018d87f545bf66f))


### Docs

* **skill:** daily_report 子命令指引——日报格式后端统一，cron 投递归宿主 agent ([7f78c68](https://github.com/longsizhuo/openInvest/commit/7f78c684bec7d88a9cf4f7ba1fdf0db2968ee072))
* 日报 cron 教程改 --no-agent 脚本方案（prompt 归零）+ AGENTS.md 固化 Hermes 测试路径 ([ae1e4b3](https://github.com/longsizhuo/openInvest/commit/ae1e4b32f537c5b43a2706b87653da212b8966d4))

## [0.26.2](https://github.com/longsizhuo/openInvest/compare/v0.26.1...v0.26.2) (2026-07-12)


### Docs

* 宿主 agent 侧日报 cron 教程（Hermes 示例 + 推荐 prompt）；PYPI/jobs README 同步 agent 口径 ([244093a](https://github.com/longsizhuo/openInvest/commit/244093a314c0059e1e8d44a4060e02a74d0ad736))

## [0.26.1](https://github.com/longsizhuo/openInvest/compare/v0.26.0...v0.26.1) (2026-07-12)


### Docs

* **skill:** Direct 路径 agent 示例补 Hermes / OpenClaw，与市场用户构成对齐 ([077c266](https://github.com/longsizhuo/openInvest/commit/077c26624d6e1f2472ac57bc1597d19baf3338b6))
* 文案去 Claude 中心化——README/wiki 四 agent 并列 + OpenClaw 改 MCP 声明式接入 ([6c7cbef](https://github.com/longsizhuo/openInvest/commit/6c7cbefe91c4f764ef7702ac707ce0499c16cf0c))

## [0.26.0](https://github.com/longsizhuo/openInvest/compare/v0.25.0...v0.26.0) (2026-07-12)


### Features

* **mcp:** streamable-HTTP transport（--http）——remote MCP，REST 退役路线 A ([f0ef9e7](https://github.com/longsizhuo/openInvest/commit/f0ef9e7f679e6a5d451e59d90f79c70a8cd3d407))


### Docs

* **deploy:** remote MCP 部署链路（invest-mcp.service + Caddy /mcp + spoke 注册） ([a1f7541](https://github.com/longsizhuo/openInvest/commit/a1f7541071969318d626fd1dd51b5f2111723d76))
* **mcp:** remote MCP 标注 BETA——作者未在真实多设备环境实测 ([e6cc184](https://github.com/longsizhuo/openInvest/commit/e6cc1844d2bb0a2b36a8a7ca125d871afbb912d9))
* **skill:** remote MCP spoke 直连指引 ([a8c36a3](https://github.com/longsizhuo/openInvest/commit/a8c36a36d9f4c2581b3f32ec0c4955badf4f4eb0))
* **skill:** remote MCP 指引同步 BETA 标注 ([67d584c](https://github.com/longsizhuo/openInvest/commit/67d584c8a49b06f9132b54f50e38aba311df4cea))

## [0.25.0](https://github.com/longsizhuo/openInvest/compare/v0.24.5...v0.25.0) (2026-07-11)


### Features

* **strategy:** strategy 写操作补齐 CLI + MCP——agent 读写对等 ([eb1bdf6](https://github.com/longsizhuo/openInvest/commit/eb1bdf635d487f5bc964fb8085794d66dc18a646))


### Docs

* **hermes:** 注册器提示的 MCP 工具数漏改 15→18 ([6ba36f8](https://github.com/longsizhuo/openInvest/commit/6ba36f82a1f5932c0330c383942c1f0970ad39e8))
* **skill:** strategy 写工具注册进 agent 指引 ([5c0d9e4](https://github.com/longsizhuo/openInvest/commit/5c0d9e4c8850667cde3962763647570a6e7224a6))

## [0.24.5](https://github.com/longsizhuo/openInvest/compare/v0.24.4...v0.24.5) (2026-07-10)


### Bug Fixes

* **p2:** issue [#179](https://github.com/longsizhuo/openInvest/issues/179) P2 批量清理——安全小修 + 文档对齐 + CI 防线补齐 ([6dc76cd](https://github.com/longsizhuo/openInvest/commit/6dc76cd15a34e4c28b53d26953b3b8a2fe8050d6))


### Refactor

* **core:** safe_symbol 收敛到中立层 utils.symbols 单一可信源 ([4ffee50](https://github.com/longsizhuo/openInvest/commit/4ffee50d1a65d9c7fb1b027e0507d3b90a167291))


### Docs

* **mcp:** MCP 工具数 14→15 全仓对齐 + 枚举补 ingest_event ([0d7fdcd](https://github.com/longsizhuo/openInvest/commit/0d7fdcd837f5b332e46dcbd6eba0ba2215fe69a5))

## [0.24.4](https://github.com/longsizhuo/openInvest/compare/v0.24.3...v0.24.4) (2026-07-10)


### Bug Fixes

* **data:** 行情增量缓存加复权拼接哨兵 ([67f46a6](https://github.com/longsizhuo/openInvest/commit/67f46a635608585388a53cd3ab9f736992e66edb))
* **research:** backtest_eval 组合对比两处偏置 ([15f1043](https://github.com/longsizhuo/openInvest/commit/15f1043b637a6e61cb19cc42083bb078c5cb3d2b))


### Docs

* **regime:** 买回点/概率提示统一标注'期末'口径 ([4946cb4](https://github.com/longsizhuo/openInvest/commit/4946cb44c60ca693688098dc6970c90d404d355c))

## [0.24.3](https://github.com/longsizhuo/openInvest/compare/v0.24.2...v0.24.3) (2026-07-10)


### Bug Fixes

* **review:** verdict_review 决议锚点过去侧护栏 + flat band 改 as-of ATR ([5f7e2f7](https://github.com/longsizhuo/openInvest/commit/5f7e2f72fe18e862c60ca9fd0c81cc01312a043c))

## [0.24.2](https://github.com/longsizhuo/openInvest/compare/v0.24.1...v0.24.2) (2026-07-10)


### Bug Fixes

* **accuracy:** 公开命中率固定单一 horizon + 同样本市场基率 ([dc2bfcf](https://github.com/longsizhuo/openInvest/commit/dc2bfcf9148435b510d7821e4c13cfbd04cfdbfa))
* **backtest:** PaperTradeSimulator 黄金腿补 USDCNY 汇率 ([d1975a4](https://github.com/longsizhuo/openInvest/commit/d1975a4e997c915811993d02994f6f17e4ca0e4c))
* **pnl:** 公开 PnL 图口径脚注 + 图例去持仓 symbol ([b20abe7](https://github.com/longsizhuo/openInvest/commit/b20abe77ebd468a61d55b34c7fb23993e83c73de))

## [0.24.1](https://github.com/longsizhuo/openInvest/compare/v0.24.0...v0.24.1) (2026-07-10)


### Bug Fixes

* **deploy:** 修复 src-layout 迁移后整体失效的 Docker 部署面 ([1f8d833](https://github.com/longsizhuo/openInvest/commit/1f8d833544269d3cfbd54a49d6fc43be9b304cf8))
* **privacy:** 红线[#2](https://github.com/longsizhuo/openInvest/issues/2) 补漏——小样本桶 hit 一并抑制 + 堵减法逆算通道 ([4907692](https://github.com/longsizhuo/openInvest/commit/4907692c8980be60521e8f13ab2bfc9591f0b4e9))


### Docs

* **wiki:** 12-verification 加 ADR-022 污染桶标注 + 修反向免责声明 ([b36d243](https://github.com/longsizhuo/openInvest/commit/b36d243653b5788b093c9bc3c3c16b6d1f694f50))

## [0.24.0](https://github.com/longsizhuo/openInvest/compare/v0.23.0...v0.24.0) (2026-07-09)


### ⚠ BREAKING CHANGES

* **committee:** 移除 web API 端点 PUT /api/user/wealth_context 和 WealthContextRequest 模型；GET /api/user 不再返回 wealth_context / exchange_buffer_cny / last_payday 字段；UserData schema 去掉这些字段 （extra="allow" 下旧 user.md 里的残留字段不报错，但不再被任何代码消费）； 删除 payday_check job（自动月度入账，改手动 /deposit）。

### Bug Fixes

* **committee:** 修 full_validation.py 残留 wealth_view_override 调用 ([2f3e7ef](https://github.com/longsizhuo/openInvest/commit/2f3e7eff9ab49d6b2982f2649cab9d6d30e2d319))


### Refactor

* **committee:** 删除 WealthContextOfficer 角色 + user.md 死字段 ([a425e9d](https://github.com/longsizhuo/openInvest/commit/a425e9deafc02d1fcdbacd7b2c3798ab0c68fd76))

## [0.23.0](https://github.com/longsizhuo/openInvest/compare/v0.22.1...v0.23.0) (2026-07-08)


### Features

* **committee:** localize committee outputs ([723be05](https://github.com/longsizhuo/openInvest/commit/723be05f4b579b4a6a79a3e352598a639ab5297e))
* **config:** add invest language setting ([18b094e](https://github.com/longsizhuo/openInvest/commit/18b094e7d03db5803a691ccaa9905122271aefec))
* **skill:** 新增 invest-backup skill，备份/恢复不入 git 的关键数据 ([6c06dbc](https://github.com/longsizhuo/openInvest/commit/6c06dbc54a41c8c468a862ed82b8df74afa1439a))


### Bug Fixes

* **committee:** 补上语言覆盖面里剩余的早退分支 + zh-CN 配置一致性 ([5659588](https://github.com/longsizhuo/openInvest/commit/56595887c89427147544ef6937a9266e9175c0cc))
* **committee:** 补全委员会语言覆盖面，修正 rebuttal 字数约束误删 ([f1af463](https://github.com/longsizhuo/openInvest/commit/f1af463f0b00f82b660e7d167ad901d4e2d2ceb3))
* **migrate:** 给 migrate_profile.py 补 safety guard，防止误跑清空真实数据 ([588df10](https://github.com/longsizhuo/openInvest/commit/588df1059800b3b73a1d9e6279940948797376b1))
* **skill:** dev-mode 分支保留真实退出码，不再吞掉非零状态 ([4052078](https://github.com/longsizhuo/openInvest/commit/4052078d0b07f8be8d4a25ec43e9a1b4dbc2405b))
* **skill:** 修正 plugin/ 重排后 REPO_ROOT 相对路径少跳一层 ([48b9b20](https://github.com/longsizhuo/openInvest/commit/48b9b2077453dd0e2be1caecf6a60927eb582c17))


### Docs

* add issue 132 i18n implementation note ([de68f87](https://github.com/longsizhuo/openInvest/commit/de68f87d230e652310e4f6c5baf7c916d2cca5eb))

## [0.22.1](https://github.com/longsizhuo/openInvest/compare/v0.22.0...v0.22.1) (2026-07-07)


### Docs

* **agents:** wiki 20 补 OpenClaw 接入（workspace skills 拷贝；plugins install 是 stub 勿用） ([05581e7](https://github.com/longsizhuo/openInvest/commit/05581e7264725304037e73cc8dc7fa5c780cb4ac))

## [0.22.0](https://github.com/longsizhuo/openInvest/compare/v0.21.2...v0.22.0) (2026-07-06)


### Features

* **hermes:** 仓库根 plugin manifest——hermes plugins install longsizhuo/openInvest 一键装 skills ([957952c](https://github.com/longsizhuo/openInvest/commit/957952c094fe237f4071bd5cd1be6d29d4349a22))

## [0.21.2](https://github.com/longsizhuo/openInvest/compare/v0.21.1...v0.21.2) (2026-07-06)


### Docs

* **skill:** SKILL.md 补 Hermes 原生元数据（platforms + metadata.hermes.tags）——增量字段，Claude/Codex 忽略 ([fa69fd7](https://github.com/longsizhuo/openInvest/commit/fa69fd79db744a6fdbf68fb42bd174098c06275a))

## [0.21.1](https://github.com/longsizhuo/openInvest/compare/v0.21.0...v0.21.1) (2026-07-06)


### Docs

* **agents:** Hermes 接入指南（config.yaml MCP + agentskills 拷入）+ 行情 skill 联动投喂；uv.lock 版本号同步 ([999214b](https://github.com/longsizhuo/openInvest/commit/999214bf6fde44fa03a030efead4387317975c72))

## [0.21.0](https://github.com/longsizhuo/openInvest/compare/v0.20.0...v0.21.0) (2026-07-06)


### Features

* **events:** ingest_event agent 投喂 + RSS 泛头条预过滤（[#153](https://github.com/longsizhuo/openInvest/issues/153) 方案①②） ([c4c9c34](https://github.com/longsizhuo/openInvest/commit/c4c9c34229126e7c43829b53186370e766c60888))
* **events:** ingest_event agent 投喂 + RSS 预过滤（[#153](https://github.com/longsizhuo/openInvest/issues/153) ①②） ([2740707](https://github.com/longsizhuo/openInvest/commit/2740707b1058824a53b81b8281d24d7c1446b79d))
* **events:** 中文快讯 wire（akshare 东财+新浪7×24）——A 股 symbol 自动激活（[#153](https://github.com/longsizhuo/openInvest/issues/153)） ([f7857f3](https://github.com/longsizhuo/openInvest/commit/f7857f3fb73b17c67026b5d4540175f99933c9f0))
* **events:** 中文快讯 wire（akshare）——A 股 symbol 自动激活（[#153](https://github.com/longsizhuo/openInvest/issues/153) ③） ([40831fe](https://github.com/longsizhuo/openInvest/commit/40831fee9a0957d1293eb7e6f3c399b599ccdbfb))

## [0.20.0](https://github.com/longsizhuo/openInvest/compare/v0.19.1...v0.20.0) (2026-07-05)


### ⚠ BREAKING CHANGES

* **plugin:** skill 源文件 git 路径变更 skills/* → plugin/skills/*（根 skills/ 符号链接保持磁盘兼容）

### Features

* **plugin:** Codex plugin cache 瘦身 44MB→156KB——真身入 plugin/，marketplace source 指回 ./plugin ([c3ad092](https://github.com/longsizhuo/openInvest/commit/c3ad0929960309afc90b0d822d8a0ad9d55c6ed4))

## [0.19.1](https://github.com/longsizhuo/openInvest/compare/v0.19.0...v0.19.1) (2026-07-05)


### Bug Fixes

* **db:** WAL 膨胀治理——启动 checkpoint(TRUNCATE) + 读路径 rollback + 回填收尾截断 ([5e900e0](https://github.com/longsizhuo/openInvest/commit/5e900e03f9f3f5057f3e558e460ad5bd10339464)), closes [#104](https://github.com/longsizhuo/openInvest/issues/104)

## [0.19.0](https://github.com/longsizhuo/openInvest/compare/v0.18.1...v0.19.0) (2026-07-05)


### ⚠ BREAKING CHANGES

* **security:** 设置 INVEST_API_TOKEN 后 loopback 请求也需要 Bearer token

### Bug Fixes

* **security:** INVEST_API_TOKEN 全域强制——删 loopback 豁免（[#106](https://github.com/longsizhuo/openInvest/issues/106)） ([76d4d8e](https://github.com/longsizhuo/openInvest/commit/76d4d8e376a5c6d99fd9d5dd8fdad5cea9919c75))

## [0.18.1](https://github.com/longsizhuo/openInvest/compare/v0.18.0...v0.18.1) (2026-07-05)


### Bug Fixes

* **mcp:** server.json description 压到 registry 100 字符上限内 ([d323020](https://github.com/longsizhuo/openInvest/commit/d323020e04a1ba20633a9c71d7bfb710ab126469))

## [0.18.0](https://github.com/longsizhuo/openInvest/compare/v0.17.1...v0.18.0) (2026-07-05)


### Features

* **mcp:** openinvest mcp 子命令 + 官方 MCP Registry 自动发布（issue [#133](https://github.com/longsizhuo/openInvest/issues/133) P0） ([256573e](https://github.com/longsizhuo/openInvest/commit/256573e1125244cba4225e868264144b54029bca))

## [0.17.1](https://github.com/longsizhuo/openInvest/compare/v0.17.0...v0.17.1) (2026-07-05)


### Docs

* 全量文档对齐 2026-07-05 现实——GUI/NapCat 退役、PyPI+uvx 分发、Web API deprecated ([f828195](https://github.com/longsizhuo/openInvest/commit/f8281951fd270d844c0986e28a34995b758b1ce3))

## [0.17.0](https://github.com/longsizhuo/openInvest/compare/v0.16.0...v0.17.0) (2026-07-05)


### ⚠ BREAKING CHANGES

* **gui:** run.sh gui 子命令移除；web_api 不再挂载 GUI 静态文件

### Bug Fixes

* **build:** 回滚 [#143](https://github.com/longsizhuo/openInvest/issues/143) 误捎带的 uv.lock 全量重解析 ([2cd2e3e](https://github.com/longsizhuo/openInvest/commit/2cd2e3e08311519f36decbec812bafb5c3e47d26))
* **build:** 回滚误重解析的 uv.lock——元数据 commit 不该捎带依赖全量升级 ([ed53a0f](https://github.com/longsizhuo/openInvest/commit/ed53a0f2cbecbcda201730d9d8f071949b69c78e))


### Refactor

* **gui:** GUI 壳层退役——后端不再 serve 静态文件，Web API 标记 deprecated ([390c87d](https://github.com/longsizhuo/openInvest/commit/390c87d6c43775d03abe3dfd42df10bf74cc1679))

## [0.16.0](https://github.com/longsizhuo/openInvest/compare/v0.15.0...v0.16.0) (2026-07-05)


### ⚠ BREAKING CHANGES

* **dist:** run.sh 不再 clone/更新后端仓库，后端版本由 PyPI 管理
* **pkg:** Python 包名 core/db/utils/... → openinvest.*；直接 import 老包名的外部脚本需改 openinvest.<pkg> 或走根目录 shim

### Features

* add anonymous installation telemetry and wiki documentation ([#130](https://github.com/longsizhuo/openInvest/issues/130)) ([0a79e71](https://github.com/longsizhuo/openInvest/commit/0a79e718fefcdfae338fdbea3c056dcdff83de71))
* **decisions:** decision accounting 闭环——决议↔执行↔结果读时 join (issue [#133](https://github.com/longsizhuo/openInvest/issues/133) Decision 9) ([8860e2f](https://github.com/longsizhuo/openInvest/commit/8860e2fbe020e2dce8c196f1bd05b6da22b4518d))
* **dist:** run.sh 收敛为 uvx 薄转发——退役 clone/uv sync/自愈 180 行 bash ([c64ba2a](https://github.com/longsizhuo/openInvest/commit/c64ba2ab5280ff9a297859d9cef38924463e8d77))
* **event-watch:** 扫描窗口修正为北京 8:00-次日2:30 并进 config 白名单 ([#128](https://github.com/longsizhuo/openInvest/issues/128)) ([54ad4e0](https://github.com/longsizhuo/openInvest/commit/54ad4e0e38c7afe23b8b40be32266cf0d1ef59fe))
* **mcp:** 14 工具全量打 tool annotations——status 和 sell 不再同级 ([6d8b334](https://github.com/longsizhuo/openInvest/commit/6d8b3345264b7fd1e8869f025f3dd32b1fb6b00f))
* **mcp:** MCP 工具危险等级标注（readOnly/destructive/idempotent hints） ([d3d82a3](https://github.com/longsizhuo/openInvest/commit/d3d82a34fdba0c90a041ae40ae3723ee32f510b0))
* **mcp:** stdio MCP adapter——14 工具复用 service 层 (issue [#133](https://github.com/longsizhuo/openInvest/issues/133) Phase 3) ([13e48d5](https://github.com/longsizhuo/openInvest/commit/13e48d55b80fa7fc689e07aec9aa1decb80391a2))
* **plugin:** Codex plugin.json 声明 mcpServers——装 plugin 即自动注册 MCP（与 Claude 对齐） ([fe54e28](https://github.com/longsizhuo/openInvest/commit/fe54e28209552eef5ce83e627450ce269d3ae0bf))
* **plugin:** 捆绑 MCP server——装 plugin 即得 14 工具零配置 (issue [#133](https://github.com/longsizhuo/openInvest/issues/133) Phase 4) ([d046d7c](https://github.com/longsizhuo/openInvest/commit/d046d7c6a00fa12c0bdfb05f388335dfb0289e0e))
* **sentinel:** 价格异动哨兵——垂直线先报警后触发委员会 (ADR-025) ([#129](https://github.com/longsizhuo/openInvest/issues/129)) ([b9ef160](https://github.com/longsizhuo/openInvest/commit/b9ef160e9f50ca1531579ec13004bf37413c2df1))
* **skill:** run.sh 加 mcp 子命令——plugin .mcp.json 的 stdio 启动入口 ([e0b27c9](https://github.com/longsizhuo/openInvest/commit/e0b27c9847ad1ae0fa30ab27f039e5028ac751fe))
* **verdict:** 现金机会成本规则改 opt-in,默认 OFF (ADR-024) ([5320926](https://github.com/longsizhuo/openInvest/commit/532092615a33bc263e9040e32c56f9f928edd42b))
* **verdict:** 现金机会成本规则改 opt-in,默认 OFF(ADR-024) ([8ea8a73](https://github.com/longsizhuo/openInvest/commit/8ea8a731087ee16827e92a1c0458fea1caaa5edc))


### Bug Fixes

* add missing __init__.py files for capabilities/ packages ([4af6d03](https://github.com/longsizhuo/openInvest/commit/4af6d03b2c56ff880adefde0391df6cdd2a3f259))
* **decisions:** 修复 code review 全部 10 项发现 ([81822d5](https://github.com/longsizhuo/openInvest/commit/81822d52cec924cb1476b07089795a497daf5300))
* **dist:** code review [#139](https://github.com/longsizhuo/openInvest/issues/139) 全部 10 项发现——uvx 纯数据目录形态的 onboarding/.env/提示链 ([41b6054](https://github.com/longsizhuo/openInvest/commit/41b6054eb16bf06ea1e29a56c13ec4040a0c39ab))
* **pkg:** __version__ 从包元数据读——不再与 release-please 管的 pyproject 双源 ([441555e](https://github.com/longsizhuo/openInvest/commit/441555e81081970a4881c2c37821bf8b2f23695c))
* **pkg:** 重排收尾——补 __init__.py / yml entry 路径 / JOBS_DIR 包内解析 / CI smoke+lint 更新 ([d3f05fd](https://github.com/longsizhuo/openInvest/commit/d3f05fd41972f7d1ab5e821e4eb5bad6147d2214))
* **plugin:** Codex 安装 skills 为空——marketplace source 改指仓库根 ([28e61d2](https://github.com/longsizhuo/openInvest/commit/28e61d26b001446337e28b327827b5f35e0bede7))
* remove eager import cascade in committee __init__.py + stale doc paths ([b8e84ab](https://github.com/longsizhuo/openInvest/commit/b8e84ab1a147c94ad7297bd9181c405c29938892))
* 修复优先级最高的三个真 bug([#105](https://github.com/longsizhuo/openInvest/issues/105) 假死 / [#108](https://github.com/longsizhuo/openInvest/issues/108) 现金穿透 / [#109](https://github.com/longsizhuo/openInvest/issues/109) 并发重复入账) ([#127](https://github.com/longsizhuo/openInvest/issues/127)) ([6ec98a9](https://github.com/longsizhuo/openInvest/commit/6ec98a9c2d75a523c67df478645b1380310b9b00))


### Refactor

* agents/skills/ → capabilities/committee/&lt;role&gt;/ (co-located .py + .md) ([838a834](https://github.com/longsizhuo/openInvest/commit/838a8347fdb0f2ab472b546d265ac331aa0ccd5f))
* agents/skills/ → capabilities/committee/&lt;role&gt;/ (co-located .py + .md) ([#135](https://github.com/longsizhuo/openInvest/issues/135)) ([a4b6465](https://github.com/longsizhuo/openInvest/commit/a4b6465b593718af3f78f093ec829edc07e6303d))
* **pkg:** src/openinvest 布局——8 顶层包收进命名空间，PyPI 可发布 ([052aff7](https://github.com/longsizhuo/openInvest/commit/052aff71565735d766f229d9eb61e37d4c66306a))
* **skill:** SKILL.md 收缩为 workflow——工具表移交 references/tools.md (issue [#133](https://github.com/longsizhuo/openInvest/issues/133) Decision 6) ([23eb04c](https://github.com/longsizhuo/openInvest/commit/23eb04c05eb94740e31f7c9bb0ed7eae3eae5c38))


### Docs

* address Copilot code review comments on README and README_zh ([21185db](https://github.com/longsizhuo/openInvest/commit/21185db260abc5e10771b97b7a2d58b0a822dfa4))
* **okf:** frontmatter schema_source 路径跟随 src/ 搬迁（41 处 dangling 修复） ([72c8273](https://github.com/longsizhuo/openInvest/commit/72c8273249f36a67c4a523cd1d52be0892af18a0))
* **pkg:** CLAUDE.md 关键文件速查换 src/ 布局 + INVEST_ROOT 纪律；dist/ 入 gitignore ([5235250](https://github.com/longsizhuo/openInvest/commit/523525049d359cf0905bee9fe8b0e0f18d7d8c68))
* **plugin:** Codex 安装命令改为实测通过的两步式 + codex mcp add 注册说明 ([92d6794](https://github.com/longsizhuo/openInvest/commit/92d6794dd134d3bdd1d28b3665451b87ac83439d))
* refactor README and add README_zh.md for double-language support ([#131](https://github.com/longsizhuo/openInvest/issues/131)) ([ff08e46](https://github.com/longsizhuo/openInvest/commit/ff08e4695b7d66245bb9ae1a3e53864cf3727321))
* **skill:** B2 截图持仓导入(agent-OCR 路径) ([2e4c83a](https://github.com/longsizhuo/openInvest/commit/2e4c83a2ce547e7b4ef3eb5b3a9a7e5ce3a44a8b))
* **skill:** config 子命令列出 cash_opportunity_cost_rule(ADR-024) ([5288dc5](https://github.com/longsizhuo/openInvest/commit/5288dc58edfcaadf7ae0abcb8622c50b42569c27))
* **skill:** decisions / record_execution 子命令 + /api/decisions 端点进 SKILL.md ([800ab61](https://github.com/longsizhuo/openInvest/commit/800ab6115f40df810e457e6c24853eb97138e11a))
* **skill:** 截图持仓导入走 agent-OCR(你读图→转文字→import) ([d294fbf](https://github.com/longsizhuo/openInvest/commit/d294fbf101f86bf2e0ebf52095eba667aa6f5e0d))
* **wiki:** 第 20 章使用教程——安装(plugin/MCP)→onboarding→日常→决策闭环 ([258bbc2](https://github.com/longsizhuo/openInvest/commit/258bbc275786be6c98d8c1e762333014dad7d903))

## [0.15.0](https://github.com/longsizhuo/openInvest/compare/v0.14.1...v0.15.0) (2026-06-30)


### Features

* **deploy:** GitHub Actions 零成本自托管——fork+secrets 每日委员会邮件 ([155a13b](https://github.com/longsizhuo/openInvest/commit/155a13b5d2591280328e452106054bd2cf2ea94b))
* **deploy:** GitHub Actions 零成本自托管(fork+secrets 每日委员会邮件) ([f240e78](https://github.com/longsizhuo/openInvest/commit/f240e78657a59ce9657b3425db42993d8b7a5bc9))
* **discipline:** 纪律台账——委员会可证价值(不作为+拦冲动)落邮件/CLI/API ([80f9a08](https://github.com/longsizhuo/openInvest/commit/80f9a08a9e45d38d08acf88cde4dcb720e0e2438))
* **discipline:** 纪律台账——委员会可证价值(不作为+拦冲动)落邮件/CLI/API ([1bd8c0f](https://github.com/longsizhuo/openInvest/commit/1bd8c0f26caf0a449bc3209e0aad3e437abdddae))
* **holdings:** 自由文本/CSV 持仓导入 (POST /api/holdings/import + CLI import) ([a7ce60c](https://github.com/longsizhuo/openInvest/commit/a7ce60cd8481d031fbdecf199bd72cbf47a9ac37))
* **holdings:** 自由文本/CSV 持仓导入(POST /api/holdings/import + CLI import) ([b3edddd](https://github.com/longsizhuo/openInvest/commit/b3eddddd4735d73992f16bc3167c50eda240a6d6))


### Bug Fixes

* **docs:** ADR-023 schema_source 置空(定位 ADR 不绑代码符号)——修 OKF lint CI 红 ([44bc434](https://github.com/longsizhuo/openInvest/commit/44bc434d4da7f914ff3e2a21ca168b6dd01689ab))
* **eval:** 处理 [#112](https://github.com/longsizhuo/openInvest/issues/112) 自审 review——修 DCA tilt 暖机 bug + 可复现 + 去陈旧数字 ([4386ef6](https://github.com/longsizhuo/openInvest/commit/4386ef61665ea2247f054fc8357b97b279831c8c))
* **ledger:** intervention.jsonl 同日重跑幂等 (closes [#118](https://github.com/longsizhuo/openInvest/issues/118)) ([d8d4dc3](https://github.com/longsizhuo/openInvest/commit/d8d4dc371558095d04a87bd6dbdfddacfcd91b41))
* **ledger:** intervention.jsonl 同日重跑幂等(closes [#118](https://github.com/longsizhuo/openInvest/issues/118)) ([404d686](https://github.com/longsizhuo/openInvest/commit/404d686290234f3bdd8ddf958f6e3d407f7a412e))


### Docs

* **deploy:** 补 GitHub Actions 自托管手把手详细教程 ([0dc9c35](https://github.com/longsizhuo/openInvest/commit/0dc9c352ed5ec8733841ebbd6795eb95bbe343ec))
* **positioning:** ADR-023 诚实定位(非 alpha 机器)+ README 首屏去 alpha 措辞 ([83f6af1](https://github.com/longsizhuo/openInvest/commit/83f6af100809cffe6d0f8e79f3b14a99bef48c58))
* **skill:** SKILL.md 加 import 子命令 + POST /api/holdings/import ([1c01391](https://github.com/longsizhuo/openInvest/commit/1c0139141a68ec3fb4b55c479ef7d456296c47ef))
* **skill:** SKILL.md 补 discipline 子命令 + GET /api/discipline ([3fa643d](https://github.com/longsizhuo/openInvest/commit/3fa643d2e748253abf78478df0dfc8a8e8705925))
* **wiki:** API 参考补 /api/holdings/import + /api/discipline ([29852f7](https://github.com/longsizhuo/openInvest/commit/29852f7239c031dd212c0ec72acc920567e5f7a6))
* **wiki:** API 参考补 /api/holdings/import + /api/discipline ([3ef21d2](https://github.com/longsizhuo/openInvest/commit/3ef21d20f6f3823f69e2ea59d9d40b627cc92dd6))

## [0.14.1](https://github.com/longsizhuo/openInvest/compare/v0.14.0...v0.14.1) (2026-06-28)


### Bug Fixes

* **committee:** 处理 [#110](https://github.com/longsizhuo/openInvest/issues/110) CR 建议 ([d3f2a38](https://github.com/longsizhuo/openInvest/commit/d3f2a380de018cbe611cdd60839959976d3ae8c9))
* **pnl:** 修复基准柱全部消失与正向满宽标签溢出问题 ([#92](https://github.com/longsizhuo/openInvest/issues/92)) ([70c9473](https://github.com/longsizhuo/openInvest/commit/70c9473aba6b845eda41d3ad09ea591f9c4bfc06))
* **pnl:** 基准柱全部消失 + 正向满宽条标签溢出 (openInvest[#92](https://github.com/longsizhuo/openInvest/issues/92)) ([ceab622](https://github.com/longsizhuo/openInvest/commit/ceab62259052ea9fe0ba54163ff08e6594088c98))

## [0.14.0](https://github.com/longsizhuo/openInvest/compare/v0.13.0...v0.14.0) (2026-06-27)


### Features

* **api:** committee prepare/save 端点 + run summary 附 cio_memo ([857acff](https://github.com/longsizhuo/openInvest/commit/857acff396492ed19d6a678d35754399a3adb07a))
* **api:** skill-parity 端点（doctor/status/strategy/history/what_if/buy/sell） ([988fdd2](https://github.com/longsizhuo/openInvest/commit/988fdd21d831d78d74359bd6ea9a7b86db06a799))
* **api:** 可选 bearer token 鉴权（INVEST_API_TOKEN） ([a3ad41b](https://github.com/longsizhuo/openInvest/commit/a3ad41bb6e7f43078a04e9e79418231452852d63))
* **audit:** 委员会审计 trail — meta.json + GET /api/committee/{id}/audit ([f31d19a](https://github.com/longsizhuo/openInvest/commit/f31d19acd1ab0976c0176b312ab01baa8b3a3058))
* backlog 清理 — Hero 重写 + O_EXCL 锁 + doctor 实测 + 32 unit test + CI ([ffe5f3f](https://github.com/longsizhuo/openInvest/commit/ffe5f3f9e4b51e18502acf2ea20e96b1e805cd05))
* **backtest+rl:** 修穿越漏洞 + workspace 隔离 + paper trading + Optuna RL 训练 ([3f21691](https://github.com/longsizhuo/openInvest/commit/3f21691c5cc28145ca738589992b65f5163c5dbc))
* **backup:** hub 权威状态 snapshot/restore + 修复账本备份缺口 ([#51](https://github.com/longsizhuo/openInvest/issues/51)) ([137ffde](https://github.com/longsizhuo/openInvest/commit/137ffded79295081a2c0fb4f4d0e05f40af4cc58))
* **brand:** 加 logo.svg，README 用 &lt;img&gt; 引用（GitHub 不渲染 inline SVG） ([5ac4806](https://github.com/longsizhuo/openInvest/commit/5ac480690856a99291b280d2170a2f93a466dbaf))
* **cli:** correlate 跨资产关联分析（"btw"附带查询，不落记忆） ([6fa72cf](https://github.com/longsizhuo/openInvest/commit/6fa72cfab5afa6510d2b57dd356bb3a29e9873b5))
* **cli:** INVEST_API_BASE 远端模式（hub-and-spoke 客户端） ([855f4fb](https://github.com/longsizhuo/openInvest/commit/855f4fbe412b14097057f9356e3274dc720d3e2f))
* **cli:** 加 deposit/withdraw/buy/sell/delete_holding 5 个写操作子命令 ([b6aaa8b](https://github.com/longsizhuo/openInvest/commit/b6aaa8bd7bb795c5755ee8581f333928817692c2))
* **committee:** add run_committee_session orchestrator + 4 契约测试 ([edefe95](https://github.com/longsizhuo/openInvest/commit/edefe95b77061aedec4efb6d157e51e01b1d2958))
* **committee:** event_brief 注入 Macro + RAG feature flag ([6f2314f](https://github.com/longsizhuo/openInvest/commit/6f2314f984b5ffb3970ba98cd6f52fb7fcd75ca5))
* **committee:** path-profile 按持仓币种自适应 —— 汇率卷积 (ADR-021) ([#95](https://github.com/longsizhuo/openInvest/issues/95)) ([2e33de7](https://github.com/longsizhuo/openInvest/commit/2e33de7f7d9571c22601d51cee502d9dd1bd5611))
* **committee:** replace Bull/Bear/Judge with Investment Committee (Quant/Macro/Risk/CIO) ([851fa71](https://github.com/longsizhuo/openInvest/commit/851fa71db7a3660faeaaeb9b2ca65af187c301c6))
* **committee:** SOLVENCY=strong 时集中度不触发 TRIM（确定性后处理） ([82a2ec1](https://github.com/longsizhuo/openInvest/commit/82a2ec153d9888251e742699f325fec2754ee8f9))
* **committee:** TRIM 路径化 — 卖出后路径 + 买回点，给不出更低买回点则降级 HOLD ([dcbaa74](https://github.com/longsizhuo/openInvest/commit/dcbaa7419d96df49f9200d3c1db44ab7c8b3cfa8))
* **committee:** uptrend 杠杆做成显式 risk_profile 风险档（默认 steady） ([3a4db47](https://github.com/longsizhuo/openInvest/commit/3a4db47cddfea1054f47a9df9b4b016c1f25f7bb))
* **committee:** 反事实记账——确定性拦截落 interventions.jsonl + 钱口径复盘 job ([#36](https://github.com/longsizhuo/openInvest/issues/36)) ([ee96225](https://github.com/longsizhuo/openInvest/commit/ee962254f23971be2c30cf923c973b42dc414167))
* **committee:** 干预账本历史回填 + 未结算浮动预览 ([#37](https://github.com/longsizhuo/openInvest/issues/37)) ([05fe18a](https://github.com/longsizhuo/openInvest/commit/05fe18acb2b8f223e11e93267e82b98a31d40d8c))
* **committee:** 指标修正 + regime 双触发器/recovery + dreaming lift-based caution + backtest 防穿越修复 ([88700fa](https://github.com/longsizhuo/openInvest/commit/88700fadf92e6fc641ea1179cfc93a04c509b861))
* **committee:** 独立快崩防御——VIX/ATR 任一触发，确定性降级买侧 verdict ([b12c691](https://github.com/longsizhuo/openInvest/commit/b12c691479c34844aeb56084e81c34b5fd01f3b4))
* **committee:** 补基本面/情绪维度对齐 TradingAgents（确定性事实块） ([22f0be9](https://github.com/longsizhuo/openInvest/commit/22f0be93e74f12b8d4e7fc2ed088701ec7e472ce))
* **committee:** 通用历史回填 backfill_history + 黄金长历史(去偏 path-profile) ([#94](https://github.com/longsizhuo/openInvest/issues/94)) ([ddf0196](https://github.com/longsizhuo/openInvest/commit/ddf01962e919a79df7555f933b4028873055ec93))
* **committee:** 防御 ATR 腿改通用口径——波动突变比，删 per-asset 绝对线 ([b16c952](https://github.com/longsizhuo/openInvest/commit/b16c9520fa394b4ef06e0e0edaa0498551605d51))
* **committee:** 黄金高VIX/ATR防御 全拦→强制分批DCA（用户裁决 wiki18 §5） ([#46](https://github.com/longsizhuo/openInvest/issues/46)) ([6f1cc19](https://github.com/longsizhuo/openInvest/commit/6f1cc190f46077729e6b98f7e8dee66aed7e5930))
* **config:** 50+ 参数 config 化，sweep runner + ADR-010 ([6a65680](https://github.com/longsizhuo/openInvest/commit/6a6568044788d1582b63d0a491bfef75b8404a46))
* **config:** 运行时 config-via-API + 集中度 lens 开关（ADR-017） ([#71](https://github.com/longsizhuo/openInvest/issues/71)) ([981a0e2](https://github.com/longsizhuo/openInvest/commit/981a0e2cb8cf6cf8b7849c4401c8d2a310295bd6))
* **connectors:** NapCat private-chat command interface ([e1589ed](https://github.com/longsizhuo/openInvest/commit/e1589ed7f81d5e81af5b9ebd53f4f1829c18d8d1))
* **debate:** Bull vs Bear vs Judge multi-agent debate (P6) ([79e6b3e](https://github.com/longsizhuo/openInvest/commit/79e6b3e1b7d008895ccd09ecc9e02493c01babdd))
* **dreaming:** OpenClaw-style 3-stage memory consolidation ([e867b60](https://github.com/longsizhuo/openInvest/commit/e867b60b0baa3ffd1348cba7ad46933a3d330fc9))
* **dspy:** DSPy scaffold + smoke test 实测 +10pp 改善 ([49a37e7](https://github.com/longsizhuo/openInvest/commit/49a37e76978b046b459171f12267747c39e0ddc1))
* **event-normalizer:** flash 批量归一化 + digest 邮件 ([cb714b2](https://github.com/longsizhuo/openInvest/commit/cb714b2da5346a4f6799ff452e5e0b7c870f570c))
* **event-store:** sqlite WAL + sqlite-vec 事件存储 + 可插拔 embedding ([8c160db](https://github.com/longsizhuo/openInvest/commit/8c160db2c26edd98dcde5146d307d5ada341b9ce))
* **event-watch:** 盘中事件 cron + 触发委员会 ([72baed8](https://github.com/longsizhuo/openInvest/commit/72baed847757ad4f5894ad57cc31a7fedd43ad7d))
* **events:** GET /api/events/recent + POST /api/events/check + SKILL.md 同步 ([23324ae](https://github.com/longsizhuo/openInvest/commit/23324ae9a19b600695ae6b1796275ae79267a23e))
* **events:** 指数→代理标的确定性映射层（closes [#26](https://github.com/longsizhuo/openInvest/issues/26)） ([5fba6a0](https://github.com/longsizhuo/openInvest/commit/5fba6a0cf44b8e1e9736e941db57de4ab76fb7da))
* **events:** 黄金事件覆盖——entity→GC=F 确定性兜底 + 持金常驻 gold queries ([54e5fa6](https://github.com/longsizhuo/openInvest/commit/54e5fa62b03a098a60c3e302ab47f3d26d4f34f4))
* experiment CLAUDE.md ([4730c59](https://github.com/longsizhuo/openInvest/commit/4730c5921acdadecc3976fc0a897f9107377ca6d))
* **experiments:** DSPy v2/v3 训练框架 + sandbox A/B + audits（trainset 不进 git） ([7609925](https://github.com/longsizhuo/openInvest/commit/7609925333a07f756bcdaa3166cdea8bf58a4652))
* **finance:** trades.db ↔ portfolio.md 同步 + intended_date schema（金融视角红线） ([794b43b](https://github.com/longsizhuo/openInvest/commit/794b43bb810a457ed840688e21232a23e1978bed))
* **fx:** utils.fx.total_portfolio_value_cny + portfolio_manager 通用化任意币种 ([8595230](https://github.com/longsizhuo/openInvest/commit/85952300b41c0cce27efe55f696649690b7e9b90))
* **fx:** 多币种 → CNY 汇率折算工具 + Web 总市值端点 ([cef9b31](https://github.com/longsizhuo/openInvest/commit/cef9b318e3122c6753e639bc0b370e35b81b01dc))
* **gold:** get_gold_snapshot 加 DB 兜底 + 7 个测试 (audit algo M7) ([0a1eefb](https://github.com/longsizhuo/openInvest/commit/0a1eefb39f3ada10950bb65c55963f543f460b44))
* **growth:** outperform feed 自动滚 README + 金融视角 survivorship 修复 ([b033779](https://github.com/longsizhuo/openInvest/commit/b0337797451823eccc3af58595430f74627e1768))
* **growth:** P2 增长杠杆 — outperform 事件 + fresh insights + reengagement nudge ([0e563db](https://github.com/longsizhuo/openInvest/commit/0e563db9a17d7e4ce159ee15415cf032dd98d28d))
* **gui:** 后端自带 GUI mount + scripts/sync_gui_dist 一键拉前端 dist (B 方案) ([45f8efa](https://github.com/longsizhuo/openInvest/commit/45f8efa98b0b8c7e4c68ef3abbfe1713898530e2))
* integration of market database, betashares scraper, and stooq fallback with gemini internet search capabilities ([d688c3b](https://github.com/longsizhuo/openInvest/commit/d688c3b10245b1a804096d1e95f3edaf20814cc2))
* **jobs:** multi-asset daily_report (NDQ + Gold via 浙商积存金) ([bf68476](https://github.com/longsizhuo/openInvest/commit/bf684761624b03cc23e4c9dbea46fae0f5ed1341))
* **llm:** 通用 LLM provider helper (DeepSeek/MiMo/千问/智谱) + sdk_agent reasoning carry response-driven ([a5066e7](https://github.com/longsizhuo/openInvest/commit/a5066e7f88b027cdbc2f611fcb1cdacf0fa7523c))
* **logging:** ADR-014 生产代码 print→log 迁移 + RotatingFileHandler ([#21](https://github.com/longsizhuo/openInvest/issues/21)) ([63b8a8a](https://github.com/longsizhuo/openInvest/commit/63b8a8a8bc2f64df4b7da2e50c4326e31e341791))
* macro分析 ([5ecbbd5](https://github.com/longsizhuo/openInvest/commit/5ecbbd5647dec86794e8970fa497ebe7a3f1cbb1))
* **memory:** OpenClaw-style markdown memory store ([e31afbd](https://github.com/longsizhuo/openInvest/commit/e31afbd0c33e57c6b3349d6a258670862870e808))
* **napcat:** 11 命令切 v2 数据模型 + 18 fixture 测试 ([4cc1db3](https://github.com/longsizhuo/openInvest/commit/4cc1db359fe19c497c7fbd6b2c0f6d94b83fe8f0))
* **news-sources:** 多源 fetcher (DDGS / RSS / yfinance) 统一入口 ([03be0d5](https://github.com/longsizhuo/openInvest/commit/03be0d5cabc9b41d84bb7fbf21d4c3e39500d3a9))
* **onboarding:** one-shot AI-driven setup via Claude Code Skill ([ac16907](https://github.com/longsizhuo/openInvest/commit/ac16907f5909b5fe26df9d724ed2040c045217c0))
* open-pot 月度补充模型（wealth_context.monthly_contribution_cny） ([#81](https://github.com/longsizhuo/openInvest/issues/81)) ([57898f3](https://github.com/longsizhuo/openInvest/commit/57898f37ad41e8f6b600fb246ec217536eae7d32))
* **openinvest:** 去"我的专属工具"感 + skill 双路径开放给非 Claude agent ([8848a65](https://github.com/longsizhuo/openInvest/commit/8848a65ffaeaf91ea9b8b1e3b8627d48bd0c99f1))
* **plugin:** add Codex plugin manifest (.codex-plugin) + Codex repo marketplace (.agents/plugins); README codex install ([9f4d4d2](https://github.com/longsizhuo/openInvest/commit/9f4d4d2f2863c5ce00a5bd846a4fc080fd64d049))
* **pnl:** backfill 60 天历史 PnL 数据让 SVG 一开图就有完整曲线 ([0e4fff3](https://github.com/longsizhuo/openInvest/commit/0e4fff38c316e5319abd31abab2e4ef0e0fac580))
* **pnl:** optional auto-push svg via INVEST_PNL_AUTOPUSH + GITHUB_TOKEN ([d337ce5](https://github.com/longsizhuo/openInvest/commit/d337ce572a89724da6e3d07bab13b427f9f15869))
* **pnl:** vs 11 个基准对比 — Phase 1 + 2 + 3 全部上线 ([0ef32d4](https://github.com/longsizhuo/openInvest/commit/0ef32d479103386686c7a59f739f81b253a6824c))
* **pnl:** 切到 orphan pnl-data 分支模式 → 主分支历史干净 ([7e926f2](https://github.com/longsizhuo/openInvest/commit/7e926f27490546a2d0eb4911c22f6940bd7558b4))
* **pnl:** 时区 bug 修复 + clean/freshness 工具脚本 ([0988f1f](https://github.com/longsizhuo/openInvest/commit/0988f1f4fdc701064aa7be6f0a740f648cac2232))
* **pnl:** 柱状图重设计 + 删自相关基准 + 频率改 2h 工作日 ([96b63c9](https://github.com/longsizhuo/openInvest/commit/96b63c922726b8110035d1a866a446fa7159358c))
* **pnl:** 隐私优先的实时 PnL 折线图嵌入 README ([cb0eefb](https://github.com/longsizhuo/openInvest/commit/cb0eefbd3474d5461d15cdcc071fe7b035f7a84d))
* **probability:** regime 概率表 — 按 (asset, regime) 给历史 forward return 分布 ([5f209de](https://github.com/longsizhuo/openInvest/commit/5f209de2c7f122db3dab51361df4d7ef1cbcc7ef))
* **probability:** 概率表路径化——30/60/90 多窗分布 + 路径形状 ([1db0f8d](https://github.com/longsizhuo/openInvest/commit/1db0f8de15ec4d34b7680c10b0a981c511caeb8d))
* **probability:** 路径形状三类→四类（加 max 轴）+ regime 持续中位标注 + 算法落 wiki ([46799a5](https://github.com/longsizhuo/openInvest/commit/46799a5871d2b60c49be8fe35e27e6de41c5abb7))
* **probability:** 路径校准层 + walk-forward 闭环 + TA 实验结论（ADR-009） ([#31](https://github.com/longsizhuo/openInvest/issues/31)) ([c705eb1](https://github.com/longsizhuo/openInvest/commit/c705eb17d9b174d73da569a20342233e370a2113))
* **prompt:** CIO 加现金机会成本规则，HOLD 100% → 44%，reward 0 → 0.3978 ([2b69c83](https://github.com/longsizhuo/openInvest/commit/2b69c83dbde4eea2b4a710e0ee13085009b50b63))
* **prompts:** force Chinese output and hard-cap token verbosity ([4e2cfc1](https://github.com/longsizhuo/openInvest/commit/4e2cfc1536add0f625ad270e8ba86f2a1efb921e))
* **regime+dreaming:** per-asset 阈值 + Deep Sleep LLM 验伪 ([4eb78dd](https://github.com/longsizhuo/openInvest/commit/4eb78dd744b3e1ec42a8d44c54153916af1aed33))
* **regime:** OHLC 直算概率表源 + sweep 全量历史 → main ([400944a](https://github.com/longsizhuo/openInvest/commit/400944a84660c62dd20c56558d9720f0445edf98))
* **regime:** 拆 regime 方向锁层，STRATEGY_HINT 改中性 OHLC 概率口径 ([8de64e5](https://github.com/longsizhuo/openInvest/commit/8de64e5ed0780a2feff9677c7a8aade1e1e12e4f))
* **regime:** 概率表/买回点数据源换成几十年 OHLC 直算（替代 verdict_review 276 条） ([1e5dffc](https://github.com/longsizhuo/openInvest/commit/1e5dffcdea9da6085ae4e18e2ab0547350885a11))
* **regime:** 概率表/买回点数据源换成几十年 OHLC 直算（替代 verdict_review 276 条） ([40d5588](https://github.com/longsizhuo/openInvest/commit/40d558853dcf3f6da5f9d8f31ebcb8f275edb58b))
* **regime:** 防御 ATR 线与 crash 分类解耦——新增 defense_atr_pct_min ([a521d04](https://github.com/longsizhuo/openInvest/commit/a521d040b35f50b54685225a3ec7464efde1e3d2))
* **regime:** 防御 ATR 线按确定性 sweep 调优 NDQ/GC → 2.0 ([e3ffad3](https://github.com/longsizhuo/openInvest/commit/e3ffad3d14ab67bd541aff7440611c2a1de995cc))
* **report:** 路径概率渲染进日报邮件——与 CIO 看到的同一份分布 ([#32](https://github.com/longsizhuo/openInvest/issues/32)) ([abc50aa](https://github.com/longsizhuo/openInvest/commit/abc50aa036863d8de4caa0755a59fa07743abb7c))
* **rl:** 加 trainset 生成器 + hold-out 验证 script，为 DSPy / 训练报告做准备 ([91dc0fd](https://github.com/longsizhuo/openInvest/commit/91dc0fd34903704a1261bf54011e33e944f87a73))
* **scheduler:** APScheduler runner replacing while-true loop ([723868f](https://github.com/longsizhuo/openInvest/commit/723868fb15a3e00882f8303d759181ba576b7d88))
* **scheduler:** 补 verdict_review.yml 让 Phase 3 自学习闭环可被发现（enabled=false） ([#58](https://github.com/longsizhuo/openInvest/issues/58)) ([50763db](https://github.com/longsizhuo/openInvest/commit/50763db1a3a693715e7d3d8fd23393d4aab8e0f6))
* **scripts:** event_check CLI + 根因标注助手 ([6899c51](https://github.com/longsizhuo/openInvest/commit/6899c514fe40218d34d149d43d7ba480a5f56441))
* **sentiment:** EVENT_STANCE 机制升级——per-asset 行 + 加权公式（默认禁用） ([e83685f](https://github.com/longsizhuo/openInvest/commit/e83685ff574279f9acd93cb9447f71ecf0cd3fb3))
* **skill:** expose invest data as Claude Code Skill (P5) ([0ce05e0](https://github.com/longsizhuo/openInvest/commit/0ce05e065911b6a614d2349ebd89cba9cbea39ea))
* **skill:** okf-frontmatter — OKF 文档维护 skill + frontmatter 迁移 ([#72](https://github.com/longsizhuo/openInvest/issues/72)) ([091a4b2](https://github.com/longsizhuo/openInvest/commit/091a4b2086150be642b0667fbc07e03bc5885c61))
* **skill:** run.sh 远端模式适配 ([86eeac2](https://github.com/longsizhuo/openInvest/commit/86eeac297b038feff26c10ce5d52ea5bb14518c3))
* **skill:** version-control SKILL.md + run.sh in repo via skill/install.sh ([4c05c61](https://github.com/longsizhuo/openInvest/commit/4c05c61983f6a9edf8a5f71bb163589c689d9e32))
* **skill:** 按 OpenClaw 模式拆 invest + invest-setup 两个 skill ([e83d3ea](https://github.com/longsizhuo/openInvest/commit/e83d3ea9d0fd36f59f9984a05a97168dd839a449))
* **sprint1-backend:** 一键记账 + state_bus 单例 + 脱敏聚合 + reengagement ([b6f127b](https://github.com/longsizhuo/openInvest/commit/b6f127b2a1d52c6d9d94ac09bc87d6270fbe7779))
* sqlite market data persistence with automated scraping and robust fallback ([18d14fa](https://github.com/longsizhuo/openInvest/commit/18d14fad4032606ce233dd82737f923da2d680af))
* Thread 避免竞争 ([310f067](https://github.com/longsizhuo/openInvest/commit/310f067b4fae79a8322fc7d9b6a35c8a540a17fa))
* uv.lock ([0aaccff](https://github.com/longsizhuo/openInvest/commit/0aaccff351ba4948a7a8a2bca73c7a3f4c90be01))
* uv.lock ([44b39b2](https://github.com/longsizhuo/openInvest/commit/44b39b203a2b6b74c9b796ace2e5c9119974f26b))
* v2 通用化数据模型 + v3 透明化 + live 多轮真辩论 + 18 README ([3353d65](https://github.com/longsizhuo/openInvest/commit/3353d650b927338a42d1b2b7345d1d4e361b8ce9))
* Web API + invest-gui 完整接入 + v2 多资产通用 + v3 透明化 ([a9e4c5e](https://github.com/longsizhuo/openInvest/commit/a9e4c5e40d5c68e5d098eb0bcadc3d49d6b94476))
* **web-api:** 新增 FastAPI 只读 REST 层（invest-gui 前端入口） ([6d9313b](https://github.com/longsizhuo/openInvest/commit/6d9313b3920fe7834e288becb62ce5765733afc0))
* 优化prompt ([09b6e9b](https://github.com/longsizhuo/openInvest/commit/09b6e9b50d1b90bb1d1007579069fa2a61fae493))
* 优化prompt ([43558f6](https://github.com/longsizhuo/openInvest/commit/43558f6199f7b06cfcec7120ac2b3f1bb160aaa0))
* 优化prompt ([e9d1f75](https://github.com/longsizhuo/openInvest/commit/e9d1f752781ddb1c88c1f584969a17f84f7ad827))
* 初始化开放脚本 ([138cf76](https://github.com/longsizhuo/openInvest/commit/138cf760898ba24680d55b33360999c8a965f4bd))
* 年华利润 ([14c177f](https://github.com/longsizhuo/openInvest/commit/14c177f527cc11183d6b457d639b105c669cfe94))
* 年华利润 ([86b058a](https://github.com/longsizhuo/openInvest/commit/86b058a93369f6187cf22335e7b440ad69911161))
* 抽出NDQ.AX参数 ([f28fce2](https://github.com/longsizhuo/openInvest/commit/f28fce20d8a7ab9d8919cb90fc8df0fdfdc074d1))
* 新闻 ([c4a9ebf](https://github.com/longsizhuo/openInvest/commit/c4a9ebf9c5bd486dbd6d78838dfe47619b108c76))
* 新闻 ([2d2b83b](https://github.com/longsizhuo/openInvest/commit/2d2b83b116d5b7efce566d0791806957a8df3fd5))
* 更新一下工具链，init ([f8217e2](https://github.com/longsizhuo/openInvest/commit/f8217e274a81c3b79a195486e381cd33b0a861bb))
* 添加邮箱获取购买的NDX股票数量 ([e9ebb10](https://github.com/longsizhuo/openInvest/commit/e9ebb1084d622747e7fb3fdc8282ed8c2bb64f69))
* 自动定投 + 子弹池现金语义（DCA / dip-reserve）+ 修数据深度 bug ([#78](https://github.com/longsizhuo/openInvest/issues/78)) ([e07076e](https://github.com/longsizhuo/openInvest/commit/e07076ea455dede38b81bb72385cd197fbd860e7))
* 重构文件结构 ([f119c4a](https://github.com/longsizhuo/openInvest/commit/f119c4a16228f7b90bb0ef9bc22770946b817c03))
* 重试逻辑 ([68b3bf0](https://github.com/longsizhuo/openInvest/commit/68b3bf04e847d96f1a815139eef87bf8565f0e32))


### Bug Fixes

* address Copilot CR (PR [#3](https://github.com/longsizhuo/openInvest/issues/3)) — 5 valid issues from 4e2cfc1 ([52f45c4](https://github.com/longsizhuo/openInvest/commit/52f45c49bb522ca2b2c0be61546144864528ecc9))
* **backtest+llm:** 切 deepseek-v4-flash + 10y 预热修空数据 + 数值稳定 ([867db9b](https://github.com/longsizhuo/openInvest/commit/867db9bd17dabe7fc610614db34b14cf872ce2ea))
* **backtest:** warmup 写全 OHLC（修被预热资产路径表样本不足） ([#86](https://github.com/longsizhuo/openInvest/issues/86)) ([1c8091c](https://github.com/longsizhuo/openInvest/commit/1c8091c6f263de5c3fa6c2f95a2ed28417fed939))
* **cio:** TRIM 约束字段名对齐 + 明确覆盖通用 TRIM 规则 ([305b162](https://github.com/longsizhuo/openInvest/commit/305b162e46548a85da902fc0ee420c7f8968e5b8))
* **cio:** TRIM 阈值改走 config 注入，消除魔法数字 ([a8578c3](https://github.com/longsizhuo/openInvest/commit/a8578c31e5fc23f9597e297ffe3bd7b386797835))
* **cio:** 零花钱账户小幅浮亏禁止 TRIM ([560fb6a](https://github.com/longsizhuo/openInvest/commit/560fb6a680df593221e601a16ba70175b892f74b))
* **ci:** smoke import 修正 state_bus 导出名（_write→write） ([b066bce](https://github.com/longsizhuo/openInvest/commit/b066bcec285871c9701bf0d2be25abe902ed30fb))
* **committee:** _persist 落盘 wealth_context_view 段，GUI 才能 parse 展示 ([9ffbe09](https://github.com/longsizhuo/openInvest/commit/9ffbe09d6459d1dea01a2e49e63e05ad62667b04))
* **committee:** backup_cny 读对 key + 抽 load_backup_cny 单一可信源 + force-HOLD 归零 alloc ([70e9ea6](https://github.com/longsizhuo/openInvest/commit/70e9ea6c9e54d235440a04ea2a5502a750eb8911))
* **committee:** coordinator 路径补确定性事实块——防御链失效修复 ([8110904](https://github.com/longsizhuo/openInvest/commit/81109048c3fb220b319c33d37a20e1c27b604590))
* **committee:** hardcoded 阈值集中化 + None sentinel + 数据陈旧硬熔断 ([7c86743](https://github.com/longsizhuo/openInvest/commit/7c86743a5caef9267394ad70bd7efdf985004d93))
* **committee:** NDQ 集中度错算 / Risk R2 越位 / CIO tool_call 漂移 / Direct 路径 portfolio_summary 统一 ([0fa05b4](https://github.com/longsizhuo/openInvest/commit/0fa05b45070cb9b1690890222afabe2abfe83069))
* **committee:** review fixes — store 未定义、solvency 拼写、Sanity4 confidence/alloc ([2888c44](https://github.com/longsizhuo/openInvest/commit/2888c440afa911c42712e6bb157f31f567a9edca))
* **committee:** show live prices + PnL%, ban tool-error complaints in agent output ([13ce175](https://github.com/longsizhuo/openInvest/commit/13ce175e24bef0270f9b23e282a51eb97bbe2d01))
* **committee:** 停止把 MiMo 调用误标成 deepseek（provider 标签从 LLM_PROVIDER 读） ([#48](https://github.com/longsizhuo/openInvest/issues/48)) ([30e1a8f](https://github.com/longsizhuo/openInvest/commit/30e1a8f4a7e46d1cf28f1ef2785b75ff760a9795))
* **committee:** 移除 solvency 集中度自动兜底，集中度只由 lens 控制 ([#84](https://github.com/longsizhuo/openInvest/issues/84)) ([0726b2c](https://github.com/longsizhuo/openInvest/commit/0726b2cba6dfaf4918b1b1dcaeb594bf2c5cee54))
* **committee:** 集中度 lens 默认 OFF —— concentration 改 opt-in (ADR-020) ([#93](https://github.com/longsizhuo/openInvest/issues/93)) ([cfe21de](https://github.com/longsizhuo/openInvest/commit/cfe21de6136f67e17e23cd436b485fc40e778ef1))
* **config:** env override 多词 section 解析 + per-asset 支持 + CR 修复 ([52392de](https://github.com/longsizhuo/openInvest/commit/52392de1196b04ce4dbd7c55d3500dc2b401e813))
* container entry + email failure raise + README sync ([c97616f](https://github.com/longsizhuo/openInvest/commit/c97616fb5083446ba9ec9f0ba943719e02a6ebfb))
* **core:** atomic writes + LLM retry — 2 production hardening from full audit ([aa7e1a4](https://github.com/longsizhuo/openInvest/commit/aa7e1a4bf34bb59d05c8bfda341959b9331f1e46))
* **core:** kill TOCTOU / Lost Update in concurrent portfolio writes ([5cdd9e3](https://github.com/longsizhuo/openInvest/commit/5cdd9e3249f568144df76ed950b5a212d1ab972a))
* **data:** kill price-fetch silent corruption (A+B+C+D from audit) ([3113000](https://github.com/longsizhuo/openInvest/commit/3113000cbc3b4f69f13555389b6b94e912f465e1))
* **dreaming:** LLM REJECT 从 candidates.json 移除 + prompt 加 uptrend 怀疑清单 ([958430d](https://github.com/longsizhuo/openInvest/commit/958430d4a95fae3959bc904f5be175e8c7f6a06e))
* **dreaming:** LLM 验伪构造 payload 用 c["action"] 崩溃 ([ad9d964](https://github.com/longsizhuo/openInvest/commit/ad9d964ac8daccbcecd4f76bc8124c6f75c8f23c))
* **email-render:** assemble_full_report 渲染 wealth_view section (第 6 层防漂移) ([ad04192](https://github.com/longsizhuo/openInvest/commit/ad041924f9d280939a09acf268ca543d9eab79f2))
* **email:** 修复分析师原文以代码块泄露 + 重设计邮件版式 ([#75](https://github.com/longsizhuo/openInvest/issues/75)) ([99bb2fb](https://github.com/longsizhuo/openInvest/commit/99bb2fb7c896307f49d2a36a42dcd66d7a4291d2))
* **event-rag:** daily_report cron 路径 event_brief 漂移修复 (4 处) ([137b429](https://github.com/longsizhuo/openInvest/commit/137b429b2833e26d5227f3e98f2f89417591af3a))
* **event-watch:** _run_committee_task 跑完补发 verdict 邮件 ([02362c1](https://github.com/longsizhuo/openInvest/commit/02362c1acdc618ceb7400a58378357595a5d86e1))
* **events+contract:** wealth_context 漂移修复 + PR [#5](https://github.com/longsizhuo/openInvest/issues/5) Copilot CR 全部处理 ([5549cab](https://github.com/longsizhuo/openInvest/commit/5549caba978e0c36699445395dda80de4087d1cf))
* full multi-agent CR pass — 5 critical + 8 major audit findings ([3c11d6d](https://github.com/longsizhuo/openInvest/commit/3c11d6d38ef4990df1729e507407828dfc8b775a))
* **fx:** NaN 价不再污染总资产/集中度，单坏腿不静默关闭风控 ([#89](https://github.com/longsizhuo/openInvest/issues/89)) ([ce49782](https://github.com/longsizhuo/openInvest/commit/ce497825ec07cdf1b0664c67406cc37ff075eef2))
* ignore ([12d1c32](https://github.com/longsizhuo/openInvest/commit/12d1c32a2f3b170b4794f8485cf5161fb747c406))
* **infra:** SQLite WAL 并发 + 删 process-global socket timeout ([70e610f](https://github.com/longsizhuo/openInvest/commit/70e610f8588d5421220213ca5e8965528d65d977))
* **lint:** skill.py 通过 committee_runner re-export 拿 load_wealth_context_view ([763e62c](https://github.com/longsizhuo/openInvest/commit/763e62c6690eb5a36958277b7f54a8bde41bbe1e))
* **metrics:** 生产 VIX/price 分位口径 730→504，强制生产与回测同源 ([#45](https://github.com/longsizhuo/openInvest/issues/45)) ([0a656b7](https://github.com/longsizhuo/openInvest/commit/0a656b7847bfe4d9eaf5692f9114992a5c389eea))
* **openinvest:** 6-agent 团队扫到的痛点 P0+P1 后端批次修 ([676af27](https://github.com/longsizhuo/openInvest/commit/676af27992a87810c039618af40c5f15a8bed366))
* **openinvest:** PM-1 v2 + Tester v2 闭环验证后的剩余痛点 ([fd7da6d](https://github.com/longsizhuo/openInvest/commit/fd7da6de54d7e78f1944978ca7bfe4205919970b))
* **payday:** atomic month-claim to prevent concurrent double-credit ([365e79d](https://github.com/longsizhuo/openInvest/commit/365e79d5c36a7f0a3bd6703c329a9f010e80b48e))
* **pnl:** redact token in generic except branch + harden fx/export public-data guards ([#57](https://github.com/longsizhuo/openInvest/issues/57)) ([c5dc4d1](https://github.com/longsizhuo/openInvest/commit/c5dc4d122c5b1fe189690b7e47cc7bf68f097c7f))
* **pnl:** 满宽负条 % 标签翻入条内,不再压住左侧基准名 (openInvest[#92](https://github.com/longsizhuo/openInvest/issues/92)) ([#96](https://github.com/longsizhuo/openInvest/issues/96)) ([a628549](https://github.com/longsizhuo/openInvest/commit/a62854989197eb69717ce5319573e8e9523a3926))
* **portfolio:** make CommSec record_external_trade idempotent ([#62](https://github.com/longsizhuo/openInvest/issues/62)) ([a1dd2d6](https://github.com/longsizhuo/openInvest/commit/a1dd2d6b173f6f200ae2578eed4e5ad5f64f9635))
* **post-e2e:** Fresh Claude 端到端测试找的 3 处问题 + 加 CLAUDE.md ([bbef961](https://github.com/longsizhuo/openInvest/commit/bbef961f4aee5c50b32f4f5f00a99e1c7e7cb945))
* **probability:** forward-return 单一可信源/日历天口径 + 干预 rule 并桶（漂移审计） ([#41](https://github.com/longsizhuo/openInvest/issues/41)) ([bb8a280](https://github.com/longsizhuo/openInvest/commit/bb8a280bbbd274f11f87ab36b1825ecc382b1051))
* **regime:** 重叠窗口用 effective_n 判 low_confidence + forward-return correctness 测试 ([7e064c3](https://github.com/longsizhuo/openInvest/commit/7e064c3272b96da7252955bc7a35c191a7745daf))
* resolve agent initialization errors and ensure robust fallback to web search ([39ee489](https://github.com/longsizhuo/openInvest/commit/39ee4892c3d890e6b729fa7965c90d06a366c4fc))
* **rl:** 修 Optuna 参数空间两个 placebo bug ([6fb038c](https://github.com/longsizhuo/openInvest/commit/6fb038c8689127e6da9070b6d1ed77522c70d699))
* **rl:** 补全 max_rounds / cio_confidence_cap 的消费代码 + walk-forward 周末 start bug ([a06730b](https://github.com/longsizhuo/openInvest/commit/a06730b8a7b659442b6c9e9459b477ffa06b5dfb))
* **safety:** cmd_init 拒绝覆盖已有真实持仓（事故防御） ([5ddb487](https://github.com/longsizhuo/openInvest/commit/5ddb4876b905a5ea8629a383e173c9377424a733))
* **self-host:** 默认值加固 — INVEST_HOME 统一 ~/openInvest + PnL 署名按 remote + 去硬编码路径 ([#74](https://github.com/longsizhuo/openInvest/issues/74)) ([f31899f](https://github.com/longsizhuo/openInvest/commit/f31899fe471efaa63ddc934a2fe8a54cbe103ab2))
* **skill+web-api:** scripts.skill 全面通用化（货币/LLM provider/集中度）+ GUI cache header 测试 ([a18d1e4](https://github.com/longsizhuo/openInvest/commit/a18d1e478402ebee297b54e8091f904421fa75a9))
* **skill:** DeepSeek 静默降级强制话术 + 部署细节 agnostic 化 ([a30b2f0](https://github.com/longsizhuo/openInvest/commit/a30b2f04f3a539fe72800fe23c0c5374b0015828))
* **skill:** GUI 不再主动推销 — fork 用户被反复告知"还有 Web GUI"是噪音 ([6daebd4](https://github.com/longsizhuo/openInvest/commit/6daebd40a2436bf1c6e78ecd2a96ec87e8d9c947))
* **skill:** GUI 是小白主入口 — 必须主动告知 + dist 缺失时直接帮用户装 ([ee43932](https://github.com/longsizhuo/openInvest/commit/ee439324b5bda2b7adbed6226410f59108c3c0c0))
* **skill:** redirect stdout to stderr so JSON output isn't polluted by utils noise ([a20be20](https://github.com/longsizhuo/openInvest/commit/a20be20f7e54a62c98edc8a610577d45c7f60c12))
* **skill:** 加 wealth_context 必读规则，防 agent 用 PWM 老逻辑误判超配 ([e2ed158](https://github.com/longsizhuo/openInvest/commit/e2ed158b9cd5ed95ab4f6e1301252143653a2606))
* **sweep:** regime 阈值验证读全量历史，去掉 get_history_data 730 天 cap ([874f443](https://github.com/longsizhuo/openInvest/commit/874f44358b17a7d45f7d99117e69c25eb9e989c1))
* **tests:** 修 CI 失败 — backtest yfinance 语义 + v4-flash pricing 同步 ([4634375](https://github.com/longsizhuo/openInvest/commit/463437586b6ce916be38ec34c2ba59ef8dc67e4c))
* token-leak redaction, public n&lt;30 suppression, backtest FX lookahead ([#53](https://github.com/longsizhuo/openInvest/issues/53)) ([1b69ada](https://github.com/longsizhuo/openInvest/commit/1b69ada17450a5d9901361228d4dd918677b55bb))
* **web-api:** make patch_trade_status idempotent on repeated executed PATCH ([fe3f5ff](https://github.com/longsizhuo/openInvest/commit/fe3f5ff0e1cb98377cd5384d3a1fef9fd11e4d6d))
* **web-api:** serve GUI in container — correct _STATIC_DIR + compose web service ([#68](https://github.com/longsizhuo/openInvest/issues/68)) ([c481a01](https://github.com/longsizhuo/openInvest/commit/c481a01decaf31fb0c34ed6e9fdc3a9fb03071bf))
* **web-api:** 修复 3 个生产风险：非原子交易、DB crash-loop、取款竞态 ([596590e](https://github.com/longsizhuo/openInvest/commit/596590ed3b8fb01d796ce79acc019d50f8171c0d))


### Refactor

* **cio:** TRIM 约束阈值默认 0（禁用），等 sweep OOS 验证后再启用 ([f7313e1](https://github.com/longsizhuo/openInvest/commit/f7313e128e00f580518e7832e13860c493124c83))
* **committee:** committee_runner.py 拆成 core/runner/ 包 + façade ([#56](https://github.com/longsizhuo/openInvest/issues/56)) ([87a16f3](https://github.com/longsizhuo/openInvest/commit/87a16f33996781b4db574b3aecdd983dd5a4bc4a))
* **committee:** core/committee.py 拆成 core/committee/ 包 + 薄壳 façade ([#59](https://github.com/longsizhuo/openInvest/issues/59)) ([90c323b](https://github.com/longsizhuo/openInvest/commit/90c323b5cc3f4db9a6e905d3da928ad48aa6c122))
* **commsec:** cron 改手动导入模式 ([2a2adaa](https://github.com/longsizhuo/openInvest/commit/2a2adaa437dfdaa1cb6ad39ffa9c37aa3a01f751))
* **config:** 情绪/估值 magic number 迁入 tunable config 统一维护 ([adbea49](https://github.com/longsizhuo/openInvest/commit/adbea4948786fc807c211bbec15079454575f5a5))
* **core:** 提取 skill 视图与委员会 prepare/save 到 service 层 ([08b90c6](https://github.com/longsizhuo/openInvest/commit/08b90c615ba5130d33fbedbb3ae12871514e123b))
* **cron:** daily_report.run 改用 run_committee_session + 删旧测试 + 收紧 lint ([7041241](https://github.com/longsizhuo/openInvest/commit/70412416b1c0cecbc6cf84c7984522d446a5e04e))
* **prompts+wealth:** SKILL.md 模式 + WealthContextOfficer 新角色 + GET/PUT /api/user ([d5b1e9f](https://github.com/longsizhuo/openInvest/commit/d5b1e9fff9293b4eb2bd6af701c66718276dc701))
* **rl:** 训练参数集中到 experiments/train_config.py 单一可信源 ([95f4cd7](https://github.com/longsizhuo/openInvest/commit/95f4cd777a27ed3ae155559193662a4f4c373543))
* **scripts:** 把 skill.py 拆成 skill_cmds 包 + 薄壳 façade ([#61](https://github.com/longsizhuo/openInvest/issues/61)) ([984da2b](https://github.com/longsizhuo/openInvest/commit/984da2b86e3fd5a94930e9cf33f25beb1bfeae0d))
* **skill:** cmd_run_committee 改用 run_committee_session ([73bc175](https://github.com/longsizhuo/openInvest/commit/73bc17541c4f31057996e649f446f5386799be23))
* **skill:** single entry that REUSES native agents/core, swaps LLM to Claude ([967c9ae](https://github.com/longsizhuo/openInvest/commit/967c9aee64e0236c1b3831e7864ab4194a1ddbfc))
* **skills:** 重组到 skills/ 父目录 + 完全重写 invest README ([1a89ad9](https://github.com/longsizhuo/openInvest/commit/1a89ad9c66763bb31a5b41c8972ffdc178600341))
* **skill:** v0.5 → v0.6 — 中文化 + progressive disclosure 重构 ([12464b5](https://github.com/longsizhuo/openInvest/commit/12464b58ac7098c8d5599982b3c25daa8f173e84))
* **sprint2-backend:** v1 fallback 退场 + dreaming SQLite + daily_report 拆分 + 冒烟脚本 ([6885265](https://github.com/longsizhuo/openInvest/commit/688526532f407bca907016586fb8ec46ee881d43))
* **web-api:** _run_committee_task 改用 run_committee_session ([d38230e](https://github.com/longsizhuo/openInvest/commit/d38230eb637e222470d728aa79f2e14977ac8824))
* **web-api:** system.py 按域拆成 6 个 router 子模块 ([#60](https://github.com/longsizhuo/openInvest/issues/60)) ([02b1d5e](https://github.com/longsizhuo/openInvest/commit/02b1d5e1a6cae318dd975335fe68f489f7936609))
* **web-api:** web_api.py 拆成 router 包 + Depends(get_pm) ([#55](https://github.com/longsizhuo/openInvest/issues/55)) ([333b345](https://github.com/longsizhuo/openInvest/commit/333b34599841db24375b0c78207e6e014b7d5ed2))


### Docs

* 30 分钟 fork 上手指南 ([5c264bd](https://github.com/longsizhuo/openInvest/commit/5c264bd10465f9770eedb312c228a816424054da))
* **adr:** ADR-011 HOLD Oracle 语义——hold_wrong 只判下跌方向 ([4201273](https://github.com/longsizhuo/openInvest/commit/4201273deb7ad9972a98e227e49a573a57a545cb))
* **adr:** record ledger-mutation idempotency invariant and audit ([d1996a3](https://github.com/longsizhuo/openInvest/commit/d1996a3537f7a247f1b6307881f64de3ef5a8dbb))
* **adr:** 新增 ADR 009 用户纪律承诺模板（理由段待本人填） ([e505c7c](https://github.com/longsizhuo/openInvest/commit/e505c7c1e21f81b330aa148d72f2615b084e85c2))
* **deploy:** document container self-host + compose pulls GHCR image ([#69](https://github.com/longsizhuo/openInvest/issues/69)) ([773f3ca](https://github.com/longsizhuo/openInvest/commit/773f3ca2e1f50c1bf79346fde6c925d8fe8daff1))
* **dspy:** 正式 DSPy run 完成，baseline 0.725 → optimized 0.825 (+10pp) ([3915d82](https://github.com/longsizhuo/openInvest/commit/3915d82cb6cdb032efa69771e199440423451e30))
* **event-layer:** ADR-006 + README + CI smoke import ([f41b95e](https://github.com/longsizhuo/openInvest/commit/f41b95ec01db5b70d0805460839ab88d658dccc6))
* fork user 提示（GUI beta / git pull / 多 LLM provider 配置 / 常见问题） ([8ac4159](https://github.com/longsizhuo/openInvest/commit/8ac41591349b2d4623493a5fbf3a007517db1900))
* **governance:** 治理章程（三原则+口径单源+否决权）——独立于代码，待用户签字 ([#43](https://github.com/longsizhuo/openInvest/issues/43)) ([929c64a](https://github.com/longsizhuo/openInvest/commit/929c64aade05107c05277aaff15bb7e79379bd5d))
* **napcat:** docstring 同步 WHITELIST_QQ 默认 0 (终审 CR M2) ([c3b494a](https://github.com/longsizhuo/openInvest/commit/c3b494a4c8710cdcc12763c066c214a8d8cdc5c1))
* README + memory_layout 同步到 v2/v3 + 修 CI smoke import ([201581a](https://github.com/longsizhuo/openInvest/commit/201581ae034f411f8a9d2d2b5dbf6b20c3f6fb76))
* README 大改 — hype 风营销文案 + Hero 区 + 卖点 grid ([6b81ecc](https://github.com/longsizhuo/openInvest/commit/6b81eccecbc3617e2195a87640faa80ec6d61d79))
* **readme:** 致谢 MiMo 赠予 token Plan(支撑 1966–2026 回测) ([fa804f5](https://github.com/longsizhuo/openInvest/commit/fa804f5fdd367b72eb2df7a12172f9331992d3cd))
* **rl:** 完整训练报告 + Optuna 30-trial + hold-out 验证数据 archive ([42437bc](https://github.com/longsizhuo/openInvest/commit/42437bca15f6f214d1808db11f9a5c9ccfa56a8f))
* **roadmap:** 加 PnL vs 基准 对比 TODO ([3ed3f3c](https://github.com/longsizhuo/openInvest/commit/3ed3f3c1d80352919afaa58adcd4f609e7c0b8ce))
* **services:** 修正 news.py 过时孤儿注释——ddgs 已经事件层接入 production ([81b8853](https://github.com/longsizhuo/openInvest/commit/81b8853eaaf79bac4f3626364e5aac3970c9051a))
* **setup-skill:** 新增'连接已有 hub'onboarding 路径 ([e8c76ae](https://github.com/longsizhuo/openInvest/commit/e8c76aef5cbe89df95b05f62086cb59df5e8140e))
* **skill:** coordinator 指引对齐 v0.6——确定性事实块粘贴义务 ([dbf8878](https://github.com/longsizhuo/openInvest/commit/dbf8878d6cc6a71e399c546abf3a3ec44df6e9a2))
* **skill:** 加防 hallucination 提示 — agent 不要脑补不存在的命令名 ([3169bf3](https://github.com/longsizhuo/openInvest/commit/3169bf371cb9a84b1afd2be6e4afa4844696adc4))
* **skill:** 远端模式（hub-and-spoke）使用说明 ([24cfb45](https://github.com/longsizhuo/openInvest/commit/24cfb45f182a43db4bec1af57a16bdd653fef7fc))
* **wiki:** combined 联合分析师补测——'直接加三个'两窗口均 FAIL 且劣于单独 ([#35](https://github.com/longsizhuo/openInvest/issues/35)) ([f248326](https://github.com/longsizhuo/openInvest/commit/f248326057121526413c5fb7bbc57b8760c63e00))
* **wiki:** hub-and-spoke 部署拓扑 + skill-parity/committee RPC 端点参考 ([1094fdb](https://github.com/longsizhuo/openInvest/commit/1094fdb9721b80522b8186fcab64f159ca8611de))
* **wiki:** Skill/Web 双路径改名 → Coordinator/Direct，对齐 skill 文案 ([52f17e6](https://github.com/longsizhuo/openInvest/commit/52f17e62578737ba345f80a6c5f00e9ff923b584))
* **wiki:** TA 实验复测矩阵——ADR-009 经 2 窗口×3 模型×ensemble 复测维持原判 ([#33](https://github.com/longsizhuo/openInvest/issues/33)) ([9af83bf](https://github.com/longsizhuo/openInvest/commit/9af83bfc1f2845cecc09b957d17f359f67f1c12a))
* **wiki:** 加 12-verification 科学证据章 + 11 末尾补 SKILL.md 模式对比 ([e6c0cd9](https://github.com/longsizhuo/openInvest/commit/e6c0cd9411d727196e15e459c172912fa301b6c2))
* **wiki:** 加章节 11 - RL 训练 / Backtest，澄清"不是 ML 训练" ([e7a64ac](https://github.com/longsizhuo/openInvest/commit/e7a64ac2c5b087ec247de105f7c14a3c0460406a))
* **wiki:** 拆方向锁后的文档对齐——硬约束→中性概率口径 ([88fc761](https://github.com/longsizhuo/openInvest/commit/88fc76186e06da539ddd4787d7538aa0b07fb600))
* 写清委员会双执行路径（Skill 真 Agent Teams vs Web 多线程） ([57a612e](https://github.com/longsizhuo/openInvest/commit/57a612e1946d74bbd5d5b1fcdf14e99449579c7d))
* 加 CONTRIBUTING.md — 完整贡献指南 ([5c9f8ee](https://github.com/longsizhuo/openInvest/commit/5c9f8ee6ab32b74bc8a56b1b802742902b0f5757))
* 加 docs/wiki/ 完整文档站 — 11 页 + 3 ADR ([a04937c](https://github.com/longsizhuo/openInvest/commit/a04937c6d07e6c3b2c20f282df6aea9dff54f362))
* 加 examples/sample_memo.md 修 README Hero 死链 (PM Critical 改动 [#2](https://github.com/longsizhuo/openInvest/issues/2)) ([dbb21ce](https://github.com/longsizhuo/openInvest/commit/dbb21cee63073940d2777aa5d65d529f186b4b6c))
* 澄清 Web vs Skill 路径的 LLM 调用数差异 ([7bc4883](https://github.com/longsizhuo/openInvest/commit/7bc4883b3e04e092de1054bc45e4a059728c7152))

## [0.13.0](https://github.com/longsizhuo/openInvest/compare/v0.12.1...v0.13.0) (2026-06-25)


### Features

* **committee:** path-profile 按持仓币种自适应 —— 汇率卷积 (ADR-021) ([#95](https://github.com/longsizhuo/openInvest/issues/95)) ([7d36e57](https://github.com/longsizhuo/openInvest/commit/7d36e57e07067e9dd7621eab73444101f6095787))
* **committee:** 通用历史回填 backfill_history + 黄金长历史(去偏 path-profile) ([#94](https://github.com/longsizhuo/openInvest/issues/94)) ([79cd0a2](https://github.com/longsizhuo/openInvest/commit/79cd0a21ccfa6ab4805ab6d8bd2ba0dd22ba12e1))
* **plugin:** add Codex plugin manifest (.codex-plugin) + Codex repo marketplace (.agents/plugins); README codex install ([c7c08c6](https://github.com/longsizhuo/openInvest/commit/c7c08c6dfde878b73526626b27b31444c32e2e39))


### Bug Fixes

* **backtest:** warmup 写全 OHLC（修被预热资产路径表样本不足） ([#86](https://github.com/longsizhuo/openInvest/issues/86)) ([29fec85](https://github.com/longsizhuo/openInvest/commit/29fec854583daf85b7f955036ed3efa78098f6d9))
* **committee:** 集中度 lens 默认 OFF —— concentration 改 opt-in (ADR-020) ([#93](https://github.com/longsizhuo/openInvest/issues/93)) ([54b1603](https://github.com/longsizhuo/openInvest/commit/54b16032c9770447bd49e9255043d8ab8cfd8e5e))
* **fx:** NaN 价不再污染总资产/集中度，单坏腿不静默关闭风控 ([#89](https://github.com/longsizhuo/openInvest/issues/89)) ([1877992](https://github.com/longsizhuo/openInvest/commit/1877992e0e8722923ef80a32b75fe1242f237716))
* **pnl:** 满宽负条 % 标签翻入条内,不再压住左侧基准名 (openInvest[#92](https://github.com/longsizhuo/openInvest/issues/92)) ([#96](https://github.com/longsizhuo/openInvest/issues/96)) ([95e543f](https://github.com/longsizhuo/openInvest/commit/95e543f4199e08c2f96958b7975cf33b76f28274))

## [0.12.1](https://github.com/longsizhuo/openInvest/compare/v0.12.0...v0.12.1) (2026-06-23)


### Bug Fixes

* **committee:** 移除 solvency 集中度自动兜底，集中度只由 lens 控制 ([#84](https://github.com/longsizhuo/openInvest/issues/84)) ([3f963c0](https://github.com/longsizhuo/openInvest/commit/3f963c0c38008f986b5fb3d82c48ad65815c1e49))

## [0.12.0](https://github.com/longsizhuo/openInvest/compare/v0.11.0...v0.12.0) (2026-06-22)


### Features

* open-pot 月度补充模型（wealth_context.monthly_contribution_cny） ([#81](https://github.com/longsizhuo/openInvest/issues/81)) ([a295f80](https://github.com/longsizhuo/openInvest/commit/a295f803c0e4519f706fb514c56ca0352ecff059))
* 自动定投 + 子弹池现金语义（DCA / dip-reserve）+ 修数据深度 bug ([#78](https://github.com/longsizhuo/openInvest/issues/78)) ([a94c912](https://github.com/longsizhuo/openInvest/commit/a94c912ae8d1326a8900ee2c290a2db4ad235e9f))

## [0.11.0](https://github.com/longsizhuo/openInvest/compare/v0.10.0...v0.11.0) (2026-06-20)


### Features

* **config:** 运行时 config-via-API + 集中度 lens 开关（ADR-017） ([#71](https://github.com/longsizhuo/openInvest/issues/71)) ([7f812b6](https://github.com/longsizhuo/openInvest/commit/7f812b62685a12fa7f918317d15830f506a8e4f0))
* **skill:** okf-frontmatter — OKF 文档维护 skill + frontmatter 迁移 ([#72](https://github.com/longsizhuo/openInvest/issues/72)) ([b4a4548](https://github.com/longsizhuo/openInvest/commit/b4a45485ee323e341704b799bec148151d2e209c))


### Bug Fixes

* **email:** 修复分析师原文以代码块泄露 + 重设计邮件版式 ([#75](https://github.com/longsizhuo/openInvest/issues/75)) ([406a805](https://github.com/longsizhuo/openInvest/commit/406a8055b3f6f4312a2537a96246e1a8e06653e0))
* **payday:** atomic month-claim to prevent concurrent double-credit ([ce1331c](https://github.com/longsizhuo/openInvest/commit/ce1331c46fd866d72466fd13986f7be631fab5ec))
* **portfolio:** make CommSec record_external_trade idempotent ([#62](https://github.com/longsizhuo/openInvest/issues/62)) ([8a00733](https://github.com/longsizhuo/openInvest/commit/8a00733f98a3356c1a046367c182ec41f0ba07a0))
* **self-host:** 默认值加固 — INVEST_HOME 统一 ~/openInvest + PnL 署名按 remote + 去硬编码路径 ([#74](https://github.com/longsizhuo/openInvest/issues/74)) ([930c0e4](https://github.com/longsizhuo/openInvest/commit/930c0e4ba1bb1b83c0c67915671e1c4170fa11cf))
* **web-api:** make patch_trade_status idempotent on repeated executed PATCH ([dbcee52](https://github.com/longsizhuo/openInvest/commit/dbcee523e0aee371bf953357efecd7ba462f0e34))
* **web-api:** serve GUI in container — correct _STATIC_DIR + compose web service ([#68](https://github.com/longsizhuo/openInvest/issues/68)) ([4c2ff71](https://github.com/longsizhuo/openInvest/commit/4c2ff71cf0337fdfbf91a5f3b692498fe6884441))


### Docs

* **adr:** record ledger-mutation idempotency invariant and audit ([3d7e7ca](https://github.com/longsizhuo/openInvest/commit/3d7e7caaab0ce341e8a7fbfda3b7b528e0afca08))
* **deploy:** document container self-host + compose pulls GHCR image ([#69](https://github.com/longsizhuo/openInvest/issues/69)) ([2ad9b88](https://github.com/longsizhuo/openInvest/commit/2ad9b8812c38d45112eb50c965d1987f5a314ab5))

## [0.10.0](https://github.com/longsizhuo/openInvest/compare/v0.9.1...v0.10.0) (2026-06-16)


### Features

* **backup:** hub 权威状态 snapshot/restore + 修复账本备份缺口 ([#51](https://github.com/longsizhuo/openInvest/issues/51)) ([1b01324](https://github.com/longsizhuo/openInvest/commit/1b0132434fca99fd0bc783ce6a9c144207ac6cbe))
* **scheduler:** 补 verdict_review.yml 让 Phase 3 自学习闭环可被发现（enabled=false） ([#58](https://github.com/longsizhuo/openInvest/issues/58)) ([c31078a](https://github.com/longsizhuo/openInvest/commit/c31078a18bb7328fa03fb06d6dfbece1ed37381e))


### Bug Fixes

* **pnl:** redact token in generic except branch + harden fx/export public-data guards ([#57](https://github.com/longsizhuo/openInvest/issues/57)) ([bf8b3e0](https://github.com/longsizhuo/openInvest/commit/bf8b3e08bdd9f9f83668d4f92a3fa9bbf1ded206))
* token-leak redaction, public n&lt;30 suppression, backtest FX lookahead ([#53](https://github.com/longsizhuo/openInvest/issues/53)) ([7ddab07](https://github.com/longsizhuo/openInvest/commit/7ddab073d729290fe53b082e00520a337308b788))


### Refactor

* **committee:** committee_runner.py 拆成 core/runner/ 包 + façade ([#56](https://github.com/longsizhuo/openInvest/issues/56)) ([b5d7083](https://github.com/longsizhuo/openInvest/commit/b5d7083758a16cb9ea5c35e4c35de6fabfc2967b))
* **committee:** core/committee.py 拆成 core/committee/ 包 + 薄壳 façade ([#59](https://github.com/longsizhuo/openInvest/issues/59)) ([124c67f](https://github.com/longsizhuo/openInvest/commit/124c67f3b06d333f98ef76dbc8e2cc131b8a21b9))
* **scripts:** 把 skill.py 拆成 skill_cmds 包 + 薄壳 façade ([#61](https://github.com/longsizhuo/openInvest/issues/61)) ([04eb155](https://github.com/longsizhuo/openInvest/commit/04eb155b133a54e609bed23411b27787e80934f2))
* **web-api:** system.py 按域拆成 6 个 router 子模块 ([#60](https://github.com/longsizhuo/openInvest/issues/60)) ([9fb31b6](https://github.com/longsizhuo/openInvest/commit/9fb31b6a48962aaae2bb6c0065f181707b975790))
* **web-api:** web_api.py 拆成 router 包 + Depends(get_pm) ([#55](https://github.com/longsizhuo/openInvest/issues/55)) ([65e9b6c](https://github.com/longsizhuo/openInvest/commit/65e9b6c953b56fcae85540a49bbb98a68c3ee054))

## [0.9.1](https://github.com/longsizhuo/openInvest/compare/v0.9.0...v0.9.1) (2026-06-14)


### Bug Fixes

* **committee:** 停止把 MiMo 调用误标成 deepseek（provider 标签从 LLM_PROVIDER 读） ([#48](https://github.com/longsizhuo/openInvest/issues/48)) ([0eb5783](https://github.com/longsizhuo/openInvest/commit/0eb5783830a9d555ad8805f4e6e2b3d991953b25))

## [0.9.0](https://github.com/longsizhuo/openInvest/compare/v0.8.0...v0.9.0) (2026-06-14)


### Features

* **committee:** 黄金高VIX/ATR防御 全拦→强制分批DCA（用户裁决 wiki18 §5） ([#46](https://github.com/longsizhuo/openInvest/issues/46)) ([6d007ff](https://github.com/longsizhuo/openInvest/commit/6d007ff96dafd3c88158cf451e61fcda61027d4e))
* experiment CLAUDE.md ([991fcce](https://github.com/longsizhuo/openInvest/commit/991fcce6fb00c8607fa68ea58a78552c761515bd))


### Bug Fixes

* **metrics:** 生产 VIX/price 分位口径 730→504，强制生产与回测同源 ([#45](https://github.com/longsizhuo/openInvest/issues/45)) ([12b574a](https://github.com/longsizhuo/openInvest/commit/12b574a6ed4615464086642b12c1f7df971bf201))
* **probability:** forward-return 单一可信源/日历天口径 + 干预 rule 并桶（漂移审计） ([#41](https://github.com/longsizhuo/openInvest/issues/41)) ([f19b415](https://github.com/longsizhuo/openInvest/commit/f19b415f3ec6bae9db8df6e8789bc99462d145fb))


### Docs

* **governance:** 治理章程（三原则+口径单源+否决权）——独立于代码，待用户签字 ([#43](https://github.com/longsizhuo/openInvest/issues/43)) ([4540538](https://github.com/longsizhuo/openInvest/commit/45405383264bf30fc98bc9309403cd64bc477666))

## [0.8.0](https://github.com/longsizhuo/openInvest/compare/v0.7.0...v0.8.0) (2026-06-13)


### Features

* **api:** committee prepare/save 端点 + run summary 附 cio_memo ([0a552a3](https://github.com/longsizhuo/openInvest/commit/0a552a3c60ba49a30cd5208665dd81ba92bf992a))
* **api:** skill-parity 端点（doctor/status/strategy/history/what_if/buy/sell） ([6d975fa](https://github.com/longsizhuo/openInvest/commit/6d975fa738324664c8ec278082f26462edb1f4a1))
* **api:** 可选 bearer token 鉴权（INVEST_API_TOKEN） ([6229bff](https://github.com/longsizhuo/openInvest/commit/6229bff1d2cd63ec3f4d4a0f3268cc63a5397906))
* **cli:** INVEST_API_BASE 远端模式（hub-and-spoke 客户端） ([2136260](https://github.com/longsizhuo/openInvest/commit/213626061e4ea98c3d54feedc016595793d08044))
* **committee:** 反事实记账——确定性拦截落 interventions.jsonl + 钱口径复盘 job ([#36](https://github.com/longsizhuo/openInvest/issues/36)) ([bff20d8](https://github.com/longsizhuo/openInvest/commit/bff20d81bb0104ccf631ffe403490c69aa1280f5))
* **committee:** 干预账本历史回填 + 未结算浮动预览 ([#37](https://github.com/longsizhuo/openInvest/issues/37)) ([d7d561a](https://github.com/longsizhuo/openInvest/commit/d7d561aadbc53f79f069fdb4dbb5cde7d65339f7))
* **events:** 指数→代理标的确定性映射层（closes [#26](https://github.com/longsizhuo/openInvest/issues/26)） ([830c25a](https://github.com/longsizhuo/openInvest/commit/830c25ad0ba24bcf8176adab3b3d42384162e33e))
* **probability:** 路径校准层 + walk-forward 闭环 + TA 实验结论（ADR-009） ([#31](https://github.com/longsizhuo/openInvest/issues/31)) ([e333076](https://github.com/longsizhuo/openInvest/commit/e333076cc2df03cfd1ba052aa8fdfa095a0b2791))
* **report:** 路径概率渲染进日报邮件——与 CIO 看到的同一份分布 ([#32](https://github.com/longsizhuo/openInvest/issues/32)) ([4f00f46](https://github.com/longsizhuo/openInvest/commit/4f00f46628400a9adb88b79f7edf79fa44901037))
* **skill:** run.sh 远端模式适配 ([c4e31eb](https://github.com/longsizhuo/openInvest/commit/c4e31eba1baa101c758d7a0b3c5761296c944b71))


### Bug Fixes

* **committee:** coordinator 路径补确定性事实块——防御链失效修复 ([1dc3e0b](https://github.com/longsizhuo/openInvest/commit/1dc3e0bc128c8ccdb534948ee1d43b023793d55f))


### Refactor

* **core:** 提取 skill 视图与委员会 prepare/save 到 service 层 ([8c104a7](https://github.com/longsizhuo/openInvest/commit/8c104a79118ce7622e27a427c884f1b69bb68392))


### Docs

* **setup-skill:** 新增'连接已有 hub'onboarding 路径 ([7ef9b8b](https://github.com/longsizhuo/openInvest/commit/7ef9b8b6bb8baf5222d320c508b35e458bfa1065))
* **skill:** coordinator 指引对齐 v0.6——确定性事实块粘贴义务 ([2a690d0](https://github.com/longsizhuo/openInvest/commit/2a690d088ff932887a291b1a4e1f771da36cc993))
* **skill:** 远端模式（hub-and-spoke）使用说明 ([bd92abc](https://github.com/longsizhuo/openInvest/commit/bd92abc91439497f6913133a181cd1fc59e92fc0))
* **wiki:** combined 联合分析师补测——'直接加三个'两窗口均 FAIL 且劣于单独 ([#35](https://github.com/longsizhuo/openInvest/issues/35)) ([57467a1](https://github.com/longsizhuo/openInvest/commit/57467a15a301b8b76f4fd1fc30ca49d93f995277))
* **wiki:** hub-and-spoke 部署拓扑 + skill-parity/committee RPC 端点参考 ([a4528c0](https://github.com/longsizhuo/openInvest/commit/a4528c04b979a8e1195c1e1ec44ead196218a053))
* **wiki:** TA 实验复测矩阵——ADR-009 经 2 窗口×3 模型×ensemble 复测维持原判 ([#33](https://github.com/longsizhuo/openInvest/issues/33)) ([76a4084](https://github.com/longsizhuo/openInvest/commit/76a4084290f17b8f93ac008c260c90586910abd8))
* **wiki:** 拆方向锁后的文档对齐——硬约束→中性概率口径 ([8d9fcd1](https://github.com/longsizhuo/openInvest/commit/8d9fcd1fa7fa4aa7028141a2dc981a5a90b0309b))

## [0.7.0](https://github.com/longsizhuo/openInvest/compare/v0.6.0...v0.7.0) (2026-06-10)


### Features

* **probability:** 路径形状三类→四类（加 max 轴）+ regime 持续中位标注 + 算法落 wiki ([641eb1e](https://github.com/longsizhuo/openInvest/commit/641eb1eccf26b36b3a3c43845d4e0deb87149cda))

## [0.6.0](https://github.com/longsizhuo/openInvest/compare/v0.5.0...v0.6.0) (2026-06-10)


### Features

* **committee:** uptrend 杠杆做成显式 risk_profile 风险档（默认 steady） ([a45e791](https://github.com/longsizhuo/openInvest/commit/a45e7910513eb4f097127847fa39527cdd0f4a66))
* **committee:** 独立快崩防御——VIX/ATR 任一触发，确定性降级买侧 verdict ([eb10a4e](https://github.com/longsizhuo/openInvest/commit/eb10a4e25d2deea8c4293a813f85e6d607548228))
* **committee:** 补基本面/情绪维度对齐 TradingAgents（确定性事实块） ([6c076a1](https://github.com/longsizhuo/openInvest/commit/6c076a1145280a01e21a811990a8eff73227f847))
* **committee:** 防御 ATR 腿改通用口径——波动突变比，删 per-asset 绝对线 ([3c53b54](https://github.com/longsizhuo/openInvest/commit/3c53b543f33b59237794d5b59a573ba3b5f1f87e))
* **events:** 黄金事件覆盖——entity→GC=F 确定性兜底 + 持金常驻 gold queries ([51686e0](https://github.com/longsizhuo/openInvest/commit/51686e064ee7ee17ae41ef7697168e483c31664c))
* **probability:** 概率表路径化——30/60/90 多窗分布 + 路径形状 ([824c4cd](https://github.com/longsizhuo/openInvest/commit/824c4cd1d7f03742d394e370f63a2885e2b385f4))
* **regime:** 拆 regime 方向锁层，STRATEGY_HINT 改中性 OHLC 概率口径 ([90e0b41](https://github.com/longsizhuo/openInvest/commit/90e0b41ba9cc1719669c5c358b9c9e148ae838e7))
* **regime:** 防御 ATR 线与 crash 分类解耦——新增 defense_atr_pct_min ([7c95950](https://github.com/longsizhuo/openInvest/commit/7c9595078e4cca5f1cf7db90daa45b2d350912df))
* **regime:** 防御 ATR 线按确定性 sweep 调优 NDQ/GC → 2.0 ([5180928](https://github.com/longsizhuo/openInvest/commit/5180928e663ba1c08d46ff7f482a4fee8013ba3a))
* **sentiment:** EVENT_STANCE 机制升级——per-asset 行 + 加权公式（默认禁用） ([7683c07](https://github.com/longsizhuo/openInvest/commit/7683c079eace5c4524377530ab9a4bea74e2adb7))


### Refactor

* **config:** 情绪/估值 magic number 迁入 tunable config 统一维护 ([7acf9e1](https://github.com/longsizhuo/openInvest/commit/7acf9e125968a299c503d4eacba3e4baf5f5d8e5))


### Docs

* **services:** 修正 news.py 过时孤儿注释——ddgs 已经事件层接入 production ([a6311a7](https://github.com/longsizhuo/openInvest/commit/a6311a73196f45fc494cb7af30e93b36a6819fef))

## [0.5.0](https://github.com/longsizhuo/openInvest/compare/v0.4.0...v0.5.0) (2026-05-30)


### Features

* **logging:** ADR-014 生产代码 print→log 迁移 + RotatingFileHandler ([#21](https://github.com/longsizhuo/openInvest/issues/21)) ([b93a42c](https://github.com/longsizhuo/openInvest/commit/b93a42cd5eab758af80cb0f24efab056fc0d15cc))
* **regime:** 概率表/买回点数据源换成几十年 OHLC 直算（替代 verdict_review 276 条） ([d187faf](https://github.com/longsizhuo/openInvest/commit/d187fafe5dc233cb66e20f96572f71e6b2792884))


### Bug Fixes

* **committee:** backup_cny 读对 key + 抽 load_backup_cny 单一可信源 + force-HOLD 归零 alloc ([1e96d3e](https://github.com/longsizhuo/openInvest/commit/1e96d3ea103d59d69250f284cc44be49507954ac))
* **committee:** review fixes — store 未定义、solvency 拼写、Sanity4 confidence/alloc ([2888c44](https://github.com/longsizhuo/openInvest/commit/2888c440afa911c42712e6bb157f31f567a9edca))
* **regime:** 重叠窗口用 effective_n 判 low_confidence + forward-return correctness 测试 ([94c32d3](https://github.com/longsizhuo/openInvest/commit/94c32d361388bfd0c5ccf4e34c30d3ea079f4ec0))
* **sweep:** regime 阈值验证读全量历史，去掉 get_history_data 730 天 cap ([9fbf9b7](https://github.com/longsizhuo/openInvest/commit/9fbf9b7e8fe0cfcc9c961f8ff7d1f2bcdeab5665))

## [0.4.0](https://github.com/longsizhuo/openInvest/compare/v0.3.0...v0.4.0) (2026-05-28)


### Features

* **committee:** SOLVENCY=strong 时集中度不触发 TRIM（确定性后处理） ([82a2ec1](https://github.com/longsizhuo/openInvest/commit/82a2ec153d9888251e742699f325fec2754ee8f9))
* **committee:** TRIM 路径化 — 卖出后路径 + 买回点，给不出更低买回点则降级 HOLD ([dcbaa74](https://github.com/longsizhuo/openInvest/commit/dcbaa7419d96df49f9200d3c1db44ab7c8b3cfa8))
* **probability:** regime 概率表 — 按 (asset, regime) 给历史 forward return 分布 ([5f209de](https://github.com/longsizhuo/openInvest/commit/5f209de2c7f122db3dab51361df4d7ef1cbcc7ef))


### Bug Fixes

* **cio:** TRIM 约束字段名对齐 + 明确覆盖通用 TRIM 规则 ([305b162](https://github.com/longsizhuo/openInvest/commit/305b162e46548a85da902fc0ee420c7f8968e5b8))
* **cio:** TRIM 阈值改走 config 注入，消除魔法数字 ([a8578c3](https://github.com/longsizhuo/openInvest/commit/a8578c31e5fc23f9597e297ffe3bd7b386797835))
* **cio:** 零花钱账户小幅浮亏禁止 TRIM ([560fb6a](https://github.com/longsizhuo/openInvest/commit/560fb6a680df593221e601a16ba70175b892f74b))
* **config:** env override 多词 section 解析 + per-asset 支持 + CR 修复 ([52392de](https://github.com/longsizhuo/openInvest/commit/52392de1196b04ce4dbd7c55d3500dc2b401e813))
* **dreaming:** LLM REJECT 从 candidates.json 移除 + prompt 加 uptrend 怀疑清单 ([958430d](https://github.com/longsizhuo/openInvest/commit/958430d4a95fae3959bc904f5be175e8c7f6a06e))


### Refactor

* **cio:** TRIM 约束阈值默认 0（禁用），等 sweep OOS 验证后再启用 ([f7313e1](https://github.com/longsizhuo/openInvest/commit/f7313e128e00f580518e7832e13860c493124c83))


### Docs

* **adr:** ADR-011 HOLD Oracle 语义——hold_wrong 只判下跌方向 ([4201273](https://github.com/longsizhuo/openInvest/commit/4201273deb7ad9972a98e227e49a573a57a545cb))

## [0.3.0](https://github.com/longsizhuo/openInvest/compare/v0.2.0...v0.3.0) (2026-05-27)


### Features

* **config:** 50+ 参数 config 化，sweep runner + ADR-010 ([6a65680](https://github.com/longsizhuo/openInvest/commit/6a6568044788d1582b63d0a491bfef75b8404a46))


### Bug Fixes

* **web-api:** 修复 3 个生产风险：非原子交易、DB crash-loop、取款竞态 ([596590e](https://github.com/longsizhuo/openInvest/commit/596590ed3b8fb01d796ce79acc019d50f8171c0d))


### Docs

* **adr:** 新增 ADR 009 用户纪律承诺模板（理由段待本人填） ([e505c7c](https://github.com/longsizhuo/openInvest/commit/e505c7c1e21f81b330aa148d72f2615b084e85c2))

## [0.2.0](https://github.com/longsizhuo/openInvest/compare/v0.1.0...v0.2.0) (2026-05-27)


### Features

* **committee:** 指标修正 + regime 双触发器/recovery + dreaming lift-based caution + backtest 防穿越修复 ([88700fa](https://github.com/longsizhuo/openInvest/commit/88700fadf92e6fc641ea1179cfc93a04c509b861))


### Bug Fixes

* **dreaming:** LLM 验伪构造 payload 用 c["action"] 崩溃 ([ad9d964](https://github.com/longsizhuo/openInvest/commit/ad9d964ac8daccbcecd4f76bc8124c6f75c8f23c))
* **event-watch:** _run_committee_task 跑完补发 verdict 邮件 ([02362c1](https://github.com/longsizhuo/openInvest/commit/02362c1acdc618ceb7400a58378357595a5d86e1))
