"""A4：同输入多次抽样的裁决方差（研究脚本，不进生产路径）

对最近 --limit 条 live 裁决（memory/.committee/<date>/<sym>.md）各重跑 --n 次 Direct 委员会
（= `run_committee_session(symbols=[sym])`，与 `run.sh run_committee SYM --force` 同一条路），
记录每次 verdict / confidence / alloc，统计：
- 方向翻转率：同一条裁决的 n 次重跑里方向类（up=BUY/ACCUMULATE、down=SELL/TRIM、flat=HOLD）不全相同的比例；
  另给两两翻转率与 verdict 标签翻转率；
- 置信度标准差：每条裁决 n 次的样本标准差，再取均值 / 中位数；
- 金额变异系数：每条裁决 |alloc| 的 样本标准差 / 均值（均值为 0 的不算，单列条数）。

⚠️ 重跑用的是**今天**的行情/宏观，不是原决议日的输入——所以这里测的是"同一输入下的抽样噪声"，
   不是"原决议能否复现"。最近 30 条裁决通常只有几个不同标的 ⇒ 实际是 k 个标的各被重复抽样，
   不是 30 个独立情形；summary 里同时给"按标的汇总"，样本量以 distinct_symbols 为准。

⚠️ 会写 INVEST_HOME 下的 memory/.committee、.dreams、db/（同日重跑把旧 transcript 归档、行情库补新 bar）。
   必须把 INVEST_HOME 指向**拷贝**：脚本拒绝 git checkout，且要求拷贝里有哨兵文件 `.variance_copy_ok`
   （或显式传 --i-know-this-is-a-copy）——PyPI 形态的生产数据目录没有 .git，光靠 checkout 检查拦不住。

⚠️ 逐条明细（真实账户逐日裁决 + 金额）只写 INVEST_HOME/memory/.dreams/verdict_variance/（gitignored 的数据目录），
   脚本拒绝把它写进仓库。可提交的只有匿名汇总 summary_<日期>.json（标的 A/B/C/D、无日期、无金额、无逐条明细）。

用法（key 只从 env 读、不打印；拷贝里放 memory/*.md + memory/.committee + db/market_data.db）：
    export $(grep -E '^(DEEPSEEK_API_KEY|LLM_API_KEY|LLM_BASE_URL|LLM_MODEL)=' <prod>/.env | xargs)
    touch /tmp/xxx/home/.variance_copy_ok
    INVEST_HOME=/tmp/xxx/home uv run python experiments/verdict_variance/run_variance.py --dry-run
    INVEST_HOME=/tmp/xxx/home uv run python experiments/verdict_variance/run_variance.py --n 3 --limit 30
重启同一条命令会跳过明细文件里已完成的 (date, symbol, rep)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

# 与 review_calc.EXPECTED_DIRECTION 同一张表；这里不 import，保证本文件的纯统计部分零依赖可单测
DIRECTION = {"BUY": "up", "ACCUMULATE": "up", "SELL": "down", "TRIM": "down", "HOLD": "flat"}
REPO = Path(__file__).resolve().parents[2]
SENTINEL = ".variance_copy_ok"


def direction_of(verdict: str) -> str:
    return DIRECTION.get(str(verdict).upper(), "none")      # UNCLEAR 等 → "none"


# ---------------------------------------------------------------- 纯统计（单测钉死）

def _wilson(k: int, n: int, z: float = 1.96) -> Optional[List[float]]:
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def _r(x: Optional[float], nd: int = 4) -> Optional[float]:
    return None if x is None else round(x, nd)


def item_stats(runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """一条裁决的 n 次重跑 → 翻转 / 标准差 / CV。有效重跑 <2 → None（算不出方差）。"""
    ok = [r for r in runs if not r.get("error")]
    if len(ok) < 2:
        return None
    dirs = [direction_of(r["verdict"]) for r in ok]
    pairs = list(combinations(dirs, 2))
    allocs = [abs(float(r.get("alloc") or 0.0)) for r in ok]
    mean_alloc = statistics.fmean(allocs)
    return {
        "n_runs": len(ok),
        "direction_flip": len(set(dirs)) > 1,
        "pair_flip_rate": sum(a != b for a, b in pairs) / len(pairs),
        "label_flip": len({str(r["verdict"]).upper() for r in ok}) > 1,
        "conf_std": statistics.stdev([float(r["confidence"]) for r in ok]),
        "alloc_cv": statistics.stdev(allocs) / mean_alloc if mean_alloc > 0 else None,
    }


def summarize(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """items: [{date, symbol, orig:{verdict,...}, runs:[{verdict,confidence,alloc,error?}]}] → 汇总。"""
    per = [(it, item_stats(it["runs"])) for it in items]
    per = [(it, s) for it, s in per if s]
    n = len(per)
    cvs = [s["alloc_cv"] for _, s in per if s["alloc_cv"] is not None]
    stds = [s["conf_std"] for _, s in per]
    flips = sum(s["direction_flip"] for _, s in per)
    # 原决议 vs 重跑：混了"输入从决议日换成今天"的效应，只当参照
    vs_orig = [direction_of(r["verdict"]) != direction_of(it["orig"]["verdict"])
               for it, _ in per for r in it["runs"] if not r.get("error")]
    by_sym: Dict[str, Any] = {}
    for sym in sorted({it["symbol"] for it, _ in per}):
        rs = [r for it, _ in per if it["symbol"] == sym for r in it["runs"] if not r.get("error")]
        dirs = [direction_of(r["verdict"]) for r in rs]
        pairs = list(combinations(dirs, 2))
        confs = [float(r["confidence"]) for r in rs]
        by_sym[sym] = {
            "n_items": sum(1 for it, _ in per if it["symbol"] == sym), "n_runs": len(rs),
            "verdict_counts": {v: sum(1 for r in rs if str(r["verdict"]).upper() == v)
                               for v in sorted({str(r["verdict"]).upper() for r in rs})},
            "pair_flip_rate": _r(sum(a != b for a, b in pairs) / len(pairs)) if pairs else None,
            "conf_mean": _r(statistics.fmean(confs)), "conf_std": _r(statistics.stdev(confs)) if len(confs) > 1 else None,
        }
    return {
        "n_items": n,
        "distinct_symbols": len(by_sym),
        "n_runs": sum(s["n_runs"] for _, s in per),
        "direction_flip_rate": _r(flips / n, 3) if n else None,
        "direction_flip_ci95": _wilson(flips, n),
        "pair_flip_rate": _r(statistics.fmean(s["pair_flip_rate"] for _, s in per), 3) if n else None,
        "label_flip_rate": _r(sum(s["label_flip"] for _, s in per) / n, 3) if n else None,
        "conf_std_mean": _r(statistics.fmean(stds)) if n else None,
        "conf_std_median": _r(statistics.median(stds)) if n else None,
        "alloc_cv_mean": _r(statistics.fmean(cvs)) if cvs else None,
        "alloc_cv_median": _r(statistics.median(cvs)) if cvs else None,
        "alloc_cv_n_items": len(cvs),
        "alloc_all_zero_items": n - len(cvs),
        "rerun_vs_orig_direction_diff_rate": _r(sum(vs_orig) / len(vs_orig), 3) if vs_orig else None,
        "by_symbol": by_sym,
    }


def anonymize_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    """可公开版：by_symbol 的标的换成 A/B/C/D（按重跑数降序）。summarize() 本身只在 by_symbol 里带标的，
    不含日期 / 金额 / 逐条明细，所以改键即可。"""
    by = summary.get("by_symbol", {})
    order = sorted(by, key=lambda s: (-by[s]["n_runs"], s))
    return {**{k: v for k, v in summary.items() if k != "by_symbol"},
            "by_symbol": {chr(ord("A") + i): by[s] for i, s in enumerate(order)}}


def public_summary(items: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    return {"meta": {k: meta[k] for k in ("n", "limit", "max_rounds") if k in meta},
            "summary": anonymize_summary(summarize(items))}


# ---------------------------------------------------------------- 取数 / 跑

def recent_live_items(home: Path, limit: int) -> List[Dict[str, Any]]:
    """memory/.committee/<YYYY-MM-DD>/<sym>.md 里最近 limit 条（日期降序、symbol 升序）。"""
    from openinvest.core.decision_ledger import parse_committee_file
    from openinvest.utils.symbols import safe_symbol
    from openinvest.core.portfolio_manager import PortfolioManager
    by_safe = {safe_symbol(a["symbol"]): a["symbol"]
               for a in PortfolioManager().strategy.get("target_assets", []) if a.get("symbol")}
    items: List[Dict[str, Any]] = []
    days = sorted((d for d in (home / "memory" / ".committee").iterdir()
                   if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)), reverse=True)
    for d in days:
        for md in sorted(d.glob("*.md")):
            p = parse_committee_file(md)
            sym = (p or {}).get("symbol") or by_safe.get(md.stem)
            if not p or not sym:
                continue
            items.append({"date": d.name, "symbol": sym,
                          "orig": {"verdict": p["verdict"], "confidence": p["confidence"],
                                   "alloc": p.get("alloc_cny")}})
            if len(items) >= limit:
                return items
    return items


def run_once(symbol: str, max_rounds: int) -> Dict[str, Any]:
    from openinvest.core.committee_runner import run_committee_session
    try:
        res = run_committee_session(symbols=[symbol], max_debate_rounds=max_rounds,
                                    progress_callback=None)["asset_committees"].get(symbol, {})
    except Exception as e:  # noqa: BLE001  单次失败只记录，不中断整轮
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    if "error" in res:
        return {"error": str(res["error"])[:120]}
    v = res.get("verdict") or {}
    return {"verdict": str(v.get("verdict", "UNCLEAR")).upper(), "confidence": float(v.get("confidence") or 0.0),
            "alloc": float(v.get("alloc_cny") or 0.0), "original_verdict": v.get("_original_verdict")}


def guard_home(raw: str, acknowledged: bool = False) -> Path:
    """拒绝在没显式设 INVEST_HOME、指向 git checkout、或没声明"这是拷贝"的目录上跑——会写 .committee / .dreams / db。
    声明 = 目录里有哨兵文件 .variance_copy_ok，或显式传 --i-know-this-is-a-copy（PyPI 形态的生产数据目录
    没有 .git / pyproject.toml，只靠 checkout 检查拦不住）。"""
    raw = (raw or "").strip()
    if not raw:
        sys.exit("拒绝运行：必须显式设 INVEST_HOME 指向数据拷贝（本脚本会写 memory/ 与 db/）。")
    home = Path(raw).expanduser().resolve()
    if (home / ".git").exists() or (home / "pyproject.toml").exists():
        sys.exit(f"拒绝运行：INVEST_HOME={home} 像是代码 checkout（生产？）。请用数据拷贝。")
    if not (acknowledged or (home / SENTINEL).exists()):
        sys.exit(f"拒绝运行：{home} 没有哨兵文件 {SENTINEL}，也没传 --i-know-this-is-a-copy。"
                 "确认它是数据拷贝（不是生产数据目录）后再来。")
    return home


def guard_out(out: Path) -> Path:
    """逐条明细是真实账户的逐日裁决 + 金额，不许写进仓库（拒绝落在 REPO 下）。"""
    out = out.expanduser().resolve()
    if out == REPO or REPO in out.parents:
        sys.exit(f"拒绝运行：明细输出 {out} 在仓库内。逐条明细含真实账户逐日裁决和金额，只能写数据目录；"
                 "要提交请用匿名汇总 --summary-out。")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=3, help="每条裁决重跑次数（默认 3）")
    ap.add_argument("--limit", type=int, default=30, help="取最近多少条 live 裁决（默认 30）")
    ap.add_argument("--max-rounds", type=int, default=1, help="辩论轮数，默认 1（= Direct CLI 默认）")
    ap.add_argument("--workers", type=int, default=4, help="并发（按标的分组，同标的串行）")
    ap.add_argument("--out", type=Path, help="逐条明细 JSON（默认 INVEST_HOME/memory/.dreams/verdict_variance/variance_<日期>.json；不许在仓库内）")
    ap.add_argument("--summary-out", type=Path, help="匿名汇总 JSON（默认与明细同目录 summary_<日期>.json；可提交）")
    ap.add_argument("--i-know-this-is-a-copy", action="store_true", help="没有哨兵文件时，显式声明 INVEST_HOME 是数据拷贝")
    ap.add_argument("--dry-run", action="store_true", help="只列计划与统计自检，不调 LLM")
    a = ap.parse_args()

    home = guard_home(os.getenv("INVEST_HOME", ""), a.i_know_this_is_a_copy)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    a.out = guard_out(a.out or home / "memory" / ".dreams" / "verdict_variance" / f"variance_{day}.json")
    a.summary_out = a.summary_out or a.out.with_name(f"summary_{day}.json")
    items = recent_live_items(home, a.limit)
    syms = sorted({it["symbol"] for it in items})
    print(f"取到 {len(items)} 条最近 live 裁决，{len(syms)} 个不同标的 {syms}；计划重跑 {len(items) * a.n} 次委员会"
          f"（≈¥{len(items) * a.n * 0.0067:.1f}）", file=sys.stderr)
    if a.dry_run:
        demo = [{"date": "d", "symbol": "X", "orig": {"verdict": "HOLD"},
                 "runs": [{"verdict": "HOLD", "confidence": 0.6, "alloc": 0},
                          {"verdict": "ACCUMULATE", "confidence": 0.7, "alloc": 1000},
                          {"verdict": "ACCUMULATE", "confidence": 0.8, "alloc": 3000}]}]
        s = summarize(demo)
        assert s["direction_flip_rate"] == 1.0 and abs(s["conf_std_mean"] - 0.1) < 1e-9, s
        print("dry-run：统计自检通过，未调用 LLM。", file=sys.stderr)
        return
    if not (os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")):
        sys.exit("LLM_API_KEY / DEEPSEEK_API_KEY 未设（Direct 路径必须有 key）。")

    done: Dict[tuple, Dict[str, Any]] = {}
    if a.out.exists():
        for it in json.loads(a.out.read_text(encoding="utf-8")).get("items", []):
            for i, r in enumerate(it["runs"]):
                if not r.get("error"):
                    done[(it["date"], it["symbol"], i)] = r
    for it in items:
        it["runs"] = [done.get((it["date"], it["symbol"], i)) or {"error": "pending"} for i in range(a.n)]

    lock = Lock()

    def dump() -> None:
        meta = {"n": a.n, "limit": a.limit, "max_rounds": a.max_rounds}
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps({
            "meta": {**meta, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "note": "重跑用运行当天的输入；见 README 局限"},
            "summary": summarize(items), "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
        a.summary_out.parent.mkdir(parents=True, exist_ok=True)
        a.summary_out.write_text(json.dumps(public_summary(items, meta), ensure_ascii=False, indent=1), encoding="utf-8")

    def work(sym: str) -> None:
        for it in (i for i in items if i["symbol"] == sym):
            for k in range(a.n):
                if it["runs"][k].get("error") != "pending":
                    continue
                r = run_once(sym, a.max_rounds)
                with lock:
                    it["runs"][k] = r
                    dump()
                print(f"  {it['date']} {sym} #{k + 1}: {r.get('verdict') or r.get('error')}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max(1, min(a.workers, len(syms)))) as pool:
        list(pool.map(work, syms))
    dump()
    print(f"明细 → {a.out}\n匿名汇总 → {a.summary_out}", file=sys.stderr)
    print(json.dumps(public_summary(items, {"n": a.n, "limit": a.limit, "max_rounds": a.max_rounds}),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
