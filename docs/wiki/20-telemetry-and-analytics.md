# Telemetry & Analytics (埋点与数据收集)

为了了解 openInvest 的真实安装量和用户交互情况，同时严格遵守用户隐私和自托管的设计理念，系统在两个环节引入了完全匿名的埋点上报。所有数据均上报至自托管的 Umami 服务。

---

## 📡 1. 安装环节埋点 (Backend & Skill Installation)

### 触发时机
当用户手动运行 `skills/install.sh` 脚本进行 openInvest Skill 安装/同步时触发。因为这是用户手动操作的入口，不依赖 AI Agent 交互。

### 上报方式
通过 `curl` 向 Umami 接口发送匿名 POST 请求，不生成任何本地文件，也不引入任何第三方包。

### 收集的数据字段 (Payload)
* **事件名称 (Event)**: `install`
* **版本号 (Version)**: openInvest 仓库根目录下的 `VERSION` 文件内容（如 `0.9.1`）。
* **操作系统 (OS)**: 执行安装的操作系统类型（由 `uname -s` 提供，如 `Linux` 或 `Darwin`）。
* **主机名 (Hostname)**: 固定为 `openinvest-install`，用于标记此事件来自 CLI 安装。

### Opt-out (如何禁用)
如果在运行 `install.sh` 之前设置了环境变量 `OPENINVEST_NO_TELEMETRY=1`，则不会进行任何上报：
```bash
OPENINVEST_NO_TELEMETRY=1 bash skills/install.sh
```

---

## 🌐 2. 官网埋点 (openinvest-site)

官网（`openinvest.involutionhell.com` / `openinvest.dev`）接入了 Umami 追踪代码，用于分析宣传页和文档页面的访问情况。主要有以下两种埋点事件：

### A. 复制行为埋点 (Event: `copy`)
捕获用户复制安装命令或页面内容的动作。

* **触发方式**:
  1. 点击安装命令组件 (InstallTabs) 的 **Copy** 按钮。
  2. 在页面中通过快捷键 (Ctrl+C / Cmd+C) 或鼠标右键菜单复制任何选中的文本。
* **收集的数据字段**:
  * `content`: 被复制文本的前 200 个字符（超出部分截断，防止上传长内容或潜在敏感信息）。
  * `method`: 复制触发的方式，`button`（点击复制按钮）或 `keyboard`（键盘快捷键或右键菜单复制）。

### B. 浏览行为埋点
用于了解用户阅读了哪些内容、哪些内容最受关注。

#### 1. 落地页模块浏览 (Event: `section-view`)
使用浏览器 `IntersectionObserver` 监听用户滚动。当某个主要模块有 **20% 以上** 面积进入视口时触发。**每个模块在单次访问中仅触发一次**。

* **收集的数据字段**:
  * `section`: 模块的 `id` 属性，包括：
    * `hero` (首屏)
    * `why` (核心机制/为什么不同)
    * `evidence` (图表与回测数据)
    * `methodology` (长期记忆与数学公式)
    * `get-started` (页脚/安装入口)

#### 2. 文档页面浏览 (Event: `doc-view`)
当用户浏览文档视图 (`DocsView`) 并且切换不同文档时触发。

* **收集的数据字段**:
  * `doc`: 当前阅读的文档唯一标识符 (slug)，例如：
    * `01-architecture`
    * `02-agents`
    * `03-dreaming`

---

## 🔒 隐私保障承诺
1. **完全匿名**: 所有埋点均不收集 IP 地址、地理位置、User-Agent 细节、邮箱或任何可识别个人的 PII 信息。
2. **无 Cookie**: 追踪脚本完全基于匿名内存会话，不在浏览器写入任何 Cookie，完全符合 GDPR / CCPA 规范。
3. **零业务数据上报**: openInvest 运行时的持仓明细、API Key、LLM 调用日志等**绝对不会**上传至任何第三方，所有敏感业务数据均保留在您的本地机器（`memory/` 目录下）。
