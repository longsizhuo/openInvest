"""sweep_runner.py — 参数 sweep runner（两种模式）

模式：
  arithmetic  纯算术，只跑 classify_regime() 等纯函数，不需要 LLM，秒级完成。
              强制传 --ground-truth 文件（git timestamp 校验防事后追加）。
  pnl         跑 walk-forward paper trading，需要 LLM，按分钟计费。
              每 trial 做 sanity check + outlier 标记。

用法:
  uv run python -m scripts.research.sweep_runner --mode arithmetic \
    --param regime.trend_spread_atr_ratio --range 2.0,8.0,0.5 \
    --train-start 2018-01-01 --train-end 2023-12-31 \
    --assets NDQ.AX,GC=F \
    --ground-truth docs/wiki/sweep_ground_truth/regime_events.yaml
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EventResult:
    """单个 ground truth 事件的评估结果"""
    name: str
    expected_regime: str
    match_rate: float  # 该事件窗口内，regime 匹配期望的比例
    total_days: int
    matched_days: int


@dataclass
class TrialResult:
    """单个参数值的 trial 结果"""
    value: float
    event_results: List[EventResult] = field(default_factory=list)
    overall_match_rate: float = 0.0
    # P&L 模式字段
    annualized_return_pct: Optional[float] = None
    verdict_distribution: Optional[Dict[str, int]] = None
    status: str = "ok"  # ok / outlier / rerun_failed


@dataclass
class SweepResult:
    """sweep 结果汇总"""
    param: str
    mode: str
    results: List[TrialResult] = field(default_factory=list)
    ground_truth_path: Optional[str] = None
    outlier_count: int = 0


# ---------------------------------------------------------------------------
# Ground truth 校验
# ---------------------------------------------------------------------------

def _get_git_commit_time(path: Path) -> datetime:
    """获取文件最后一次 git commit 的时间。"""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ci", str(path)],
            capture_output=True, text=True, check=True,
        )
        ts = result.stdout.strip()
        # git %ci 格式: "2026-05-27 15:30:00 +0800"
        return datetime.fromisoformat(ts)
    except (subprocess.CalledProcessError, ValueError) as e:
        raise ValueError(
            f"无法获取 ground truth 文件的 git commit 时间: {path}\n"
            f"错误: {e}\n"
            f"文件必须已被 git 追踪。"
        ) from e


def _validate_ground_truth(ground_truth_path: Path) -> List[Dict[str, Any]]:
    """校验 ground truth 文件，返回 events 列表。"""
    if not ground_truth_path.exists():
        raise ValueError(
            f"纯算术 sweep 必须传入 ground truth 事件清单文件。\n"
            f"文件不存在: {ground_truth_path}\n"
            f"参考: docs/wiki/sweep_ground_truth/regime_events.yaml"
        )

    gt = yaml.safe_load(ground_truth_path.read_text(encoding="utf-8"))
    events = gt.get("events", [])
    if not events:
        raise ValueError(
            f"ground truth 文件中没有 events: {ground_truth_path}\n"
            f"至少需要 1 个事件。"
        )

    # git timestamp 校验：ground truth 必须在 sweep 之前 commit
    gt_commit_time = _get_git_commit_time(ground_truth_path)
    sweep_start_time = datetime.now(gt_commit_time.tzinfo)
    if gt_commit_time >= sweep_start_time:
        raise ValueError(
            f"ground truth 文件的 git commit 时间 ({gt_commit_time}) "
            f"晚于 sweep 开始时间 ({sweep_start_time})。\n"
            f"ground truth 必须在 sweep 之前 commit，防止事后追加事件。"
        )

    # 工作区干净校验：防止未提交修改绕过 git timestamp 检查
    import subprocess
    rel_path = str(ground_truth_path.resolve())
    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        rel_path = str(ground_truth_path.resolve().relative_to(repo_root))
    except (subprocess.CalledProcessError, ValueError):
        pass
    diff_rc = subprocess.call(
        ["git", "diff", "--quiet", "--", rel_path],
        stderr=subprocess.DEVNULL,
    )
    if diff_rc != 0:
        raise ValueError(
            f"ground truth 文件有未提交修改: {ground_truth_path}\n"
            f"请先 commit 再运行 sweep，防止事后篡改。"
        )

    return events


# ---------------------------------------------------------------------------
# Arithmetic sweep
# ---------------------------------------------------------------------------

def _evaluate_event(
    asset: str,
    event: Dict[str, Any],
    train_start: str,
    train_end: str,
    df_cache: dict[str, Any] | None = None,
) -> EventResult:
    """评估单个 asset 在单个 ground truth 事件上的 regime 分类准确率。"""
    from openinvest.core.config import load_config
    from openinvest.core.regime import classify_regime
    from openinvest.db.market_store import MarketStore
    from openinvest.utils.market_metrics import compute_metrics

    expected = event["expected_regime"]
    event_start = event["start"]
    event_end = event["end"]

    # 全量历史（不走 get_history_data 的 730 天 cap）——sweep 必须看到 2008/2020/2022
    # 真危机窗口才能验证 regime（尤其 crash）阈值。纯读已 backfill 的 DB，不触发 yfinance。
    # per-asset 缓存避免重复 IO。
    if df_cache is not None and asset in df_cache:
        df = df_cache[asset]
    else:
        df = MarketStore().get_history_df(asset, days=100_000)
        if df_cache is not None:
            df_cache[asset] = df
    if df is None or df.empty:
        return EventResult(
            name=event["name"], expected_regime=expected,
            match_rate=0.0, total_days=0, matched_days=0,
        )

    import pandas as pd

    # 归一化时区（yfinance 可能返回 tz-aware index，事件日期是 tz-naive）
    if df.index.tz is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)

    # 截取事件窗口内能算出 metrics 的日期
    start_dt = pd.to_datetime(event_start)
    end_dt = pd.to_datetime(event_end)
    mask = (df.index >= start_dt) & (df.index <= end_dt)
    event_df = df[mask]
    if event_df.empty:
        return EventResult(
            name=event["name"], expected_regime=expected,
            match_rate=0.0, total_days=0, matched_days=0,
        )

    # 每天算 regime，检查是否匹配期望
    matched = 0
    total = 0
    for date in event_df.index:
        # 截取到该日为止的数据算 metrics
        df_cut = df[df.index <= date]
        if len(df_cut) < 30:
            continue
        metrics = compute_metrics(df_cut)
        result = classify_regime(metrics, symbol=asset)
        regime = result.get("regime", "unknown")
        total += 1
        if regime == expected:
            matched += 1

    rate = matched / total if total > 0 else 0.0
    return EventResult(
        name=event["name"], expected_regime=expected,
        match_rate=rate, total_days=total, matched_days=matched,
    )


def run_arithmetic_sweep(
    param: str,
    values: List[float],
    train_start: str,
    train_end: str,
    assets: List[str],
    ground_truth_path: Path,
) -> SweepResult:
    """纯算术 sweep — 只跑 classify_regime() 等纯函数，不需要 LLM。"""
    from openinvest.core.config import reset_config, set_config_override

    events = _validate_ground_truth(ground_truth_path)

    # 解析参数路径: "regime.trend_spread_atr_ratio" → section="regime", key="trend_spread_atr_ratio"
    parts = param.split(".")
    if len(parts) != 2:
        raise ValueError(f"参数格式必须是 section.key，如 regime.trend_spread_atr_ratio，实际: {param}")
    section, key = parts

    results: List[TrialResult] = []
    df_cache: dict[str, Any] = {}  # per-asset 历史数据缓存，避免重复 yfinance IO
    for value in values:
        # 注入参数（同时覆盖全局 + 所有 per-asset，确保 sweep 生效）
        override = {section: {key: value}}
        # #113：per-asset override 已删——regime 阈值尺度无关，单一 override 即全资产生效
        set_config_override(override)

        trial = TrialResult(value=value)
        for event in events:
            for asset in assets:
                er = _evaluate_event(asset, event, train_start, train_end, df_cache=df_cache)
                trial.event_results.append(er)

        # 整体匹配率 = 所有事件所有 asset 的加权平均
        total_days = sum(e.total_days for e in trial.event_results)
        matched_days = sum(e.matched_days for e in trial.event_results)
        trial.overall_match_rate = matched_days / total_days if total_days > 0 else 0.0
        results.append(trial)

    # 清理 config
    reset_config()

    return SweepResult(
        param=param,
        mode="arithmetic",
        results=results,
        ground_truth_path=str(ground_truth_path),
    )


# ---------------------------------------------------------------------------
# P&L sweep (框架，不含实际 LLM 调用)
# ---------------------------------------------------------------------------

def _trial_sanity_check(trial_result: Dict[str, Any]) -> str:
    """每个 P&L trial 跑完后做 sanity check。返回 'ok' / 'outlier'。"""
    verdict_dist = trial_result.get("verdict_distribution", {})
    total = sum(verdict_dist.values())
    if total == 0:
        return "outlier"  # 无 verdict = LLM 完全失败

    # 1. UNCLEAR 率 > 30% = LLM 输出质量差
    unclear_rate = verdict_dist.get("UNCLEAR", 0) / total
    if unclear_rate > 0.30:
        return "outlier"

    # 2. 单一 verdict 占比 > 90% = verdict 塌缩（100% HOLD 复读机）
    max_verdict_rate = max(verdict_dist.values()) / total
    if max_verdict_rate > 0.90:
        return "outlier"

    return "ok"


def _detect_outliers_mad(returns: List[float]) -> List[bool]:
    """跨 trial 的 outlier 检测：年化收益偏离中位数 > 3 倍 MAD。"""
    import numpy as np
    arr = np.array(returns)
    median_return = np.median(arr)
    mad = np.median(np.abs(arr - median_return))
    if mad < 1e-6:
        return [False] * len(returns)  # 所有 trial 收益相同，无 outlier
    return [bool(abs(r - median_return) > 3 * 1.4826 * mad) for r in returns]


def run_pnl_sweep(
    param: str,
    values: List[float],
    train_start: str,
    train_end: str,
    assets: List[str],
) -> SweepResult:
    """P&L 模式 sweep（框架）。

    实际 LLM 调用需要集成 committee_runner，此处只验证 runner 能启动 + 接受参数。
    真正的 P&L sweep 在单独决定预算后执行。
    """
    parts = param.split(".")
    if len(parts) != 2:
        raise ValueError(f"参数格式必须是 section.key，如 reward.weight_max_drawdown，实际: {param}")

    log.info(
        f"P&L sweep 框架已就绪: param={param}, values={values}, "
        f"train={train_start}~{train_end}, assets={assets}\n"
        f"注意: 实际 P&L trial 需要集成 committee_runner（LLM 调用），"
        f"本次只验证 runner 能启动 + 接受参数。"
    )

    # 框架模式：不实际跑 trial，返回占位结果
    results = []
    for value in values:
        results.append(TrialResult(
            value=value,
            status="pending",
        ))

    return SweepResult(
        param=param,
        mode="pnl",
        results=results,
    )


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------

def _format_arithmetic_table(result: SweepResult) -> str:
    """格式化 arithmetic sweep 结果为对比表。"""
    lines = [
        f"Parameter: {result.param}",
        f"Ground truth: {result.ground_truth_path}",
        f"Mode: {result.mode}",
        "",
    ]

    # 表头
    header = f"{'Value':>10} | {'Match Rate':>10} | {'Events Detail'}"
    lines.append(header)
    lines.append("-" * len(header))

    for trial in result.results:
        # 每个事件的匹配率
        event_details = ", ".join(
            f"{e.name}: {e.match_rate:.0%}({e.matched_days}/{e.total_days})"
            for e in trial.event_results
        )
        lines.append(
            f"{trial.value:>10.1f} | {trial.overall_match_rate:>9.0%} | {event_details}"
        )

    # 最佳参数
    if result.results:
        best = max(result.results, key=lambda t: t.overall_match_rate)
        lines.append("")
        lines.append(f"Best: {result.param}={best.value:.1f} (match rate: {best.overall_match_rate:.0%})")

    return "\n".join(lines)


def _format_pnl_table(result: SweepResult) -> str:
    """格式化 P&L sweep 结果为对比表。"""
    lines = [
        f"Parameter: {result.param}",
        f"Mode: {result.mode} (框架模式，未实际执行)",
        "",
    ]

    header = f"{'Value':>10} | {'Status':>10} | {'Return':>10} | {'Verdict Dist'}"
    lines.append(header)
    lines.append("-" * len(header))

    for trial in result.results:
        ret = f"{trial.annualized_return_pct:+.2f}%" if trial.annualized_return_pct is not None else "N/A"
        dist = str(trial.verdict_distribution) if trial.verdict_distribution else "N/A"
        lines.append(f"{trial.value:>10.2f} | {trial.status:>10} | {ret:>10} | {dist}")

    if result.outlier_count > 0:
        lines.append(f"\nOutliers: {result.outlier_count} (excluded from summary)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_range(range_str: str) -> List[float]:
    """解析 range 字符串: '2.0,8.0,0.5' → [2.0, 2.5, 3.0, ..., 8.0]"""
    parts = [float(x.strip()) for x in range_str.split(",")]
    if len(parts) == 3:
        start, end, step = parts
        values = []
        v = start
        while v <= end + step * 0.01:  # 浮点容差
            values.append(round(v, 10))
            v += step
        return values
    elif len(parts) == 1:
        return parts
    else:
        # 逗号分隔的离散值
        return parts


def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(
        description="openInvest 参数 sweep runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mode", choices=["arithmetic", "pnl"], required=True,
                        help="sweep 模式: arithmetic (纯算术) / pnl (需要 LLM)")
    parser.add_argument("--param", required=True,
                        help="要 sweep 的参数，格式: section.key (如 regime.trend_spread_atr_ratio)")
    parser.add_argument("--range", required=True,
                        help="参数范围: start,end,step (如 2.0,8.0,0.5)")
    parser.add_argument("--train-start", required=True,
                        help="训练集起始日期 (YYYY-MM-DD)")
    parser.add_argument("--train-end", required=True,
                        help="训练集结束日期 (YYYY-MM-DD)")
    parser.add_argument("--assets", required=True,
                        help="资产列表，逗号分隔 (如 NDQ.AX,GC=F)")
    parser.add_argument("--ground-truth", type=Path, default=None,
                        help="ground truth 事件清单文件路径 (arithmetic 模式必传)")

    args = parser.parse_args(argv)
    values = parse_range(args.range)
    assets = [a.strip() for a in args.assets.split(",")]

    if args.mode == "arithmetic":
        if args.ground_truth is None:
            raise ValueError(
                "纯算术 sweep 必须传入 --ground-truth 文件。\n"
                "参考: docs/wiki/sweep_ground_truth/regime_events.yaml"
            )
        result = run_arithmetic_sweep(
            param=args.param,
            values=values,
            train_start=args.train_start,
            train_end=args.train_end,
            assets=assets,
            ground_truth_path=args.ground_truth,
        )
        print(_format_arithmetic_table(result))
    elif args.mode == "pnl":
        result = run_pnl_sweep(
            param=args.param,
            values=values,
            train_start=args.train_start,
            train_end=args.train_end,
            assets=assets,
        )
        print(_format_pnl_table(result))

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
