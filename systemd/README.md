# systemd/

仓库自带的 systemd unit 模板。**仅作示例**——实际部署时 `sudo cp` 到 `/etc/systemd/system/` 再 `enable --now`。

## 内容

- `invest-web.service` — FastAPI Web API 后端（`uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765`）
  - 依赖 `network-online.target`；`Restart=on-failure RestartSec=15`
  - `ProtectSystem=strict` + `ReadWritePaths=%h/openInvest`（允许写 memory/db）

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
