---
type: wiki-chapter
title: 配置参考（LLM Provider + Runtime Overrides）
tags: [config, env, llm-provider, runtime-overrides, adr-017]
intent: LLM provider .env 配置契约与运行时可调参数的单一参考页（从 README Configuration 章节迁入）
documents:
  endpoints: []
  config_keys:
    - LLM_API_KEY
    - LLM_BASE_URL
    - LLM_MODEL
    - verdict.concentration_lens_enabled
    - verdict.risk_profile
    - verdict.gold_defense_dca_enabled
    - dca.auto_dca_enabled
    - dca.auto_dca_amount_cny
  symbols: []
---

# 配置参考（LLM Provider + Runtime Overrides）

> 本页是 README「Configuration」章节的完整版（2026-07 从 README 迁入）。
> 机制设计依据见 [ADR-017](adr/017-config-via-api.md)（config-via-API 白名单）；
> 端点与白名单实现细节见 [06-api.md](06-api.md)。

## LLM Provider 配置（.env）

系统默认采用 DeepSeek 端点，支持任何标准 OpenAI 兼容 API。替换 provider 时按 `.env` 契约映射以下变量（务必确保 `LLM_MODEL` 是目标 provider 的官方真实模型 ID，不可只改 URL）：

```env
# === 选项 A: DeepSeek (默认) ===
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# === 选项 B: 通义千问 (Aliyun DashScope 兼容模式) ===
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-max

# === 选项 C: 智谱 AI (GLM OpenAI 兼容端点) ===
LLM_API_KEY=xxxxxxxxxxxxxxxx.xxxxxxxxx
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v1
LLM_MODEL=glm-4-flash
```

## Tunable Runtime Overrides（运行时可调参数）

依据 [ADR-017](adr/017-config-via-api.md)，以下 Tunable 变量在 CLI / REST API / `.env` 通道具备一致的覆盖优先级，持久化于 `memory/.state/config_overrides.json`：

| Config Key | 类型（默认值） | 行为 |
|---|---|---|
| `verdict.concentration_lens_enabled` | `bool` (`true`) | **持仓集中度过滤器**。开启时对过度集中的资产触发告警 / TRIM；关闭后集中度不再触发 TRIM 警告（波动率与估值风控仍生效）。见 [ADR-019](adr/019-remove-solvency-concentration-override.md) |
| `verdict.risk_profile` | `str` (`"steady"`) | 风险偏好：`steady`（稳健）/ `aggressive`（下行阶段允许更大规模买入）。 |
| `verdict.gold_defense_dca_enabled` | `bool` (`true`) | 黄金防御机制。高波动骤增阶段把单次大额加仓拆分为多期 DCA。 |
| `dca.auto_dca_enabled` | `bool` (`false`) | 全自动定投决策总开关。 |
| `dca.auto_dca_amount_cny` | `float` (`0.0`) | 每期自动定投的基准人民币配置额度。见 [ADR-018](adr/018-dca-dip-reserve.md) |

### 运行时覆盖示例（以关闭集中度 Lens 为例）

```bash
# 途径 1: CLI
uvx openinvest config --set verdict.concentration_lens_enabled false

# 途径 2: REST API（deprecated surface —— 仅 remote hub 模式）
curl -X PUT http://localhost:8765/api/config -d '{"key":"verdict.concentration_lens_enabled","value":false}'
```

> 白名单 `API_SETTABLE`（`core/config/_loader.py`）只放用户安全的行为开关；机密与部署引导仍只走 env，`locked.py` 永不暴露。
