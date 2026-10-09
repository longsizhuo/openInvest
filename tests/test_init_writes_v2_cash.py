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

    out = _run_init(tmp_path, {"profile": profile, "env": {"LLM_API_KEY": "sk-fake", "LLM_BASE_URL": fake_llm}},
                    "--force")
    assert out.returncode == 0, out.stderr
    result = _json(out)
    pm = PortfolioManager(MemoryStore(tmp_path / "memory"))
    assert pm.cash_amount("CNY") == 50000.0
    if traded:
        assert list(pm.holdings) == []
        assert "v2 write failed" in result["holdings_parse_note"]
        assert result["user_review_required"] is False
        assert "没有写入" in result["next_step"]
    else:
        assert [(h["symbol"], h["units"]) for h in pm.holdings] == [("510300.SS", 3000.0)]
        assert result["user_review_required"] is True


def test_init_survives_malformed_existing_portfolio(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "portfolio.md").write_text("---\ncash: {CNY: [unclosed\n---\n", encoding="utf-8")
    out = _run_init(tmp_path, {"profile": {"name": "T"}, "env": {}})
    assert out.returncode == 0, out.stderr
    assert _json(out)["status"] == "ok"
