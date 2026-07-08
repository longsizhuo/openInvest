from __future__ import annotations

import argparse
import json


def test_cmd_run_committee_english_next_step(monkeypatch, capfd, tmp_path):
    # 用 capfd 而非 capsys：_print_json 直写 sys.__stdout__（fd 级）绕过 capsys（见 test_prepare_committee.py）。
    from openinvest.core.config import reset_config, set_config_override
    from openinvest.skill_cmds import committee_cmds as cc

    reset_config()
    set_config_override({"language": {"invest_lang": "en"}})

    monkeypatch.setattr(cc, "ROOT", tmp_path)

    class FakePM:
        strategy = {
            "target_assets": [
                {
                    "symbol": "AAPL",
                    "target_pct": 0.7,
                    "max_single_invest_cny": 100.0,
                    "display_name": "Apple Inc.",
                }
            ]
        }

    class FakeReport:
        cio_memo = "VERDICT: HOLD\nCONFIDENCE: 0.5\nDOMINANT_VIEW: macro\nSUGGESTED_ALLOC_CNY: 0"

    monkeypatch.setattr("openinvest.core.portfolio_manager.PortfolioManager", lambda: FakePM())
    monkeypatch.setattr(
        "openinvest.core.committee_runner.run_committee_session",
        lambda **_: {
            "asset_committees": {
                "AAPL": {
                    "verdict": {"verdict": "HOLD", "confidence": 0.5, "alloc_cny": 0, "dominant_view": "macro"},
                    "report": FakeReport(),
                }
            }
        },
    )

    args = argparse.Namespace(symbol="AAPL", force=True, max_rounds=1)
    cc.cmd_run_committee(args)
    out = json.loads(capfd.readouterr().out)

    assert out["status"] == "ok"
    next_step = out["next_step"]
    # 三步流程全覆盖，不只测第一句——之前只断言步骤 1，步骤 2/3、render hint、
    # memory 警告改错了也不会被这个测试抓到。
    assert "The `cio_memo` field is a Markdown string" in next_step
    assert "1) The user opens their broker or banking app" in next_step
    assert "2) Come back and record the trade with the CLI `buy`/`sell` subcommands" in next_step
    assert "3) Use `record_execution` to link the decision to the execution outcome" in next_step
    assert "Do not write to memory/ directly" in next_step
    assert "已生成 verdict" not in next_step
    assert "直接写 memory/" not in next_step

    reset_config()
