"""向后兼容入口 - 单次跑 daily_report

实际逻辑已搬到 jobs/daily_report.py，本文件保留是为了：
- 老的 `python main.py` 命令仍然能跑
- 老的 invest-agent.service 启动方式（如果还在用）不至于挂

新代码请用：
    python -m scheduler.runner             # 后台调度
    python -m scheduler.runner --once daily_report
    python -m jobs.daily_report             # 直接跑一次
"""
from jobs.daily_report import run

if __name__ == "__main__":
    print(run())
