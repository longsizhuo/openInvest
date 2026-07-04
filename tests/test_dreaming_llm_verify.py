"""P1-3 Dreaming Deep Sleep LLM 验伪测试

验证：
- 默认 env off → 行为完全不变（旧 candidates 全过）
- env on + LLM 返 KEEP/REJECT → 应用 mask
- LLM API 失败 → fallback 全 KEEP（保守策略）
- 全部被 REJECT → 不写 insights，dream_event 留 audit
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openinvest.core.config import reset_config, set_config_override
import openinvest.jobs.dreaming as dreaming_mod


@pytest.fixture(autouse=True)
def _reset_config():
    """每个 test 重置 config 隔离状态。"""
    reset_config()
    yield
    reset_config()


def _make_candidate(asset="GC=F", action="bought", verdict=None,
                    regime=("vix_low",), window=7, count=10,
                    hit_rate=0.95, avg_return=4.0):
    """生产一个会通过 score≥0.8 阈值的 candidate

    score = hit*0.5 + min(|ret|/5, 1)*0.3 + min(count/10, 1)*0.2
    默认 0.95*0.5 + 0.8*0.3 + 1.0*0.2 = 0.915 ≥ 0.8 ✓
    """
    c = {
        "asset": asset,
        "regime": list(regime),
        "window_days": window,
        "count": count,
        "hit_rate": hit_rate,
        "avg_return_pct": avg_return,
    }
    if verdict:
        c["verdict"] = verdict
    else:
        c["action"] = action
    return c


@pytest.fixture
def store(tmp_path):
    """tmp memory store"""
    from openinvest.core.memory_store import MemoryStore
    return MemoryStore(tmp_path / "memory")


# ---------- env off：默认行为不变 ----------

def test_default_env_off_skips_llm_verify(store, monkeypatch):
    """env 不设 → 完全不调 LLM"""
    monkeypatch.delenv("INVEST_DREAMING_LLM_VERIFY", raising=False)
    cand = [_make_candidate()]

    # 用 patch 监控 _llm_verify_candidates 是否被调
    with patch.object(dreaming_mod, "_llm_verify_candidates") as mock_llm:
        accepted = dreaming_mod.deep_sleep(store, cand)
        mock_llm.assert_not_called()

    assert len(accepted) == 1
    # insight 应该写盘了
    insights_dir = store.root / "insights"
    assert insights_dir.exists()
    assert len(list(insights_dir.glob("*.md"))) == 1


def test_env_off_explicit_zero_also_skips(store, monkeypatch):
    monkeypatch.setenv("INVEST_DREAMING_LLM_VERIFY", "0")
    cand = [_make_candidate()]
    with patch.object(dreaming_mod, "_llm_verify_candidates") as mock_llm:
        dreaming_mod.deep_sleep(store, cand)
        mock_llm.assert_not_called()


# ---------- env on：LLM 介入 ----------

def test_env_on_calls_llm_and_applies_keep_mask(store, monkeypatch):
    """env on + LLM 全 KEEP → 不影响结果"""
    monkeypatch.setenv("INVEST_DREAMING_LLM_VERIFY", "1")
    cand = [_make_candidate(asset="GC=F"), _make_candidate(asset="NDQ.AX")]

    with patch.object(
        dreaming_mod, "_llm_verify_candidates",
        return_value=([True, True], [{"id": 0, "decision": "KEEP"}, {"id": 1, "decision": "KEEP"}]),
    ) as mock_llm:
        accepted = dreaming_mod.deep_sleep(store, cand)
        mock_llm.assert_called_once()

    assert len(accepted) == 2


def test_env_on_rejects_via_mask(store, monkeypatch):
    """LLM 返第二个 REJECT → insights 只写第一个"""
    monkeypatch.setenv("INVEST_DREAMING_LLM_VERIFY", "1")
    cand = [_make_candidate(asset="GC=F"), _make_candidate(asset="NDQ.AX")]

    with patch.object(
        dreaming_mod, "_llm_verify_candidates",
        return_value=(
            [True, False],
            [
                {"id": 0, "decision": "KEEP", "reason": "样本充分"},
                {"id": 1, "decision": "REJECT", "reason": "regime 切薄"},
            ],
        ),
    ):
        accepted = dreaming_mod.deep_sleep(store, cand)

    assert len(accepted) == 1
    assert accepted[0]["asset"] == "GC=F"
    insights_dir = store.root / "insights"
    files = list(insights_dir.glob("*.md"))
    assert len(files) == 1
    assert "gc_f" in files[0].name.lower()


def test_env_on_all_rejected_writes_audit_event(store, monkeypatch):
    """LLM 拒了所有候选 → 不写 insights 但有 audit event"""
    monkeypatch.setenv("INVEST_DREAMING_LLM_VERIFY", "1")
    cand = [_make_candidate()]

    with patch.object(
        dreaming_mod, "_llm_verify_candidates",
        return_value=([False], [{"id": 0, "decision": "REJECT", "reason": "可疑"}]),
    ):
        accepted = dreaming_mod.deep_sleep(store, cand)

    assert accepted == []
    # 不应该写 insights
    insights_dir = store.root / "insights"
    if insights_dir.exists():
        assert len(list(insights_dir.glob("*.md"))) == 0
    # 但应该写 audit event
    events_path = store.root / ".dreams" / "events.jsonl"
    assert events_path.exists()
    content = events_path.read_text()
    assert "all_rejected_by_llm_verify" in content


# ---------- LLM API 失败 → fallback 保守 ----------

def test_llm_api_failure_falls_back_to_keep_all(store, monkeypatch):
    """env on 但 LLM 抛异常 → 全 KEEP（保守）"""
    monkeypatch.setenv("INVEST_DREAMING_LLM_VERIFY", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key-for-test")
    cand = [_make_candidate()]

    # mock OpenAI client 抛网络错
    fake_openai = MagicMock()
    fake_openai.chat.completions.create.side_effect = ConnectionError("network down")

    with patch("openai.OpenAI", return_value=fake_openai):
        accepted = dreaming_mod.deep_sleep(store, cand)

    # API fail → fallback 全 KEEP → 候选仍然通过
    assert len(accepted) == 1


def test_llm_no_api_key_skips_verify(store, monkeypatch):
    """env on 但没 DEEPSEEK_API_KEY → 直接全 KEEP，不真调 OpenAI"""
    monkeypatch.setenv("INVEST_DREAMING_LLM_VERIFY", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cand = [_make_candidate()]

    accepted = dreaming_mod.deep_sleep(store, cand)
    assert len(accepted) == 1


# ---------- 边界 ----------

def test_empty_candidates_no_llm_call(store, monkeypatch):
    """没候选 → 早返回，根本不到 LLM"""
    monkeypatch.setenv("INVEST_DREAMING_LLM_VERIFY", "1")

    with patch.object(dreaming_mod, "_llm_verify_candidates") as mock_llm:
        accepted = dreaming_mod.deep_sleep(store, [])
        mock_llm.assert_not_called()

    assert accepted == []


# ---------- prompt 内容验证 ----------

def test_llm_verify_prompt_uses_config_values(store, monkeypatch):
    """验证 prompt 里 MIN_RECALL/MIN_SCORE 从 config/locked 读，dict key 是 verdict 非 action"""
    monkeypatch.setenv("INVEST_DREAMING_LLM_VERIFY", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key-for-test")

    # 捕获 OpenAI 调用的 messages
    captured_messages = []
    fake_openai = MagicMock()
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = '{"verdicts": [{"id": 0, "decision": "KEEP", "reason": "ok"}]}'
    fake_openai.chat.completions.create.return_value = fake_response

    def capture_create(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return fake_response

    fake_openai.chat.completions.create.side_effect = capture_create

    cand = [_make_candidate(verdict="ACCUMULATE")]

    with patch("openai.OpenAI", return_value=fake_openai):
        dreaming_mod.deep_sleep(store, cand)

    # 找 user payload
    user_msg = next(m for m in captured_messages if m["role"] == "user")
    payload = user_msg["content"]

    # 默认值: MIN_RECALL=3, MIN_SCORE=0.8
    assert "count≥3" in payload, f"MIN_RECALL=3 不在 payload: {payload[:200]}"
    assert "score≥0.8" in payload, f"MIN_SCORE=0.8 不在 payload: {payload[:200]}"
    # _label() 把 verdict 值映射到 "action" key，值本身保留（2026-05-27 KeyError 回归锁）
    assert '"action": "ACCUMULATE"' in payload, f"verdict 值 ACCUMULATE 未正确映射: {payload[:300]}"


def test_llm_verify_prompt_override_min_recall(store, monkeypatch):
    """set_config_override 改 min_recall 后 prompt 里的 count≥N 跟着变"""
    monkeypatch.setenv("INVEST_DREAMING_LLM_VERIFY", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key-for-test")

    set_config_override({"dreaming": {"min_recall": 7}})

    captured_messages = []
    fake_openai = MagicMock()
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = '{"verdicts": [{"id": 0, "decision": "KEEP", "reason": "ok"}]}'

    def capture_create(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return fake_response

    fake_openai.chat.completions.create.side_effect = capture_create

    cand = [_make_candidate()]

    with patch("openai.OpenAI", return_value=fake_openai):
        dreaming_mod.deep_sleep(store, cand)

    user_msg = next(m for m in captured_messages if m["role"] == "user")
    payload = user_msg["content"]

    assert "count≥7" in payload, f"min_recall=7 override 未生效: {payload[:200]}"
