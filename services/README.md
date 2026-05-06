# services/

外部服务集成（IO 边界）。所有需要"网络往外打"或"读邮件/外部 API"的代码都在这里，方便 mock 测试和限流。

## 内容

- `commsec_reader.py` — IMAP 拉 CommSec 成交回执邮件 + 解析 → 转成 trade dict（symbol/units/amount/email_id）
- `news.py` — 新闻搜索（DuckDuckGo via `ddgs` + 网页正文抽取 via `trafilatura`）；macro_strategist 用
- `notifier.py` — 邮件发送（Gmail SMTP）；daily_report 完成后给用户发 brief

## 与其他目录的关系

- 上游：`jobs/commsec_sync.py` 调 `commsec_reader.fetch_new_trades()`；`jobs/daily_report.py` 调 `notifier.send_brief()`
- 下游：原生第三方库（imaplib / smtplib / ddgs）
- 测试：`tests/test_commsec.py` mock IMAP 服务器
