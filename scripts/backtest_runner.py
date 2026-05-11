"""backtest_runner.py — backtest workspace 隔离 wrapper

包装 scripts.backtest_committee，在 import 真实 invest 模块**之前** monkey-patch
数据路径，让 backtest 写入独立目录（不污染真实 memory/）。

跟 ~/investdemo/demo_server.py 同思路：通过运行时 patch 实现"代码同源、数据隔离"。

用法:
    python -m scripts.backtest_runner --workspace /tmp/bt --start 2024-01-01 \\
        --end 2024-03-31 --assets NDQ.AX,GC=F,510300.SS,AAPL

workspace 目录结构:
    /tmp/bt/
    ├── memory/                  ← MEMORY_ROOT 指向这
    │   ├── user.md              ← 必须 seed 一份，否则 PortfolioManager 报错
    │   ├── strategy.md
    │   ├── portfolio.md
    │   └── .backtest/<date>/<sym>.md   ← backtest verdict 产物
    └── db/
        ├── trades.db            ← TradesDB.DB_PATH 指向这
        └── insights.db          ← InsightsDB.DEFAULT_DB_PATH 指向这

真实 ~/projects-review/invest/memory/ 完全不动。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_MEMORY = REPO_ROOT / "memory"

# 默认 workspace 在仓库外，避免污染
DEFAULT_WORKSPACE = REPO_ROOT.parent / ".backtest_workspace"


def _setup_isolated_paths(workspace: Path) -> None:
    """在 import invest 之前 patch 数据路径常量。

    顺序很重要：每个模块的 path 常量在 import 时被读取一次，必须先 import 再
    赋值（而不是 set env var 之类）。
    """
    workspace = workspace.resolve()
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "db").mkdir(parents=True, exist_ok=True)

    # 安全检查：禁止 workspace 指向真实 memory/
    if (workspace / "memory").resolve() == REAL_MEMORY.resolve():
        raise SystemExit(
            f"❌ workspace 不能指向真实 memory/，会污染你的持仓数据。"
            f"传一个临时路径，如 --workspace /tmp/bt 或 ~/backtest_workspace。"
        )

    # 1. core.memory_store.MEMORY_ROOT
    import core.memory_store as _ms
    _ms.MEMORY_ROOT = workspace / "memory"

    # 2. db.trades_db.DB_PATH
    import db.trades_db as _trades
    _trades.DB_PATH = str(workspace / "db" / "trades.db")

    # 3. db.insights_db.DEFAULT_DB_PATH
    import db.insights_db as _insights
    _insights.DEFAULT_DB_PATH = str(workspace / "db" / "insights.db")

    # 注：market_data.db (yfinance 行情缓存) 故意**共享**真实 db/ —— 行情缓存
    # 无所谓污染，且阶段 1 的 as_of_date 过滤已经防止 backtest 读到 T+ 数据。
    # 这能让 backtest 跑得快得多（已有 yfinance 缓存可直接用）

    print(f"✓ workspace 隔离已激活")
    print(f"  MEMORY_ROOT: {_ms.MEMORY_ROOT}")
    print(f"  trades DB:   {_trades.DB_PATH}")
    print(f"  insights DB: {_insights.DEFAULT_DB_PATH}")
    print(f"  market_data: <共享真实 db/market_data.db，as_of_date 保证安全>")


def _seed_workspace_memory(workspace: Path) -> None:
    """如果 workspace 还没 seed，从真实 memory/ 拷一份基本结构。

    只拷 user.md / strategy.md / portfolio.md（不拷历史、insights、流水）。
    portfolio.md 改成"标准持仓"作为 baseline 起点（所有 backtest 共享同样
    起始持仓，可比性更好）。
    """
    workspace_memory = workspace / "memory"

    if (workspace_memory / "user.md").exists():
        return  # 已 seed，不动

    print(f"📦 首次启动，seed workspace memory ←  {REAL_MEMORY}")

    for fname in ["user.md", "strategy.md"]:
        src = REAL_MEMORY / fname
        if src.exists():
            shutil.copy2(src, workspace_memory / fname)
            print(f"  ✓ {fname}")

    # portfolio.md 用"标准起点"：现金 ¥100,000 / 无持仓
    # 这样 backtest 是从纯现金开始，每次 BUY/SELL 都是真实的资金动作
    standard_portfolio = """---
schema_version: 2
cash:
  CNY: 100000.00
  AUD: 0.00
  USD: 0.00
holdings: []
name: portfolio
type: state
updated: '2024-01-01T00:00:00+00:00'
---

# Backtest 起始持仓（标准 baseline）

现金 ¥100,000，无持仓。每次回测从这个起点开始 walk-forward。
"""
    (workspace_memory / "portfolio.md").write_text(standard_portfolio, encoding="utf-8")
    print(f"  ✓ portfolio.md (¥100k baseline)")

    # MEMORY.md 索引（防 MemoryStore 报错）
    (workspace_memory / "MEMORY.md").write_text(
        "# Backtest Memory Index\n\nworkspace 自动 seed。\n",
        encoding="utf-8",
    )
    # portfolio_history 空文件
    (workspace_memory / "portfolio_history.jsonl").write_text("", encoding="utf-8")
    (workspace_memory / ".state").mkdir(exist_ok=True)
    (workspace_memory / ".state" / "processed_emails.json").write_text("[]", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE),
                        help=f"backtest 数据目录，默认 {DEFAULT_WORKSPACE}")
    parser.add_argument("--reset", action="store_true",
                        help="清空 workspace 后重新 seed（小心！会丢失已跑的 backtest 产物）")
    args, rest = parser.parse_known_args()

    workspace = Path(args.workspace)
    if args.reset and workspace.exists():
        print(f"⚠️ --reset：清空 {workspace}")
        shutil.rmtree(workspace)

    # 1. monkey-patch 数据路径
    _setup_isolated_paths(workspace)

    # 2. seed 初始持仓
    _seed_workspace_memory(workspace)

    # 3. 转给 backtest_committee.main()
    sys.argv = ["backtest_committee"] + rest
    from scripts.backtest_committee import main as _bt_main
    _bt_main()

    print(f"\n📁 backtest 产物在 {workspace}/memory/.backtest/")


if __name__ == "__main__":
    main()
