"""init 防撞：新用户的数据目录是 git checkout（agent 把仓库 clone 进默认数据目录 ~/openInvest）→ 拒绝写账本。"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openinvest import paths
from openinvest.skill_cmds import lifecycle_cmds


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "openInvest"
    (root / "skills").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setattr(paths, "DEFAULT_HOME", root)
    monkeypatch.delenv("INVEST_HOME", raising=False)
    monkeypatch.delenv("OPENINVEST_DEV_MODE", raising=False)
    return root


def test_blocks_fresh_checkout_without_invest_home(checkout):
    err = lifecycle_cmds._checkout_collision(checkout)
    assert err["status"] == "error" and "INVEST_HOME" in err["hint"]


def test_invest_home_equal_to_default_is_not_explicit(checkout, monkeypatch):
    # run.sh / 安装文档都会自动填 INVEST_HOME=~/openInvest：它不能当成用户的显式选择
    monkeypatch.setenv("INVEST_HOME", str(checkout))
    assert lifecycle_cmds._checkout_collision(checkout) is not None


@pytest.mark.parametrize("setup", ["explicit_home", "user_md", "dev_mode", "not_checkout"])
def test_any_escape_condition_allows(checkout, monkeypatch, tmp_path, setup):
    if setup == "explicit_home":
        monkeypatch.setenv("INVEST_HOME", str(tmp_path / "elsewhere"))
    elif setup == "user_md":
        (checkout / "memory").mkdir()
        (checkout / "memory" / "user.md").write_text("---\n---\n")
    elif setup == "dev_mode":
        monkeypatch.setenv("OPENINVEST_DEV_MODE", "1")
    else:
        (checkout / "pyproject.toml").unlink()
    assert lifecycle_cmds._checkout_collision(checkout) is None


def test_cmd_init_refuses_before_writing(checkout):
    out = io.StringIO()
    with patch.object(lifecycle_cmds, "ROOT", checkout), patch("sys.__stdout__", out), \
            patch("sys.stdin", io.StringIO(json.dumps({"profile": {"name": "x"}}))), \
            pytest.raises(SystemExit) as exc:
        lifecycle_cmds.cmd_init(SimpleNamespace(from_stdin=True, force=False))
    assert exc.value.code == 1
    assert json.loads(out.getvalue())["status"] == "error"
    assert not (checkout / "user_profile.json").exists() and not (checkout / ".env").exists()
