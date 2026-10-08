# event-gate-replay-2026-10 — 事件触发闸 + 冷却 + 日上限 + 越级 回放

**问题**：`ingest_event`（agent 投喂门，Hermes `market-intel-sentinel` 走这条）以前只入库
不触发委员会；2026-10 把判级闸提成共享 service（`src/openinvest/services/event_trigger.py`），
两条门（爬虫 `event_watch` + 投喂 `ingest_event`）共用同一道闸 + 频控：

- 同 symbol 12h 冷却；冷却期内新事件 severity **严格高于**开冷却那条 → 越级放行
  （`event.committee_escalation_bypass`，默认开）
- 任意滚动 24h ≤ 4 次（按 symbol 计）；同一批内额度紧时 severity 高的先占

上线前先回算：这套规则在历史上会触发多少次委员会、拦掉了什么。

零 LLM、零网络、只读 `events.db`。闸和频控直接调生产纯函数 `is_triggerable` / `admit`，
回放口径 = 生产口径。默认参数（mid / 12h / 4）按任务规格在跑之前定死；越级开关是 review
发现"低 severity 冷却吞掉 sev-3"后按推荐方案加的，开/关两组结果都列出，没有看结果后调阈值。

## 跑法（仓库根）

```bash
DB=$INVEST_HOME/db/events.db; W=<holdings ∪ target_assets，逗号分隔>; E=2026-10-07T11:00:00+00:00
R="uv run python experiments/event-gate-replay-2026-10/replay.py --db $DB --watched $W --warmup-from 2026-08-12"
O=experiments/event-gate-replay-2026-10
$R --start 2026-08-20 --end $E                                   > $O/result_hermes.json
$R --start 2026-08-20 --end $E --no-escalation-bypass            > $O/result_hermes_no_bypass.json
$R --start 2026-08-20 --end $E --source all \
   --committee-dir $INVEST_HOME/memory/.committee                > $O/result_all_doors.json
$R --start 2026-08-20 --end $E --source all --no-escalation-bypass > $O/result_all_doors_no_bypass.json
# 事件密集日案例：--trace 给要追踪的 ticker
$R --start <D> --end <D+1> --source all --trace <TICKER> \
   --trace-from <D> --trace-to <D+2>                 > $O/result_case_event_day.json
# 同上加 --no-escalation-bypass                                   > $O/result_case_event_day_no_bypass.json
```

- **去标识**：关注 symbol 按 `--watched` 顺序记为 S1..S4，结果文件里没有真实 ticker
  （关注集合 = 持仓 ∪ 目标资产，公开仓库不能出现）。映射只在跑的人手里。
- `--end` 用完整时间戳 = 冻结快照上界（生产库还在长），同参数重跑逐字节一致。
- `--warmup-from 2026-08-12`：频控有路径依赖（谁先占了额度），先用前一周的事件把冷却/额度
  状态推到稳态再开始计数；冷启动会让窗口头一两天结论失真（下面案例就会看到）。

## 结果（2026-08-20 → 2026-10-07，快照 2026-10-07T11:00Z，49 个北京自然日）

一次"委员会" = 一个 symbol 跑一次（多 symbol task 算 N 次，LLM 花费按 symbol 走）。
"sev-3 命中" = 一条 sev-3 过闸事件 × 它命中的一个关注 symbol。

| | 只看 Hermes · 越级开 | 只看 Hermes · 越级关 | **两门合计 · 越级开（推荐默认）** | 两门合计 · 越级关 | 旧码实际（仅爬虫门，无频控） |
|---|---|---|---|---|---|
| 委员会总数 | 104 | 96 | **154** | 139 | 901（885 个 task） |
| 其中 Hermes 门 / 爬虫门 | 104 / — | 96 / — | **59 / 95** | 52 / 87 | 0 / 901 |
| 按 symbol | S2 59 · S1 31 · S3 12 · S4 2 | S2 53 · S1 31 · S3 10 · S4 2 | S2 107 · S1 33 · S3 11 · S4 3 | S2 88 · S1 34 · S3 14 · S4 3 | S2 861 · S1 26 · S3 12 · S4 2 |
| 单日最多 | 4 | 4 | 4 | 4 | 34 |
| 顶到日上限的天数 | 10 | 7 | 21 | 13 | — |
| sev-3 命中：跑了（含越级） | 69（15） | 61 | **82（39）** | 49 | — |
| sev-3 命中：被冷却拦 / 被上限拦 | 280 / 33 | 317 / 4 | 537 / 149 | 707 / 12 | — |
| 低 severity 开的冷却吞掉 sev-3 的窗口数 | 0 | 19 | 0 | 61 | — |

人话：

- **两门共享预算，看"两门合计"那两列**：推荐配置下 49 天跑 154 次委员会，单日最多 4 次；
  其中 Hermes 门占 59 次。单看 Hermes 门（104 次）是假设爬虫门不存在，会高估它的份额。
- **对爬虫门是大幅降频**：旧码同期实际跑了 901 次（单日最多 34 次，几乎全是 S2）。
- **越级修掉的就是"sev-2 冷却吞 sev-3"**：关掉时有 61 个冷却窗口（由 sev-2 事件打开）把随后
  的 sev-3 吞了，sev-3 命中只跑了 49 次；打开后这个数归零，sev-3 跑 82 次（其中 39 次靠越级），
  总量 +15。
- **代价是日上限变成了瓶颈**：越级开时 21 天顶到上限（关时 13 天），被上限拦下的 sev-3
  从 12 升到 149。上限内按 severity 排序只在同一批里起作用；Hermes 一次只喂一条，跨批次
  仍是先到先得——上午的 sev-2 会占掉晚上 sev-3 的额度。要不要把上限调高 / 给 sev-3 留额度，
  是下一个要用户拍板的旋钮，这里不改。

## 案例：关注标的 S3 的事件密集日（北京时间）

当天 S3 相关的过闸事件全部来自 Hermes 门：上午两条是**预告性**报道（sev-2）；下午两条是
**事件本身**的首批报道（sev-2）；晚间是第一条 sev-3；次日早上又有两条 sev-3 后续报道。
旧码：这些事件全部 0 次触发。

主要过闸事件（当晚另有两条 sev-2 后续报道，两种配置都被冷却拦）：

| 时间（D=事件日） | sev | 类型 | 越级开（预热） | 越级关（预热） |
|---|---|---|---|---|
| D 上午① | 2 | 预告 | **跑** | 上限拦 |
| D 上午② | 2 | 预告 | 冷却拦 | 上限拦 |
| D 下午① | 2 | 事件本身 | 冷却拦 | 上限拦 |
| D 下午② | 2 | 事件本身 | 冷却拦 | 上限拦 |
| D 晚间 | 3 | 事件本身 | **跑（越级）** | **跑** |
| D+1 早① | 3 | 后续 | 冷却拦 | 冷却拦 |
| D+1 早② | 3 | 后续 | **跑** | 上限拦（再晚一条才跑） |

如实说：

- **第一次看到事件本身的委员会是 D 晚间那条 sev-3，两种配置都是**；下午两条 sev-2 首批报道
  两种配置下都没触发——越级开时被预告打开的同级冷却拦，越级关时被日上限拦（当天额度已被其它
  symbol 用掉）。
- 结论对状态起点敏感：若从 D 日 0 点冷启动，越级关时晚间的 sev-3 被预告打开的冷却吞掉
  （正是越级要修的情况），越级开时它又被日上限拦下，两种配置都要到 D+1 早上才第一次看到
  事件本身。所以单日案例证明的是"现在会触发"，不是"一定第一时间看到结果"——后者取决于日上限。

## 输入与口径（限制如实记）

- **关注集合**：用当前 holdings ∪ target_assets（4 个 symbol）。target_assets 在整个窗口内
  稳定（每日委员会 transcript 目录逐日都是同一组）；holdings 历史无法逐日重建，按当前近似。
- **severity / stance 是入库后合并值**：同一事件被别的源重复报道时 severity 取 max、stance 取
  最新，不完全等于第一次入库那一刻。
- **假设每次触发都成功**（生产里触发失败会回滚预占，不占冷却/额度）；爬虫事件逐条喂，生产是
  每 30 分钟一批——symbol 级计数基本相同，只在顶上限的边界处批内 severity 排序可能改变谁先占。
- **排除 `price_action` 事件**：那是 price_sentinel 自己的门（独立冷却），不走这道闸。
- min_severity 按默认 `mid`（生产 config override 里没有 event 段）。
- 别名不打通：事件只标了指数代码、没标跟踪该指数的 ETF 时，不会命中关注集合里跟踪该指数的标的——
  闸按 ticker 字面匹配（与生产一致），这类事件在本回放里也不计。
