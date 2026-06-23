---
type: adr
title: ADR-017 — 配置走 API（config-via-API）
status: accepted
date: 2026-06-17
tags: [config, governance, adr]
intent: 决策参数
schema_source:
  - core/config/_loader.py:API_SETTABLE
  - connectors/web_api/models.py:ConfigItem
documents:
  endpoints:
    - GET /api/config
    - PUT /api/config
  config_keys:
    - verdict.concentration_lens_enabled
    - verdict.risk_profile
    - verdict.gold_defense_dca_enabled
    - dreaming.llm_verify_enabled
  symbols: []
supersedes: []
superseded_by: []
---

# ADR-017 — 配置走 API（config-via-API）

**日期**: 2026-06-17
**状态**: Accepted
**关联**: [010-param-management](010-param-management.md) ·
        [013-trim-concentration-override](013-trim-concentration-override.md)

## 背景

ADR-010 把参数分三类：可调（`tunable.py`，走 default→YAML→CLI→env 注入链）、锁定
（`locked.py`，改需 ADR）、工程参数。但"可调"参数过去只能在**部署期**经 env / yaml 改——
GUI 小白用户和 agent **没有运行时改配置的途径**，破坏了产品哲学的三层对等
（GUI 能做的 agent 都能做、都走 Web API 底层）。

典型痛点：单资产 / 刻意集中策略的用户被委员会**永久 TRIM-for-concentration 纠缠**，
唯一规避手段是把 `emergency_buffer_cny` 填一个假的大数去触发 Sanity-4
（见 [013](013-trim-concentration-override.md)）——把"我有兜底钱"和"别唠叨我集中度"
两件事混为一谈。正确解法是给一个真 toggle，并把"配置普遍走 API"作为方向。

## 决策

### 规则 1：配置走 API，env 只留两桶

非机密、非部署引导的 **tunable 行为开关一律可经 API 运行时配置**，落盘持久化、跨进程共读。
env **只**保留两类：

| 桶 | 例子 | 为什么留 env |
|----|------|-------------|
| 机密 | `LLM_API_KEY` / `GITHUB_TOKEN` / Gmail / `INVEST_API_TOKEN` / `CF_ACCESS_*` | 绝不进 API 响应 / 不落配置存档 / 不进日志 |
| 部署引导 | `INVEST_API_BASE` / `INVEST_HOME` / 端口 / hub 模式 | 在配置系统起来之前就要决定进程"在哪跑 / 连哪" |

`locked.py` 既不进 env 也不进 API（改需 ADR）。

### 规则 2：持久 override 是注入链最高优先级（纯追加，不 reorder）

`core/config/_loader.py` 的 `load_config()` 在 env 之后**追加**一层持久 override：

```
default(dataclass) → YAML → CLI/set_config_override → env → [新增] 持久 API override
```

- 落盘 `memory/.state/config_overrides.json`（`MemoryStore.state_set/state_get`，原子写 + fcntl）。
- **纯追加一层、不改既有层相对顺序**：存档为空时行为与历史逐字节一致（零回归）。
- 优先级：持久 API override > env > yaml > 默认。即"配置走 API，env 退为 bootstrap 默认"。
- `load_config()` 是 web / cron / skill 三进程共同读取点 → 改一处三路径自动一致，零 entry 改动。

### 规则 3：只暴露白名单（`API_SETTABLE`），非白名单一律拒绝

`core/config/_loader.py:API_SETTABLE` 是**唯一可经 API 读写的 key 清单**，只放"用户安全"
的行为开关。首批：

| key | 类型 | 含义 |
|-----|------|------|
| `verdict.concentration_lens_enabled` | bool（默认 True） | 关=单资产/刻意集中策略不再因持仓集中度被建议减仓（仍保留波动/回撤/止损/估值）。见 [013](013-trim-concentration-override.md) |
| `verdict.risk_profile` | enum `steady`/`aggressive`（默认 steady） | aggressive=uptrend 顺势加仓杠杆（高风险） |
| `verdict.gold_defense_dca_enabled` | bool（默认 True） | 高 VIX/ATR 区黄金买入分批放行 vs 全拦 |
| `dreaming.llm_verify_enabled` | bool（默认 False） | Deep Sleep 写 insights 前过一次廉价 LLM 挑伪相关 |

内部 sweep 阈值（alloc ceiling / confidence floor / regime 阈值 / oracle / reward 等）
**不进白名单**（守 ADR-010 rule 4：未经 OOS 验证的阈值不当用户旋钮）。加新 key 必须显式
往 `API_SETTABLE` 加一行 + 写 type/label/help，PUT 按 spec 校验类型/枚举值。

### 规则 4：三层对等——同一底层

- **Web API**：`GET /api/config`（白名单生效值 + `overridden` + 元信息）、`PUT /api/config`
  （设一条 override）、`DELETE /api/config/{key}`（删 override 回退默认）。路由
  `connectors/web_api/routers/config.py`，模型 `connectors/web_api/models.py`。
- **CLI/skill**：`skill config` / `--set KEY VALUE` / `--clear KEY`
  （`scripts/skill_cmds/config_cmds.py`）。
- GUI（invest-gui）「设置 → 委员会配置」区读写同一端点。
- 三者都调同一份 `core.config.{effective_api_config, set_persisted_override, clear_persisted_override}`。

## 证据 / 不变量

- 持久存档为空 → `load_config()` 行为与历史一致（全套 790 passed，含 `test_api_override_beats_env`
  守"API > env"优先级、`test_env_applies_when_no_override` 守 env 仍是 bootstrap 默认）。
- `INVEST_DREAMING_LLM_VERIFY` 旧裸 `os.getenv` 收进 `dreaming.llm_verify_enabled`，经
  `_LEGACY_MAP` 向后兼容。
- PUT/DELETE 非白名单 key → 400/404；enum 非法值 → 400。

## 边界

- 不改 `locked.py` 的暴露面（依旧 ADR 闸）。
- 缓存：`load_config()` module 级缓存；API 写后 `reset_config()` 失效，同进程下次重读；
  跨进程（cron/skill 新进程）天然读最新存档。
- 远端 hub 模式：存档在 hub 的 `memory/.state/`，客户端经 `$INVEST_API_BASE` 转发，行为一致。

## 后续（2026-06-23, ADR-019）

集中度 lens 刚上线时与 ADR-013 的 solvency 自动兜底**并存**（Sanity 4 双触发）。
2026-06-23 [ADR-019](019-remove-solvency-concentration-override.md) 移除了 solvency 那条
自动触发：集中度是否构成约束，现在**只由 `verdict.concentration_lens_enabled` 这一个
显式开关控制**。lens 成为单一可信源，正合本 ADR"配置走显式 API、env 仅 bootstrap"的哲学。
