import html
import os
import smtplib
import socket
import ssl
import time  # 用于重试间隔
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable, Optional
from urllib.parse import quote, urlsplit

import markdown
import nh3
from dotenv import load_dotenv
from markdown.extensions.tables import TableExtension

load_dotenv()


class EmailDeliveryError(RuntimeError):
    """邮件投递失败（重试耗尽）。

    在此之前 send_gmail_notification 重试 5 次后只 print 一行就 return，
    调用方（jobs/daily_report.py）拿不到任何信号，scheduler/runner 也无法
    记 status=failed —— 用户以为收到日报，实际上从未发出。改为重试耗尽
    时显式抛出本异常，让 scheduler runner 自动记入 job_runs 表，并允许
    上层做差异化处理（继续 job vs. 中断）。
    """


def _resolve_receiver(default_sender: str) -> str:
    """收件人优先级：memory/user.md 的 email 字段 → .env DIGEST_EMAIL_TO → 发件人自身"""
    try:
        from openinvest.core.memory_store import MemoryStore
        store = MemoryStore()
        user_doc = store.read("user")
        if user_doc:
            email = user_doc.get("email")
            if email:
                return str(email)
    except Exception:
        pass
    digest = os.getenv("DIGEST_EMAIL_TO")
    if digest:
        return digest
    return default_sender


# 邮件视觉设计（2026-06-20 重设计）：卡片式版式，主内容白底圆角卡，灰底衬托。
# 关键：分析师长文（CIO/Quant/Risk）走 .analyst 卡片（见 daily_report_builder），
# 用 md_in_html 扩展让卡片内部 markdown 仍被解析——粗体/换行正常，不再塞进灰色
# 代码块导致原文泄露。等宽数据块（黄金快照 / 摩擦成本表）才保留 <pre> monospace。
_DEFAULT_EMAIL_CSS = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, "PingFang SC", "Microsoft YaHei", sans-serif; line-height: 1.7; color: #2b2f36; background: #eef1f5; margin: 0; padding: 24px 12px; }
    .container { max-width: 720px; margin: 0 auto; background: #ffffff; border-radius: 12px; padding: 28px 34px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
    h1 { font-size: 23px; color: #1a2533; margin: 4px 0 22px; padding-bottom: 14px; border-bottom: 3px solid #2d7ff9; }
    h2 { font-size: 19px; color: #1a2533; margin: 30px 0 12px; padding-left: 12px; border-left: 4px solid #2d7ff9; }
    h3 { font-size: 16px; color: #0b7285; margin: 22px 0 10px; }
    p { margin: 0 0 13px; }
    strong { color: #1a2533; }
    a { color: #2d7ff9; text-decoration: none; }
    a:not([href]) { color: inherit; }
    blockquote { border-left: 4px solid #d7dee6; background: #f7f9fb; padding: 12px 18px; color: #52606d; margin: 18px 0; }
    ul, ol { margin: 0 0 14px; padding-left: 24px; }
    li { margin-bottom: 6px; }
    hr { border: 0; border-top: 1px solid #e4e9ee; margin: 26px 0; }
    table { border-collapse: collapse; width: 100%; margin: 18px 0; font-size: 14px; }
    th, td { border: 1px solid #e4e9ee; padding: 10px 12px; text-align: left; }
    th { background: #f4f6f8; color: #1a2533; font-weight: 600; }
    tr:nth-child(even) { background: #fafbfc; }
    /* 等宽数据块（对齐的快照/表格）才用 monospace */
    pre { background: #f7f9fb; border: 1px solid #e4e9ee; border-radius: 8px; padding: 14px 16px; overflow-x: auto; font-size: 13px; line-height: 1.5; }
    pre code { background: none; padding: 0; color: #2b2f36; }
    code { font-family: "SFMono-Regular", Consolas, Monaco, monospace; }
    :not(pre) > code { background: #eef1f5; padding: 2px 6px; border-radius: 4px; color: #c0392b; font-size: 13px; }
    /* 分析师卡片：LLM 长文走这里，正常排版而非灰色代码块 */
    .analyst { background: #f7f9fb; border: 1px solid #e7edf3; border-left: 4px solid #0b7285; border-radius: 8px; padding: 12px 18px; margin: 6px 0 18px; font-size: 14.5px; color: #3a444f; }
    .analyst p { margin: 0 0 9px; }
    .analyst p:last-child { margin-bottom: 0; }
    .footer { font-size: 12px; color: #9aa5b1; margin-top: 34px; padding-top: 18px; border-top: 1px solid #e4e9ee; text-align: center; }
    .highlight { background: #fff3cd; padding: 2px 5px; border-radius: 4px; }
"""


def md_text(text: object) -> str:
    r"""不可信纯文本（新闻标题/来源名/归一化后的事件 claim）插进邮件 markdown 前过一遍。

    python-markdown 原样透传 HTML（render_markdown_email 也靠这点渲染自家 <div>），所以
    外部文本里的 <img>/<a>/<style> 会直接进邮件，[x](url) / ![x](url) 会变成链接/图片。
    做 HTML 转义 + 反斜杠/方括号/反引号转义（先转反斜杠，否则外部文本自带的 \ 会和新加的
    凑成 \\ 把后面的 [ 放出来，\[x\](url) 照样成链接），其余 markdown 字符不动。
    """
    s = html.escape(str(text or ""), quote=False).replace("\\", "\\\\")
    return s.replace("[", r"\[").replace("]", r"\]").replace("`", r"\`")


def md_url(url: object) -> str:
    """不可信 URL 放进 markdown 链接的 (...) 前：只放行 http(s)，并编码掉能提前闭合括号、
    插入 HTML 或 title 的字符（空格 ( ) < > " [ ]）。其它 scheme（javascript:/data:…）返回
    空串，调用方据此只出文字不出链接。"""
    u = str(url or "").strip()
    try:
        scheme = urlsplit(u).scheme.lower()
    except ValueError:  # 畸形 URL（如未闭合的 [ IPv6 字面量）→ 只出文字
        return ""
    if scheme not in ("http", "https"):
        return ""
    return quote(u, safe=":/?#&=%.-_~+,;@!$*'")


# 渲染期 HTML 白名单 = 模板自己会产出的全部标签/属性，别的一律丢（标签去掉、文字保留；
# script/style 连内容一起删）。LLM 备忘 / 新闻转述 / 异常文本都会拼进 markdown，而
# python-markdown 原样透传 HTML——上游转义（md_text、日报 builder 与 /view 的 _defang）是
# 第一道，这里是兜底的那道，所有邮件和 /view 都经过这一个函数。图片一律不放：没有模板用 <img>。
# 'toc' 扩展已去掉：没人用标题锚点，而 [TOC] 会让正文自己长出一块导航。
_MD_EXTENSIONS = ["fenced_code", "nl2br", "sane_lists", "md_in_html",
                  TableExtension(use_align_attribute=True)]  # 对齐走 align 属性，免放行 style
_SAFE_TAGS = {"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "code",
              "pre", "blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th",
              "td", "a", "div"}
_ALIGN = {"align": {"left", "center", "right"}}


def md_to_safe_html(content_md: str, *, link_hosts: Iterable[str] = ()) -> str:
    """markdown → 白名单过滤后的 HTML 片段（不含外壳）。

    链接只在 host ∈ link_hosts 且 http(s) 时保留 href；其它链接退化成不可点的文字
    （nh3 去掉 href 留文字，不另外显示 URL——LLM 正文里本就没有合法链接）。只有事件
    预警会传 link_hosts（它的来源链接是模板自己拼的）。
    """
    hosts = {h.lower() for h in link_hosts if h}

    def _keep_href(tag: str, attr: str, value: str) -> str | None:
        if attr != "href":
            return value
        # 必须 fail-closed：回调一抛异常，nh3 就保留原值（链接照样可点）。
        # 反斜杠 / userinfo：浏览器和 urlsplit 对 host 的解读会不一致，直接不给链接。
        try:
            u = urlsplit(value)
            ok = (u.scheme.lower() in ("http", "https") and "\\" not in value
                  and u.username is None and (u.hostname or "") in hosts)
        except Exception:  # noqa: BLE001
            return None
        return value if ok else None

    # "<!" 先转义：模板从不产出注释/声明，而 python-markdown 对某些注释形输入会不收敛
    # （与版本、html.parser 实现有关），这里一刀切掉，不依赖库版本。
    content_md = content_md.replace("<!", "&lt;!")
    return nh3.clean(
        markdown.markdown(content_md, extensions=_MD_EXTENSIONS),
        tags=_SAFE_TAGS,
        attributes={"*": set(), "a": {"href"}, "ol": {"start"}},
        tag_attribute_values={"th": _ALIGN, "td": _ALIGN},
        allowed_classes={"div": {"analyst"}},
        url_schemes={"http", "https"},
        attribute_filter=_keep_href,
        strip_comments=True,
    )


def wrap_email_html(inner_html: str, *, footer_label: str, extra_css: str = "") -> str:
    """可信外壳（CSS + 容器 + footer）。inner_html 必须已经是安全的 HTML。"""
    return f"""
    <html>
    <head><meta charset="utf-8"><style>{_DEFAULT_EMAIL_CSS}{extra_css}</style></head>
    <body>
        <div class="container">
            {inner_html}
            <div class="footer">
                Generated by <b>{html.escape(footer_label)}</b> • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            </div>
        </div>
    </body>
    </html>
    """


def render_markdown_email(
    content_md: str, *, footer_label: str = "Invest Agent", link_hosts: Iterable[str] = (),
) -> str:
    """Markdown → 完整 HTML body（含 CSS + footer），给 send_email_html 用。

    md_in_html 扩展：让 <div class="analyst" markdown="1"> 等 HTML 块内部的 markdown
    仍被解析（粗体/列表/换行正常）。这替代了旧的"把分析师长文塞进 ``` 代码块"做法
    —— 那会让 LLM 原文（含 **粗体** 标记）以灰色等宽块原样泄露、难以阅读。
    正文过 md_to_safe_html 白名单，外壳在白名单之后才套上。
    """
    return wrap_email_html(
        md_to_safe_html(content_md, link_hosts=link_hosts), footer_label=footer_label,
    )


def send_email_html(
    *,
    subject: str,
    html_body: str,
    plain_body: str,
    receiver: Optional[str] = None,
    max_retries: int = 5,
) -> str:
    """SMTP 退避发邮件 —— event_notifier 和 daily_report 共用。

    receiver 不传则按 _resolve_receiver 优先级解析。
    成功返回 receiver；凭据缺失返回 ""；重试耗尽抛 EmailDeliveryError。
    """
    sender = os.getenv("EMAIL_SENDER")
    password = os.getenv("EMAIL_PASSWORD")
    if not sender or not password:
        print("⚠️ Email credentials not found in .env. Skipping email notification.")
        return ""
    receiver = receiver or _resolve_receiver(default_sender=sender)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = sender
    msg['To'] = receiver
    msg.attach(MIMEText(plain_body, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    retry_interval = 5  # 5s → 10s → 20s → 40s → 80s
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"🔄 [Attempt {attempt}/{max_retries}] 正在连接 SMTP 服务器...")
            # 注：smtplib.SMTP timeout=30 已 cover per-call，不动 socket.setdefaulttimeout
            context = ssl.create_default_context()
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                print(f"🔑 [Attempt {attempt}/{max_retries}] 正在验证身份...")
                server.login(sender, password)
                print(f"📨 [Attempt {attempt}/{max_retries}] 正在发送数据...")
                server.sendmail(sender, [receiver], msg.as_string())
            print(f"✅ Email successfully sent to {receiver}")
            return receiver
        except (socket.timeout, smtplib.SMTPException, ConnectionError, OSError) as e:
            last_exc = e
            print(f"❌ [Attempt {attempt}/{max_retries}] 发送失败: {e}")
            if attempt < max_retries:
                print(f"⏳ 等待 {retry_interval} 秒后进行下一次重试...")
                time.sleep(retry_interval)
                retry_interval *= 2

    print("⛔️ 已达到最大重试次数，放弃发送。")
    raise EmailDeliveryError(
        f"send_email_html 重试 {max_retries} 次后仍失败: "
        f"{type(last_exc).__name__}: {last_exc}"
    ) from last_exc


def send_gmail_notification(content: str) -> str:
    """通过 Gmail SMTP 发邮件（markdown 内容版本，给 daily_report 用）。

    成功：返回收件人地址。
    凭据缺失：返回空字符串（视为"故意 skip"，不算失败）。
    重试耗尽：抛 EmailDeliveryError —— 让 scheduler runner 记 job 状态为 failed。

    Receiver 优先级:
    1. memory/user.md 的 email 字段
    2. .env 的 DIGEST_EMAIL_TO
    3. 发件人本身
    """
    subject = f"Invest Agent Analysis Report - {datetime.now().strftime('%Y-%m-%d')}"
    html_body = render_markdown_email(content, footer_label="Gemini Invest Agent")
    return send_email_html(
        subject=subject,
        html_body=html_body,
        plain_body=content,
    )


if __name__ == "__main__":
    # Test call
    test_md = """
# Weekly Analysis
## Market Overview
The market is **bullish**.

- Apple: Up
- Google: Down

> "Buy low, sell high."
"""
    send_gmail_notification(test_md)
