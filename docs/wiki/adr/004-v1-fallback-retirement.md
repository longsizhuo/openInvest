---
type: adr
title: "ADR-004: v1 Portfolio Fallback 正式退场"
tags: [portfolio, migration, v2, schema, refactor]
intent: 记录 v1 portfolio 兼容 fallback 正式删除的决策
schema_source:
  - core/schemas.py:PortfolioData
  - core/portfolio_manager.py:PortfolioManager
documents:
  endpoints: []
  config_keys: []
  symbols: [PortfolioData, PortfolioManager, _ensure_v2_inplace, with_portfolio_tx]
status: accepted
date: 2026-05-10
supersedes: []
superseded_by: []
---

# ADR-004: v1 Portfolio Fallback 正式退场

**状态**: 已接受  
**日期**: 2026-05-10  
**作者**: Dev-Core Sprint 2

---

## 背景

openInvest 在 2026-05-06 的 B7 重构中把 `portfolio.md` 从"扁平字段"（v1）迁移到"cash dict + holdings list"（v2）。重构当时为了零宕机升级，在三处保留了 read-time fallback：

1. `PortfolioManager.cash` property — 检测 `cash` 字段为空时，从 `cash_cny`/`aud_cash` 反推
2. `PortfolioManager.holdings` property — 检测 `holdings` 字段为空时，从 `ndq_shares`/`gold_grams` 反推
3. `_ensure_v2_inplace()` — `with_portfolio_tx()` 入口处把 v1 数据 in-place 转 v2

这些 fallback 在迁移期内是必要的，但在迁移脚本（`scripts/migrate_portfolio_to_holdings.py`）稳定运行后，继续保留它们带来了以下问题：

- **代码噪音**：读路径有双重分支（v2 fresh → v1 fallback），读者无法直观判断当前生产数据是哪个格式
- **测试困难**：测试 fixture 可以用 v1 格式躲过 schema 校验，导致 test_web_api.py 的 `_seed_memory` 长期用 `cash_cny` 而非 `cash` dict，掩盖了 schema 校验的实际覆盖范围
- **schema 滑坡**：新加的 `PortfolioData` Pydantic 模型要求 `schema_version=2`，但 fallback 让 v1 数据绕过了写门口校验

## 决策

在 2026-05-10，经确认生产实例已完成 v1→v2 迁移（`schema_version=2`），正式删除全部 v1 fallback：

### 删除内容

| 位置 | 删除代码 |
|------|----------|
| `core/portfolio_manager.py` `cash` property | v1 从 `cash_cny`/`aud_cash` 反推的 8 行 |
| `core/portfolio_manager.py` `holdings` property | v1 从 `ndq_shares`/`gold_grams` 反推的 25 行 |
| `core/portfolio_manager.py` `_ensure_v2_inplace()` | v1→v2 in-place 转换逻辑，函数保留为 `pass` 空壳（调用位置暂不动）|

### 保留内容

- `with_portfolio_tx()` 退出时清除 v1 旧字段名的逻辑（`cash_cny`, `aud_cash` 等）——防止被手工写坏的文件在 tx commit 时带入脏字段
- `scripts/migrate_portfolio_to_holdings.py` 迁移脚本本身——它是数据层工具，继续保留供历史记录和 fork 用户使用

## 时间线

| 日期 | 事件 |
|------|------|
| 2026-05-06 | B7 重构引入 v2 数据模型，保留 v1 fallback 兼容 |
| 2026-05-06 | `scripts/migrate_portfolio_to_holdings.py` 编写，实现幂等 v1→v2 迁移 |
| 2026-05-10 | 生产数据确认 v2，v1 fallback 正式删除（本 ADR） |
| 2026-05-10 | `tests/test_web_api.py` 的 `_seed_memory` fixture 升级为 v2 格式 |
| 2026-05-10 | 新增 `tests/test_portfolio_manager.py`，502 行的 PM 代码终于有独立测试 |

## 受影响用户

**仍在使用 v1 格式 `portfolio.md` 的 fork 用户**（`schema_version` 字段不存在或为 1）：

升级步骤：

```bash
# 1. 备份当前 portfolio.md
cp memory/portfolio.md memory/portfolio.md.bak

# 2. 跑迁移脚本（幂等，可多次执行）
python -m scripts.migrate_portfolio_to_holdings

# 3. 验证结果
python -m scripts.skill doctor
```

迁移脚本会：
- 自动备份原文件为 `portfolio.md.bak.<timestamp>`
- 把 `cash_cny`→`cash.CNY`、`aud_cash`→`cash.AUD`
- 把 `ndq_shares`→`holdings[NDQ.AX].units`、`gold_grams`→`holdings[GC=F].units`
- 设置 `schema_version: 2`
- 校验后原子写入（失败自动 rollback）

## 后续

- `_ensure_v2_inplace()` 函数目前留为空 `pass`，下一次清理 sprint 中彻底删除函数和调用位置
- `with_portfolio_tx()` 退出时的 v1 字段清除逻辑可在确认无遗留用户后一并删除
