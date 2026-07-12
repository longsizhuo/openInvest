"""cmd_daily_report stdout 契约

cli.main() 会把 sys.stdout 重定向到 stderr（挡 utils/* 的 print noise），
真输出必须写 sys.__stdout__。这个测试用 capfd（fd 级捕获）守住：
报告落在真 stdout（fd1，宿主 agent cron 原样投递的通道），不在 stderr。
跑：uv run pytest tests/test_cli_daily_report.py -q
"""
from __future__ import annotations

import sys

import openinvest.cli as cli
import openinvest.jobs.daily_report as daily_report_mod


def _fake_run(send_email: bool = True, include_report: bool = False):
    assert send_email is False, "CLI 路径必须跳过邮件（投递归宿主 agent）"
    assert include_report is True, "CLI 路径必须要求返回 full_report"
    return {"status": "success", "full_report": "REPORT_BODY_SENTINEL"}


def test_report_goes_to_real_stdout(monkeypatch, capfd):
    # 先让 monkeypatch 记录原始 sys.stdout —— cli.main() 会覆盖它，teardown 时还原
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(daily_report_mod, "run", _fake_run)
    monkeypatch.setattr(sys, "argv", ["skill", "daily_report"])

    cli.main()

    out, err = capfd.readouterr()
    assert "REPORT_BODY_SENTINEL" in out, "报告必须写到真 stdout（fd1）"
    assert "REPORT_BODY_SENTINEL" not in err


def test_no_report_falls_back_to_json(monkeypatch, capfd):
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(
        daily_report_mod, "run",
        lambda send_email=True, include_report=False: {
            "status": "aborted", "reason": "stale_data_hard_abort",
        },
    )
    monkeypatch.setattr(sys, "argv", ["skill", "daily_report"])

    cli.main()

    out, _ = capfd.readouterr()
    assert '"aborted"' in out, "无报告时必须输出结构化 JSON 让 cron 投递失败原因"
