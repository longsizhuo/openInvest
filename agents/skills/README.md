# agents/skills/ — Prompt 本体（SKILL.md 模式）

参考 [OpenClaw](https://github.com/openclaw/openclaw) 和
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 的
prompt 组织模式：**每个角色 = 一个独立 SKILL.md 文件**，含 YAML frontmatter +
markdown body。

## 目录结构

```
agents/skills/
├── cio/
│   └── SKILL.md
├── macro_strategist/
│   └── SKILL.md
├── quant/
│   ├── SKILL.md           # round_label="opening"
│   └── SKILL_rebuttal.md  # round_label="rebuttal" (Round 2 cross-challenge)
├── risk_officer/
│   ├── SKILL.md
│   └── SKILL_rebuttal.md
└── wealth_context_officer/
    └── SKILL.md
```

## SKILL.md 格式

```markdown
---
name: <角色 slug>
description: <一句话功能描述>
role: macro|quant|risk|cio|wealth_context
---

<prompt 正文，markdown 格式>

可用占位符 {{asset_name}} 和 {{asset_symbol}}（运行时由
agents/skills_loader.py 替换）。
```

## 加载方式

`agents/<role>.py` 是薄 wrapper，调 `agents.skills_loader.load_skill()`：

```python
from agents.skills_loader import load_skill

def build_cio_prompt(asset):
    return load_skill(
        "cio",
        asset_name=asset.get("display_name", asset["symbol"]),
        asset_symbol=asset["symbol"],
    )
```

## 改 prompt 流程

1. 直接编辑对应 `SKILL.md`
2. 不要碰 `.py`（除非加新角色或新占位符）
3. 跑 `pytest tests/test_skills_loader.py` 确认 frontmatter 解析无误
4. commit `agents/skills/<role>/SKILL.md`，diff 一目了然

## DSPy / GEPA 集成

参考 `NousResearch/hermes-agent-self-evolution:evolution/skills/skill_module.py`
的做法：把 SKILL.md 当作"可优化参数"，DSPy/GEPA 直接读写 .md 文件。
当前 invest 用 `scripts/rl_optimize_prompts.py`（BootstrapFewShotWithRandomSearch）。
