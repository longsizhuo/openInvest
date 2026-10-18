"""B6 后半：同一论点被多家转载 → brief 里只算 1 条，EVENT_STANCE 计数随之按"不同论点"算。

归并条件 = 桶相同（affected_symbols 集合 + stance + severity + 来源身份）且归一化完整 claim 相似度 ≥ 0.85；
event_store 的 event_id 是整句 sha256，措辞差一个词就是新 id，挡不住转载。
阈值 event.max_corroboration（默认 1，0 = 不归并）。不联网、不碰真库。

安全评审两条（证据压制 / 来源洗白）：立场或 severity 不同永不合并；agent 投喂的事件永不与爬虫事件合并。
"""
from datetime import datetime, timezone
from unittest.mock import patch

from openinvest.core.config import load_config, reset_config
from openinvest.core.runner import event_brief as eb
from openinvest.core.runner.event_format import format_event_brief
from openinvest.utils.sentiment import event_stance_line_for_symbol

CLAIM = "Fed signals pause after strong jobs data, gold slips as yields climb to a fresh high"


def _ev(claim, *, eid=None, ts="2026-10-10T10:00:00+00:00", stance="risk", syms=("GC=F",), src="reuters", **kw):
    return {
        "event_id": eid or claim[:12], "ts": ts, "stance": stance, "severity": "mid",
        "affected_symbols": list(syms), "one_line_claim": claim,
        "sources": [{"src_name": src}], **kw,
    }


def _reprints():
    """同一论点三家转载：只有标点 / 大小写 / 日期戳 / 句尾小尾巴不同（整句相似度 > 0.85，数字一致）"""
    return [
        _ev(CLAIM + ".", eid="a", ts="2026-10-10T12:00:00+00:00", src="reuters"),
        _ev(CLAIM + " (2026-10-10)", eid="b", ts="2026-10-10T11:00:00+00:00", src="bloomberg"),
        _ev(CLAIM.upper() + ", analysts say", eid="c", ts="2026-10-10T10:00:00+00:00", src="ft"),
    ]


def test_default_is_one_per_thesis():
    assert load_config().event.max_corroboration == 1


def test_reprints_collapse_to_one_line_with_count():
    out = format_event_brief(_reprints())
    assert out.count("[risk/mid]") == 1
    assert out.endswith("(×3 来源)") and "2026-10-10T12:00:00" in out  # 留最新（输入最新在前），标在 claim 行末


def test_stance_counts_distinct_theses_not_reports():
    """0/1：不归并时 3 条转载算 risk=3；归并后 risk=1。"""
    line_off = event_stance_line_for_symbol(format_event_brief(_reprints(), max_corroboration=0), "GC=F")
    line_on = event_stance_line_for_symbol(format_event_brief(_reprints()), "GC=F")
    assert "risk=3" in line_off
    assert "risk=1" in line_on and "risk=3" not in line_on


def test_different_theses_or_symbol_sets_are_not_merged():
    events = [
        _ev(CLAIM, eid="a"),
        _ev(CLAIM, eid="b", syms=("GC=F", "^TNX")),            # 同 claim、不同标的集合 → 另一条
        _ev("Kuaishou picks banks for Hong Kong listing of its AI video unit", eid="c"),
    ]
    out = format_event_brief(events)
    assert out.count("[risk/mid]") == 3 and "来源)" not in out


def test_symbol_set_order_and_case_do_not_matter():
    out = format_event_brief([
        _ev(CLAIM, eid="a", syms=("GC=F", "^tnx")),
        _ev(CLAIM + "!", eid="b", syms=("^TNX", "gc=f")),
    ])
    assert out.count("[risk/mid]") == 1 and "(×2 来源)" in out


def test_cap_threshold_and_off_switch(monkeypatch):
    out2 = format_event_brief(_reprints(), max_corroboration=2)
    assert out2.count("[risk/mid]") == 2 and "(×3 来源)" in out2
    assert format_event_brief(_reprints(), max_corroboration=3).count("[risk/mid]") == 3
    assert "来源)" not in format_event_brief(_reprints(), max_corroboration=3)  # 没折叠就不标
    # config / env 路径：默认读 config，0 = 不归并
    monkeypatch.setenv("INVEST_EVENT_MAX_CORROBORATION", "0")
    reset_config()
    assert format_event_brief(_reprints()).count("[risk/mid]") == 3


def test_empty_claim_is_never_merged():
    out = format_event_brief([_ev("", eid="a"), _ev("  ...  ", eid="b")])
    assert out.count("[risk/mid]") == 2


def test_multi_merges_across_symbols_and_dedups_same_event_id():
    """合并发生在跨 symbol 之后：A 召回 a，B 召回 b（同论点）+ a（同 event_id，supersedes 行不同）
    → 1 条。旧版按段落文本去重，supersedes 行不同的同一事件会被列两遍。"""
    a_in_A = _ev(CLAIM, eid="a", ts="2026-10-10T12:00:00+00:00")
    a_in_B = _ev(CLAIM, eid="a", ts="2026-10-10T12:00:00+00:00", supersedes="old1")
    b_in_B = _ev(CLAIM + ", analysts say", eid="b", ts="2026-10-10T09:00:00+00:00")

    class Store:
        vec_loaded = False

        def recall(self, symbol, **kw):
            return {"A.X": [a_in_A], "B.X": [a_in_B, b_in_B]}[symbol]

    with patch.object(eb, "_get_event_store", return_value=Store()):
        out = eb.resolve_event_brief_multi(
            ["A.X", "B.X"], as_of=datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert out.count("[risk/mid]") == 1 and "(×2 来源)" in out


def test_opposite_stance_or_severity_never_merged():
    """证据压制：措辞几乎一样但立场相反的两条是两条证据，不能折成一条（否则一边被抹掉）。"""
    events = [
        _ev(CLAIM, eid="a", stance="risk"),
        _ev(CLAIM + ".", eid="b", stance="opportunity"),
        _ev(CLAIM + "!", eid="c", stance="risk", severity="high"),
    ]
    out = format_event_brief(events)
    assert out.count("[risk/mid]") == 1 and out.count("[opportunity/mid]") == 1 and out.count("[risk/high]") == 1
    assert "来源)" not in out
    line = event_stance_line_for_symbol(out, "GC=F")
    assert "risk=2" in line and "opportunity=1" in line


def test_same_opening_different_tail_is_not_a_reprint():
    """旧版只比前 60 字：开头相同、结尾说的是两件事的两句会被误并。现在比整句相似度。"""
    head = "Fed raises rates by 25bp at the October meeting, the first hike since the 2023 cycle ended"
    a = _ev(head + ", and markets now price one more hike before year end", eid="a")
    b = _ev(head + " but the statement signals a pause through the winter and a debate on balance sheet runoff", eid="b")
    assert format_event_brief([a, b]).count("[risk/mid]") == 2


def test_agent_event_neither_absorbs_crawler_events_nor_counts_in_n():
    """来源洗白：agent 投喂的事件不能被当成爬虫论点的一份"×N 来源"，也不能吞掉爬虫事件。"""
    crawler = [_ev(CLAIM, eid="a"), _ev(CLAIM + ".", eid="b")]
    agent = _ev(CLAIM + "!", eid="c", ingested_by="hermes-sentinel")
    out = format_event_brief([agent, *crawler])           # agent 排最前：不能因为先到就吸收后面两条
    assert out.count("[risk/mid]") == 2
    blocks = out.split("\n\n")
    agent_block = next(b for b in blocks if "[agent:hermes-sentinel]" in b)
    crawler_block = next(b for b in blocks if "[agent:" not in b)
    assert "来源)" not in agent_block                     # agent 行不带 ×N，没吃掉爬虫的报道
    assert crawler_block.endswith("(×2 来源)")            # 爬虫的 N 只数爬虫事件，不含 agent 那条
    # 反过来：爬虫排最前也一样
    out2 = format_event_brief([*crawler, agent])
    assert out2.count("[risk/mid]") == 2 and "(×2 来源)" in out2 and "(×3" not in out2
    # EVENT_STANCE 把 agent 事件和爬虫论点各算 1 条
    assert "risk=2" in event_stance_line_for_symbol(out, "GC=F")


def test_agent_reprints_merge_only_within_same_agent_and_keep_tag():
    events = [
        _ev(CLAIM, eid="a", ingested_by="hermes-sentinel"),
        _ev(CLAIM + ".", eid="b", ingested_by="hermes-sentinel"),
        _ev(CLAIM + "!", eid="c", ingested_by="other-agent"),
    ]
    out = format_event_brief(events)
    assert out.count("[risk/mid]") == 2
    assert "[agent:hermes-sentinel]" in out and "[agent:other-agent]" in out
    assert out.count("(×2 来源)") == 1


def test_claims_differing_in_a_number_are_different_facts():
    """几乎一样的句子只差一个数字（加息 25bp vs 50bp）是两个事实，不是转载。"""
    a = _ev("Fed raises rates by 25bp at the October meeting and signals the path ahead is data dependent", eid="a")
    b = _ev("Fed raises rates by 50bp at the October meeting and signals the path ahead is data dependent", eid="b")
    assert format_event_brief([a, b]).count("[risk/mid]") == 2


def test_swapped_numbers_are_different_facts():
    """数字相同但顺序相反（CPI 2.5% vs 预期 2.6% / CPI 2.6% vs 预期 2.5%）：一个低于预期、一个高于预期。
    小数不被拆成 2 和 5，数字也不排序后再比。"""
    a = _ev("US September CPI rose 2.5% year on year versus 2.6% expected, easing pressure on the Fed before November", eid="a")
    b = _ev("US September CPI rose 2.6% year on year versus 2.5% expected, easing pressure on the Fed before November", eid="b")
    assert format_event_brief([a, b]).count("[risk/mid]") == 2
    # 对照：数字一致、只是标点不同 → 仍是转载
    c = _ev("US September CPI rose 2.5% year on year versus 2.6% expected, easing pressure on the Fed before November.", eid="c")
    assert format_event_brief([a, c]).count("[risk/mid]") == 1


def test_keeps_newest_report_even_when_it_comes_later_in_the_input():
    """跨 symbol 合并后输入不是全局最新在前：保留并标 ×N 的必须是 ts 最新的那条。"""
    older = _ev(CLAIM, eid="a", ts="2026-10-10T09:00:00+00:00", src="reuters")
    newer = _ev(CLAIM + ".", eid="b", ts="2026-10-10T12:00:00+08:00", src="ft")   # = 04:00Z 比 09:00Z 旧
    newest = _ev(CLAIM + "!", eid="c", ts="2026-10-10T13:00:00Z", src="bloomberg")
    out = format_event_brief([older, newer, newest])
    assert out.count("[risk/mid]") == 1 and out.endswith("(×3 来源)")
    assert "2026-10-10T13:00:00Z" in out and "(sources: bloomberg)" in out
    # ts 解析不了的排最后：不影响有 ts 的那条被留下
    junk = _ev(CLAIM + "?", eid="d", ts="not-a-time")
    out2 = format_event_brief([junk, older])
    assert "2026-10-10T09:00:00" in out2 and "not-a-time" not in out2
