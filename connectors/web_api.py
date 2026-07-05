"""兼容 shim → openinvest.connectors.web_api（旧 systemd unit `uvicorn connectors.web_api:app`）。"""
from openinvest.connectors.web_api import *  # noqa: F401,F403
from openinvest.connectors.web_api import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("openinvest.connectors.web_api:app", host="127.0.0.1", port=8765)
