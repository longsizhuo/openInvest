"""兼容 shim → openinvest.migrate_portfolio_to_holdings（老 clone / 文档里的 `python -m scripts.migrate_portfolio_to_holdings`）。"""
import sys

from openinvest.migrate_portfolio_to_holdings import main, migrate, render_portfolio_body_v2  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())
