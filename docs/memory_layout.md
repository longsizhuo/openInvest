# Memory 目录布局 (OpenClaw-style)

仿 [OpenClaw 2026.4.9](https://github.com/openclaw/openclaw) 的 Markdown + frontmatter 持久化设计。
Memory 整个目录 git-ignored（含个人资产、工资、交易历史），新部署需自己跑迁移脚本生成。

## 目录结构

```
memory/
├── MEMORY.md                  # 索引文件（人类 + agent 都读）
├── DREAMS.md                  # Dreaming 写出的叙事性梦日记（P3 后启用）
├── user.md                    # 用户身份 / 风险偏好 / 月薪月支
├── strategy.md                # 投资策略 / target_assets 数组
├── portfolio.md               # 当前持仓（state，高频更新）
├── portfolio_history.jsonl    # 交易流水 (append-only)
├── insights/                  # Deep Sleep 通过阈值门的长期洞察
│   └── *.md
├── daily/                     # 每日 append-only 日志
│   └── YYYY-MM-DD.md
├── .dreams/                   # Dreaming 子系统私有
│   ├── short-term-recall.json # Light Sleep 摄入信号
│   ├── candidates.json        # REM Sleep 候选模式
│   └── events.jsonl           # 三阶段审计日志
└── .state/                    # 简单 KV (已处理邮件 ID 等)
    └── processed_emails.json
```

## 文件格式

每个 `*.md` 都是 frontmatter + body：

```markdown
---
name: portfolio
type: state
updated: 2026-04-27T18:03:15+08:00
cash_cny: 50000
aud_cash: 1000.0
ndq_shares: 50.0
gold_grams: 124.0
gold_avg_cost_cny_per_gram: 1008.79
---

# 当前持仓

- CNY 现金: ¥50,000
...
```

- **frontmatter**：结构化数据的 source of truth（代码读写）
- **body**：自然语言版本（agent 直接看，每次写入由模板重新渲染）

## 类型分类

| type | 含义 | 更新频率 | 例子 |
|------|------|---------|------|
| `user` | 用户身份与偏好 | 几乎不变 | `user.md` |
| `strategy` | 投资策略配置 | 偶尔（NapCat 命令调） | `strategy.md` |
| `state` | 当前状态 | 高频（每次交易后） | `portfolio.md` |
| `log` | 日志 | append-only | `daily/*.md`, `*.jsonl` |
| `insight` | 长期洞察 | Deep Sleep 写入 | `insights/*.md` |

## 初始化

```bash
# 从旧 user_profile.json 迁移（兼容 v0.1 用户）
python scripts/migrate_profile.py

# 升级单资产 → 多资产 (NDQ.AX + GC=F)
python scripts/upgrade_to_multi_asset.py

# 导入实际黄金交易历史（按需，给原作者用的）
python scripts/import_gold_trades.py
```

## 并发安全

`core.memory_store.MemoryStore` 用 `fcntl.LOCK_EX` 文件锁保证：
- 同进程多线程（agent ThreadPool）安全
- 跨进程（scheduler runner + napcat_bot 同时跑）也安全

## Dreaming 整合

`jobs/dreaming.py` 每天 03:00 跑三阶段（实际实现见 `jobs/dreaming.py`）：

1. **Light Sleep** — 读 `memory/portfolio_history.jsonl` 最近 90 天交易（`LOOKBACK_DAYS=90`），结合多 symbol 的 2y 行情上下文（`CONTEXT_SYMBOLS`）提取信号 + 各 window 事后收益 → `.dreams/short-term-recall.json`
2. **REM Sleep** — 找跨时间重复模式，输出 `.dreams/candidates.json`
3. **Deep Sleep** — 阈值门 `score≥0.8 / count≥3` 通过的 → 写 `insights/*.md` + 更新 `MEMORY.md` 索引

> 后续如果改为消费 `daily/*.md` 或调整 LOOKBACK_DAYS / 阈值门，请同步更新本节，避免文档与实现脱节。

详见 [OpenClaw Dreaming Guide](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e)。
