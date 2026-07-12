"""Dreaming 聚合纯核（域绑定纯模块，ADR-026）

从 jobs/dreaming.py 拆出的纯计算：macro 上下文离散化、候选标签/评分/slug。
评分读 config（含 ADR-008 locked 参数）——确定性配置，纯度契约放行。
三阶段主流程（读 dream jsonl / LLM 验伪 / 写 memory index）留在 dreaming.py。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Tuple

def _get_macro_buckets():
    """读 macro bucket 分桶阈值（实时，支持 set_config_override）。"""
    from openinvest.core.config import load_config
    return load_config().macro_buckets


def _classify_regime(ctx: Dict[str, float]) -> Tuple[str, ...]:
    """把上下文离散化为 regime tag（用于聚合）

    VIX/TNX 分桶阈值从 config 读取，set_config_override() 实时生效。
    """
    buckets = _get_macro_buckets()
    tags = []
    if "vix" in ctx:
        if ctx["vix"] < buckets.vix_low:
            tags.append("vix_low")
        elif ctx["vix"] < buckets.vix_high:
            tags.append("vix_mid")
        else:
            tags.append("vix_high")
    if "tnx" in ctx:
        if ctx["tnx"] < buckets.tnx_low:
            tags.append("tnx_low")
        elif ctx["tnx"] < buckets.tnx_high:
            tags.append("tnx_mid")
        else:
            tags.append("tnx_high")
    return tuple(sorted(tags))


def _label(c: Dict[str, Any]) -> str:
    """候选的动作标签：verdict（新 verdict-outcome 路径）优先，回落 action（旧成交路径）。"""
    return c.get("verdict") or c.get("action") or "?"


def _score(c: Dict[str, Any]) -> float:
    """综合评分。

    verdict-outcome 路径（带 'verdict' 字段）：纯可靠性评分 = 命中率 0.7 + 样本量 0.3。
      不再用 abs(avg_return) 加分——对 HOLD 而言"市场动得越大"恰恰说明 HOLD 越错，
      用绝对收益加分会反向奖励坏的 HOLD 模式。命中率已是方向正确性的唯一可信度量。
    旧成交路径（带 'action'）：保持原公式（命中率 0.5 + 收益绝对值 0.3 + 样本量 0.2），
      不影响既有 LLM-验伪契约测试。
    """
    sample = min(c["count"] / 10.0, 1.0)
    if "verdict" in c:
        if c.get("kind") == "caution":
            from openinvest.core.config import get_locked
            _, locked_dreaming, _, _ = get_locked()
            # lift-based caution 评分（2026-05-27 ADR 008）。旧公式用绝对 missed_up_rate，
            # 会把"单向上涨 regime 里 HOLD 必然踏空"当强信号（Phase1.5 假 caution 即此）。
            # 试金石：新公式同时拒绝 Phase1.5 假 caution（base_down≈0）和 post-cutoff 急跌
            # caution（V 反弹 base_down≈0），只在"真有下行风险 + HOLD 比基率更踏空"时接受。
            base_up = c.get("base_up")
            base_down = c.get("base_down")
            if base_up is None or base_down is None:
                return 0.0  # 无 regime 基率 → 无法判真伪 → 安全休眠
            if base_down < locked_dreaming.caution_min_base_down:
                return 0.0  # regime 无真实下行 → "踏空"是单向基率假象 → 非 caution
            lift = c.get("missed_up_rate", 0.0) - base_up
            if lift <= 0:
                return 0.0  # HOLD 没比该 regime 基率更频繁踏空 → 非真信号
            quality = min(lift / locked_dreaming.caution_lift_full, 1.0)
            return round(quality * 0.7 + sample * 0.3, 3)
        # reliable：方向判断越准越有价值 → 用 hit_rate（不变）
        return round(c["hit_rate"] * 0.7 + sample * 0.3, 3)
    avg = min(abs(c["avg_return_pct"]) / 5.0, 1.0)
    return round(c["hit_rate"] * 0.5 + avg * 0.3 + sample * 0.2, 3)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower()


def _candidate_slug(c: Dict[str, Any]) -> str:
    regime_tag = "_".join(c["regime"]) or "any"
    return _slugify(f"{c['asset']}_{_label(c)}_{regime_tag}_{c['window_days']}d")



__all__ = [
    "_get_macro_buckets",
    "_classify_regime",
    "_label",
    "_score",
    "_slugify",
    "_candidate_slug",
]
