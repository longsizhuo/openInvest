"""Inventory numerical decision rules and expose their authority, not just values."""
from __future__ import annotations

import ast

from dataclasses import asdict
from pathlib import Path
import re


# These are auditable rule families, not new trading thresholds. A missing
# rationale never authorizes an automatic production deletion or relaxation.
FAMILIES = [
    ("quant.entry_bands", "market_judgment", "capabilities/committee/quant/quant.md", "legacy_unvalidated", "Price quantile and RSI conjunction can suppress early recovery signals"),
    ("quant.crash_direction_lock", "semantic_defect", "capabilities/committee/quant/quant.md", "repaired", "Execution risk must be separate from market direction"),
    ("quant.cash_direction_lock", "semantic_defect", "capabilities/committee/quant/quant_rebuttal.md", "repaired", "Available cash is not evidence of price direction"),
    ("cio.duplicate_defense", "semantic_defect", "capabilities/committee/cio/cio.md", "repaired", "Raw intent is preserved; the backend applies defense and permitted tranches once"),
    ("cio.consensus_confidence", "semantic_defect", "capabilities/committee/cio/cio.md", "repaired", "Agreement is not independent evidence or calibrated probability"),
    ("risk.strength_bands", "market_judgment", "capabilities/committee/risk_officer/risk_officer_rebuttal.md", "legacy_unvalidated", "LLM strength 7/3 is not a calibrated market probability"),
    ("cio.sell_conjunction", "market_judgment", "capabilities/committee/cio/cio.md", "legacy_unvalidated", "Joint macro/risk labels can hide otherwise valid exit alternatives"),
    ("cio.reentry_requirement", "semantic_defect", "core/committee/cio_parse.py", "repaired", "Permanent risk exits do not require a lower repurchase price"),
    ("path.terminal_vs_touch", "semantic_defect", "core/regime_probability.py", "repaired", "Terminal return probability cannot substitute for first-touch probability"),
    ("cio.worker_failure", "input_integrity", "core/committee/cio_parse.py", "repaired", "Missing evidence must disable action independently of confidence and risk profile"),
    ("dreaming.score_gate", "research_diagnostic", "jobs/dreaming.py", "shadow_only", "Legacy score is recorded, not used to establish source validity or publication permission"),
    ("review.flat_band", "evaluation_measure", "jobs/review_calc.py", "diagnostic_only", "HOLD market-flat hit is not unchanged-portfolio economic value"),
    ("review.model_cutoff", "provenance", "jobs/review_calc.py", "repaired", "A date or model self-report cannot prove historical model identity or evaluation eligibility"),
    ("notification.hold_silence", "delivery_policy", "services/decision_quality.py", "repaired", "Quality and expired/triggered plan alerts are independent of trade direction"),
]


def inventory() -> dict:
    from openinvest.core.config import load_config, get_locked
    root = Path(__file__).resolve().parents[1]
    config = asdict(load_config(_force_reload=True))
    groups = {"regime": "market_judgment", "market_rules": "market_judgment_legacy_defaults_shadow_calibration", "sentiment": "market_and_protection",
              "valuation": "market_judgment", "path": "statistical_calibration",
              "macro_buckets": "market_feature", "dreaming": "research_diagnostic",
              "oracle_accuracy": "frozen_evaluation_measure", "reward": "frozen_evaluation_measure",
              "verdict": "decision_policy_requires_per_rule_review", "portfolio": "user_constraint",
              "dca": "user_funding_instruction", "autopilot": "execution_cost_and_permission",
              "ibkr": "broker_permission_and_safety", "staleness": "input_integrity",
              "event": "collection_and_monitoring", "scheduler": "operational_monitoring",
              "fund": "settlement_contract", "language": "presentation",
              "fleet": "research_only", "sonnet_arm": "research_only"}
    from openinvest.core.config import _loader
    import yaml
    yaml_path = _loader._yaml_path_used or _loader._DEFAULTS_YAML
    yaml_values = yaml.safe_load(yaml_path.read_text()) if yaml_path.exists() else {}
    layers = [("default", {}), ("yaml", yaml_values or {}),
              ("process_override", _loader._overrides_used or {}),
              ("environment", _loader._read_env_overrides()),
              ("persisted_override", _loader._read_persisted_overrides())]
    def origin(section, name):
        return [label for label, values in layers if label == "default" or name in values.get(section, {})]
    def public_value(name, value):
        return "[redacted]" if any(word in name.lower() for word in ("url", "key", "token", "password", "secret")) and value else value
    parameters = [{"id": f"{section}.{name}", "value": public_value(name, value), "source_layers": origin(section, name),
                   "authority": groups.get(section, "unclassified"),
                   "automatic_change_authorized": False}
                  for section, fields in config.items() for name, value in fields.items()]
    numeric_sites = []
    for family, kind, relative, status, rationale in FAMILIES:
        p = root / relative
        for line, text in enumerate(p.read_text().splitlines(), 1):
            if re.search(r"\d", text) and re.search(r"[<>≥≤]|HOLD|SIGNAL|threshold|score|floor|cap", text, re.I):
                numeric_sites.append({"family": family, "path": relative, "line": line, "text": text.strip()})
    # Exhaustive inspection candidates, including rules outside the known
    # families. A numeric literal alone is not evidence that removal is safe.
    static_candidates = []
    for path in sorted(root.rglob("*.py")):
        if "config" in path.parts:
            continue
        source = path.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and any(isinstance(n, ast.Constant)
                    and type(n.value) in (int, float) for n in ast.walk(node)):
                static_candidates.append({"path": str(path.relative_to(root)), "line": node.lineno,
                                          "expression": ast.get_source_segment(source, node),
                                          "classification": "inspection_required_not_automatic_removal"})
    prompt_numeric_sites = []
    for path in sorted((root / "capabilities").rglob("*.md")):
        for line, content in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\d", content) and re.search(r"[<>≥≤%]|HOLD|阈值|threshold", content, re.I):
                prompt_numeric_sites.append({"path": str(path.relative_to(root)), "line": line, "text": content.strip()})
    return {"schema": 1, "parameters": parameters,
            "locked": [asdict(c) for c in get_locked()],
            "families": [dict(zip(("id", "authority", "source", "status", "rationale"), row)) for row in FAMILIES],
            "numeric_sites": numeric_sites, "static_comparisons": static_candidates, "prompt_numeric_sites": prompt_numeric_sites,
            "policy": "Candidate analysis must remain visible; changes to market rules require qualified independent evaluation. User constraints, input integrity and evaluation definitions are not self-relaxed."}


SHADOW_INSTRUCTION = """
=== 候选分析与旧规则分开 ===
上面的数值门槛是当前版本的旧判定口径，不是普适市场定律。
在保留原 SIGNAL / ADJUSTED_SIGNAL 输出的同时，额外输出：
SHADOW_SIGNAL: <仅根据本次市场证据独立判断的候选方向，不受上述分位、RSI、强度档位或现金余额门槛遮蔽>
SHADOW_REASON: <支持证据、反证、原规则与候选不同的原因；证据不足明确写未知>
候选仅供隔离比较，不授权交易，也不自动替换生产规则。执行风险、账户现金与市场方向分别说明。
"""
