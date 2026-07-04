"""`python -m connectors.web_api` → 起 uvicorn dev server。

包形态下需要 __main__.py 才能被 `python -m` 调用（systemd/README.md 提到的
非 systemd 启动方式）。生产仍走 `uvicorn connectors.web_api:app`（见 __init__ docstring）。
"""
import uvicorn

uvicorn.run("openinvest.connectors.web_api:app", host="127.0.0.1", port=8765)
