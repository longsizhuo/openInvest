"""回归（#191）：init --from-stdin 录入的现金必须对 PortfolioManager 可见；错 shape 必须报错。

之前 migrate_profile 写 v1 扁平 cash_cny，PortfolioManager 只读 v2 cash dict → 新装用户 status 现金=0；
扁平 payload（没包 profile）被静默吃掉还回 status ok。
子进程 + INVEST_HOME 走真实链路（不 patch 模块常量，不受测试顺序污染）。
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager


def _run_init(home, payload, *flags):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("INVEST_", "LLM_", "DEEPSEEK_"))}
    env["INVEST_HOME"] = str(home)
    env["NO_PROXY"] = "127.0.0.1"
    return subprocess.run(
        [sys.executable, "-c", "from openinvest.cli import main; main()", "init", "--from-stdin", *flags],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )


def _json(out):
    return json.loads(out.stdout[out.stdout.index("{"):])


@pytest.mark.parametrize("extra, expected_cny", [
    ({}, 20000.0),
    # 无 key 的 holdings_description → 回退 current_assets 现金
    ({"holdings_description": "只有 2 万人民币现金"}, 20000.0),
    # cash_cny + holdings_v2 同给：v2 覆盖不能被 _write_v2_portfolio 的防覆盖 guard 拦掉
    ({"holdings_v2": {"cash": {"CNY": 5000}, "holdings": []}}, 5000.0),
])
def test_init_cash_visible_to_portfolio_manager(tmp_path, extra, expected_cny):
    payload = {"profile": {"name": "T", "risk_tolerance": "Balanced",
                           "current_assets": {"cash_cny": 20000}, **extra}, "env": {}}
    out = _run_init(tmp_path, payload)
    assert out.returncode == 0, out.stderr
    result = _json(out)
    assert result["migrate_returncode"] == 0, result["migrate_stderr"]
    assert "v2 write failed" not in result["holdings_parse_note"]

    pm = PortfolioManager(MemoryStore(tmp_path / "memory"))
    assert pm.portfolio.get("schema_version") == 2
    assert pm.cash_amount("CNY") == expected_cny


def test_init_rejects_flat_payload(tmp_path):
    # 旧 invest-setup SKILL.md 教的扁平 shape：以前 status ok + 全部丢失
    out = _run_init(tmp_path, {"display_name": "T", "holdings_description": "2 万现金"})
    assert out.returncode == 1
    result = _json(out)
    assert result["status"] == "error"
    assert "profile" in result["expected_shape"]
    assert not (tmp_path / "user_profile.json").exists()


_PARSED = {"cash": {"CNY": 50000}, "holdings": [
    {"symbol": "510300.SS", "kind": "etf", "units": 3000, "avg_cost": 4.2, "cost_currency": "CNY"}]}


@pytest.fixture
def fake_llm():
    """本地 OpenAI 兼容端点，固定返回 _PARSED。"""
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                               "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                   "role": "assistant", "content": json.dumps(_PARSED)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


@pytest.mark.parametrize("traded", [False, True])
def test_force_reinit_with_key_records_holdings_unless_traded(tmp_path, fake_llm, traded):
    # no-key init 写的是纯现金兜底；配 key 后 init --force 必须能写入解析出的持仓（init 自己推荐的恢复路径）
    profile = {"name": "T", "holdings_description": "510300 3000股 4.2元，现金5万",
               "current_assets": {"cash_cny": 50000}}
    assert _run_init(tmp_path, {"profile": profile, "env": {}}).returncode == 0
    store = MemoryStore(tmp_path / "memory")
    if traded:  # 有流水（buy/sell/deposit 都会记）= 真实数据，--force 也不能覆盖
        store.append_history({"action": "deposit", "currency": "CNY", "amount": 1})

    before = (tmp_path / "memory" / "portfolio.md").read_bytes()
    out = _run_init(tmp_path, {"profile": profile, "env": {"LLM_API_KEY": "sk-fake", "LLM_BASE_URL": fake_llm}},
                    "--force")
    assert out.returncode == 0, out.stderr
    result = _json(out)
    pm = PortfolioManager(MemoryStore(tmp_path / "memory"))
    assert pm.cash_amount("CNY") == 50000.0
    # 覆盖（fresh）和拒绝两条路都先留原文件备份
    assert before in [b.read_bytes() for b in (tmp_path / "memory").glob("portfolio.md.bak.*")]
    if traded:
        assert list(pm.holdings) == []
        assert "v2 write failed" in result["holdings_parse_note"]
        assert result["user_review_required"] is False
        # 不能叫 agent 把解析出的仓位全补一遍（status 里已有的会重复计数），也不能再教 deposit+buy
        nxt = result["next_step"]
        assert "没有写入" in nxt and "不要再加" in nxt and "--existing-position" in nxt
        assert "deposit" not in nxt
    else:
        assert [(h["symbol"], h["units"]) for h in pm.holdings] == [("510300.SS", 3000.0)]
        assert result["user_review_required"] is True


def test_init_survives_malformed_existing_portfolio(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "portfolio.md").write_text("---\ncash: {CNY: [unclosed\n---\n", encoding="utf-8")
    out = _run_init(tmp_path, {"profile": {"name": "T"}, "env": {}})
    assert out.returncode == 0, out.stderr
    assert _json(out)["status"] == "ok"


_NOKEY = {"profile": {"name": "T", "holdings_description": "510300 3000股 4.2元，现金5万",
                      "current_assets": {"cash_cny": 50000}}, "env": {}}


def test_backfill_existing_position_cli_keeps_cash_and_marks_history(tmp_path):
    # 用系统前就持有的仓位不是现金买入：--existing-position 不扣现金，history 可区分
    assert "--existing-position" in _json(_run_init(tmp_path, _NOKEY))["next_step"]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("INVEST_", "LLM_", "DEEPSEEK_"))}
    out = subprocess.run(
        [sys.executable, "-c", "from openinvest.cli import main; main()", "buy", "--symbol", "510300.SS",
         "--units", "3000", "--price", "4.2", "--kind", "etf", "--existing-position"],
        capture_output=True, text=True, env=dict(env, INVEST_HOME=str(tmp_path)))
    assert out.returncode == 0, out.stdout + out.stderr
    store = MemoryStore(tmp_path / "memory")
    pm = PortfolioManager(store)
    assert pm.cash_amount("CNY") == 50000.0
    assert [(h["symbol"], h["units"]) for h in pm.holdings] == [("510300.SS", 3000.0)]
    last = store.read_history()[-1]
    assert (last["action"], last["source"], last["funding_source"]) == (
        "buy", "skill_cli:existing_position", "external_funding")


def test_backfill_existing_position_mcp(tmp_path, monkeypatch):
    assert _run_init(tmp_path, _NOKEY).returncode == 0
    import openinvest.connectors.mcp_server as m
    store = MemoryStore(tmp_path / "memory")
    monkeypatch.setattr(m, "_pm", lambda: PortfolioManager(store))
    out = m.buy(symbol="510300.SS", units=3000, price=4.2, kind="etf", existing_position=True)
    assert out["funding_source"] == "external_funding", out
    assert PortfolioManager(store).cash_amount("CNY") == 50000.0
    assert store.read_history()[-1]["source"] == "mcp:existing_position"
    m.buy(symbol="510300.SS", units=1, price=4.2)  # 默认仍是现金买入
    assert PortfolioManager(store).cash_amount("CNY") == 50000.0 - 4.2


def test_parse_failed_and_no_cash_next_steps(tmp_path):
    (tmp_path / "dead").mkdir()
    (tmp_path / "nocash").mkdir()
    # 有 key 但 LLM 挂了：不能落到 completed_full（指去 adding-assets 的普通 buy）
    dead = {"profile": _NOKEY["profile"], "env": {"LLM_API_KEY": "sk-fake", "LLM_BASE_URL": "http://127.0.0.1:9",
                                                   "EMAIL_SENDER": "a@example.com", "EMAIL_PASSWORD": "x"}}
    r = _json(_run_init(tmp_path / "dead", dead))
    assert "LLM parse failed" in r["holdings_parse_note"]
    assert "--existing-position" in r["next_step"] and "adding-assets" not in r["next_step"]
    assert r["cash_recorded"] == {"CNY": 50000.0}
    # 没给 current_assets：不能说"只录了现金"
    r = _json(_run_init(tmp_path / "nocash", {"profile": {"name": "B", "holdings_description": "510300 3000股"},
                                              "env": {"DEEPSEEK_API_KEY": ""}}))
    assert r["cash_recorded"] == {} and "只录了现金" not in r["next_step"]


def test_doctor_flags_v1_portfolio_with_working_command(tmp_path):
    # #191 修复前的 init 留下 v1 扁平 portfolio.md：status 现金恒 0，doctor 要给出能直接跑的转换命令
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "user.md").write_text("---\nname: user\ntype: user\ndisplay_name: T\n---\n", encoding="utf-8")
    (mem / "strategy.md").write_text("---\nname: strategy\ntype: strategy\n---\n", encoding="utf-8")
    (mem / "portfolio.md").write_text("---\nname: portfolio\ntype: state\ncash_cny: 50000\naud_cash: 0\n---\n",
                                      encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("INVEST_", "LLM_", "DEEPSEEK_"))}
    out = subprocess.run([sys.executable, "-c", "from openinvest.cli import main; main()", "doctor"],
                         capture_output=True, text=True, env=dict(env, INVEST_HOME=str(tmp_path)))
    check = next(c for c in _json(out)["checks"] if c["name"] == "portfolio_schema")
    assert check["status"] == "needs_migration"
    cmd = check["hint"].split("`")[1]
    assert subprocess.run(["bash", "-c", cmd], capture_output=True, env=env).returncode == 0
    assert PortfolioManager(MemoryStore(mem)).cash_amount("CNY") == 50000.0
