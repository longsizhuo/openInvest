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
  const n = D.c.length, R = 40, K = ["c", "ma20", "ma60", "ma120", "ma250"], COL = { c: T.nav, ma20: T.ma20, ma60: T.ma60, ma120: T.ma120, ma250: T.ma250 };
  const last = n - 1, lg = $("lg"); lg.textContent = "";
  [["", D.d[last], "#5F5E5A"], ["净值 ", D.c[last], T.nav], ["MA20 ", D.ma20[last], T.ma20], ["MA60 ", D.ma60[last], T.ma60], ["MA120 ", D.ma120[last], T.ma120], ["MA250 ", D.ma250[last], T.ma250]]
    .forEach(([k, v, c]) => { if (v == null) return; const s = el("span", null, k + (typeof v === "number" ? v.toFixed(3) : v)); s.style.color = c; lg.append(s); });
  let W = size($("m"), 200); const PW = W - R, X = (i) => i * PW / (n - 1);
  const all = K.flatMap((k) => D[k]).filter((v) => v != null), lo = Math.min(...all), hi = Math.max(...all), Y = (v) => 8 + (hi - v) / (hi - lo || 1) * 184;
  const path = (k) => D[k].map((v, i) => v == null ? "" : `${i && D[k][i - 1] != null ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  const i0 = D.d.findIndex((d) => d >= D.peak), i1 = D.d.findIndex((d) => d >= D.trough);
  let m = `<rect x="${X(i0)}" y="0" width="${X(i1) - X(i0)}" height="200" fill="#E24B4A" opacity=".07"/>`;
  for (let t = 0; t <= 3; t++) { const v = lo + (hi - lo) * t / 3; m += `<line x1="0" x2="${PW}" y1="${Y(v)}" y2="${Y(v)}" stroke="${T.grid}"/><text x="${PW + 6}" y="${Y(v) + 4}" fill="${T.axis}" font-size="11">${v.toFixed(2)}</text>`; }
  m += `<text x="${(X(i0) + X(i1)) / 2}" y="14" text-anchor="middle" fill="#A32D2D" font-size="11">最大回撤 ${D.mdd}%</text>`;
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
  $("meta").textContent = `${f.code}　净值 ${f.nav}　${f.nav_date}` + (sample ? "　今日示例，输入代码可换一只" : "");
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
// 一进来就有内容：默认展示今天的示例报告（服务端每天跑一次，不占访客额度）。拿不到就保持只有输入框
(async () => {
  try {
    const r = await fetch(API + "/api/sample"); if (!r.ok || cur) return;
    const j = await r.json(); if (cur) return;
    cur = j.job_id; showFacts(j.facts, true); poll(cur);
  } catch {}
})();
// 安装方式、命令和说明照抄官网 openinvest-site（src/data/site.ts 的 INSTALL_TABS + src/i18n.tsx 的中文 install 说明，
// 对齐 openinvest-site#5），那边改了这里同步。复制整个标签的全部命令，和官网一致
const INSTALL = [
  { label: "Claude Code", cmds: ["/plugin marketplace add longsizhuo/openInvest", "/plugin install invest@openinvest"],
    note: "自动注册 MCP server（含全部工具）+ 委员会 skills。安装后对 Claude 说“帮我初始化 invest”。Coordinator 路径不需要 API key。" },
  { label: "Codex", cmds: ["codex plugin marketplace add longsizhuo/openInvest", "codex plugin add invest@openinvest"],
    note: "Codex 读同一份 SKILL.md。要 MCP 工具再跑：codex mcp add openinvest --env INVEST_HOME=$HOME/openInvest -- uvx openinvest-mcp" },
  { label: "Hermes", cmds: ["hermes plugins install longsizhuo/openInvest/plugin --enable"],
    note: "安装 plugin（skills 以 openinvest:invest 加载）。MCP 工具：在 ~/.hermes/config.yaml 加 mcp_servers.openinvest → command: bash，args: [~/.hermes/plugins/openinvest/skills/invest/scripts/run.sh 的绝对路径, mcp]，trust: untrusted（写账本前 Hermes 会先问你）。" },
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
window.addEventListener("resize", () => { if (lastFacts) drawChart(lastFacts.chart); });

}
