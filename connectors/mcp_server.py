"""兼容 shim → openinvest.connectors.mcp_server（旧 plugin run.sh 入口）。"""
from openinvest.connectors.mcp_server import *  # noqa: F401,F403
from openinvest.connectors.mcp_server import main, mcp  # noqa: F401

if __name__ == "__main__":
    main()
