"""pytest 配置：让仓库根加进 sys.path，无需安装即可 import core/jobs/scripts"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# 真实对外发送的凭证（Discord DM 经 ChatBot 转发、Gmail SMTP）。load_dotenv() 会从开发机 .env
# 灌进来（worktree 里还会向上找到主 checkout 的 .env），漏 patch 一个出口就真发——2026-10-13
# 本地全量 pytest 把 test_committee_verdict_email 的 5 条 fixture verdict 推进了真实 Discord DM。
# 每个测试前清掉，出口函数拿不到凭证就直接返回；要测出口本身的测试照常 monkeypatch.setenv。
OUTBOUND_KEYS = ("CHATBOT_ALERT_URL", "CHATBOT_INTERNAL_KEY", "EMAIL_SENDER", "EMAIL_PASSWORD", "DIGEST_EMAIL_TO")


@pytest.fixture(autouse=True)
def _isolate_invest_env(monkeypatch):
    """测试与开发者本地 .env 隔离（修"配置渗进测试"这一类 bug）。

    services/notifier.py 等模块在 import 时 load_dotenv()，把开发者机器 .env 里的
    INVEST_* override 灌进 os.environ。全套跑时这些值会渗进"断言仓库默认值"的测试
    （DCA / 集中度 lens 等），导致本地 pytest 顺序敏感地飘红——CI 没 .env 不受影响，
    所以是只在开发机出现的假红。本 fixture 每个测试前清掉所有 INVEST_* env，让
    load_config() 看到的是 tunable.py 默认值；需要特定 env 的测试照常用
    monkeypatch.setenv（在 test body 里设，晚于本 fixture，不冲突）。

    仅清 env 不够：load_config() 有模块级 _config_cache（只在 cache-miss 时读
    os.environ），且 set_config_override 写的 _persistent_overrides 也是模块全局——
    任一在 env 还在时被填充，清 env 后 cache/override 仍命中旧值，默认值断言照样飘红。
    所以 yield 前后各 reset_config() 一次：清缓存 + 清 persistent overrides，让本 fixture
    成为真正的 per-test config 隔离闸（不再只隔离 env 一半）。
    """
    from openinvest.core.config import reset_config
    for key in [k for k in os.environ if k.startswith("INVEST_")] + list(OUTBOUND_KEYS):
        monkeypatch.delenv(key, raising=False)
    reset_config()
    yield
    reset_config()


@pytest.fixture(autouse=True)
def _isolate_memory_root(tmp_path, monkeypatch):
    """MemoryStore() 默认目录指到每个测试自己的 tmp（2026-05-10 事故类：测试调 cmd_init 时
    只 patch 了 ROOT，MemoryStore() 仍落在 clone 的 memory/，把 fixture 写进真实 portfolio.md）。
    需要特定目录的测试照常在 test body 里 patch MEMORY_ROOT（晚于本 fixture，不冲突）。"""
    import openinvest.core.memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "_memory_root")


@pytest.fixture(autouse=True)
def _no_digest_calendar_network(monkeypatch):
    """日报"今日持仓归因"的财报 / 除息日历是该段联网的一步（yfinance Ticker.calendar；基准刷新另走 account_performance）。
    测试默认当取不到（→ "无数据源"），任何经 daily_report.run 的测试都不会真发请求；
    要测日历的测试显式给 build_holdings_digest 传 calendar_fn。"""
    monkeypatch.setattr("openinvest.services.holdings_digest._yf_calendar", lambda symbol: {})
