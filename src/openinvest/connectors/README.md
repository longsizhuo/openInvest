# connectors/

对外**消费层**——业务逻辑全在 `core/`，本目录只做协议/接口适配，不重写决策。

## 内容

- `napcat_bot.py` — QQ 私聊命令式入口（NapCat OneBot 11 协议）。白名单 QQ 触发，命令格式 `/cmd args`，零 LLM 解析成本。
- `web_api.py` — FastAPI REST 层（GUI / 外部 agent 用）。同源部署 + Cloudflare Access 鉴权（生产）/ localhost-only（开源默认）。

## 与其他目录的关系

- 上游：用户（QQ / 浏览器 / agent）→ 本目录入口
- 下游：调用 `core/portfolio_manager.py` `core/memory_store.py` 读写数据；调用 `jobs/daily_report.py` 触发委员会
- 平级：与 `scripts/skill.py`（Claude Skill 入口）三个消费者平级，都是 core 的 wrapper
