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
from scripts.export_accuracy import (
    build_summary,
    _aggregate,
    _filter_by_window,
    _suppress_small_samples,
    DEFAULT_OUT,
    MIN_SAMPLE_FOR_PUBLIC,
)


# ---------- helper: 小样本 rate 泄露断言（红线 #2 的可信源谓词）----------

def _assert_no_small_sample_rate_leak(summary: dict, where: str) -> None:
    """对一份 accuracy_summary dict 施加红线 #2：

    - 窗口 sample_size < MIN_SAMPLE_FOR_PUBLIC ⇒ direction_hit_rate 必须为 None
    - 每个方向 bucket total < MIN_SAMPLE_FOR_PUBLIC ⇒ bucket rate 必须为 None

    计数（hit/total）可以保留——只有具体 rate 在小样本时必须被抹掉。
    where 仅用于失败信息定位。
    """
    windows = summary.get("windows") or {}
    assert windows, f"{where}: summary 缺 windows，结构异常"
    for name, window in windows.items():
        sample_size = int(window.get("sample_size", 0) or 0)
        if sample_size < MIN_SAMPLE_FOR_PUBLIC:
            assert window.get("direction_hit_rate") is None, (
                f"{where}: 窗口 {name!r} sample_size={sample_size} "
                f"< {MIN_SAMPLE_FOR_PUBLIC} 却泄露了 direction_hit_rate="
                f"{window.get('direction_hit_rate')!r}（红线 #2）"
            )
        for bucket_name, bucket in (window.get("by_direction") or {}).items():
            total = int(bucket.get("total", 0) or 0)
            if total < MIN_SAMPLE_FOR_PUBLIC:
                assert bucket.get("rate") is None, (
                    f"{where}: 窗口 {name!r} 方向 {bucket_name!r} total={total} "
                    f"< {MIN_SAMPLE_FOR_PUBLIC} 却泄露了 rate={bucket.get('rate')!r}"
                    f"（红线 #2）"
                )


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


# ---------- ADR-022:backtest 不进公开 accuracy ----------

def test_backtest_entries_excluded_from_public(tmp_path):
    """公开 accuracy 只反映 live 实时战绩;backtest 条目(污染 pre-cutoff 或干净 holdout)全剔除。"""
    from scripts.export_accuracy import build_summary
    jsonl = tmp_path / "verdict_review.jsonl"
    live = [{**_make_row("2025-03-03", "up", True), "source": "live"} for _ in range(5)]
    bt = [{**_make_row("2020-02-14", "up", True), "source": "backtest"} for _ in range(40)]
    _write_jsonl(jsonl, live + bt)
    assert build_summary(jsonl)["windows"]["all"]["sample_size"] == 5, \
        "backtest 条目泄漏进了公开 accuracy(ADR-022 红线)"
    # 缺 source 的老条目按 live 计入(向后兼容:jsonl 早于 backtest 集成时全是 live)
    _write_jsonl(jsonl, [_make_row("2025-03-03", "up", True)])
    assert build_summary(jsonl)["windows"]["all"]["sample_size"] == 1


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


# ---------- 小样本抑制（红线 #2：n < 30 不展示具体数字）----------

def test_suppress_small_samples_nulls_overall_rate():
    """窗口 sample_size < 30 → direction_hit_rate 置 None，但保留 sample_size"""
    window = {
        "direction_hit_rate": 0.8929,
        "sample_size": 28,
        "by_direction": {
            "bullish": {"hit": 0, "total": 3, "rate": 0.0},
            "hold": {"hit": 24, "total": 24, "rate": 1.0},
        },
    }
    out = _suppress_small_samples(window)
    assert out["direction_hit_rate"] is None
    assert out["sample_size"] == 28              # 计数保留
    assert out["by_direction"]["bullish"]["rate"] is None
    assert out["by_direction"]["hold"]["rate"] is None
    assert out["by_direction"]["bullish"]["total"] == 3  # 计数保留


def test_suppress_keeps_large_overall_but_nulls_small_direction():
    """整体 n>=30 保留 direction_hit_rate；但某方向 n<30 仍抹该方向 rate"""
    window = {
        "direction_hit_rate": 0.9118,
        "sample_size": 34,
        "by_direction": {
            "bullish": {"hit": 0, "total": 3, "rate": 0.0},   # 小样本 → 抹
            "hold": {"hit": 30, "total": 30, "rate": 1.0},    # >=30 → 保留
        },
    }
    out = _suppress_small_samples(window)
    assert out["direction_hit_rate"] == 0.9118
    assert out["by_direction"]["bullish"]["rate"] is None
    assert out["by_direction"]["hold"]["rate"] == 1.0


def test_build_summary_suppresses_small_sample_public_output(tmp_path):
    """端到端：build_summary 输出里小样本窗口不得带具体命中率数字"""
    now = datetime.now(timezone.utc)
    # 只造 5 条近期记录 → 所有窗口 n=5 < 30
    rows = [
        _make_row((now - timedelta(days=i)).strftime("%Y-%m-%d"), "up", True)
        for i in range(5)
    ]
    jsonl = tmp_path / "v.jsonl"
    _write_jsonl(jsonl, rows)

    summary = build_summary(jsonl)
    for name, window in summary["windows"].items():
        assert window["sample_size"] < MIN_SAMPLE_FOR_PUBLIC
        assert window["direction_hit_rate"] is None, f"{name} 窗口泄露了小样本命中率"
        for bucket in window["by_direction"].values():
            assert bucket["rate"] is None


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


# ---------- 守护 COMMITTED 的 docs/accuracy_summary.json（红线 #2）----------
# 背景：CI 的「脱敏」grep 只扫 symbol / threshold / verdict 关键字，
# 不会捕获 n<30 的命中率泄露。所以这一红线此前只有 synthetic 输入的单测在守，
# 真正对外的 committed JSON 没人验。这里直接加载 committed 文件做谓词断言。

def test_committed_accuracy_summary_no_small_sample_rate_leak():
    """加载 *真实 committed* docs/accuracy_summary.json，断言红线 #2：

    任何 sample_size < MIN_SAMPLE_FOR_PUBLIC 的窗口，其 direction_hit_rate 必须为 None；
    任何 total < MIN_SAMPLE_FOR_PUBLIC 的方向 bucket，其 rate 必须为 None。

    路径用 export_accuracy.DEFAULT_OUT（基于 repo root 解析，单一可信源），
    阈值用 MIN_SAMPLE_FOR_PUBLIC（跟随源，不硬编码 30）。

    文件缺失 → skip（带清晰原因）；存在但泄露小样本 rate → FAIL。
    """
    committed = DEFAULT_OUT
    if not committed.exists():
        pytest.skip(
            f"committed accuracy_summary 不存在: {committed}（无法守红线 #2，跳过）"
        )

    summary = json.loads(committed.read_text(encoding="utf-8"))
    _assert_no_small_sample_rate_leak(summary, where=str(committed))


def test_leaky_summary_dict_is_flagged_by_predicate():
    """非空证明（non-vacuous guard）：同一个谓词若遇到泄露的 JSON 必然 FAIL。

    构造一份内存 dict——sample_size=5 < 30 却带 direction_hit_rate=0.8，
    且方向 bucket total=5 < 30 却带 rate=0.8——断言 _assert_no_small_sample_rate_leak
    抛 AssertionError。这证明：若有人 commit 这样一份 JSON，
    test_committed_accuracy_summary_no_small_sample_rate_leak 会变红。
    """
    leaky = {
        "windows": {
            "30d": {
                "direction_hit_rate": 0.8,   # 泄露：n<30 仍给整体命中率
                "sample_size": 5,
                "by_direction": {
                    "bullish": {"hit": 4, "total": 5, "rate": 0.8},  # 泄露：n<30 仍给方向 rate
                },
            },
        },
    }
    with pytest.raises(AssertionError):
        _assert_no_small_sample_rate_leak(leaky, where="<in-memory leaky>")


def test_predicate_passes_on_clean_small_sample_dict():
    """对照：小样本但 rate 已被抹（None）的 dict 不应触发断言。

    确保谓词不是「永远 raise」——计数保留、rate=None 时必须 PASS，
    避免 test_leaky_summary_dict_is_flagged_by_predicate 变成假阳性。
    """
    clean = {
        "windows": {
            "30d": {
                "direction_hit_rate": None,  # 已抹
                "sample_size": 5,
                "by_direction": {
                    "bullish": {"hit": 4, "total": 5, "rate": None},  # 计数保留、rate 已抹
                },
            },
        },
    }
    # 不应抛异常
    _assert_no_small_sample_rate_leak(clean, where="<in-memory clean>")
