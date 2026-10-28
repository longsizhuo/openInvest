"""系统服务单元的名字和位置：service_cmds 装 / 卸，brief 据此判断"装了服务却没在跑"。"""
from __future__ import annotations

import sys
from pathlib import Path

UNIT = "openinvest-scheduler.service"
LABEL = "com.openinvest.scheduler"


def unit_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / UNIT
