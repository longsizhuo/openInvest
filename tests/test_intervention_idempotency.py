"""intervention 账本幂等(ADR-016):同 (date,asset,rule) 当天重跑只入账一次。
跑:uv run pytest tests/test_intervention_idempotency.py -q"""
import json

from core.runner.intervention import _log_intervention


def _rows(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_same_intervention_twice_logs_once(tmp_path):
    p = tmp_path / "interventions.jsonl"
    rec = {"date": "2026-06-29", "asset": "GC=F", "rule": "defense_downgrade", "delta_exposure_cny": 500.0}
    _log_intervention(rec, path=p)
    _log_intervention(rec, path=p)  # 同日手动重跑
    assert len(_rows(p)) == 1, "同 (date,asset,rule) 重跑应幂等只 1 行"


def test_different_rule_same_day_both_logged(tmp_path):
    p = tmp_path / "interventions.jsonl"
    _log_intervention({"date": "2026-06-29", "asset": "GC=F", "rule": "defense_downgrade"}, path=p)
    _log_intervention({"date": "2026-06-29", "asset": "GC=F", "rule": "sanity5_overbought"}, path=p)
    assert len(_rows(p)) == 2, "不同 rule 是不同干预，都该记"


def test_same_rule_different_day_both_logged(tmp_path):
    p = tmp_path / "interventions.jsonl"
    _log_intervention({"date": "2026-06-29", "asset": "GC=F", "rule": "defense_downgrade"}, path=p)
    _log_intervention({"date": "2026-06-30", "asset": "GC=F", "rule": "defense_downgrade"}, path=p)
    assert len(_rows(p)) == 2, "跨天同规则是两次独立干预，都该记"


if __name__ == "__main__":
    import tempfile, pathlib
    for t in (test_same_intervention_twice_logs_once, test_different_rule_same_day_both_logged, test_same_rule_different_day_both_logged):
        with tempfile.TemporaryDirectory() as d:
            t(pathlib.Path(d))
    print("intervention idempotency self-checks passed")
