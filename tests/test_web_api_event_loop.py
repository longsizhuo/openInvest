"""web_api 事件循环不被阻塞 IO 冻结（#233-3）。

单 worker uvicorn 下 `async def` 端点里跑同步阻塞调用 = 整个事件循环停摆：
/api/health 探活超时、SSE 卡住、别的请求全排队。修法是阻塞端点改 `def`
（FastAPI 丢线程池），必须 async 的（SSE）把阻塞调用包 run_in_threadpool。

`with TestClient(app)` 让所有请求共享同一个事件循环线程（≈ 单 worker
uvicorn）；不进 with 的话每个请求各开一个 portal/loop，测不出冻结。
"""
from __future__ import annotations

import inspect
import threading
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from openinvest.connectors import web_api
from openinvest.connectors.web_api.routers import trades as trades_router

# 允许保持 async 的端点 —— 其余一律 def。新增 async 端点必须先确认它不碰
# 磁盘/sqlite/网络/子进程（或已全部 await 线程池），再加进这里。
ASYNC_OK = {
    "meta.health": "纯内存无 IO；留在事件循环上，线程池打满时探活仍能响应",
    "committee.committee_live": "SSE 异步生成器；status.json 读取走 run_in_threadpool",
    "trades.patch_trade_status": "阻塞 DB/portfolio 调用全部 asyncio.to_thread",
}


def test_only_whitelisted_endpoints_are_async():
    async_eps = {
        f"{r.endpoint.__module__.rsplit('.', 1)[-1]}.{r.endpoint.__name__}"
        for r in web_api.app.routes
        if isinstance(r, APIRoute) and inspect.iscoroutinefunction(r.endpoint)
    }
    assert async_eps == set(ASYNC_OK), (
        f"不该是 async 的端点: {sorted(async_eps - set(ASYNC_OK))}"
        "（同步阻塞 IO 放 async def 会冻结事件循环，改成 def）；"
        f"白名单里已不存在的: {sorted(set(ASYNC_OK) - async_eps)}"
    )


def _patch_attr(target):
    return lambda mp, fn: mp.setattr(target, fn)


@pytest.mark.parametrize(
    "install, slow_request, ret",
    [
        # def 端点代表：event_watch 生产上内联跑 30-90s
        (_patch_attr("openinvest.jobs.event_watch.run"),
         lambda c: c.post("/api/events/check"), {"status": "ok"}),
        # 必须 async 的 SSE 端点：阻塞读 status.json 包进线程池
        (_patch_attr("openinvest.connectors.web_api.routers.committee._read_committee_status"),
         lambda c: c.get("/api/committee/live/t-loop"), {"status": "done"}),
        # async 的 trades PATCH：残留的 sqlite 点查也必须 to_thread（返回 None → 404）
        (lambda mp, fn: mp.setattr(trades_router, "_trades_db", SimpleNamespace(get_trade=fn)),
         lambda c: c.patch("/api/trades/1/status", json={"status": "cancelled"}), None),
        # 单例首次构造（wal_checkpoint，busy_timeout 5s）也不能跑在事件循环上
        (lambda mp, fn: (mp.setattr(trades_router, "_trades_db", None),
                         mp.setattr(trades_router, "_TradesDB", fn)),
         lambda c: c.patch("/api/trades/1/status", json={"status": "cancelled"}),
         SimpleNamespace(get_trade=lambda _id: None)),
    ],
    ids=["events_check", "committee_live_sse", "trades_patch_status", "trades_db_first_init"],
)
def test_blocking_call_does_not_freeze_health(monkeypatch, install, slow_request, ret):
    monkeypatch.delenv("INVEST_API_TOKEN", raising=False)
    entered, release = threading.Event(), threading.Event()

    def _blocking(*_a, **_kw):
        entered.set()
        release.wait(10)
        return ret

    install(monkeypatch, _blocking)
    health: dict = {}
    with TestClient(web_api.app) as c:
        slow = threading.Thread(target=slow_request, args=(c,))
        probe = threading.Thread(target=lambda: health.setdefault("r", c.get("/api/health")))
        slow.start()
        try:
            assert entered.wait(5), "慢请求没走到被 patch 的阻塞调用"
            probe.start()
            probe.join(3)
            assert "r" in health, "阻塞调用期间 /api/health 3s 无响应 —— 事件循环被冻结"
            assert health["r"].status_code == 200
        finally:
            release.set()
            slow.join(10)
            if probe.is_alive():
                probe.join(10)


def test_events_check_overlap_is_single_flight(monkeypatch):
    """重叠的手动扫描（超时重试 / 双击）不能并行跑两份 event_watch —— 否则同一篇
    新闻重复触发委员会 + 重复预警邮件。第二个请求立即返回 already_running。"""
    monkeypatch.delenv("INVEST_API_TOKEN", raising=False)
    entered, release, calls = threading.Event(), threading.Event(), []

    def _fake_run():
        calls.append(1)
        entered.set()
        release.wait(10)
        return {"status": "ok"}

    monkeypatch.setattr("openinvest.jobs.event_watch.run", _fake_run)
    second: dict = {}
    with TestClient(web_api.app) as c:
        first = threading.Thread(target=c.post, args=("/api/events/check",))
        t2 = threading.Thread(target=lambda: second.setdefault("r", c.post("/api/events/check")))
        first.start()
        try:
            assert entered.wait(5)
            t2.start()
            t2.join(3)
            assert "r" in second, "第二个扫描没被单飞挡住，跟第一个一起卡在 event_watch 里"
            assert second["r"].status_code == 200
            assert second["r"].json()["status"] == "already_running"
            assert len(calls) == 1
        finally:
            release.set()
            first.join(10)
            if t2.is_alive():
                t2.join(10)
        # 第一个跑完锁已释放：下一次扫描正常执行
        assert c.post("/api/events/check").json()["status"] == "ok"
        assert len(calls) == 2
