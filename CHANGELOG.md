# Changelog

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
