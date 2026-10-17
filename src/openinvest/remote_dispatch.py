"""INVEST_API_BASE 远端模式 —— skill.py 子命令的统一 HTTP 分发（hub-and-spoke）

设计：
- 表驱动路由：command → handler；输出与本地模式同形状的 JSON（agent / SKILL.md
  协议零感知），错误也映射回 CLI 同款 {"status": "error", "error", "hint"}
- 本地白名单：live_prices / correlate 纯 yfinance、不碰 memory → 返回 False 继续本地跑
- 远端禁用：init（数据在 hub）/ event_check --recall|--live → 结构化 error + hint
- 命令既不在路由表也不在白名单 → 显式报错。**默认拒绝**：新加的子命令必须显式
  归类，避免远端模式下意外读写客户端本地 memory/
- 鉴权 header：INVEST_API_TOKEN → Authorization: Bearer；CF_ACCESS_CLIENT_ID/SECRET →
  CF-Access-Client-Id/Secret（Cloudflare Tunnel + Service Token 部署）。token 不打日志

钩子在 scripts/skill.py:main()：parse_args 之后、args.func 之前——必须发生在任何
MemoryStore()/PortfolioManager() 实例化前（MemoryStore.__init__ 会 mkdir memory/，
客户端机器不应产生该目录）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any, Callable, Dict, Optional

import requests
from openinvest.utils.symbols import safe_symbol

# 纯本地命令：不碰 memory/，远端模式下照常本地跑
LOCAL_WHITELIST = {"live_prices", "correlate"}

# 远端禁用命令 → (error, hint)
DISABLED = {
    "init": (
        "远端模式下 init 已禁用——数据在 hub 上，客户端不需要 onboarding",
        "本机 .env 只需要 INVEST_API_BASE（+ INVEST_API_TOKEN / CF service token），"
        "然后跑 `run.sh doctor` 验证连通。要重置数据去 hub 机器上跑 `run.sh init`。",
    ),
}

_POLL_INTERVAL_S = 2
_POLL_MAX_S = 20 * 60   # 与 hub SSE 上限对齐（20 min）


def _print_json(obj: Any) -> None:
    """与 scripts/skill.py._print_json 同款：直写原始 stdout"""
    real_stdout = getattr(sys, "__stdout__", sys.stdout)
    real_stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    real_stdout.write("\n")
    real_stdout.flush()


def _api_base() -> str:
    return os.getenv("INVEST_API_BASE", "").strip().rstrip("/")


def _headers() -> Dict[str, str]:
    h: Dict[str, str] = {}
    token = os.getenv("INVEST_API_TOKEN", "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    cf_id = os.getenv("CF_ACCESS_CLIENT_ID", "").strip()
    cf_secret = os.getenv("CF_ACCESS_CLIENT_SECRET", "").strip()
    if cf_id and cf_secret:
        h["CF-Access-Client-Id"] = cf_id
        h["CF-Access-Client-Secret"] = cf_secret
    return h


def _die(err: Dict[str, Any]) -> None:
    _print_json(err)
    sys.exit(1)


def _with_idem(err: Dict[str, Any], key: Optional[str]) -> Dict[str, Any]:
    """"hub 可能已入账、但客户端不知道"的错误（超时 / 连不上 / 5xx / Tunnel 502-524）都回显
    本次幂等键：agent 带同一个 key 重试，hub 只记一次（ADR-016）。生成新 uuid4 重试才会双记。"""
    if key:
        err["idempotency_key"] = key
        err["hint"] += (
            f" 这笔写操作可能已在 hub 入账：重试请带 --idempotency-key {key}，hub 同 key 只记一次"
            f"（hub 须为支持幂等键的版本——`run.sh doctor` 的 hub `version` ≥ 0.45.1；"
            f"旧 hub 忽略该字段，重试会双记）。"
        )
    return err


def _request(
    method: str,
    path: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 30,
    ok_404: bool = False,
    idempotency_key: Optional[str] = None,
) -> requests.Response:
    """发请求 + 统一错误映射。基础设施错误（连不上/401/5xx）直接 _die 退出；
    业务 4xx 交给 caller 处理（400 detail 多数映射回 CLI 的 status=error）。
    idempotency_key：动钱 POST 传入；网络失败 / 5xx（= 不知道 hub 记没记）时回显在 error JSON
    里，agent 重试带同一个 key，hub 只入账一次（ADR-016）。"""
    base = _api_base()
    url = f"{base}{path}"
    try:
        r = requests.request(
            method, url, json=json_body, params=params,
            headers=_headers(), timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        _die(_with_idem({
            "status": "error",
            "error": f"无法连接 hub {base}: {type(e).__name__}: {e}",
            "hint": (
                "检查 INVEST_API_BASE 是否正确、hub 上 invest-web 服务是否在跑"
                "（hub 机器: systemctl status invest-web）、网络/Tunnel 是否通。"
            ),
        }, idempotency_key))
    if r.status_code == 401:
        _die({
            "status": "error",
            "error": "hub 鉴权失败（401）",
            "hint": (
                "hub 开了 INVEST_API_TOKEN——在本机 .env 设同名 INVEST_API_TOKEN；"
                "走 Cloudflare Access 的话设 CF_ACCESS_CLIENT_ID/CF_ACCESS_CLIENT_SECRET。"
            ),
        })
    if r.status_code == 404 and ok_404:
        return r
    if r.status_code == 503:
        _die(_with_idem({
            "status": "error",
            "error": _detail(r) or "hub 返回 503（memory 未初始化或服务降级）",
            "hint": "去 hub 机器上跑 `run.sh doctor` / `run.sh init` 完成 onboarding。",
        }, idempotency_key))
    if r.status_code >= 500:
        # 事务已提交、随后 append_history/_reload 抛 500，或 Tunnel 502/524 吞掉成功响应：
        # hub 很可能已记账，同样回显 key（#432 评审第 2 条）
        _die(_with_idem({
            "status": "error",
            "error": f"hub 内部错误 HTTP {r.status_code}: {_detail(r) or r.text[:200]}",
            "hint": "看 hub 日志：journalctl -u invest-web -n 100",
        }, idempotency_key))
    # 400 留给调用方（业务错误，CLI 同款 error JSON）；其余 4xx（403 Cloudflare Access 拦截、
    # 404 端点不存在、422 请求体不合法……）原先落到 raise_for_status 抛 traceback（#414 评审）
    if r.status_code >= 400 and r.status_code != 400:
        _die({
            "status": "error",
            "error": f"hub 拒绝了请求 HTTP {r.status_code}: {_detail(r) or r.text[:200]}",
            "hint": "403 多半是 Cloudflare Access 凭据不对（CF_ACCESS_CLIENT_ID/SECRET）；"
                    "404 / 422 多半是 hub 版本比本机旧，先升级 hub",
        })
    return r


def _detail(r: requests.Response) -> str:
    try:
        return str(r.json().get("detail", ""))
    except Exception:  # noqa: BLE001
        return ""


def _print_response_or_die(r: requests.Response) -> None:
    """2xx → 原样打印 body（与本地输出同形状）；400 → CLI 同款 error JSON + exit 1"""
    if r.status_code == 400:
        _die({"status": "error", "error": _detail(r) or r.text[:200]})
    r.raise_for_status()
    _print_json(r.json())


# ---------- 各命令 handler ----------

def _h_status(_: argparse.Namespace) -> None:
    _print_response_or_die(_request("GET", "/api/skill/status", timeout=60))


def _h_strategy(_: argparse.Namespace) -> None:
    _print_response_or_die(_request("GET", "/api/skill/strategy"))


def _h_history(args: argparse.Namespace) -> None:
    _print_response_or_die(_request("GET", "/api/skill/history", params={"n": args.n}))


def _h_what_if(args: argparse.Namespace) -> None:
    _print_response_or_die(_request("POST", "/api/skill/what_if", json_body={
        "symbol": args.symbol, "pct": args.pct, "price": args.price,
        "gold_price": args.gold_price, "gold_pct": args.gold_pct,
        "ndq_price": args.ndq_price, "ndq_pct": args.ndq_pct,
        "audcny": args.audcny,
    }, timeout=120))


def _h_doctor(_: argparse.Namespace) -> None:
    """hub 的 doctor 结果 + 客户端远端段（连通性/鉴权方式），让 agent 一眼看清两端状态"""
    r = _request("GET", "/api/doctor", timeout=30)
    r.raise_for_status()
    out = r.json()
    auth_mode = "none"
    if os.getenv("INVEST_API_TOKEN", "").strip():
        auth_mode = "bearer_token"
    elif os.getenv("CF_ACCESS_CLIENT_ID", "").strip():
        auth_mode = "cf_service_token"
    out["remote"] = {
        "mode": "remote",
        "api_base": _api_base(),
        "hub_reachable": True,
        "auth": auth_mode,
        "note": "以上 checks 是 hub 视角（hub 的 memory/.env/LLM key）；本机只是客户端，"
                "不需要 memory/ 与 LLM key。",
    }
    _print_json(out)


def _idem_key(args: argparse.Namespace) -> str:
    """动钱命令的幂等键：--idempotency-key 显式给了就复用（重试同一笔），否则本次调用生成
    一个 uuid4。hub 端同 key 只入账一次（ADR-016）；网络失败时 _request 把 key 回显给 agent。"""
    return getattr(args, "idempotency_key", None) or str(uuid.uuid4())


def _h_deposit(args: argparse.Namespace) -> None:
    key = _idem_key(args)
    _print_response_or_die(_request("POST", "/api/skill/deposit", json_body={
        "currency": args.currency, "amount": float(args.amount), "idempotency_key": key,
    }, idempotency_key=key))


def _h_withdraw(args: argparse.Namespace) -> None:
    key = _idem_key(args)
    _print_response_or_die(_request("POST", "/api/skill/withdraw", json_body={
        "currency": args.currency, "amount": float(args.amount), "idempotency_key": key,
    }, idempotency_key=key))


def _h_buy(args: argparse.Namespace) -> None:
    if getattr(args, "existing_position", False):
        # /api/skill/buy 没有这个字段——静默转发会在 hub 上扣现金，宁可拒绝
        _die({
            "status": "error",
            "error": "--existing-position 暂不支持远端模式（INVEST_API_BASE 已设置）",
            "hint": "在 hub 机器上跑 `run.sh buy ... --existing-position`，"
                    "或用 remote MCP 的 `record_existing_position` 工具。",
        })
    key = _idem_key(args)
    body = {
        "symbol": args.symbol, "units": float(args.units), "price": float(args.price),
        "kind": args.kind, "unit_label": args.unit_label, "idempotency_key": key,
    }
    # 不写 --currency 就不带这个键（不发 null）：新 hub 按持仓币种记；还没升级的 hub
    # 回落到它自己的默认 CNY，至少不会因为 null 校验失败而 422
    if args.currency:
        body["currency"] = args.currency
    _print_response_or_die(_request("POST", "/api/skill/buy", json_body=body, idempotency_key=key))


def _h_sell(args: argparse.Namespace) -> None:
    key = _idem_key(args)
    _print_response_or_die(_request("POST", "/api/skill/sell", json_body={
        "symbol": args.symbol, "units": float(args.units), "price": float(args.price),
        "idempotency_key": key,
    }, idempotency_key=key))


def _h_delete_holding(args: argparse.Namespace) -> None:
    _print_response_or_die(_request("POST", "/api/skill/delete_holding", json_body={
        "symbol": args.symbol, "force": bool(args.force),
    }))


def _h_prepare_committee(args: argparse.Namespace) -> None:
    # prepare 在 hub 拉 2y 行情 + 事实块，秒级到几十秒
    _print_response_or_die(_request(
        "POST", "/api/committee/prepare",
        json_body={"symbol": args.symbol}, timeout=180,
    ))


def _h_save_committee(args: argparse.Namespace) -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        _print_json({"error": "empty stdin"})   # 与本地行为一致（exit 0）
        return
    _print_response_or_die(_request(
        "POST", "/api/committee/save",
        json_body={"symbol": args.symbol, "transcript": raw}, timeout=120,
    ))


def _h_event_check(args: argparse.Namespace) -> None:
    if args.recall:
        _die({
            "status": "error",
            "error": "远端模式不支持 event_check --recall（事件库在 hub 上）",
            "hint": "去 hub 机器上跑 `run.sh event_check --recall SYM`。",
        })
    if args.live:
        _die({
            "status": "error",
            "error": "远端模式不支持 event_check --live（hub 的 cron 每 30 分钟已自动跑 live 链路）",
            "hint": "手动扫描用不带 --live 的 event_check（等价 hub 的 POST /api/events/check）。",
        })
    # 注意：hub 端点返回的是 EventCheckResponse 摘要（fetched/new_events/triggered），
    # 比本地 dry-run 的完整 dump 精简——agent 要细节可以追问 /api/events/recent
    _print_response_or_die(_request("POST", "/api/events/check", timeout=180))


def _h_explain_decision(args: argparse.Namespace) -> None:
    """转发 hub 现有的单条决议端点，拼成本地 explain_decision 同形状。
    该端点不带 path snapshot（web API 不再新增端点）→ path_snapshot 恒为 None；
    要 snapshot 用 hub 的 remote MCP explain_decision。"""
    from openinvest.core.decision_ledger import parse_committee_text, parse_decision_id
    try:
        date, symbol = parse_decision_id(args.decision_id)
    except ValueError as e:
        _die({"status": "error", "error": str(e)})
    r = _request("GET", f"/api/committee_sessions/{date}/{safe_symbol(symbol)}",
                 timeout=15, ok_404=True)
    if r.status_code == 404:
        _die({"status": "error", "error": f"decision {args.decision_id} not found on hub"})
    r.raise_for_status()
    body = r.json()
    md = body.get("content") or ""
    parsed = parse_committee_text(md)
    if not parsed:
        _die({"status": "error", "error": f"decision {args.decision_id} has no verdict on hub"})
    _print_json({
        "decision_id": args.decision_id,
        **{k: parsed[k] for k in ("verdict", "confidence", "alloc_cny", "alloc_currency")},
        "confidence_lookup": body.get("confidence_lookup"),   # 旧 hub 没这个字段 → None
        "transcript_markdown": md,
        "path_snapshot": None,
    })


def _hub_today(symbol_tz_fallback: bool = True) -> str:
    """用 hub 的 /api/health 时间戳推"今天"——避免客户端/hub 时区错位导致同日 cache 失效"""
    r = _request("GET", "/api/health", timeout=15)
    r.raise_for_status()
    ts = str(r.json().get("timestamp", ""))
    if len(ts) >= 10:
        return ts[:10]
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


def _h_run_committee(args: argparse.Namespace) -> None:
    """Direct 路径远端化：触发 hub 跑 → 轮询 → 输出与本地 run_committee 同款字段

    LLM key 只在 hub 的 .env；进度打 stderr，最终 JSON 打 stdout。
    """
    symbol = args.symbol
    safe_sym = safe_symbol(symbol)

    # Stage 0：同日 cache（hub 日期口径）。--force 跳过
    if not args.force:
        today = _hub_today()
        r = _request(
            "GET", f"/api/committee_sessions/{today}/{safe_sym}",
            timeout=15, ok_404=True,
        )
        if r.status_code == 200:
            from openinvest.capabilities.committee.i18n import bilingual
            _print_json({
                "status": "cached",
                "reason": bilingual(
                    "今天已经跑过这个资产了；用 --force 重跑",
                    "This asset already ran today; pass --force to rerun.",
                ),
                "transcript_path": f"(hub) memory/.committee/{today}/{safe_sym}.md",
                "transcript_md": r.json().get("content", ""),
                # 与 fresh 路径 / 本地 CLI 同款；旧 hub 没这个字段 → None
                "confidence_lookup": r.json().get("confidence_lookup"),
            })
            return

    # 触发
    r = _request("POST", "/api/committee/run", json_body={
        "symbols": [symbol],
        "max_debate_rounds": args.max_rounds,
        "note": "skill remote",
    })
    r.raise_for_status()
    task_id = r.json()["task_id"]
    print(f"[remote] committee task {task_id} 已在 hub 排队，轮询中…", file=sys.stderr)

    # 轮询
    deadline = time.time() + _POLL_MAX_S
    last_phase = ""
    final: Optional[Dict[str, Any]] = None
    while time.time() < deadline:
        s = _request("GET", f"/api/committee/{task_id}", timeout=15)
        s.raise_for_status()
        status = s.json()
        phase = str(status.get("phase") or status.get("status") or "")
        if phase != last_phase:
            last_phase = phase
            print(f"[remote] {phase}", file=sys.stderr)
        if status.get("status") in ("done", "error"):
            final = status
            break
        time.sleep(_POLL_INTERVAL_S)

    if final is None:
        _die({
            "status": "error",
            "error": f"hub 委员会超过 {_POLL_MAX_S // 60} 分钟未完成（task {task_id}）",
            "hint": f"稍后查 GET {_api_base()}/api/committee/{task_id}，或看 hub 日志。",
        })
    if final.get("status") == "error":
        _die({
            "status": "error",
            "error": f"hub 委员会失败: {final.get('error')}",
            "hint": "检查 hub 的 LLM key / 行情数据；hub 日志: journalctl -u invest-web",
        })

    by_asset = ((final.get("result") or {}).get("by_asset") or {})
    asset_result = by_asset.get(symbol) or {}
    if asset_result.get("error"):
        _die({
            "status": "error",
            "error": asset_result["error"],
            "hint": "session 内单资产失败，检查 hub 的行情数据 / LLM key",
        })

    gui_url = _api_base()
    today = _hub_today()
    # 用 hub 实际跑委员会时的语言（result.language），而不是本机的 get_invest_lang()——
    # remote hub 模式下 cio_memo/verdict 是 hub 生成的，语言由 hub 自己的 INVEST_LANG 决定，
    # 跟本机配置可能不一致；next_step 提示必须跟着 memo 实际语言走，否则同一响应里两种语言。
    # hub 没回传 language 字段 = hub 还没升级到这个 i18n 版本，那种旧 hub 固定只输出
    # 中文 cio_memo，所以 fallback 到 "zh" 而不是本机配置——本机配置只描述本机意图，
    # 不代表旧 hub 真的能生成英文。
    hub_lang = (final.get("result") or {}).get("language")
    if hub_lang not in ("zh", "en"):
        hub_lang = "zh"
    if hub_lang == "en":
        next_step = (
            "⚠️ The `cio_memo` field is a Markdown string. Render it directly as Markdown instead of printing the full JSON blob.\n\n"
            "A verdict has been generated. If the user agrees, guide them through these steps:\n"
            f"1) Record the trade in openInvest first (easiest path: open {gui_url} in a browser and use the holdings page; "
            "or run remote-mode `run.sh buy/sell ...`, which writes to the hub automatically)\n"
            "2) Open the real broker or banking app and place the order using the alloc_cny amount from the verdict (currency in alloc_currency, CNY if absent) "
            "(openInvest does not connect to exchanges; it only produces decisions)\n"
            "3) Return to openInvest and mark the execution outcome\n\n"
            "**Do not write to memory/ directly.** In remote mode the local machine does not even own the memory/ directory; "
            "all state changes must go through run.sh write commands or the hub API."
        )
    else:
        next_step = (
            "⚠️ `cio_memo` 字段是 Markdown 字符串，**直接当 Markdown 渲染给用户看**，"
            "不要把整个 JSON 原样打印。\n\n"
            "已生成 verdict。如果用户同意，告诉他按下面三步走：\n"
            f"1) 在 openInvest 里登记这笔（最方便：浏览器开 {gui_url} → 持仓页；"
            "或远端模式直接 `run.sh buy/sell ...`，写入自动落 hub）\n"
            "2) 打开自己的证券 App / 银行 App，按 verdict 里的 alloc_cny 金额（币种见 alloc_currency，缺省 CNY）真实下单"
            "（openInvest 不接交易所，只做决策）\n"
            "3) 回 openInvest 标记成交\n\n"
            "**不要直接写 memory/**——远端模式下本机根本没有 memory/，"
            "所有状态变更必须走 run.sh 写命令或 hub API。"
        )
    _print_json({
        "status": "ok",
        "verdict": asset_result.get("verdict", {}),
        # 给用户看这个，不是 verdict.confidence（CIO 自报）；旧 hub 没这个字段 → None
        "confidence_lookup": asset_result.get("confidence_lookup"),
        "cio_memo": asset_result.get("cio_memo") or "",
        "transcript_path": f"(hub) memory/.committee/{today}/{safe_sym}.md",
        "next_step": next_step,
    })


_HANDLERS: Dict[str, Callable[[argparse.Namespace], None]] = {
    "status": _h_status,
    "strategy": _h_strategy,
    "history": _h_history,
    "what_if": _h_what_if,
    "doctor": _h_doctor,
    "deposit": _h_deposit,
    "withdraw": _h_withdraw,
    "buy": _h_buy,
    "sell": _h_sell,
    "delete_holding": _h_delete_holding,
    "prepare_committee": _h_prepare_committee,
    "save_committee": _h_save_committee,
    "run_committee": _h_run_committee,
    "event_check": _h_event_check,
    "explain_decision": _h_explain_decision,
}


def maybe_dispatch_remote(args: argparse.Namespace) -> bool:
    """远端模式总入口。True = 已远端处理（或已报错退出）；False = 本地白名单命令，继续本地跑"""
    cmd = getattr(args, "cmd", None) or ""
    if cmd in LOCAL_WHITELIST:
        return False
    if cmd in DISABLED:
        err, hint = DISABLED[cmd]
        _die({"status": "error", "error": err, "hint": hint})
    handler = _HANDLERS.get(cmd)
    if handler is None:
        # 默认拒绝：新子命令必须显式归类（路由 / 白名单 / 禁用），防止远端模式下
        # 静默读写客户端本地 memory/ 造成两份账本
        _die({
            "status": "error",
            "error": f"子命令 {cmd} 尚未支持远端模式（INVEST_API_BASE 已设置）",
            "hint": "去 hub 机器上跑；或 unset INVEST_API_BASE 在本机跑（会用本机 memory/）。",
        })
    handler(args)
    return True
