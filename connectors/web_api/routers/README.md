# connectors/web_api/routers/

FastAPI 路由子包，按 tag/域拆分。每个文件只暴露一个 `router = APIRouter()`，
由 `connectors/web_api/__init__.py` 的 `include_router` 循环统一挂载（不被
from-import 符号）。响应模型集中在 `../models.py`，`get_pm` 依赖在 `../deps.py`。

各文件职责：

- `meta.py` — 健康检查（/api/health）。
- `read.py` — 只读端点：持仓 / 行情 / 策略 / 历史 / symbol 搜索。
- `holdings_write.py` / `cash_write.py` / `strategy_write.py` / `write.py` / `user.py` — 写入类端点（持仓、现金、策略、交易记账、用户配置）。
- `committee.py` — 委员会异步任务编排（run/status/prepare/save）。
- `committee_sessions.py` — 历史委员会决议列表 + 单条 markdown（#55 后从 system.py 拆出）。
- `events.py` / `commsec.py` / `trades.py` / `skill.py` — 事件流、CommSec 邮件、内部账本、skill 桥接端点。
- `insights.py` — Dreaming 长期洞察 / 新鲜洞察 toast / 主动 reengagement nudge（system.py 拆出）。
- `observability.py` — cron job 状态 / LLM 用量明细汇总 / 数据源健康 / tool 调用明细（system.py 拆出）。
- `verdict_review.py` — 后验命中率原始数据 / 汇总 / markdown 报告（system.py 拆出）。
- `regime.py` — 实时 regime 分类 + 硬规则/提示词暴露（system.py 拆出）。
- `state.py` — Dreaming 状态 / PnL 历史 / 跑赢基准事件（system.py 拆出）。

> 2026-06-15：`system.py`（#55 漏下的 catch-all）按域拆成上述
> insights/observability/verdict_review/committee_sessions/regime/state 六个
> router，path 全不变，旧 `system.py` 已删除（全仓无外部 import）。
