"""一次性迁移脚本：user_profile.json -> memory/*.md (OpenClaw 风格)

把单一 JSON 拆成 4 类 markdown + 2 类 jsonl/json：
- memory/user.md           身份 + 偏好（user 类，永久）
- memory/strategy.md       投资策略（strategy 类，永久）
- memory/portfolio.md      当前持仓（state 类，高频更新）
- memory/MEMORY.md         索引（仿 Claude memory 的 INDEX）
- memory/portfolio_history.jsonl  交易流水（append-only）
- memory/.state/processed_emails.json  已处理邮件 ID

迁移完成后保留 user_profile.json.bak 作为兜底。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

# 把项目根加进 path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.memory_store import MemoryStore  # noqa: E402

PROFILE_PATH = ROOT / "user_profile.json"


def main():
    if not PROFILE_PATH.exists():
        print(f"❌ {PROFILE_PATH} 不存在，无需迁移")
        return

    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        profile = json.load(f)

    store = MemoryStore()

    # --- 1. user.md  身份和偏好 ---
    user_data = {
        "display_name": profile.get("name", "Anonymous"),
        "risk_tolerance": profile.get("risk_tolerance", "Balanced"),
        "monthly_income_cny": profile.get("monthly_income_cny", 0),
        "monthly_expenses_cny": profile.get("monthly_expenses_cny", 0),
        "exchange_buffer_cny": profile.get("exchange_buffer_cny", 0),
        "last_payday": profile.get("last_run_date", "1970-01-01"),
    }
    user_body = f"""# 用户画像

- **姓名**: {user_data['display_name']}
- **风险偏好**: {user_data['risk_tolerance']}
- **月收入 (CNY)**: ¥{user_data['monthly_income_cny']:,}
- **月支出 (CNY)**: ¥{user_data['monthly_expenses_cny']:,}
- **换汇周转金 (CNY)**: ¥{user_data['exchange_buffer_cny']:,}
- **上次发薪日**: {user_data['last_payday']}

## 备注

风险偏好用于 manager agent 决策时的仓位上限：
- Conservative: 单次最多 30% 可投资金
- Balanced: 单次最多 60%
- Aggressive: 单次最多 100%
"""
    store.write("user", "user", user_data, user_body)
    print(f"✓ memory/user.md 已写入")

    # --- 2. strategy.md  投资策略 ---
    strat = profile.get("investment_strategy", {})
    strategy_data = {
        "target_asset": strat.get("target_asset", "NDQ.AX"),
        "target_allocation_stock": strat.get("target_allocation_stock", 0.7),
        "target_allocation_cash": strat.get("target_allocation_cash", 0.3),
        "max_single_invest_cny": strat.get("max_single_invest_cny", 10000),
    }
    strategy_body = f"""# 投资策略

- **目标资产**: `{strategy_data['target_asset']}`
- **股票仓位目标**: {strategy_data['target_allocation_stock']:.0%}
- **现金仓位目标**: {strategy_data['target_allocation_cash']:.0%}
- **单次入场上限 (CNY)**: ¥{strategy_data['max_single_invest_cny']:,}

## 决策约束

1. 即使现金充足，单次投入也不得超过上限（防梭哈）
2. 当 macro_score < 0 时强制降低仓位至 10%-20%
3. 当 RSI(14) >= 60 或价格分位 >= 70% 时禁止买入
"""
    store.write("strategy", "strategy", strategy_data, strategy_body)
    print(f"✓ memory/strategy.md 已写入")

    # --- 3. portfolio.md  当前持仓 ---
    assets = profile.get("current_assets", {})
    portfolio_data = {
        "cash_cny": assets.get("cash_cny", 0.0),
        "aud_cash": assets.get("aud_cash", 0.0),
        "ndq_shares": assets.get("ndq_shares", 0.0),
    }
    portfolio_body = f"""# 当前持仓

- **CNY 现金**: ¥{portfolio_data['cash_cny']:,.2f}
- **AUD 现金**: ${portfolio_data['aud_cash']:,.2f}
- **NDQ.AX 持仓**: {portfolio_data['ndq_shares']} 股

## 说明

此文件由 daily_report / commsec_sync / payday_check 三个 job 自动更新。
不要手动编辑——如需调整，请走 jobs/manual_adjust.py。
"""
    store.write("portfolio", "state", portfolio_data, portfolio_body)
    print(f"✓ memory/portfolio.md 已写入")

    # --- 4. portfolio_history.jsonl ---
    history = profile.get("transaction_history", [])
    for trade in history:
        store.append_history(trade)
    print(f"✓ memory/portfolio_history.jsonl 已迁移 {len(history)} 条交易")

    # --- 5. .state/processed_emails.json ---
    processed = profile.get("processed_emails", [])
    store.state_set("processed_emails", processed)
    print(f"✓ memory/.state/processed_emails.json 已迁移 {len(processed)} 条邮件 ID")

    # --- 6. MEMORY.md  索引 ---
    index_body = """# Memory Index

仿 OpenClaw 的 memory 索引文件。每条一行，格式：`- [Title](file.md) — 一句话说明`。

## 永久 (permanent)

- [用户画像](user.md) — 姓名、风险偏好、月薪、月支出
- [投资策略](strategy.md) — 目标资产、仓位、单次上限

## 状态 (state) — 自动更新

- [当前持仓](portfolio.md) — 现金 + 股票（每次交易后由 commsec_sync / daily_report 更新）

## 日志 (log)

- `portfolio_history.jsonl` — 交易流水（append-only）
- `daily/YYYY-MM-DD.md` — 每日 agent 决策与市场快照
- `.dreams/events.jsonl` — Dreaming 三阶段审计日志

## 长期洞察 (insight) — 由 Dreaming 写入

- `insights/*.md` — 每个一条 Deep Sleep 通过阈值门的洞察
- `DREAMS.md` — 人类可读的叙事性梦日记
"""
    (store.root / "MEMORY.md").write_text(index_body, encoding="utf-8")
    print(f"✓ memory/MEMORY.md 已写入")

    # --- 7. 备份原文件 ---
    bak = PROFILE_PATH.with_suffix(".json.bak")
    shutil.copy2(PROFILE_PATH, bak)
    print(f"✓ user_profile.json -> {bak.name}（备份保留）")

    print("\n🎉 迁移完成。下一步：删 user_profile.json 后改 portfolio_manager.py")


if __name__ == "__main__":
    main()
