# Changelog

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
