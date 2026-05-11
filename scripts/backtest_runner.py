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


def _warmup_market_data(symbols: list) -> None:
    """**重要**：跑 backtest 前预热 yfinance → DB，让 cutoff 过滤后还有足够历史数据
    可算 RSI/ATR 等技术指标。

    问题背景：阶段 1 防穿越让 backtest 模式跳过 yfinance；如果 DB 里没历史，
    cutoff 过滤后只剩极少数据，LLM 看到"RSI 缺失/最大回撤 0"等空数据 →
    Macro/Quant/Risk 全输出 neutral → LLM 只能 HOLD → backtest 全是 0% 收益。

    解法：先用 yfinance 把 2 年历史灌入共享 DB（market_data.db），backtest
    跑时按 as_of_date 过滤即可。yfinance 给的是"截至现在的 2 年"数据，
    backtest 不会"穿越"——cutoff 过滤会截断到 decision_date 之前。
    """
    # 用 10y：yfinance 默认 2y 只回到 today-2y，无法 backtest 早于此的日期。
    # 10y 给所有 symbol 都覆盖回 2016+，配合 _apply_cutoff 安全过滤。
    print("🔥 预热 market_data：拉 10y 历史给 backtest 用...")
    import yfinance as yf
    from db.market_store import MarketStore
    store = MarketStore()
    macro_symbols = ["^VIX", "^TNX", "USDCNY=X", "AUDCNY=X"]
    all_syms = list(set(symbols + macro_symbols))
    for sym in all_syms:
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(period="10y")
            if df.empty:
                print(f"  ⚠ {sym}: yfinance 返回空")
                continue
            for idx, row in df.iterrows():
                store.save_generic_price(sym, idx.strftime("%Y-%m-%d"), row["Close"])
            print(f"  ✓ {sym}: {len(df)} 行入库")
        except Exception as e:
            print(f"  ❌ {sym}: {e}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE),
                        help=f"backtest 数据目录，默认 {DEFAULT_WORKSPACE}")
    parser.add_argument("--reset", action="store_true",
                        help="清空 workspace 后重新 seed（小心！会丢失已跑的 backtest 产物）")
    parser.add_argument("--skip-warmup", action="store_true",
                        help="跳过 yfinance 预热（DB 已有历史时用）")
    args, rest = parser.parse_known_args()

    workspace = Path(args.workspace)
    if args.reset and workspace.exists():
        print(f"⚠️ --reset：清空 {workspace}")
        shutil.rmtree(workspace)

    # 1. monkey-patch 数据路径
    _setup_isolated_paths(workspace)

    # 2. seed 初始持仓
    _seed_workspace_memory(workspace)

    # 3. 预热 market_data DB —— 解决 LLM "数据缺失" 全 HOLD 问题
    if not args.skip_warmup:
        # 从 rest 里解析 --assets，确定预热哪些 symbol
        import argparse as _ap
        bt_parser = _ap.ArgumentParser(add_help=False)
        bt_parser.add_argument("--assets", default="NDQ.AX,GC=F")
        bt_args, _ = bt_parser.parse_known_args(rest)
        asset_list = [s.strip() for s in bt_args.assets.split(",") if s.strip()]
        _warmup_market_data(asset_list)

    # 4. 转给 backtest_committee.main()
    sys.argv = ["backtest_committee"] + rest
    from scripts.backtest_committee import main as _bt_main
    _bt_main()

    print(f"\n📁 backtest 产物在 {workspace}/memory/.backtest/")


if __name__ == "__main__":
    main()
