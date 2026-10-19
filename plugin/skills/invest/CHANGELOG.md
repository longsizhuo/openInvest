# Changelog

## [0.32.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.31.0...invest-skill-v0.32.0) (2026-10-19)


### Features

* **autopilot:** IBKR live autopilot — LLM picks and trades US stocks/ETFs via Web API OAuth under hard caps ([#522](https://github.com/longsizhuo/openInvest/issues/522)) ([348998e](https://github.com/longsizhuo/openInvest/commit/348998e3a85d2c3675355333b5b42268c8d9513e))

## [0.31.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.30.0...invest-skill-v0.31.0) (2026-10-19)


### Features

* **events:** agent mode — queue model work for the host agent ([#505](https://github.com/longsizhuo/openInvest/issues/505)) ([ed8727c](https://github.com/longsizhuo/openInvest/commit/ed8727ce4e00a6c0faff28237c597864b00580ef))
* **report:** agent-mode daily report built from submitted verdicts ([#510](https://github.com/longsizhuo/openInvest/issues/510)) ([5dc8823](https://github.com/longsizhuo/openInvest/commit/5dc8823a4d7680bee5e3384d6d188ddd73e03544))
* **review:** split scorecard by model era (Sonnet 5.5 vs DeepSeek) + Coordinator records model ([#497](https://github.com/longsizhuo/openInvest/issues/497)) ([be74cd7](https://github.com/longsizhuo/openInvest/commit/be74cd7317002addb32993345ab6344bcd64b872))


### Docs

* **skill:** Coordinator protocol names the MCP submit_committee hand-in ([#515](https://github.com/longsizhuo/openInvest/issues/515)) ([9567060](https://github.com/longsizhuo/openInvest/commit/95670601e66dbf73dec62ff38a320904b5091ade))

## [0.30.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.29.0...invest-skill-v0.30.0) (2026-10-18)


### Features

* **committee:** agent-run committee over MCP with backend-held facts ([#495](https://github.com/longsizhuo/openInvest/issues/495)) ([164e5d2](https://github.com/longsizhuo/openInvest/commit/164e5d2e63980a0ae17f280ce1be549f821ed5e8))

## [0.29.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.28.0...invest-skill-v0.29.0) (2026-10-18)


### Features

* **fund:** 场外基金两段式——下单扣现金记在途，确认日按净值到账（ADR-027 PR3） ([#490](https://github.com/longsizhuo/openInvest/issues/490)) ([36e9e32](https://github.com/longsizhuo/openInvest/commit/36e9e32db2ecb25a7c0430df2c97c17427acdd13))
* **ledger:** 账本活动类型 + 原始价估值（ADR-027 PR2） ([#482](https://github.com/longsizhuo/openInvest/issues/482)) ([e3bc54a](https://github.com/longsizhuo/openInvest/commit/e3bc54aef53b86fd50c98505f452f782b5d8d518))

## [0.28.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.27.0...invest-skill-v0.28.0) (2026-10-18)


### Features

* **sentinel:** per-symbol alert lines (price / daily move / drawdown from cost) ([#469](https://github.com/longsizhuo/openInvest/issues/469)) ([c460c47](https://github.com/longsizhuo/openInvest/commit/c460c4779f02c3d48b994973b2af1e62373ade8d))


### Bug Fixes

* **ops:** 运维加固 D3 超时+心跳 / D6 部署守卫 / D7 锁·jsonl·bak·scheduler unit ([#459](https://github.com/longsizhuo/openInvest/issues/459)) ([3149869](https://github.com/longsizhuo/openInvest/commit/3149869c89193f7afe334872faeeef2bf26723df))
* **portfolio:** gold booked per gram in CNY, MCP doctor + optional checks, target_pct writable ([#443](https://github.com/longsizhuo/openInvest/issues/443) [#444](https://github.com/longsizhuo/openInvest/issues/444) [#449](https://github.com/longsizhuo/openInvest/issues/449)) ([#457](https://github.com/longsizhuo/openInvest/issues/457)) ([30f120d](https://github.com/longsizhuo/openInvest/commit/30f120d2f582c2d90a4a4f7237b39b12a5075ea4))


### Refactor

* 去重 + 删死代码 + 文档漂移修正（[#236](https://github.com/longsizhuo/openInvest/issues/236) / 评审 D8） ([#458](https://github.com/longsizhuo/openInvest/issues/458)) ([a84632e](https://github.com/longsizhuo/openInvest/commit/a84632e1d103c58b51fbb63105a83fcd41f23159))


### Docs

* **skill:** point screeners / options / earnings calls / SEC filings at hosted MCPs ([46e0c55](https://github.com/longsizhuo/openInvest/commit/46e0c559f9ff2166f306c96d59f9012063cd8e1b))

## [0.27.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.26.0...invest-skill-v0.27.0) (2026-10-18)


### Features

* **portfolio:** real-account TWR/XIRR/drawdown/benchmark in status and daily report ([#433](https://github.com/longsizhuo/openInvest/issues/433)) ([74dac7b](https://github.com/longsizhuo/openInvest/commit/74dac7b1a1e50cbab66ae4e263180746eaa33480))
* **portfolio:** rebalance_plan turns strategy targets into a buy/sell list ([#447](https://github.com/longsizhuo/openInvest/issues/447)) ([3425844](https://github.com/longsizhuo/openInvest/commit/3425844a19e2242600b00df760722e9af2e1757e))


### Bug Fixes

* **committee:** Sprint 1 端到端跟进——成交匹配起点、单次上限措辞与守卫、缓存路径、performance 提示 ([#445](https://github.com/longsizhuo/openInvest/issues/445)) ([f02b3b2](https://github.com/longsizhuo/openInvest/commit/f02b3b29f39c6aa39825a60c87a8efbcfdab54b6))

## [0.26.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.25.0...invest-skill-v0.26.0) (2026-10-16)


### Features

* **committee:** base currency for committee amounts, thresholds and portfolio summary ([#426](https://github.com/longsizhuo/openInvest/issues/426)) ([7b1519a](https://github.com/longsizhuo/openInvest/commit/7b1519a894e53aab4fa2a3cbba8789482a0917da))

## [0.25.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.24.3...invest-skill-v0.25.0) (2026-10-15)


### Features

* **cli:** add explain_decision subcommand for MCP parity ([#414](https://github.com/longsizhuo/openInvest/issues/414)) ([fea2c64](https://github.com/longsizhuo/openInvest/commit/fea2c64ad53ddc1270cfbd3f65d940a4185b66d2))


### Bug Fixes

* **committee:** load event brief in Coordinator prepare_committee_brief ([#404](https://github.com/longsizhuo/openInvest/issues/404)) ([e745ab5](https://github.com/longsizhuo/openInvest/commit/e745ab584f0a36a3dc7d10169f4d73f2fe713d13))
* run.sh hints only on silent failures; JSON error for an uninitialized data dir ([#413](https://github.com/longsizhuo/openInvest/issues/413)) ([eb71140](https://github.com/longsizhuo/openInvest/commit/eb71140d0a2655f44d613dacd1fa75c6d446dd73))

## [0.24.3](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.24.2...invest-skill-v0.24.3) (2026-10-13)


### Bug Fixes

* **skill:** keys never go through the chat; skill-relative run.sh paths ([accc7d9](https://github.com/longsizhuo/openInvest/commit/accc7d9155388debce51be810a0e581455dfa28c))
* **skill:** onboarding asks 3 questions; drop answers nothing reads ([a0a03dc](https://github.com/longsizhuo/openInvest/commit/a0a03dc4d62a59c99a5c57c47ba3bc494f5c8e7b))

## [0.24.2](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.24.1...invest-skill-v0.24.2) (2026-10-13)


### Bug Fixes

* **plugin:** Hermes installs from plugin/, backend version pinned in run.sh; Codex/Hermes install docs ([#369](https://github.com/longsizhuo/openInvest/issues/369)) ([6153827](https://github.com/longsizhuo/openInvest/commit/61538273971a8b682761a8124e201aa2aaefc786))

## [0.24.1](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.24.0...invest-skill-v0.24.1) (2026-10-11)


### Docs

* **skill:** tell the user when an imported fund could not be converted to units ([6c148f0](https://github.com/longsizhuo/openInvest/commit/6c148f043caddc1a88e102d0b8c3c73a55ddee71))

## [0.24.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.23.1...invest-skill-v0.24.0) (2026-10-10)


### Features

* **committee:** run the committee on Chinese off-exchange funds (FUND:&lt;code&gt;) ([#349](https://github.com/longsizhuo/openInvest/issues/349)) ([a8b85d1](https://github.com/longsizhuo/openInvest/commit/a8b85d18d35ccb40c49710a0afc149a011c03d2a))

## [0.23.1](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.23.0...invest-skill-v0.23.1) (2026-10-10)


### Bug Fixes

* **onboarding:** init --force updates name and risk tolerance in user.md ([#338](https://github.com/longsizhuo/openInvest/issues/338)) ([81a5bdf](https://github.com/longsizhuo/openInvest/commit/81a5bdfb1aa4d6ac9c97e6890d5c936ddf470158))

## [0.23.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.22.1...invest-skill-v0.23.0) (2026-10-10)


### Features

* **onboarding:** new installs record the cash they enter; record_existing_position for pre-held positions ([#320](https://github.com/longsizhuo/openInvest/issues/320)) ([a5e54ba](https://github.com/longsizhuo/openInvest/commit/a5e54ba9d26aa4b208b715e9614bacf79d7de954))


### Bug Fixes

* **mcp:** disable ingest_event in advisory mode ([#339](https://github.com/longsizhuo/openInvest/issues/339)) ([0d52d1e](https://github.com/longsizhuo/openInvest/commit/0d52d1e762b436bdbe4c96772eb5d490e7c97da4))
* **status:** only fetch and show ndq/gold/AUD sections when the user holds them ([#335](https://github.com/longsizhuo/openInvest/issues/335)) ([1b98c17](https://github.com/longsizhuo/openInvest/commit/1b98c17897a82fd017ddc51aecb84e4ff6e8b186))


### Docs

* **mcp:** stop hard-coding the MCP tool count; list every tool in the invest skill ([#336](https://github.com/longsizhuo/openInvest/issues/336)) ([3017df5](https://github.com/longsizhuo/openInvest/commit/3017df5c136e97cb1dc077b65546d3ae9817630a))

## [0.22.1](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.22.0...invest-skill-v0.22.1) (2026-10-09)


### Docs

* **skill:** use FUND:&lt;code&gt; for Chinese off-exchange mutual funds ([1770975](https://github.com/longsizhuo/openInvest/commit/1770975054089f0a6a0a488d464e5dba11492c06))

## [0.22.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.21.2...invest-skill-v0.22.0) (2026-10-08)


### Features

* **mcp:** run_committee 上报辩论进度、decisions 支持过滤与截断（[#133](https://github.com/longsizhuo/openInvest/issues/133)） ([#309](https://github.com/longsizhuo/openInvest/issues/309)) ([447ab58](https://github.com/longsizhuo/openInvest/commit/447ab58a7d5875346f4bc92d45fde4640c630d92))

## [0.21.2](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.21.1...invest-skill-v0.21.2) (2026-10-08)


### Docs

* **skill:** quote confidence_lookup next to verdicts, keep self-reported confidence as raw ([#295](https://github.com/longsizhuo/openInvest/issues/295)) ([5864ac3](https://github.com/longsizhuo/openInvest/commit/5864ac3a4403137fcc5e4b3c99f160e5d0219c72))

## [0.21.1](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.21.0...invest-skill-v0.21.1) (2026-10-08)


### Docs

* **skill:** explain weekend_dup_excluded in the verdict_review summary ([#292](https://github.com/longsizhuo/openInvest/issues/292)) ([368aa91](https://github.com/longsizhuo/openInvest/commit/368aa916d510601d5a480935c4aa6305b4752328))

## [0.21.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.20.2...invest-skill-v0.21.0) (2026-10-08)


### Features

* **verdict-review:** live-only daily review, DB-only prices, summary split by source ([#279](https://github.com/longsizhuo/openInvest/issues/279)) ([4382f7e](https://github.com/longsizhuo/openInvest/commit/4382f7e0b1045e2fea2285d84c4c7982879685e8))

## [0.20.2](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.20.1...invest-skill-v0.20.2) (2026-10-08)


### Bug Fixes

* **events:** 触发闸提成共享 service——Hermes 投喂的事件接上委员会触发，两门共享冷却/日上限 ([#261](https://github.com/longsizhuo/openInvest/issues/261)) ([b633cf2](https://github.com/longsizhuo/openInvest/commit/b633cf20f88e3969117013719586fdd6bd5b0eb5))
* **news:** RSS 抓取加整次总时限与地址校验，add_news_source 只收公网源且不阻塞 MCP ([#262](https://github.com/longsizhuo/openInvest/issues/262)) ([566538f](https://github.com/longsizhuo/openInvest/commit/566538fd1973ab3f19e032ff55452f2fa676f7e2))

## [0.20.1](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.20.0...invest-skill-v0.20.1) (2026-07-17)


### Bug Fixes

* **skill:** 修 Stage 3 括号理由——Macro 不进 Round 2 与"共享"无关 ([7a956eb](https://github.com/longsizhuo/openInvest/commit/7a956ebef6a58658d4e045703190558c4d117bec)), closes [#221](https://github.com/longsizhuo/openInvest/issues/221)

## [0.20.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.19.0...invest-skill-v0.20.0) (2026-07-16)


### Features

* **skill:** SKILL.md 与全部 references 英文化——正文英文 + 双语触发短语 ([83b6e59](https://github.com/longsizhuo/openInvest/commit/83b6e59a4cca8ecdb65214c4381b04faa336f87f))

## [0.19.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.18.8...invest-skill-v0.19.0) (2026-07-14)


### Features

* **skill:** 新闻投喂补「情报哨兵」定时主动喂料指引 ([6123b9e](https://github.com/longsizhuo/openInvest/commit/6123b9e15101df3b44a86f01eaf8c6b41b070474))


### Docs

* 情报哨兵产品化——SKILL.md 指引 + wiki 21 章 ([97989d8](https://github.com/longsizhuo/openInvest/commit/97989d813cd205f2558766e35a1d596f11790b83))

## [0.18.8](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.18.7...invest-skill-v0.18.8) (2026-07-14)


### Docs

* **skill:** Hermes Coordinator 协议 + 路由表改按交互/无人值守分轴 ([27d06ee](https://github.com/longsizhuo/openInvest/commit/27d06ee63b23e645210339a905b0fde6475f6dbb))

## [0.18.7](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.18.6...invest-skill-v0.18.7) (2026-07-13)


### Docs

* **skill:** daily_report 子命令指引——日报格式后端统一，cron 投递归宿主 agent ([7f78c68](https://github.com/longsizhuo/openInvest/commit/7f78c684bec7d88a9cf4f7ba1fdf0db2968ee072))
* **skill:** Direct 路径 agent 示例补 Hermes / OpenClaw，与市场用户构成对齐 ([077c266](https://github.com/longsizhuo/openInvest/commit/077c26624d6e1f2472ac57bc1597d19baf3338b6))

## [0.18.6](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.18.5...invest-skill-v0.18.6) (2026-07-11)


### Docs

* **skill:** remote MCP 指引同步 BETA 标注 ([67d584c](https://github.com/longsizhuo/openInvest/commit/67d584c8a49b06f9132b54f50e38aba311df4cea))

## [0.18.5](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.18.4...invest-skill-v0.18.5) (2026-07-11)


### Docs

* **skill:** remote MCP spoke 直连指引 ([a8c36a3](https://github.com/longsizhuo/openInvest/commit/a8c36a36d9f4c2581b3f32ec0c4955badf4f4eb0))

## [0.18.4](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.18.3...invest-skill-v0.18.4) (2026-07-11)


### Docs

* **skill:** strategy 写工具注册进 agent 指引 ([5c0d9e4](https://github.com/longsizhuo/openInvest/commit/5c0d9e4c8850667cde3962763647570a6e7224a6))

## [0.18.3](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.18.2...invest-skill-v0.18.3) (2026-07-10)


### Docs

* **mcp:** MCP 工具数 14→15 全仓对齐 + 枚举补 ingest_event ([0d7fdcd](https://github.com/longsizhuo/openInvest/commit/0d7fdcd837f5b332e46dcbd6eba0ba2215fe69a5))

## [0.18.2](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.18.1...invest-skill-v0.18.2) (2026-07-08)


### Bug Fixes

* **skill:** dev-mode 分支保留真实退出码，不再吞掉非零状态 ([4052078](https://github.com/longsizhuo/openInvest/commit/4052078d0b07f8be8d4a25ec43e9a1b4dbc2405b))
* **skill:** 修正 plugin/ 重排后 REPO_ROOT 相对路径少跳一层 ([48b9b20](https://github.com/longsizhuo/openInvest/commit/48b9b2077453dd0e2be1caecf6a60927eb582c17))

## [0.18.1](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.18.0...invest-skill-v0.18.1) (2026-07-07)


### Docs

* **agents:** Hermes 接入指南（config.yaml MCP + agentskills 拷入）+ 行情 skill 联动投喂；uv.lock 版本号同步 ([999214b](https://github.com/longsizhuo/openInvest/commit/999214bf6fde44fa03a030efead4387317975c72))
* **skill:** SKILL.md 补 Hermes 原生元数据（platforms + metadata.hermes.tags）——增量字段，Claude/Codex 忽略 ([fa69fd7](https://github.com/longsizhuo/openInvest/commit/fa69fd79db744a6fdbf68fb42bd174098c06275a))

## [0.18.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.17.0...invest-skill-v0.18.0) (2026-07-06)


### Features

* **events:** ingest_event agent 投喂 + RSS 泛头条预过滤（[#153](https://github.com/longsizhuo/openInvest/issues/153) 方案①②） ([c4c9c34](https://github.com/longsizhuo/openInvest/commit/c4c9c34229126e7c43829b53186370e766c60888))
* **events:** ingest_event agent 投喂 + RSS 预过滤（[#153](https://github.com/longsizhuo/openInvest/issues/153) ①②） ([2740707](https://github.com/longsizhuo/openInvest/commit/2740707b1058824a53b81b8281d24d7c1446b79d))

## [0.17.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.16.1...invest-skill-v0.17.0) (2026-07-06)


### ⚠ BREAKING CHANGES

* **plugin:** skill 源文件 git 路径变更 skills/* → plugin/skills/*（根 skills/ 符号链接保持磁盘兼容）

### Features

* **plugin:** Codex plugin cache 瘦身 44MB→156KB——真身入 plugin/，marketplace source 指回 ./plugin ([c3ad092](https://github.com/longsizhuo/openInvest/commit/c3ad0929960309afc90b0d822d8a0ad9d55c6ed4))

## [0.16.1](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.16.0...invest-skill-v0.16.1) (2026-07-05)


### Docs

* 全量文档对齐 2026-07-05 现实——GUI/NapCat 退役、PyPI+uvx 分发、Web API deprecated ([f828195](https://github.com/longsizhuo/openInvest/commit/f8281951fd270d844c0986e28a34995b758b1ce3))

## [0.16.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.15.0...invest-skill-v0.16.0) (2026-07-05)


### ⚠ BREAKING CHANGES

* **gui:** run.sh gui 子命令移除；web_api 不再挂载 GUI 静态文件
* **dist:** run.sh 不再 clone/更新后端仓库，后端版本由 PyPI 管理

### Features

* **dist:** run.sh 收敛为 uvx 薄转发——退役 clone/uv sync/自愈 180 行 bash ([c64ba2a](https://github.com/longsizhuo/openInvest/commit/c64ba2ab5280ff9a297859d9cef38924463e8d77))
* **skill:** run.sh 加 mcp 子命令——plugin .mcp.json 的 stdio 启动入口 ([e0b27c9](https://github.com/longsizhuo/openInvest/commit/e0b27c9847ad1ae0fa30ab27f039e5028ac751fe))


### Bug Fixes

* **decisions:** 修复 code review 全部 10 项发现 ([81822d5](https://github.com/longsizhuo/openInvest/commit/81822d52cec924cb1476b07089795a497daf5300))
* **dist:** code review [#139](https://github.com/longsizhuo/openInvest/issues/139) 全部 10 项发现——uvx 纯数据目录形态的 onboarding/.env/提示链 ([41b6054](https://github.com/longsizhuo/openInvest/commit/41b6054eb16bf06ea1e29a56c13ec4040a0c39ab))
* **pkg:** 重排收尾——补 __init__.py / yml entry 路径 / JOBS_DIR 包内解析 / CI smoke+lint 更新 ([d3f05fd](https://github.com/longsizhuo/openInvest/commit/d3f05fd41972f7d1ab5e821e4eb5bad6147d2214))
* remove eager import cascade in committee __init__.py + stale doc paths ([b8e84ab](https://github.com/longsizhuo/openInvest/commit/b8e84ab1a147c94ad7297bd9181c405c29938892))


### Refactor

* agents/skills/ → capabilities/committee/&lt;role&gt;/ (co-located .py + .md) ([838a834](https://github.com/longsizhuo/openInvest/commit/838a8347fdb0f2ab472b546d265ac331aa0ccd5f))
* agents/skills/ → capabilities/committee/&lt;role&gt;/ (co-located .py + .md) ([#135](https://github.com/longsizhuo/openInvest/issues/135)) ([a4b6465](https://github.com/longsizhuo/openInvest/commit/a4b6465b593718af3f78f093ec829edc07e6303d))
* **gui:** GUI 壳层退役——后端不再 serve 静态文件，Web API 标记 deprecated ([390c87d](https://github.com/longsizhuo/openInvest/commit/390c87d6c43775d03abe3dfd42df10bf74cc1679))
* **skill:** SKILL.md 收缩为 workflow——工具表移交 references/tools.md (issue [#133](https://github.com/longsizhuo/openInvest/issues/133) Decision 6) ([23eb04c](https://github.com/longsizhuo/openInvest/commit/23eb04c05eb94740e31f7c9bb0ed7eae3eae5c38))


### Docs

* **skill:** decisions / record_execution 子命令 + /api/decisions 端点进 SKILL.md ([800ab61](https://github.com/longsizhuo/openInvest/commit/800ab6115f40df810e457e6c24853eb97138e11a))

## [0.15.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.14.0...invest-skill-v0.15.0) (2026-07-03)


### Features

* **event-watch:** 扫描窗口修正为北京 8:00-次日2:30 并进 config 白名单 ([#128](https://github.com/longsizhuo/openInvest/issues/128)) ([54ad4e0](https://github.com/longsizhuo/openInvest/commit/54ad4e0e38c7afe23b8b40be32266cf0d1ef59fe))
* **sentinel:** 价格异动哨兵——垂直线先报警后触发委员会 (ADR-025) ([#129](https://github.com/longsizhuo/openInvest/issues/129)) ([b9ef160](https://github.com/longsizhuo/openInvest/commit/b9ef160e9f50ca1531579ec13004bf37413c2df1))
* **verdict:** 现金机会成本规则改 opt-in,默认 OFF (ADR-024) ([5320926](https://github.com/longsizhuo/openInvest/commit/532092615a33bc263e9040e32c56f9f928edd42b))


### Docs

* **skill:** B2 截图持仓导入(agent-OCR 路径) ([2e4c83a](https://github.com/longsizhuo/openInvest/commit/2e4c83a2ce547e7b4ef3eb5b3a9a7e5ce3a44a8b))
* **skill:** config 子命令列出 cash_opportunity_cost_rule(ADR-024) ([5288dc5](https://github.com/longsizhuo/openInvest/commit/5288dc58edfcaadf7ae0abcb8622c50b42569c27))
* **skill:** 截图持仓导入走 agent-OCR(你读图→转文字→import) ([d294fbf](https://github.com/longsizhuo/openInvest/commit/d294fbf101f86bf2e0ebf52095eba667aa6f5e0d))

## [0.14.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.13.0...invest-skill-v0.14.0) (2026-06-30)


### Features

* **discipline:** 纪律台账——委员会可证价值(不作为+拦冲动)落邮件/CLI/API ([80f9a08](https://github.com/longsizhuo/openInvest/commit/80f9a08a9e45d38d08acf88cde4dcb720e0e2438))
* **holdings:** 自由文本/CSV 持仓导入 (POST /api/holdings/import + CLI import) ([a7ce60c](https://github.com/longsizhuo/openInvest/commit/a7ce60cd8481d031fbdecf199bd72cbf47a9ac37))


### Docs

* **skill:** SKILL.md 加 import 子命令 + POST /api/holdings/import ([1c01391](https://github.com/longsizhuo/openInvest/commit/1c0139141a68ec3fb4b55c479ef7d456296c47ef))
* **skill:** SKILL.md 补 discipline 子命令 + GET /api/discipline ([3fa643d](https://github.com/longsizhuo/openInvest/commit/3fa643d2e748253abf78478df0dfc8a8e8705925))

## [0.13.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.12.0...invest-skill-v0.13.0) (2026-06-27)


### Features

* **config:** 运行时 config-via-API + 集中度 lens 开关（ADR-017） ([#71](https://github.com/longsizhuo/openInvest/issues/71)) ([981a0e2](https://github.com/longsizhuo/openInvest/commit/981a0e2cb8cf6cf8b7849c4401c8d2a310295bd6))
* **events:** GET /api/events/recent + POST /api/events/check + SKILL.md 同步 ([23324ae](https://github.com/longsizhuo/openInvest/commit/23324ae9a19b600695ae6b1796275ae79267a23e))
* open-pot 月度补充模型（wealth_context.monthly_contribution_cny） ([#81](https://github.com/longsizhuo/openInvest/issues/81)) ([57898f3](https://github.com/longsizhuo/openInvest/commit/57898f37ad41e8f6b600fb246ec217536eae7d32))
* **skill:** run.sh 远端模式适配 ([86eeac2](https://github.com/longsizhuo/openInvest/commit/86eeac297b038feff26c10ce5d52ea5bb14518c3))
* 自动定投 + 子弹池现金语义（DCA / dip-reserve）+ 修数据深度 bug ([#78](https://github.com/longsizhuo/openInvest/issues/78)) ([e07076e](https://github.com/longsizhuo/openInvest/commit/e07076ea455dede38b81bb72385cd197fbd860e7))


### Bug Fixes

* **self-host:** 默认值加固 — INVEST_HOME 统一 ~/openInvest + PnL 署名按 remote + 去硬编码路径 ([#74](https://github.com/longsizhuo/openInvest/issues/74)) ([f31899f](https://github.com/longsizhuo/openInvest/commit/f31899fe471efaa63ddc934a2fe8a54cbe103ab2))


### Refactor

* **committee:** core/committee.py 拆成 core/committee/ 包 + 薄壳 façade ([#59](https://github.com/longsizhuo/openInvest/issues/59)) ([90c323b](https://github.com/longsizhuo/openInvest/commit/90c323b5cc3f4db9a6e905d3da928ad48aa6c122))
* **skills:** 重组到 skills/ 父目录 + 完全重写 invest README ([1a89ad9](https://github.com/longsizhuo/openInvest/commit/1a89ad9c66763bb31a5b41c8972ffdc178600341))


### Docs

* fork user 提示（GUI beta / git pull / 多 LLM provider 配置 / 常见问题） ([8ac4159](https://github.com/longsizhuo/openInvest/commit/8ac41591349b2d4623493a5fbf3a007517db1900))
* **skill:** coordinator 指引对齐 v0.6——确定性事实块粘贴义务 ([dbf8878](https://github.com/longsizhuo/openInvest/commit/dbf8878d6cc6a71e399c546abf3a3ec44df6e9a2))
* **skill:** 远端模式（hub-and-spoke）使用说明 ([24cfb45](https://github.com/longsizhuo/openInvest/commit/24cfb45f182a43db4bec1af57a16bdd653fef7fc))

## [0.12.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.11.0...invest-skill-v0.12.0) (2026-06-24)


### Features

* open-pot 月度补充模型（wealth_context.monthly_contribution_cny） ([#81](https://github.com/longsizhuo/openInvest/issues/81)) ([a295f80](https://github.com/longsizhuo/openInvest/commit/a295f803c0e4519f706fb514c56ca0352ecff059))
* 自动定投 + 子弹池现金语义（DCA / dip-reserve）+ 修数据深度 bug ([#78](https://github.com/longsizhuo/openInvest/issues/78)) ([a94c912](https://github.com/longsizhuo/openInvest/commit/a94c912ae8d1326a8900ee2c290a2db4ad235e9f))

## [0.11.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.10.0...invest-skill-v0.11.0) (2026-06-20)


### Features

* **config:** 运行时 config-via-API + 集中度 lens 开关（ADR-017） ([#71](https://github.com/longsizhuo/openInvest/issues/71)) ([7f812b6](https://github.com/longsizhuo/openInvest/commit/7f812b62685a12fa7f918317d15830f506a8e4f0))


### Bug Fixes

* **self-host:** 默认值加固 — INVEST_HOME 统一 ~/openInvest + PnL 署名按 remote + 去硬编码路径 ([#74](https://github.com/longsizhuo/openInvest/issues/74)) ([930c0e4](https://github.com/longsizhuo/openInvest/commit/930c0e4ba1bb1b83c0c67915671e1c4170fa11cf))


### Refactor

* **committee:** core/committee.py 拆成 core/committee/ 包 + 薄壳 façade ([#59](https://github.com/longsizhuo/openInvest/issues/59)) ([124c67f](https://github.com/longsizhuo/openInvest/commit/124c67f3b06d333f98ef76dbc8e2cc131b8a21b9))

## [0.10.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.9.0...invest-skill-v0.10.0) (2026-06-16)


### Features

* **skill:** run.sh 远端模式适配 ([c4e31eb](https://github.com/longsizhuo/openInvest/commit/c4e31eba1baa101c758d7a0b3c5761296c944b71))


### Refactor

* **committee:** core/committee.py 拆成 core/committee/ 包 + 薄壳 façade ([#59](https://github.com/longsizhuo/openInvest/issues/59)) ([124c67f](https://github.com/longsizhuo/openInvest/commit/124c67f3b06d333f98ef76dbc8e2cc131b8a21b9))


### Docs

* **skill:** coordinator 指引对齐 v0.6——确定性事实块粘贴义务 ([2a690d0](https://github.com/longsizhuo/openInvest/commit/2a690d088ff932887a291b1a4e1f771da36cc993))
* **skill:** 远端模式（hub-and-spoke）使用说明 ([bd92abc](https://github.com/longsizhuo/openInvest/commit/bd92abc91439497f6913133a181cd1fc59e92fc0))

## [0.10.0](https://github.com/longsizhuo/openInvest/compare/invest-skill-v0.9.0...invest-skill-v0.10.0) (2026-06-14)


### Features

* **skill:** run.sh 远端模式适配 ([c4e31eb](https://github.com/longsizhuo/openInvest/commit/c4e31eba1baa101c758d7a0b3c5761296c944b71))


### Docs

* **skill:** coordinator 指引对齐 v0.6——确定性事实块粘贴义务 ([2a690d0](https://github.com/longsizhuo/openInvest/commit/2a690d088ff932887a291b1a4e1f771da36cc993))
* **skill:** 远端模式（hub-and-spoke）使用说明 ([bd92abc](https://github.com/longsizhuo/openInvest/commit/bd92abc91439497f6913133a181cd1fc59e92fc0))
