"""Aggregate-only replay of frozen path-review rows. No network, DB or LLM.

Run from the repository root with PYTHONPATH=src. Input stays private.
Uses the production pure calibration function and original grid/tie order.
"""
import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

from openinvest.calc.regime_probability import calibrate_profile

WINDOWS = ("30d", "60d", "90d")


def evaluate(rows, k, gamma):
    out = {}
    for w in WINDOWS:
        subset = [r for r in rows if r["window"] == w]
        if not subset:
            raise ValueError(f"missing evaluation window {w}")
        cov = brier = 0.0
        for r in subset:
            x = calibrate_profile(
                {"windows": {w: r["pred"]}, "uncond_windows": {w: r["uncond"]}},
                shrinkage_k=k, band_gamma=gamma)["windows"][w]
            cov += x["p10_pct"] <= r["fwd_real"] <= x["p90_pct"]
            brier += (x["p_below"] - int(r["below_current"])) ** 2
        n = len(subset)
        out[w] = {"n": n, "coverage": cov / n, "brier": brier / n}
    return out


def replay(path, purge):
    raw = path.read_bytes()
    source = [json.loads(line) for line in raw.splitlines() if line.strip()]
    rows, seen = [], set()
    for r in source:
        key = (r["date"], r["asset"], r.get("source"))
        if key in seen:
            raise ValueError("duplicate date/asset/source; resolve before scoring")
        seen.add(key)
        for w, x in r["windows"].items():
            if w in WINDOWS and "pred" in x and "uncond" in x:
                rows.append(dict(date=r["date"], window=w, **x))
    fit_end = date(2017, 12, 31)
    fit = [r for r in rows if date.fromisoformat(r["date"]) <= fit_end]
    oos = [r for r in rows if date.fromisoformat(r["date"]) > fit_end]
    # Conservative 7-day margin for next-session settlement after a calendar horizon.
    purged = [r for r in fit if date.fromisoformat(r["date"]) +
              timedelta(days=int(r["window"][:-1]) + 7) <= fit_end]
    training = purged if purge else fit
    best, best_loss = None, float("inf")
    for k in (0, 5, 10, 20, 40, 80):
        for g in (1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6):
            metrics = evaluate(training, k, g)
            loss = sum(abs(x["coverage"] - .8) for x in metrics.values())
            loss += sum(x["brier"] for x in metrics.values()) / 3
            if loss < best_loss:
                best, best_loss = (k, g), loss
    before = evaluate(oos, 0, 1)
    after = evaluate(oos, *best)
    return {"input_sha256": hashlib.sha256(raw).hexdigest(),
            "source_rows": len(source), "purge_fit_labels": purge,
            "fit_window_rows": len(training), "removed_window_rows": len(fit)-len(training),
            "k": best[0], "gamma": best[1], "oos_raw": before, "oos_calibrated": after,
            "gate_pass": sum(.75 <= x["coverage"] <= .85 for x in after.values()) >= 2
            and all(after[w]["brier"] <= before[w]["brier"] + 1e-9 for w in WINDOWS),
            "inference": "descriptive only; overlapping labels, no independence claim"}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path)
    p.add_argument("--purge-fit-labels", action="store_true",
                   help="sensitivity analysis; does not replace the frozen historical result")
    args = p.parse_args()
    print(json.dumps(replay(args.input, args.purge_fit_labels), indent=2))
