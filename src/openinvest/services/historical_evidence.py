"""Use historical forecast generators only under version-bound evaluation approval."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib
import json
from pathlib import Path

from openinvest.core.evidence_eligibility import Eligibility,digest

ARTIFACT="model:historical_path_generator"


def model_specification():
    from openinvest.core.config import load_config
    modules=("openinvest.core.regime_probability","openinvest.calc.regime_probability",
             "openinvest.calc.regime","openinvest.utils.market_metrics","openinvest.calc.market_metrics")
    code={name:hashlib.sha256(Path(importlib.import_module(name).__file__).read_bytes()).hexdigest() for name in modules}
    config=load_config()
    return {"schema":1,"kind":"historical_path_generator","code":code,
            "parameters":{"path":asdict(config.path),"regime":asdict(config.regime)},
            "purpose":"current_forecast_prior_not_historical_availability_or_first_touch"}


def qualification(data_root):
    """Administrative evidence, never an LLM-supplied approval flag."""
    root=Path(data_root);spec=model_specification();gate=Eligibility(root)
    denied=gate.excluded_ids()  # Broken revocation state must be visible.
    result={"artifact_id":ARTIFACT,"model_sha256":digest(spec),"status":"unavailable",
            "reason":"no_current_evaluated_generator","forecast_use_allowed":False}
    path=root/"integrity/eligibility.json"
    if not path.exists() or ARTIFACT in denied:return result
    record=json.loads(path.read_text()).get("artifacts",{}).get(ARTIFACT,{})
    parents=record.get("parents",[])
    required=("dataset:","evaluation:","protocol:")
    if not all(any(parent.startswith(kind) for parent in parents) for kind in required):return result
    if not gate.allows(ARTIFACT,spec,"advice"):return result
    return {**result,"status":"qualified","forecast_use_allowed":True,
            "reason":"version_bound_independent_evaluation","approval_sha256":digest(record),
            "parent_ids":parents}


def project(regime_text,path_text,*,data_root):
    from openinvest.core.committee.evidence import historical_reference_context
    proof=qualification(data_root)
    return (historical_reference_context(regime_text,qualification=proof,kind="regime"),
            historical_reference_context(path_text,qualification=proof),proof)
