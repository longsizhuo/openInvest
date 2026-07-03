---
type: wiki-chapter
title: 19 — 埋点与数据收集：匿名安装统计 / Opt-out / 隐私承诺
tags: [telemetry, analytics, privacy, umami, opt-out]
intent: 埋点与隐私
documents:
  endpoints: []
  config_keys:
    - OPENINVEST_NO_TELEMETRY
    - INVEST_NO_TELEMETRY
    - DO_NOT_TRACK
  symbols: []
---

# 19 — 埋点与数据收集 (Telemetry & Analytics)

为了了解 openInvest 的真实安装量，同时严格遵守用户隐私和自托管的设计理念，安装脚本引入了完全匿名的一次性埋点上报。数据上报至自托管的 Umami 服务。

---

## 📡 安装埋点 (skills/install.sh)

### 触发时机
用户**首次**运行 `skills/install.sh` 时触发一次。上报成功与否都会在 `$CLAUDE_SKILLS_DIR/.openinvest-install-reported` 写一个 marker 文件，之后的重跑/同步不再上报——统计口径是"安装台数"，不是"同步次数"。

### 上报方式
通过 `curl` 后台发送匿名 POST（不阻塞安装流程，断网/超时也不影响安装结果），不引入任何第三方包。脚本会在发送前打印一行提示，明示本次上报及关闭方式。

> 实现细节：User-Agent 必须是 `Mozilla/5.0 (compatible; openInvest-install/<version>)` 这种浏览器样式——Umami 的 `/api/send` 默认用 isbot 过滤非浏览器 UA（返回 200 但静默丢弃）。改 UA 前先用 isbot 验证，否则统计会静默归零。

### 收集的数据字段 (Payload)
* **事件名称 (Event)**: `install`
* **版本号 (Version)**: 来自仓库根目录 `pyproject.toml` 的 `[project].version`（release-please 维护，如 `0.15.0`）。
* **操作系统 (OS)**: `uname -s` 的输出（如 `Linux` / `Darwin`）。
* **主机名 (Hostname)**: 固定为 `openinvest-install`，是 Umami 归类用的合成值，不是用户机器的真实主机名。

### Opt-out (如何禁用)
以下环境变量**任意一个非空**即完全跳过上报：

* `OPENINVEST_NO_TELEMETRY` — 本项目专用开关
* `INVEST_NO_TELEMETRY` — 与仓库 `INVEST_*` 环境变量前缀一致的别名
* `DO_NOT_TRACK` — 业界通用约定（[consoledonottrack.com](https://consoledonottrack.com)）

```bash
OPENINVEST_NO_TELEMETRY=1 bash skills/install.sh
```

README 的安装命令旁也有同样的披露与关闭说明。

---

## 🌐 官网埋点 (openinvest-site)

官网（`openinvest.involutionhell.com` / `openinvest.dev`）接入了同一 Umami 实例，采集复制/浏览等交互事件。官网是独立仓库、独立部署（Cloudflare Pages），其埋点事件的定义与字段以 **openinvest-site 仓库自己的文档**为准，本页不重复维护，避免跨仓漂移。

---

## 🔒 隐私保障承诺
1. **完全匿名**:
   * 埋点 Payload 中**不包含**任何 IP 地址或个人标识数据（PII）。
   * 网络层请求会自动携带源 IP，但自托管的 Umami 服务已配置为不存储真实 IP。
   * User-Agent 是固定的合成值 `Mozilla/5.0 (compatible; openInvest-install/<version>)`，不泄露真实设备的浏览器/系统指纹。
   * 不收集地理位置，不收集邮箱等可识别个人的信息。
2. **一次性 + 可关闭**: 每台机器只上报一次；三个 opt-out 环境变量任一非空即完全禁用。
3. **零业务数据上报**: openInvest 运行时的持仓明细、API Key、LLM 调用日志等**绝对不会**上传至任何第三方，所有敏感业务数据均保留在您的本地机器（`memory/` 目录下）。
