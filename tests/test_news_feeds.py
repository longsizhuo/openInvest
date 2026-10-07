"""用户级新闻源清单（INVEST_HOME/rss_feeds.yml）—— add/remove/merge 契约。

这是顾问模式暴露给群聊的写入口（add_news_source），护栏必须有测试钉住：
地址校验、probe 校验、name 规整、url 幂等、上限；以及 fetch_rss 的抓取护栏。
全程离线：DNS 走 _DNS 假表，真实建连被禁。
"""
from __future__ import annotations

import io
import socket
from unittest.mock import MagicMock

import pytest
import requests
import urllib3
import urllib3.util.connection
import yaml

_PUBLIC = "93.184.215.14"
_DNS = {
    "example.com": [_PUBLIC],
    "feeds.example.com": [_PUBLIC],
    "loop.example.com": ["127.0.0.1"],
    "lan.example.com": ["10.1.2.3"],
    "linklocal.example.com": ["169.254.1.1"],
    "mapped.example.com": ["::ffff:10.0.0.1"],
    "v6local.example.com": ["fe80::1"],
    "mcast.example.com": ["239.1.1.1"],
    "zero.example.com": ["0.0.0.0"],
    "mixed.example.com": [_PUBLIC, "10.0.0.1"],
    # IPv6 内嵌 IPv4 的各种形态
    "nat64.example.com": ["64:ff9b::7f00:1"],
    "v4compat.example.com": ["::127.0.0.1"],
    "sixtofour.example.com": ["2002:a00:1::1"],
    "teredo.example.com": ["2001:0:808:808::1"],
    "scoped.example.com": ["fe80::1%eth0"],
}


def _fake_getaddrinfo(host, port, *_a, **_kw):
    if host not in _DNS:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
            for ip in _DNS[host]]


def _no_network(*_a, **_kw):
    raise OSError("tests are offline")


@pytest.fixture
def home(tmp_path, monkeypatch):
    from openinvest import paths
    monkeypatch.setattr(paths, "INVEST_ROOT", tmp_path)
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(urllib3.util.connection, "create_connection", _no_network)
    return tmp_path


def test_add_list_remove_roundtrip(home, monkeypatch):
    from openinvest.services.news_sources import rss_feed as rf
    monkeypatch.setattr(rf, "fetch_rss", lambda *a, **k: [object()])  # probe 通过

    out = rf.add_extra_feed("WSJ-Markets!", "https://example.com/feed.xml")
    assert out["feed"]["name"] == "wsj_markets"  # 规整为 [a-z0-9_]
    assert out["already_exists"] is False
    assert rf.load_extra_feeds() == [{"name": "wsj_markets", "url": "https://example.com/feed.xml"}]

    # url 幂等：重复 add 返回已有条目，不写第二份
    again = rf.add_extra_feed("other_name", "https://example.com/feed.xml")
    assert again["already_exists"] is True
    assert len(rf.load_extra_feeds()) == 1

    # merged 清单 = 默认 + 额外
    assert {"name": "wsj_markets", "url": "https://example.com/feed.xml"} in rf.load_feeds()
    assert len(rf.load_feeds()) == len(rf.load_default_feeds()) + 1

    assert rf.remove_extra_feed("wsj_markets") is True
    assert rf.load_extra_feeds() == []
    assert rf.remove_extra_feed("nope") is False


def test_add_rejects_bad_input(home, monkeypatch):
    from openinvest.services.news_sources import rss_feed as rf
    with pytest.raises(ValueError, match="http"):
        rf.add_extra_feed("x", "ftp://nope/feed")
    with pytest.raises(ValueError, match="name"):
        rf.add_extra_feed("!!!", "https://example.com/feed")
    # probe 拉不到 entry → 拒绝（挡"随手贴个网页"）
    monkeypatch.setattr(rf, "fetch_rss", lambda *a, **k: [])
    with pytest.raises(ValueError, match="probe"):
        rf.add_extra_feed("x", "https://example.com/not-a-feed")
    # 与默认源撞名 → 拒绝
    monkeypatch.setattr(rf, "fetch_rss", lambda *a, **k: [object()])
    default_name = rf.load_default_feeds()[0]["name"]
    with pytest.raises(ValueError, match="占用"):
        rf.add_extra_feed(default_name, "https://example.com/another-feed")


def test_extra_feeds_cap(home, monkeypatch):
    from openinvest.services.news_sources import rss_feed as rf
    monkeypatch.setattr(rf, "fetch_rss", lambda *a, **k: [object()])
    for i in range(rf.MAX_EXTRA_FEEDS):
        rf.add_extra_feed(f"feed{i}", f"https://example.com/{i}")
    with pytest.raises(ValueError, match="上限"):
        rf.add_extra_feed("overflow", "https://example.com/overflow")


def test_broken_extra_yml_degrades_to_empty(home):
    from openinvest.services.news_sources import rss_feed as rf
    (home / "rss_feeds.yml").write_text(":: not yaml [", encoding="utf-8")
    assert rf.load_extra_feeds() == []
    assert rf.load_feeds() == rf.load_default_feeds()  # 抓取链不受坏文件影响


def test_default_feeds_env_override(home, monkeypatch, tmp_path):
    """INVEST_RSS_FEEDS_YML 整体替换默认清单（此前只写在注释里没实现）。"""
    from openinvest.services.news_sources import rss_feed as rf
    custom = tmp_path / "custom.yml"
    custom.write_text(yaml.safe_dump({"feeds": [{"name": "only", "url": "https://x/f"}]}),
                      encoding="utf-8")
    monkeypatch.setenv("INVEST_RSS_FEEDS_YML", str(custom))
    assert rf.load_default_feeds() == [{"name": "only", "url": "https://x/f"}]


@pytest.mark.parametrize("host", ["loop", "lan", "linklocal", "mapped", "v6local", "mcast", "zero", "mixed",
                                  "nat64", "v4compat", "sixtofour", "teredo", "scoped"])
def test_add_rejects_non_public_address(home, monkeypatch, host):
    """主机解析出任一非公网地址 → 拒绝，且不发 probe、不落盘。"""
    from openinvest.services.news_sources import rss_feed as rf
    probe = MagicMock(return_value=[object()])
    monkeypatch.setattr(rf, "fetch_rss", probe)
    with pytest.raises(ValueError, match="非公网"):
        rf.add_extra_feed("x", f"https://{host}.example.com/feed")
    assert not probe.called
    assert rf.load_extra_feeds() == []


def test_add_rejects_userinfo_and_unresolvable(home, monkeypatch):
    from openinvest.services.news_sources import rss_feed as rf
    monkeypatch.setattr(rf, "fetch_rss", lambda *a, **k: [object()])
    with pytest.raises(ValueError, match="用户名"):
        rf.add_extra_feed("x", "https://u:p@example.com/feed")
    with pytest.raises(ValueError, match="解析失败"):
        rf.add_extra_feed("x", "https://nxdomain.example.com/feed")
    assert rf.load_extra_feeds() == []


_RSS = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        b'<item><title>Hello</title><link>/a</link></item></channel></rss>')


def _resp(status, body=b"", headers=None):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r.raw = urllib3.HTTPResponse(body=io.BytesIO(body), preload_content=False)
    return r


@pytest.fixture
def fake_get(monkeypatch):
    """Session.get 假实现：routes = {url: Response}；记录每次调用 (url, kwargs)。"""
    calls, routes = [], {}

    def _get(_self, url, **kw):
        calls.append((url, kw))
        return routes[url]
    monkeypatch.setattr(requests.Session, "get", _get)
    return calls, routes


def test_fetch_rss_timeouts_and_safe_redirect(home, fake_get):
    from openinvest.services.news_sources import rss_feed as rf
    calls, routes = fake_get
    routes["https://feeds.example.com/old"] = _resp(301, headers={"Location": "/rss"})
    routes["https://feeds.example.com/rss"] = _resp(200, _RSS, {"Content-Type": "application/rss+xml"})
    out = rf.fetch_rss("t", "https://feeds.example.com/old")
    assert [(i.title, i.url) for i in out] == [("Hello", "https://feeds.example.com/a")]  # 相对链接按最终 URL
    assert [u for u, _ in calls] == ["https://feeds.example.com/old", "https://feeds.example.com/rss"]
    assert all(kw["timeout"] and kw["allow_redirects"] is False and kw["stream"] for _, kw in calls)


def test_fetch_rss_rejects_redirect_to_non_public(home, fake_get):
    from openinvest.services.news_sources import rss_feed as rf
    calls, routes = fake_get
    routes["https://feeds.example.com/rss"] = _resp(302, headers={"Location": "http://lan.example.com/x"})
    routes["http://lan.example.com/x"] = _resp(200, _RSS)
    assert rf.fetch_rss("t", "https://feeds.example.com/rss") == []
    assert [u for u, _ in calls] == ["https://feeds.example.com/rss"]  # 内网那一跳根本没发


def test_fetch_rss_caps_redirects_body_and_time(home, fake_get, monkeypatch):
    from openinvest.services.news_sources import rss_feed as rf
    calls, routes = fake_get
    for i in range(5):
        routes[f"https://feeds.example.com/r{i}"] = _resp(302, headers={"Location": f"/r{i + 1}"})
    assert rf.fetch_rss("t", "https://feeds.example.com/r0") == []
    assert len(calls) == rf._MAX_REDIRECTS + 1

    url = "https://feeds.example.com/rss"
    monkeypatch.setattr(rf, "_MAX_FEED_BYTES", len(_RSS) - 1)
    routes[url] = _resp(200, _RSS)
    assert rf.fetch_rss("t", url) == []          # 超体积
    monkeypatch.setattr(rf, "_MAX_FEED_BYTES", len(_RSS))
    monkeypatch.setattr(rf, "_FETCH_DEADLINE_SEC", -1)
    routes[url] = _resp(200, _RSS)
    assert rf.fetch_rss("t", url) == []          # 超总时长
    monkeypatch.setattr(rf, "_FETCH_DEADLINE_SEC", 30)
    routes[url] = _resp(200, _RSS)
    assert len(rf.fetch_rss("t", url)) == 1      # 对照：上限内照常解析


def test_operator_default_feeds_skip_address_check(home, fake_get, monkeypatch, tmp_path):
    """运维配置的默认清单可信（wiki 推荐自建 RSSHub 常在本机）；非默认源同一地址被拒。"""
    from openinvest.services.news_sources import rss_feed as rf
    calls, routes = fake_get
    url = "http://loop.example.com:1200/rss"
    routes[url] = _resp(200, _RSS)
    custom = tmp_path / "custom.yml"
    custom.write_text(yaml.safe_dump({"feeds": [{"name": "hub", "url": url}]}), encoding="utf-8")
    monkeypatch.setenv("INVEST_RSS_FEEDS_YML", str(custom))
    assert len(rf.fetch_rss("hub", url)) == 1
    monkeypatch.delenv("INVEST_RSS_FEEDS_YML")
    routes[url] = _resp(200, _RSS)
    assert rf.fetch_rss("hub", url) == []


def test_fetch_connects_to_the_validated_address(home, monkeypatch):
    """校验时解析到公网、连接时再解析变成内网 → 必须连校验过的那个地址，且 Host 头保持原主机名。"""
    import threading

    from openinvest.services.news_sources import rss_feed as rf

    answers = iter([[_PUBLIC], ["10.0.0.1"]])  # 第一次解析公网，之后变内网
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
        for ip in (next(answers) if host == "flip.example.com" else _DNS[host])])

    client, server = socket.socketpair()
    dialed, request = [], []

    def _fake_create_connection(address, *_a, **_kw):  # 模拟 urllib3 建连：主机名会再解析一次
        host = address[0]
        dialed.append(host if host[0].isdigit() else socket.getaddrinfo(host, address[1])[0][4][0])
        return client

    def _serve():
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += server.recv(4096)
        request.append(buf)
        server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(_RSS) + _RSS)
        server.close()

    monkeypatch.setattr(urllib3.util.connection, "create_connection", _fake_create_connection)
    threading.Thread(target=_serve, daemon=True).start()
    out = rf.fetch_rss("t", "http://flip.example.com/rss")
    assert dialed == [_PUBLIC]
    assert b"\r\nHost: flip.example.com\r\n" in request[0]
    assert [i.title for i in out] == ["Hello"]


def test_pinned_adapter_keeps_tls_hostname():
    """https 连接钉 IP 时，SNI / 证书校验仍按原主机名。"""
    from openinvest.services.news_sources import rss_feed as rf
    req = requests.Request("GET", "https://feeds.example.com/rss").prepare()
    host_params, pool_kwargs = rf._PinnedAdapter(_PUBLIC).build_connection_pool_key_attributes(req, True)
    assert host_params["host"] == _PUBLIC
    assert pool_kwargs["server_hostname"] == "feeds.example.com"
