# systemd/

仓库自带的 systemd unit 模板。**仅作示例**——实际部署时 `sudo cp` 到 `/etc/systemd/system/` 再 `enable --now`。

## 内容

- `invest-web.service` — FastAPI Web API 后端（`uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765`）
  - 依赖 `network-online.target`；`Restart=on-failure RestartSec=15`
  - `ProtectSystem=strict` + `ReadWritePaths=/home/ubuntu/projects-review/invest`（允许写 memory/db）

## 部署

```bash
sudo cp systemd/invest-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now invest-web
sudo systemctl status invest-web
```

## 注意

- **绑 127.0.0.1**：公网入口由 Caddy 反代 + Cloudflare Access 鉴权（开源默认 localhost-only 模式同样适用）
- 其他用户场景（不用 systemd）：直接 `python -m connectors.web_api` 或写 docker-compose
