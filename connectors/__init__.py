"""兼容 shim 包 —— 真身在 openinvest.connectors（src/ 重排）。

旧版 plugin run.sh 的 mcp 分支调 `.venv/bin/python -m connectors.mcp_server`，
systemd 老 unit 用 `uvicorn connectors.web_api:app`。只在 git clone 形态存在，
不进 wheel。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
