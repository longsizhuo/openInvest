"""Calibration baseline uses raw, qualified samples and mature temporal labels."""
import json
import pytest

from openinvest.core.memory_store import MemoryStore
from scripts.research import fit_path_calibration as fit


def sample(source="recompute",kind="raw_conditional"):
    return {"date":"2017-12-10","asset":"SYNTHETIC","regime":"range_bound","source":source,
            "windows":{"30d":{"pred":{"median_pct":0,"effective_n":2},"uncond":{"median_pct":1},
                       "pred_kind":kind,"label_end":"2018-01-09","fwd_real":-1,"below_current":True}}}


def test_live_and_already_calibrated_predictions_never_become_fit_input(tmp_path,monkeypatch):
    store=MemoryStore(tmp_path/"memory")
    monkeypatch.setattr(fit,"MemoryStore",lambda:store)
    path=store.root/".dreams/path_review.jsonl";path.parent.mkdir()
    path.write_text("\n".join(json.dumps(row) for row in [sample("live","calibrated"),sample("recompute","calibrated"),sample()])+"\n")
    assert fit.load_rows()==[]  # No administrative learning qualification.
    rows=fit.load_rows(diagnostic=True)
    assert len(rows)==1
    train,validation,purged=fit.temporal_split(rows)
    assert train==[] and validation==[] and len(purged)==1


def test_conflicting_duplicate_raw_samples_cannot_silently_double_count(tmp_path,monkeypatch):
    store=MemoryStore(tmp_path/"memory");monkeypatch.setattr(fit,"MemoryStore",lambda:store)
    path=store.root/".dreams/path_review.jsonl";path.parent.mkdir()
    other=sample();other["windows"]["30d"]["fwd_real"]=-2
    path.write_text(json.dumps(sample())+"\n"+json.dumps(other)+"\n")
    with pytest.raises(ValueError,match="conflicting duplicate"):
        fit.load_rows(diagnostic=True)


def test_manual_recompute_never_loads_live_samples_into_baseline(tmp_path,monkeypatch):
    from openinvest.jobs import path_review
    monkeypatch.setattr(path_review,"collect_live_snapshots",lambda:(_ for _ in ()).throw(AssertionError("live mixed into recompute")))
    monkeypatch.setattr(path_review,"recompute_snapshots",lambda *args:[])
    monkeypatch.setattr(path_review,"write_outputs",lambda *args,**kwargs:(tmp_path/"baseline.jsonl",tmp_path/"report.md"))
    assert path_review.run(recompute_dates=["2020-01-01"],symbols=["SYNTHETIC"])["reviews"]==0
