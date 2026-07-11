"""services/strategy_write 服务层测试（issue #179：strategy 写操作三入口共用实现）。

覆盖：4 个基础操作、upsert（track 语义）幂等、冲突/缺失异常、
schema 失败回滚（配比和≠1 / 删最后一个资产 / 新建缺必填字段）。
"""
from __future__ import annotations

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.services import strategy_write as svc


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory")
    s.write("strategy", "strategy", {
        "target_allocation_stock": 0.7,
        "target_allocation_cash": 0.3,
        "target_assets": [
            {"symbol": "NDQ.AX", "max_single_invest_cny": 10000},
            {"symbol": "GC=F", "max_single_invest_cny": 5000, "sell_fee_pct": 0.0038},
        ],
    }, "# 策略说明 body（写操作不得动它）")
    return s


def _symbols(store):
    return [a["symbol"] for a in store.read("strategy").metadata["target_assets"]]


def test_set_allocations(store):
    out = svc.set_allocations(0.8, 0.2, store=store)
    assert out["status"] == "ok"
    assert out["target_allocation_stock"] == 0.8
    meta = store.read("strategy").metadata
    assert meta["target_allocation_cash"] == 0.2


def test_set_allocations_sum_violation_rolls_back(store):
    with pytest.raises(ValueError, match="validation failed"):
        svc.set_allocations(0.8, 0.8, store=store)
    # 回滚：原值未动
    assert store.read("strategy").metadata["target_allocation_stock"] == 0.7


def test_add_conflict_and_remove_notfound(store):
    with pytest.raises(svc.StrategyConflict):
        svc.add_target_asset({"symbol": "NDQ.AX", "max_single_invest_cny": 1}, store=store)
    with pytest.raises(svc.StrategyNotFound):
        svc.remove_target_asset("NOPE", store=store)


def test_add_missing_required_field_rolls_back(store):
    # 新建缺 max_single_invest_cny → schema 拒绝且不落盘
    with pytest.raises(ValueError, match="validation failed"):
        svc.add_target_asset({"symbol": "AAPL"}, store=store)
    assert "AAPL" not in _symbols(store)


def test_upsert_track_semantics(store):
    # 不存在 → 新建
    out = svc.upsert_target_asset("AAPL", {"max_single_invest_cny": 8000}, store=store)
    assert "AAPL" in _symbols(store) and "已新增" in out["message"]
    # 已存在 → 只更新传入字段（None 被滤掉）
    svc.upsert_target_asset("AAPL", {"sell_fee_pct": 0.001, "channel": None}, store=store)
    aapl = next(a for a in store.read("strategy").metadata["target_assets"] if a["symbol"] == "AAPL")
    assert aapl["sell_fee_pct"] == 0.001 and aapl["max_single_invest_cny"] == 8000
    assert "channel" not in aapl
    # 重复 track 无字段 → 幂等 no-op，不报错
    out = svc.upsert_target_asset("AAPL", {}, store=store)
    assert out["status"] == "ok"


def test_remove_and_last_asset_guard(store):
    svc.remove_target_asset("GC=F", store=store)
    assert _symbols(store) == ["NDQ.AX"]
    # schema 要求至少剩 1 个 → 删最后一个回滚
    with pytest.raises(ValueError, match="validation failed"):
        svc.remove_target_asset("NDQ.AX", store=store)
    assert _symbols(store) == ["NDQ.AX"]


def test_body_untouched(store):
    """写操作只动 frontmatter，人类写的 markdown body 一个字不动。"""
    svc.set_allocations(0.6, 0.4, store=store)
    assert "写操作不得动它" in store.read("strategy").body


def test_cli_cmds_wire_to_service(store, monkeypatch, capfd):
    """CLI 三个子命令 → service 的接线 + 输出 JSON。"""
    import argparse
    import json
    import openinvest.services.strategy_write as svc_mod
    from openinvest.skill_cmds import strategy_cmds as sc

    # 让 service 的默认 store 指到 tmp（CLI 不传 store 参数）
    monkeypatch.setattr(svc_mod, "_store", lambda s: store if s is None else s)

    sc.cmd_track_asset(argparse.Namespace(
        symbol="TSLA", display_name=None, channel=None,
        max_single_invest_cny=3000.0, price_offset_pct=None, sell_fee_pct=None,
    ))
    out = json.loads(capfd.readouterr().out)
    assert out["status"] == "ok" and "TSLA" in _symbols(store)

    sc.cmd_set_allocations(argparse.Namespace(stock=0.75, cash=0.25))
    assert json.loads(capfd.readouterr().out)["target_allocation_stock"] == 0.75

    sc.cmd_untrack_asset(argparse.Namespace(symbol="TSLA"))
    assert "TSLA" not in _symbols(store)
    capfd.readouterr()

    # 错误路径：exit 1 + error JSON
    with pytest.raises(SystemExit) as ei:
        sc.cmd_untrack_asset(argparse.Namespace(symbol="NOPE"))
    assert ei.value.code == 1
    assert json.loads(capfd.readouterr().out)["status"] == "error"


def test_upsert_concurrent_same_new_symbol_all_succeed(store):
    """幂等承诺的并发面（Sonnet review 复现过的 race）：N 线程同时 track 同一个
    新 symbol，全部成功（不许有人吃 StrategyConflict），且列表里恰好一条。"""
    import threading

    errors = []

    def _track():
        try:
            svc.upsert_target_asset("RACE", {"max_single_invest_cny": 100}, store=store)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_track) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, f"并发 track 不该报错: {errors}"
    assert _symbols(store).count("RACE") == 1
