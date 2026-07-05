"""一次性回填：从历史 transcript 反推干预记录，种子化 interventions.jsonl。

反事实记账 2026-06-12 才上线，但 2026-04-28 起的每份委员会 transcript 都存有
CIO 原话（VERDICT/SUGGESTED_ALLOC_CNY），daily log 存有确定性后处理后的最终
裁决——两者的差值就是历史干预。本脚本确定性 diff 重建，把账本起点从今天
提前到 04-28（约 6 周样本的时间价值）。

口径与局限（如实声明）：
- rule 归因是启发式（原 TRIM→HOLD 标 trim_blocked；原买侧→降级 标
  defense_downgrade）——历史标记字段没落盘，无法区分 sanity4/sanity5
- 同日多跑只取**最后一跑**（transcript 为当日最后覆盖写，daily log 取末段）
- 重建行带 "reconstructed": true，与 live 行可区分
- 幂等：先清掉已有 reconstructed 行再写，live 行不动

用法：uv run python scripts/reconstruct_interventions.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from openinvest.core.committee_runner import rule_family  # noqa: E402
from openinvest.core.memory_store import MemoryStore  # noqa: E402

# transcript 里 CIO memo 的原话字段
RE_VERDICT = re.compile(r"^VERDICT:\s*([A-Z]+)", re.M)
RE_ALLOC = re.compile(r"^SUGGESTED_ALLOC_CNY:\s*(-?[\d,]+)", re.M)
RE_REGIME = re.compile(r"^REGIME:\s*(\w+)", re.M)
# daily log 的最终裁决行（同一天多跑取最后一次出现）
RE_FINAL = re.compile(r"\(({sym})\): ([A-Z]+) \(conf [\d.]+, alloc ¥(-?\d+)\)")

SYM_FILE = {"GC=F": "GC_F.md", "NDQ.AX": "NDQ_AX.md",
            "ASIA.AX": "ASIA_AX.md", "0700.HK": "0700_HK.md"}


def parse_transcript(path: Path) -> Optional[Tuple[str, float, str]]:
    """→ (原 verdict, 原 alloc, regime)；解析失败返回 None。"""
    text = path.read_text(encoding="utf-8")
    mv, ma = RE_VERDICT.search(text), RE_ALLOC.search(text)
    if not mv or not ma:
        return None
    mr = RE_REGIME.search(text)
    return (mv.group(1), float(ma.group(1).replace(",", "")),
            mr.group(1) if mr else "")


def parse_final(daily_text: str, sym: str) -> Optional[Tuple[str, float]]:
    """daily log 最后一段 committee_report 里该资产的最终裁决。"""
    pat = re.compile(
        r"\(" + re.escape(sym) + r"\): ([A-Z]+) \(conf [\d.]+, alloc ¥(-?\d+)\)")
    hits = pat.findall(daily_text)
    if not hits:
        return None
    v, a = hits[-1]
    return v, float(a)


def infer_rule(orig_v: str, final_v: str) -> Optional[str]:
    """只认确定性规则语义上可能的降级方向；其余（如 HOLD→TRIM、TRIM 改额度）
    是 "transcript=当日最后一跑(任意路径) vs daily=cron 跑" 的错配假象 → 丢弃。"""
    if orig_v == "TRIM" and final_v == "HOLD":
        return "reconstructed_trim_blocked"          # sanity4/5 无法区分
    if orig_v in ("BUY", "ACCUMULATE") and final_v in ("ACCUMULATE", "HOLD") \
            and orig_v != final_v:
        return "reconstructed_defense_downgrade"
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = MemoryStore().root
    com, daily = root / ".committee", root / "daily"
    rows = []
    for ddir in sorted(p for p in com.iterdir()
                       if p.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", p.name)):
        date = ddir.name
        dfile = daily / f"{date}.md"
        if not dfile.exists():
            continue
        dtext = dfile.read_text(encoding="utf-8")
        for sym, fname in SYM_FILE.items():
            t = ddir / fname
            if not t.exists():
                continue
            orig = parse_transcript(t)
            final = parse_final(dtext, sym)
            if orig is None or final is None:
                continue
            (ov, oa, regime), (fv, fa) = orig, final
            if ov == fv and oa == fa:
                continue
            if fv == "UNCLEAR":
                continue   # 解析失败日不算干预
            rule = infer_rule(ov, fv)
            if rule is None:
                continue   # 跑次错配假象，丢弃
            rows.append({
                "schema": 1, "date": date, "asset": sym, "regime": regime,
                "price": None,   # review job 以行情收盘为基准，不依赖此字段
                "rule": rule, "rule_family": rule_family(rule),
                "atr_defense_on": None,
                "original_verdict": ov, "original_alloc": oa,
                "final_verdict": fv, "final_alloc": fa,
                "delta_exposure_cny": oa - fa,
                "reconstructed": True,
            })

    print(f"重建干预 {len(rows)} 条（{rows[0]['date']} → {rows[-1]['date']}）" if rows
          else "无可重建干预")
    from collections import Counter
    for rule, n in Counter(r["rule"] for r in rows).items():
        print(f"  {rule}: {n}")
    if args.dry_run:
        for r in rows[:8]:
            print(" ", json.dumps(r, ensure_ascii=False))
        return

    path = root / ".dreams" / "interventions.jsonl"
    live = []
    if path.exists():
        live = [json.loads(l) for l in path.open(encoding="utf-8") if l.strip()]
        live = [r for r in live if not r.get("reconstructed")]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows + live:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"已写 {path}（重建 {len(rows)} + live {len(live)}）")


if __name__ == "__main__":
    main()
