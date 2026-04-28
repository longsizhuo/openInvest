"""每 2 小时拉取 CommSec 成交回报邮件，更新 portfolio.

替代旧 main.py 内联的 IMAP 检查。
现在独立 job，不依赖 daily_report 触发。
"""
from __future__ import annotations

import os
from typing import Any, Dict

from dotenv import load_dotenv

from core.portfolio_manager import PortfolioManager
from services.commsec_reader import CommSecReader

load_dotenv()


def run() -> Dict[str, Any]:
    email_user = os.getenv("EMAIL_SENDER")
    email_pass = os.getenv("EMAIL_PASSWORD")
    if not (email_user and email_pass):
        return {"status": "skipped", "reason": "no_email_credentials"}

    pm = PortfolioManager()
    reader = CommSecReader(email_user, email_pass)
    if not reader.connect():
        return {"status": "failed", "reason": "imap_connection_failed"}

    try:
        processed = pm.get_processed_emails()
        new_trades = reader.fetch_trade_confirmations(
            lookback_days=180, processed_ids=processed
        )
        for trade in new_trades:
            pm.record_external_trade(trade)
    finally:
        reader.close()

    return {"status": "success", "new_trades": len(new_trades)}


if __name__ == "__main__":
    print(run())
