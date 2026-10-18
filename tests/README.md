# tests/

pytest 测试套件：约 130 个 `test_*.py`、1700+ 条用例，**全部不联网**（外部依赖一律 mock / 假 uvx / tmp 目录）。CI（`.github/workflows/ci.yml`）跑 `uv run pytest tests/`，红了不合并；`experiments/signal-eval/` 另有一条独立 CI 步骤。

## 跑法

```bash
DEEPSEEK_API_KEY=test-fake-key uv run pytest -q   # 全量（CI 同款：给个假 key，不会真调用）
uv run pytest tests/test_xxx.py                   # 单文件
uv run pytest -v -k "concurrent"                  # 关键词过滤
uv run lint-imports --no-cache                    # 分层契约（import-linter，CI 同跑）
```

**验证以全套（字母序）为准**：个别测试依赖模块级全局状态，手工拼文件顺序可能复现假失败。

## conftest.py 做了什么

不是"只加 sys.path"。它做三件事：

1. **`sys.path` 补仓库根**——后端已在 `src/openinvest/`（`import openinvest.*`），但仓库根的兼容 shim（`connectors/`、`scripts/` 等，git clone 形态才有）仍要能被测试 import。
2. **`_isolate_invest_env`（autouse）——凭据 scrub + env 隔离**：每个测试前清掉所有 `INVEST_*` 环境变量和 `OUTBOUND_KEYS`（`CHATBOT_ALERT_URL` / `CHATBOT_INTERNAL_KEY` / `EMAIL_SENDER` / `EMAIL_PASSWORD` / `DIGEST_EMAIL_TO`），前后各 `reset_config()` 一次（清 `load_config()` 缓存与 persistent overrides）。前半是防开发机 `.env` 渗进"断言默认值"的测试；后半是 **外发守卫**——出口函数拿不到凭据就直接返回，测试里不会真发 Discord DM / 邮件（2026-10-13 事故，worktree 会向上找到主 checkout 的生产 `.env`）。要测出口本身的测试在 body 里 `monkeypatch.setenv`。
3. **`_isolate_memory_root`（autouse）**：`MemoryStore()` 默认目录指到每个测试自己的 tmp，防止 fixture 写进真实 `memory/portfolio.md`。

守卫测试（改动这些机制前先看它们）：

- `test_no_outbound_in_tests.py`——出口函数在测试进程里是 inert 的（`requests.post` / `smtplib.SMTP` 被替成会炸的替身）
- `test_no_bare_dotenv.py`——`src/` `scripts/` 里不得有裸 `load_dotenv()`（运行时根因；`.env` 只在 `openinvest/paths.py` 单点加载）
- `test_committee_contract.py`——分层契约 SENTINEL：session 内部的 event / macro / sentiment / valuation 真的注入 `run_committee`（见根 `CLAUDE.md` "分层契约"）
- `test_tools_md_lists_cli.py`——`plugin/skills/invest/references/tools.md` 必须列全 CLI 每个子命令
- `test_plugin_manifest_version.py`——plugin manifest 的 version 策略（见根 `CLAUDE.md` "发版"）

## 按域分组（代表文件，不是全表）

| 域 | 代表文件 |
|---|---|
| 账本 / 并发 / 幂等 | `test_memory_store`（原子写 + 50 线程 RMW）、`test_portfolio_manager`、`test_ledger_idempotency`、`test_state_claim_concurrency`、`test_decision_ledger`、`test_trades_db`、`test_trades_portfolio_sync` |
| 委员会 | `test_committee_parser`（CIO 输出解析）、`test_cio_json_output`、`test_prepare_committee`（Coordinator）、`test_run_committee_cached`、`test_regime_*`、`test_fast_crash_defense` |
| 入口层 | `test_web_api`（FastAPI TestClient 全端点）、`test_web_api_event_loop`、`test_mcp_server`（MCP 工具集快照）、`test_mcp_http*`、`test_remote_dispatch`、`test_cli_*`、`test_portfolio_cmds` |
| 事件 / 新闻 | `test_event_*`、`test_news_sources`、`test_news_feeds`、`test_searxng_news`、`test_price_sentinel`、`test_job_watchdog` |
| 定时任务 / 报告 | `test_scheduler_*`、`test_daily_report_*`、`test_dreaming_*`、`test_verdict_review*`、`test_backup_job`、`test_pnl_snapshot`、`test_account_performance` |
| 行情 / 计算 | `test_market_store*`、`test_gold_price`、`test_fx`、`test_market_metrics`、`test_what_if_fx`、`test_base_currency`、`test_backtest_*`、`test_paper_trade_simulator*` |
| 插件 / skill 打包 | `test_run_sh_hint`、`test_backup_skill_run_sh`（真跑 bash + zip）、`test_skills_loader`、`test_onboarding_smoke` |
| 配置 / schema | `test_config`、`test_schemas`、`test_migrate_*`、`test_risk_profile` |

## 与其他目录的关系

- 测真实代码：`src/openinvest/` 下的 `core/` `connectors/` `jobs/` `services/` `utils/` `calc/`，以及 `plugin/skills/*/scripts/run.sh`。
- 网络一律 mock（yfinance / ddgs / searxng / IMAP / SMTP / LLM）；需要真实数据的研究类验证放 `experiments/`，不进本套件。
