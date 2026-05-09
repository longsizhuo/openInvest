"""NapCat 私聊 connector - QQ 命令式交互入口

设计要点：
- 只响应白名单 QQ（必须通过 INVEST_WHITELIST_QQ env 配置；未配置时默认 0
  = 拒绝所有，audit security m1 修复，避免公开仓库泄露个人 QQ）
- 命令格式 `/cmd args` —— 不依赖 LLM 解析，零 token 成本
  （自然语言交互留给 P5: Claude Skill）
- 长跑 daemon，建议 `nohup python -m connectors.napcat_bot &` 或 systemd

支持命令：
  /help                                  显示帮助
  /balance                               当前持仓 + 现金 + 黄金估值
  /strategy                              当前策略与目标资产
  /gold                                  实时伦敦金 + 渠道参考价（含点差）
  /price <symbol>                        通用现价查询（例 /price 510300.SS）
  /ndq                                   deprecated：等同 /price NDQ.AX
  /history [N]                           最近 N 笔交易（默认 5）
  /deposit <amount_cny>                  增加 CNY 现金（工资/转入）
  /withdraw <amount_cny>                 减少 CNY 现金
  /gold_buy <grams> @<price>             记录黄金买入
  /gold_sell <grams> @<price>            记录黄金卖出
  /gold_set <grams>                      直接设置黄金克数
  /gold_offset <bank_price>              报当日实际买入克价，自动反推渠道点差写回 strategy
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
from typing import Any, Callable, Dict, List, Optional, Tuple

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
WHITELIST_QQ = int(os.getenv("INVEST_WHITELIST_QQ", "0"))  # 必须 env 配置，0 = 拒绝所有

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
        "/gold — 实时金价 + 渠道参考\n"
        "/price <symbol> — 通用现价查询\n"
        "/ndq — 等同 /price NDQ.AX (deprecated)\n"
        "/history [N] — 最近 N 笔交易\n"
        "/deposit <数额> — CNY 入账\n"
        "/withdraw <数额> — CNY 出账\n"
        "/gold_buy <克数> @<克价> — 记买入\n"
        "/gold_sell <克数> @<克价> — 记卖出\n"
        "/gold_set <克数> — 直接覆盖黄金克数\n"
        "/gold_offset <当日克价> — 反推渠道点差\n"
        "/risk <conservative|balanced|aggressive>\n"
        "/payday — 月度入账\n"
        "/run — 异步触发 daily_report"
    )


@cmd("balance")
def _balance(ctx: CommandContext) -> str:
    """v2 通用化：遍历 holdings 列表显示，不再硬编码 NDQ.AX + GC=F 两条"""
    pm = ctx.pm
    cash_cny = pm.cash_amount("CNY")
    aud_cash = pm.cash_amount("AUD")

    lines = [
        "💰 当前持仓",
        "━━━━━━━━━━━━",
        "现金",
        f"  CNY: ¥{cash_cny:,.2f}",
    ]
    if aud_cash > 0:
        lines.append(f"  AUD: ${aud_cash:,.2f}")
    # 把其他币种现金也展示出来（USD/HKD 等 fork 用户场景）
    for ccy, amount in (pm.cash or {}).items():
        if ccy in ("CNY", "AUD"):
            continue
        if float(amount) > 0:
            lines.append(f"  {ccy}: {amount:,.2f}")

    if not list(pm.holdings):
        lines.append("\n持仓: (无)")
        return "\n".join(lines) + "\n"

    snap = get_gold_snapshot(offset_pct=0.0)

    for h in pm.holdings:
        sym = str(h.get("symbol", "?"))
        units = float(h.get("units", 0) or 0)
        avg = float(h.get("avg_cost", 0) or 0)
        ccy = str(h.get("cost_currency", "CNY"))
        unit_label = str(h.get("unit_label", ""))
        display = h.get("display_name") or sym
        kind = str(h.get("kind", ""))

        lines.append("")
        # 黄金类按克现价折算 CNY
        if kind == "metal" and snap is not None:
            value = snap.spot_cny_per_gram * units
            pnl = (snap.spot_cny_per_gram - avg) * units if avg else 0
            lines.append(f"{display} ({sym}): {units:.4f}{unit_label}")
            if avg:
                lines.append(f"  均价: ¥{avg:.2f}/{unit_label or '克'}")
                lines.append(f"  现值: ¥{value:,.2f}")
                lines.append(f"  浮盈: ¥{pnl:+,.2f}")
            else:
                lines.append(f"  现值: ¥{value:,.2f}")
            continue

        # 其他类按 yfinance close 取最近价
        df = get_history_data(sym, "5d")
        cur_price = float(df["Close"].iloc[-1]) if not df.empty else 0.0
        unit_sign = "$" if ccy in ("USD", "AUD", "HKD") else "¥"
        lines.append(f"{display} ({sym}): {units}{unit_label}")
        if avg:
            lines.append(f"  均价: {unit_sign}{avg:.4f} {ccy}")
        if cur_price > 0:
            lines.append(f"  现价: {unit_sign}{cur_price:.2f} {ccy}")
            if avg:
                pnl_pct = ((cur_price / avg) - 1) * 100
                lines.append(f"  浮盈: {pnl_pct:+.2f}%")

    return "\n".join(lines) + "\n"


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
            # 旧"浙商点差"是作者偏好；通用化用"渠道点差"
            lines.append(f"\n  渠道点差: {a['price_offset_pct']*100:.2f}%")
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
        return "❌ 黄金数据获取失败（可能是 yfinance 限流或网络问题，稍候重试）"
    return f"🪙 {format_gold_report(snap)}"


@cmd("price")
def _price(ctx: CommandContext, *args: str) -> str:
    """`/price <symbol>` 通用现价查询（旧 /ndq 是 NDQ.AX 专属，被 /price 取代）"""
    if not args:
        # 缺参数兜底：如果用户有持仓就给一句友好提示
        holdings = list(ctx.pm.holdings)
        if holdings:
            samples = ", ".join(h.get("symbol", "?") for h in holdings[:3])
            return f"用法: /price <symbol>  (你的持仓: {samples})"
        return "用法: /price <symbol>  (例: /price NDQ.AX 或 /price 510300.SS 或 /price BTC-USD)"
    sym = args[0].upper()
    df = get_history_data(sym, "5d")
    if df.empty:
        return f"❌ {sym} 数据获取失败（symbol 不存在 / yfinance 限流 / 网络问题）"
    last = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[-2]) if len(df) > 1 else last
    pct = (last / prev - 1) * 100
    return (
        f"📈 {sym}\n"
        f"价格: {last:.4f}\n"
        f"日变化: {pct:+.2f}%\n"
        f"日期: {df.index[-1].strftime('%Y-%m-%d')}"
    )


@cmd("ndq")
def _ndq(ctx: CommandContext) -> str:
    """deprecated: NDQ.AX 专属命令，请改用 `/price NDQ.AX`。保留给老用户"""
    return _price(ctx, "NDQ.AX")


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
    """v2: 存入 CNY。/deposit <amount> 默认 CNY；/deposit <ccy> <amount> 任意币种"""
    if not ctx.args:
        return "用法: /deposit <CNY金额> 或 /deposit USD 100"
    # 解析币种 + 金额（兼容旧的单参 = CNY）
    if len(ctx.args) >= 2:
        ccy = ctx.args[0].upper()
        try:
            amount = float(ctx.args[1])
        except ValueError:
            return "金额格式错误"
    else:
        ccy = "CNY"
        try:
            amount = float(ctx.args[0])
        except ValueError:
            return "金额格式错误"

    with ctx.pm.with_portfolio_tx() as p:
        cash = dict(p.get("cash") or {})
        new_balance = float(cash.get(ccy, 0) or 0) + amount
        cash[ccy] = round(new_balance, 2)
        p["cash"] = cash
    ctx.pm._reload()
    return f"✅ 已存入 {ccy} {amount:,.2f}，新余额 {new_balance:,.2f}"


@cmd("withdraw")
def _withdraw(ctx: CommandContext) -> str:
    """v2: 取出 CNY。/withdraw <amount> 或 /withdraw <ccy> <amount>。余额不足拒绝"""
    if not ctx.args:
        return "用法: /withdraw <CNY金额> 或 /withdraw USD 100"
    if len(ctx.args) >= 2:
        ccy = ctx.args[0].upper()
        try:
            amount = float(ctx.args[1])
        except ValueError:
            return "金额格式错误"
    else:
        ccy = "CNY"
        try:
            amount = float(ctx.args[0])
        except ValueError:
            return "金额格式错误"

    cur = ctx.pm.cash_amount(ccy)
    if cur < amount:
        return f"❌ {ccy} 余额不足：当前 {cur:.2f}，要扣 {amount:.2f}（防误操作）"

    with ctx.pm.with_portfolio_tx() as p:
        cash = dict(p.get("cash") or {})
        new_balance = float(cash.get(ccy, 0) or 0) - amount
        cash[ccy] = round(new_balance, 2)
        p["cash"] = cash
    ctx.pm._reload()
    return f"✅ 已扣减 {ccy} {amount:,.2f}，新余额 {new_balance:,.2f}"


GOLD_BUY_RE = re.compile(r"([\d.]+)\s*g?\s*@\s*([\d.]+)")


def _gold_defaults(pm: "PortfolioManager") -> Tuple[str, str]:
    """计算"创建黄金 holding"时用的默认 (channel, display_name)

    优先级：
      1. strategy.target_assets[GC=F].channel / display_name（用户在 GUI/策略里配的）
      2. INVEST_GOLD_CHANNEL / INVEST_GOLD_DISPLAY env（fork 用户最简配置点）
      3. 通用兜底"黄金（自营）" / "实物黄金"
    避免硬编码"浙商积存金"——非作者用户用工行/招行/华安 ETF 等渠道时账目从第一笔就错。
    """
    targets = list(pm.strategy.get("target_assets", []) or [])
    gold_cfg = next((a for a in targets if a.get("symbol") == "GC=F"), None)
    if gold_cfg:
        ch = str(gold_cfg.get("channel") or "").strip()
        dn = str(gold_cfg.get("display_name") or "").strip()
        if ch and dn:
            return ch, dn
    env_ch = os.getenv("INVEST_GOLD_CHANNEL", "").strip()
    env_dn = os.getenv("INVEST_GOLD_DISPLAY", "").strip()
    if env_ch and env_dn:
        return env_ch, env_dn
    return "黄金（自营）", "实物黄金"


@cmd("gold_buy")
def _gold_buy(ctx: CommandContext) -> str:
    match = GOLD_BUY_RE.search(ctx.raw)
    if not match:
        return "用法: /gold_buy 12.5g @1040"
    grams = float(match.group(1))
    price = float(match.group(2))
    total = grams * price

    # v2 RMW: holdings.find("GC=F") + 加权均价；克数 + 均价必须在同一锁内
    channel, display_name = _gold_defaults(ctx.pm)
    with ctx.pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        gold = next((h for h in holdings if h.get("symbol") == "GC=F"), None)
        cur_grams = float(gold.get("units", 0) or 0) if gold else 0.0
        cur_avg = float(gold.get("avg_cost", 0) or 0) if gold else 0.0
        new_grams = cur_grams + grams
        new_avg = (
            (cur_avg * cur_grams + price * grams) / new_grams if new_grams else price
        )
        if gold:
            gold["units"] = round(new_grams, 4)
            gold["avg_cost"] = round(new_avg, 2)
        else:
            holdings.append({
                "symbol": "GC=F", "kind": "metal",
                "units": round(new_grams, 4), "unit_label": "克",
                "avg_cost": round(new_avg, 2), "cost_currency": "CNY",
                "channel": channel, "display_name": display_name,
                "yfinance_proxy": "GC=F", "proxy_kind": "gold_cny_per_gram",
                "sell_fee_pct": 0.0038,
            })
        p["holdings"] = holdings

    # 历史 jsonl 是独立 append-only 文件，自带锁，放 portfolio 锁外
    ctx.pm.store.append_history({
        "ts_origin": datetime.now().isoformat(timespec="seconds"),
        "action": "bought", "symbol": "GOLD-CNY", "channel": channel,
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
    channel, _ = _gold_defaults(ctx.pm)

    gross = grams * price
    fee = gross * fee_pct
    net = gross - fee

    # v2 RMW: holdings GC=F 减克数 + cash CNY 加现金，同一锁内
    with ctx.pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        gold = next((h for h in holdings if h.get("symbol") == "GC=F"), None)
        cur_grams = float(gold.get("units", 0) or 0) if gold else 0.0
        if cur_grams < grams:
            raise RuntimeError(
                f"卖出克数 {grams} 超过持仓 {cur_grams}（防误操作）",
            )
        new_grams = round(cur_grams - grams, 4)
        if gold:
            gold["units"] = new_grams
        cash = dict(p.get("cash") or {})
        cur_cash = float(cash.get("CNY", 0) or 0)
        new_cash = round(cur_cash + net, 2)
        cash["CNY"] = new_cash
        p["holdings"] = holdings
        p["cash"] = cash

    ctx.pm.store.append_history({
        "ts_origin": datetime.now().isoformat(timespec="seconds"),
        "action": "sold", "symbol": "GOLD-CNY", "channel": channel,
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
    # v2: 直接覆盖 GC=F holding 的 units（均价不变，不计流水）
    channel, display_name = _gold_defaults(ctx.pm)
    with ctx.pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        gold = next((h for h in holdings if h.get("symbol") == "GC=F"), None)
        if gold:
            gold["units"] = round(grams, 4)
        else:
            holdings.append({
                "symbol": "GC=F", "kind": "metal",
                "units": round(grams, 4), "unit_label": "克",
                "avg_cost": 0.0, "cost_currency": "CNY",
                "channel": channel, "display_name": display_name,
                "yfinance_proxy": "GC=F", "proxy_kind": "gold_cny_per_gram",
                "sell_fee_pct": 0.0038,
            })
        p["holdings"] = holdings
    ctx.pm._reload()
    return f"✅ 黄金克数已直接设为 {grams}g（成本均价不变）"


@cmd("gold_offset")
def _gold_offset(ctx: CommandContext) -> str:
    if not ctx.args:
        return "用法: /gold_offset <当日实际买入克价>  (例: /gold_offset 1040)"
    try:
        bank_price = float(ctx.args[0])
    except ValueError:
        return "价格格式错误"

    offset = infer_offset_pct(bank_price)
    if offset is None:
        return "❌ 无法获取实时金价反推（可能是 yfinance 限流，稍候重试）"

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
        f"✅ 渠道点差已更新: {offset*100:+.2f}%\n"
        f"(用户报当日买入价 ¥{bank_price}/g，反推现货 spot 后写回 strategy.md)"
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
