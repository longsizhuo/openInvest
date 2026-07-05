"""Web GUI / API server 入口（console script `openinvest-web`，也可 `python -m`）。

原 run.sh gui 分支的 bash 逻辑（远端模式提示 / GUI dist 自动拉取 / host+port env）
全部收进这里——run.sh 收敛为 uvx 转发后，bash 里不留业务分支。
"""
from __future__ import annotations

import json
import os
import sys


def main() -> None:
    # 远端模式：GUI 由 hub serve，本机不起 uvicorn，直接给出入口
    api_base = os.getenv("INVEST_API_BASE", "").strip()
    if api_base:
        print(json.dumps({
            "status": "ok", "mode": "remote", "gui_url": api_base,
            "hint": "远端模式：GUI 由 hub serve，浏览器直接打开 gui_url 即可。",
        }, ensure_ascii=False))
        return

    # GUI dist 缺失时自动拉一次（失败不阻塞——API/Swagger 仍可用）
    from openinvest.paths import INVEST_ROOT
    if not (INVEST_ROOT / "static" / "index.html").exists():
        print("🎨 static/ 缺 GUI dist，自动拉取...", file=sys.stderr)
        try:
            from openinvest.gui_dist import main as sync_dist
            sync_dist([])  # 显式空 argv——不能让它误吞本进程的 --sync-only 等参数
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  GUI dist 拉取失败（{e}），跳过——API/Swagger 仍可用", file=sys.stderr)

    if "--sync-only" in sys.argv:
        return

    host = os.getenv("INVEST_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("INVEST_WEB_PORT", "8765"))

    # 端口已被占 = GUI 大概率已在跑——给 URL 优雅退出，别甩 uvicorn traceback
    import socket
    with socket.socket() as _s:
        if _s.connect_ex((host, port)) == 0:
            print(f"ℹ️  端口 {port} 已有服务在跑，直接打开 http://{host}:{port} 即可；"
                  f"要重启先停掉占用进程。", file=sys.stderr)
            return
    print(f"🚀 http://{host}:{port} （API: /api/…  Swagger: /docs，Ctrl+C 退出）",
          file=sys.stderr)
    import uvicorn
    uvicorn.run("openinvest.connectors.web_api:app", host=host, port=port)


if __name__ == "__main__":
    main()
