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

    # 信任边界不裸奔（与 mcp_server._serve_http 同款守卫，CR 命中此处缺失）：
    # 文档教用户设 INVEST_WEB_HOST=0.0.0.0，漏配 token 就是把含 symbol/金额/
    # verdict 的全量读写 API 暴露公网——公开数据红线必须代码强制，不能只靠文档。
    token = os.getenv("INVEST_API_TOKEN", "").strip()
    if not token and host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit(
            f"拒绝启动：绑定 {host}（非 loopback）但 INVEST_API_TOKEN 未设置。"
            "远端暴露必须有 bearer 鉴权——在 .env 设 INVEST_API_TOKEN，"
            "或改绑 127.0.0.1 走反向代理。"
        )

    # 端口已被占 = 服务大概率已在跑——给 URL 优雅退出，别甩 uvicorn traceback。
    # 退出码 1（不是 0）：systemd Restart=on-failure 才会重试、compose/监控才看得见"没起来"；
    # 退出 0 会让 unit 静默停在 inactive（2026-10 评审 #459：systemd/compose 改走本入口后这条路径才被踩到）。
    import socket
    with socket.socket() as _s:
        if _s.connect_ex((host, port)) == 0:
            sys.exit(f"ℹ️  端口 {port} 已有服务在跑（http://{host}:{port}）；要重启先停掉占用进程。")
    print(f"🚀 API http://{host}:{port} （Swagger: /docs，Ctrl+C 退出）", file=sys.stderr)
    import uvicorn
    uvicorn.run("openinvest.connectors.web_api:app", host=host, port=port)


if __name__ == "__main__":
    main()
