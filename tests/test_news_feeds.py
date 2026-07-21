"""用户级新闻源清单（INVEST_HOME/rss_feeds.yml）—— add/remove/merge 契约。

这是顾问模式暴露给群聊的写入口（add_news_source），护栏必须有测试钉住：
probe 校验、name 规整、url 幂等、上限。
"""
from __future__ import annotations

import pytest
import yaml


@pytest.fixture
def home(tmp_path, monkeypatch):
    from openinvest import paths
    monkeypatch.setattr(paths, "INVEST_ROOT", tmp_path)
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
