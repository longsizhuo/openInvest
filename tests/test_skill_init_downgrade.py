"""tests/test_skill_init_downgrade.py — cmd_init 降级话术测试

核心断言：
- holdings_description 给了但 DEEPSEEK_API_KEY 空 → next_step 必须含
  "platform.deepseek.com"（强制话术路径）
- 降级话术中必须提示"只录了现金"（让 agent 明白要说什么）
- key 存在时走正常 LLM 路径（next_step 不含降级话术）

测试策略：
- monkeypatch _parse_holdings_with_llm 避免真实 LLM 调用
- monkeypatch subprocess.run 避免真实 migrate_profile.py 执行
- 写操作重定向到 tmp_path（user_profile.json / .env）
"""
from __future__ import annotations

import json
import subprocess
import sys
import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------- helper ----------

def _build_args(from_stdin: bool = True, force: bool = True) -> argparse.Namespace:
    """构造 argparse.Namespace 模拟 cmd_init 入参"""
    return argparse.Namespace(from_stdin=from_stdin, force=force)


def _run_cmd_init_with_payload(
    tmp_path: Path,
    payload: Dict[str, Any],
    monkeypatch,
) -> Dict[str, Any]:
    """在隔离环境里跑 cmd_init，捕获 JSON 输出，返回 parsed dict。

    副作用隔离：
    - ROOT 重定向到 tmp_path（避免写真实 user_profile.json / .env）
    - subprocess.run → 返回假 CompletedProcess（migrate_profile.py 不跑）
    - _parse_holdings_with_llm → 不实际调 LLM（按需 monkeypatch）
    - 标准输出 → 捕获
    """
    import scripts.skill as skill_mod

    # 重定向 ROOT 到 tmp_path，让 profile_path / env_path 写到临时目录
    monkeypatch.setattr(skill_mod, "ROOT", tmp_path)
    # 确保 memory/user.md 不存在（避免 memory_initialized 误为 True）
    # migrate_profile.py subprocess 改为假 ok
    fake_result = MagicMock(spec=subprocess.CompletedProcess)
    fake_result.stdout = ""
    fake_result.stderr = ""
    fake_result.returncode = 0

    captured_output: list[str] = []

    # 拦截 _print_json：把输出存到 captured_output，不写真实 stdout
    def _fake_print_json(obj: Any) -> None:
        captured_output.append(json.dumps(obj, ensure_ascii=False))

    with (
        patch("subprocess.run", return_value=fake_result),
        patch.object(skill_mod, "_print_json", side_effect=_fake_print_json),
    ):
        args = _build_args()
        # 用 monkeypatch stdin 模拟 --from-stdin
        monkeypatch.setattr(
            "sys.stdin",
            __import__("io").StringIO(json.dumps(payload)),
        )
        skill_mod.cmd_init(args)

    assert captured_output, "cmd_init 没有调用 _print_json，测试前提失败"
    return json.loads(captured_output[-1])


# ---------- 降级话术断言 ----------

def test_downgrade_next_step_contains_deepseek_url(tmp_path, monkeypatch):
    """holdings_description 给了但 DEEPSEEK_API_KEY 为空 → next_step 含 platform.deepseek.com"""
    payload = {
        "profile": {
            "name": "TestUser",
            "risk_tolerance": "Balanced",
            "monthly_income_cny": 0,
            "monthly_expenses_cny": 0,
            "exchange_buffer_cny": 0,
            "last_run_date": "2026-05-10",
            "holdings_description": "NDQ.AX 10 股，CNY 现金 5000",
            "current_assets": {"cash_cny": 5000, "aud_cash": 0, "ndq_shares": 0},
            "investment_strategy": {
                "target_allocation_stock": 0.7,
                "target_allocation_cash": 0.3,
                "max_single_invest_cny": 10000,
            },
        },
        "env": {
            # 关键：key 为空字符串 → 触发降级路径
            "DEEPSEEK_API_KEY": "",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        },
    }

    result = _run_cmd_init_with_payload(tmp_path, payload, monkeypatch)

    next_step = result.get("next_step", "")
    assert "platform.deepseek.com" in next_step, (
        f"降级话术缺少 platform.deepseek.com。\nnext_step 实际值: {next_step!r}"
    )


def test_downgrade_next_step_mentions_cash_only(tmp_path, monkeypatch):
    """降级话术必须明确告知用户'只录了现金'"""
    payload = {
        "profile": {
            "name": "TestUser",
            "risk_tolerance": "Balanced",
            "monthly_income_cny": 0,
            "monthly_expenses_cny": 0,
            "exchange_buffer_cny": 0,
            "last_run_date": "2026-05-10",
            "holdings_description": "我有 GC=F 黄金 50 克",
            "current_assets": {"cash_cny": 0, "aud_cash": 0},
            "investment_strategy": {
                "target_allocation_stock": 0.7,
                "target_allocation_cash": 0.3,
                "max_single_invest_cny": 5000,
            },
        },
        "env": {"DEEPSEEK_API_KEY": ""},
    }

    result = _run_cmd_init_with_payload(tmp_path, payload, monkeypatch)

    next_step = result.get("next_step", "")
    # 降级话术核心：告诉用户只录了现金
    assert "现金" in next_step, (
        f"降级话术缺少'现金'关键词。\nnext_step: {next_step!r}"
    )


def test_holdings_parse_note_on_downgrade(tmp_path, monkeypatch):
    """降级时 holdings_parse_note 必须含 DEEPSEEK_API_KEY 缺失相关说明"""
    payload = {
        "profile": {
            "name": "TestUser",
            "risk_tolerance": "Balanced",
            "monthly_income_cny": 0,
            "monthly_expenses_cny": 0,
            "exchange_buffer_cny": 0,
            "last_run_date": "2026-05-10",
            "holdings_description": "AAPL 5 股，CNY 现金 1000",
            "current_assets": {"cash_cny": 1000},
            "investment_strategy": {
                "target_allocation_stock": 0.7,
                "target_allocation_cash": 0.3,
                "max_single_invest_cny": 5000,
            },
        },
        "env": {"DEEPSEEK_API_KEY": ""},
    }

    result = _run_cmd_init_with_payload(tmp_path, payload, monkeypatch)

    note = result.get("holdings_parse_note", "")
    assert "DEEPSEEK_API_KEY" in note, (
        f"holdings_parse_note 未体现 key 缺失信息。实际: {note!r}"
    )


def test_no_downgrade_when_key_provided(tmp_path, monkeypatch):
    """提供了有效 key 时，next_step 不走降级路径（不含降级专属措辞）"""
    import scripts.skill as skill_mod

    # mock LLM 解析直接返回空 holdings（省掉真实 API 调用）
    def fake_llm_parse(text, api_key, base_url):
        return {"cash": {"CNY": 5000}, "holdings": []}

    monkeypatch.setattr(skill_mod, "_parse_holdings_with_llm", fake_llm_parse)

    payload = {
        "profile": {
            "name": "TestUser",
            "risk_tolerance": "Balanced",
            "monthly_income_cny": 0,
            "monthly_expenses_cny": 0,
            "exchange_buffer_cny": 0,
            "last_run_date": "2026-05-10",
            "holdings_description": "CNY 现金 5000",
            "current_assets": {"cash_cny": 5000},
            "investment_strategy": {
                "target_allocation_stock": 0.7,
                "target_allocation_cash": 0.3,
                "max_single_invest_cny": 5000,
            },
        },
        "env": {
            "DEEPSEEK_API_KEY": "sk-fake-key-for-test",
            "EMAIL_SENDER": "test@gmail.com",
        },
    }

    result = _run_cmd_init_with_payload(tmp_path, payload, monkeypatch)

    next_step = result.get("next_step", "")
    # 有 key 时不走降级话术——这个特定句子不应出现
    assert "只录了现金，没识别你说的具体股票" not in next_step, (
        f"有 key 时不应触发降级话术。\nnext_step: {next_step!r}"
    )
