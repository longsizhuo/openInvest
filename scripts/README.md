# scripts/

CLI 脚本入口 + 维护工具。**不在生产 cron 里跑**（生产 cron 走 `src/openinvest/jobs/`）。

三层结构：根目录 = 活跃工具；`research/` = 离线研究/验证；`archive/` = 已退役/一次性，仅存档。

> ⚠️ 根目录这些模块的 import 路径被 tests/ 和 ci.yml smoke import 锁死
> （`from scripts.<name> import ...`），移动前先改测试。

## 根目录（活跃）

| 脚本 | 用途 | 频率 |
|------|------|------|
| `skill.py` | 兼容 shim → `openinvest.cli`（旧版 plugin run.sh 还按这个路径进来） | 按需 |
| `migrate_profile.py` | 兼容 shim → `openinvest.migrate_profile` | 按需 |
| `migrate_portfolio_to_holdings.py` | v1 → v2 portfolio.md 迁移（幂等，fork 用户用；测试锁死） | 一次 |
| `import_commsec.py` | CommSec 交易手动导入（`jobs/commsec_sync` IMAP 失败时的兜底） | 按需 |
| `backfill_history.py` | 深历史价格回填 MarketStore（商品期货 inception 差异处理） | 按需 |
| `export_accuracy.py` | verdict_review.jsonl → 脱敏 `docs/accuracy_summary.json` | 按需 |
| `snapshot.py` | memory/db/委员会历史打包快照与恢复（hub 迁移/备份） | 按需 |
| `refresh_benchmarks.py` | 拉取/缓存外部 benchmark NAV | 周度候选 |
| `check_benchmark_freshness.py` | 硬编码 benchmark 收益率过期告警（cron 友好退出码） | cron 候选 |
| `backtest_committee.py` | 历史日期跑委员会，工具全部截 as-of-D 防 look-ahead | 研究用 |
| `backtest_runner.py` | `backtest_committee` 的 workspace 隔离壳（monkeypatch 数据路径） | 研究用 |
| `run_walk_forward.py` | walk-forward paper trading 主入口（PaperTradeSimulator + 基准曲线） | 研究用 |
| `holdout_validate.py` | Optuna 调参结果 holdout 窗验证（防过拟合） | 研究用 |
| `rl_train.py` | Optuna 贝叶斯搜索 regime/committee 参数 | 研究用 |

回测链 `backtest_committee ← backtest_runner ← holdout_validate`、`run_walk_forward ← holdout_validate / rl_train` 有交叉 import，整链留在根目录。

## research/（离线研究 / 验证，standalone）

| 脚本 | 用途 |
|------|------|
| `full_validation.py` | 开卷下限验证：2024-2026 周度重跑真委员会（8-12h，可断点续跑） |
| `sweep_runner.py` | 参数 sweep（arithmetic 纯函数 / pnl 全 walk-forward 两模式），见 wiki/14 |
| `backtest_eval.py` | 已落盘 verdict → 真实 P&L 曲线（CR/AR/Sharpe/MaxDD vs buy-and-hold） |
| `fit_path_calibration.py` | 路径分布校准参数拟合（λ/γ，fit/OOS 分窗），见 wiki/15 |
| `validate_gold_defense.py` | 高 VIX 是否该对黄金 BUY 单独设闸的 fit/OOS 验证 |
| `eval_event_stance.py` | EVENT_STANCE 加权聚合 vs 纯计数的前瞻收益检验 |
| `analyze_news_attribution.py` | miss verdict 根因打标（决定 event-RAG 值不值得开） |
| `audit_convention_diff.py` (+csv) | VIX/价格分位窗口口径差异审计（对冻结 transcript 复算） |
| `build_dspy_trainset_v2.py` | DSPy trainset v2（yfinance + oracle labeling，5565 样本） |
| `build_dspy_trainset_v3.py` | v2 输出重打标（path_c oracle）。⚠️ ADR-022 记录已知 bug：reward 把 cash 锚在 0 |
| `rl_optimize_prompts_v2.py` | MIPROv2 prompt 训练器（配 trainset_v2） |

## archive/（已退役 / 一次性，仅存档不再跑）

| 脚本 | 退役原因 |
|------|------|
| `init_market_db.py` | 已被 yfinance 通用路径取代 |
| `upgrade_to_multi_asset.py` | B7 起仅原作者 fork 用，fork 用户勿跑 |
| `backfill_ohlcv.py` | High/Low/Volume schema 扩列回填，已完成 |
| `backfill_pnl_history.py` | pnl_history.jsonl 一次性回填，已完成 |
| `import_gold_trades.py` | 浙商黄金交易一次性导入（缺私有文件时回落合成 demo 数据） |
| `tune_defense_thresholds.py` | #113 防御参数 scale-independent 化后目标参数已删，仅留方法论参考 |
| `build_dspy_trainset.py` | v1，被 v2 取代（66 样本、symbol 打码等 6 项结构缺陷，从未接入 production） |
| `rl_optimize_prompts.py` | v1（BootstrapFewShot），被 v2 取代，留作历史参考 |

## 与其他目录的关系

- 上游：plugin `run.sh` 调 `scripts/skill.py`（shim）；研究脚本手动跑
- 下游：调用 `openinvest.*` 业务层、`db/` 行情库
