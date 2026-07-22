"""paper_fleet — 前瞻纸面委员会舰队：唯一不受模型升级影响的干净样本源（ADR-022 更新节）

为什么存在
==========
干净样本 = 决策时点晚于模型训练截止的委员会运行。真实账本 ~1 条/天攒不动
（3650 条要十年）；回填桶又是"相对模型"的——升级到含新语料的模型就整桶变脏。
前瞻纸面运行没有这两个问题：**决策时未来尚不存在，任何未来模型都不可能记得**。
每天对 universe.yml 里 N 个 symbol 跑 Direct 委员会（确定性 CLI，符合"unattended
一律 Direct"），verdict_review cron 在 30/90d 后自动用真实后市回填评分。
50 symbol/天 → 73 天攒满 3650 条干净样本。

隔离
====
一切都发生在独立的 INVEST_HOME（默认 ~/openInvest-fleet）——决策/账本/事件全部
与真实组合分离，不污染 decisions/discipline 统计。⚠️ 与 ADR-022 §6 同款 caveat：
舰队 home 是中性 cash-only 组合，verdict 分布缺集中度维度，不可外推 live。

用法（在 openInvest 仓库根运行）
================================
    # 首次：初始化舰队 home + 注册全部标的（幂等，可重跑）
    INVEST_HOME=~/openInvest-fleet uv run python experiments/paper_fleet/run_fleet.py --bootstrap
    # 每日：全池跑一轮（--limit N 控制成本试跑）
    INVEST_HOME=~/openInvest-fleet uv run python experiments/paper_fleet/run_fleet.py

前置：$INVEST_HOME/.env 里配 DEEPSEEK_API_KEY（Direct 路径需要；不自动拷贝密钥，
自己放一份）。激活 crontab 见 README.md——merge 前不要挂 cron。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
UNIVERSE = HERE / "universe.yml"
FLEET_HOME = Path(os.environ.get("INVEST_HOME", "~/openInvest-fleet")).expanduser()

# 舰队 home 的中性画像（cash-only；金额是名义值，纸面运行不动真钱）
NEUTRAL_PROFILE = {
    "display_name": "PaperFleet",
    "risk_tolerance": "Balanced",
    "monthly_income_cny": 0,
    "monthly_expense_cny": 0,
    "exchange_buffer_cny": 0,
    "holdings_description": "100000 CNY cash only",
}
MAX_SINGLE_INVEST_CNY = 10000   # track_asset 新建必填的名义上限


def load_universe() -> list:
    import yaml
    data = yaml.safe_load(UNIVERSE.read_text(encoding="utf-8"))
    return list(data["symbols"])


def cli(*args: str, stdin: str | None = None, timeout: int = 600) -> dict:
    """调 openinvest CLI（同解释器 -m 方式，免 PATH 假设），stdout 解析成 JSON"""
    env = {**os.environ, "INVEST_HOME": str(FLEET_HOME)}
    proc = subprocess.run(
        [sys.executable, "-m", "openinvest.cli", *args],
        input=stdin, capture_output=True, text=True, timeout=timeout, env=env,
    )
    out = proc.stdout.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"status": "error", "raw": out[-500:], "stderr": proc.stderr[-500:],
                "returncode": proc.returncode}


def bootstrap() -> None:
    FLEET_HOME.mkdir(parents=True, exist_ok=True)
    doc = cli("doctor")
    if doc.get("status") == "needs_setup":
        print("init 舰队 home（中性 cash-only 画像）...")
        r = cli("init", "--from-stdin", stdin=json.dumps(NEUTRAL_PROFILE))
        print(f"  init → {r.get('status')}")
    else:
        print(f"doctor → {doc.get('status')}（跳过 init）")
    symbols = load_universe()
    ok = 0
    for sym in symbols:
        r = cli("track_asset", "--symbol", sym,
                "--max-single-invest-cny", str(MAX_SINGLE_INVEST_CNY))
        ok += 1 if r.get("status") != "error" else 0
    print(f"track_asset：{ok}/{len(symbols)} 注册（upsert 幂等）")
    if not (FLEET_HOME / ".env").exists():
        print(f"⚠️ {FLEET_HOME}/.env 不存在——放一份含 DEEPSEEK_API_KEY 的 .env 才能跑委员会")


def run_day(limit: int | None, sleep_s: float) -> None:
    symbols = load_universe()
    if limit:
        symbols = symbols[:limit]
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []
    for i, sym in enumerate(symbols, 1):
        t0 = time.time()
        r = cli("run_committee", sym)
        verdict = r.get("verdict") or r.get("final_verdict") or r.get("status")
        results.append({"symbol": sym, "verdict": verdict, "sec": round(time.time() - t0, 1)})
        print(f"[{i}/{len(symbols)}] {sym:10s} → {verdict} ({results[-1]['sec']}s)")
        time.sleep(sleep_s)
    # 每日一行 summary 追加到舰队 home（jsonl，方便后续统计样本积累速度）
    log_f = FLEET_HOME / "fleet_runs.jsonl"
    ok = sum(1 for x in results if x["verdict"] not in (None, "error"))
    with log_f.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"started": started, "total": len(results), "ok": ok,
                            "results": results}, ensure_ascii=False) + "\n")
    print(f"\n{ok}/{len(results)} 成功；日志追加 → {log_f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="前瞻纸面委员会舰队")
    ap.add_argument("--bootstrap", action="store_true", help="初始化舰队 home + 注册标的（幂等）")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个 symbol（试跑控成本）")
    ap.add_argument("--sleep", type=float, default=3.0, help="symbol 间隔秒数（限速）")
    args = ap.parse_args()
    print(f"INVEST_HOME = {FLEET_HOME}")
    if args.bootstrap:
        bootstrap()
    else:
        run_day(args.limit, args.sleep)


if __name__ == "__main__":
    main()
