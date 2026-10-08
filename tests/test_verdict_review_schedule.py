"""verdict_review 调度契约测试（Phase 3 闭环"随时可一键开"的守门）

verdict_review.py 早就存在，但缺 verdict_review.yml → scheduler 发现不到、永远不会被注册，
Phase 3 自学习闭环（.committee live 快照 → .dreams/verdict_review.jsonl → dreaming）就断了上游。
本测试守住补上的 yml：

a. runner._load_job_configs() 能发现 verdict_review（被 scheduler 看见）
b. CronTrigger.from_crontab(schedule) 不抛异常（cron 表达式合法，daemon 启动不会崩）
c. entry 字符串能 import 到可调用的 run（_resolve_entry 不会在注册时炸）
d. review_all(include_backtest=False, include_live=True) 能发现并解析 live 快照、全标 source==live
   （hermetic：tmp MEMORY_ROOT 种合成快照，不依赖生产 memory/ 数据，CI 可跑；
   防"yml 在但 live 消费链路又被改断"的回退）

注意：enabled 暂 false 是 Phase 3 设计的一部分（开火前由用户确认 cron 频率），
这里**不**断言 enabled 值——开/关由用户在开火清单里决定，测试只守"可被发现 + 可被注册 + 上游有料"。
"""
from __future__ import annotations

import pandas as pd
import pytest
from apscheduler.triggers.cron import CronTrigger

from openinvest.scheduler import runner


def _verdict_cfg():
    """从 jobs/*.yml 里捞 verdict_review 的配置（找不到则 None）"""
    configs = runner._load_job_configs()
    return next((c for c in configs if c.get("name") == "verdict_review"), None)


# ---------- a. 被 scheduler 发现 ----------

def test_verdict_review_discovered_by_loader():
    """_load_job_configs 必须能列出 verdict_review（否则 yml 缺失/名字写错）"""
    names = [c.get("name") for c in runner._load_job_configs()]
    assert "verdict_review" in names, f"verdict_review 未被发现，当前 jobs: {names}"


# ---------- b. cron 表达式合法 ----------

def test_verdict_review_schedule_is_valid_crontab():
    """schedule 能被 CronTrigger.from_crontab 解析（非法表达式会让 daemon 启动崩）"""
    cfg = _verdict_cfg()
    assert cfg is not None
    # 不抛异常即通过；同时带上 timezone（与 register_jobs 注册路径一致）
    trigger = CronTrigger.from_crontab(
        cfg["schedule"], timezone=cfg.get("timezone", "Asia/Shanghai")
    )
    assert trigger is not None


# ---------- c. entry 可解析到 callable ----------

def test_verdict_review_entry_resolves_to_callable():
    """entry 'openinvest.jobs.verdict_review:run' 能 import 到一个 callable（注册时不会炸）"""
    cfg = _verdict_cfg()
    assert cfg is not None
    fn = runner._resolve_entry(cfg["entry"])
    assert callable(fn)


# ---------- d. live 快照上游有料（回归下界） ----------

def _write_committee(root: "Path", sub: str, date: str, sym: str, verdict: str) -> None:
    """种一条最小可解析的 committee 快照（自带 **Symbol** 行 → 免依赖 user.md resolver）。

    格式对齐 _parse_committee_file 的解析契约：**Verdict** 行 + **Symbol** 行 + Macro JSON 块。
    """
    d = root / sub / date
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sym}.md").write_text(
        f"# Committee: {sym}\n"
        f"**Symbol**: {sym}\n"
        f"**Verdict**: {verdict} (confidence 0.70)\n\n"
        f"## Macro Context Snapshot\n```json\n"
        f'{{"vix": 15.0, "regime": "neutral"}}\n```\n',
        encoding="utf-8",
    )


def test_review_all_live_path_parses_seeded_snapshots(monkeypatch, tmp_path):
    """review_all(include_live=True) 能发现并解析 .committee live 快照、全标 source==live；
    include_backtest=False 不混 .backtest。**hermetic**：tmp MEMORY_ROOT 种合成快照，
    不依赖生产 memory/ 真实数据，CI 可跑。守的是"Phase 3 一旦开调度，live 消费链路能真吃到 verdict"。
    """
    from pathlib import Path  # noqa: F401  仅给上面 _write_committee 的类型注释用

    from openinvest.core import memory_store as ms
    from openinvest.jobs import verdict_review as vr

    # tmp MEMORY_ROOT → MemoryStore()/_build_symbol_resolver 全部指向它（不碰生产 memory/）
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path)

    # 种 3 条 live + 1 条 backtest（验证 include_backtest=False 把 backtest 排除）
    _write_committee(tmp_path, ".committee", "2024-02-01", "AAPL", "HOLD")
    _write_committee(tmp_path, ".committee", "2024-02-02", "MSFT", "ACCUMULATE")
    _write_committee(tmp_path, ".committee", "2024-02-05", "NVDA", "TRIM")
    _write_committee(tmp_path, ".backtest", "2024-02-01", "AAPL", "HOLD")

    # 合成价格序列替身（覆盖决议日 + 各 forward 窗口），免 yfinance；macro_shock no-op
    idx = pd.bdate_range("2024-01-01", "2024-12-31")
    series = pd.DataFrame({"Close": [100.0 + i * 0.1 for i in range(len(idx))]}, index=idx)
    monkeypatch.setattr(vr, "_closes", lambda s: series)
    monkeypatch.setattr(vr, "_detect_macro_shock", lambda *a, **k: {"detected": False, "drivers": []})

    reviews = vr.review_all(include_backtest=False, include_live=True)

    assert len(reviews) == 3, f"应解析 3 条种入的 live 快照，实际 {len(reviews)}"
    assert all(r.source == "live" for r in reviews), "include_backtest=False 不应混入 backtest 样本"
    assert {r.asset for r in reviews} == {"AAPL", "MSFT", "NVDA"}


# ---------- e. 开火形态：cron run() 只复盘 live + 只读行情库、零网络（2026-10 D4） ----------

def test_run_is_live_only_and_never_touches_network(monkeypatch, tmp_path):
    """run()（scheduler 无参调用）：只写 live 行；行情全从 market_data.db 来。

    任何网络尝试（get_history_data 刷新 / yfinance / socket.connect）都被记下并抛错——
    代码里的 try/except 会吞异常，所以断言"零调用"而不是等异常冒泡。
    同时断言收益/宏观突变真算出来了：证明数据来自库，不是全退化成 None。
    """
    import json
    import socket

    import yfinance as yf

    from openinvest.core import memory_store as ms
    from openinvest.db import market_store as mstore
    from openinvest.jobs import verdict_review as vr
    from openinvest.utils import exchange_fee as ef

    mem = tmp_path / "memory"
    monkeypatch.setattr(ms, "MEMORY_ROOT", mem)
    monkeypatch.setattr(vr, "ROOT", tmp_path)                 # docs/verdict_accuracy.md 写 tmp
    monkeypatch.setattr(mstore, "DB_PATH", str(tmp_path / "market_data.db"))
    monkeypatch.setattr(vr, "_STORE", None)                   # 让 _closes 连上 tmp 库
    for sub, d, sym, v in [(".committee", "2026-01-05", "AAPL", "HOLD"),
                           (".committee", "2026-01-06", "MSFT", "ACCUMULATE"),
                           (".backtest", "2026-01-05", "AAPL", "HOLD")]:
        _write_committee(mem, sub, d, sym, v)

    store = mstore.MarketStore()
    for i, day in enumerate(pd.bdate_range("2025-01-01", "2026-03-31")):
        for sym, base in [("AAPL", 100.0), ("MSFT", 200.0), ("^VIX", 15.0),
                          ("^TNX", 4.0), ("USDCNY=X", 7.0)]:
            c = base * (1 + 0.001 * i)
            store.save_generic_price(sym, day.strftime("%Y-%m-%d"), c,
                                     high=c * 1.01, low=c * 0.99, volume=1e6)

    calls = []

    def _net(*a, **k):
        calls.append(a[:2])
        raise RuntimeError("network attempted during verdict_review")

    monkeypatch.setattr(ef, "get_history_data", _net)
    monkeypatch.setattr(yf, "Ticker", _net)
    monkeypatch.setattr(yf, "download", _net)
    monkeypatch.setattr(socket.socket, "connect", _net)
    monkeypatch.setattr(vr, "_build_symbol_resolver", lambda: {})  # 快照自带 **Symbol** 行

    out = vr.run()

    assert calls == [], f"verdict_review 不许触网，实际尝试 {calls}"
    assert out["status"] == "ok"
    rows = [json.loads(x) for x in (mem / ".dreams" / "verdict_review.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and {r["source"] for r in rows} == {"live"}
    assert all("30d" in r["actual_returns"] for r in rows), "收益应从库里算出"
    assert all("detected" in r["macro_shock"] for r in rows), "macro_shock 应从库里算出"
