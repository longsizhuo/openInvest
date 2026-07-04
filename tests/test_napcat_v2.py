"""NapCat 11 命令在 v2 数据模型上的端到端验证

只测 6 个写命令 + balance（读路径足够覆盖其它 3 个查询命令）：
- _deposit / _withdraw 多币种
- _gold_buy upsert + 加权均价
- _gold_sell 余额校验 + cash 累加
- _gold_set 单纯覆盖
- _balance 显示

不测 _gold_offset（只动 strategy 不动 portfolio，逻辑独立）。
不测 _help / _strategy / _gold / _ndq / _history（纯读 + 外部行情）。

Fixture：tmp_path 隔离 MemoryStore，让 PortfolioManager 拿到 v2 portfolio.md
"""
from __future__ import annotations

from pathlib import Path

import pytest
import frontmatter

from openinvest.connectors import napcat_bot
from openinvest.connectors.napcat_bot import (
    CommandContext,
    _balance,
    _deposit,
    _gold_buy,
    _gold_sell,
    _gold_set,
    _withdraw,
)
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager


# ---------- helpers ----------

def _seed_memory(root: Path, *, cash_cny=10000.0, cash_aud=2000.0,
                 gold_grams=20.0, gold_avg=700.0,
                 ndq_shares=50.0, ndq_avg=38.0) -> None:
    """构造 v2 schema 的 portfolio.md / strategy.md / user.md 三件套"""
    root.mkdir(parents=True, exist_ok=True)

    holdings = []
    # fixture seed 用通用 channel/display_name —— 之前写死 CommSec / 浙商积存金，
    # fork 用户跑测试通过不代表他们的实际渠道路径被覆盖
    if ndq_shares > 0:
        holdings.append({
            "symbol": "NDQ.AX", "kind": "etf",
            "units": ndq_shares, "unit_label": "股",
            "avg_cost": ndq_avg, "cost_currency": "AUD",
            "channel": "broker_a",
            "display_name": "NDQ.AX",
            "proxy_kind": "direct",
        })
    if gold_grams > 0:
        holdings.append({
            "symbol": "GC=F", "kind": "metal",
            "units": gold_grams, "unit_label": "克",
            "avg_cost": gold_avg, "cost_currency": "CNY",
            "channel": "bank_gold_a",
            "display_name": "黄金（按克）",
            "yfinance_proxy": "GC=F",
            "proxy_kind": "gold_cny_per_gram",
            "sell_fee_pct": 0.0038,
        })

    portfolio_meta = {
        "name": "portfolio",
        "type": "portfolio",
        "schema_version": 2,
        "cash": {"CNY": cash_cny, "AUD": cash_aud},
        "holdings": holdings,
    }
    (root / "portfolio.md").write_text(
        frontmatter.dumps(frontmatter.Post("# portfolio", **portfolio_meta)),
        encoding="utf-8",
    )

    strategy_meta = {
        "name": "strategy", "type": "strategy",
        "target_assets": [
            {"symbol": "GC=F", "target_pct": 0.2,
             "max_single_buy_cny": 5000, "price_offset_pct": 0.025,
             "sell_fee_pct": 0.0038,
             "display_name": "黄金"},
            {"symbol": "NDQ.AX", "target_pct": 0.5,
             "max_single_buy_aud": 1000,
             "display_name": "NDQ"},
        ],
        "target_allocation_stock": 0.7,
        "target_allocation_cash": 0.3,
    }
    (root / "strategy.md").write_text(
        frontmatter.dumps(frontmatter.Post("# strategy", **strategy_meta)),
        encoding="utf-8",
    )

    user_meta = {
        "name": "user", "type": "user",
        "risk_level": "balanced",
    }
    (root / "user.md").write_text(
        frontmatter.dumps(frontmatter.Post("# user", **user_meta)),
        encoding="utf-8",
    )


def _ctx(pm: PortfolioManager, raw: str, args: list[str]) -> CommandContext:
    return CommandContext(pm=pm, user_id=12345, raw=raw, args=args)


@pytest.fixture
def pm(tmp_path):
    """每个测试独立 memory 根，互不污染"""
    _seed_memory(tmp_path)
    return PortfolioManager(MemoryStore(tmp_path))


# ---------- _balance（读路径，验证 v2 读取链路）----------

def test_balance_reads_v2_cash_and_holdings(pm, monkeypatch):
    """v2 数据进 → balance 文本能正确显示 CNY/AUD/NDQ/Gold"""
    import pandas as pd
    # 屏蔽外部行情，避免测试受网络影响
    monkeypatch.setattr(napcat_bot, "get_gold_snapshot", lambda **kw: None)
    monkeypatch.setattr(
        napcat_bot, "get_history_data",
        lambda *a, **kw: pd.DataFrame(),  # 空 df → ndq_price=0 分支
    )

    out = _balance(_ctx(pm, "/balance", []))
    assert "CNY: ¥10,000.00" in out
    assert "AUD: $2,000.00" in out
    # v2 通用化后 /balance 输出格式：`<display_name> (<symbol>): <units><unit_label>`
    # 不再硬编码 NDQ.AX / 浙商两条；遍历 holdings 列表显示
    assert "(NDQ.AX): 50" in out
    assert "(GC=F): 20" in out
    assert "克" in out  # 黄金 unit_label
    assert "$38" in out  # NDQ.AX AUD 均价
    assert "¥700" in out  # 黄金 CNY 均价


# ---------- _deposit ----------

def test_deposit_default_cny(pm):
    """单参 = CNY 入账"""
    out = _deposit(_ctx(pm, "/deposit 500", ["500"]))
    assert "CNY 500.00" in out
    assert pm.cash_amount("CNY") == pytest.approx(10500.0)


def test_deposit_with_currency_arg(pm):
    """两参 = 任意币种"""
    out = _deposit(_ctx(pm, "/deposit USD 100", ["USD", "100"]))
    assert "USD 100.00" in out
    assert pm.cash_amount("USD") == pytest.approx(100.0)
    # 原 CNY 不动
    assert pm.cash_amount("CNY") == pytest.approx(10000.0)


def test_deposit_invalid_amount(pm):
    """非法金额 → 拒绝，不动 portfolio"""
    out = _deposit(_ctx(pm, "/deposit abc", ["abc"]))
    assert "格式错误" in out
    assert pm.cash_amount("CNY") == pytest.approx(10000.0)


def test_deposit_no_args(pm):
    out = _deposit(_ctx(pm, "/deposit", []))
    assert "用法" in out


# ---------- _withdraw ----------

def test_withdraw_happy_path(pm):
    out = _withdraw(_ctx(pm, "/withdraw 300", ["300"]))
    assert "CNY 300.00" in out
    assert pm.cash_amount("CNY") == pytest.approx(9700.0)


def test_withdraw_insufficient_balance_rejected(pm):
    """v2 新加的硬校验：余额不足直接拒绝，不写"""
    out = _withdraw(_ctx(pm, "/withdraw 99999", ["99999"]))
    assert "余额不足" in out
    # 拒绝后 portfolio 完全不变
    assert pm.cash_amount("CNY") == pytest.approx(10000.0)


def test_withdraw_aud_currency(pm):
    out = _withdraw(_ctx(pm, "/withdraw AUD 500", ["AUD", "500"]))
    assert "AUD 500.00" in out
    assert pm.cash_amount("AUD") == pytest.approx(1500.0)


# ---------- _gold_buy ----------

def test_gold_buy_existing_holding_weighted_avg(pm):
    """已有 GC=F 持仓 → 累加克数 + 加权均价"""
    # seed 是 20g @ ¥700
    out = _gold_buy(_ctx(pm, "/gold_buy 10g @800", []))
    h = pm.holdings.find("GC=F")
    assert h is not None
    assert h["units"] == pytest.approx(30.0)
    # (700*20 + 800*10) / 30 = 733.33
    assert h["avg_cost"] == pytest.approx(733.33, abs=0.01)
    assert "30.0000g" in out


def test_gold_buy_creates_holding_when_none(tmp_path):
    """空仓首次买入 → upsert 一条 GC=F；strategy 没设 channel 时走通用 fallback"""
    _seed_memory(tmp_path, gold_grams=0)  # 不创 GC=F；fixture strategy 也没设 channel
    pm = PortfolioManager(MemoryStore(tmp_path))
    assert pm.holdings.find("GC=F") is None

    _gold_buy(_ctx(pm, "/gold_buy 5g @900", []))
    pm._reload()
    h = pm.holdings.find("GC=F")
    assert h is not None
    assert h["units"] == pytest.approx(5.0)
    assert h["avg_cost"] == pytest.approx(900.0)
    # B4: strategy.target_assets[GC=F] 没 channel 字段 → 走 _gold_defaults 通用 fallback
    # 不再硬编码"浙商积存金"（fork 用户用工行/招行/华安 ETF 各异）
    assert h["channel"] == "黄金（自营）"
    assert h["display_name"] == "实物黄金"


def test_gold_buy_uses_strategy_channel_when_configured(tmp_path):
    """strategy.target_assets[GC=F] 配了 channel/display_name → upsert 用配置值"""
    _seed_memory(tmp_path, gold_grams=0)
    pm = PortfolioManager(MemoryStore(tmp_path))
    # 在已有 strategy 上覆盖 GC=F entry 加 channel
    strat = pm.strategy
    new_targets = list(strat.get("target_assets") or [])
    for t in new_targets:
        if t.get("symbol") == "GC=F":
            t["channel"] = "工行积存金"
            t["display_name"] = "ICBC 黄金"
            break
    pm.store.write(
        "strategy", "strategy",
        {"target_assets": new_targets,
         "target_allocation_stock": strat.get("target_allocation_stock", 0.7),
         "target_allocation_cash": strat.get("target_allocation_cash", 0.3)},
        strat.body,
    )
    pm._reload()

    _gold_buy(_ctx(pm, "/gold_buy 3g @950", []))
    pm._reload()
    h = pm.holdings.find("GC=F")
    assert h["channel"] == "工行积存金"
    assert h["display_name"] == "ICBC 黄金"


def test_gold_buy_invalid_format(pm):
    out = _gold_buy(_ctx(pm, "/gold_buy abc", []))
    assert "用法" in out


# ---------- _gold_sell ----------

def test_gold_sell_happy_path_updates_cash_and_grams(pm):
    """卖 5g @1000 → grams -5, cash CNY +(5000 - fee)"""
    cny_before = pm.cash_amount("CNY")
    fee_pct = 0.0038
    expected_net = 5 * 1000 * (1 - fee_pct)

    _gold_sell(_ctx(pm, "/gold_sell 5g @1000", []))
    pm._reload()

    assert pm.holdings.find("GC=F")["units"] == pytest.approx(15.0)
    assert pm.cash_amount("CNY") == pytest.approx(cny_before + expected_net, abs=0.01)


def test_gold_sell_exceeds_holding_rolls_back(pm):
    """卖出超持仓 → RuntimeError，cash + holdings 都不变"""
    cny_before = pm.cash_amount("CNY")
    grams_before = pm.holdings.find("GC=F")["units"]

    with pytest.raises(RuntimeError, match="超过持仓"):
        _gold_sell(_ctx(pm, "/gold_sell 999g @1000", []))

    pm._reload()
    assert pm.cash_amount("CNY") == pytest.approx(cny_before)
    assert pm.holdings.find("GC=F")["units"] == pytest.approx(grams_before)


def test_gold_sell_invalid_format(pm):
    out = _gold_sell(_ctx(pm, "/gold_sell abc", []))
    assert "用法" in out


# ---------- _gold_set ----------

def test_gold_set_overwrites_grams_keeps_avg(pm):
    """直接覆盖克数，均价保持不变"""
    avg_before = pm.holdings.find("GC=F")["avg_cost"]

    _gold_set(_ctx(pm, "/gold_set 124.5", ["124.5"]))
    pm._reload()
    h = pm.holdings.find("GC=F")
    assert h["units"] == pytest.approx(124.5)
    assert h["avg_cost"] == pytest.approx(avg_before)


def test_gold_set_creates_when_missing(tmp_path):
    _seed_memory(tmp_path, gold_grams=0)
    pm = PortfolioManager(MemoryStore(tmp_path))
    assert pm.holdings.find("GC=F") is None

    _gold_set(_ctx(pm, "/gold_set 50", ["50"]))
    pm._reload()
    h = pm.holdings.find("GC=F")
    assert h is not None
    assert h["units"] == pytest.approx(50.0)
    assert h["avg_cost"] == pytest.approx(0.0)


def test_gold_set_invalid(pm):
    out = _gold_set(_ctx(pm, "/gold_set xx", ["xx"]))
    assert "格式错误" in out


# ---------- 跨 RMW 一致性（防 race 边界）----------

def test_deposit_then_withdraw_balanced(pm):
    """连续存取 100 次 +500 / -500，余额应回到原值"""
    for _ in range(50):
        _deposit(_ctx(pm, "/deposit 500", ["500"]))
        _withdraw(_ctx(pm, "/withdraw 500", ["500"]))
    assert pm.cash_amount("CNY") == pytest.approx(10000.0)
