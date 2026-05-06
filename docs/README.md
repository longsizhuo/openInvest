# docs/

项目文档与自动生成的可视化产物。**人类阅读 + 仓库展示** 用。

## 内容

- `memory_layout.md` — `memory/` 目录的数据布局说明（portfolio.md / strategy.md / user.md / daily/ 各自职责与 frontmatter schema）
- `pnl_chart.svg` — `jobs/pnl_snapshot` 工作日每 2h 自动重渲染的 PnL 趋势图（vs 8 基准）；只含百分比，不暴露绝对金额
- `verdict_accuracy.md` — `jobs/verdict_review` 月度生成的委员会决策命中率报告（含真实金额，**git ignored**）
- `DDGS.README.md` — DuckDuckGo Search 库 (`ddgs`) 的二方备注

## 与其他目录的关系

- 上游：`jobs/pnl_snapshot.py` `jobs/verdict_review.py` 写入文件
- 下游：`connectors/web_api.py:/api/pnl_chart.svg` 把 svg serve 给前端展示
- README 引用 `docs/pnl_chart.svg` 作为项目首屏截图
