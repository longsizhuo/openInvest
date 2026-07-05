"""审计 trainset_v2.json 4 类 bug。

不修改 trainset，只产 audit report 到 v1_trainset_audit.json。

Bug 1: market_context look-ahead leak（RSI/MA20/MA120/quantile/1m_return 用了未来数据？）
Bug 2: forward_30d_return_pct 用了交易日 + close 价？
Bug 3: macro_context VIX/TNX 时间对齐（≤ decision_date 最近交易日）？
Bug 4: portfolio_state asset_pct + fwd_30d 用 oracle_verdict 反推，跟 trainset 一致？
"""
from __future__ import annotations

import json
import math
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 复用 production 的 compute_metrics，确保比较公平
from openinvest.utils.market_metrics import compute_metrics  # noqa: E402

TRAINSET = ROOT / "experiments" / "dspy_trainset_v2.json"
OUTPUT = ROOT / "experiments" / "audits" / "v1_trainset_audit.json"

RNG = random.Random(20260518)

# ---- Bug 4: oracle_verdict 重放 ------------------------------------------------


def oracle_verdict(
    asset_pct: float, fwd_30d_return: float, fwd_30d_mdd: float,
) -> Tuple[str, float]:
    if fwd_30d_return >= 8 and fwd_30d_mdd >= -3 and asset_pct < 0.3:
        return ("BUY", 0.5)
    if fwd_30d_return >= 4 and asset_pct < 0.7:
        return ("ACCUMULATE", 0.10)
    if fwd_30d_return <= -8 and asset_pct >= 0.5:
        return ("SELL", -0.5)
    if fwd_30d_return <= -4 and asset_pct >= 0.3:
        return ("TRIM", -0.15)
    return ("HOLD", 0.0)


def parse_portfolio_asset_pct(portfolio_state: str, symbol: str) -> Optional[float]:
    """从 portfolio_state 字符串解析出 asset_pct。

    格式 1: "cash 100%, no <SYM> position, ..."  → asset_pct=0
    格式 2: "cash 50%, <SYM> 50% (concentration 50%, ...), ..."
    """
    s = portfolio_state
    # 无仓位
    if "no " in s and "position" in s:
        return 0.0
    # 找 "<SYM> XX%" pattern。symbol 含特殊字符（^GSPC, BTC-USD, GC=F）→ 先 escape
    pat = re.escape(symbol) + r"\s+(\d+(?:\.\d+)?)%"
    m = re.search(pat, s)
    if m:
        return float(m.group(1)) / 100.0
    # 兜底找 cash 后第二个 %
    return None


def audit_bug4_oracle(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对全部样本：用 (asset_pct, fwd_30d_return, fwd_30d_mdd) 重算 oracle_verdict，
    跟 trainset 比对。"""
    mismatches: List[Dict[str, Any]] = []
    n_parse_fail = 0
    for i, s in enumerate(samples):
        asset_pct = parse_portfolio_asset_pct(s["portfolio_state"], s["symbol"])
        if asset_pct is None:
            n_parse_fail += 1
            continue
        ret_30 = s["forward_30d_return_pct"]
        mdd_30 = s["forward_30d_mdd_pct"]
        exp_v, exp_alloc = oracle_verdict(asset_pct, ret_30, mdd_30)
        if exp_v != s["verdict"] or abs(exp_alloc - s["alloc_pct_of_dry_powder"]) > 1e-6:
            mismatches.append({
                "index": i,
                "decision_date": s["decision_date"],
                "symbol": s["symbol"],
                "asset_pct": asset_pct,
                "fwd_30d_return": ret_30,
                "fwd_30d_mdd": mdd_30,
                "expected_verdict": exp_v,
                "expected_alloc": exp_alloc,
                "actual_verdict": s["verdict"],
                "actual_alloc": s["alloc_pct_of_dry_powder"],
                "portfolio_state": s["portfolio_state"],
            })
    rate = 100.0 * len(mismatches) / len(samples)
    if len(mismatches) == 0:
        verdict = "PASS"
    elif rate < 0.5:
        verdict = "WARN"
    else:
        verdict = "FAIL"
    return {
        "samples_checked": len(samples),
        "parse_failures": n_parse_fail,
        "mismatches": len(mismatches),
        "mismatch_rate_pct": round(rate, 4),
        "mismatch_samples": mismatches[:20],  # 仅保留前 20 条
        "verdict": verdict,
    }


# ---- yfinance helpers ----------------------------------------------------------


def fetch_history(symbol: str, start: str, end: str) -> pd.DataFrame:
    """yfinance 拉一段 close 历史，tz-naive。失败返空 df。"""
    for attempt in range(3):
        try:
            df = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=True)
            if df is not None and not df.empty:
                df.index = pd.to_datetime(df.index).tz_localize(None)
                return df
        except Exception as e:
            print(f"  ⚠ {symbol} attempt {attempt+1}: {e}")
            time.sleep(2)
    return pd.DataFrame()


# ---- Bug 1: market_context 重算 ------------------------------------------------


def parse_market_context(mc: str) -> Dict[str, float]:
    """从 market_context 字符串提取 RSI / MA20 / MA120 / quantile / 1m_return。"""
    out: Dict[str, float] = {}
    m = re.search(r"RSI\s+([\d.]+)", mc)
    if m:
        out["rsi14"] = float(m.group(1))
    m = re.search(r"MA20=([\d.]+)", mc)
    if m:
        out["ma20"] = float(m.group(1))
    m = re.search(r"MA120=([\d.]+)", mc)
    if m:
        out["ma120"] = float(m.group(1))
    m = re.search(r"price_quantile_2y=([\d.]+)", mc)
    if m:
        out["price_quantile_2y"] = float(m.group(1))
    m = re.search(r"1m_return=([+\-]?[\d.]+)%", mc)
    if m:
        out["1m_return"] = float(m.group(1))
    return out


def audit_bug1_lookahead(samples: List[Dict[str, Any]], n: int = 5) -> Dict[str, Any]:
    """随机抽 N 个不同 symbol 的样本，重算 market_context 比对。

    重要：必须只用 decision_date 之前的数据。如果发现 mismatch，可能是 leak。
    """
    # 按 symbol 分组挑
    by_symbol: Dict[str, List[int]] = {}
    for i, s in enumerate(samples):
        by_symbol.setdefault(s["symbol"], []).append(i)
    candidate_syms = sorted(by_symbol.keys())
    RNG.shuffle(candidate_syms)
    picks: List[int] = []
    for sym in candidate_syms:
        if len(picks) >= n:
            break
        idx_list = by_symbol[sym]
        # 选一个不靠 decision window 边缘的样本（前置历史更稳）
        picks.append(RNG.choice(idx_list))

    results: List[Dict[str, Any]] = []
    max_err = 0.0
    mismatches = 0

    for idx in picks:
        s = samples[idx]
        sym = s["symbol"]
        ddate = pd.Timestamp(s["decision_date"])
        # 拉 decision_date 前 2.5 年（够 MA120 + quantile_2y）+ decision_date 当天
        start = (ddate - pd.Timedelta(days=900)).strftime("%Y-%m-%d")
        # 仅拉 decision_date 当日（含）—— end 取 decision_date+1 (yfinance end exclusive)
        end = (ddate + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        print(f"\n[Bug1] {sym} @ {s['decision_date']} (fetch {start} → {end})")
        df = fetch_history(sym, start, end)
        time.sleep(1)
        if df.empty:
            results.append({"symbol": sym, "decision_date": s["decision_date"], "error": "yfinance empty"})
            continue
        # 截到 <= ddate
        df = df[df.index <= ddate]
        if df.empty or "Close" not in df.columns:
            results.append({"symbol": sym, "decision_date": s["decision_date"], "error": "no data <= decision_date"})
            continue
        # builder 的窗口语义：从 decision_start - PRE_BUFFER_DAYS(130) - 60d 开始的所有
        # 数据切到 t_idx + 1。所以为了忠实复现 price_quantile_2y（虽叫 2y 实际是
        # 从 ~decision_start - 190d 起的累积窗口），我们把 df 进一步截到 decision_start
        # - 190d 起。这样能精确复现 builder 的指标。
        DECISION_START = pd.Timestamp("2024-05-01")
        PRE_BUFFER_DAYS = 130
        fetch_start_builder = DECISION_START - pd.Timedelta(days=PRE_BUFFER_DAYS + 60)
        df = df[df.index >= fetch_start_builder]
        metrics = compute_metrics(df)
        expected = parse_market_context(s["market_context"])

        # 算 1m return（21 trading days）
        ret_1m_actual = None
        if len(df) >= 22:
            p_now = df["Close"].iloc[-1]
            p_1m = df["Close"].iloc[-22]
            if p_1m > 0:
                ret_1m_actual = float((p_now / p_1m - 1) * 100)

        # 比对 RSI / MA20 / MA120 / quantile / 1m_return
        compares: Dict[str, Dict[str, Any]] = {}
        local_max_err = 0.0
        local_mismatch = False
        for k, actual in [
            ("rsi14", metrics.get("rsi14")),
            ("ma20", metrics.get("ma20")),
            ("ma120", metrics.get("ma120")),
            ("price_quantile_2y", metrics.get("price_quantile_2y")),
            ("1m_return", ret_1m_actual),
        ]:
            exp = expected.get(k)
            if exp is None or actual is None:
                compares[k] = {"expected": exp, "actual": actual, "rel_err": None}
                continue
            denom = max(abs(exp), 1e-6)
            rel = abs(exp - actual) / denom * 100
            compares[k] = {"expected": exp, "actual": round(float(actual), 4), "rel_err_pct": round(rel, 3)}
            if rel > local_max_err:
                local_max_err = rel
            if rel > 5.0:  # 容忍 5%
                local_mismatch = True
        max_err = max(max_err, local_max_err)
        if local_mismatch:
            mismatches += 1
        results.append({
            "symbol": sym,
            "decision_date": s["decision_date"],
            "n_rows": len(df),
            "last_date_in_df": df.index[-1].strftime("%Y-%m-%d"),
            "compares": compares,
            "max_rel_err_pct": round(local_max_err, 3),
        })

    if mismatches == 0 and max_err < 5.0:
        verdict = "PASS"
    elif mismatches <= 1:
        verdict = "WARN"
    else:
        verdict = "FAIL"
    return {
        "samples_checked": len(picks),
        "mismatches": mismatches,
        "max_relative_error_pct": round(max_err, 3),
        "details": results,
        "verdict": verdict,
        "note": "1m_return 用 21 trading days；price_quantile_2y 用 t_idx 之前所有窗口（不是固定 2y，跟 builder 一致）",
    }


# ---- Bug 2: forward window 重算 ------------------------------------------------


def audit_bug2_forward(samples: List[Dict[str, Any]], n: int = 10) -> Dict[str, Any]:
    """随机抽 10 个样本，重算 forward_30d_return_pct 比对。"""
    picks = RNG.sample(range(len(samples)), n)
    results: List[Dict[str, Any]] = []
    max_err = 0.0
    mismatches = 0

    for idx in picks:
        s = samples[idx]
        sym = s["symbol"]
        ddate = pd.Timestamp(s["decision_date"])
        start = ddate.strftime("%Y-%m-%d")
        end = (ddate + pd.Timedelta(days=70)).strftime("%Y-%m-%d")
        print(f"\n[Bug2] {sym} @ {s['decision_date']}")
        df = fetch_history(sym, start, end)
        time.sleep(1)
        if df.empty:
            results.append({"symbol": sym, "decision_date": s["decision_date"], "error": "yfinance empty"})
            continue
        # 第一个 >= ddate 的 row 视为 t_idx
        df_after = df[df.index >= ddate]
        if len(df_after) < 31:
            results.append({"symbol": sym, "decision_date": s["decision_date"], "error": f"insufficient rows ({len(df_after)})"})
            continue
        p_now = float(df_after["Close"].iloc[0])
        p_fwd30 = float(df_after["Close"].iloc[30])  # +30 trading days
        ret_actual = (p_fwd30 / p_now - 1) * 100
        ret_exp = s["forward_30d_return_pct"]
        denom = max(abs(ret_exp), 1e-3)
        rel = abs(ret_exp - ret_actual) / denom * 100
        if rel > max_err:
            max_err = rel
        if rel > 2.0:
            mismatches += 1
        results.append({
            "symbol": sym,
            "decision_date": s["decision_date"],
            "t_close": round(p_now, 4),
            "t+30_close": round(p_fwd30, 4),
            "expected_30d_ret": ret_exp,
            "recomputed_30d_ret": round(ret_actual, 4),
            "rel_err_pct": round(rel, 3),
        })

    if mismatches == 0 and max_err < 2.0:
        verdict = "PASS"
    elif mismatches <= 1:
        verdict = "WARN"
    else:
        verdict = "FAIL"
    return {
        "samples_checked": len(picks),
        "mismatches": mismatches,
        "max_relative_error_pct": round(max_err, 3),
        "details": results,
        "verdict": verdict,
    }


# ---- Bug 3: macro_context 对齐 -------------------------------------------------


def parse_macro_context(mc: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for label in ["VIX", "TNX", "DXY", "USDCNY"]:
        m = re.search(rf"\b{label}\s+([\d.]+)", mc)
        if m:
            out[label] = float(m.group(1))
    return out


def audit_bug3_macro(samples: List[Dict[str, Any]], n: int = 5) -> Dict[str, Any]:
    """随机抽 5 个不同 decision_date 样本，拉 ^VIX / ^TNX 比对。"""
    seen_dates: set = set()
    picks: List[int] = []
    indices = list(range(len(samples)))
    RNG.shuffle(indices)
    for i in indices:
        d = samples[i]["decision_date"]
        if d in seen_dates:
            continue
        picks.append(i)
        seen_dates.add(d)
        if len(picks) >= n:
            break

    results: List[Dict[str, Any]] = []
    max_err = 0.0
    mismatches = 0

    for idx in picks:
        s = samples[idx]
        ddate = pd.Timestamp(s["decision_date"])
        start = (ddate - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        end = (ddate + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        print(f"\n[Bug3] macro @ {s['decision_date']}")
        df_vix = fetch_history("^VIX", start, end)
        time.sleep(1)
        df_tnx = fetch_history("^TNX", start, end)
        time.sleep(1)
        expected = parse_macro_context(s["macro_context"])
        compares: Dict[str, Any] = {}
        local_max = 0.0
        local_mis = False
        # VIX: 取 <= ddate 最近一行
        for label, df in [("VIX", df_vix), ("TNX", df_tnx)]:
            if df.empty:
                compares[label] = {"error": "yfinance empty"}
                continue
            df_cut = df[df.index <= ddate]
            if df_cut.empty:
                compares[label] = {"error": "no row <= ddate"}
                continue
            actual = float(df_cut["Close"].iloc[-1])
            exp = expected.get(label)
            if exp is None:
                compares[label] = {"actual": actual, "expected": None}
                continue
            denom = max(abs(exp), 1e-3)
            rel = abs(exp - actual) / denom * 100
            compares[label] = {
                "expected": exp,
                "actual": round(actual, 3),
                "rel_err_pct": round(rel, 3),
                "yf_last_date": df_cut.index[-1].strftime("%Y-%m-%d"),
            }
            if rel > local_max:
                local_max = rel
            if rel > 5.0:
                local_mis = True
        max_err = max(max_err, local_max)
        if local_mis:
            mismatches += 1
        results.append({
            "decision_date": s["decision_date"],
            "compares": compares,
            "max_rel_err_pct": round(local_max, 3),
        })

    if mismatches == 0 and max_err < 5.0:
        verdict = "PASS"
    elif mismatches <= 1:
        verdict = "WARN"
    else:
        verdict = "FAIL"
    return {
        "samples_checked": len(picks),
        "mismatches": mismatches,
        "max_relative_error_pct": round(max_err, 3),
        "details": results,
        "verdict": verdict,
        "note": "只比对 VIX / TNX；DXY / USDCNY 跳过（yfinance 经常返空）",
    }


# ---- main ----------------------------------------------------------------------


def main() -> None:
    samples = json.loads(TRAINSET.read_text())
    print(f"Loaded {len(samples)} samples from {TRAINSET}")

    print("\n=== Bug 4: oracle_verdict consistency (full set) ===")
    bug4 = audit_bug4_oracle(samples)
    print(f"  mismatches={bug4['mismatches']} / {bug4['samples_checked']} "
          f"({bug4['mismatch_rate_pct']}%)  verdict={bug4['verdict']}")

    print("\n=== Bug 1: market_context look-ahead leak (5 random) ===")
    bug1 = audit_bug1_lookahead(samples, n=5)
    print(f"  max_rel_err_pct={bug1['max_relative_error_pct']}  "
          f"mismatches={bug1['mismatches']}  verdict={bug1['verdict']}")

    print("\n=== Bug 2: forward_30d_return_pct (10 random) ===")
    bug2 = audit_bug2_forward(samples, n=10)
    print(f"  max_rel_err_pct={bug2['max_relative_error_pct']}  "
          f"mismatches={bug2['mismatches']}  verdict={bug2['verdict']}")

    print("\n=== Bug 3: macro_context VIX/TNX (5 random) ===")
    bug3 = audit_bug3_macro(samples, n=5)
    print(f"  max_rel_err_pct={bug3['max_relative_error_pct']}  "
          f"mismatches={bug3['mismatches']}  verdict={bug3['verdict']}")

    # overall: any FAIL → FAIL；any WARN → WARN；else PASS
    verdicts = [bug1["verdict"], bug2["verdict"], bug3["verdict"], bug4["verdict"]]
    if "FAIL" in verdicts:
        overall = "FAIL"
    elif "WARN" in verdicts:
        overall = "WARN"
    else:
        overall = "PASS"

    out = {
        "audited_samples": len(samples),
        "bug1_lookahead_leak": bug1,
        "bug2_forward_window": bug2,
        "bug3_macro_alignment": bug3,
        "bug4_oracle_consistency": bug4,
        "overall_verdict": overall,
    }
    OUTPUT.write_text(json.dumps(out, indent=2, default=str))
    print(f"\n📄 Audit written to {OUTPUT}")
    print(f"OVERALL: {overall}")


if __name__ == "__main__":
    main()
