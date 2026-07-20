"""install_skills 契约：git clone 形态从 plugin/skills 拷贝、替换式幂等、版本可解析。"""
from __future__ import annotations

import argparse
import json
import os

from openinvest.skill_cmds.lifecycle_cmds import cmd_install_skills


def _run(dest, capfd):
    cmd_install_skills(argparse.Namespace(dest=str(dest)))
    return json.loads(capfd.readouterr().out)


def test_install_skills_copies_and_idempotent(tmp_path, capfd):
    dest = tmp_path / "skills"
    out = _run(dest, capfd)
    assert out["status"] == "ok"
    names = {s["name"] for s in out["installed"]}
    assert {"invest", "invest-setup", "invest-backup"} <= names
    assert (dest / "invest" / "SKILL.md").is_file()
    run_sh = dest / "invest" / "scripts" / "run.sh"
    assert run_sh.is_file()
    assert os.access(run_sh, os.X_OK), "copytree 必须保留 run.sh 可执行位"
    # version 来自 SKILL.md 的 release-please 锚行，必须解析得出
    assert all(s["version"] for s in out["installed"])

    # 再跑一遍 = 替换式幂等
    out2 = _run(dest, capfd)
    assert out2["status"] == "ok"
    assert {s["name"] for s in out2["installed"]} == names


def test_install_skills_replaces_plain_file(tmp_path, capfd):
    """目标位置被普通文件占位时也走替换语义，输出 JSON 而非裸 traceback（CR 建议1）。"""
    dest = tmp_path / "skills"
    dest.mkdir()
    (dest / "invest").write_text("stale placeholder", encoding="utf-8")
    out = _run(dest, capfd)
    assert out["status"] == "ok"
    assert (dest / "invest" / "SKILL.md").is_file()


def test_install_skills_replaces_symlink(tmp_path, capfd):
    """dev 用 install.sh 装过 symlink 的目录必须被换成实拷贝，而不是写穿链接。"""
    dest = tmp_path / "skills"
    dest.mkdir()
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (dest / "invest").symlink_to(decoy)

    out = _run(dest, capfd)
    assert out["status"] == "ok"
    assert not (dest / "invest").is_symlink()
    assert (dest / "invest" / "SKILL.md").is_file()
    assert not (decoy / "SKILL.md").exists(), "不能写进 symlink 指向的旧目录"
