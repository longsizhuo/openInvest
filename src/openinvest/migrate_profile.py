"""一次性迁移脚本：user_profile.json -> memory/*.md (OpenClaw 风格)

把单一 JSON 拆成 4 类 markdown + 2 类 jsonl/json：
- memory/user.md           身份 + 偏好（user 类，永久）
- memory/strategy.md       投资策略（strategy 类，永久）
- memory/portfolio.md      当前持仓（state 类，高频更新）
- memory/MEMORY.md         索引（仿 Claude memory 的 INDEX）
- memory/portfolio_history.jsonl  交易流水（append-only）
- memory/.state/processed_emails.json  已处理邮件 ID

迁移完成后保留 user_profile.json.bak 作为兜底。

**只应该跑一次**。2026-07-08 事故：这个脚本被直接重跑了一次（绕开 cmd_init），
没有任何保护，无条件把 user.md/strategy.md/portfolio.md 覆盖成
user_profile.json 里的旧/demo 数据，daily_report 因 target_assets 变空
每天早退、邮件全断。现在加同款 safety guard（对齐
skill_cmds/lifecycle_cmds.py:_write_v2_portfolio 在 2026-05-10 那次事故后
补的模式）：目标文件已存在 → 拒绝并要求显式 force=True，覆盖前先备份。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from openinvest.core.memory_store import MemoryStore
from openinvest.paths import INVEST_ROOT

ROOT = INVEST_ROOT
PROFILE_PATH = ROOT / "user_profile.json"
_GUARDED_DOCS = ("user", "strategy", "portfolio")


def _refuse_if_already_migrated(store: MemoryStore, force: bool) -> None:
    """这个脚本只该跑一次。目标文件任一已存在 = 之前跑过（真实数据或此前的
    迁移结果），无条件覆盖等于重演 2026-07-08 那次数据丢失。"""
    existing = [name for name in _GUARDED_DOCS if store.read(name) is not None]
    if not existing:
        return
    if force:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        for name in existing:
            src = store.root / f"{name}.md"
            if src.exists():
                backup = store.root / f"{name}.md.bak.{ts}"
                backup.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                print(f"  已备份 {name}.md -> {backup.name}")
        return
    raise RuntimeError(
        f"⚠️ {', '.join(f'{n}.md' for n in existing)} 已存在，拒绝覆盖。"
        f"这个脚本只该跑一次（2026-07-08 事故：直接重跑过一次，把真实数据覆盖成"
        f" user_profile.json 里的旧数据）。确认要重跑请传 force=True"
        f"（CLI: `python -m openinvest.migrate_profile --force`），会先自动备份现有文件。"
    )


def main(force: bool = False):
    if not PROFILE_PATH.exists():
        print(f"❌ {PROFILE_PATH} 不存在，无需迁移")
        return

    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        profile = json.load(f)

    store = MemoryStore()
    _refuse_if_already_migrated(store, force)

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
    # B7: target_asset 默认值不再硬编码 NDQ.AX —— 让 fork 用户在 onboarding
    # 或之后自己用 GUI/strategy_dialog 配。空字符串走 doctor 引导补全流程。
    strategy_data = {
        "target_asset": strat.get("target_asset", ""),
        "target_allocation_stock": strat.get("target_allocation_stock", 0.7),
        "target_allocation_cash": strat.get("target_allocation_cash", 0.3),
        "max_single_invest_cny": strat.get("max_single_invest_cny", 10000),
    }
    target_display = strategy_data["target_asset"] or "(未配置 — 请在 GUI 策略页或 strategy.md 里加 target_assets)"
    strategy_body = f"""# 投资策略

- **目标资产**: `{target_display}`
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
    # B7: 不再硬编码 NDQ.AX 持仓行——让 body 只列实际有数据的字段，
    # fork 用户填 0 就不在 body 里出现"NDQ.AX 持仓 0 股"误导（下一步会被
    # migrate_portfolio_to_holdings.py 转成 v2 cash dict + holdings list）。
    assets = profile.get("current_assets", {})
    portfolio_data = {
        "cash_cny": assets.get("cash_cny", 0.0),
        "aud_cash": assets.get("aud_cash", 0.0),
        "ndq_shares": assets.get("ndq_shares", 0.0),
    }
    body_lines = ["# 当前持仓", ""]
    body_lines.append(f"- **CNY 现金**: ¥{portfolio_data['cash_cny']:,.2f}")
    if portfolio_data["aud_cash"] > 0:
        body_lines.append(f"- **AUD 现金**: ${portfolio_data['aud_cash']:,.2f}")
    if portfolio_data["ndq_shares"] > 0:
        body_lines.append(f"- **NDQ.AX 持仓**: {portfolio_data['ndq_shares']} 股")
    body_lines.append("")
    body_lines.append("## 说明")
    body_lines.append("")
    body_lines.append("此文件由 daily_report / commsec_sync / payday_check 三个 job 自动更新。")
    body_lines.append("通过 GUI / NapCat 命令调整，不要手动编辑 frontmatter。")
    portfolio_body = "\n".join(body_lines) + "\n"
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
    main(force="--force" in sys.argv)
