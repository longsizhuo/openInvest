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


def test_bare_a_share_code_suffixed_and_deduped(store):
    svc.upsert_target_asset("510300", {"max_single_invest_cny": 1000}, store=store)
    assert "510300.SS" in _symbols(store) and "510300" not in _symbols(store)
    # 同一标的的另一种写法：更新原条目，不新建；add 视为重复；untrack / target_pct 用裸码也找得到
    svc.upsert_target_asset("510300.ss", {"sell_fee_pct": 0.001}, store=store)
    assert _symbols(store).count("510300.SS") == 1
    with pytest.raises(svc.StrategyConflict):
        svc.add_target_asset({"symbol": "510300", "max_single_invest_cny": 1}, store=store)
    svc.set_allocations(target_pcts={"510300": 0.1}, store=store)
    svc.remove_target_asset("510300", store=store)
    assert "510300.SS" not in _symbols(store)
    svc.add_target_asset({"symbol": "159915", "max_single_invest_cny": 1000}, store=store)
    assert "159915.SZ" in _symbols(store)


def test_legacy_bare_target_matched_by_suffixed_spelling(store):
    meta = store.read("strategy").metadata
    store.write("strategy", "strategy", {**meta, "target_assets": [
        *meta["target_assets"], {"symbol": "510300", "max_single_invest_cny": 1000}]}, "")
    svc.upsert_target_asset("510300.SS", {"sell_fee_pct": 0.001}, store=store)
    assert "510300.SS" not in _symbols(store)
    assert next(a for a in store.read("strategy").metadata["target_assets"]
                if a["symbol"] == "510300")["sell_fee_pct"] == 0.001


@pytest.mark.parametrize("held, track", [
    ({"symbol": "510300", "kind": "etf"}, "510300"),      # 老账本裸码：target 沿用持仓写法，holdings_digest 连得上
    ({"symbol": "510300", "kind": "etf"}, "510300.SS"),
    ({"symbol": "110011", "kind": "fund"}, "110011"),     # 场外基金：不能补成 110011.SS（沪市可转债）
])
def test_new_target_follows_holding_spelling(store, held, track):
    store.write("portfolio", "portfolio", {"schema_version": 2, "cash": {},
                                           "holdings": [{**held, "units": 100, "avg_cost": 1.0}]}, "")
    svc.upsert_target_asset(track, {"max_single_invest_cny": 1000}, store=store)
    assert held["symbol"] in _symbols(store) and f"{held['symbol']}.SS" not in _symbols(store)
    svc.remove_target_asset(held["symbol"], store=store)
    svc.add_target_asset({"symbol": track, "max_single_invest_cny": 1000}, store=store)
    assert held["symbol"] in _symbols(store)


def test_upsert_new_symbol_defaults_cap_from_strategy(store):
    """2026-10-11：CLI `track_asset` 不带 --max-single-invest-cny 时直接甩 pydantic 原文。
    新建没给单笔上限 → 沿用策略顶层的单次入场上限，并在消息里写明。"""
    meta = dict(store.read("strategy").metadata, max_single_invest_cny=9000)
    store.write("strategy", "strategy", meta, "# body")
    out = svc.upsert_target_asset("AAPL", {"display_name": "Apple"}, store=store)
    aapl = next(a for a in store.read("strategy").metadata["target_assets"] if a["symbol"] == "AAPL")
    assert aapl["max_single_invest_cny"] == 9000 and aapl["display_name"] == "Apple"
    assert "沿用策略的单次入场上限 9,000 CNY" in out["message"]


def test_upsert_new_symbol_without_any_cap_gives_readable_error(store):
    before = store.read("strategy").metadata["target_assets"]
    with pytest.raises(ValueError, match="--max-single-invest-cny") as e:
        svc.upsert_target_asset("AAPL", {}, store=store)
    assert "validation" not in str(e.value)  # 不是 pydantic 原文
    assert store.read("strategy").metadata["target_assets"] == before


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
