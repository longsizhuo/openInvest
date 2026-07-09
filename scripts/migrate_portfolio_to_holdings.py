"""把 portfolio.md 从 v1（扁平字段）迁移到 v2（cash dict + holdings list）

v1 字段:
- cash_cny: 1234.56
- aud_cash: -100.00
- ndq_shares: 256.0
- ndq_avg_cost_aud_per_share: 53.86
- gold_grams: 133.88
- gold_avg_cost_cny_per_gram: 1008.34

v2 结构:
- cash: {"CNY": 1234.56, "AUD": -100.00}
- holdings: [{symbol, kind, units, unit_label, avg_cost, cost_currency, ...}]
- schema_version: 2

设计：
- **幂等**: schema_version >= 2 时直接 noop
- **原子**: store.transaction 单锁 RMW + commit-on-success（schema fail 自动 rollback）
- **可恢复**: 跑前 cp 备份到 portfolio.md.bak.<timestamp>
- **body 重渲染**: 重写 markdown 正文为 v2 模板

跑法:
    cd /home/ubuntu/projects-review/invest
    uv run python -m scripts.migrate_portfolio_to_holdings
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from openinvest.core.memory_store import MemoryStore
from openinvest.core.schemas import validate_portfolio


def render_portfolio_body_v2(cash: Dict[str, float], holdings: List[Dict[str, Any]]) -> str:
    """v2 portfolio.md body 模板"""
    lines = ["# 当前持仓", ""]

    if cash:
        lines.append("## 现金")
        for ccy, amt in sorted(cash.items()):
            lines.append(f"- **{ccy}**: {amt:,.2f}")
        lines.append("")

    if holdings:
        lines.append("## 持仓")
        for h in holdings:
            unit = h.get("unit_label", "share")
            label = h.get("display_name") or h["symbol"]
            avg = float(h.get("avg_cost", 0) or 0)
            ccy = h.get("cost_currency", "")
            tracking = " 🔍 [追踪仓]" if h.get("is_tracking_only") else ""
            line = f"- **{label}** (`{h['symbol']}`){tracking}: {h['units']} {unit}"
            if avg:
                line += f"，均价 {avg:.2f} {ccy}/{unit}"
            channel = h.get("channel")
            if channel:
                line += f"（渠道 {channel}）"
            lines.append(line)
        lines.append("")
    else:
        lines.append("（暂无持仓）")
        lines.append("")

    lines.extend([
        "## 说明",
        "",
        "此文件由 daily_report / commsec_sync / web_api / napcat_bot 自动更新。",
        "不要手动编辑——如需调整，请走 GUI / NapCat /cmd 命令。",
    ])
    return "\n".join(lines) + "\n"


def migrate(store: Optional[MemoryStore] = None, force: bool = False) -> Dict[str, Any]:
    """执行迁移，返回结果 dict

    Args:
        store: MemoryStore 实例；None 则用默认
        force: 即便检测到部分新结构（holdings:）也强制重跑（删原 holdings 重建）
    """
    store = store or MemoryStore()
    portfolio_path = store.path_of("portfolio")

    if not portfolio_path.exists():
        return {"status": "noop", "reason": "portfolio.md 不存在"}

    # 备份（先于 transaction，确保锁外操作）
    bak_path = portfolio_path.with_suffix(
        f".md.bak.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy(portfolio_path, bak_path)

    with store.transaction("portfolio") as tx:
        # 幂等检查
        version = int(tx.get("schema_version", 1) or 1)
        if version >= 2 and not force:
            return {
                "status": "noop",
                "reason": f"already v{version}",
                "backup": str(bak_path),
            }

        # 警示：如果检测到 holdings 字段已存在但 version 还是 1，可能是手工改过半，需要 --force
        if "holdings" in tx.metadata and not force:
            return {
                "status": "abort",
                "reason": "已检测到 holdings 字段但 schema_version<2，可能数据混乱；用 --force 强制",
                "backup": str(bak_path),
            }

        # 提取 v1 字段
        cash_cny = float(tx.get("cash_cny", 0) or 0)
        aud_cash = float(tx.get("aud_cash", 0) or 0)
        ndq_shares = float(tx.get("ndq_shares", 0) or 0)
        ndq_avg = float(tx.get("ndq_avg_cost_aud_per_share", 0) or 0)
        gold_grams = float(tx.get("gold_grams", 0) or 0)
        gold_avg = float(tx.get("gold_avg_cost_cny_per_gram", 0) or 0)

        # 构造 cash（保留所有非零；包含负数）
        cash: Dict[str, float] = {}
        if cash_cny != 0:
            cash["CNY"] = cash_cny
        if aud_cash != 0:
            cash["AUD"] = aud_cash

        # 构造 holdings
        holdings: List[Dict[str, Any]] = []
        if ndq_shares > 0:
            holdings.append({
                "symbol": "NDQ.AX",
                "kind": "etf",
                "units": ndq_shares,
                "unit_label": "股",
                "avg_cost": ndq_avg,
                "cost_currency": "AUD",
                "channel": "CommSec",
                "display_name": "BetaShares Nasdaq 100 ETF",
                "proxy_kind": "direct",
            })
        if gold_grams > 0:
            holdings.append({
                "symbol": "GC=F",
                "kind": "metal",
                "units": gold_grams,
                "unit_label": "克",
                "avg_cost": gold_avg,
                "cost_currency": "CNY",
                "channel": "浙商积存金",
                "display_name": "伦敦金 (浙商积存金)",
                "yfinance_proxy": "GC=F",
                "proxy_kind": "gold_cny_per_gram",
                "sell_fee_pct": 0.0038,
            })

        # 删除 v1 旧字段（避免新旧并存的 fan-out bug）
        for k in (
            "cash_cny", "aud_cash",
            "ndq_shares", "ndq_avg_cost_aud_per_share",
            "gold_grams", "gold_avg_cost_cny_per_gram",
        ):
            tx.metadata.pop(k, None)

        # 写入 v2 结构
        tx["cash"] = cash
        tx["holdings"] = holdings
        tx["schema_version"] = 2

        # commit 前 schema 校验（fail 自动 rollback by transaction commit-on-success）
        validate_portfolio(dict(tx.metadata))

        # 重渲染 body
        tx.set_body(render_portfolio_body_v2(cash, holdings))

    return {
        "status": "migrated",
        "backup": str(bak_path),
        "cash_currencies": list(cash.keys()),
        "holdings_count": len(holdings),
        "holdings": [h["symbol"] for h in holdings],
    }


def main() -> int:
    force = "--force" in sys.argv
    result = migrate(force=force)
    print("=" * 50)
    for k, v in result.items():
        print(f"{k}: {v}")
    print("=" * 50)
    return 0 if result["status"] in ("migrated", "noop") else 1


if __name__ == "__main__":
    sys.exit(main())
