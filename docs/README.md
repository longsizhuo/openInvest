---
type: readme
title: "docs/"
tags: [documentation, index, wiki, pnl, 文档]
intent: 文档目录索引
documents:
  endpoints:
    - "GET /api/pnl_chart.svg"
  config_keys: []
  symbols: []
---

# docs/

项目文档与自动生成的可视化产物。**人类阅读 + 仓库展示** 用。

## 主要文档

- **[wiki/](wiki/README.md)** — 完整文档站（11 页 + 3 ADR），按读者画像分路径
  - 用户：[QUICK_START](QUICK_START.md) → [09-troubleshooting](wiki/09-troubleshooting.md)
  - 开发者：[01-architecture](wiki/01-architecture.md) → [05-data-model](wiki/05-data-model.md) → [07-extending](wiki/07-extending.md)
  - 研究者：[02-agents](wiki/02-agents.md) → [03-dreaming](wiki/03-dreaming.md) → [04-execution-paths](wiki/04-execution-paths.md) → [adr/](wiki/adr/)
  - 设计师 / 前端：[10-design-system](wiki/10-design-system.md)

- **[QUICK_START.md](QUICK_START.md)** — 30 分钟 fork 上手（5 步 + 6 个 troubleshooting）

- **[memory_layout.md](memory_layout.md)** — `memory/` 目录的 v2 数据布局说明（portfolio / strategy / user / daily / committee 各自职责与 frontmatter schema）

## 自动生成产物

- **`pnl_chart.svg`** — `jobs/pnl_snapshot` 工作日每 2h 自动重渲染的 PnL 趋势图（vs 8 基准）；只含百分比，不暴露绝对金额
- **`verdict_accuracy.md`** — `jobs/verdict_review` 月度生成的委员会决策命中率报告（含真实金额，**git ignored**）

## 二方备注

- **`DDGS.README.md`** — DuckDuckGo Search 库 (`ddgs`) 的使用说明

## 与其他目录的关系

- 上游：`jobs/pnl_snapshot.py` / `jobs/verdict_review.py` 写入自动产物
- 下游：`connectors/web_api.py:/api/pnl_chart.svg` 把 svg serve 给前端展示
- 主 [README.md](../README.md) 引用 `docs/pnl_chart.svg` 作为项目首屏截图
