"""一次性升级脚本：strategy.md → 多资产 / portfolio.md 加 gold_grams

执行后生效：
- strategy.md: target_asset (单值) → target_assets (list, 含 NDQ.AX + GC=F)
- portfolio.md: 新增 gold_grams 字段（默认 0，等用户在 NapCat 报实际持仓）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.memory_store import MemoryStore  # noqa: E402


def main():
    store = MemoryStore()

    # --- strategy.md 升级 ---
    strat_doc = store.read("strategy")
    if strat_doc is None:
        print("❌ memory/strategy.md 不存在，先跑 migrate_profile.py")
        return

    target_assets = [
        {
            "symbol": "NDQ.AX",
            "display_name": "BetaShares Nasdaq 100 ETF",
            "market": "AU",
            "type": "equity_etf",
            "currency": "AUD",
            "channel": "CommSec",
            "max_single_invest_cny": 10000,
            "note": "AUD 子弹已用尽，重点观察突破回调",
        },
        {
            "symbol": "GC=F",
            "display_name": "伦敦金 (浙商积存金)",
            "market": "spot",
            "type": "metal",
            "currency": "CNY",
            "channel": "浙商银行积存金",
            "max_single_invest_cny": 5000,
            "price_offset_pct": 0.015,  # auto 推断后会被 NapCat 命令覆盖
            "note": "CNY 直接买克，offset 由用户报浙商克价后自动推断",
        },
    ]
    new_strategy_data = {
        "target_assets": target_assets,
        "target_allocation_stock": strat_doc.get("target_allocation_stock", 0.7),
        "target_allocation_cash": strat_doc.get("target_allocation_cash", 0.3),
    }
    new_strategy_body = """# 投资策略 (多资产)

## 目标资产清单

### 1. NDQ.AX — BetaShares Nasdaq 100 ETF
- **渠道**: CommSec (AUD)
- **单次入场上限 (CNY)**: ¥10,000
- **状态**: AUD 子弹已用尽，重点观察突破回调
- **数据源**: yfinance `NDQ.AX`

### 2. 伦敦金 (浙商积存金)
- **渠道**: 浙商银行积存金（CNY 直接买克）
- **单次入场上限 (CNY)**: ¥5,000
- **数据源**: yfinance `GC=F`（COMEX 期货）+ `USDCNY=X`
- **点差**: 默认 1.5%，由 NapCat 报"今天浙商 X 元/克"后自动反推更新

## 决策约束

1. 单次投入不得超过该资产 `max_single_invest_cny`
2. 当 `macro_score < 0` 时强制降低仓位至 10%-20%
3. 股票类：RSI(14) >= 60 或价格分位 >= 70% 时禁止买入
4. 黄金类：避险逻辑反向 — VIX > 25 / 美元走弱 时倾向加仓
"""
    store.write("strategy", "strategy", new_strategy_data, new_strategy_body)
    print("✓ memory/strategy.md 已升级为多资产")

    # --- portfolio.md 升级（加 gold_grams 字段） ---
    port_doc = store.read("portfolio")
    if port_doc is None:
        print("❌ memory/portfolio.md 不存在")
        return

    cash_cny = float(port_doc.get("cash_cny", 0))
    aud_cash = float(port_doc.get("aud_cash", 0))
    ndq_shares = float(port_doc.get("ndq_shares", 0))
    gold_grams = float(port_doc.get("gold_grams", 0))  # 新增字段，默认 0

    new_portfolio_data = {
        "cash_cny": cash_cny,
        "aud_cash": aud_cash,
        "ndq_shares": ndq_shares,
        "gold_grams": gold_grams,
    }
    new_portfolio_body = f"""# 当前持仓

## 现金
- **CNY 现金**: ¥{cash_cny:,.2f}
- **AUD 现金**: ${aud_cash:,.2f}

## 持仓
- **NDQ.AX**: {ndq_shares} 股
- **黄金 (浙商积存金)**: {gold_grams} 克

## 说明

此文件由 daily_report / commsec_sync / payday_check / napcat_bot 四方更新。
- 黄金持仓需通过 NapCat 私聊命令 `/gold_set 12.5` 设置（用户主动报）
- 其余通过自动化流程更新
"""
    store.write("portfolio", "state", new_portfolio_data, new_portfolio_body)
    print("✓ memory/portfolio.md 已加 gold_grams 字段")

    # --- 更新 MEMORY.md 索引 ---
    index_path = store.root / "MEMORY.md"
    index_body = """# Memory Index

仿 OpenClaw 的 memory 索引文件。每条一行，格式：`- [Title](file.md) — 一句话说明`。

## 永久 (permanent)

- [用户画像](user.md) — 姓名、风险偏好、月薪、月支出
- [投资策略](strategy.md) — 多资产清单（NDQ.AX + 伦敦金）、仓位、单次上限

## 状态 (state) — 自动更新

- [当前持仓](portfolio.md) — CNY/AUD 现金 + NDQ 股数 + 黄金克数

## 日志 (log)

- `portfolio_history.jsonl` — 交易流水（append-only）
- `daily/YYYY-MM-DD.md` — 每日 agent 决策与市场快照
- `.dreams/events.jsonl` — Dreaming 三阶段审计日志

## 长期洞察 (insight) — 由 Dreaming 写入

- `insights/*.md` — Deep Sleep 通过阈值门的洞察
- `DREAMS.md` — 人类可读的叙事性梦日记
"""
    index_path.write_text(index_body, encoding="utf-8")
    print("✓ memory/MEMORY.md 索引已更新")


if __name__ == "__main__":
    main()
