# cooldown-demo：只给正反理由、不给结论的委员会演示

参赛用的公开演示（“投资冷静期”）：用户说出想买或想卖的东西，委员会照常辩论，
但对外**只给支持的理由 / 反对的理由 / 分歧最大的地方**，不给买卖结论、金额、置信度。
未经许可向公众提供金融领域的确定性结论有合规风险，比赛规则也明令禁止。

**生产代码零改动**，这里只是外面接的一层：

1. `run_committee_session` 跑完整委员会（顾问模式：不读真实持仓、不落盘）。CIO 照跑，
   它的裁决在进程内直接丢弃——代价是每次多一次 LLM 调用，换来的是不用在
   MCP / CLI / web_api 各处加闸。
2. 书记员（`scripts/scribe.md`）只读三个角色的辩论和确定性事实（`to_cio_brief()`，
   不含 CIO memo），整理成三段 Markdown，末尾由代码固定追加免责声明。
3. 逐字闸 `find_verdict_language`：裁决词、“建议/应该/适合 + 买卖加减仓”句式、金额、仓位、
   目标价、支撑阻力位、概率数字。查两个视图——返回给用户的那一份（NFKC + 繁→简），
   以及模拟 Markdown 渲染后人读到的字串（去空白和所有 Markdown 语法、组合附加符，
   西里尔/希腊同形字折回拉丁）；HTML 标签、实体、格式控制字符直接算命中。命中就带着
   命中片段重写一次，再命中整份拦下（fail closed，不做局部删改）。
4. `debate_summary()` 的返回值按白名单构造：`{symbol, debate_summary}` 或 `{status, error}`。

## 跑

必须顾问模式 + 独立 `INVEST_HOME`（不在顾问模式直接拒绝：否则 CIO 裁决和路径快照会落进真实账本）：

```bash
INVEST_HOME=~/openinvest-demo INVEST_ADVISORY_MODE=1 \
    uv run python experiments/cooldown-demo/scripts/cooldown.py 510300.SS
```

**公开服务只能暴露 `debate_summary()`**，不能直接暴露 openinvest 的 MCP / web_api
（`run_committee` / `explain_decision` 等出口都带裁决）。

## 演示站

`scripts/server.py`（FastAPI）+ `scripts/index.html`：输入基金或股票代码 → 先查标的并给出纯计算的事实
（近两年净值 / 价格小图、最深跌幅、离高点多远，不调 LLM）→ 后台跑一次委员会 → 页面轮询拿书记员纪要。
6 位数字先当基金查（东方财富），查不到再按 A 股查（沪 6/9、深 0/2/3 开头）；带后缀的 `000001.SZ`、港股 `0700.HK`、
美股 `AAPL` 直接查股票（yfinance 行情，新浪取中文名）。一次查询额度覆盖全部候选。

```bash
INVEST_HOME=~/openinvest-demo INVEST_ADVISORY_MODE=1 \
    uv run uvicorn --app-dir experiments/cooldown-demo/scripts server:app --host 127.0.0.1 --port 8769
```

- 只绑 127.0.0.1，公网经反代进来。限频认 `X-Client-IP`，反代必须用自己算的客户端地址**覆盖**它
  （Caddy：`header_up X-Client-IP {client_ip}`；经 Cloudflare 时配 `trusted_proxies` 为 CF 网段），
  客户端自带的同名头会被覆盖，伪造不了。
- 辩论：每人每天 `COOLDOWN_PER_IP_DAILY`（默认 3）次，全站每天 `COOLDOWN_GLOBAL_DAILY`（默认 100）次。
  一次委员会约 ¥0.01（deepseek-v4-flash），封顶约 ¥1/天。查不到的代码不扣辩论次数、不调 LLM。
- 查基金（下载东方财富净值）另有额度：每人 30 次 / 全站 2000 次每天，在下载**之前**扣，查不到的也算；
  查到的同一只基金当天只下载一次；查不到的不缓存（东方财富超时和代码不存在一样返回空，缓存会让这只基金当天对所有人都"没找到"），2 分钟内同一代码直接回"没找到"（不重下、不扣额度，挡连点），之后重查再扣额度。
- 出错时页面只看到一句通用提示，后端报错只进服务端日志。
- 首页 `/` 302 跳到参赛登记的 QMuse 镜像（`COOLDOWN_HOME_REDIRECT`，默认 render.qmuse.pub/p/muse/7129971474537804）：
  那个页面带支付宝访问统计，自有域名的访问大赛看不到；镜像的 `/api/` 仍打到本服务。设成空串即回退为本站页面。
- 多空对照里点「先看今天的示例」展示 `COOLDOWN_SAMPLE_CODE`（默认 110011）当天的报告（`/api/sample`）：服务起来后和每天过零点后自动先跑好，
  每天一次委员会，失败当天最多重试 2 次，不占访客额度。
- 同一时刻只跑一个委员会，其余排队。任务和计数在进程内存，重启清零。
- 页面按行把纪要渲染成纯文本节点（标题 / 列表 / 段落），不解析 Markdown 或 HTML。
- 一次辩论实测约 20 秒（1 轮 cross-challenge）。

## 试玩：你 vs AI（首页）

首屏就是盲局的第一手：随机抽取标的与起点，显示开局前最多 63 个交易日的走势，右侧斜线雾区是还没发生的「下一周」；左边直接是「第 1 周，你押：空仓 / 半仓 / 满仓」，点了就开局。规则收进右上角「怎么玩」；多空对照是次入口，今日示例点了才加载。首屏和对局中不显示标的名、代码、真实日期、原始价格或 AI 的原始理由；价格以开局 = 100 归一化，结局再揭晓标的与日期。

题库为 2026 年 1–9 月的历史行情：黄金、纳指100、沪深300、腾讯控股，以及标普500、道琼斯工业 ETF、罗素2000 ETF、日本股市 ETF、德国股市 ETF、新兴市场 ETF、中证500 ETF、科创50 ETF，共 12 个标的。ETF 按对应 ETF 的价格回放，不冒充指数本体。

默认随机 12 周快局，也可以选择 24／36 周。先在有完整记录的标的中等概率抽一个，再等概率抽连续区间的起点，不按事后收益或输赢挑题；换一局时尽量不重复上一个标的。新增 8 个标的在 4–7 月有 AI 记录缺口，当前只参与 12 周局；24／36 周局用原有 4 个记录完整的标的。缺口不补成 HOLD，也不跨过缺口拼成一局。

玩家每周选空仓、半仓、满仓，
对手是委员会当周的回测决定（`memory/.backtest/<日>/<标的>.md`，deepseek-v4-flash，训练截止
2025-05-31，每个决定只喂当天之前的行情）。AI 规则：加仓 +50%、减仓 −50%、清仓归零、其余不动，
调仓收 0.1%；AI 的决定在玩家选完之后才揭晓。不跑 LLM：`build_game.py` 一次性生成
`data/game_data.json`，`/api/game` 原样下发，输赢在浏览器里算。

玩家只看匿名价格走势，AI 沿用当时包含标的和宏观信息的历史决策；双方输入不同，不能把输赢当作同等信息下的人机能力测评。匿名是界面玩法，不是防作弊协议：静态 JSON 仍含结局揭晓所需的名称、日期和后续行情。

- 停在 9 月底：最近几周的决定离"现在"太近，像当下建议（细则 4.9）；也不展示置信度。
- 纵轴只按"到这一周为止"的价格定刻度，免得把之后的高低点漏给玩家。
- `build_game.py` 校验价格为有限正数、日期与决策点有序、每个标的至少有连续 12 周真实决策；公开数据不导出原始理由。抽局后只保留该局背景、决策与最终结算日，不使用窗口以后的价格结算。
- 表情包在 `data/stickers/`（堆糖「永雏塔菲表情包」，pillow 重新编码去元数据），`/api/sticker/{name}` 只认目录里真实存在的文件名：首屏图块、每周输赢反应、结局各一张。
- 委员会全年几乎只给 ACCUMULATE / HOLD，所以 AI 基本是第一次加仓后满仓不动——这是真实
  回测结果，不调。

重新生成（缺回测的日子先用 `scripts.backtest_committee --holdout` / `--allow-lookahead` 补）：

```bash
INVEST_HOME=<生产 checkout> uv run python experiments/cooldown-demo/scripts/build_game.py
# 本机已有完整行情缓存时，不请求行情网络、不调用模型：
INVEST_HOME=<生产 checkout> uv run python experiments/cooldown-demo/scripts/build_game.py --cached
```

## 测试

不在 CI 里（CI 只跑 `tests/`），改代码前后手动跑：

```bash
uv run pytest experiments/cooldown-demo/scripts/ -q
```

闸的正例（含 13 种绕过写法）、反例（正常分析句不误伤）、重写一次、二次命中拦下、LLM
不可用拦下、非顾问模式拒绝、出口白名单 + 书记员看不到 CIO；演示站的代码校验、查不到不计次、
查基金额度在下载前扣且有缓存、每人/全站辩论限额、两种失败（抛异常 / 返回带后端报错）都不外泄。

## 实测（2026-10）

真实 DeepSeek 跑了 A 股 ETF 和黄金共 6 次（3 次走本目录的外壳，3 次是早期写进委员会内部的
同一套逻辑）：三段结构完整、带免责声明、无落盘；最后两次确认返回原文、中文标点不被改成半角。
场外基金要等 `FUND:` 历史行情（openInvest#349）合进去后再跑。

- 早期两次漏过支撑/阻力价位（“下方有 4.30 支撑参考”“4.30 被其视为下方支撑”“MA120 4.76 为上方阻力”），
  闸放宽成“同一分句里数字和支撑/阻力相距 12 字以内”，三句都进了用例。
- 放宽后的两次实跑：第一版都写了支撑价位、被闸拦下，带着命中片段重写后第二版通过——
  重写路径在真实 LLM 上走得通。Quant 几乎每次都谈支撑位，所以 prompt 里点名不转述，
  以减少重写（这条 prompt 改动之后还没实跑过）。

## 上限

- 关键词闸挡不住同义改写。要更稳：加一道 LLM 二审，或者演示前端按**纯文本**渲染
  （从根上消掉 Markdown 渲染差异）。
- 书记员 prompt 和闸只覆盖中文。
- 顾问模式下群友可加新闻源，新闻经 Macro 进书记员上下文——注入入口是现实的，闸就是为它设的。

## QMuse 版（比赛表单只收 render.qmuse.pub 链接）

`qmuse-app/` 是同一页面的 React 外壳：`python3 sync.py` 从 `scripts/index.html` 生成样式、标记和逻辑
（`/api/` 改写成 `${VITE_API_URL}/api/`，默认 `https://fund.longsizhuo.com`），页面不另行维护。
接口仍在本机演示服务，`server.py` 对 qmuse 域名开放 CORS（不带凭证，限额照旧按 X-Client-IP）。

```bash
cd experiments/cooldown-demo/qmuse-app
python3 sync.py && npm install && npm run build   # 本地验证
qmuse login && qmuse import . --title "多空对照"     # 首次导入建应用，之后同目录再 import 即更新
```
