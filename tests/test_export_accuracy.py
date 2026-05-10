"""tests/test_export_accuracy.py — scripts/export_accuracy.py 单元测试

覆盖：
- 跑一次脚本后断言输出 JSON 里不存在敏感字段
- 聚合逻辑正确性（命中率计算、窗口过滤）
- 空 jsonl 时各窗口 sample_size 为 0
- hits 全空的记录被跳过
- 输出文件写入正确
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# 直接 import 模块（不作为 __main__ 运行）
from scripts.export_accuracy import build_summary, _aggregate, _filter_by_window


# ---------- helper ----------

def _make_row(
    date: str,
    expected_direction: str,
    hit_30d: bool | None,
    asset: str = "REDACTED",  # asset 字段存在于原始数据但不应出现在输出
) -> dict:
    """构造单条 verdict_review 记录"""
    hits: dict = {}
    if hit_30d is not None:
        hits["30d"] = hit_30d
    return {
        "date": date,
        "asset": asset,           # 原始数据有此字段
        "verdict": "HOLD",        # 原始数据有此字段
        "expected_direction": expected_direction,
        "hits": hits,
    }


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8",
    )


# ---------- 脱敏红线测试 ----------

def test_no_sensitive_fields_in_output(tmp_path):
    """输出 JSON 文件中不得出现 symbol / threshold / NDQ / GC=F 等敏感字符串"""
    jsonl = tmp_path / "verdict_review.jsonl"
    out_json = tmp_path / "accuracy_summary.json"

    rows = [
        _make_row("2026-04-10", "up", True, asset="NDQ.AX"),
        _make_row("2026-04-11", "down", False, asset="GC=F"),
        _make_row("2026-04-12", "flat", True, asset="GC_F"),
    ]
    _write_jsonl(jsonl, rows)

    from scripts.export_accuracy import build_summary
    summary = build_summary(jsonl)
    out_json.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")

    content = out_json.read_text(encoding="utf-8")

    # 严格断言：这些字符串不得出现在输出文件中
    for forbidden in ("symbol", "threshold", "NDQ", "GC=F", "GC_F", "verdict", "asset"):
        assert forbidden not in content, (
            f"输出文件包含敏感字段 {forbidden!r}，脱敏失败！\n内容片段: {content[:500]}"
        )


def test_output_keys_structure(tmp_path):
    """输出 JSON 顶层结构必须有 generated_at + windows；windows 有 30d/90d/all"""
    jsonl = tmp_path / "v.jsonl"
    _write_jsonl(jsonl, [_make_row("2026-05-01", "up", True)])

    summary = build_summary(jsonl)
    assert "generated_at" in summary
    assert "windows" in summary
    assert set(summary["windows"].keys()) == {"30d", "90d", "all"}

    # 每个窗口有 direction_hit_rate / sample_size / by_direction
    for window_data in summary["windows"].values():
        assert "direction_hit_rate" in window_data
        assert "sample_size" in window_data
        assert "by_direction" in window_data
        for bucket in ("bullish", "bearish", "hold"):
            assert bucket in window_data["by_direction"]


# ---------- 聚合逻辑 ----------

def test_aggregate_hit_rate():
    """3 BUY 2 中 → bullish 命中率 2/3 = 0.6667"""
    rows = [
        _make_row("2026-05-01", "up", True),
        _make_row("2026-05-02", "up", True),
        _make_row("2026-05-03", "up", False),
    ]
    result = _aggregate(rows)
    assert result["sample_size"] == 3
    assert result["by_direction"]["bullish"]["hit"] == 2
    assert result["by_direction"]["bullish"]["total"] == 3
    assert result["by_direction"]["bullish"]["rate"] == pytest.approx(2 / 3, rel=1e-3)
    assert result["by_direction"]["bearish"]["total"] == 0
    assert result["by_direction"]["bearish"]["rate"] is None


def test_aggregate_empty_hits_skipped():
    """hits 全空的记录（backtest 无数据）不计入统计"""
    rows = [
        {"date": "2026-05-01", "expected_direction": "up", "hits": {}},      # 空 → 跳过
        {"date": "2026-05-02", "expected_direction": "up", "hits": {"30d": True}},  # 计
    ]
    result = _aggregate(rows)
    assert result["sample_size"] == 1
    assert result["by_direction"]["bullish"]["total"] == 1


def test_aggregate_direction_mapping():
    """bearish / down 都映射到 by_direction.bearish"""
    rows = [
        _make_row("2026-05-01", "bearish", False),
        _make_row("2026-05-02", "down", True),
        _make_row("2026-05-03", "neutral", True),  # neutral → hold
    ]
    result = _aggregate(rows)
    assert result["by_direction"]["bearish"]["total"] == 2
    assert result["by_direction"]["hold"]["total"] == 1


def test_aggregate_all_hit():
    """全命中时 direction_hit_rate = 1.0"""
    rows = [_make_row(f"2026-05-0{i}", "flat", True) for i in range(1, 4)]
    result = _aggregate(rows)
    assert result["direction_hit_rate"] == pytest.approx(1.0)


def test_aggregate_empty_rows():
    """空 rows 时各字段为 0 / None"""
    result = _aggregate([])
    assert result["sample_size"] == 0
    assert result["direction_hit_rate"] is None
    for bucket in ("bullish", "bearish", "hold"):
        assert result["by_direction"][bucket]["total"] == 0


# ---------- 窗口过滤 ----------

def test_filter_window_30d():
    """30d 窗口只保留近 30 天记录"""
    now = datetime.now(timezone.utc)
    rows = [
        _make_row((now - timedelta(days=10)).strftime("%Y-%m-%d"), "up", True),  # 在内
        _make_row((now - timedelta(days=50)).strftime("%Y-%m-%d"), "up", False), # 在外
    ]
    filtered = _filter_by_window(rows, 30, now)
    assert len(filtered) == 1
    assert filtered[0]["expected_direction"] == "up"


def test_filter_window_none_returns_all():
    """days=None 返回所有记录"""
    now = datetime.now(timezone.utc)
    rows = [_make_row("2020-01-01", "flat", True) for _ in range(5)]
    assert _filter_by_window(rows, None, now) == rows


# ---------- 文件 I/O ----------

def test_build_summary_file_not_exist(tmp_path):
    """jsonl 不存在时返回空 summary（不抛异常）"""
    summary = build_summary(tmp_path / "nonexistent.jsonl")
    for window_data in summary["windows"].values():
        assert window_data["sample_size"] == 0


def test_build_summary_writes_file(tmp_path):
    """通过 main() 路径验证文件落盘"""
    import subprocess, sys
    jsonl = tmp_path / "v.jsonl"
    out = tmp_path / "out.json"
    _write_jsonl(jsonl, [_make_row("2026-05-09", "up", True)])

    import subprocess
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "scripts" / "export_accuracy.py"),
         "--jsonl", str(jsonl), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "windows" in data

    # 再次确认无敏感字段
    raw = out.read_text(encoding="utf-8")
    for forbidden in ("symbol", "threshold", "NDQ", "GC=F"):
        assert forbidden not in raw
