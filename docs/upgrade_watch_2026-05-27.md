# 上线监控备忘 — 2026-05-27 regime/指标/dreaming 升级

上线内容：真 TR ATR + Wilder RSI + 真百分位 + RVOL（market_metrics）、regime 双触发器 crash + recovery、
backtest cutoff/窗口修复、dreaming macro_shock 退役（免责只剩 crash）、verdict_review regime_at_decision +
窗口修复、6 条 clean reliable insight 落盘。备份在 `~/invest_preupgrade_backup_2026-05-27/`（文件级，无 git 网）。

## 接下来几天盯这 6 个信号 —— 任一出现 = 可能改坏了，考虑回退

1. **regime 误判**：NDQ/GC=F 在没有 20–30% 下跌时被标 `crash`；或高分位（>0.5）资产被标 `recovery`
   （recovery 要求分位 < 0.5）。→ 新 regime 逻辑坏。
2. **verdict 频繁 UNCLEAR / 委员会报错**：daily_report 邮件或 `.committee/<date>/*.md` 里 Verdict 解析成
   UNCLEAR，或 journal 报错飙升。→ CIO 解析 / Quant 格式问题恶化（已知 ~7% Quant 格式抽风，飙升才异常）。
3. **verdict 一刀切**：连续多天 / 多资产全 HOLD 或全 BUY。→ insight 注入或 regime 退化。
4. **alloc_cny 反复打到 ±100000 clamp**：→ LLM 单位错乱。
5. **指标失真**（看 `.committee/<date>/*.md` 的 Quant KEY_DATA）：RSI 不在 0–100、ATR 为 None/异常大、
   分位不在 0–1、NDQ/GC=F 的 MA250 缺失（live as_of_date=None 应能算出）。
6. **同行情下 verdict 日间剧烈跳变**：市场没动 verdict 却来回翻。→ 不稳定。

## 回退步骤（若触发）
```bash
# 1. 从文件级备份还原代码 + insights + DB
BK=~/invest_preupgrade_backup_2026-05-27
cp -r $BK/{core,jobs,db,utils,scripts,tests} /home/ubuntu/projects-review/invest/   # 按需挑文件
cp $BK/memory_insights/*.md /home/ubuntu/projects-review/invest/memory/insights/
cp $BK/market_data.db.snapshot /home/ubuntu/projects-review/invest/db/market_data.db   # 仅当 DB 也要退
# 2. 重启
sudo systemctl restart invest-scheduler.service
# （若已重启 web）sudo systemctl restart invest-web.service invest-web-demo.service
```

## 上线注意事项（已知，非异常）
- **invest-web.service / invest-web-demo.service 仍在跑旧代码**（本次只重启了 scheduler）。GUI / Web Direct
  路径触发的委员会仍是旧逻辑，直到这俩也重启。cron daily_report 已是新逻辑 → 短期内 cron 与 web 行为不一致。
- **invest-deploy.timer 每小时 `git pull --ff-only`**：本批改动**未 commit**。ff-only 不会 `reset --hard`，
  不会静默清掉未提交改动；但若 origin/main 推了改到这些文件的 commit，deploy 会**失败**（set -e）。
  未提交状态下这些改动只活在工作区 + 上面那个备份里。
- DB 的 OHLCV 列 + 回填**已在库**（向后兼容）。

---

## 追加 — 2026-05-27 晚:lift-based caution + post-cutoff 验证(已随本次 commit 上线)
- **post-cutoff 干净下跌窗口回测**（NDQ 2025-01~06 + 2026-01~05、GC=F 2026-02~05，max_debate_rounds=4）：
  crash 双触发器 0 次真实触发（−21%/VIX52 也只到 atr 2.3%/ret30d −19% → downtrend）；avoided_down>0 首次出现。
- **caution 改 lift-based 评分**（`_score`：base_down<0.15 或 lift≤0 → 0）。试金石：拒绝 Phase1.5 假 caution + post-cutoff V 形假 caution，接受合成真 caution。现有数据下 **0 条 caution 固化**（信号不存在，非门槛高）→ 正确休眠态。
- 反保守仍只靠 CONCENTRATION_PCT + reliable insight。详见 ADR 008。
- ⚠️ 仍未做：分支 `fix/email-render-wealth-view` 与 origin/main 的 14/13 分叉 reconciliation（独立大动作，未碰）。
