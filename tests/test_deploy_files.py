"""D6 运维加固：部署文件不得绕过 openinvest-web 的 token 守卫

openinvest-web（connectors/web_api/__main__.py）规定"非 loopback 绑定 + 无 INVEST_API_TOKEN
= 拒绝启动"。2026-10 评审 D6：docker-compose.yml 与 systemd/invest-web.service 直接起 uvicorn，
守卫形同虚设（compose 在容器内绑 0.0.0.0，全靠 127.0.0.1 端口映射兜底）。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_web_goes_through_openinvest_web_entry():
    svc = _compose()["services"]["invest-web"]
    cmd = " ".join(svc["command"]) if isinstance(svc["command"], list) else svc["command"]
    assert "uvicorn" not in cmd
    assert "exec openinvest-web" in cmd
    assert "0.0.0.0" in " ".join(svc["environment"])  # 容器内绑 0.0.0.0 靠 INVEST_WEB_HOST 显式声明


def test_compose_web_precheck_requires_token():
    """把 compose 里的 sh 脚本（$$ 还原为 $）真跑一遍：无 token 退出 1，有 token 才 exec 入口。"""
    script = _compose()["services"]["invest-web"]["command"][2].replace("$$", "$")
    env = {"PATH": "/usr/bin:/bin"}
    bad = subprocess.run(["/bin/sh", "-c", script], env=env, capture_output=True, text=True)
    assert bad.returncode == 1 and "INVEST_API_TOKEN" in bad.stderr
    # 有 token → 走到 exec openinvest-web（沙箱里没有这个命令 → 127，说明已越过检查）
    ok = subprocess.run(["/bin/sh", "-c", script], env={**env, "INVEST_API_TOKEN": "t"},
                        capture_output=True, text=True)
    assert ok.returncode == 127 and "openinvest-web" in ok.stderr


@pytest.mark.parametrize("unit", sorted((ROOT / "systemd").glob("*.service")), ids=lambda p: p.name)
def test_systemd_units_never_launch_bare_uvicorn(unit):
    exec_lines = re.findall(r"^ExecStart=(.*)$", unit.read_text(encoding="utf-8"), re.M)
    assert exec_lines and not any("uvicorn" in line for line in exec_lines)


def test_systemd_web_unit_uses_entry_and_loopback_default():
    text = (ROOT / "systemd" / "invest-web.service").read_text(encoding="utf-8")
    assert re.search(r"^ExecStart=.*\bopeninvest-web\b", text, re.M)
    assert "Environment=INVEST_WEB_HOST=127.0.0.1" in text


# ---------- 守卫本身（此前无测试） ----------

def _run_web_main(monkeypatch, host, token):
    import uvicorn

    from openinvest.connectors.web_api import __main__ as web_main

    seen = {}
    monkeypatch.setenv("INVEST_WEB_HOST", host)
    monkeypatch.setenv("INVEST_WEB_PORT", "58765")
    if token is None:
        monkeypatch.delenv("INVEST_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("INVEST_API_TOKEN", token)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.update(k))
    web_main.main()
    return seen


def test_web_main_refuses_non_loopback_without_token(monkeypatch):
    with pytest.raises(SystemExit) as e:
        _run_web_main(monkeypatch, "0.0.0.0", None)
    assert "INVEST_API_TOKEN" in str(e.value)
    with pytest.raises(SystemExit):
        _run_web_main(monkeypatch, "0.0.0.0", "   ")  # 全空白也算没设


def test_web_main_allows_non_loopback_with_token_and_loopback_without(monkeypatch):
    assert _run_web_main(monkeypatch, "0.0.0.0", "s3cret")["host"] == "0.0.0.0"
    assert _run_web_main(monkeypatch, "127.0.0.1", None)["host"] == "127.0.0.1"
