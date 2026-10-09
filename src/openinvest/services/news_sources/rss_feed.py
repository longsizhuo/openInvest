"""RSS feed 适配器 —— feedparser 解析免费财经 RSS

默认源在 rss_feeds.yml，env 可覆盖 INVEST_RSS_FEEDS_YML 指向自定义文件。

为什么 RSS 而不是 API：
- Reuters/BBC/FT/财新 都没免费 API key 那条路，但 RSS 是 public + 稳定
- 拿到的是结构化 entry（title/link/summary/published），不用抓 HTML
"""
from __future__ import annotations

import io
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import requests
import requests.adapters
import urllib3
import yaml

from openinvest.core.memory_store import _atomic_write_text, _file_lock
from openinvest.services.news_sources import RawNewsItem

log = logging.getLogger(__name__)

# 默认 yml 在同目录
_DEFAULT_YML = Path(__file__).parent / "rss_feeds.yml"

# 用户级额外源上限——add_news_source 开放给顾问模式群聊，无上限等于把
# normalize 的 LLM 账单交给陌生人
MAX_EXTRA_FEEDS = 30

# 抓取护栏：(connect, read) 超时 + 整次抓取（含 DNS 之后的建连/TLS/响应头/重定向/
# 响应体）一个总时长上限——read 超时只管单次读，慢速持续吐字节要靠总时长 + 到点
# 强制断开连接兜住；重定向手动跟、最多 3 跳；响应体 ≤2MB
_FETCH_TIMEOUT = (5, 15)
_FETCH_DEADLINE_SEC = 30
_MAX_REDIRECTS = 3
_MAX_FEED_BYTES = 2 * 1024 * 1024


# 内嵌 IPv4 的 IPv6 段（NAT64 两段 / IPv4-translated / 已废弃的 IPv4 兼容地址）：取低 32 位一起判
_V4_EMBEDDING_NETS = tuple(ipaddress.ip_network(n) for n in (
    "64:ff9b::/96", "64:ff9b:1::/48", "::ffff:0:0:0/96", "::/96"))
_REJECTED = "url 被拒：主机必须能解析且只指向公网地址（额外源不经代理、直连）"


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """is_global 已排除 loopback/私网/link-local/保留/未指定，但组播、已废弃的 site-local
    （fec0::/10）仍算 global；IPv6 里内嵌的 IPv4（mapped / 6to4 / Teredo / NAT64 /
    translated / 兼容地址）要连同 v6 本身都是公网才放行。"""
    if ip.is_multicast or not ip.is_global or getattr(ip, "is_site_local", False):
        return False
    if ip.version == 6:
        embedded = [ip.ipv4_mapped, ip.sixtofour, *(ip.teredo or ())]
        if any(ip in n for n in _V4_EMBEDDING_NETS):
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        return all(_is_public(v4) for v4 in embedded if v4 is not None)
    return True


def _check_url(url: str) -> str:
    """只放行 http(s)、不带 userinfo、且主机解析出的每个地址都是公网地址的 URL；否则抛 ValueError。

    返回要连接的地址（解析结果第一条，与系统默认连接顺序一致），供 _FeedAdapter 钉住。
    解析失败与非公网对调用方是同一条报错（细节只进日志），不给群聊用户探测内网 DNS。
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"url 必须是 http(s) RSS/Atom 地址: {url!r}")
    if "@" in parts.netloc:
        raise ValueError(f"url 不能带用户名/密码: {url!r}")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as e:  # gaierror ⊂ OSError；非法端口 parts.port 抛 ValueError
        log.info(f"RSS url 拒绝（解析失败）: {parts.hostname}: {e}")
        raise ValueError(_REJECTED) from None
    for *_, sockaddr in infos:
        try:
            ok = _is_public(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            ok = False
        if not ok:
            log.info(f"RSS url 拒绝（非公网地址）: {parts.hostname} -> {sockaddr[0]}")
            raise ValueError(_REJECTED)
    return infos[0][4][0]


# 本线程正在抓的 feed 的连接（dup 出的 socket，与真实连接同一条内核 socket）——
# 到点由看门狗 shutdown，卡在建连/TLS/响应头/响应体任何一步的阻塞读都会立刻返回
_inflight = threading.local()


class _WatchedConn:
    def _new_conn(self):
        sock = super()._new_conn()
        socks = getattr(_inflight, "socks", None)
        if socks is not None:
            socks.append(sock.dup())
        return sock


class _WatchedHTTPPool(urllib3.HTTPConnectionPool):
    ConnectionCls = type("_WatchedHTTPConnection", (_WatchedConn, urllib3.connection.HTTPConnection), {})


class _WatchedHTTPSPool(urllib3.HTTPSConnectionPool):
    ConnectionCls = type("_WatchedHTTPSConnection", (_WatchedConn, urllib3.connection.HTTPSConnection), {})


_WATCHED_POOLS = {"http": _WatchedHTTPPool, "https": _WatchedHTTPSPool}


class _FeedAdapter(requests.adapters.HTTPAdapter):
    """所有 feed 的连接都登记给看门狗；ip 非空时连接直接打到 _check_url 校验过的那个地址
    （不再二次解析），Host 头与 TLS SNI/证书校验仍按原主机名。"""

    def __init__(self, ip: Optional[str] = None):
        self._ip = ip
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = _WATCHED_POOLS

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        manager = super().proxy_manager_for(proxy, **proxy_kwargs)
        if type(manager) is urllib3.ProxyManager:  # SOCKS 代理有自己的连接类，不动
            manager.pool_classes_by_scheme = _WATCHED_POOLS
        return manager

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        if self._ip:
            pool_kwargs["server_hostname"] = host_params["host"]  # http 池由 urllib3 自动丢弃
            host_params["host"] = self._ip
        return host_params, pool_kwargs

    def add_headers(self, request, **kwargs):
        if self._ip:
            request.headers["Host"] = urlsplit(request.url).netloc


def _shutdown_all(socks: list) -> None:
    for s in list(socks):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _fetch_feed(url: str, *, public_only: bool = True):
    """抓 feed 并解析 —— fetch_rss 的所有源都走这里（护栏见 _FETCH_* 常量）。

    public_only=True 时每一跳（含重定向目标）都重新解析+校验，钉住校验过的地址直连
    （不走环境代理）；False（运维默认源）只信任同主机的跳转，跨主机跳转照样校验。
    """
    import feedparser

    deadline = time.monotonic() + _FETCH_DEADLINE_SEC
    origin_host = urlsplit(url).hostname
    _inflight.socks = socks = []
    watchdog = threading.Timer(_FETCH_DEADLINE_SEC, _shutdown_all, (socks,))
    watchdog.daemon = True
    watchdog.start()
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(f"feed 抓取超过 {_FETCH_DEADLINE_SEC}s 上限: {url}")
            session = requests.Session()
            if public_only or urlsplit(url).hostname != origin_host:
                session.trust_env = False  # 不读环境代理/.netrc：经代理时由代理自行解析主机名，钉住的地址就不生效
                adapter = _FeedAdapter(_check_url(url))
            else:
                adapter = _FeedAdapter()
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            timeout = tuple(min(t, remaining) for t in _FETCH_TIMEOUT)
            with session, session.get(url, timeout=timeout, stream=True, allow_redirects=False,
                                      headers={"User-Agent": feedparser.USER_AGENT}) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers["location"])
                    continue
                resp.raise_for_status()
                body = bytearray()
                # read1：有数据就返回，总时长检查才不会被一次凑满 chunk 的阻塞读架空
                while chunk := resp.raw.read1(64 * 1024, decode_content=True):
                    body += chunk
                    if len(body) > _MAX_FEED_BYTES or time.monotonic() > deadline:
                        raise ValueError(f"feed 超过 {_MAX_FEED_BYTES} 字节或 {_FETCH_DEADLINE_SEC}s 上限: {url}")
                headers = {k.lower(): v for k, v in resp.headers.items()}
                headers["content-location"] = url  # 相对链接按最终 URL 解析（同原 parse(url) 行为）
                # BytesIO 包一层：feedparser 收到裸 bytes/str 会尝试当本地路径打开
                return feedparser.parse(io.BytesIO(body), response_headers=headers)
        raise ValueError(f"重定向超过 {_MAX_REDIRECTS} 次: {url}")
    except Exception:
        if time.monotonic() >= deadline:
            raise ValueError(f"feed 抓取超过 {_FETCH_DEADLINE_SEC}s 上限，已断开: {url}") from None
        raise
    finally:
        watchdog.cancel()
        _inflight.socks = None
        for s in socks:
            s.close()


def fetch_rss(name: str, url: str, *, max_items: int = 20) -> List[RawNewsItem]:
    """单个 RSS feed → RawNewsItem 列表"""
    # 运维配的默认清单（包内 yml / INVEST_RSS_FEEDS_YML）可信——wiki 推荐的自建
    # RSSHub 常在本机/内网；其余（群聊 add 的额外源、add 时的 probe）只放行公网地址
    public_only = url not in {f.get("url") for f in load_default_feeds()}
    try:
        parsed = _fetch_feed(url, public_only=public_only)
    except Exception as e:
        direct = "（额外源直连、不经代理）" if public_only else ""
        log.warning(f"RSS {name} 抓取/解析失败{direct}: {e}")
        return []

    items: List[RawNewsItem] = []
    for entry in (parsed.entries or [])[:max_items]:
        link = entry.get("link") or entry.get("id") or ""
        title = entry.get("title") or ""
        if not link or not title:
            continue
        snippet = entry.get("summary") or entry.get("description") or ""
        # feedparser 给出 published_parsed (time.struct_time)，转 ISO
        published = None
        if entry.get("published_parsed"):
            try:
                published = datetime(*entry["published_parsed"][:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
            except Exception:
                published = None
        items.append(RawNewsItem(
            src_name=f"rss:{name}",
            title=title.strip(),
            url=link.strip(),
            snippet=_trim(_strip_html(snippet), 260),
            published_at=published,
            raw_meta={"feed_name": name, "feed_url": url},
        ))
    return items


def load_default_feeds(yml_path: Optional[Path] = None) -> List[Dict[str, str]]:
    """加载默认 RSS feed 列表 (name + url)。

    优先级：显式 yml_path > env `INVEST_RSS_FEEDS_YML`（整体替换默认清单）>
    包内 rss_feeds.yml。env 此前只写在注释里没实现，现在是真的。
    """
    env_p = os.getenv("INVEST_RSS_FEEDS_YML", "").strip()
    p = Path(yml_path) if yml_path else (Path(env_p) if env_p else _DEFAULT_YML)
    if not p.exists():
        log.warning(f"RSS feed yml 不存在: {p}")
        return []
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("feeds", []) or []


# ------------------------------------------------------------------
# 用户级额外源（INVEST_HOME/rss_feeds.yml）—— MCP/CLI news_sources 管理
# ------------------------------------------------------------------

def _extra_yml() -> Path:
    from openinvest import paths
    return paths.INVEST_ROOT / "rss_feeds.yml"


def load_extra_feeds() -> List[Dict[str, str]]:
    """用户/群聊自助添加的额外源。文件不存在 = 没加过，返回空。"""
    p = _extra_yml()
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("feeds", []) or []
    except Exception as e:  # noqa: BLE001  手编坏 yml 不该炸掉整条抓取链
        log.warning(f"额外源 yml 解析失败，忽略: {p}: {e}")
        return []


def load_feeds() -> List[Dict[str, str]]:
    """默认源 + 用户级额外源，按 url 去重（默认源优先）。抓取方统一用这个。"""
    feeds = list(load_default_feeds())
    seen = {f.get("url") for f in feeds}
    for f in load_extra_feeds():
        if f.get("url") not in seen:
            feeds.append(f)
            seen.add(f.get("url"))
    return feeds


def _write_extra_feeds(feeds: List[Dict[str, str]]) -> None:
    """调用方须持 _feeds_lock()：读-改-写整段串行，否则并发 add/remove 互相覆盖。"""
    _atomic_write_text(_extra_yml(),
                       yaml.safe_dump({"feeds": feeds}, allow_unicode=True, sort_keys=False))


def _feeds_lock():
    # fcntl 排它锁，跨线程（MCP 工具在 worker 线程并发跑）+ 跨进程（CLI 与 MCP 同 INVEST_HOME）
    return _file_lock(_extra_yml())


def add_extra_feed(name: str, url: str) -> Dict[str, object]:
    """加一个用户级 RSS/Atom 源。返回 {feed, probe_items}；不合法抛 ValueError。

    守护（顾问模式下这是暴露给群聊的写入口）：
    - name 规整为 [a-z0-9_]（与默认清单同一约定）
    - url 必须 http(s)、不带 userinfo、主机只解析到公网地址（_check_url）
    - live probe 能解析出至少 1 条 entry（挡"随手贴个网页"）
    - 上限 MAX_EXTRA_FEEDS；url 与默认/已有源重复 = 幂等返回已有条目
    """
    name = re.sub(r"[^a-z0-9_]", "_", (name or "").strip().lower()).strip("_")
    url = (url or "").strip()
    if not name:
        raise ValueError("name 不能为空（规整后仅剩 [a-z0-9_]）")

    def _existing_or_check(extras):
        for f in load_default_feeds() + extras:
            if f.get("url") == url:
                return {"feed": f, "probe_items": None, "already_exists": True}
        if any(f.get("name") == name for f in load_default_feeds() + extras):
            raise ValueError(f"源名 {name!r} 已被占用，换一个 name")
        if len(extras) >= MAX_EXTRA_FEEDS:
            raise ValueError(f"额外源已达上限 {MAX_EXTRA_FEEDS} 个，先 remove 再 add")
        return None

    hit = _existing_or_check(load_extra_feeds())
    if hit:
        return hit

    # probe 是网络 IO，不持锁（最长 ~十几秒，别卡住其他 add/remove）
    _check_url(url)
    probe = fetch_rss(name, url, max_items=3)
    if not probe:
        raise ValueError(f"probe 失败：{url} 解析不出任何 RSS/Atom entry"
                         "（不是 feed 或暂时抓不到；额外源直连、不经 HTTP(S)_PROXY）")

    feed = {"name": name, "url": url}
    with _feeds_lock():
        # probe 期间清单可能被别的调用改过：锁内重读、重校验（重复/占名/上限）再写
        extras = load_extra_feeds()
        hit = _existing_or_check(extras)
        if hit:
            return hit
        _write_extra_feeds(extras + [feed])
    return {"feed": feed, "probe_items": len(probe), "already_exists": False}


def remove_extra_feed(key: str) -> bool:
    """按 name 或 url 删一个额外源（只动用户级清单，默认源不可删）。"""
    key = (key or "").strip()
    with _feeds_lock():
        extras = load_extra_feeds()
        kept = [f for f in extras if f.get("name") != key and f.get("url") != key]
        if len(kept) == len(extras):
            return False
        _write_extra_feeds(kept)
    return True


def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "..."


def _strip_html(s: str) -> str:
    """粗暴去 HTML tag —— RSS summary 经常带 <p>/<a>"""
    import re
    return re.sub(r"<[^>]+>", " ", s or "").strip()
