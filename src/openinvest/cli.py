"""统一 Skill 入口 - 复用 invest 项目本体的 capabilities/core 模块

设计要点：
- 不再复制 invest 主流程逻辑，所有 prompt / debate 编排都走项目里现有的代码
  (capabilities.committee 各角色, core.debate, core.memory_store)
- Skill 模式下"答辩"的 LLM 不是 DeepSeek，而是 Claude 自己
  → prepare_debate 吐出 prompt 给 Claude 看
  → Claude 在主对话里依次扮演 bull/bear/judge
  → save_debate 把 Claude 的 transcript 落地到 memory/.debate/
- 所有子命令都输出 JSON 或 markdown，给 Claude 读

子命令：
  status                持仓 + 实时价 + 浮盈
  strategy              target_assets + Dreaming insights
  history [-n N]        近期交易 + 近期辩论
  what_if [...]         P&L 情景模拟
  live_prices           ^VIX, ^TNX, USDCNY, AUDCNY, NDQ, GC=F 一次拉齐
  prepare_debate SYM    输出辩论 brief（含项目原生 bull/bear/judge prompt）
  save_debate SYM       把 stdin 上来的 transcript 落到 memory/.debate/

——————————————————————————————————————————————————————————————————————
重构说明（refactor/skill-cmds-pkg）：
本文件已退化为**薄壳 façade / dispatcher**。所有 cmd_*() 实现已逐字搬到
scripts/skill_cmds/ 子包（按职责拆 5 个子模块）：
  - skill_cmds/_helpers.py        共享工具 _print_json / _safe_close / _now_iso_local
  - skill_cmds/analysis_cmds.py   status / strategy / history / what_if / correlate
                                  / live_prices / event_check
  - skill_cmds/committee_cmds.py  prepare_committee / run_committee / save_committee
  - skill_cmds/portfolio_cmds.py  deposit / withdraw / buy / sell / delete_holding / _resolve_pm
  - skill_cmds/lifecycle_cmds.py  doctor / init / _parse_holdings_with_llm / _write_v2_portfolio
本文件下面用 `from scripts.skill_cmds.<子模块> import *` 把全部符号重新对齐回
scripts.skill.<name> 访问点（兼容历史 `from scripts.skill import X`）；并保留
main() + __main__ guard + ROOT + bootstrap 头，保证 `python -m scripts.skill` 入口零变。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from openinvest.paths import INVEST_ROOT  # noqa: E402

ROOT = INVEST_ROOT

# 让 cmd_doctor 能看到 .env 里的 DEEPSEEK_API_KEY 等（_safe_close 等模块里也会
# 自己 load_dotenv，但 doctor 不依赖 utils 所以这里显式加一道）
try:
    from dotenv import load_dotenv  # noqa: E402
    load_dotenv(ROOT / ".env")
except ImportError:
    pass  # dotenv 尚未装时（极少见）跳过

from openinvest.core.memory_store import MemoryStore  # noqa: E402

# façade：把子包里各模块的 cmd_*() / helper / 常量 重新对齐回 scripts.skill.<name>。
# sys.path.insert 必须在这些 import 之前执行（裸跑 `python scripts/skill.py` 时
# 才能解析到包根）。各子模块各自定义 __all__（含下划线名），import * 才带得出。
from openinvest.skill_cmds._helpers import *  # noqa: E402,F401,F403
from openinvest.skill_cmds.analysis_cmds import *  # noqa: E402,F401,F403
from openinvest.skill_cmds.committee_cmds import *  # noqa: E402,F401,F403
from openinvest.skill_cmds.portfolio_cmds import *  # noqa: E402,F401,F403
from openinvest.skill_cmds.strategy_cmds import *  # noqa: E402,F401,F403
from openinvest.skill_cmds.lifecycle_cmds import *  # noqa: E402,F401,F403
from openinvest.skill_cmds.config_cmds import *  # noqa: E402,F401,F403


# ---------- main ----------

def main() -> None:
    # mcp 子命令最先分流：stdout 是 JSON-RPC 通道，不能进下面的重定向/argparse 输出
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        # 摘掉子命令 token——mcp_server.main() 自己 argparse（--http），留着会
        # "unrecognized arguments: mcp"（0.32.0 实际打挂过 uvx 部署）
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from openinvest.connectors.mcp_server import main as _mcp_main
        _mcp_main()
        return

    # 把 sys.stdout 重定向到 stderr，让 utils/* 里的 print() noise 走 stderr。
    # _print_json 用 sys.__stdout__ 写真正的 JSON。
    sys.stdout = sys.stderr

    parser = argparse.ArgumentParser(prog="skill")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("strategy").set_defaults(func=cmd_strategy)
    sub.add_parser("live_prices").set_defaults(func=cmd_live_prices)
    sub.add_parser("discipline", help="委员会纪律台账(不作为率+拦冲动+反事实损益)").set_defaults(func=cmd_discipline)

    p = sub.add_parser("decisions",
        help="统一决策视图：决议↔干预↔执行↔结果 join + 采纳率（等价 GET /api/decisions）")
    p.add_argument("--days", type=int, default=90, help="回看天数，默认 90")
    p.set_defaults(func=cmd_decisions)

    p = sub.add_parser("explain_decision",
        help="单条决议完整回放：辩论全文+CIO memo+path snapshot（等价 MCP explain_decision）")
    p.add_argument("decision_id", help='形如 "2026-07-03/GC=F"（decisions 输出里的 decision_id）')
    p.set_defaults(func=cmd_explain_decision)

    p = sub.add_parser("record_execution",
        help="记录你对某条决议的执行/拒绝 + 原因（等价 POST /api/decisions/execution）")
    p.add_argument("decision_id", help='形如 "2026-07-03/GC=F"（decisions 输出里的 decision_id）')
    p.add_argument("--rejected", action="store_true", help="标记为未执行（默认=已执行）")
    p.add_argument("--reason", help="原因（估值过高/资金不足/不同意委员会/...），宿主 Agent 采集")
    p.add_argument("--trade-ids", dest="trade_ids", help="关联 trades.db id，逗号分隔（可选）")
    p.set_defaults(func=cmd_record_execution)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    p = sub.add_parser("init")
    p.add_argument("--from-stdin", action="store_true",
                   help="读 stdin 上的 JSON（Claude 模式），否则走交互 input()")
    p.add_argument("--force", action="store_true",
                   help="user_profile.json 已存在时也覆盖")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("install_skills", aliases=["install-skills"],
        help="把随包分发的 skills（invest / invest-setup / invest-backup）拷进宿主平台"
             " skills 目录（替换式幂等）。无 plugin 机制的平台一条命令装齐")
    p.add_argument("--dest", default="~/.claude/skills",
                   help="目标 skills 目录，默认 ~/.claude/skills；OpenClaw 传 workspace 的"
                        " skills/。git clone 老用户不需要本命令（仓库 skills/ 已就位，"
                        "拷进 clone 会弄脏 working tree）")
    p.set_defaults(func=cmd_install_skills)

    p = sub.add_parser("history")
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("what_if",
        help="P&L 情景模拟（任意 yfinance symbol 涨跌）",
    )
    # 通用参数（推荐）：--symbol + --pct/--price 配对使用
    p.add_argument("--symbol", help="持仓里的 yfinance symbol，如 510300.SS / AAPL / BTC-USD")
    p.add_argument("--pct", type=float, help="该 symbol 涨跌百分比，如 -5 表示跌 5%%")
    p.add_argument("--price", type=float, help="该 symbol 的情景绝对价（与 --pct 二选一）")
    # 旧参数（兼容）：仅对 NDQ.AX 黄金生效
    p.add_argument("--gold-price", type=float, help="兼容旧参数：黄金克价绝对值 CNY/g")
    p.add_argument("--gold-pct", type=float, help="兼容旧参数：黄金涨跌百分比")
    p.add_argument("--ndq-price", type=float, help="兼容旧参数：NDQ.AX 价格 AUD")
    p.add_argument("--ndq-pct", type=float, help="兼容旧参数：NDQ.AX 涨跌百分比")
    p.add_argument("--audcny", type=float, help="情景 AUDCNY 汇率")
    p.set_defaults(func=cmd_what_if)

    p = sub.add_parser("correlate",
        help="跨资产关联分析（任意 yfinance symbol，无金额建议）"
             "—— 用户问'X 和 Y 有啥关联'、'X 趋势像 Y 吗'")
    p.add_argument("--symbols", required=True,
                   help="逗号分隔，至少 2 个，如 'NDQ.AX,0700.HK,510300.SS'")
    p.add_argument("--period", default="6mo",
                   help="yfinance period, e.g. 3mo/6mo/1y/2y，默认 6mo")
    p.add_argument("--with-llm", action="store_true",
                   help="可选：调 DeepSeek 给一句话中文总结（需要 DEEPSEEK_API_KEY）")
    p.set_defaults(func=cmd_correlate)

    p = sub.add_parser("prepare_committee")
    p.add_argument("symbol")
    p.set_defaults(func=cmd_prepare_committee)

    p = sub.add_parser("save_committee")
    p.add_argument("symbol")
    p.add_argument(
        "--provider", default=None,
        help="实际扮演 4 角色的 agent 品牌（claude/hermes/...），写进 transcript "
             "**Provider** 行。不传默认 claude（历史兼容）。",
    )
    p.set_defaults(func=cmd_save_committee)

    p = sub.add_parser(
        "run_committee",
        help="Direct 路径：调 DeepSeek 一键跑完委员会（任意 agent 可用，"
             "不依赖 Claude Code 的 Agent 工具）。需要 DEEPSEEK_API_KEY。",
    )
    p.add_argument("symbol")
    p.add_argument(
        "--force", action="store_true",
        help="即使今天已经跑过也重新跑（默认会读 cache 不重复消耗 token）",
    )
    p.add_argument(
        "--max-rounds", type=int, default=1, dest="max_rounds",
        help="cross-challenge 最大轮数，默认 1（同 daily_report cron）",
    )
    p.set_defaults(func=cmd_run_committee)

    sub.add_parser(
        "daily_report",
        help="完整日报管道（多资产委员会+Gemini 第二意见+翻译官+纪律台账），stdout "
             "输出邮件同款 markdown，不发邮件——宿主 agent 侧 cron 原样投递用。"
             "需要 DEEPSEEK_API_KEY。",
    ).set_defaults(func=cmd_daily_report)

    # ============ 写操作子命令（Agent 用） ============
    p = sub.add_parser("deposit", help="存入现金（任意币种）")
    p.add_argument("--currency", "-c", required=True, help="币种 (CNY/AUD/USD/HKD/...)")
    p.add_argument("--amount", "-a", type=float, required=True, help="存入金额（正数）")
    p.set_defaults(func=cmd_deposit)

    p = sub.add_parser("withdraw", help="取出现金（任意币种），余额不足报错")
    p.add_argument("--currency", "-c", required=True, help="币种")
    p.add_argument("--amount", "-a", type=float, required=True, help="取出金额（正数）")
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("buy", help="加仓 / 建新仓（已有 symbol → 加权平均成本；新 → 建仓）")
    p.add_argument("--symbol", required=True, help="yfinance symbol（如 510300.SS / AAPL / BTC-USD）")
    p.add_argument("--units", type=float, required=True, help="买入数量")
    p.add_argument("--price", type=float, required=True, help="单价（与 currency 同币种）")
    p.add_argument("--currency", "-c", default="CNY", help="计价币种，默认 CNY")
    p.add_argument("--kind", choices=["equity", "etf", "metal", "crypto", "bond", "fund", "other"],
                   default="equity", help="资产类型，默认 equity")
    p.add_argument("--unit-label", default="股", help="单位（股/克/oz/coin），默认 '股'")
    p.set_defaults(func=cmd_buy)

    p = sub.add_parser("sell", help="减仓（units 减，cost_avg 不变，按 holding 的 cost_currency 还现金）")
    p.add_argument("--symbol", required=True)
    p.add_argument("--units", type=float, required=True, help="卖出数量")
    p.add_argument("--price", type=float, required=True, help="卖出单价")
    p.set_defaults(func=cmd_sell)

    p = sub.add_parser("delete_holding", help="删除持仓行（units 必须 0 或加 --force）")
    p.add_argument("--symbol", required=True)
    p.add_argument("--force", action="store_true", help="units > 0 也强删")
    p.set_defaults(func=cmd_delete_holding)

    # ---- strategy 写操作（issue #179：读写对等，与 MCP 工具同名同语义）----
    p = sub.add_parser("set_allocations", help="改股票/现金目标配比（两者和必须 ≈1）")
    p.add_argument("--stock", type=float, required=True, help="股票目标比例 0~1")
    p.add_argument("--cash", type=float, required=True, help="现金目标比例 0~1")
    p.set_defaults(func=cmd_set_allocations)

    p = sub.add_parser(
        "track_asset",
        help="跟踪标的（upsert 幂等：不存在新建 / 已存在只更新传入字段）。新建须带 --max-single-invest-cny",
    )
    p.add_argument("--symbol", required=True, help="yfinance symbol（如 AAPL / GC=F / 510300.SS）")
    p.add_argument("--display-name", default=None, help="人类可读名")
    p.add_argument("--channel", default=None, help="交易渠道（如 CommSec / 浙商积存金）")
    p.add_argument("--max-single-invest-cny", type=float, default=None,
                   help="单笔投入上限 CNY（0~1,000,000；新建必填）")
    p.add_argument("--price-offset-pct", type=float, default=None, help="报价偏移 -0.1~0.1")
    p.add_argument("--sell-fee-pct", type=float, default=None, help="卖出费率 0~0.05")
    p.set_defaults(func=cmd_track_asset)

    p = sub.add_parser("untrack_asset", help="移除跟踪标的（schema 保证至少剩 1 个）")
    p.add_argument("--symbol", required=True)
    p.set_defaults(func=cmd_untrack_asset)

    p = sub.add_parser(
        "import",
        help="自由文本/CSV 持仓描述 → LLM 解析成结构化持仓。默认只预览，--commit 才落盘（非破坏）。",
    )
    p.add_argument("--file", help="读文件（CSV/txt）")
    p.add_argument("--text", help="直接给文本（与 --file 二选一；都不给则读 stdin）")
    p.add_argument("--commit", action="store_true",
                   help="非破坏写入（只加新 symbol、cash 只填当前为 0 的币种）；默认只预览")
    p.set_defaults(func=cmd_import_holdings)

    p = sub.add_parser(
        "ingest_event",
        help="把 agent 搜到的新闻投喂进事件账本（归一化+判级+入库，幂等；需后端 LLM key）",
    )
    p.add_argument("--title", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--snippet", help="摘要（可选，给归一化 LLM 更多上下文）")
    p.add_argument("--source", help="来源标注（如 caixin / 你的搜索渠道），入库为 agent:<source>")
    p.add_argument("--ts", help="事件时间 ISO 8601（可选）")
    p.add_argument("--ingested-by", dest="ingested_by", default="host-agent",
                   help="喂料 agent 身份（溯源，落 events.ingested_by；区别于 --source=新闻来源）")
    p.set_defaults(func=cmd_ingest_event)

    p = sub.add_parser(
        "news_sources",
        help="新闻源清单：默认列出（默认+额外）；--add NAME URL 加源（live probe 校验）；--remove NAME_OR_URL 删额外源",
    )
    p.add_argument("--add", nargs=2, metavar=("NAME", "URL"))
    p.add_argument("--remove", metavar="NAME_OR_URL")
    p.set_defaults(func=cmd_news_sources)

    p = sub.add_parser(
        "event_check",
        help="事件层（第一层）—— 拉多源新闻 / 归一化 / 入库 / 触发委员会 + 邮件。"
             "默认 dry-run（只入库，不发邮件不触委员会）。"
             "--live 才真发；--recall SYM 测 RAG 召回。",
    )
    p.add_argument("--live", action="store_true",
                   help="真发邮件 + POST /api/committee/run。不加这个默认 dry-run。")
    p.add_argument("--recall", metavar="SYMBOL",
                   help="只测 event_store.recall(SYMBOL)，不抓新源")
    p.set_defaults(func=cmd_event_check)

    p = sub.add_parser(
        "config",
        help="读/改可经 API 配置的白名单参数（concentration_lens / risk_profile / "
             "gold_defense_dca / dreaming.llm_verify）。等价 GET/PUT /api/config。",
    )
    p.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"),
                   help="设一条 override，如 --set verdict.concentration_lens_enabled false")
    p.add_argument("--clear", metavar="KEY", help="删一条 override，回退 env/yaml/默认")
    p.set_defaults(func=cmd_config)

    args = parser.parse_args()

    # 远端模式（hub-and-spoke）：INVEST_API_BASE 设置时子命令转发给远端 hub。
    # 必须在 args.func（任何 MemoryStore()/PortfolioManager() 实例化）之前分发——
    # MemoryStore.__init__ 会 mkdir memory/，客户端机器不应产生这个目录。
    import os
    if os.getenv("INVEST_API_BASE", "").strip():
        from openinvest.remote_dispatch import maybe_dispatch_remote
        if maybe_dispatch_remote(args):
            return

    args.func(args)


if __name__ == "__main__":
    main()
