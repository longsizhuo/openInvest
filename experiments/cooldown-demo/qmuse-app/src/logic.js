// 由 sync.py 从 ../scripts/index.html 生成，勿手改
export function init(API) {

const $ = (id) => document.getElementById(id);
const T = { nav: "#2C2C2A", ma20: "#BA7517", ma60: "#D4537E", ma120: "#378ADD", ma250: "#7F77DD", up: "#E24B4A", down: "#1D9E75", grid: "#ECEAE4", axis: "#888780", band: "#F1EFE8", rsi: "#534AB7" };
let timer = null, lastFacts = null, tick = null, cur = null;   // cur：当前在轮询的任务，旧任务的回包丢掉
const EXPECT = 30;   // 秒；一次委员会实测 20–40 秒

// 等待态：计时 + 进度条 + 正反对照骨架，让人知道下面还有内容。进度条按经验时长走到 90% 停住，不假装精确
function startWait() {
  const t0 = Date.now(); $("wt").hidden = false; $("hl").textContent = ""; $("hl").hidden = true;
  const vs = $("vs"); vs.textContent = "";
  vs.append(el("div", "vh", "支持"), el("div", "vh r", "反对"));
  for (let i = 0; i < 3; i++) ["vc", "vc r"].forEach((cls) => { const c = el("div", cls); c.append(el("div", "sk"), el("div", "sk")); vs.append(c); });
  $("vs-wrap").hidden = false;
  const step = (state) => {
    const s = Math.round((Date.now() - t0) / 1000);
    $("wt-s").textContent = (state === "queued" ? "排队中，前面有人在跑" : "宏观、量化、风险三个角色正在交叉质询") + `　已用 ${s} 秒`;
    $("wt-bar").style.width = Math.min(90, s / EXPECT * 90) + "%";
  };
  clearInterval(tick); step("running"); tick = setInterval(() => step($("wt").dataset.state), 1000);
}
function stopWait() { clearInterval(tick); $("wt").hidden = true; $("hl").hidden = false; }

function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function size(svg, h) { const w = Math.max(240, Math.round(svg.clientWidth)); svg.setAttribute("viewBox", `0 0 ${w} ${h}`); svg.setAttribute("height", h); return w; }

// 图里只有程序算的数字和日期，不放任何模型文本，所以用 innerHTML 拼 SVG 是安全的
function drawChart(D) {
  const n = D.c.length, R = 48, K = ["c", "ma20", "ma60", "ma120", "ma250"], COL = { c: T.nav, ma20: T.ma20, ma60: T.ma60, ma120: T.ma120, ma250: T.ma250 };
  const last = n - 1, lg = $("lg"); lg.textContent = "";
  [["", D.d[last], "#5F5E5A"], ["净值 ", D.c[last], T.nav], ["MA20 ", D.ma20[last], T.ma20], ["MA60 ", D.ma60[last], T.ma60], ["MA120 ", D.ma120[last], T.ma120], ["MA250 ", D.ma250[last], T.ma250]]
    .forEach(([k, v, c]) => { if (v == null) return; const s = el("span", null, k + (typeof v === "number" ? v.toFixed(3) : v)); s.style.color = c; lg.append(s); });
  let W = size($("m"), 200); const PW = W - R, X = (i) => i * PW / (n - 1);
  const all = K.flatMap((k) => D[k]).filter((v) => v != null), lo = Math.min(...all), hi = Math.max(...all), Y = (v) => 8 + (hi - v) / (hi - lo || 1) * 184;
  const path = (k) => D[k].map((v, i) => v == null ? "" : `${i && D[k][i - 1] != null ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  const i0 = D.d.findIndex((d) => d >= D.peak), i1 = D.d.findIndex((d) => d >= D.trough);
  let m = `<rect x="${X(i0)}" y="0" width="${X(i1) - X(i0)}" height="200" fill="#E24B4A" opacity=".07"/>`;
  for (let t = 0; t <= 3; t++) { const v = lo + (hi - lo) * t / 3; m += `<line x1="0" x2="${PW}" y1="${Y(v)}" y2="${Y(v)}" stroke="${T.grid}"/><text x="${PW + 6}" y="${Y(v) + 4}" fill="${T.axis}" font-size="11">${v.toFixed(2)}</text>`; }
  // 回撤区间靠右时标签改右对齐、不越过绘图区，免得压到右侧刻度（手机窄屏实测重叠）
  const mx = (X(i0) + X(i1)) / 2, right = mx > PW - 60;
  m += `<text x="${right ? PW - 4 : mx}" y="14" text-anchor="${right ? "end" : "middle"}" fill="#A32D2D" font-size="11">最大回撤 ${D.mdd}%</text>`;
  ["ma250", "ma120", "ma60", "ma20"].forEach((k) => m += `<path d="${path(k)}" fill="none" stroke="${COL[k]}" stroke-width="1"/>`);
  m += `<path d="${path("c")}" fill="none" stroke="${T.nav}" stroke-width="1.5"/>`;
  $("m").innerHTML = m;
  W = size($("r"), 48); const amax = Math.max(...D.ret.map(Math.abs)) || 1, bw = Math.max(1, PW / n * .65);
  let r = `<line x1="0" x2="${PW}" y1="24" y2="24" stroke="${T.grid}"/>`;
  D.ret.forEach((v, i) => { const h = Math.abs(v) / amax * 22; r += `<rect x="${(X(i) - bw / 2).toFixed(1)}" y="${(v >= 0 ? 24 - h : 24).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(.6, h).toFixed(1)}" fill="${v >= 0 ? T.up : T.down}"/>`; });
  r += `<text x="${PW + 6}" y="10" fill="${T.axis}" font-size="11">+${amax.toFixed(1)}%</text>`;
  $("r").innerHTML = r;
  W = size($("s"), 70); const YS = (v) => 2 + (100 - v) / 100 * 50;
  let s = `<rect x="0" y="${YS(70)}" width="${PW}" height="${YS(30) - YS(70)}" fill="${T.band}"/>`;
  [70, 30].forEach((v) => s += `<text x="${PW + 6}" y="${YS(v) + 4}" fill="${T.axis}" font-size="11">${v}</text>`);
  s += `<path d="${D.rsi.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${YS(v).toFixed(1)}`).join("")}" fill="none" stroke="${T.rsi}" stroke-width="1.2"/>`;
  [0, Math.floor(n / 2), last].forEach((i, j) => s += `<text x="${X(i)}" y="68" text-anchor="${["start", "middle", "end"][j]}" fill="${T.axis}" font-size="11">${D.d[i]}</text>`);
  $("s").innerHTML = s;
  $("rsi-l").textContent = `RSI(14)　${D.rsi[last].toFixed(1)}`;
}

function drawDist(rows) {
  const box = $("dist"); box.textContent = ""; $("ax").textContent = "";
  if (!rows.length) { $("dist-wrap").hidden = true; return; }
  const L = Math.min(-5, ...rows.map((r) => r.p10)) - 2, H = Math.max(5, ...rows.map((r) => r.p90)) + 2;
  const x = (v) => (v - L) / (H - L) * 100;
  rows.forEach((r) => {
    const row = el("div", "hd" + (r.low ? " g" : "")); row.append(el("span", null, r.w));
    const bar = el("div", "rb");
    const rng = el("i"); Object.assign(rng.style, { top: "3px", height: "8px", borderRadius: "2px", left: x(r.p10) + "%", width: (x(r.p90) - x(r.p10)) + "%", background: r.low ? "#E5E3DC" : "#D3D1C7" });
    const zero = el("i"); Object.assign(zero.style, { top: "-2px", width: "1px", height: "18px", left: x(0) + "%", background: "#E24B4A" });
    const med = el("i"); Object.assign(med.style, { top: "0", width: "2px", height: "14px", left: x(r.med) + "%", background: r.low ? "#B4B2A9" : "#2C2C2A" });
    bar.append(rng, zero, med); row.append(bar, el("b", null, r.low ? "样本少" : `${r.below}% 更低`));
    box.append(row);
  });
  [[L, `${Math.round(L)}%`], [0, "0"], [H, `+${Math.round(H)}%`]].forEach(([v, t]) => { const s = el("span", null, t); s.style.left = x(v) + "%"; $("ax").append(s); });
  $("dist-wrap").hidden = false;
}

function showFacts(f, sample) {
  lastFacts = f;
  $("name").textContent = f.name;
  $("meta").textContent = `${f.code}　${f.kind === "stock" ? "收盘" : "净值"} ${f.nav}　${f.nav_date}` + (sample ? "　今日示例，输入代码可换一只" : "");
  const chips = $("chips"); chips.textContent = ""; f.chips.forEach((c) => chips.append(el("span", null, c)));
  $("rep").hidden = false; startWait();
  drawChart(f.chart); drawDist(f.dist);
}

// 模型文本一律走 textContent，不进 innerHTML
function showResult(res) {
  stopWait();
  const hl = $("hl"); hl.className = "hl"; hl.textContent = res.headline;
  const vs = $("vs"); vs.textContent = "";
  vs.append(el("div", "vh", "支持"), el("div", "vh r", "反对"));
  const rows = Math.max(res.pros.length, res.cons.length);
  for (let i = 0; i < rows; i++) {
    [[res.pros[i], "vc"], [res.cons[i], "vc r"]].forEach(([it, cls]) => {
      const c = el("div", cls);
      if (it) {
        c.append(it.claim);
        const ev = el("span", it.hot ? "hot" : null); ev.append(el("small", null, it.role), it.evidence || "");
        c.append(ev);
      }
      vs.append(c);
    });
  }
  $("vs-wrap").hidden = false;
}

function showError(msg) { $("err").textContent = msg; $("err").hidden = false; }
function finish() { $("go").disabled = false; }

async function poll(id) {
  if (id !== cur) return;
  const r = await fetch(`${API}/api/debate/${id}`); const j = await r.json();
  if (id !== cur) return;
  if (!r.ok) { stopWait(); $("vs-wrap").hidden = true; showError(j.detail || "查询失败，稍后再试"); $("hl").textContent = ""; return finish(); }
  if (j.status !== "done") { $("wt").dataset.state = j.status; timer = setTimeout(() => poll(id), 2500); return; }
  if (j.result.headline) showResult(j.result); else { stopWait(); $("vs-wrap").hidden = true; $("hl").className = "hl wait"; $("hl").textContent = j.result.error || "这次辩论没跑成，稍后再试"; }
  finish();
}

$("ask").addEventListener("submit", async (e) => {
  e.preventDefault(); clearTimeout(timer); cur = null;
  $("err").hidden = true; $("go").disabled = true;
  try {
    const r = await fetch(API + "/api/debate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: $("code").value.trim() }) });
    const j = await r.json();
    if (!r.ok) { showError(j.detail || "提交失败，稍后再试"); return finish(); }
    cur = j.job_id; showFacts(j.facts); poll(cur);
  } catch { showError("连不上服务，检查网络后再试"); finish(); }
});
// 今天的示例报告点了才看（服务端每天跑一次，不占访客额度）：首屏只留输入框
$("sample").addEventListener("click", async () => {
  if (cur) return;   // 已经在跑自己输的代码，不拿示例盖掉
  $("sample").hidden = true;
  try {
    const r = await fetch(API + "/api/sample"); if (!r.ok) throw 0;
    const j = await r.json(); if (cur) return;
    cur = j.job_id; showFacts(j.facts, true); poll(cur);
  } catch { $("sample").hidden = false; showError("示例暂时拿不到，稍后再试"); }   // 按钮留着能再点
});
// 安装方式、命令和说明照抄官网 openinvest-site（src/data/site.ts 的 INSTALL_TABS + src/i18n.tsx 的中文 install 说明，
// 对齐 openinvest-site#5），那边改了这里同步。复制整个标签的全部命令，和官网一致
const INSTALL = [
  { label: "Claude Code", cmds: ["/plugin marketplace add longsizhuo/openInvest", "/plugin install invest@openinvest"],
    note: "自动注册 MCP server（含全部工具）+ 委员会 skills。安装后对 Claude 说“帮我初始化 invest”。Coordinator 路径不需要 API key。" },
  { label: "Codex", cmds: ["codex plugin marketplace add longsizhuo/openInvest", "codex plugin add invest@openinvest"],
    note: "Codex 读同一份 SKILL.md。要 MCP 工具再跑：codex mcp add openinvest --env INVEST_HOME=$HOME/openInvest -- uvx openinvest-mcp" },
  { label: "Hermes", cmds: ["hermes plugins install openinvest --enable"],
    note: "从 Hermes 官方插件目录安装审核过的版本（skills 以 openinvest:invest 加载）；老版本 Hermes 没有插件目录时用 hermes plugins install longsizhuo/openInvest/plugin --enable。MCP 工具：在 ~/.hermes/config.yaml 加 mcp_servers.openinvest → command: bash，args: [~/.hermes/plugins/openinvest/skills/invest/scripts/run.sh 的绝对路径, mcp]，trust: untrusted（写账本前 Hermes 会先问你）。" },
  { label: "OpenClaw", cmds: ["openclaw plugins install clawhub:openinvest"],
    note: "装 ClawHub 上的 bundle：skills 和 MCP server 一起注册。不支持 bundle 的旧版：在 ~/.openclaw/openclaw.json 的 mcp.servers 里声明 MCP server（command: uvx，args: [openinvest, mcp]），skills 从仓库的 plugin/skills 拷进 workspace。" },
  { label: "GitHub", cmds: ["git clone https://github.com/longsizhuo/openInvest.git ~/openInvest && cd ~/openInvest && bash skills/install.sh"],
    note: "手动安装。后端在首次运行时自举（uv sync）。" },
];
function drawInstall(on) {
  const bar = $("it-bar"); bar.textContent = "";
  INSTALL.forEach((x, i) => {
    const b = el("button", null, x.label); b.type = "button"; b.setAttribute("role", "tab"); b.setAttribute("aria-selected", String(i === on));
    b.addEventListener("click", () => drawInstall(i)); bar.append(b);
  });
  const cp = el("button", "cp", "复制"); cp.type = "button"; cp.setAttribute("aria-label", "复制安装命令");
  cp.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(INSTALL[on].cmds.join("\n")); cp.textContent = "已复制"; setTimeout(() => { cp.textContent = "复制"; }, 1600); }
    catch { getSelection().selectAllChildren($("it-cmd")); }   // 剪贴板被拦就选中命令，让人自己复制
  });
  bar.append(cp);
  const cmd = $("it-cmd"); cmd.textContent = ""; INSTALL[on].cmds.forEach((c) => cmd.append(el("div", null, c)));
  $("it-note").textContent = INSTALL[on].note;
}
drawInstall(0);

// ---------- 试玩：你 vs AI。行情和 AI 决定是 build_game.py 生成的静态数据，输赢全在浏览器里算 ----------
const POS = [["空仓", 0], ["半仓", 0.5], ["满仓", 1]];
const ACT = { ACCUMULATE: ["加仓", 0.5], BUY: ["加仓", 0.5], TRIM: ["减仓", -0.5], SELL: ["清仓", -1] };
const ROUND_LENGTHS = [12, 24, 36];
const C = { you: "#EC5F93", ai: "#6B57F0", hold: "#A9A9B6" };
const COST = 0.001, START = 100000, CALM = matchMedia("(prefers-reduced-motion: reduce)").matches;
let gm = null, gameUrl = null, busy = false;   // gm：当前一局；gameUrl：数据的绝对地址，表情包按它拼（QMuse 版接口在别的域名）
const yuan = (v) => "¥" + Math.round(v).toLocaleString("zh-CN");
const pct = (v) => { const x = Math.round(v * 1000) / 10; return (x > 0 ? "+" : "") + x.toFixed(1) + "%"; };   // 先取整再定符号，免得出 "-0.0%"
const tone = (v) => (v > 0 ? "up" : v < 0 ? "dn" : "");
const posName = (p) => POS.find(([, v]) => v === p)[0];
const px = (v) => v.toFixed(2);
const sticker = (name) => String(new URL(`sticker/${name}`, gameUrl));

function go(v) {
  ["home", "pick", "play", "dbt"].forEach((k) => { $("v-" + k).hidden = k !== v; });
  window.scrollTo(0, 0);
  if (v === "dbt" && lastFacts) drawChart(lastFacts.chart);   // 隐藏时画的图宽度不对，切过来重画
  if (v === "home") {
    if (gm && gm.k >= gm.a.turns.length && heroA === gm.a) setHero(randomRound(heroA.turns.length));
    drawHero();
  }
  if (v === "play" && gm) { if (!$("gm-live").hidden) drawGame(); else drawEq(); }
}

function aiStep(p, v) {
  const a = ACT[v];
  if (!a) return ["持有", p];
  const np = v === "SELL" ? 0 : Math.min(1, Math.max(0, p + a[1]));
  return [np === p ? `${a[0]}，但仓位已到头，没动` : a[0], np];
}

function tween(node, from, to, fmt) {
  if (CALM || from === to) { node.textContent = fmt(to); return; }
  const t0 = performance.now();
  const f = (t) => { const k = Math.min(1, (t - t0) / 450); node.textContent = fmt(from + (to - from) * (1 - (1 - k) ** 3)); if (k < 1) requestAnimationFrame(f); };
  requestAnimationFrame(f);
}

// 纵轴只按「到这一周为止」的价格定刻度，避免泄露后续高低点。横轴按本局长度，线随周数往右长；
// prev 给了就把这一周新长出来的那段线描出来
function drawGame(prev) {
  const a = gm.a, i = a.turns[gm.k].i, base = a.c[a.turns[0].i], c = a.c.slice(0, i + 1).map((v) => v / base * 100), N = a.c.length;
  const svg = $("gm-chart"), W = size(svg, 160), PW = W - 44, X = (j) => j * PW / (N - 1);
  const lo = Math.min(...c), hi = Math.max(...c), Y = (v) => 8 + (hi - v) / (hi - lo || 1) * 140;
  const line = (from, to) => c.slice(from, to + 1).map((v, j) => `${j ? "L" : "M"}${X(from + j).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  const cut = prev == null || CALM ? i : prev, g0 = X(a.turns[0].i);
  let s = `<rect x="0" y="0" width="${g0.toFixed(1)}" height="156" rx="8" fill="#F3F3F6"/><text x="6" y="152" fill="${T.axis}" font-size="11">开局前</text>`;
  for (let t = 0; t <= 2; t++) { const v = lo + (hi - lo) * t / 2; s += `<line x1="${g0.toFixed(1)}" x2="${PW}" y1="${Y(v)}" y2="${Y(v)}" stroke="${T.grid}"/><text x="${PW + 6}" y="${Y(v) + 4}" fill="${T.axis}" font-size="11">${px(v)}</text>`; }
  s += `<path d="${line(0, cut)}" fill="none" stroke="${T.nav}" stroke-width="1.8" stroke-linejoin="round"/>`;
  if (cut < i) s += `<path class="seg" d="${line(cut, i)}" fill="none" stroke="${C.you}" stroke-width="2.4" stroke-linejoin="round"/>`;
  s += `<circle class="dot" cx="${X(i).toFixed(1)}" cy="${Y(c[i]).toFixed(1)}" r="5" fill="${C.you}" stroke="#fff" stroke-width="2"/>`;
  svg.innerHTML = s;
  const seg = svg.querySelector(".seg");
  if (seg) { const L = seg.getTotalLength(); seg.style.strokeDasharray = L; seg.style.strokeDashoffset = L; seg.getBoundingClientRect(); seg.style.transition = "stroke-dashoffset .45s ease-out"; seg.style.strokeDashoffset = "0"; }
}

function drawProg() {
  const p = $("gm-prog"); p.textContent = "";
  gm.a.turns.forEach((_, j) => { const d = gm.hist[j]; p.append(el("i", d == null ? (j === gm.k ? "now" : null) : d > 5e-4 ? "y" : d < -5e-4 ? "a" : "t")); });
}

function drawTurn(prev, old) {
  const a = gm.a, t = a.turns[gm.k];
  $("gm-week").textContent = `第 ${gm.k + 1} / ${a.turns.length} 周`;
  $("gm-date").textContent = `第 ${gm.k + 1} 周 · 走势 ${px(a.c[t.i] / a.c[a.turns[0].i] * 100)}（开局 = 100）`;
  [["you", gm.you], ["ai", gm.ai]].forEach(([k, s]) => {
    tween($("gm-" + k), old ? old[k] : s.eq, s.eq, yuan);
    const r = s.eq / START - 1, p = $(`gm-${k}-p`); p.textContent = "";
    p.append(el("span", tone(r), pct(r)), `　${posName(s.p)}`);
  });
  const d = (gm.you.eq - gm.ai.eq) / START * 100;
  $("gm-lead").textContent = Math.abs(d) < 0.05 ? "目前打平" : d > 0 ? `你领先 ${d.toFixed(1)} 个点` : `AI 领先 ${(-d).toFixed(1)} 个点`;
  drawProg(); drawGame(prev);
  $("gm-pick").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.p === gm.you.p)));
}

// 一周：双方按各自仓位吃下一周涨跌（调仓先扣成本）
function step(p) {
  const a = gm.a, t = a.turns[gm.k];
  const i1 = gm.k + 1 < a.turns.length ? a.turns[gm.k + 1].i : a.c.length - 1;
  const r = a.c[i1] / a.c[t.i] - 1, [act, aiP] = aiStep(gm.ai.p, t.v);
  const [ry, ra] = [[gm.you, p], [gm.ai, aiP]].map(([s, np]) => {
    const e0 = s.eq; s.eq *= (1 - Math.abs(np - s.p) * COST) * (1 + np * r); s.p = np; return s.eq / e0 - 1;
  });
  gm.hist.push(ry - ra); gm.eq.push([gm.you.eq, gm.ai.eq]); gm.k += 1;
  return { t, act, r, ry, ra };
}

function reveal({ t, act, r, ry, ra }) {
  const box = $("gm-last"), d = ry - ra;
  box.textContent = ""; box.hidden = false; box.classList.remove("pop"); void box.offsetWidth; box.classList.add("pop");
  if (Math.abs(d) > 5e-4) { const img = el("img"); img.src = sticker(d > 0 ? "cheer.jpg" : "pout.jpg"); img.alt = d > 0 ? "这周你赢了" : "这周 AI 赢了"; box.append(img); }
  const txt = el("div");
  txt.append(el("b", null, `AI 亮牌：${act}` + (t.v ? "" : "（这周没有记录，按持有算）")));
  // 原始理由可能含标的、绝对价格或事件日期，对局中不展示。
  const wk = el("p", "g-wk");
  wk.append("这周行情 ", el("b", tone(r), pct(r)), "　你 ", el("span", tone(ry), pct(ry)), "　AI ", el("span", tone(ra), pct(ra)));
  txt.append(wk); box.append(txt);
}

function play(p) {
  if (busy || !gm || gm.k >= gm.a.turns.length) return;
  const prev = gm.a.turns[gm.k].i, old = { you: gm.you.eq, ai: gm.ai.eq };
  reveal(step(p));
  if (gm.k >= gm.a.turns.length) { endGame(); return; }
  drawTurn(prev, old);
  if (!CALM) { busy = true; setTimeout(() => { busy = false; }, 450); }   // 线描完再收下一手，连点不会跳周
}

function drawEq() {   // 结局图：三条净值线
  const svg = $("gm-eq"); if (!svg || !gm) return;
  const a = gm.a, W = size(svg, 150), PW = W - 52, n = gm.eq.length, base = a.c[a.turns[0].i];
  const hold = gm.eq.map((_, j) => START * a.c[j < a.turns.length ? a.turns[j].i : a.c.length - 1] / base);
  const all = [...gm.eq.flat(), ...hold], lo = Math.min(...all), hi = Math.max(...all);
  const X = (j) => j * PW / (n - 1), Y = (v) => 8 + (hi - v) / (hi - lo || 1) * 132;
  const line = (vs, col, w, dash) => `<path d="${vs.map((v, j) => `${j ? "L" : "M"}${X(j).toFixed(1)},${Y(v).toFixed(1)}`).join("")}" fill="none" stroke="${col}" stroke-width="${w}"${dash ? ' stroke-dasharray="4 4"' : ""} stroke-linejoin="round"/>`;
  let s = `<line x1="0" x2="${PW}" y1="${Y(START)}" y2="${Y(START)}" stroke="${T.grid}"/><text x="${PW + 6}" y="${Y(START) + 4}" fill="${T.axis}" font-size="11">10 万</text>`;
  s += line(hold, C.hold, 1.5, true) + line(gm.eq.map((e) => e[1]), C.ai, 2.2) + line(gm.eq.map((e) => e[0]), C.you, 2.6);
  svg.innerHTML = s;
}

function endGame() {
  const a = gm.a, you = gm.you.eq / START - 1, ai = gm.ai.eq / START - 1, i0 = a.turns[0].i;
  const hold = a.c[a.c.length - 1] / a.c[i0] - 1, diff = (you - ai) * 100, tie = Math.abs(diff) < 0.05;
  $("gm-live").hidden = true; $("gm-week").textContent = "结局"; drawProg();
  $("gm-name").textContent = a.name;
  const box = $("gm-end"); box.textContent = ""; box.hidden = false;
  const k = tie ? "tie" : diff > 0 ? "win" : "lose", img = el("img");
  img.src = sticker(k === "lose" ? "lose.gif" : k + ".jpg"); img.alt = { tie: "打平", win: "你赢了", lose: "AI 赢了" }[k];
  box.append(img, el("h2", null, tie ? "和 AI 打平" : diff > 0 ? `你赢了 AI ${diff.toFixed(1)} 个点` : `AI 赢了你 ${(-diff).toFixed(1)} 个点`),
    el("p", "g-sub", `揭晓：${a.name}（${a.code}），${a.d[i0]} 到 ${a.d[a.d.length - 1]}，${a.turns.length} 周`));
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.id = "gm-eq"; svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "全程资金曲线：你、AI、一直满仓");
  const res = el("div", "g-res");
  [["你", you, C.you], ["AI", ai, C.ai], ["一直满仓", hold, "#7C7C88"]].forEach(([n, v, col]) => {
    const c = el("div"), lab = el("small", null, n); lab.style.color = col; c.append(lab, el("b", tone(v), pct(v))); res.append(c);
  });
  const row = el("div", "g-ctas");
  const again = el("button", "g-cta pri", "随机下一局"); again.type = "button"; again.addEventListener("click", () => { const next = randomRound(a.turns.length, a); setHero(next); startGame(next); });
  const other = el("button", "g-cta sec", "重玩本局"); other.type = "button"; other.addEventListener("click", () => startGame(a));
  row.append(again, other); box.append(svg, res, row);
  drawEq();
}

function startGame(a) {
  gm = { a, k: 0, you: { eq: START, p: 0.5 }, ai: { eq: START, p: 0.5 }, hist: [], eq: [[START, START]] };
  $("gm-name").textContent = "随机盲局";
  $("gm-end").hidden = true; $("gm-last").hidden = true; $("gm-live").hidden = false; busy = false;
  go("play"); drawTurn();
}

function pickButtons(box, onPick, keys) {
  POS.forEach(([name, p], k) => {
    const b = el("button", null, name), g = el("span", "gauge"), f = el("i");
    b.type = "button"; b.dataset.p = p; f.style.width = p * 100 + "%"; g.append(f); b.append(g);
    if (keys) b.append(el("kbd", null, String(k + 1)));
    b.addEventListener("click", () => onPick(p)); box.append(b);
  });
}
pickButtons($("gm-pick"), play, true);

// 首屏就是第一手：随机一个标的，画开局前三个月的走势（不含开局后任何价格），右边一块雾区是还没发生的下一周
let GA = [], heroA = null;

// 每局保留开局前 63 个交易日，只截取本局行情与结算点；从不按后续涨跌挑题。
function makeRound(a, start, weeks) {
  const selected = a.turns.slice(start, start + weeks);
  if (selected.length !== weeks || selected.some((t) => !t.v)) throw new Error("AI 决策记录不完整");
  const first = selected[0].i, from = Math.max(0, first - 63);
  const end = start + weeks < a.turns.length ? a.turns[start + weeks].i : a.c.length - 1;
  return { ...a, d: a.d.slice(from, end + 1), c: a.c.slice(from, end + 1), turns: selected.map((t) => ({ ...t, i: t.i - from })) };
}

function roundCandidates(weeks) {
  return GA.map((a) => {
    const starts = [];
    for (let i = 0; i + weeks <= a.turns.length; i++) {
      if (a.turns.slice(i, i + weeks).every((t) => t.v)) starts.push(i);
    }
    return { a, starts };
  }).filter(({ starts }) => starts.length);
}

function randomRound(weeks = 12, previous = heroA) {
  const eligible = roundCandidates(weeks);
  const different = eligible.filter(({ a }) => a.key !== previous?.key);
  const pool = different.length ? different : eligible;
  if (!pool.length) throw new Error("没有足够的完整历史决策");
  const { a, starts } = pool[Math.floor(Math.random() * pool.length)];
  return makeRound(a, starts[Math.floor(Math.random() * starts.length)], weeks);
}
function drawHero() {
  const a = heroA; if (!a || $("v-home").hidden) return;
  const i0 = a.turns[0].i, c = a.c.slice(0, i0 + 1), n = c.length, svg = $("hc-chart"), H = 230, W = size(svg, H);
  const FW = Math.max(72, Math.round(W * .22)), PW = W - FW - 10, X = (j) => j * PW / (n - 1);
  const lo = Math.min(...c), hi = Math.max(...c), Y = (v) => 16 + (hi - v) / (hi - lo || 1) * (H - 46);
  const d = c.map((v, j) => `${j ? "L" : "M"}${X(j).toFixed(1)},${Y(v).toFixed(1)}`).join(""), x1 = X(n - 1), y1 = Y(c[n - 1]);
  let s = `<defs><pattern id="fog" width="9" height="9" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="9" stroke="#E7E5F6" stroke-width="4"/></pattern></defs>`;
  s += `<path d="${d}L${x1},${H - 22}L0,${H - 22}Z" fill="#F5F4FA"/><path d="${d}" fill="none" stroke="${T.nav}" stroke-width="2" stroke-linejoin="round"/>`;
  s += `<rect x="${W - FW}" y="0" width="${FW}" height="${H - 22}" rx="16" fill="url(#fog)"/>`;
  s += `<text x="${W - FW / 2}" y="${(H - 22) / 2 + 20}" text-anchor="middle" font-size="60" font-weight="700" fill="${C.ai}">?</text>`;
  s += `<text x="${W - FW / 2}" y="${H - 6}" text-anchor="middle" font-size="12" fill="${T.axis}">下一周</text>`;
  s += `<text x="0" y="${H - 6}" font-size="12" fill="${T.axis}">开局前</text><text x="${x1}" y="${H - 6}" text-anchor="end" font-size="12" fill="${T.axis}">现在</text>`;
  s += `<circle class="pulse" cx="${x1}" cy="${y1}" r="5" fill="${C.you}"/><circle cx="${x1}" cy="${y1}" r="5" fill="${C.you}" stroke="#fff" stroke-width="2"/>`;
  svg.innerHTML = s;
}
function setHero(a) {
  heroA = a;
  $("hc-name").textContent = "随机盲局";
  $("hc-date").textContent = `${a.turns.length} 周 · 开局 = 100`;
  drawHero();
}
pickButtons($("hc-pick"), (p) => { if (!heroA) return; startGame(heroA); play(p); }, false);
$("hc-dice").addEventListener("click", () => { if (heroA) setHero(randomRound(heroA.turns.length)); });
document.addEventListener("keydown", (e) => {   // 桌面上 1/2/3 直接出手
  if ($("v-play").hidden || $("gm-live").hidden || $("rules").open || e.metaKey || e.ctrlKey || e.altKey || /INPUT|TEXTAREA/.test(e.target.tagName)) return;
  const k = "123".indexOf(e.key); if (k >= 0 && e.key) play(POS[k][1]);
});
$("gm-ff").addEventListener("click", () => {
  if (busy) return;
  while (gm.k < gm.a.turns.length - 1) step(gm.you.p);
  play(gm.you.p);
});
document.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => go(b.dataset.go)));
document.querySelectorAll("[data-how]").forEach((b) => b.addEventListener("click", () => $("rules").showModal()));
(async () => {
  try {
    const r = await fetch(API + "/api/game"); if (!r.ok) throw 0;
    gameUrl = r.url;
    $("hero-img").src = sticker("hero.jpg");
    GA = (await r.json()).assets;
    const tape = "<span>随机标的<b>名称隐藏</b></span><span>随机开局<b>日期隐藏</b></span><span>只看走势<b>开局 = 100</b></span><span>打完揭晓<b>你 vs AI</b></span>";
    $("tape").innerHTML = tape.repeat(8);
    setHero(randomRound());
    ROUND_LENGTHS.forEach((weeks, j) => {
      const [icon, tag, bg] = [["⚡", "快局", "#FFE7A6"], ["🎯", "标准局", "#DED8FF"], ["🏁", "长局", "#D3EAFF"]][j];
      const b = el("button"), ic = el("span", "g-ic", icon);
      b.type = "button"; b.dataset.weeks = weeks; ic.style.background = bg;
      b.append(ic, el("b", null, `${weeks} 周`), el("small", null, `${tag} · ${roundCandidates(weeks).length} 个标的随机抽取`));
      b.addEventListener("click", () => { const a = randomRound(weeks); setHero(a); startGame(a); }); $("gm-assets").append(b);
    });
  } catch { ["gm-err", "hc-err"].forEach((k) => { $(k).textContent = "游戏数据暂时拿不到，稍后再试"; $(k).hidden = false; }); }
})();
window.addEventListener("resize", () => {
  drawHero();
  if (lastFacts && !$("v-dbt").hidden) drawChart(lastFacts.chart);
  if (gm && !$("v-play").hidden) { if (!$("gm-live").hidden) drawGame(); else drawEq(); }
});

}
