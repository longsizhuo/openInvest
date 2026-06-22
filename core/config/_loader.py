"""注入优先级链实现（只对 tunable 生效）

优先级: default(dataclass) → YAML → CLI → env
locked 参数不经过这里。
"""
from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

import yaml

from .tunable import (
    DCAConfig,
    DreamingTunableConfig,
    MacroBucketConfig,
    OracleAccuracyConfig,
    RegimeConfig,
    RegimePerAssetConfig,
    PathConfig,
    RewardConfig,
    SentimentConfig,
    TunableConfig,
    ValuationConfig,
    VerdictConfig,
)

_DEFAULTS_YAML = Path(__file__).parent / "defaults.yaml"

# module-level 缓存
_config_cache: TunableConfig | None = None
_yaml_path_used: Path | None = None
_overrides_used: dict | None = None

# set_config_override() 存的持久 override（reset_config 时清空）
# load_config() 每次都会合入这个 override，不需要 _force_reload
_persistent_overrides: dict | None = None


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base（override 优先）。"""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _deep_set(d: dict, dotted_key: str, value: Any) -> None:
    """用点分路径设置嵌套 dict 值。'regime.trend_ma_spread_pct' → d['regime']['trend_ma_spread_pct']"""
    parts = dotted_key.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _read_env_overrides() -> dict[str, Any]:
    """读 INVEST_* 环境变量，映射到 config 结构。

    命名规则: INVEST_<SECTION>_<KEY> → section.key
    特殊处理:
    - INVEST_DREAMING_LOOKBACK_DAYS → dreaming.lookback_days
    - INVEST_DREAMING_WINDOWS → dreaming.windows (逗号分隔)
    - INVEST_REGIME_PER_ASSET_<SYMBOL>_<KEY> → regime_per_asset.<SYMBOL>.<key>
    """
    prefix = "INVEST_"
    overrides: dict[str, Any] = {}

    # 已知的多词 section 名（按长度降序排列，确保最长前缀优先匹配）
    _KNOWN_SECTIONS = sorted(
        ["regime_per_asset", "oracle_accuracy", "macro_buckets", "regime", "verdict",
         "dreaming", "reward", "sentiment", "valuation", "path", "dca"],
        key=len,
        reverse=True,
    )

    # 已知的 env → config 映射（处理不符合命名规则的旧变量）
    _LEGACY_MAP = {
        "INVEST_DREAMING_LOOKBACK_DAYS": "dreaming.lookback_days",
        # 旧裸 os.getenv 收进 config 后保留向后兼容（dreaming.llm_verify_enabled）
        "INVEST_DREAMING_LLM_VERIFY": "dreaming.llm_verify_enabled",
    }

    for key, val in os.environ.items():
        if not key.startswith(prefix):
            continue

        # 先查 legacy map
        if key in _LEGACY_MAP:
            dotted = _LEGACY_MAP[key]
        else:
            suffix = key[len(prefix):].lower()

            # 特殊处理: INVEST_REGIME_PER_ASSET_<SYMBOL>_<KEY>
            if suffix.startswith("regime_per_asset_"):
                rest = suffix[len("regime_per_asset_"):]
                parts = rest.split("_", 1)
                if len(parts) == 2:
                    symbol, field_name = parts
                    dotted = f"regime_per_asset.{symbol.upper()}.{field_name}"
                else:
                    continue
            else:
                # 匹配最长已知 section 前缀
                matched = False
                for section in _KNOWN_SECTIONS:
                    if suffix.startswith(section + "_"):
                        field_name = suffix[len(section) + 1:]
                        dotted = f"{section}.{field_name}"
                        matched = True
                        break
                if not matched:
                    continue

        # 类型转换
        try:
            converted: Any = float(val)
            if converted == int(converted):
                converted = int(converted)
        except ValueError:
            if val.lower() in ("true", "false"):
                converted = val.lower() == "true"
            elif "," in val:
                # 逗号分隔 → list（如 windows）
                converted = [v.strip() for v in val.split(",")]
            else:
                converted = val

        _deep_set(overrides, dotted, converted)

    return overrides


def _build_regime_per_asset(raw: dict[str, Any]) -> dict[str, RegimePerAssetConfig]:
    """把 YAML 的 per_asset dict 转成 RegimePerAssetConfig 实例。"""
    result = {}
    for symbol, overrides in raw.items():
        if isinstance(overrides, dict):
            result[symbol] = RegimePerAssetConfig(**overrides)
    return result


def _build_tunable_from_dict(data: dict[str, Any]) -> TunableConfig:
    """从 flat dict 构建 TunableConfig（处理嵌套结构）。"""
    # 提取 per_asset（可能来自两个位置，需要合并）
    # 1. regime.per_asset（YAML 嵌套结构）
    # 2. regime_per_asset（override 顶层 key，用于 set_config_override({"regime_per_asset": {...}})）
    regime_data = data.get("regime", {})
    per_asset_from_nested = regime_data.pop("per_asset", {})
    per_asset_from_top = data.pop("regime_per_asset", {})
    # 合并：顶层 override 优先
    per_asset_raw = {**per_asset_from_nested, **per_asset_from_top}
    per_asset = _build_regime_per_asset(per_asset_raw) if per_asset_raw else {}

    # 构建各子 config
    regime = RegimeConfig(**{k: v for k, v in regime_data.items() if k in {f.name for f in fields(RegimeConfig)}})
    verdict = VerdictConfig(**{k: v for k, v in data.get("verdict", {}).items() if k in {f.name for f in fields(VerdictConfig)}})

    dreaming_data = data.get("dreaming", {})
    # windows: list → tuple
    if "windows" in dreaming_data and isinstance(dreaming_data["windows"], list):
        dreaming_data = {**dreaming_data, "windows": tuple(dreaming_data["windows"])}
    dreaming = DreamingTunableConfig(**{k: v for k, v in dreaming_data.items() if k in {f.name for f in fields(DreamingTunableConfig)}})

    macro = MacroBucketConfig(**{k: v for k, v in data.get("macro_buckets", {}).items() if k in {f.name for f in fields(MacroBucketConfig)}})
    sentiment = SentimentConfig(**{k: v for k, v in data.get("sentiment", {}).items() if k in {f.name for f in fields(SentimentConfig)}})
    valuation = ValuationConfig(**{k: v for k, v in data.get("valuation", {}).items() if k in {f.name for f in fields(ValuationConfig)}})
    path = PathConfig(**{k: v for k, v in data.get("path", {}).items() if k in {f.name for f in fields(PathConfig)}})
    oracle = OracleAccuracyConfig(**{k: v for k, v in data.get("oracle_accuracy", {}).items() if k in {f.name for f in fields(OracleAccuracyConfig)}})
    reward = RewardConfig(**{k: v for k, v in data.get("reward", {}).items() if k in {f.name for f in fields(RewardConfig)}})

    dca_data = dict(data.get("dca", {}))
    # auto_dca_symbols: 单 str / 逗号 str / list → tuple（frozen dataclass 字段需不可变）
    if "auto_dca_symbols" in dca_data:
        v = dca_data["auto_dca_symbols"]
        if isinstance(v, str):
            v = [s.strip() for s in v.split(",") if s.strip()]
        dca_data["auto_dca_symbols"] = tuple(v)
    dca = DCAConfig(**{k: v for k, v in dca_data.items() if k in {f.name for f in fields(DCAConfig)}})

    return TunableConfig(
        regime=regime,
        regime_per_asset=per_asset,
        verdict=verdict,
        dreaming=dreaming,
        macro_buckets=macro,
        sentiment=sentiment,
        valuation=valuation,
        path=path,
        oracle_accuracy=oracle,
        reward=reward,
        dca=dca,
    )


def load_config(
    yaml_path: Path | None = None,
    cli_overrides: dict | None = None,
    *,
    _force_reload: bool = False,
) -> TunableConfig:
    """加载 tunable config，带 module-level 缓存。

    缓存策略：
    - 首次调用：构建 config，缓存到 module-level 变量
    - 后续调用：返回缓存（参数相同则直接返回，不重新解析）
    - 参数变化（yaml_path 或 cli_overrides 不同）：重新构建并更新缓存
    - sweep runner 需要重置时：调用 reset_config()
    - set_config_override() 存的 override 每次都合入（持久生效直到 reset）
    """
    global _config_cache, _yaml_path_used, _overrides_used

    # 合入持久 override（set_config_override 存的）+ 调用方传的 cli_overrides
    merged_cli = _merge_overrides(_persistent_overrides, cli_overrides)

    # 缓存命中：参数完全相同
    if (
        not _force_reload
        and _config_cache is not None
        and _yaml_path_used == yaml_path
        and _overrides_used == merged_cli
    ):
        return _config_cache

    # 1. dataclass defaults
    base = asdict(TunableConfig())

    # 2. YAML（默认 defaults.yaml，可被 custom 覆盖）
    effective_yaml = yaml_path if yaml_path is not None else _DEFAULTS_YAML
    if effective_yaml.exists():
        yaml_data = yaml.safe_load(effective_yaml.read_text()) or {}
        base = _deep_merge(base, yaml_data)

    # 3. CLI overrides（含持久 override）
    if merged_cli:
        base = _deep_merge(base, merged_cli)

    # 4. env overrides
    env_overrides = _read_env_overrides()
    if env_overrides:
        base = _deep_merge(base, env_overrides)

    # 5. 持久化 API override（GET/PUT /api/config 落盘，跨 cron/web/skill 三进程共读）。
    #    最高优先级——"配置走 API，env 退为 bootstrap 默认"（ADR-017）。白名单见 API_SETTABLE。
    persisted = _read_persisted_overrides()
    if persisted:
        base = _deep_merge(base, persisted)

    config = _build_tunable_from_dict(base)

    # 更新缓存
    _config_cache = config
    _yaml_path_used = yaml_path
    _overrides_used = merged_cli

    return config


def _merge_overrides(a: dict | None, b: dict | None) -> dict | None:
    """合并两层 override（a 是持久层，b 是调用层）。"""
    if a and b:
        return _deep_merge(a, b)
    return a or b


def reset_config() -> None:
    """清除缓存 + 持久 override，下次 load_config() 回到默认。

    使用场景：
    - sweep runner 每个 trial 开始前调用，注入新参数
    - 测试 fixture teardown 时调用，隔离不同 test 的 config 状态
    """
    global _config_cache, _yaml_path_used, _overrides_used, _persistent_overrides
    _config_cache = None
    _yaml_path_used = None
    _overrides_used = None
    _persistent_overrides = None


def set_config_override(overrides: dict) -> TunableConfig:
    """注入持久 override 并返回新 config。

    override 存在 module-level 变量里，后续所有 load_config() 调用都会合入，
    直到 reset_config() 清除。

    使用场景：
    - sweep runner 每个 trial 注入参数：set_config_override({"regime": {"trend_ma_spread_pct": 4.0}})
    - scripts/rl_train.py 替代旧的 monkey-patch
    - 测试注入特定值

    override dict 的结构与 defaults.yaml 一致（嵌套 dict，key 是 section.field）。
    """
    global _persistent_overrides
    _persistent_overrides = overrides
    # 清缓存，下次 load_config() 用新 override 重建
    return load_config(_force_reload=True)


# ============================================================================
# 持久化 API 配置层（ADR-017）：用户可经 GET/PUT /api/config 改的白名单 tunable，
# 落盘 memory/.state/config_overrides.json（MemoryStore 原子写 + fcntl），load_config
# 在 env 之上合入 → web/cron/skill 三进程共读一致；env 退为 bootstrap 默认。
# locked.py 永不在此暴露；内部 sweep 阈值也不进白名单（守 ADR-010）。
# ============================================================================

_PERSIST_STATE_NAME = "config_overrides"

# 白名单：dotted key → 元信息。只放"用户安全"的行为开关（GUI/agent 都经 /api/config 改）。
API_SETTABLE: Dict[str, Dict[str, Any]] = {
    "verdict.concentration_lens_enabled": {
        "type": "bool",
        "label": "集中度 lens",
        "help": "关闭=单资产/刻意集中策略不再因持仓集中度被建议减仓（仍保留波动/回撤/止损/估值风险）",
    },
    "verdict.risk_profile": {
        "type": "enum",
        "choices": ["steady", "aggressive"],
        "label": "风险档",
        "help": "steady=无方向锁（Sharpe/回撤最优，默认）；aggressive=uptrend 顺势加仓杠杆（高风险）",
    },
    "verdict.gold_defense_dca_enabled": {
        "type": "bool",
        "label": "黄金防御分批 DCA",
        "help": "高 VIX/ATR 区不全拦黄金买入，改分批放行；关闭=退回旧'全拦'行为",
    },
    "dreaming.llm_verify_enabled": {
        "type": "bool",
        "label": "Dreaming LLM 验伪",
        "help": "Deep Sleep 写 insights 前过一次廉价 LLM 挑伪相关（默认关=零 LLM 成本）",
    },
    "dca.auto_dca_enabled": {
        "type": "bool",
        "label": "自动定投",
        "help": "开启=jobs/dca_daily 每日给配置的 symbols 记一笔 external_funding 买入（不扣子弹池现金）；默认关",
    },
    "dca.auto_dca_amount_cny": {
        "type": "float",
        "label": "定投金额(CNY)",
        "help": "每个标的每次定投基准金额；智慧定投实际浮动，本系统按此估算记账、月度对账校准",
    },
}


def _coerce_and_validate(key: str, value: Any) -> Any:
    """按白名单 spec 校验 + 归一化 value；非法 → ValueError。"""
    if key not in API_SETTABLE:
        raise ValueError(f"config key 不可经 API 配置（不在白名单）: {key}")
    spec = API_SETTABLE[key]
    t = spec["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValueError(f"{key} 需 bool，得到 {value!r}")
    if t == "enum":
        if value in spec["choices"]:
            return value
        raise ValueError(f"{key} 需 ∈ {spec['choices']}，得到 {value!r}")
    if t == "float":
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 需 float，得到 {value!r}")
        if f < 0:
            raise ValueError(f"{key} 不能为负，得到 {f}")
        return f
    raise ValueError(f"未知白名单 type: {t}")  # pragma: no cover


def _read_persisted_overrides() -> dict:
    """读落盘 API override（memory/.state/config_overrides.json）。缺失/异常 → {}（优雅退化）。"""
    try:
        from core.memory_store import MemoryStore
        return MemoryStore().state_get(_PERSIST_STATE_NAME, {}) or {}
    except Exception:  # noqa: BLE001 — 配置读取永不应让调用方崩
        return {}


def _get_dotted(cfg: TunableConfig, dotted: str) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        cur = getattr(cur, part)
    return cur


def effective_api_config() -> List[Dict[str, Any]]:
    """GET /api/config 视图：白名单每项的当前生效值 + 是否被持久 override + 元信息。"""
    cfg = load_config()
    persisted = _read_persisted_overrides()
    out: List[Dict[str, Any]] = []
    for key, spec in API_SETTABLE.items():
        section, fld = key.split(".", 1)
        overridden = isinstance(persisted.get(section), dict) and fld in persisted[section]
        item: Dict[str, Any] = {
            "key": key,
            "value": _get_dotted(cfg, key),
            "overridden": overridden,
            "type": spec["type"],
            "label": spec["label"],
            "help": spec["help"],
        }
        if spec["type"] == "enum":
            item["choices"] = spec["choices"]
        out.append(item)
    return out


def set_persisted_override(key: str, value: Any) -> TunableConfig:
    """白名单校验后落盘一条 API override + 失效缓存。返回新 config。"""
    coerced = _coerce_and_validate(key, value)
    from core.memory_store import MemoryStore
    store = MemoryStore()
    cur = store.state_get(_PERSIST_STATE_NAME, {}) or {}
    _deep_set(cur, key, coerced)
    store.state_set(_PERSIST_STATE_NAME, cur)
    reset_config()
    return load_config()


def clear_persisted_override(key: str) -> TunableConfig:
    """删除一条持久 override（回退 env/yaml/默认）+ 失效缓存。返回新 config。"""
    if key not in API_SETTABLE:
        raise ValueError(f"config key 不在白名单: {key}")
    from core.memory_store import MemoryStore
    store = MemoryStore()
    cur = store.state_get(_PERSIST_STATE_NAME, {}) or {}
    section, fld = key.split(".", 1)
    if isinstance(cur.get(section), dict):
        cur[section].pop(fld, None)
        if not cur[section]:
            cur.pop(section, None)
    store.state_set(_PERSIST_STATE_NAME, cur)
    reset_config()
    return load_config()
