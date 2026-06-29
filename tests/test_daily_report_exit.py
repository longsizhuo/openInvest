"""daily_report 退出码契约：邮件发出=0、其余=1(自托管 Actions 红勾信号)。
跑：uv run pytest tests/test_daily_report_exit.py -q"""
from jobs.daily_report import _report_exit_code


def test_email_sent_is_green():
    assert _report_exit_code({"status": "success", "email": {"sent": True, "receiver": "x@y"}}) == 0


def test_email_failed_is_red():
    assert _report_exit_code({"status": "success", "email": {"sent": False, "error": "SMTPAuth"}}) == 1


def test_no_receiver_is_red():
    assert _report_exit_code({"status": "success", "email": {"sent": False, "skipped": True}}) == 1


def test_no_target_assets_early_return_is_red():
    # 早返回无 email 键 → 红(让首跑漏配 target_assets 的用户立刻看到失败)
    assert _report_exit_code({"status": "skipped", "reason": "no_target_assets"}) == 1


if __name__ == "__main__":
    test_email_sent_is_green()
    test_email_failed_is_red()
    test_no_receiver_is_red()
    test_no_target_assets_early_return_is_red()
    print("daily_report exit-code contract passed")
