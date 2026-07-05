"""API server 入口（console script `openinvest-web`，也可 `python -m`）。

deprecated 面（见 web_api/__init__ 头注）：只为 remote hub 部署保留启动器。
GUI 壳层已退役——本进程不再 serve 静态文件。
"""
from __future__ import annotations

import os
import sys


def main() -> None:
    host = os.getenv("INVEST_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("INVEST_WEB_PORT", "8765"))

    # 端口已被占 = 服务大概率已在跑——给 URL 优雅退出，别甩 uvicorn traceback
    import socket
    with socket.socket() as _s:
        if _s.connect_ex((host, port)) == 0:
            print(f"ℹ️  端口 {port} 已有服务在跑（http://{host}:{port}）；要重启先停掉占用进程。",
                  file=sys.stderr)
            return
    print(f"🚀 API http://{host}:{port} （Swagger: /docs，Ctrl+C 退出）", file=sys.stderr)
    import uvicorn
    uvicorn.run("openinvest.connectors.web_api:app", host=host, port=port)


if __name__ == "__main__":
    main()
