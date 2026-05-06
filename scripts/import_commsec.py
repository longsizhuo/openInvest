"""手动导入 CommSec 邮件成交回报到 portfolio。

替代 jobs/commsec_sync 的 cron 自动触发——cron 模式遇到 IMAP 临时
失败会静默丢成交，导致 portfolio 和真实账户对不上。

跑法：
    # 仅预览（不写入），lookback 默认 180 天
    uv run python -m scripts.import_commsec --dry-run

    # 真正导入（lookback 30 天，加快速度）
    uv run python -m scripts.import_commsec --lookback 30 --apply

    # 默认 dry-run；加 --apply 才会写
    uv run python -m scripts.import_commsec

依赖环境变量：EMAIL_SENDER / EMAIL_PASSWORD（Gmail 应用密码）
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List

from dotenv import load_dotenv

load_dotenv()

from core.portfolio_manager import PortfolioManager  # noqa: E402
from services.commsec_reader import CommSecReader  # noqa: E402


def _print_trade(t: Dict[str, Any]) -> None:
    """美化打印一条成交"""
    print(
        f"  • [{t.get('ts_origin', '?')}] {t.get('action', '?').upper():6s}"
        f" {t.get('symbol', '?'):10s} "
        f"{t.get('units', 0)} @ {t.get('price_per_unit', 0)} = "
        f"{t.get('total_amount', 0)} {t.get('currency', '?')}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lookback", type=int, default=180,
        help="回溯天数（默认 180）",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真正写入 portfolio（默认仅 dry-run 打印）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="（兼容老脚本 alias）等同于不加 --apply",
    )
    args = parser.parse_args()

    apply = args.apply and not args.dry_run

    email_user = os.getenv("EMAIL_SENDER")
    email_pass = os.getenv("EMAIL_PASSWORD")
    if not (email_user and email_pass):
        print("❌ 缺少 EMAIL_SENDER / EMAIL_PASSWORD 环境变量")
        return 2

    pm = PortfolioManager()
    reader = CommSecReader(email_user, email_pass)
    if not reader.connect():
        print("❌ IMAP 连接失败")
        return 3

    try:
        processed = pm.get_processed_emails()
        new_trades: List[Dict[str, Any]] = reader.fetch_trade_confirmations(
            lookback_days=args.lookback, processed_ids=processed,
        )
    finally:
        reader.close()

    if not new_trades:
        print(f"✓ 回溯 {args.lookback} 天无新成交")
        return 0

    print(f"\n🔎 拿到 {len(new_trades)} 条新成交：")
    for t in new_trades:
        _print_trade(t)

    if not apply:
        print(
            f"\n💡 这是 dry-run（默认）。要真正写入：\n"
            f"   uv run python -m scripts.import_commsec --lookback {args.lookback} --apply",
        )
        return 0

    print(f"\n→ 写入 portfolio...")
    written = 0
    for trade in new_trades:
        try:
            pm.record_external_trade(trade)
            written += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ 写入失败 {trade.get('symbol')}: {e}")
    print(f"✅ 已写入 {written}/{len(new_trades)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
