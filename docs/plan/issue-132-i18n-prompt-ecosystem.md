# Issue #132 计划：I18N Prompt / Ecosystem

## Summary

目标文档：`docs/plan/issue-132-i18n-prompt-ecosystem.md`

本计划分两条主线一起解决 issue #132：
1. 给委员会 prompt、skill 指引和对外文案建立统一的 i18n 机制，避免默认强制中文、混合输出和多入口漂移。
2. 补一套“全球用户可用性”验证链路，覆盖海外地区运行、新闻抓取、symbol/query 召回和语言回退行为，先验证再放量扩展。

默认先做一个可落地的 v1：支持 `zh-CN` 和 `en` 两种语言，语言选择显式传递到 prompt/render/news-query 层；其他语言先回退到英文，不做自动机翻。

## Key Changes

### 1. 建立统一 Locale 契约
- 增加单一可信源的 locale 配置，优先级定为：显式请求参数 > 用户配置 > 环境变量 > 默认 `zh-CN`。
- 新增轻量 locale 解析层，统一规范值：`zh-CN`、`en`，未知值回退 `en`。
- 所有委员会入口统一接收并传递 locale：Coordinator、Direct、Web API、CLI/skill、daily/event 相关生成路径都走同一参数链，不允许某一路径私自默认中文。

### 2. Prompt 体系改造成可本地化资源
- 保留现有 `capabilities/<capability>/<role>/` 结构，但把 prompt 文件改成按 locale 分层读取；建议规则：
  - 默认文件保留为兼容壳或迁移后由 loader 统一分发。
  - 新增本地化资源命名规范，例如 `quant.zh-CN.md`、`quant.en.md`，round2 同理。
- `capabilities/loader.py` 扩展为按 `role + round + locale` 解析，查找顺序固定：
  1. `<role>.<locale>.md`
  2. `<role>.<language>.md`
  3. `<role>.en.md`
  4. 兼容旧 `<role>.md`
- 现有所有“必须中文回复”指令改成 locale 驱动文案，不在业务逻辑里硬编码中文输出要求。
- 输出格式、字段名、可解析结构保持不变；只本地化自然语言说明，不改机器依赖字段，如 `VERDICT`、`SIGNAL`、`KEY_DATA` 等。

### 3. Skill / CLI / Web API 语言行为对齐
- `skills/invest`、`invest-setup`、CLI 帮助文案、必要的 Web API prompt 预览接口统一接 locale。
- 首次 onboarding 增加语言偏好落盘；已有用户无该字段时按默认值处理。
- `prepare_committee`、prompt preview、任何暴露“系统 prompt 全文”的接口都返回当前 locale 对应文本，避免前端或 agent 看到中文 prompt 后被带偏。
- README/用户文档层只补最小说明：支持 `zh-CN`/`en`、如何配置 locale、未知 locale 的回退规则。

### 4. 全球信息源与查询策略验证
- 对 `jobs/event_watch.py` 和 `services/news_sources/ddgs_news.py` 做 locale/region 显式化设计，不再隐式只靠 `wt-wt` + `<symbol> news`。
- query 生成拆成可测试策略：
  - symbol query
  - 英文 canonical asset query
  - 必要的别名/地区 query
  - 宏观 query
- v1 不追求“按语言抓所有本地新闻”，重点保证全球用户至少能稳定拿到英文全球财经覆盖。
- 明确把 issue 正文中提到的 “bug like #83” 当作待核实历史引用，不把当前仓库里的现有 `#83` 当成实现依据；计划里只落“补回归样例和复现脚本”。

### 5. 迁移与兼容
- 老用户不配 locale 也能继续跑；默认行为与当前中文用户尽量兼容。
- 旧 prompt 文件在迁移窗口内保留兼容读取，避免一次性重命名打断现有路径。
- 所有新增 locale 逻辑都集中在 loader/config/entry 编排层，避免角色 prompt builder 各自实现一套。

## Test Plan

- Prompt loader
  - `zh-CN` 能命中中文模板。
  - `en` 能命中英文模板。
  - 未知 locale 回退英文。
  - 缺少某 round 的 locale 文件时按约定回退，不报错。
- Committee paths
  - Coordinator、Direct、Web API 三条路径对同一 locale 生成同语言 prompt。
  - 同一资产在 `zh-CN` 与 `en` 下，结构化字段稳定，只自然语言正文变化。
- Regression
  - 现有默认中文用户不传 locale 时仍输出中文。
  - 现有 transcript 保存、verdict 解析、review 流程不因 prompt 本地化失效。
- News / ecosystem
  - query builder 对美股、澳股、黄金、宏观标签至少各有一个海外样例。
  - DDGS/yfinance/RSS 在英文 locale 下能返回非空结果或明确降级，不出现静默失败。
  - 增加一组“全球用户 smoke cases”，验证英文用户从 onboarding 到 `run_committee` 的最短链路。
- Docs / UX
  - 配置说明、技能说明、README 至少覆盖 locale 设置与回退行为。
  - prompt preview 或调试接口能让开发者确认当前实际使用的是哪种 locale 资源。

## Assumptions

- 计划文件使用中文撰写，但实现目标是中英双语运行。
- v1 只承诺 `zh-CN` 和 `en`；其他语言统一回退英文。
- v1 不做运行时机器翻译，所有核心 prompt 采用人工维护的双语模板。
- 结构化输出字段继续维持英文枚举和值，避免影响解析器和历史账本。
- `docs/plan` 目录目前不存在，实施时需一并创建。
- 若后续确认用户更希望默认英文而不是默认中文，只需调整 locale 默认值，不改变整体方案结构。
