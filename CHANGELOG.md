# Changelog

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
