"""service_cmds —— `service install|uninstall|status`：把调度器装成开机自启的用户级服务。

不装也行：openinvest-mcp（stdio）在没有调度器时会自己拉起一个内嵌子进程，跟着宿主走
（connectors/mcp_server._start_embedded_scheduler）。装了服务 = 不依赖宿主常驻：宿主关了照跑、
开机自启；服务起来时会请走内嵌实例（scheduler/runner._acquire_lock）。

- Linux：systemd --user 单元 + loginctl enable-linger（用户没登录也跑）
- macOS：~/Library/LaunchAgents plist + launchctl bootstrap（ponytail: 未在 mac 真机实测）
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from openinvest.paths import INVEST_ROOT
from openinvest.skill_cmds._helpers import _print_json

__all__ = ["cmd_service"]

UNIT = "openinvest-scheduler.service"
LABEL = "com.openinvest.scheduler"
RUNNER = "openinvest.scheduler.runner"
_NO_SERVICE_HINT = ("不装服务也行：openinvest-mcp 在宿主（Hermes / Claude Code 等）运行时会自动拉起内嵌调度器，"
                    "宿主退出时跟着停。")


def _exec_args() -> List[str]:
    """服务里怎么起调度器。uvx 的临时环境（uv 缓存 archive-v*）路径会随缓存清理失效，
    不能写死 sys.executable——改由 uvx 重建：用 run.sh 导出的 OPENINVEST_SPEC（插件钉的版本 / 用户覆盖的
    spec，和 MCP 跑的是同一个），没有就钉当前版本；其余（venv / uv tool / 仓库 checkout）路径稳定，直接用。"""
    from openinvest import __version__

    uvx = shutil.which("uvx")
    if "archive-v" in sys.prefix and uvx:
        spec = os.getenv("OPENINVEST_SPEC", "").strip() or f"openinvest=={__version__}"
        return [uvx, "--from", spec, "python", "-m", RUNNER]
    return [sys.executable, "-m", RUNNER]


def _systemd_unit(exec_args: List[str]) -> str:
    return "\n".join([
        "[Unit]",
        "Description=openInvest scheduler (news / price sentinel / NAV snapshot / backup)",
        "After=network-online.target",
        "",
        "[Service]",
        f'Environment="INVEST_HOME={INVEST_ROOT}"',
        f"ExecStart={shlex.join(exec_args)}",
        "Restart=on-failure",
        "RestartSec=30",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def _launchd_plist(exec_args: List[str]) -> bytes:
    log = str(INVEST_ROOT / "logs" / "scheduler.launchd.log")
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": exec_args,
        "EnvironmentVariables": {"INVEST_HOME": str(INVEST_ROOT)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def _unit_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / UNIT


def _run(cmd: List[str]) -> Dict[str, Any]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {"cmd": shlex.join(cmd), "rc": r.returncode,
                "out": (r.stdout.strip() or r.stderr.strip())[-300:]}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"cmd": shlex.join(cmd), "rc": -1, "out": f"{type(e).__name__}: {e}"}


def _install() -> Dict[str, Any]:
    path = _unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    args = _exec_args()
    if sys.platform == "darwin":
        path.write_bytes(_launchd_plist(args))
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{LABEL}"])  # 重装：先卸旧的，没装过会报错，忽略
        steps = [_run(["launchctl", "bootstrap", domain, str(path)])]
    else:
        path.write_text(_systemd_unit(args), encoding="utf-8")
        steps = [_run(["systemctl", "--user", "daemon-reload"]),
                 _run(["systemctl", "--user", "enable", "--now", UNIT])]
    out: Dict[str, Any] = {"unit_path": str(path), "exec": shlex.join(args), "steps": steps}
    if any(s["rc"] != 0 for s in steps):
        return {"status": "error", **out,
                "error": "服务管理器拒绝了（容器 / WSL 里常见：没有可用的 systemd 用户实例）",
                "hint": _NO_SERVICE_HINT + "卸掉单元文件：service uninstall"}
    if sys.platform != "darwin":
        user = os.getenv("USER") or ""
        linger = _run(["loginctl", "show-user", user, "-p", "Linger", "--value"])
        if linger["out"] != "yes" and _run(["loginctl", "enable-linger", user])["rc"] != 0:
            out["hint"] = f"用户退出登录后服务会停；要一直跑请执行一次：sudo loginctl enable-linger {user}"
    return {"status": "ok", **out}


def _uninstall() -> Dict[str, Any]:
    path = _unit_path()
    if sys.platform == "darwin":
        steps = [_run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])]
        path.unlink(missing_ok=True)
    else:
        steps = [_run(["systemctl", "--user", "disable", "--now", UNIT])]
        path.unlink(missing_ok=True)
        steps.append(_run(["systemctl", "--user", "daemon-reload"]))
    return {"status": "ok", "removed": str(path), "steps": steps, "hint": _NO_SERVICE_HINT}


def _status() -> Dict[str, Any]:
    from openinvest import __version__
    from openinvest.scheduler.lock import holder

    path = _unit_path()
    h = holder()
    out: Dict[str, Any] = {"status": "ok", "installed": path.exists(), "unit_path": str(path),
                           "running": h, "version": __version__}
    if path.exists() and sys.platform != "darwin":
        out["active"] = _run(["systemctl", "--user", "is-active", UNIT])["out"]
    if h and h.get("mode") == "daemon" and h.get("version") not in (None, __version__):
        out["hint"] = f"服务跑的是 {h['version']}，当前是 {__version__}：重跑 service install 更新"
    return out


def cmd_service(args: argparse.Namespace) -> None:
    if sys.platform not in ("linux", "darwin"):
        _print_json({"status": "error", "error": f"不支持 {sys.platform}", "hint": _NO_SERVICE_HINT})
        sys.exit(1)
    result = {"install": _install, "uninstall": _uninstall, "status": _status}[args.service_cmd]()
    _print_json(result)
    if result["status"] != "ok":
        sys.exit(1)
