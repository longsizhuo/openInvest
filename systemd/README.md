# systemd/

仓库自带的 systemd unit 模板。**仅作示例**——实际部署时 `sudo cp` 到 `/etc/systemd/system/` 再 `enable --now`。

## 内容

- `invest-web.service` — FastAPI Web API 后端（`uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765`）
  - 依赖 `network-online.target`；`Restart=on-failure RestartSec=15`
  - `ProtectSystem=strict` + `ReadWritePaths=%h/openInvest`（允许写 memory/db）
- `invest-mcp.service` — remote MCP（`openinvest-mcp --http`，streamable-HTTP 绑 127.0.0.1:8766）
  - spoke 机器的 agent 直连 `https://<hub>/mcp`（Caddy 反代 /mcp → 8766），替代旧的
    CLI→REST 转发路径
  - 鉴权复用同一 `.env` 的 `INVEST_API_TOKEN`（bearer；`/health` 豁免探活）
  - 部署步骤同 invest-web（下面命令里把 `invest-web` 换成 `invest-mcp`）；若用
    invest-deploy.sh 自动部署，记得在其 restart 行加 `invest-mcp.service`

## 部署

```bash
# 1) 改 unit 的 User=ubuntu 为你自己的用户名（如 ec2-user / pi / deploy）
sudo sed -i "s/^User=ubuntu/User=$USER/; s/^Group=ubuntu/Group=$(id -gn)/" \
    systemd/invest-web.service

# 2) 确认 INVEST_HOME 路径（默认 %h/openInvest = ~/openInvest）
#    如果你 clone 到了别的地方，改 Environment=INVEST_HOME / WorkingDirectory /
#    EnvironmentFile / ReadWritePaths 这 4 行的路径

# 3) 装 + 起
sudo cp systemd/invest-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now invest-web
sudo systemctl status invest-web
```

## 注意

- **`%h` 占位符**：systemd 会展开成本服务 `User=` 那个用户的 home 目录。原本仓库里
  写死 `/home/ubuntu/...`，fork 用户用 `ec2-user`/`pi`/`deploy` 等账号时无法直接用，
  改用 `%h` 后只需要改 `User=` 一行就能跑
- **绑 127.0.0.1**：公网入口由 Caddy 反代 + Cloudflare Access 鉴权（开源默认 localhost-only 模式同样适用）
- 其他用户场景（不用 systemd）：直接 `python -m connectors.web_api` 或写 docker-compose
