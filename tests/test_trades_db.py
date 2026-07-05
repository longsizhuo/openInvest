"""tests/test_trades_db.py — db/trades_db.py 单元测试

覆盖：
- record / list / patch 基本路径
- WAL 模式验证（两个连接同时读写不报 OperationalError）
- direction 校验（非 BUY/SELL 应抛 ValueError）
- status 校验（非法值应抛 ValueError）
- 不存在的 id patch 返回 False
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from openinvest.db.trades_db import TradesDB


# ---------- fixture ----------

@pytest.fixture
def tdb(tmp_path):
    """每个测试一个独立临时 trades.db"""
    return TradesDB(db_path=str(tmp_path / "trades.db"))


# ---------- record ----------

def test_record_basic(tdb):
    """record_trade 返回正整数 id，trade 可被 get_trade 查到"""
    new_id = tdb.record_trade(
        symbol="NDQ.AX",
        direction="BUY",
        units=10.0,
        price=129.5,
        cost_currency="AUD",
        note="测试买入",
    )
    assert isinstance(new_id, int) and new_id >= 1

    row = tdb.get_trade(new_id)
    assert row is not None
    assert row["symbol"] == "NDQ.AX"
    assert row["direction"] == "BUY"
    assert row["units"] == 10.0
    assert row["price"] == pytest.approx(129.5)
    assert row["cost_currency"] == "AUD"
    assert row["note"] == "测试买入"
    assert row["status"] == "planned"


def test_record_with_verdict_id(tdb):
    """verdict_id 字段存储并可读回"""
    vid = "memory/.committee/2026-05-09/NDQ.AX.md"
    new_id = tdb.record_trade(
        symbol="NDQ.AX", direction="BUY", units=5.0, verdict_id=vid
    )
    row = tdb.get_trade(new_id)
    assert row["verdict_id"] == vid


def test_record_price_none(tdb):
    """price=None（市价单）应存为 NULL，不报错"""
    new_id = tdb.record_trade(symbol="GC=F", direction="SELL", units=2.0, price=None)
    row = tdb.get_trade(new_id)
    assert row["price"] is None


def test_record_invalid_direction(tdb):
    """非 BUY/SELL direction 应抛 ValueError"""
    with pytest.raises(ValueError, match="BUY 或 SELL"):
        tdb.record_trade(symbol="NDQ.AX", direction="HOLD", units=1.0)


def test_record_direction_lowercase(tdb):
    """小写 'buy' 也应被标准化为 BUY，正常写入"""
    new_id = tdb.record_trade(symbol="NDQ.AX", direction="buy", units=1.0)
    row = tdb.get_trade(new_id)
    assert row["direction"] == "BUY"


# ---------- list ----------

def test_list_empty(tdb):
    """空库 list_trades 返回空列表"""
    assert tdb.list_trades() == []


def test_list_order(tdb):
    """list_trades 按 ts 倒序；limit 控制条数"""
    tdb.record_trade(symbol="NDQ.AX", direction="BUY", units=1.0,
                     ts="2026-05-01T10:00:00+00:00")
    tdb.record_trade(symbol="GC=F", direction="BUY", units=5.0,
                     ts="2026-05-02T10:00:00+00:00")
    tdb.record_trade(symbol="NDQ.AX", direction="SELL", units=2.0,
                     ts="2026-05-03T10:00:00+00:00")

    rows = tdb.list_trades(limit=10)
    assert len(rows) == 3
    # 倒序：最新的在前
    assert rows[0]["ts"] == "2026-05-03T10:00:00+00:00"
    assert rows[-1]["ts"] == "2026-05-01T10:00:00+00:00"


def test_list_limit(tdb):
    """limit 参数有效——写 5 条取 2 条"""
    for i in range(5):
        tdb.record_trade(
            symbol="NDQ.AX", direction="BUY", units=float(i + 1),
            ts=f"2026-05-0{i+1}T00:00:00+00:00"
        )
    rows = tdb.list_trades(limit=2)
    assert len(rows) == 2


# ---------- patch ----------

def test_patch_status_ok(tdb):
    """planned → executed → cancelled 流转正常"""
    new_id = tdb.record_trade(symbol="NDQ.AX", direction="BUY", units=1.0)

    assert tdb.patch_status(new_id, "executed") is True
    assert tdb.get_trade(new_id)["status"] == "executed"

    assert tdb.patch_status(new_id, "cancelled") is True
    assert tdb.get_trade(new_id)["status"] == "cancelled"


def test_patch_status_not_found(tdb):
    """不存在的 id 返回 False"""
    assert tdb.patch_status(99999, "executed") is False


def test_patch_status_invalid(tdb):
    """非法 status 抛 ValueError"""
    new_id = tdb.record_trade(symbol="NDQ.AX", direction="BUY", units=1.0)
    with pytest.raises(ValueError, match="planned / executed / cancelled"):
        tdb.patch_status(new_id, "unknown_status")


# ---------- WAL 模式验证 ----------

def test_wal_mode(tmp_path):
    """journal_mode 应为 WAL"""
    db = TradesDB(db_path=str(tmp_path / "wal_test.db"))
    cur = db.conn.cursor()
    cur.execute("PRAGMA journal_mode")
    mode = cur.fetchone()[0]
    assert mode == "wal"


def test_concurrent_read_write_no_error(tmp_path):
    """两个连接同时读写（WAL 模式）不应抛 OperationalError

    WAL 模式允许一个 writer + 多个 reader 同时进行，不互相阻塞。
    这里用 threading 模拟两个独立连接并发操作。
    """
    db_path = str(tmp_path / "concurrent.db")

    # 主连接先写一批数据
    db1 = TradesDB(db_path=db_path)
    for i in range(10):
        db1.record_trade(symbol="NDQ.AX", direction="BUY", units=float(i + 1))

    errors: list[Exception] = []

    def writer():
        """在子线程用第二个连接写"""
        try:
            db2 = TradesDB(db_path=db_path)
            for i in range(5):
                db2.record_trade(symbol="GC=F", direction="SELL", units=1.0)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        """在子线程用主连接读"""
        try:
            db1.list_trades(limit=20)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    # 同时起读写线程
    t_write = threading.Thread(target=writer)
    t_read = threading.Thread(target=reader)
    t_write.start()
    t_read.start()
    t_write.join()
    t_read.join()

    # WAL 模式下不应有并发异常
    assert errors == [], f"并发读写出错: {errors}"


# ---------- intended_date 字段 ----------

def test_record_with_intended_date(tdb):
    """record_trade 传入 intended_date 时应正确存储"""
    new_id = tdb.record_trade(
        symbol="NDQ.AX",
        direction="BUY",
        units=10.0,
        price=130.0,
        intended_date="2026-05-20",
    )
    row = tdb.get_trade(new_id)
    assert row is not None
    assert row["intended_date"] == "2026-05-20"


def test_record_without_intended_date(tdb):
    """不传 intended_date 时，字段应为 None（不报错）"""
    new_id = tdb.record_trade(
        symbol="GC=F",
        direction="BUY",
        units=5.0,
    )
    row = tdb.get_trade(new_id)
    assert row is not None
    assert row["intended_date"] is None


def test_intended_date_in_list(tdb):
    """list_trades 返回的 dict 含 intended_date 字段"""
    tdb.record_trade(symbol="NDQ.AX", direction="BUY", units=1.0,
                     intended_date="2026-06-01")
    tdb.record_trade(symbol="GC=F", direction="SELL", units=2.0)

    rows = tdb.list_trades(limit=10)
    # 第一条（最新）有 intended_date
    assert rows[0]["intended_date"] == "2026-06-01" or rows[1]["intended_date"] == "2026-06-01"
    # 其中一条为 None
    dates = [r["intended_date"] for r in rows]
    assert None in dates


def test_schema_migration_idempotent(tmp_path):
    """反复构建 TradesDB（模拟 ALTER TABLE ADD COLUMN 被调用多次）不应报错"""
    db_path = str(tmp_path / "migrate_test.db")
    # 第一次：建表并加 intended_date
    db1 = TradesDB(db_path=db_path)
    db1.record_trade(symbol="NDQ.AX", direction="BUY", units=1.0,
                     intended_date="2026-05-10")
    # 第二次：再次构建，ALTER TABLE 走 try/except 兜底，不崩溃
    db2 = TradesDB(db_path=db_path)
    row = db2.list_trades(limit=1)[0]
    # 已写入的数据不丢失
    assert row["symbol"] == "NDQ.AX"
    assert row["intended_date"] == "2026-05-10"
