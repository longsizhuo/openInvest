---
type: report
title: "Issue #191 跨机器原生 MCP 客户端（Codex CLI）实测报告"
tags: [mcp, remote-mcp, codex-cli, testing, issue-191]
intent: 社区贡献者用原生 MCP 客户端跨机器实测 Remote MCP 的记录
documents:
  endpoints: []
  config_keys: []
  symbols: []
---

# Issue #191：跨机器原生 MCP 实测报告

测试日期：2026-10-09（调用时间统一使用 UTC）
对应 Issue：[Remote MCP (streamable-HTTP) is BETA and needs real-world testing #191](https://github.com/longsizhuo/openInvest/issues/191)
验收重点：[维护者评论](https://github.com/longsizhuo/openInvest/issues/191#issuecomment-6066387480)

## 1. 结论

本次完成了 M5 Pro 客户端连接 M1 Pro 服务端的原生 Codex CLI MCP 测试。最终确认：

- 原生客户端成功发现 21 个 openinvest 工具。
- `status`、`strategy` 成功，测试账户现金 CNY 20,000、空持仓、策略跟踪 `GC=F` 均核对通过。
- 真实 `run_committee(symbol="GC=F", force=true, max_rounds=1)` 成功返回裁决，`isError=false`、`cached=false`，耗时 20.923 秒，无超时或断连。
- 退出客户端后重新连接，两个只读工具仍可正常调用。
- 两个独立 Codex CLI 客户端分别查询同一服务均成功；没有调用起止时间证明请求重叠，不能据此宣称严格并发测试通过。

**维护者关注的“一分钟以上真实委员会调用是否超时或断连”尚未验证。** 本次成功调用仅持续约 21 秒。当前链路直接访问 Tailscale 地址，未经过反向代理、公网 HTTPS 或 Cloudflare Access。

原生客户端测试最终使用 `--no-daemon`，并为服务端地址设置 `NO_PROXY` / `no_proxy`。共享后台进程模式此前持续初始化失败；独立进程模式成功，但尚未定位后台进程模式失败的底层原因。

## 2. 证据范围

本报告依据本轮测试中的工具输出、测试者粘贴的终端日志和原生 Codex CLI 调用结果整理。

- **直接执行验证**：本机配置及日志检查、远端健康检查、Python MCP SDK 基础调用。
- **测试者提供的原生客户端证据**：M5 Pro CLI 截图、工具调用记录、时间和返回字段；这些耗时按测试者粘贴结果记录，未另行从服务端访问日志独立复算。
- **同批记录**：同一批提交的 [Remote MCP 测试记录（基于未合入的本地修改）](issue-191-remote-mcp.md) 中的自动化测试、nginx 和 130 秒模拟任务，不是本轮重新执行的测试，单独说明，避免混用。

未在本报告保存 API Key、MCP Bearer token、截图背景中的 Secret 或完整账户分析内容。

## 3. 环境

| 项目 | 本次环境 / 已知信息 |
|---|---|
| 客户端机器 | M5 Pro，macOS |
| 原生客户端 | Codex CLI 0.162.0（截图可见） |
| 服务端机器 | M1 Pro，macOS |
| 服务启动命令 | `INVEST_HOME=<TEST_DATA_DIR> INVEST_MCP_HOST=0.0.0.0 INVEST_API_TOKEN=<token> uv run openinvest-mcp --http` |
| 传输 | Streamable HTTP，`/mcp` |
| 网络 | 两台机器通过 Tailscale 地址连接 |
| 服务监听 | `0.0.0.0:8766`，启用应用级 Bearer token |
| 反向代理 / Cloudflare Access | 本次直连测试未经过 |
| 服务端 MCP 协议实现版本 | initialize 返回 `serverInfo.version="1.28.1"`；这不是 openinvest 项目版本 |
| 测试数据目录 | M1 Pro 上的独立测试目录，以 `INVEST_HOME` 指定 |
| 测试账户 | `Issue 191 Test`，虚拟数据 |
| 最终数据 | CNY 20,000、空持仓、跟踪 `GC=F` |
| 策略 | 股票 70%、现金 30%；`GC=F` 单次投入上限 CNY 1,000 |
| 本地代码参考 | M5 Pro 工作区 HEAD `b45409c`，项目版本 0.39.0，存在未提交修改 |
| 服务端代码精确版本 | 本次未独立采集服务端 commit / diff，不能将本机版本直接当作远端运行版本 |

## 4. 测试过程与问题处理

### 4.1 最初未加载原生工具：客户端缺少 token

最初请求原生 `status`、`strategy` 时，会话工具目录没有 openinvest，两项均未实际调用。

Codex 日志明确返回：

```text
Environment variable INVEST_API_TOKEN for MCP server 'openinvest' is not set
```

配置使用 `bearer_token_env_var = "INVEST_API_TOKEN"`，但客户端启动环境没有对应值。随后配置客户端 token，后续检查确认进程环境已有 token，且无首尾空白、CR/LF、控制字符或非 ASCII 字符。

只读网络与鉴权检查：

| 检查 | 结果 |
|---|---|
| 服务 `/health` | HTTP 200，`{"status":"ok"}` |
| 不带 token 的 `/mcp` initialize | HTTP 401，提示需要 Bearer token |
| 带正确 token 的 `/mcp` initialize | HTTP 200，返回协议及服务器信息 |

### 4.2 首次 SDK 查询：缺少初始化数据

原生工具仍不可用时，通过官方 Python MCP SDK 做了独立诊断。它不计入原生 Codex CLI 验收。

| 调用 | 结果 |
|---|---|
| initialize | 成功 |
| `status` | 失败：`memory/user.md / strategy.md / portfolio.md 缺失。首次使用请跑 openinvest init 初始化` |
| `strategy` | 成功 |

在 M1 Pro 检查发现：

- 服务工作目录为 M1 Pro 上的项目仓库目录。
- 服务进程环境未发现 `INVEST_HOME`，仓库 `.env` 未配置此项。
- 仓库 `memory` 下上述三个文件均缺失。
- 测试者确认从未运行过初始化。

随后在独立测试目录执行 `openinvest init --from-stdin`，提供虚拟 CNY 20,000、空持仓及测试策略。返回：

```text
status: ok
completion: completed_partial
memory_initialized: true
migrate_returncode: 0
```

三个数据文件已生成。`completed_partial` 对应尚未配置 LLM / 邮件凭据，不能等同于基础文件初始化失败。

服务重新启动，显式指定测试 `INVEST_HOME`。旧 PID 已退出时出现的 `kill: no such process` 没有阻止后续服务成功启动。

SDK 重测结果：

| 项目 | 结果 | 耗时 |
|---|---|---:|
| initialize / tools/list | 成功，21 个工具 | 未记录 |
| `status` | 成功 | 4.18 秒 |
| `strategy` | 成功 | 0.06 秒 |

这次 SDK 重测仅报告调用成功，没有核对现金字段；余额正确性由后面的原生调用及数据迁移验证。

### 4.3 原生 Codex 连接失败与独立进程模式

Codex 桌面会话及默认 CLI 模式反复显示：

```text
openinvest: failed (0 tools)
```

最新日志由缺少 token 转为 initialize 请求发送失败：

```text
handshaking with MCP server failed
error sending request for url (.../mcp), when send initialize request
has_authorization_header=true
```

客户端日志另记录 `error_is_timeout=false`、`error_is_connect=false`，但未提供足以确定根因的底层错误。`/mcp verbose` 也仅显示失败状态及 Bearer token 鉴权方式。

排查顺序：

1. 在 M5 Pro 普通终端执行绕过代理的健康检查，返回 200。
2. 使用同一 token 发送 initialize，返回 200。
3. 在同一终端重新启动默认 Codex CLI，仍失败。
4. 增加目标地址的 `NO_PROXY` / `no_proxy`，仍失败。
5. 保留上述环境并增加 `--no-daemon`，成功显示 `connected (21 tools)`。

CLI 日志显示默认模式连接共享 app-server；本机 `codex --help` 明确支持 `--no-daemon`。这些结果支持将故障范围缩小到客户端运行模式 / 环境差异，但不证明是某个具体代理、权限或 Codex 实现缺陷。

后续长调用测试采用如下启动方式（地址以占位符表示）：

```zsh
NO_PROXY="${NO_PROXY:+$NO_PROXY,}<M1_PRO_TAILSCALE_IP>" \
no_proxy="${no_proxy:+$no_proxy,}<M1_PRO_TAILSCALE_IP>" \
codex --no-daemon -c 'mcp_servers.openinvest.tool_timeout_sec=600'
```

token 由启动终端环境提供。这里的 600 秒是客户端工具超时设置，不代表实际完成了 600 秒测试。

### 4.4 原生只读调用通过，但发现旧数据格式导致余额为 0

首次原生 `status`、`strategy` 都返回 `isError=false`，但：

```text
cash.cny = 0
all_holdings = []
```

代码检查发现，之前使用的 `current_assets.cash_cny` 初始化路径经 `migrate_profile` 写入旧版扁平字段 `cash_cny`；当前 `PortfolioManager` 只读取新版 `cash["CNY"]`，缺失时返回 0。初始化操作提示成功，并不保证该路径的数据已转换为当前持仓 schema。

在 M1 Pro 对测试目录执行仓库提供的迁移：

```zsh
cd "<REPO_DIR>"
INVEST_HOME="<TEST_DATA_DIR>" \
uv run python -m scripts.migrate_portfolio_to_holdings
```

返回：

```text
status: migrated
backup: <TEST_DATA_DIR>/memory/portfolio.md.bak.<TIMESTAMP>
cash_currencies: ['CNY']
holdings_count: 0
holdings: []
```

随后原生 `status` 核对通过：`cash.cny=20000`、`all_holdings=[]`。没有通过额外入金掩盖数据格式问题。

这属于初始化 / 数据兼容问题，应与 MCP 传输结果区分记录。

### 4.5 真实委员会测试的前置配置

测试目录最初没有 `LLM_API_KEY` 或 `DEEPSEEK_API_KEY`。测试者通过隐藏输入将 DeepSeek Key 保存到测试目录 `.env`，随后重启服务加载配置。完整密钥未出现在任何测试记录中。

第一次委员会尝试因 `GC=F` 未配置在 `strategy.target_assets` 中失败。随后通过原生 MCP 配置：

```text
track_asset(
  symbol="GC=F",
  max_single_invest_cny=1000,
  display_name="黄金委员会测试"
)
```

原生 `strategy` 确认跟踪项存在。测试者粘贴的记录显示该项被幂等更新；没有重复添加。该操作修改测试策略，不执行买入，也不改变现金或持仓。

### 4.6 三次真实委员会调用记录

每次均为测试者明确发起的独立测试，参数相同：

```text
run_committee(symbol="GC=F", force=true, max_rounds=1)
```

每次宿主只调用一次，没有自动重试。后续人工再次测试不应被合并描述成“整个过程仅调用一次”。

| 次数 | 开始时间 UTC | 结束时间 UTC | 耗时 | isError | cached | 结果 |
|---|---|---|---:|---|---|---|
| 1 | 02:43:09.964 | 02:43:32.087 | 22.123 秒 | true | 未返回 | `asset GC=F not in strategy.target_assets` |
| 2 | 02:56:56.855 | 02:57:39.407 | 42.552 秒 | false | false | LLM 返回 402 `Insufficient Balance`；业务结果 `WORKER_UNAVAILABLE` / `UNCLEAR` |
| 3 | 02:59:44.561 | 03:00:05.484 | 20.923 秒 | false | false | 正常返回裁决，`decision_id="2026-10-09/GC=F"` |

以上时间均为 2026-10-09 UTC。

- 三次均未报告超时或断连。
- 三次均未观察到 MCP 进度通知。没有采集客户端是否发送 `progressToken` 的证据，不能据此断定服务端进度功能失效。
  维护者补充：main 的 HTTP 传输是 `json_response=True` 的纯 JSON 响应，没有可推送通知的流，该模式下进度通知**从不发送**；若服务端运行的是 main 代码，未观察到进度属预期行为。服务端是否运行了同批记录中的本地 SSE 修改，见第 3 节环境表“服务端代码精确版本”一项（未采集）。
- 第二次虽然 `isError=false`，但业务分析失败；不能作为委员会成功证据。返回内容包含后端内部 `retry_exhausted`，与宿主未自动重试并不矛盾。
- 第三次确认真实分析成功且未命中缓存，但只持续约 21 秒。
- 两次后续尝试之间的余额 / 凭据调整细节未提供，报告不推断具体充值或换 Key 操作。
- 宿主未调用买卖、入金等工具。委员会本身会产生决策记录 / 分析产物，因此不能将委员会调用描述成完全不写数据。

### 4.7 原生客户端重连

保持 M1 Pro 服务运行，退出 M5 Pro CLI，再使用独立进程命令启动客户端。

重连后原生调用结果：

- `status`：`isError=false`，`cash.cny=20000`、`all_holdings=[]`。
- `strategy`：`isError=false`，`target_assets` 包含 `GC=F`。

本项证明正常退出后重新连接并查询成功，不等同于调用中断线恢复或长时间空闲后的恢复。

### 4.8 两个独立客户端

测试者分别提供两个 CLI 的原生只读调用结果：

| 客户端 | status | strategy | 超时 |
|---|---:|---:|---|
| A | 成功，1.477 秒 | 成功，0.176 秒 | 无 |
| B | 成功，1.475 秒 | 成功，0.046 秒 | 无 |

两边均未运行委员会、未修改数据。由于缺少绝对起止时间或服务端请求交叠证据，本项结论仅为“两个独立客户端分别调用同一端点成功”，不将其标记为严格并发请求验证。

## 5. 验收矩阵

| 场景 | 状态 | 说明 |
|---|---|---|
| 第二台物理机器连接 | 通过 | M5 Pro → M1 Pro |
| 原生客户端工具发现 | 通过 | Codex CLI 0.162.0，独立进程模式，21 个工具 |
| 原生 status / strategy | 通过 | 测试数据最终核对正确 |
| Bearer 鉴权 | 通过基础检查 | 无 token 401，正确 token initialize 200 |
| 无鉴权 health | 通过 | 200 |
| 真实委员会缓存未命中 | 通过 | 最终 `cached=false`，正常返回裁决 |
| 超过一分钟的真实委员会 | 未验证 | 成功调用仅 20.923 秒 |
| 正常退出后的客户端重连 | 通过 | 重新连接后查询正确 |
| 两个独立客户端分别使用 | 通过 | 两份只读调用结果均成功 |
| 严格并发请求 | 未确认 | 缺少时间重叠证据 |
| 长时间空闲后重用 | 未验证 | 未执行明确的空闲等待测试 |
| 调用中断线 / 恢复 | 未验证 | 不以正常重连替代 |
| 原生客户端进度通知 | 未确认 | 未观察到，未核对 progressToken；main 的 HTTP（json_response）模式本就不发进度 |
| 共享后台进程模式 | 未通过 | initialize 发送失败，根因未定位 |
| 反向代理 / Host 白名单 | 本轮未覆盖 | 本轮为直接连接 |
| Cloudflare 长调用、Access + Bearer | 未覆盖 | 本轮没有 Cloudflare |
| 全新 Linux systemd / Docker 部署 | 未覆盖 | 本轮为 macOS 手动启动 |

## 6. 与同批 Remote MCP 测试记录的关系

同一批提交的 [Remote MCP 测试记录（基于未合入的本地修改）](issue-191-remote-mcp.md) 记载了 nginx 反代下约 130 秒的模拟委员会任务、进度及第二客户端查询，以及自动化测试结果。

这些结果可以补充说明传输层的测试覆盖，但：

- 本轮没有重新执行这些测试。
- 那批结果（SSE 进度、130 秒任务、自动化测试）针对的是未合入 main 的本地分支，不代表 main 行为。
- 130 秒任务替换了委员会业务服务，不是一次真实 LLM 分析。
- 该记录同样明确未经过 Cloudflare / Access。
- 该记录的自动化测试数量不能作为本轮重新跑过测试套件的证明。

## 7. 尚待补测与建议

1. **真实调用超过一分钟**：当前最主要缺项。需要一次实际持续超过 60 秒的未命中缓存调用，记录起止时间、业务结果、进度和连接表现。不能将 21 秒成功调用或 130 秒模拟调用写成此项通过。
2. **严格并发**：记录两个原生客户端调用的绝对时间，或采集可证明请求重叠的服务端日志。
3. **Cloudflare / 反代链路**：若继续覆盖 Issue 的其他场景，单独记录代理、HTTPS、Access 与 Bearer 组合；本次直连结果不能外推到这些部署。
4. **客户端共享进程问题**：保留默认模式失败、独立模式成功的差异，后续调查具体路由 / 环境 / 运行时原因。
5. **初始化数据格式问题**：将 `current_assets` 初始化后产生旧 schema、查询现金为 0 的行为单独记录为缺陷；本次仅使用现有迁移工具处理测试数据，没有在本轮修复初始化实现。
6. **版本复现信息**：提交报告时补充 M1 Pro 的 commit、工作区差异及依赖版本，明确是否运行修改后的代码。

本报告仅记录测试结果，不代表 Issue #191 的全部场景已验收，也不建议仅据此移除 BETA 标记。
