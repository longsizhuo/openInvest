"""cio_parse — CIO memo 解析 + Sanity-check 后处理 + 集中度覆写（从 core/committee.py 拆分，逻辑逐字不变）。

职责：verdict/confidence/alloc 等正则 + `parse_cio_memo`（含 7 道 sanity check）
+ `regime_label_from_text` / `atr_defense_from_text` 确定性文本提取
+ THRESHOLDS dict（向后兼容导出，import 时即求值）+ `_force_hold`
+ portfolio_summary 集中度提取 `_extract_concentration_from_summary`
+ Risk Officer 输出集中度覆写 `_override_concentration_in_risk_output`。
inner import（core.config.load_config）保持函数内，set_config_override 实时生效。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from openinvest.core.committee.agent_io import AGENT_UNAVAILABLE_MARKER

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Verdict 解析
# ----------------------------------------------------------------------

# 字段行容忍 markdown 加粗（**VERDICT**: HOLD / VERDICT: **HOLD**）：键后 `\**`，值前 `[\s*]*`
VERDICT_RE = re.compile(r"VERDICT\**:[\s*]*(BUY|ACCUMULATE|HOLD|TRIM|SELL)", re.I)
# 数字只取 \d*\.?\d+：句末的 "0.72." 不能把句号吃进去（float 抛错 → 整轮作废）
CONFIDENCE_RE = re.compile(r"CONFIDENCE\**:[\s*]*(\d*\.?\d+)")
DOMINANT_RE = re.compile(r"DOMINANT_VIEW\**:[\s*]*(quant|macro|risk)", re.I)
# 金额字段名带基准币种（SUGGESTED_ALLOC_CNY / _AUD …）；接受任意三字母，老存档照常解析。
# 值容忍 ¥5,000 / ￥5,000 / A$5,000 / AUD 5,000 / -¥3,000 / ¥-3000 / +5000，交给 _alloc_int 归一
# （旧式 (-?\d+) 把 10,000 读成 10、¥800 读成 0）。值前只吃 [ \t*]（不跨行）：值为空、下一行是
# "1. 执行计划" / "- 3000" 列表时不能误读成 1 / -3000。
ALLOC_RE = re.compile(
    r"SUGGESTED_ALLOC_[A-Z]{3}\**:[ \t*]*"
    r"([+\-−]?[ \t]*(?:[A-Za-z]{1,3}[ \t]*)?[¥￥$€£]?[ \t]*[+\-−]?[ \t]*\d[\d,]*)"
)
_ALLOC_LINE_RE = re.compile(r"SUGGESTED_ALLOC_[A-Z]{3}\**:[^\n]*")
_ALLOC_KEY_RE = re.compile(r"suggested_alloc_[a-z]{3}")
TRIM_REASON_RE = re.compile(r"TRIM_REASON\**:[\s*]*(concentration|stop_loss|bearish)", re.I)
# TRIM 路径化：买回点 + 预期路径（EXECUTION_PLAN/RISK_PLAN 仍丢弃，只解析这三个）
REENTRY_PRICE_RE = re.compile(r"REENTRY_PRICE\**:[\s*]*[¥$]?\s*(-?[\d,]+(?:\.\d+)?)", re.I)
REENTRY_CONDITION_RE = re.compile(r"REENTRY_CONDITION:\s*(.+)")
EXPECTED_PATH_RE = re.compile(r"EXPECTED_PATH:\s*(.+)")
# regime 标签（format_regime_brief 输出首行 / coordinator transcript 里的同款行）
REGIME_LABEL_RE = re.compile(r"^REGIME:\s*([a-z_]+)\s*$", re.MULTILINE)
# worker 失败哨兵的完整前缀（agent_io._ask 的唯一产出格式）
_WU_SENTINEL = f"{AGENT_UNAVAILABLE_MARKER} reason="
# 独立快崩防御 ATR 腿：从 format_regime_brief 的确定性 INPUTS 行提取波动突变比
ATR_SPIKE_RE = re.compile(r"\batr_spike_ratio=([\d.]+)")


def _alloc_int(raw: Any) -> int:
    """建议金额 → 整数（基准币种）。数字直接取整；字符串去货币符号 / 千分位 / 加粗，
    负号在货币符号前后都认（-¥3,000 / ¥-3000）；读不出数字 → 0 + warning。JSON 与正则两条路径共用。
    字符串先按纯数字试 float（JSON 路径的 "1e4" → 10000，不能截成 1），失败再走正则。"""
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw or "")
    try:
        return int(float(s.replace(",", "")))
    except (ValueError, OverflowError):
        pass
    m = re.search(r"\d[\d,]*", s)
    if not m:
        if s.strip():
            log.warning("parse_cio_memo: 金额字段读不出数字 → 0: %r", s[:80])
        return 0
    n = int(m.group().replace(",", ""))
    return -n if any(c in s[:m.start()] for c in "-−") else n


def regime_label_from_text(text: str) -> Optional[str]:
    """从确定性文本（regime_brief / coordinator transcript）提取 regime 标签。

    给 parse_cio_memo 的 risk_profile 后处理用：来源是系统自己
    format_regime_brief 生成的 `REGIME: <label>` 行，不是 LLM 输出。
    """
    m = REGIME_LABEL_RE.search(text or "")
    return m.group(1) if m else None


def atr_defense_from_text(text: str) -> bool:
    """从 coordinator transcript 判断独立快崩防御的 ATR 腿是否触发。

    波动突变比从 transcript 里粘贴的 format_regime_brief 确定性 INPUTS 行提取
    （`INPUTS: ..., atr_spike_ratio=2.3456, ...`），与
    sentiment.atr_defense_spike_ratio（通用口径，尺度无关，无 per-asset 数字）
    比较——与 direct 路径（committee_runner 从 metrics 直读）同一条线。
    缺字段 / 值为 None → False（graceful，不阻断解析）。
    """
    m = ATR_SPIKE_RE.search(text or "")
    if not m:
        return False
    try:
        from openinvest.core.config import load_config
        return float(m.group(1)) >= load_config().sentiment.atr_defense_spike_ratio
    except ValueError:
        return False


# ============ Sanity-check 阈值（单点维护，便于调参）============
#
# 统一抽到这里，避免散落 magic number。每条都附"为什么这个数"的来源说明，
# 否则未来无法判断该改还是不该改。
#
# 调参流程：
#   1. 改 core/config/defaults.yaml（或 set_config_override()）
#   2. 同步 docs/wiki/02-agents.md "CIO Sanity Check" 表
#   3. tests/test_committee_parser.py 跑一遍
#
# Step 3a: 从 config 读取，向后兼容保留 dict 形式
def _build_thresholds_from_config() -> Dict[str, float]:
    """从 config 构建 THRESHOLDS dict。"""
    from openinvest.core.config import load_config
    cfg = load_config().verdict
    return {
        "buy_confidence_overdrive": cfg.buy_confidence_overdrive,
        "buy_confidence_downgrade_to": cfg.buy_confidence_downgrade_to,
        "alloc_cny_ceiling": cfg.alloc_cny_ceiling,
        "worker_unavailable_confidence_floor": cfg.worker_unavailable_confidence_floor,
    }


THRESHOLDS: Dict[str, float] = _build_thresholds_from_config()


def _force_hold(out: Dict[str, Any], *, confidence_ceiling: float) -> None:
    """统一 force-HOLD 后处理：verdict→HOLD、confidence 压到 ≤ ceiling、alloc_cny→0。

    多个 sanity check（worker 输入失败 / 集中度 lens 关闭）最终都要把裁决
    钉成"HOLD 且不带方向性信号强度"。集中到一处，避免每个 check 各自手抄时漏步
    —— Sanity 3 历史上 force HOLD 却没归零 alloc_cny，导致 HOLD 仍带建议金额。
    调用方负责记录各自的 _original_* 溯源（语义不同，不放进这里）。
    """
    out["verdict"] = "HOLD"
    out["confidence"] = min(out["confidence"], confidence_ceiling)
    out["alloc_cny"] = 0


def _fields_to_out(out: Dict[str, Any], fields: Dict[str, Any]) -> None:
    """从结构化 JSON fields（DeepSeek JSON Output）填 out 基础字段，与 regex 路径同口径
    （verdict&dominant_view&trim_reason 限定集 / confidence float / alloc int）。
    类型异常一律退化到与"regex 没匹配"等价的默认值，不抛。"""
    # 与 VERDICT_RE 同口径：非 5 个合法值 → UNCLEAR。否则 'STRONG_BUY' / 回显模板
    # 'BUY|ACCUMULATE|...' 之类原样绕过下方 sanity/快崩防御，直进邮件标题和正文。
    v = str(fields.get("verdict") or "").strip().upper()
    if v in ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"):
        out["verdict"] = v
    else:
        out["verdict"] = "UNCLEAR"
        if v:
            log.warning("parse_cio_memo: JSON verdict 非法值 %r → UNCLEAR", fields.get("verdict"))
    try:
        out["confidence"] = float(fields.get("confidence") or 0.0)
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    dv = str(fields.get("dominant_view") or "").strip().lower()
    out["dominant_view"] = dv if dv in ("quant", "macro", "risk") else "tie"
    try:
        alloc = next((v for k, v in fields.items() if _ALLOC_KEY_RE.fullmatch(str(k))), None)
        out["alloc_cny"] = _alloc_int(alloc)
    except (TypeError, ValueError, OverflowError):
        out["alloc_cny"] = 0
    tr = fields.get("trim_reason")
    tr = str(tr).lower() if tr else None
    out["trim_reason"] = tr if tr in ("concentration", "stop_loss", "bearish") else None
    rp = fields.get("reentry_price")
    try:
        out["reentry_price"] = float(rp) if rp is not None else None
    except (TypeError, ValueError):
        out["reentry_price"] = None
    rc = fields.get("reentry_condition")
    rc = str(rc).strip() if rc else None
    out["reentry_condition"] = rc if (rc and rc.upper() != "N/A") else None
    ep = fields.get("expected_path")
    ep = str(ep).strip() if ep else None
    out["expected_path"] = ep if (ep and ep.upper() != "N/A") else None


def parse_cio_memo(
    text: str,
    *,
    fields: Optional[Dict[str, Any]] = None,
    worker_brief: Optional[str] = None,
    current_price: Optional[float] = None,
    regime: Optional[str] = None,
    defense_flag_on: bool = False,
    defense_dca: Optional[Dict[str, Any]] = None,
    alloc_ceiling: Optional[int] = None,
    single_invest_cap: Optional[int] = None,
) -> Dict[str, Any]:
    """fields（DeepSeek JSON Output 解析后的 dict）非 None 时走结构化抽取，否则 regex（旧路径）。
    worker_brief（CIO 输入的 worker 简报）非 None 时额外参与 [WORKER_UNAVAILABLE] backstop——
    JSON 模式下 CIO 输出是纯 JSON 不回显哨兵，靠 brief 才查得到 worker 失败（加性，不动旧文本检测）。

    defense_dca（2026-06-13 黄金裁决，wiki18 §5）：非 None = 本资产是黄金且分批 DCA
    启用，防御触发的买侧不再全拦，改"放行一批 or 按 spacing/quota 拦"。service 层算好传：
        {"allow": bool, "fraction": float, "reason": str, "tranche_idx": int}
    None = 非黄金/未启用 → 走旧"全拦"行为。两条腿已 OR 成单 defense_flag_on，单一计划。

    alloc_ceiling：单笔建议金额上限（基准币种）。调用方用 utils.base_currency.alloc_ceiling()
    算好传入（本模块是纯计算层，不碰汇率）；None = 配置值原数（人民币）。

    single_invest_cap（Sanity 6）：用户在 strategy.md 设的单次入场上限（基准币种，调用方用
    utils.base_currency.single_invest_cap(target, strategy) 换好传入；标的级优先于策略级）。
    None / 0 = 不设限。只钳买侧正金额，方向不变。
    """
    out: Dict[str, Any] = {"raw": text}
    if fields is not None:
        _fields_to_out(out, fields)
    else:
        m = VERDICT_RE.search(text)
        out["verdict"] = m.group(1).upper() if m else "UNCLEAR"
        m = CONFIDENCE_RE.search(text)
        out["confidence"] = float(m.group(1)) if m else 0.0
        m = DOMINANT_RE.search(text)
        out["dominant_view"] = m.group(1).lower() if m else "tie"
        m = ALLOC_RE.search(text)
        out["alloc_cny"] = _alloc_int(m.group(1)) if m else 0
        if not m and (line := _ALLOC_LINE_RE.search(text)):
            log.warning("parse_cio_memo: 金额字段读不出数字 → 0: %r", line.group(0)[:80])
        m = TRIM_REASON_RE.search(text)
        out["trim_reason"] = m.group(1).lower() if m else None

        # TRIM 路径化字段：买回价 / 买回条件 / 预期路径（真正解析并保留，不再全丢）
        m = REENTRY_PRICE_RE.search(text)
        out["reentry_price"] = float(m.group(1).replace(",", "")) if m else None
        m = REENTRY_CONDITION_RE.search(text)
        rc = m.group(1).strip() if m else None
        out["reentry_condition"] = rc if (rc and rc.upper() != "N/A") else None
        m = EXPECTED_PATH_RE.search(text)
        ep = m.group(1).strip() if m else None
        out["expected_path"] = ep if (ep and ep.upper() != "N/A") else None

    # Sanity check 0: INVEST_CIO_CONFIDENCE_CAP（Optuna 训练参数）clamp confidence 上限
    cap_env = os.getenv("INVEST_CIO_CONFIDENCE_CAP")
    if cap_env:
        try:
            cap = float(cap_env)
            if out["confidence"] > cap:
                out["_original_confidence_cap"] = out["confidence"]
                out["confidence"] = cap
        except ValueError:
            pass

    # Sanity check 1: 防 prompt injection / LLM 过度自信
    # 从 config 读取（set_config_override() 实时生效）
    from openinvest.core.config import load_config
    _verdict_cfg = load_config().verdict
    confidence_threshold = _verdict_cfg.buy_confidence_overdrive
    confidence_downgrade = _verdict_cfg.buy_confidence_downgrade_to
    if out["verdict"] == "BUY" and out["confidence"] >= confidence_threshold:
        out["_original_verdict"] = "BUY"
        out["_original_confidence"] = out["confidence"]
        out["verdict"] = "ACCUMULATE"
        out["confidence"] = confidence_downgrade
        log.warning(
            "parse_cio_memo: 降级 BUY(%s) → ACCUMULATE(%s) — 防 LLM 过度自信 / prompt injection",
            out["_original_confidence"], confidence_downgrade,
        )

    # Sanity check 2: alloc_cny 合理性 clamp
    # INVEST_ALLOC_AGGRESSIVENESS（Optuna 训练参数，0.05~0.30）会按 baseline ¥100k
    # 收紧 ceiling 到 100_000 × agg。例如 agg=0.10 → 单笔 ≤ ¥10k，避免 LLM 给
    # ¥50k alloc 导致 simulator 大量 SKIP。
    if alloc_ceiling is None:
        alloc_ceiling = _verdict_cfg.alloc_cny_ceiling
    agg_env = os.getenv("INVEST_ALLOC_AGGRESSIVENESS")
    if agg_env:
        try:
            # 100_000 是人民币基线；ceiling 已按基准币种换算，同比例换算基线（CNY 时比例为 1，逐位不变）
            _scale = alloc_ceiling / (_verdict_cfg.alloc_cny_ceiling or 1)
            alloc_ceiling = min(alloc_ceiling, int(100_000 * _scale * float(agg_env)))
        except ValueError:
            pass
    if abs(out["alloc_cny"]) > alloc_ceiling:
        log.warning(
            "parse_cio_memo: alloc_cny=%s 超出合理区间，clamp 到 ±%s",
            out["alloc_cny"], alloc_ceiling,
        )
        out["_original_alloc"] = out["alloc_cny"]
        out["alloc_cny"] = max(-alloc_ceiling, min(alloc_ceiling, out["alloc_cny"]))

    # Sanity check 3（audit algo M4）: worker 输入失败时 confidence 降级
    # 上游传来的 raw 是 brief，含 macro/quant/risk 内容；如果 brief 里出现 worker
    # unavailable 哨兵，CIO 大概率是在 garbage 上综合
    # 只认哨兵的产出格式（agent_io._ask 恒为 "[WORKER_UNAVAILABLE] reason=..."）：裸子串会把
    # CIO 自己写的"无 [WORKER_UNAVAILABLE] 标记"当成 worker 失败——2026-09~10 live 6 次
    # 强制 HOLD 全是这种误判（阶段四 R4 核查）。
    floor = _verdict_cfg.worker_unavailable_confidence_floor
    _wu = _WU_SENTINEL in text or (
        worker_brief is not None and _WU_SENTINEL in worker_brief
    )
    # Failed input invalidates the directional proposal independently of how
    # confident the CIO is. The floor is a ceiling on the resulting confidence,
    # not a condition for enabling the failure backstop.
    if _wu:
        out.setdefault("_original_verdict", out["verdict"])
        out.setdefault("_original_alloc", out["alloc_cny"])
        out["_original_confidence_unavailable"] = out["confidence"]
        _force_hold(out, confidence_ceiling=floor)
        log.warning("parse_cio_memo: 检测到 [WORKER_UNAVAILABLE] 标记，"
                    "强制 verdict=HOLD + confidence≤floor + alloc=0")

    # Sanity check 4: 用户关掉集中度 lens（concentration_lens_enabled=False — 单资产 /
    # 刻意集中 / 全可投资金池）→ 无条件 force-HOLD 掉 TRIM_REASON=concentration 的减仓。
    # 2026-06-23 移除 solvency 自动兜底（曾："兜底充足 ⇒ 账户内集中度高不算风险"）：它只在
    # parse 层事后反转 CIO 减仓、prompt 层却不知情 → 既自相矛盾又掩盖真实集中度风险。现在
    # 集中度是否构成约束只由这一个显式开关说了算。本检查是硬兜底；lens 关时 prompt 层
    # （capabilities/committee/cio.py + risk_officer.py）同步软抑制，防 LLM 把超配换标签成 bearish 绕过。
    _lens_off = not _verdict_cfg.concentration_lens_enabled
    if (_lens_off
            and out["verdict"] == "TRIM"
            and out.get("trim_reason") == "concentration"):
        out["_original_verdict"] = "TRIM"
        out["_original_trim_reason"] = "concentration"
        # 标记 lens 关闭触发，供 intervention 反事实账本 / daily_report 留痕。
        out["_concentration_lens"] = "disabled"
        # setdefault: 若 Sanity 1/2 已记录更早的原值（如 Sanity 2 的 pre-clamp
        # alloc），不要被这里覆盖丢掉真正的原始值。
        out.setdefault("_original_confidence", out["confidence"])
        out.setdefault("_original_alloc", out["alloc_cny"])
        out["trim_reason"] = None
        _force_hold(out, confidence_ceiling=_verdict_cfg.forced_hold_confidence_ceiling)
        log.warning("parse_cio_memo: 集中度 lens 已关闭（config）+ TRIM(concentration) → 强制 HOLD")

    # Sanity check 5: TRIM 必须给出"低于现价的买回点"，否则降级 HOLD
    # 卖出后买回点缺失 or 不低于现价 = 卖了高价大概率接回 = 纯亏，TRIM 不成立。
    # 只在拿得到 current_price 的 live 路径校验（re-parse 存档时 current_price=None 跳过）。
    if (current_price is not None
            and current_price > 0
            and out["verdict"] == "TRIM"):
        rp = out.get("reentry_price")
        if rp is None or rp >= current_price:
            out["_original_verdict"] = "TRIM"
            out.setdefault("_original_alloc", out.get("alloc_cny"))
            out["_sanity5_reason"] = (
                "reentry_missing" if rp is None else "reentry_not_below_current"
            )
            out["_current_price"] = current_price
            # 走统一 force-HOLD：verdict→HOLD + alloc→0 + confidence 压顶。手设
            # verdict="HOLD" 会漏归零 alloc_cny，让 TRIM 的负 alloc 存活下去
            # （_force_hold docstring 记载的 Sanity 3 同款历史 bug）。
            _force_hold(out, confidence_ceiling=out["confidence"])
            log.warning(
                "parse_cio_memo: TRIM 但买回点%s → 强制 HOLD（卖出后买不回更低 = 纯亏，TRIM 不成立）",
                "缺失" if rp is None else f"{rp} ≥ 现价 {current_price}（报价币种）",
            )

    # 独立快崩防御（Defense check）: VIX 哨兵（市场级）/ ATR 飙升（资产级）任一
    # 触发 → 确定性降级买侧 verdict。把 CIO SKILL 里的降级规则从 prompt 搬进代码强制。
    # 背景：MA120 regime 看不见快速崩盘（COVID 全程被分类 uptrend），原 crash 锁因
    # 双条件（ATR + 30d 回撤确认）永不触发——防御必须独立于 regime、只取快腿。
    # 只拦"往快崩里加仓"，不强制卖出：VIX/ATR 飙升常在恐慌底部，确定性强制卖出
    # 反而高买低卖；卖出判断留给委员会。
    if defense_flag_on and out["verdict"] in ("BUY", "ACCUMULATE"):
        out.setdefault("_original_verdict", out["verdict"])
        if defense_dca is not None:
            # 黄金分批 DCA（2026-06-13 裁决）：不全拦，放行一批 or 按 spacing/quota 拦。
            # 保护左尾(挤兑深跌)同时尊重中位右偏(典型涨,不该禁)。
            out.setdefault("_original_alloc", out["alloc_cny"])
            if defense_dca.get("allow"):
                frac = float(defense_dca.get("fraction", 0.3333))
                out["verdict"] = "ACCUMULATE"   # 买侧但小批
                out["alloc_cny"] = int(round(out["alloc_cny"] * frac))
                out["_defense_dca"] = "tranche"
                out["_defense_dca_tranche_idx"] = int(defense_dca.get("tranche_idx", 1))
                log.warning(
                    "parse_cio_memo: 黄金防御分批 DCA 放行第 %s 批（×%.2f 意图金额=%s）",
                    out["_defense_dca_tranche_idx"], frac, out["alloc_cny"],
                )
            else:
                out["_defense_dca"] = f"blocked_{defense_dca.get('reason', 'spacing')}"
                out["verdict"] = "HOLD"
                out["alloc_cny"] = 0
                log.warning(
                    "parse_cio_memo: 黄金防御分批 DCA 本批暂拦（%s，等满 spacing/quota）",
                    out["_defense_dca"],
                )
        elif out["verdict"] == "BUY":
            out["_defense_downgrade"] = "buy_to_accumulate"
            out["verdict"] = "ACCUMULATE"
            log.warning(
                "parse_cio_memo: 快崩防御触发（VIX 哨兵/ATR 飙升）→ %s（确定性降级，独立于 regime）",
                out["_defense_downgrade"],
            )
        else:
            out["_defense_downgrade"] = "accumulate_to_hold"
            out.setdefault("_original_alloc", out["alloc_cny"])
            out["verdict"] = "HOLD"
            out["alloc_cny"] = 0
            log.warning(
                "parse_cio_memo: 快崩防御触发（VIX 哨兵/ATR 飙升）→ %s（确定性降级，独立于 regime）",
                out["_defense_downgrade"],
            )

    # 风险档 aggressive: uptrend 顺势加仓杠杆（显式风险偏好，不是 regime 智能）。
    # 2026-06 消融结论：原 prompt 层 uptrend 方向锁 = 纯杠杆（牛市 +CR，代价
    # −Sharpe + 深 MaxDD + 熊市更亏）。拆锁后把杠杆效应保留为显式 config 开关，
    # 默认 steady（不开）。确定性后处理，不依赖 prompt。约束：
    # - 只升级 HOLD→ACCUMULATE，不动 TRIM/SELL（防御性卖出不被杠杆覆盖）、不动 BUY
    # - INDEP_DEFENSE_FLAG=on（VIX 快崩哨兵）时跳过——MA regime 看不见快速崩盘，
    #   杠杆挂在滞后的假 uptrend 上正是最大伤害源
    if (_verdict_cfg.risk_profile == "aggressive"
            and regime == "uptrend"
            and not defense_flag_on
            and not _wu
            and out["verdict"] == "HOLD"):
        out.setdefault("_original_verdict", "HOLD")
        out["_risk_profile_applied"] = "aggressive_uptrend_hold_to_accumulate"
        out["verdict"] = "ACCUMULATE"
        log.warning(
            "parse_cio_memo: risk_profile=aggressive + uptrend → HOLD 升级 ACCUMULATE"
            "（显式杠杆档，消融口径 +CR −Sharpe）",
        )

    # Sanity check 6（必须是最后一道）: 用户单次入场上限（strategy.md max_single_invest_cny）。
    # 此前只在 prompt 里"建议 ≤ cap"（portfolio_summary 一行），解析层不执行——LLM 超了就原样
    # 进邮件/账本，唯一硬闸是 Sanity 2 的 ±100k"天文数字"钳。只钳买侧（正 alloc）：字段语义是
    # "入场"上限，减仓金额不受它约束（否则防御性卖出也被缩成小单）。方向不变、只改金额；记
    # intervention（rule=single_invest_cap）。放在所有改 verdict/alloc 的规则之后：若放在
    # Sanity 3/5/防御之前，后续规则把裁决打成 HOLD/0 时，"金额已钳制（方向不变）"那句和干预
    # 归因都会说错（PR #430 评审）。setdefault：Sanity 1/2 已记的更早原值不被覆盖。
    # verdict 守卫：HOLD/TRIM/SELL 带正数金额不是"入场"，不钳、不记干预、不加"钳制"句。
    if (single_invest_cap and out["alloc_cny"] > single_invest_cap
            and out["verdict"] in ("BUY", "ACCUMULATE")):
        out.setdefault("_original_verdict", out["verdict"])
        out.setdefault("_original_alloc", out["alloc_cny"])
        out["_single_invest_cap"] = int(single_invest_cap)
        log.warning(
            "parse_cio_memo: alloc_cny=%s 超过用户单次入场上限 %s，钳到上限（方向不变）",
            out["alloc_cny"], single_invest_cap,
        )
        out["alloc_cny"] = int(single_invest_cap)

    return out


def single_invest_cap_note(verdict: Dict[str, Any]) -> str:
    """Sanity 6 钳过金额时追加到 CIO memo 末尾的一句人话（transcript / 邮件 / CLI 都渲染 memo）；
    没钳返回 ""。调用方（debate / coordinator）负责拼进 memo。"""
    cap = verdict.get("_single_invest_cap")
    if not cap:
        return ""
    from openinvest.utils.base_currency import money
    orig = verdict.get("_original_alloc", cap)
    return (f"金额已按单次上限 {money(cap)} 钳制（每笔建议金额上限，非当日额度，可分多笔；"
            f"CIO 原建议 {money(orig)}，裁决方向不变）")


def rewrite_tags(out: Dict[str, Any]) -> List[str]:
    """从 parse_cio_memo 产出的 `_` 溯源键推回"哪些确定性规则改写了 CIO 原始裁决"。

    纯函数，只读 dict。标签词汇与 runner/intervention._intervention_record 的 rule 名对齐
    （defense_* / sanity5_* / config_concentration_lens_off），并补上干预账本不记的三类
    （sanity1 BUY 降档、sanity2 金额钳制、sanity3 worker 失败、aggressive 升档）。
    落进 transcript 的 Input Health 块，让 verdict_review 不再靠"confidence ≤ 0.4"猜强制 HOLD。
    """
    tags: List[str] = []
    orig_v = out.get("_original_verdict")
    if out.get("_defense_dca"):
        tags.append(f"defense_gold_dca_{out['_defense_dca']}")
    elif out.get("_defense_downgrade"):
        tags.append(f"defense_{out['_defense_downgrade']}")
    if out.get("_sanity5_reason"):
        tags.append(f"sanity5_{out['_sanity5_reason']}")
    if out.get("_original_trim_reason") == "concentration":
        tags.append("config_concentration_lens_off")
    if "_original_confidence_unavailable" in out:
        tags.append("sanity3_worker_unavailable")
    if out.get("_risk_profile_applied"):
        tags.append(str(out["_risk_profile_applied"]))
    if orig_v == "BUY" and "_original_confidence" in out and not tags:
        tags.append("sanity1_buy_overdrive")
    if "_single_invest_cap" in out:   # Sanity 6（PR #430）：买侧金额钳到用户单次入场上限
        tags.append("single_invest_cap")
    if "_original_alloc" in out and not tags:
        tags.append("sanity2_alloc_clamp")
    if "_original_confidence_cap" in out:
        tags.append("confidence_cap_env")
    return tags


def forced_hold(out: Dict[str, Any]) -> bool:
    """最终 HOLD 且原始裁决不是 HOLD = 被规则强制成 HOLD（与 review_calc 展示口径同定义）。"""
    return (str(out.get("verdict") or "").upper() == "HOLD"
            and out.get("_original_verdict") not in (None, "HOLD"))


def _extract_concentration_from_summary(
    portfolio_summary: str, sym: str,
) -> Optional[float]:
    """从 portfolio_summary 文本里提取指定 asset 的集中度数字（百分比，float）

    portfolio_summary 由 utils.portfolio_summary 拼装，含每行 asset 字面写出：
      - **<display_name>** (SYM) (channel): ..., **集中度 33.6%** (CNY 市值 ...)

    抓不到（asset 不在 summary / 格式异常）返回 None，让上层不做覆写。
    """
    if not portfolio_summary or not sym:
        return None
    # 同一行：(SYM) ... **集中度 N%**
    pattern = re.compile(
        rf"\({re.escape(sym)}\)[^\n]*?\*\*集中度\s+([\d.]+)\s*%\*\*",
    )
    m = pattern.search(portfolio_summary)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, IndexError):
        return None


# Risk Officer 输出里的集中度行匹配（容忍 % 缺失、空白变化）
_RISK_CONCENTRATION_RE = re.compile(
    r"^(\s*CONCENTRATION_PCT\s*:\s*)([\d.]+)\s*%?",
    re.MULTILINE,
)


def _strip_concentration_if_lens_off(risk_output: str) -> str:
    """集中度 lens 关（ADR-020 默认）时删掉 Risk 输出里的 CONCENTRATION_PCT 行。

    模板在 lens 关时已不要这个字段；模型照填（2026-10-11 插件实测：自算 16.7% 写进字段，
    CIO 引为"仓位仅 16.7%"）也不让它流到 CIO。散文里的占比删不掉，靠 prompt 约束。
    """
    from openinvest.core.config import load_config
    if not risk_output or load_config().verdict.concentration_lens_enabled:
        return risk_output
    return re.sub(r"(?m)^[ \t]*[*_`]*CONCENTRATION_PCT[*_`]*[ \t]*[:：].*(?:\n|$)", "", risk_output)


def _override_concentration_in_risk_output(
    risk_output: str, true_pct: Optional[float],
) -> str:
    """把 Risk Officer 输出里的 CONCENTRATION_PCT 强制覆写为 portfolio_summary 字面值

    背景（2026-05-20 漂移修复）：portfolio_summary 已显式喂入"**集中度 33.6%**"，
    但 Risk Officer LLM（provider 无关，当前 MiMo / 历史 DeepSeek 都见过）仍偶发
    hallucinate 编成 70.2%（同 prompt 前一日还能输出 33.4%）。Prompt 强约束 +
    service layer 覆写形成双层防御——LLM 不听话也能保证 CIO 看到真数。

    true_pct=None（portfolio_summary 也没给）时不动；
    Risk 没输出该字段时不补救（避免凭空注入）。
    """
    if true_pct is None or not risk_output:
        return risk_output
    formatted = f"{true_pct:.1f}%"

    def _sub(m: "re.Match[str]") -> str:
        llm_val = m.group(2)
        try:
            llm_float = float(llm_val)
        except ValueError:
            llm_float = -1.0
        # 容差 0.3% 内视为一致，不覆写（避免把 33.4 / 33.6 这种正常浮点 rounding
        # 误差当 hallucination 误改；33.6 - 33.4 在 IEEE 754 下 = 0.2000000...284，
        # 用 0.2 会卡边界，0.3 留余量但仍远低于真实 hallucination 量级 30%+）
        if abs(llm_float - true_pct) <= 0.3:
            return m.group(0)
        log.warning(
            "Risk Officer 集中度 hallucination 已覆写: LLM=%s%% → 真实=%s "
            "(portfolio_summary 字面值)",
            llm_val, formatted,
        )
        return f"{m.group(1)}{formatted}"

    new_output, _n = _RISK_CONCENTRATION_RE.subn(_sub, risk_output)
    return new_output


__all__ = [
    "VERDICT_RE",
    "CONFIDENCE_RE",
    "DOMINANT_RE",
    "ALLOC_RE",
    "TRIM_REASON_RE",
    "REENTRY_PRICE_RE",
    "REENTRY_CONDITION_RE",
    "EXPECTED_PATH_RE",
    "REGIME_LABEL_RE",
    "ATR_SPIKE_RE",
    "regime_label_from_text",
    "atr_defense_from_text",
    "rewrite_tags",
    "forced_hold",
    "_build_thresholds_from_config",
    "THRESHOLDS",
    "_force_hold",
    "parse_cio_memo",
    "single_invest_cap_note",
    "_extract_concentration_from_summary",
    "_RISK_CONCENTRATION_RE",
    "_override_concentration_in_risk_output",
    "_strip_concentration_if_lens_off",
]
