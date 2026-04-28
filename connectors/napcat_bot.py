"""NapCat 私聊 connector - QQ 命令式交互入口

设计要点：
- 只响应白名单 QQ（默认 1169771750）
- 命令格式 `/cmd args` —— 不依赖 LLM 解析，零 token 成本
  （自然语言交互留给 P5: Claude Skill）
- 长跑 daemon，建议 `nohup python -m connectors.napcat_bot &` 或 systemd

支持命令：
  /help                                  显示帮助
  /balance                               当前持仓 + 现金 + 黄金估值
  /strategy                              当前策略与目标资产
  /gold                                  实时伦敦金 + 浙商参考价
  /ndq                                   实时 NDQ.AX
  /history [N]                           最近 N 笔交易（默认 5）
  /deposit <amount_cny>                  增加 CNY 现金（工资/转入）
  /withdraw <amount_cny>                 减少 CNY 现金
  /gold_buy <grams> @<price>             记录黄金买入
  /gold_sell <grams> @<price>            记录黄金卖出
  /gold_set <grams>                      直接设置黄金克数
  /gold_offset <bank_price>              报浙商当日克价，自动反推 offset 写回 strategy
  /risk <conservative|balanced|aggressive> 调整风险偏好
  /payday                                立即触发月度入账
  /run                                   异步触发 daily_report (~6 分钟)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import requests
import websockets
from dotenv import load_dotenv

from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
from utils.gold_price import format_gold_report, get_gold_snapshot, infer_offset_pct
from utils.exchange_fee import get_history_data

load_dotenv()

NAPCAT_WS_URL = os.getenv("NAPCAT_WS_URL", "ws://localhost:6101")
NAPCAT_HTTP_URL = os.getenv("NAPCAT_HTTP_URL", "http://localhost:6100")
WHITELIST_QQ = int(os.getenv("INVEST_WHITELIST_QQ", "1169771750"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("napcat_bot")


# ========== HTTP 发送 ==========

def send_private_msg(user_id: int, message: str) -> None:
    """通过 NapCat HTTP API 发私聊消息"""
    try:
        resp = requests.post(
            f"{NAPCAT_HTTP_URL}/send_private_msg",
            json={"user_id": user_id, "message": message},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(f"send_private_msg HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.error(f"send_private_msg failed: {e}")


# ========== 命令处理 ==========

@dataclass
class CommandContext:
    """每个命令拿到的上下文"""
    pm: PortfolioManager
    user_id: int
    raw: str        # 完整原始消息
    args: List[str]  # 去掉命令名后的参数列表


CommandHandler = Callable[[CommandContext], str]
COMMANDS: Dict[str, CommandHandler] = {}


def cmd(name: str):
    def deco(fn: CommandHandler) -> CommandHandler:
        COMMANDS[name] = fn
        return fn
    return deco


# ----- 查询类 -----

@cmd("help")
def _help(ctx: CommandContext) -> str:
    return (
        "📋 命令列表：\n"
        "/balance — 持仓 + 现金\n"
        "/strategy — 当前策略\n"
        "/gold — 实时金价 + 浙商参考\n"
        "/ndq — 实时 NDQ.AX\n"
        "/history [N] — 最近 N 笔交易\n"
        "/deposit <数额> — CNY 入账\n"
        "/withdraw <数额> — CNY 出账\n"
        "/gold_buy <克数> @<克价> — 记买入\n"
        "/gold_sell <克数> @<克价> — 记卖出\n"
        "/gold_set <克数> — 直接覆盖黄金克数\n"
        "/gold_offset <浙商克价> — 报当日克价，反推点差\n"
        "/risk <conservative|balanced|aggressive>\n"
        "/payday — 月度入账\n"
        "/run — 异步触发 daily_report"
    )


@cmd("balance")
def _balance(ctx: CommandContext) -> str:
    pm = ctx.pm
    cash_cny = float(pm.portfolio.get("cash_cny", 0))
    aud_cash = float(pm.portfolio.get("aud_cash", 0))
    ndq_shares = float(pm.portfolio.get("ndq_shares", 0))
    gold_grams = float(pm.portfolio.get("gold_grams", 0))
    gold_avg_cost = float(pm.portfolio.get("gold_avg_cost_cny_per_gram", 0))

    snap = get_gold_snapshot(offset_pct=0.0)
    if snap:
        gold_value = snap.spot_cny_per_gram * gold_grams
        gold_pnl = (snap.spot_cny_per_gram - gold_avg_cost) * gold_grams if gold_avg_cost else 0
    else:
        gold_value = 0
        gold_pnl = 0

    ndq_df = get_history_data("NDQ.AX", "1d")
    ndq_price = float(ndq_df["Close"].iloc[-1]) if not ndq_df.empty else 0

    return (
        f"💰 当前持仓\n"
        f"━━━━━━━━━━━━\n"
        f"现金\n"
        f"  CNY: ¥{cash_cny:,.2f}\n"
        f"  AUD: ${aud_cash:,.2f}\n"
        f"\n"
        f"NDQ.AX: {ndq_shares} 股 @ ${ndq_price:.2f}\n"
        f"\n"
        f"黄金 (浙商积存金): {gold_grams:.4f}g\n"
        f"  均价: ¥{gold_avg_cost:.2f}/g\n"
        f"  现值: ¥{gold_value:,.2f}\n"
        f"  浮盈: ¥{gold_pnl:+,.2f}\n"
    )


@cmd("strategy")
def _strategy(ctx: CommandContext) -> str:
    targets = ctx.pm.strategy.get("target_assets", [])
    lines = ["📊 投资策略"]
    for a in targets:
        lines.append(
            f"\n• {a.get('display_name', a['symbol'])} ({a['symbol']})"
            f"\n  渠道: {a.get('channel', 'N/A')}"
            f"\n  单次上限: ¥{a.get('max_single_invest_cny', 0):,}"
        )
        if "price_offset_pct" in a:
            lines.append(f"\n  浙商点差: {a['price_offset_pct']*100:.2f}%")
        if "sell_fee_pct" in a:
            lines.append(f"\n  卖出手续费: {a['sell_fee_pct']*100:.2f}%")
    return "".join(lines)


@cmd("gold")
def _gold(ctx: CommandContext) -> str:
    # 先取 strategy 里的 offset
    targets = ctx.pm.strategy.get("target_assets", [])
    gold_a = next((a for a in targets if a.get("symbol") == "GC=F"), None)
    offset = float(gold_a.get("price_offset_pct", 0.0)) if gold_a else 0.0
    snap = get_gold_snapshot(offset_pct=offset)
    if snap is None:
        return "❌ 黄金数据获取失败"
    return f"🪙 {format_gold_report(snap)}"


@cmd("ndq")
def _ndq(ctx: CommandContext) -> str:
    df = get_history_data("NDQ.AX", "5d")
    if df.empty:
        return "❌ NDQ.AX 数据获取失败"
    last = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[-2]) if len(df) > 1 else last
    pct = (last / prev - 1) * 100
    return (
        f"📈 NDQ.AX\n"
        f"价格: ${last:.2f}\n"
        f"日变化: {pct:+.2f}%\n"
        f"日期: {df.index[-1].strftime('%Y-%m-%d')}"
    )


@cmd("history")
def _history(ctx: CommandContext) -> str:
    n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 5
    rows = ctx.pm.store.read_history()[-n:]
    if not rows:
        return "暂无交易记录"
    lines = [f"📜 最近 {len(rows)} 笔："]
    for r in rows:
        ts = r.get("ts_origin", r.get("ts", ""))[:19]
        lines.append(
            f"  [{ts}] {r.get('action')} {r.get('units')} "
            f"{r.get('symbol')} @ ¥{r.get('price_per_unit', 0):.2f}"
        )
    return "\n".join(lines)


# ----- 修改类 -----

@cmd("deposit")
def _deposit(ctx: CommandContext) -> str:
    if not ctx.args:
        return "用法: /deposit <CNY金额>"
    try:
        amount = float(ctx.args[0])
    except ValueError:
        return "金额格式错误"
    # RMW 在单锁内完成，scheduler 同时写不会丢这次存款
    with ctx.pm.with_portfolio_tx() as p:
        new_cash = float(p.get("cash_cny", 0)) + amount
        p["cash_cny"] = new_cash
    ctx.pm._reload()
    return f"✅ 已存入 ¥{amount:,.2f}，现金余额 ¥{new_cash:,.2f}"


@cmd("withdraw")
def _withdraw(ctx: CommandContext) -> str:
    if not ctx.args:
        return "用法: /withdraw <CNY金额>"
    try:
        amount = float(ctx.args[0])
    except ValueError:
        return "金额格式错误"
    with ctx.pm.with_portfolio_tx() as p:
        new_cash = float(p.get("cash_cny", 0)) - amount
        p["cash_cny"] = new_cash
    ctx.pm._reload()
    return f"✅ 已扣减 ¥{amount:,.2f}，现金余额 ¥{new_cash:,.2f}"


GOLD_BUY_RE = re.compile(r"([\d.]+)\s*g?\s*@\s*([\d.]+)")


@cmd("gold_buy")
def _gold_buy(ctx: CommandContext) -> str:
    match = GOLD_BUY_RE.search(ctx.raw)
    if not match:
        return "用法: /gold_buy 12.5g @1040"
    grams = float(match.group(1))
    price = float(match.group(2))
    total = grams * price

    # RMW: 拿锁里读旧 grams + avg_cost，算加权均价，写回，避免被 scheduler 插入
    with ctx.pm.with_portfolio_tx() as p:
        cur_grams = float(p.get("gold_grams", 0))
        cur_avg = float(p.get("gold_avg_cost_cny_per_gram", 0))
        new_grams = cur_grams + grams
        new_avg = (
            (cur_avg * cur_grams + price * grams) / new_grams if new_grams else price
        )
        p["gold_grams"] = round(new_grams, 4)
        p["gold_avg_cost_cny_per_gram"] = round(new_avg, 2)

    # 历史 jsonl 是独立 append-only 文件，自带锁，放 portfolio 锁外
    ctx.pm.store.append_history({
        "ts_origin": datetime.now().isoformat(timespec="seconds"),
        "action": "bought", "symbol": "GOLD-CNY", "channel": "浙商积存金",
        "units": grams, "price_per_unit": price, "total_amount": total,
        "currency": "CNY", "source": "napcat",
    })
    ctx.pm._reload()
    return (
        f"✅ 买入 {grams}g @ ¥{price}/g (¥{total:,.2f})\n"
        f"持仓 {new_grams:.4f}g，均价 ¥{new_avg:.2f}/g"
    )


@cmd("gold_sell")
def _gold_sell(ctx: CommandContext) -> str:
    match = GOLD_BUY_RE.search(ctx.raw)
    if not match:
        return "用法: /gold_sell 5g @1050"
    grams = float(match.group(1))
    price = float(match.group(2))

    # 找 strategy 里的 sell_fee_pct（strategy 是只读，不需要进 portfolio 锁）
    targets = ctx.pm.strategy.get("target_assets", [])
    gold_a = next((a for a in targets if a.get("symbol") == "GC=F"), None)
    fee_pct = float(gold_a.get("sell_fee_pct", 0.0038)) if gold_a else 0.0038

    gross = grams * price
    fee = gross * fee_pct
    net = gross - fee

    # RMW: 同时改 gold_grams 和 cash_cny，必须在同一锁内
    with ctx.pm.with_portfolio_tx() as p:
        cur_grams = float(p.get("gold_grams", 0))
        new_grams = max(0.0, cur_grams - grams)
        cur_cash = float(p.get("cash_cny", 0))
        new_cash = cur_cash + net
        p["gold_grams"] = round(new_grams, 4)
        p["cash_cny"] = round(new_cash, 2)

    ctx.pm.store.append_history({
        "ts_origin": datetime.now().isoformat(timespec="seconds"),
        "action": "sold", "symbol": "GOLD-CNY", "channel": "浙商积存金",
        "units": grams, "price_per_unit": price, "total_amount": gross,
        "fee": round(fee, 2), "net_amount": round(net, 2),
        "currency": "CNY", "source": "napcat",
    })
    ctx.pm._reload()
    return (
        f"✅ 卖出 {grams}g @ ¥{price}/g\n"
        f"毛收入 ¥{gross:,.2f} - 手续费 ¥{fee:,.2f} = 净 ¥{net:,.2f}\n"
        f"剩余 {new_grams:.4f}g，现金 ¥{new_cash:,.2f}"
    )


@cmd("gold_set")
def _gold_set(ctx: CommandContext) -> str:
    if not ctx.args:
        return "用法: /gold_set 124.5"
    try:
        grams = float(ctx.args[0])
    except ValueError:
        return "克数格式错误"
    # 直接设克数也走 transaction：body 重渲染要看其它字段，必须在同一锁内
    with ctx.pm.with_portfolio_tx() as p:
        p["gold_grams"] = round(grams, 4)
    ctx.pm._reload()
    return f"✅ 黄金克数已直接设为 {grams}g（成本均价不变）"


@cmd("gold_offset")
def _gold_offset(ctx: CommandContext) -> str:
    if not ctx.args:
        return "用法: /gold_offset <浙商克价>  (例: /gold_offset 1040)"
    try:
        bank_price = float(ctx.args[0])
    except ValueError:
        return "价格格式错误"

    offset = infer_offset_pct(bank_price)
    if offset is None:
        return "❌ 无法获取实时金价反推"

    targets = list(ctx.pm.strategy.get("target_assets", []))
    for a in targets:
        if a.get("symbol") == "GC=F":
            a["price_offset_pct"] = round(offset, 4)

    new_data = {
        "target_assets": targets,
        "target_allocation_stock": ctx.pm.strategy.get("target_allocation_stock", 0.7),
        "target_allocation_cash": ctx.pm.strategy.get("target_allocation_cash", 0.3),
    }
    ctx.pm.store.write("strategy", "strategy", new_data, ctx.pm.strategy.body)
    ctx.pm._reload()
    return (
        f"✅ 浙商点差已更新: {offset*100:+.2f}%\n"
        f"(用户报 ¥{bank_price}/g，反推现货 spot 后写回 strategy.md)"
    )


@cmd("risk")
def _risk(ctx: CommandContext) -> str:
    if not ctx.args:
        return "用法: /risk <conservative|balanced|aggressive>"
    level = ctx.args[0].lower()
    mapping = {"conservative": "Conservative", "balanced": "Balanced", "aggressive": "Aggressive"}
    if level not in mapping:
        return "支持值: conservative / balanced / aggressive"
    ctx.pm.store.update_fields("user", risk_tolerance=mapping[level])
    ctx.pm._reload()
    return f"✅ 风险偏好已调整为 {mapping[level]}"


# ----- 触发类 -----

@cmd("payday")
def _payday(ctx: CommandContext) -> str:
    from jobs.payday_check import run as payday_run
    result = payday_run()
    return f"💰 payday_check 结果: {result}"


@cmd("run")
def _run(ctx: CommandContext) -> str:
    """异步触发 daily_report，立即返回。"""
    def _bg():
        try:
            from jobs.daily_report import run as dr_run
            result = dr_run()
            send_private_msg(ctx.user_id, f"✅ daily_report 完成: {result}")
        except Exception as e:
            send_private_msg(ctx.user_id, f"❌ daily_report 失败: {e}")

    threading.Thread(target=_bg, daemon=True).start()
    return "🚀 daily_report 已在后台启动 (~6 分钟)，完成后会推送结果"


# ========== 路由 ==========

def route(raw: str, user_id: int) -> str:
    """解析消息，执行命令，返回响应文本"""
    raw = raw.strip()
    if not raw.startswith("/"):
        return ("ℹ️ 我只支持 /命令 格式（自然语言留给 Claude Skill 模式）。\n"
                "发 /help 看命令清单。")

    parts = raw[1:].split(maxsplit=1)
    cmd_name = parts[0].lower()
    args = parts[1].split() if len(parts) > 1 else []

    handler = COMMANDS.get(cmd_name)
    if handler is None:
        return f"❌ 未知命令 /{cmd_name}，发 /help 看清单"

    pm = PortfolioManager()  # 每次重新读 memory，确保数据最新
    ctx = CommandContext(pm=pm, user_id=user_id, raw=raw, args=args)
    try:
        return handler(ctx)
    except Exception as e:
        log.exception(f"command /{cmd_name} failed")
        return f"❌ 命令执行失败: {type(e).__name__}: {e}"


# ========== WebSocket 主循环 ==========

async def _handle_event(event: Dict[str, Any]) -> None:
    if event.get("post_type") != "message":
        return
    if event.get("message_type") != "private":
        return
    user_id = event.get("user_id")
    if user_id != WHITELIST_QQ:
        log.warning(f"非白名单 QQ {user_id} 私聊，已忽略")
        return

    raw = event.get("raw_message") or event.get("message") or ""
    if not isinstance(raw, str):
        raw = str(raw)
    log.info(f"[{user_id}] {raw}")

    response = route(raw, user_id)
    send_private_msg(user_id, response)


async def main_loop() -> None:
    log.info(f"连接 NapCat WS: {NAPCAT_WS_URL} (白名单 QQ: {WHITELIST_QQ})")
    while True:
        try:
            async with websockets.connect(NAPCAT_WS_URL, ping_interval=30) as ws:
                log.info("WebSocket 已连接")
                async for raw_msg in ws:
                    try:
                        event = json.loads(raw_msg)
                        await _handle_event(event)
                    except Exception as e:
                        log.exception(f"事件处理失败: {e}")
        except Exception as e:
            log.warning(f"WS 断开: {e}，5 秒后重连")
            await asyncio.sleep(5)


def main() -> None:
    asyncio.run(main_loop())


if __name__ == "__main__":
    main()
