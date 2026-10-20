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
    # 测试环境没有 LLM key，reasoning_by_agent() 自动判会落到 agent 模式；既有测试断言的是
    # backend 行为，默认钉 backend，测 agent 模式的测试在 body 里 setenv("…", "agent")
    monkeypatch.setenv("INVEST_REASONING_BY", "backend")
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
def _isolate_market_fetch_log(monkeypatch):
    """exchange_fee._STORE 是 import 时连上 INVEST_ROOT/db 的全局实例。只 patch 了它个别方法的测试
    （test_backtest_no_lookahead / test_betashares_fallback 等）会经 seconds_since_fetch / mark_fetched
    读写开发者本地库的 fetch_log——默认钉成进程内 dict。换成 tmp MarketStore 的测试不受影响。"""
    import time
    import openinvest.utils.exchange_fee as ef
    log: dict[str, float] = {}
    monkeypatch.setattr(ef._STORE, "mark_fetched", lambda symbol: log.__setitem__(symbol, time.time()))
    monkeypatch.setattr(ef._STORE, "seconds_since_fetch",
                        lambda symbol: time.time() - log[symbol] if symbol in log else float("inf"))


_LOOPBACK_HOSTS = {None, "", "localhost", "127.0.0.1", "::1", "testserver"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """测试一律不联网。2026-10-19 test_committee_run_error_path 在 CI 偶发红：session 里
    load_sentiment_brief 真去拉 ^VIX（yfinance）+ CNN 恐贪（urllib，超时 4s），网慢就吃光测试的
    5s 截止时间。全套断网普查同类漏 mock 十几处（sentiment / fx / gold_price / valuation …），
    所以在出口统一挡，不逐个补。

    挡两层：yfinance 走 curl_cffi（libcurl 自己解析和建连，patch socket 挡不住）；其余
    urllib / requests / smtplib 都要经 socket.getaddrinfo。被挡时抛连接错误，生产代码的
    graceful 退化路径照常走；要测网络出口的测试照常 monkeypatch 上层函数。"""
    import socket
    from curl_cffi import requests as curl_requests

    real_getaddrinfo = socket.getaddrinfo

    def _getaddrinfo(host, *args, **kwargs):
        if host not in _LOOPBACK_HOSTS:
            raise socket.gaierror(f"tests are offline (conftest._no_network): {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    def _curl_request(self, method, url, *args, **kwargs):
        raise curl_requests.exceptions.ConnectionError(f"tests are offline (conftest._no_network): {url}")

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(curl_requests.Session, "request", _curl_request)


@pytest.fixture(autouse=True)
def _no_digest_calendar_network(monkeypatch):
    """日报"今日持仓归因"的财报 / 除息日历是该段联网的一步（yfinance Ticker.calendar；基准刷新另走 account_performance）。
    测试默认当取不到（→ "无数据源"），任何经 daily_report.run 的测试都不会真发请求；
    要测日历的测试显式给 build_holdings_digest 传 calendar_fn。"""
    monkeypatch.setattr("openinvest.services.holdings_digest._yf_calendar", lambda symbol: {})
