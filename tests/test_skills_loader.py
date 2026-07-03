"""skills_loader 回归测试：SKILL.md 加载 + frontmatter 解析 + 占位符渲染"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from capabilities.loader import (
    load_skill,
    load_skill_metadata,
    list_skills,
    _split_frontmatter,
    _render_placeholders,
)


def test_split_frontmatter_basic():
    raw = "---\nname: foo\ndescription: bar\n---\n\n# Hello\nbody"
    meta, body = _split_frontmatter(raw)
    assert meta == {"name": "foo", "description": "bar"}
    assert body == "# Hello\nbody"


def test_split_frontmatter_no_frontmatter():
    raw = "# Just markdown\nbody"
    meta, body = _split_frontmatter(raw)
    assert meta == {}
    assert body == raw


def test_split_frontmatter_quoted_values():
    raw = "---\nname: 'foo'\ndescription: \"bar baz\"\n---\nbody"
    meta, _ = _split_frontmatter(raw)
    assert meta["name"] == "foo"
    assert meta["description"] == "bar baz"


def test_render_placeholders_basic():
    body = "Hello {{name}}, you bought {{symbol}}"
    out = _render_placeholders(body, {"name": "Alice", "symbol": "AAPL"})
    assert out == "Hello Alice, you bought AAPL"


def test_render_placeholders_missing_var_preserved():
    """未提供的占位符原样保留（不抛错）"""
    body = "Hello {{name}}, {{unknown}}"
    out = _render_placeholders(body, {"name": "Alice"})
    assert out == "Hello Alice, {{unknown}}"


def test_list_skills_returns_all_roles():
    """确认 5 个角色 SKILL.md 都在"""
    skills = list_skills()
    expected = {"cio", "macro_strategist", "quant", "risk_officer", "wealth_context_officer"}
    assert expected.issubset(set(skills)), f"missing: {expected - set(skills)}"


@pytest.mark.parametrize("role", ["cio", "macro_strategist", "quant", "risk_officer", "wealth_context_officer"])
def test_load_skill_all_roles_have_metadata(role):
    """每个 role 的 SKILL.md frontmatter 必须有 name + description + role"""
    meta = load_skill_metadata(role)
    assert "name" in meta
    assert "description" in meta
    assert "role" in meta


def test_load_skill_renders_asset_placeholders():
    """Quant prompt 应正确渲染 {{asset_name}} / {{asset_symbol}}"""
    text = load_skill("quant", round_label="opening",
                     asset_name="Test ETF", asset_symbol="TEST.AX")
    assert "Test ETF" in text
    assert "TEST.AX" in text
    assert "{{asset_name}}" not in text
    assert "{{asset_symbol}}" not in text


def test_load_skill_rebuttal_round():
    """SKILL_rebuttal.md 应被加载，不回退到 SKILL.md"""
    text = load_skill("quant", round_label="rebuttal",
                     asset_name="X", asset_symbol="X")
    # rebuttal prompt 含"Round 1"字样，opening 不含
    assert "Round 1" in text or "round 1" in text.lower()


def test_load_skill_fallback_to_opening():
    """不存在的 round_label 应回退到 SKILL.md (opening)"""
    text = load_skill("cio", round_label="nonexistent_round",
                     asset_name="X", asset_symbol="X")
    # 应返回 CIO 主 SKILL.md（含 'CIO' 字样）
    assert "CIO" in text or "首席投资官" in text


def test_load_skill_unknown_role_raises():
    with pytest.raises(FileNotFoundError):
        load_skill("nonexistent_role_xyz", asset_name="X", asset_symbol="X")
