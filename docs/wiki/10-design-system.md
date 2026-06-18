---
type: wiki-chapter
title: GUI 设计系统
tags: [design-system, frontend, css-tokens, typography, information-architecture]
intent: GUI 设计规范
documents:
  endpoints: []
  config_keys: []
  symbols: []
---

# GUI 设计系统

> Token 三层 + 排版三层 + IA 决策。
> 这一章给前端 / 设计师看，解释 GUI 为什么这样而不是那样。

[← 09-troubleshooting](09-troubleshooting.md) · [Wiki 索引](README.md)

---

## 设计哲学

走"金融 + 编辑感"的中间路线。**不是**纯 Monochrome 杂志风（金融场景需保留涨跌红绿语义），**也不是**Bloomberg 终端那种密集图表流。

### 借用 Monochrome 的部分

- ✅ 克制：颜色不当装饰，只承担数据语义
- ✅ 排版层次：display serif（Playfair）做大字 hero
- ✅ 0 圆角原则（除按钮 / input 触感圆角）
- ✅ 线条结构而非阴影 / ring 来分割

### 拒绝的部分

- ❌ 纯黑白杂志风（金融工具不能去掉红绿涨跌）
- ❌ 8xl/9xl 大字（dashboard 不是 Vogue 封面）
- ❌ 浅色背景（金融场景长时间使用，暗色护眼）
- ❌ 全 serif body（serif 数字宽度抖动伤可读性）

---

## Token 系统（CSS variables）

源：`invest-gui/src/index.css :root`

### Surface 容器层（4 级）

```css
--surface-base:     #0a0a0b;     /* body 最底 */
--surface-raised:   #131417;     /* 卡片 / dialog */
--surface-overlay:  #1c1d22;     /* hover / nested */
--surface-inverse:  #f5f5f5;     /* marketing hero 反相区 */
```

→ 三段密度的暗背景 + 一个反相白底（给"我们的 AI 怎么决策"这种 marketing 空态用）

### Text 文本层（3+1 级）

```css
--text-primary:    #f5f5f5;
--text-secondary:  #a1a1aa;
--text-tertiary:   #71717a;
--text-inverse:    #0a0a0b;
```

→ Tertiary 用于 label / hint / 时间戳；secondary 用于 placeholder / 二级信息；primary 是正文。

### Border 边线层（2 级）

```css
--border-subtle:   rgba(255,255,255,0.08);  /* 仅分割 */
--border-strong:   rgba(255,255,255,0.16);  /* 卡片边 / focus 起点 */
```

→ 用边线而不是阴影分隔。因为暗色背景下阴影本来就看不清。

### Accent 单色装饰类

```css
--accent:             #f5f5f5;
--accent-hover:       #ffffff;
--accent-foreground:  #0a0a0b;
```

→ 仅给主 CTA / focus / wordmark / active state 用。**禁止当背景填充装饰**。

### Data 数据语义层

```css
--pos:    #4ade80;     /* 涨 / 盈利 / 健康 */
--pos-bg: rgba(74,222,128,0.10);
--neg:    #f87171;     /* 跌 / 亏损 / 错误 */
--neg-bg: rgba(248,113,113,0.10);
--warn:   #fbbf24;     /* 警示 / 接近阈值 */
--warn-bg: rgba(251,191,36,0.10);
--stale:  #71717a;     /* 数据陈旧 */
```

→ **只给数字 / chip / 状态 badge 用，禁装饰**。
→ kind=etf 用蓝色 ring 是装饰（已废）；BUY=绿色是语义（保留）。

### Tailwind 接通

`tailwind.config.js` 用 `var(...)` 桥接：

```js
colors: {
  surface: {
    base: "var(--surface-base)",
    raised: "var(--surface-raised)",
    ...
  },
}
```

→ 组件里写 `bg-[var(--surface-raised)]` 或 `bg-surface-raised` 都行（语义一致）。

→ 未来想做 light mode：只改 `:root` 的 var 值，组件不动。

---

## 排版三层

### Display：Playfair Display (serif)

```css
--font-display: "Playfair Display", "Source Han Serif SC", Georgia, serif;
```

**用在哪**：
- Wordmark `invest`
- Page title（Dashboard 「持仓」/ Strategy 「策略」）
- Hero 大字（总资产 ¥XXX,XXX）
- 委员会 verdict 大字（"TRIM" / "ACCUMULATE"）
- 空态主标语

**不要用在**：正文、表格、数字（serif 数字宽度抖动）

### UI：Inter (sans)

```css
--font-ui: "Inter", "PingFang SC", "Hiragino Sans GB", system-ui, sans-serif;
```

**用在哪**：默认所有 UI 文本——按钮、label、表格、菜单、说明性段落、错误提示。

### Mono：JetBrains Mono

```css
--font-mono: "JetBrains Mono", ui-monospace, monospace;
```

**用在哪**：
- 所有数字（`tabular-nums` 配合）
- 时间戳（`02:17:18`）
- Symbol（`NDQ.AX` / `GC=F`）
- Task ID / 日志 / pre code block

→ 数字用 mono 解决了 serif 数字抖动 + 表格列对齐两个问题。

---

## 0 圆角原则

```css
:root {
  /* 没有 --radius，但默认也不给 */
}
```

`tailwind.config.js` 没写 `borderRadius` 扩展，沿用 Tailwind 默认。
所有组件**不写 `rounded-*`**——直角是默认。

例外：

| 例外 | 理由 |
|------|------|
| `rounded` (2px) on input/button | 触感圆角，避免视觉刺 |
| `rounded-full` on PipelineFlow agent avatar | 圆形 avatar 必须圆 |

→ 视觉一致性的代价是接受这两个例外。

---

## 焦点环（Focus Ring）

```css
*:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
```

→ 高对比 outline，**不靠颜色** identify。
→ accessibility WCAG AAA 要求；键盘 navigation 友好。

---

## 信息架构（IA）

### 顶导分层（不是 7 项平权）

```
invest    主面板  流水  策略  委员会                            系统
└──┬──┘   └──────────┬──────────┘                              └─┬─┘
wordmark    核心日常 4 项                                       次级
display     用户高频                                            字号缩 + 灰阶降一级
```

**核心 vs 次级的判定**：每天打开几次。

- 主面板 / 流水 / 策略 / 委员会 → 用户每天看好几次
- 系统 → 偶尔诊断 / 看 cron 跑没跑

**Active 状态用底边 2px 实色而不是颜色**——颜色留给数据语义。

### 委员会 7 tab 一站式

历史教训：之前有 `/committee` `/system` `/transparency` 三个路由都讲"AI 怎么决策"，用户分不清。

合并后：

```
/committee = 一切关于"AI 怎么决策"
  - 触发 / 直播
  - 决议归档（历史 markdown）
  - 决策回放（Pipeline 动画）
  - 4 角色 + 规则（system prompt 全文）
  - 历史命中率
  - LLM 用量
  - Tool 调用

/system = 内部状态 + 数据源
  - Cron Jobs
  - 市场 Regime
  - 数据源
  - PnL 历史
  - 长期模式
  - Dreams
```

→ 用户每次想知道"AI 是怎么算的" → /committee；"系统跑没跑" → /system。

详见 [adr/](adr/) 里关于 IA 合并的决策记录。

---

## Dashboard Hero Landmark

进入 Dashboard 第一眼必须看到的两个事实：

```
┌─────────────────────────────────────────────────────┐
│ 总资产 · 折 CNY                  上次委员会         │
│                                                     │
│ ¥225,378  ←─── Playfair display │ TRIM (italic)    │
│ 5xl/6xl                          │ NDQ.AX · 05-07   │
│                                  │ 置信 75%         │
│ 现金 ¥15,511    持仓 ¥209,867    │                  │
│ AUD=4.92                         │ 查看完整记录 →    │
└─────────────────────────────────────────────────────┘
```

设计要点：
- 总资产用 Playfair 大字 → 仪式感 + 一眼可读
- 数字用 mono 不用 serif → 解决宽度抖动
- 拉不到时显示 `¥—` 而不是 `¥0`（不假装零资产，避免误导）

---

## 数据语义颜色 vs 装饰颜色（关键分辨）

| 用途 | 是装饰还是语义 | 是否可用色 |
|------|---------------|----------|
| BUY 按钮 | 装饰（这是个动作不是状态）| ❌ accent 即可 |
| 浮盈 +12% | 语义（涨了）| ✅ text-pos |
| 浮亏 -3% | 语义（跌了）| ✅ text-neg |
| BUY verdict chip | 语义（建议方向）| ✅ chip-pos |
| HOLD verdict chip | 中性 | ❌ 灰阶 |
| ETF kind label | 装饰（kind 是结构信息不是状态）| ❌ 灰阶 mono |
| Macro role label | 装饰（角色是结构）| ❌ 灰阶 mono |
| 数据陈旧标记 | 语义（异常状态）| ✅ text-stale 或 text-warn |
| Dialog "新增" 按钮 | 装饰 | ❌ accent |
| Dialog "删除" 按钮 | 语义（破坏性动作）| ✅ chip-neg |

**判断公式**：「能不能映射到涨跌 / 成败 / 健康陈旧」？能 → 用色；只是分类标签 → 灰阶 + mono。

---

## 间距 / 节奏

无强制 grid 系统，但默认 padding：

| 容器 | padding |
|------|---------|
| Page main | `px-6 py-10`（max-w-6xl）|
| Card | `p-5`（5 = 1.25rem = 20px）|
| Dialog body | `p-5` |
| Dialog header | `px-5 py-4` |
| Tab button | `px-3 py-2` |
| Button md | `h-10 px-4` |
| Button sm | `h-8 px-3` |

垂直节奏：sections 之间用 `mt-10 border-t border-[var(--border-subtle)] pt-6` 强结构。

---

## 动画

`motion` (formerly framer-motion v12) 库仅用在：

- `PipelineFlow` 组件：粒子流动动画 + agent avatar 选中状态
- 其他**不动**

设计意图：金融工具不要 fancy 动画，hover 状态走 `transition-colors duration-100` 足够。

---

## 不用 shadcn/ui 的理由

| 决策 | 原因 |
|------|------|
| 不上 shadcn/ui | 单人内部工具，9 个自定义组件（Button/Card/Dialog/Field/StatusBadge/Tabs/SymbolSearch/PipelineFlow/HoldingCard）已够用 |
| 不上 Radix UI | 同上，Dialog 用原生 `<dialog>` 已经满足焦点管理 + ESC 关闭 |
| 不上 React Query | SWR 更轻，单纯 GET 场景两者无差 |
| 不上 Next.js | 静态 + Caddy file_server 模式，**禁 SSR daemon**（参考 mc-website 教训）|

---

## 维护规则

- 加新组件 → 先看 `Card` / `Button` / `Field` / `Dialog` 能不能复用
- 加新颜色 → 先问"是装饰还是语义"，能用 token 就别 hardcode
- 加新字体 → 9 成情况下用现有三层就够，新字体先在 wiki 提案讨论
- 改 token → 同步本 wiki 章节 + 通知所有用 hardcoded 颜色的地方

---

## 下一步

→ [invest-gui/README.md](https://github.com/longsizhuo/invest-gui#readme) — 前端仓库结构

→ [adr/](adr/) — IA 决策记录（为什么合并 transparency → committee）
