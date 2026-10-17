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
    """对一份 accuracy_summary dict 施加红线 #2（issue #179 P0-2 收紧后口径）：

    - 窗口 sample_size < MIN_SAMPLE_FOR_PUBLIC ⇒ direction_hit_rate 必须为 None
    - 每个方向 bucket total < MIN_SAMPLE_FOR_PUBLIC ⇒ bucket 的 rate **和 hit** 都必须为 None
      （hit/total 一次除法可精确还原被抑制的 rate）
    - 被抑制 bucket 的 total 合计在 (0, MIN_SAMPLE_FOR_PUBLIC) ⇒ 窗口 direction_hit_rate 也必须为 None
      （否则 hit = round(rate×n) − Σ其余桶 hit 减法可逆）

    total 计数保留（GUI 展示 n=XX「样本不足」）。where 仅用于失败信息定位。
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
        n_suppressed = 0
        suppressed_total = 0
        for bucket_name, bucket in (window.get("by_direction") or {}).items():
            total = int(bucket.get("total", 0) or 0)
            if total < MIN_SAMPLE_FOR_PUBLIC:
                n_suppressed += 1
                suppressed_total += total
                for field in ("rate", "hit"):
                    assert bucket.get(field) is None, (
                        f"{where}: 窗口 {name!r} 方向 {bucket_name!r} total={total} "
                        f"< {MIN_SAMPLE_FOR_PUBLIC} 却泄露了 {field}="
                        f"{bucket.get(field)!r}（红线 #2）"
                    )
        if 0 < suppressed_total < MIN_SAMPLE_FOR_PUBLIC:
            assert window.get("direction_hit_rate") is None, (
                f"{where}: 窗口 {name!r} 被抑制桶合计 total={suppressed_total} <"
                f" {MIN_SAMPLE_FOR_PUBLIC}，direction_hit_rate="
                f"{window.get('direction_hit_rate')!r} 仍暴露 ⇒ 被抑制桶的合计 hit 可由"
                f"减法精确还原（红线 #2 补充抑制）"
            )
        cal = window.get("calibration") or {}
        n_excluded = sample_size - int(cal.get("n", 0) or 0)      # 进命中率、没进校准的行（不管原因）
        if cal and (int(cal.get("n", 0) or 0) < MIN_SAMPLE_FOR_PUBLIC
                    or window.get("direction_hit_rate") is None
                    or 0 < n_excluded < MIN_SAMPLE_FOR_PUBLIC):
            # base_rate_brier = p(1−p) 一步还原整体命中率：direction_hit_rate 被抑制、或校准排除了一小撮
            # 行（0<sample_size−calibration.n<30，与 direction_hit_rate 相减逆算那几行的命中）时，校准数字也必须抹
            for k in ("ece", "brier", "base_rate_brier"):
                assert cal.get(k) is None, (
                    f"{where}: 窗口 {name!r} calibration.n={cal.get('n')} / direction_hit_rate 被抑制，"
                    f"却泄露了 calibration.{k}={cal.get(k)!r}（红线 #2）"
                )
        br = window.get("base_rate") or {}
        if br and int(br.get("n", 0) or 0) < MIN_SAMPLE_FOR_PUBLIC:
            for k in ("up", "down", "flat"):
                assert br.get(k) is None, (
                    f"{where}: 窗口 {name!r} base_rate.n={br.get('n')} "
                    f"< {MIN_SAMPLE_FOR_PUBLIC} 却泄露了 base_rate.{k}="
                    f"{br.get(k)!r}（红线 #2 同纪律）"
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


def test_weekend_dups_excluded_from_public(tmp_path):
    """D8 统一周末口径：周末休市资产的周末决议（=周五样本重复）不计入任何窗口，只输出被剔条数。"""
    jsonl = tmp_path / "verdict_review.jsonl"
    rows = ([_make_row("2025-03-07", "up", True)] * 3        # 周五
            + [_make_row("2025-03-08", "up", True)] * 2      # 周六 → 剔
            + [_make_row("2025-03-09", "up", True, asset="BTC-USD")])  # 加密周日照算
    _write_jsonl(jsonl, rows)
    s = build_summary(jsonl)
    assert s["weekend_dup_excluded"] == 2
    assert s["windows"]["all"]["sample_size"] == 4


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


def test_suppress_single_small_direction_also_nulls_overall():
    """恰好 1 个方向 n<30：该方向 rate+hit 抹掉，且 overall rate 也抹
    （否则 hit = round(rate×n) − Σ其余桶 hit 减法可逆，issue #179 P0-2）"""
    window = {
        "direction_hit_rate": 0.9118,
        "sample_size": 34,
        "by_direction": {
            "bullish": {"hit": 0, "total": 3, "rate": 0.0},   # 小样本 → 抹 rate+hit
            "hold": {"hit": 30, "total": 30, "rate": 1.0},    # >=30 → 保留
        },
    }
    out = _suppress_small_samples(window)
    assert out["direction_hit_rate"] is None  # 补充抑制：堵减法通道
    assert out["by_direction"]["bullish"]["rate"] is None
    assert out["by_direction"]["bullish"]["hit"] is None
    assert out["by_direction"]["bullish"]["total"] == 3  # total 保留（GUI 展示 n=XX）
    assert out["by_direction"]["hold"]["rate"] == 1.0
    assert out["by_direction"]["hold"]["hit"] == 30


def test_suppress_two_small_directions_combined_sub30_nulls_overall():
    """2 个方向都 n<30 且合计也 <30：overall 也抹——减法还原的两桶合计命中本身就是
    n<30 的命中率；一桶很小时另一桶几乎被钉死（2026-10 D8 补，原规则只管恰好一个桶）"""
    window = {
        "direction_hit_rate": 0.646,
        "sample_size": 113,
        "by_direction": {
            "bullish": {"hit": 3, "total": 12, "rate": 0.25},
            "bearish": {"hit": 0, "total": 1, "rate": 0.0},
            "hold": {"hit": 70, "total": 100, "rate": 0.7},
        },
    }
    out = _suppress_small_samples(window)
    assert out["direction_hit_rate"] is None
    for b in ("bullish", "bearish"):
        assert out["by_direction"][b]["rate"] is None
        assert out["by_direction"][b]["hit"] is None
    assert out["by_direction"]["hold"]["hit"] == 70


def test_suppress_small_directions_combined_ge30_or_empty_keeps_overall():
    """被抑制桶合计 ≥30（减法只得到 n≥30 的合计）或合计 0 条（无可还原）→ overall 保留"""
    window = {
        "direction_hit_rate": 0.85,
        "sample_size": 80,
        "by_direction": {
            "bullish": {"hit": 10, "total": 20, "rate": 0.5},
            "bearish": {"hit": 15, "total": 20, "rate": 0.75},
            "hold": {"hit": 27, "total": 40, "rate": 0.675},
        },
    }
    assert _suppress_small_samples(window)["direction_hit_rate"] == 0.85
    window["by_direction"] = {
        "bullish": {"hit": 0, "total": 0, "rate": None},
        "bearish": {"hit": 0, "total": 0, "rate": None},
        "hold": {"hit": 68, "total": 80, "rate": 0.85},
    }
    assert _suppress_small_samples(window)["direction_hit_rate"] == 0.85


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
                    "bullish": {"hit": None, "total": 5, "rate": None},  # total 保留、rate+hit 已抹
                },
            },
        },
    }
    # 不应抛异常
    _assert_no_small_sample_rate_leak(clean, where="<in-memory clean>")


def test_base_rate_and_fixed_horizon():
    """issue #179 P1-A②：固定 HIT_HORIZON（不混窗口）+ 同样本市场基率"""
    from scripts.export_accuracy import _aggregate, _suppress_small_samples, HIT_HORIZON

    rows = []
    # 5 条成熟 30d 记录（3 up / 2 down），外加 1 条只有 7d 的未成熟记录（必须被跳过）
    for i, mkt in enumerate(["up", "up", "up", "down", "down"]):
        rows.append({
            "expected_direction": "up",
            "hits": {HIT_HORIZON: mkt == "up"},
            "directions": {HIT_HORIZON: mkt},
        })
    rows.append({"expected_direction": "up", "hits": {"7d": True}, "directions": {"7d": "up"}})

    agg = _aggregate(rows)
    assert agg["sample_size"] == 5, "7d-only 未成熟记录不得回落计入"
    assert agg["hit_horizon"] == HIT_HORIZON
    assert agg["base_rate"]["n"] == 5
    assert agg["base_rate"]["up"] == pytest.approx(0.6)
    assert agg["base_rate"]["down"] == pytest.approx(0.4)

    # n<30 → base_rate 数字同样被抑制（n 保留）
    supp = _suppress_small_samples(agg)
    assert supp["base_rate"]["n"] == 5
    assert supp["base_rate"]["up"] is None


# ---------- A5：公开 JSON 的 calibration（只出整体四个数，n<30 / 命中率被抑制时置 null）----------

def _conf_rows(n_hit, n_miss, *, conf=0.6, direction="flat", date="2026-04-10"):
    rows = [{**_make_row(date, direction, True), "confidence": conf} for _ in range(n_hit)]
    return rows + [{**_make_row(date, direction, False), "confidence": conf} for _ in range(n_miss)]


def test_calibration_key_values_in_every_window(tmp_path):
    """手算：40 条 conf 0.6，30 中 10 不中 → p̄=0.75，Brier=(30×0.16+10×0.36)/40=0.21，
    基率 Brier=0.75×0.25=0.1875，ECE=|0.6−0.75|=0.15。"""
    jsonl = tmp_path / "v.jsonl"
    _write_jsonl(jsonl, _conf_rows(30, 10))
    s = build_summary(jsonl)
    for name, w in s["windows"].items():
        assert set(w["calibration"]) == {"ece", "brier", "base_rate_brier", "n", "n_excluded", "n_zero_conf"}, name   # 无分档明细
    cal = s["windows"]["all"]["calibration"]
    assert cal == {"ece": 0.15, "brier": 0.21, "base_rate_brier": 0.1875, "n": 40, "n_excluded": 0, "n_zero_conf": 0}
    _assert_no_small_sample_rate_leak(s, where="calibration")


def test_calibration_suppressed_below_30_keeps_n():
    agg = _suppress_small_samples(_aggregate(_conf_rows(20, 9)))
    assert agg["calibration"] == {"ece": None, "brier": None, "base_rate_brier": None,
                                  "n": 29, "n_excluded": 0, "n_zero_conf": 0}
    ok = _suppress_small_samples(_aggregate(_conf_rows(20, 10)))
    assert ok["calibration"]["n"] == 30 and ok["calibration"]["brier"] is not None


def test_calibration_suppressed_when_hit_rate_complementary_suppressed():
    """bullish 桶 10 条被抑制 → direction_hit_rate 互补抑制；此时 base_rate_brier(=p̄(1−p̄)) 会把
    整体命中率还原出来，小桶 hit 可减法逆算——校准数字必须一并抹掉，n 保留。"""
    rows = _conf_rows(30, 10, direction="flat") + _conf_rows(6, 4, direction="up")
    agg = _suppress_small_samples(_aggregate(rows))
    assert agg["sample_size"] == 50 and agg["direction_hit_rate"] is None
    assert agg["calibration"] == {"ece": None, "brier": None, "base_rate_brier": None,
                                  "n": 50, "n_excluded": 0, "n_zero_conf": 0}
    _assert_no_small_sample_rate_leak({"windows": {"all": agg}}, where="complementary")


def test_calibration_skips_rows_without_numeric_confidence_and_unclear():
    rows = _conf_rows(30, 0) + [_make_row("2026-04-10", "up", True)]       # 老存档：没有 confidence
    rows.append({**_make_row("2026-04-10", "none", None), "confidence": 0.0, "directions": {"30d": "flat"}})
    assert _aggregate(rows)["calibration"]["n"] == 30


def test_public_json_with_calibration_has_no_sensitive_fields(tmp_path):
    """含 confidence / verdict / asset 的原始行 → 输出 JSON 不得出现 CI grep 的任一关键字，也没有逐档明细。"""
    rows = [{**r, "asset": "NDQ.AX" if i % 2 else "GC=F", "verdict": "HOLD", "source": "live"}
            for i, r in enumerate(_conf_rows(30, 10))]
    jsonl = tmp_path / "v.jsonl"
    _write_jsonl(jsonl, rows)
    content = json.dumps(build_summary(jsonl), ensure_ascii=False)
    assert '"calibration"' in content
    for forbidden in ('"symbol"', '"threshold"', '"verdict_id"', '"asset"', "NDQ", "GC=F", "GC_F",
                      "verdict", "asset", '"bins"', "mean_confidence"):
        assert forbidden not in content, f"公开 JSON 含 {forbidden!r}"


def test_leak_predicate_flags_calibration_numbers_on_small_sample():
    """非空证明：n<30 却带校准数字的 JSON 必被谓词抓住（同 test_leaky_summary_dict_is_flagged_by_predicate）。"""
    leaky = {"windows": {"30d": {
        "direction_hit_rate": None, "sample_size": 5, "by_direction": {},
        "calibration": {"ece": 0.1, "brier": 0.2, "base_rate_brier": 0.2, "n": 5}}}}
    with pytest.raises(AssertionError):
        _assert_no_small_sample_rate_leak(leaky, where="<in-memory leaky calibration>")


def test_calibration_excludes_zero_confidence_rows_and_counts_them():
    """自报恰为 0.0（cio_parse 解析失败兜底）不进校准，单列 n_zero_conf（评审 #455-4）。"""
    ref = _aggregate(_conf_rows(30, 10))
    agg = _aggregate(_conf_rows(30, 10) + _conf_rows(5, 5, conf=0.0))
    assert agg["sample_size"] == 50                                  # 命中率口径照旧含这些行
    cal = agg["calibration"]
    assert (cal["n"], cal["n_zero_conf"], cal["n_excluded"]) == (40, 10, 10)
    for k in ("ece", "brier", "base_rate_brier"):
        assert cal[k] == ref["calibration"][k]


def test_calibration_suppressed_when_zero_conf_subset_is_small():
    """校准样本排除了 n_zero_conf 行而 direction_hit_rate 含它们：两个公开的命中率相减能逆算这几行的命中。
    所以 0<n_zero_conf<30 时校准数字置 null（计数保留）；0 条或 ≥30 条不受影响。"""
    small = _suppress_small_samples(_aggregate(_conf_rows(30, 10) + _conf_rows(2, 0, conf=0.0)))
    assert small["direction_hit_rate"] is not None                   # 命中率本身照常公开
    assert small["calibration"] == {"ece": None, "brier": None, "base_rate_brier": None,
                                    "n": 40, "n_excluded": 2, "n_zero_conf": 2}
    _assert_no_small_sample_rate_leak({"windows": {"all": small}}, where="zero-conf subset")
    big = _suppress_small_samples(_aggregate(_conf_rows(30, 10) + _conf_rows(15, 15, conf=0.0)))
    assert big["calibration"]["n_zero_conf"] == 30 and big["calibration"]["brier"] is not None
    leaky = {"windows": {"30d": {"direction_hit_rate": 0.75, "sample_size": 42, "by_direction": {},
             "calibration": {"ece": 0.1, "brier": 0.2, "base_rate_brier": 0.19, "n": 40, "n_zero_conf": 2}}}}
    with pytest.raises(AssertionError):
        _assert_no_small_sample_rate_leak(leaky, where="<in-memory leaky zero-conf>")


def test_calibration_suppressed_when_any_small_subset_is_excluded():
    """不管排除原因（confidence 缺失 / 非数字 / 0.0）：n_excluded = sample_size − calibration.n 落在 (0, 30) 就置 null。
    老存档没有 confidence 字段的 5 行 → 校准 n=40、sample_size=45：p̄ 与 Brier 能逆算这 5 行的命中。"""
    rows = _conf_rows(30, 10) + [_make_row("2026-04-10", "flat", i % 2 == 0) for i in range(5)]   # 5 行无 confidence
    raw = _aggregate(rows)
    assert (raw["sample_size"], raw["calibration"]["n"], raw["calibration"]["n_excluded"]) == (45, 40, 5)
    assert raw["calibration"]["brier"] is not None                   # 原始统计有数；抑制只发生在对外这一层
    pub = _suppress_small_samples(raw)
    assert pub["direction_hit_rate"] is not None
    assert pub["calibration"] == {"ece": None, "brier": None, "base_rate_brier": None,
                                  "n": 40, "n_excluded": 5, "n_zero_conf": 0}
    _assert_no_small_sample_rate_leak({"windows": {"all": pub}}, where="n_excluded=5")
    # 谓词非空证明：同一份数据如果不抑制，谓词必须报
    with pytest.raises(AssertionError):
        _assert_no_small_sample_rate_leak({"windows": {"all": raw}}, where="<unsuppressed n_excluded=5>")
    # 排除 ≥30 行（子集本身够大）不抑制
    big = _suppress_small_samples(_aggregate(
        _conf_rows(30, 10) + [_make_row("2026-04-10", "flat", True) for _ in range(30)]))
    assert big["calibration"]["n_excluded"] == 30 and big["calibration"]["brier"] is not None
