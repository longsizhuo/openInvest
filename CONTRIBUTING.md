# 贡献指南

> 如何为 openInvest 贡献代码。先读完[完整 Wiki](docs/wiki/README.md)再开 PR，能省下双方很多时间。

---

## 0. 在开始之前

**它是什么 / 不是什么**：

- ✅ 单人/小团队投资决策辅助工具
- ✅ 实验性多 agent 编排
- ✅ 自托管，数据留在你机器
- ❌ **不是**金融建议软件
- ❌ **不是**给券商/机构用的
- ❌ **不是**生产级合规系统

如果你的 PR 想往"专业金融软件"方向推（合规审计 / KYC / 多用户隔离），先开 Issue 讨论——这个项目刻意保持单人范围。

---

## 1. 怎么找事做

### 我能贡献什么

| 类型 | 难度 | 看哪 |
|------|------|------|
| 修 bug / 改文档错别字 | 🟢 | [Issues](https://github.com/longsizhuo/openInvest/issues) 标 `bug` / `docs` |
| 加新数据源 / 资产代理 | 🟡 | [Wiki: 07-extending](docs/wiki/07-extending.md#2-加新数据源) |
| 加 GUI 功能 | 🟡 | invest-gui 仓库 + [Wiki: 10-design-system](docs/wiki/10-design-system.md) |
| 加新 agent 角色 | 🔴 | [Wiki: 02-agents](docs/wiki/02-agents.md) + [07-extending](docs/wiki/07-extending.md#3-加新-agent-角色) |
| 大架构改动 | 🔴 | 先开 ADR 讨论，见下文 |

### 不接受的 PR 方向

- 🚫 加新一种付费 LLM provider 让用户必须订阅（除非保留 DeepSeek 路径）
- 🚫 改成多用户系统（项目目标是单人自托管）
- 🚫 引入需要后端 daemon 的前端框架（参考 mc-website 教训：`next start` 整机三次挂死）
- 🚫 抹掉数据语义颜色（涨绿跌红，详见 [10-design-system](docs/wiki/10-design-system.md)）
- 🚫 加任何 telemetry / analytics 上报到第三方

---

## 2. 准备开发环境

```bash
# clone
git clone https://github.com/longsizhuo/openInvest.git
cd openInvest

# Python 3.13+
uv sync --frozen

# 配 .env（DEEPSEEK_API_KEY 必填，其他可选）
cp .env.example .env
$EDITOR .env

# 跑测试确认环境 OK
uv run pytest tests/ -v
# 期望: 166 passed
```

如果你要改 GUI：

```bash
git clone https://github.com/longsizhuo/invest-gui.git
cd invest-gui
pnpm install

# 起后端 + 前端 dev
# Terminal 1: 在 invest 目录
INVEST_WEB_DEV_CORS=1 uv run uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765
# Terminal 2: 在 invest-gui 目录
pnpm dev   # 浏览器开 http://localhost:5173
```

详见 [QUICK_START.md](docs/QUICK_START.md)。

---

## 3. PR 流程

### 3.1 fork + 分支

```bash
# 在你的 GitHub fork
git checkout -b feat/your-feature   # 或 fix/xxx / docs/xxx
```

分支命名约定：

| 前缀 | 用途 |
|------|------|
| `feat/` | 新功能 |
| `fix/` | bug 修复 |
| `refactor/` | 重构（行为不变）|
| `docs/` | 文档 |
| `test/` | 加测试 |
| `chore/` | 依赖升级 / CI |

### 3.2 提交

**Commit message 风格**（看 `git log --oneline` 学）：

```
<type>(<scope>): <一句话说做了什么 + 为什么>

<可选：详细说明>
- 改 A 因为 B
- 删 C 因为 D
```

例：

```
feat(napcat): 11 命令切 v2 数据模型 + 18 fixture 测试

写命令全部从 cash_cny / gold_grams 扁平字段改成 cash dict + holdings list：
- _balance: pm.cash_amount("CNY") + holdings.find()
- _withdraw: 加余额校验，不足直接拒绝
...
```

**别用**：

- ❌ `fix bug`（说哪个 bug）
- ❌ `update`（说更新什么）
- ❌ `wip`（开 PR 前 squash 掉）

**Co-author 规则**：如果你的改动接管了别人的工作（基于他们的 PR / Issue），commit message 加 `Co-authored-by: Name <email>`。**不要**加 Claude 署名（[memory 决策](https://github.com/anthropics/claude-code) `feedback_commit_no_claude_coauthor`）。

### 3.3 测试

**底线**：166 测试不能挂。新功能必须**至少 3 测试**：
- happy path（功能正常）
- error path（输入错 / 资源缺失）
- 边界（空 / 超大 / 并发）

```bash
uv run pytest tests/ -v
uv run pytest tests/test_xxx.py -v   # 单跑
```

测试在哪写：

| 改动 | 测试位置 |
|------|---------|
| `core/*` | `tests/test_<module>.py` 直接测函数 |
| `agents/*` | mock LLM client，测 prompt 渲染 + parse |
| `connectors/web_api.py` | `tests/test_web_api.py`，FastAPI TestClient |
| `connectors/napcat_bot.py` | `tests/test_napcat_v2.py`，CommandContext fixture |
| 前端 | invest-gui 暂未配单测，至少 build 过 + 浏览器手测 |

详见 [tests/README.md](tests/README.md)。

### 3.4 文档同步（必做）

**改代码必须改文档**——否则 reviewer 会让你重提。

| 改动 | 同步文档 |
|------|---------|
| 加新 endpoint | `docs/wiki/06-api.md` 端点表 + invest-gui 跑 `pnpm gen-types` |
| 改 Pydantic schema | `docs/wiki/05-data-model.md` |
| 加新 agent 角色 | `docs/wiki/02-agents.md` 角色矩阵 |
| 加新 cron job | `jobs/README.md` |
| 加新数据源 | `docs/wiki/05-data-model.md` proxy_kind 表 |
| 大架构决策 | 开 ADR：`docs/wiki/adr/00X-xxx.md` |
| 子目录新增文件 | 该目录的 `README.md` |

**绝对不要**：改了代码不改 docs，让 reviewer 自己拼图。

### 3.5 开 PR

```bash
git push origin feat/your-feature
gh pr create --base main --title "feat(scope): xxx" --body "$(cat <<'EOF'
## Summary
<1-3 bullet 说做了什么 + 为什么>

## Test plan
- [ ] 跑通哪些场景
- [ ] 不破坏哪些现有功能

## 文档同步
- [ ] 列举改了哪些 docs
EOF
)"
```

PR 描述模板见 `.github/PULL_REQUEST_TEMPLATE.md`（如果有）。

### 3.6 CI

PR 自动跑 `.github/workflows/ci.yml`：
- `pytest tests/` 必须全过
- Smoke import check（防关键模块改名漏改）

CI 挂了先看日志再问人。常见原因：
- 加了新依赖没 `uv sync`
- 改了模块名导致 smoke check fail（同步改 `ci.yml`）
- 测试用了真 yfinance 网络（用 mock 替代）

详见 [Wiki: 09-troubleshooting#测试挂了](docs/wiki/09-troubleshooting.md#8-测试挂了)。

---

## 4. 代码风格

### 4.1 Python

- **Python 3.13+**
- **类型注解必填**：所有 public 函数标 type hints
- **docstring**：在做"为什么"非 trivial 的函数加，**不写"做什么"**（看代码即知）
- **import 顺序**：stdlib / third-party / 本地，组间空行
- **行长**：尽量 ≤ 100 字符（不强制）
- **注释**：**中文**（用户偏好，[memory feedback](https://github.com/anthropics/claude-code) `feedback_push_and_comments`）

例：

```python
def cash_total_in_base(
    cash: dict[str, float],
    base: str = "CNY",
) -> tuple[float, dict[str, Optional[float]]]:
    """把多币种 cash dict 折算到 base 总额

    返回 (total, per_currency_rate)：拉不到汇率的币种不计入。
    """
    total = 0.0
    rates: dict[str, Optional[float]] = {}
    for ccy, amt in cash.items():
        rate = get_fx_rate(ccy, base)
        rates[ccy] = rate
        if rate is not None:
            total += amt * rate
    return round(total, 2), rates
```

**禁止**：
- 不写类型的 dict / list（用 `dict[str, float]` 不写 `dict`）
- 全局可变状态（用闭包 / class 封装）
- 直接改 dict（必须走 `with_portfolio_tx()`）—— 详见 [Wiki: 05-data-model](docs/wiki/05-data-model.md)

### 4.2 TypeScript（invest-gui）

- **strict mode 开**
- **类型由 OpenAPI 自动生成**：`pnpm gen-types`，不手写 API 类型
- **CSS variables 走 token**：用 `bg-[var(--surface-raised)]` 不写 `bg-zinc-900`
- **不引 shadcn/ui / Material UI**（自定义组件库已够用）
- **注释**：中文（同 Python）

详见 [Wiki: 10-design-system](docs/wiki/10-design-system.md)。

### 4.3 通用

- 不写多段 docstring：一句话说清"为什么"，多了变 noise
- 不加 emoji 到代码里（除非是错误标记 `⚠`）
- 删除代码就**真删**，不要留 `// removed` 注释或 `# old code`
- 永远不 `git push --force` 到 main / 别人的分支

---

## 5. 架构决策（ADR）

**改架构必须先开 ADR**。

什么算"改架构"：
- 加新顶层目录
- 改数据 schema（`core/schemas.py`）
- 改并发模型（锁粒度 / atomic write 策略）
- 引入新外部依赖（LLM provider / 数据源）
- 修改 deployment 拓扑

ADR 流程：
1. 在 `docs/wiki/adr/` 加 `00N-your-decision.md`
2. 模板：[adr/001-dual-execution-paths.md](docs/wiki/adr/001-dual-execution-paths.md)
3. 状态先标 `🟡 提议`，PR 里讨论
4. PR merge = 状态改 `✅ 已采纳`
5. 更新 [adr/](docs/wiki/adr) 索引

**已采纳的 ADR 不要改**——要推翻新开一个 ADR-XXX `supersedes` 老的。

---

## 6. 安全 / 隐私

- 🚫 **永不 commit 凭据**：`.env` 在 .gitignore，但 push 前 `git diff --staged` 确认没漏
- 🚫 **永不 commit `memory/`**：含个人持仓 / 交易历史
- 🚫 **永不 commit `db/*.db`**：含行情 + 缓存
- 🚫 **永不写 hardcoded URL / API key**：所有走 env

发现历史 commit 不慎泄漏密钥？立刻：
1. 在密钥发行方（DeepSeek / Gmail）作废
2. 开 issue 标记 security
3. **不要**用 `git push --force` 重写——已经被 fork 也已被 CI 上传，重写无用

---

## 7. 沟通

- **Bug**：[GitHub Issue](https://github.com/longsizhuo/openInvest/issues)，附复现步骤
- **新功能讨论**：先开 Issue 讨论，避免大 PR 一来直接被拒
- **架构争议**：开 ADR 提议（见上）
- **私问**：longsizhuo@gmail.com，但优先公开讨论

---

## 8. License

MIT。提交 PR 即代表你同意以 MIT 协议授权这部分代码。

如果你 PR 里包含别人代码（CC / Apache / GPL），必须**提前说明**，可能不被接受（避免 license incompatibility）。

---

## 9. 给 Claude / AI 协作者的提示

如果你用 AI 工具帮写代码：

- ✅ 接受：让 AI 帮你写 boilerplate / 测试 fixture / 文档
- ✅ 接受：让 AI review 你的 diff
- ❌ 拒绝：把整个 PR 全交给 AI 自动跑（用户经验：100% AI 写的代码 90% 有架构问题）
- ❌ 拒绝：commit message 带 `Co-authored-by: Claude` 类似署名

最佳工作流：你定方向 + 写关键决策 + AI 写实现，**你 review 每一行 commit**。

---

## 10. 不知道从哪开始

- 看 [Wiki: 07-extending.md](docs/wiki/07-extending.md) 7 个 cookbook
- 跑 `git log --oneline | head -30` 看最近什么活跃
- 翻 [Issues](https://github.com/longsizhuo/openInvest/issues) 找 `good first issue` 标签
- 实在没头绪，开 Issue 说"我想贡献，能推荐什么"

---

## 致谢

> 写代码是一回事，让别人能读懂、能改、能扩，是另一回事。
> openInvest 不指望成为下一个开源大热门，但它欢迎认真的贡献者。

— longsizhuo
