/* 实时字幕延迟与改写复盘 —— 前端
 * 原生 JavaScript + Web Audio API + Canvas
 * 联动：音频播放 / 波形 / 字幕演变带 / 词元详情 / 校审控件
 */
"use strict";

// ---------------------------------------------------------------- 工具
const $ = (id) => document.getElementById(id);

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).error || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}
const post = (url, body) => api(url, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});
const put = (url, body) => api(url, {
  method: "PUT",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});

const REASONS = {
  no_anchor: "无锚点",
  anchor_residual_exceeded: "锚点残差过大",
  ambiguous_alignment: "对齐多解",
  no_reference_match: "参考稿无对应",
  timestamp_nonmonotonic: "时标倒序",
  snapshot_gap: "快照缺页",
  never_stable: "未稳定",
  audio_range_incomplete: "音频范围不完整",
  no_valid_intervals: "无有效间隔",
  snapshot_gap_counts: "快照缺页",
};
const reasonText = (r) => REASONS[r] || r;
const undefText = (list) => "未定（" + list.map(reasonText).join("、") + "）";
const fmtS = (v) => (v == null ? "—" : Number(v).toFixed(2) + "s");
const fmtSigned = (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "s");

// ---------------------------------------------------------------- 全局状态
let S = null;            // 会话状态
let L = null;            // 版面复核状态
let SID = null;
let audioCtx = null;
let audioBuf = null;
let srcNode = null;
let playing = false;
let playStartCtx = 0;
let playStartOff = 0;
let peaks = null;
let selectedToken = null;
let selectedUtt = null;
let recessMode = false;
let drag = null;         // 波形拖选 {x0, x1}
let anchorRows = null;   // 锚点编辑副本
let editorSel = null;    // 断行编辑器拖选 {a, b}
let brkDrag = null;      // 换行点拖动 {gap}
let gapClick = null;     // 间隙点击候选 {gap, x, y}

const CELL_W = 7, CELL_H = 10;

// ---------------------------------------------------------------- 会话装载
async function refreshSessions(selectId) {
  const list = await api("/api/sessions");
  const sel = $("sess-select");
  sel.innerHTML = "";
  for (const s of list) {
    const o = document.createElement("option");
    o.value = s.id;
    o.textContent = "#" + s.id + " " + s.name +
      (s.status === "confirmed" ? "（已锁定）" : "");
    sel.appendChild(o);
  }
  if (selectId != null) sel.value = selectId;
  return list;
}

async function loadSession(sid) {
  S = await api("/api/session/" + sid + "/state");
  SID = sid;
  L = null;
  selectedToken = null;
  selectedUtt = null;
  anchorRows = S.anchors.map(a => ({ log_ts: a.log_ts, audio_ts: a.audio_ts }));
  stopAudio();
  renderAll();
  loadAudio();
  loadLayout();
}

function applyState(st) {
  S = st;
  anchorRows = S.anchors.map(a => ({ log_ts: a.log_ts, audio_ts: a.audio_ts }));
  renderAll();
  loadLayout();   // 词元指标/时刻可能变化，版面复核同步刷新
}

// ---------------------------------------------------------------- 渲染总览
function renderAll() {
  renderHeader();
  renderMetrics();
  renderFlags();
  renderAnchors();
  renderUtterances();
  renderRecess();
  renderRecomputeLog();
  drawWave();
  drawBand();
  renderDetail();
}

function renderHeader() {
  const locked = S.session.status === "confirmed";
  const badge = $("sess-status");
  badge.textContent = locked ? "已确认锁定" : "开放校审";
  badge.className = "badge " + (locked ? "confirmed" : "open");
  $("sess-digest").textContent = S.session.confirm_digest
    ? "摘要 " + S.session.confirm_digest.slice(0, 16) + "…" : "";
  $("btn-confirm").disabled = locked;
  const el = $("export-links");
  el.innerHTML = "";
  if (S.confirmation) {
    for (const [k, label] of [["vtt", "修正 WebVTT"], ["csv", "逐词 CSV"],
                              ["svg", "延迟曲线 SVG"], ["json", "复算 JSON"],
                              ["breaks_vtt", "断行 WebVTT"],
                              ["issues_csv", "问题 CSV"],
                              ["window_svg", "窗口预览 SVG"],
                              ["layout_json", "版面复算 JSON"]]) {
      if (!S.confirmation.exports || !S.confirmation.exports[k]) continue;
      const a = document.createElement("a");
      a.href = "/api/session/" + SID + "/export/" + k;
      a.textContent = label;
      a.setAttribute("download", "");
      el.appendChild(a);
    }
  }
}

function statLine(label, st, unit) {
  if (!st) return `<tr><td>${label}</td><td class="num undef">未定</td><td></td></tr>`;
  return `<tr><td>${label}</td><td class="num">` +
    `均值 ${st.mean}${unit} ｜ 中位 ${st.median} ｜ p90 ${st.p90} ｜ 最大 ${st.max}` +
    `</td><td class="num">n=${st.n}</td></tr>`;
}

function renderMetrics() {
  const ag = S.aggregates, iv = S.interval_metrics;
  if (!ag) { $("metrics").innerHTML = "<p class='hint'>计算中…</p>"; return; }
  let h = "<table>";
  h += statLine("首显延迟", ag.first_latency, "s");
  h += statLine("稳定延迟", ag.stable_latency, "s");
  const cnt = (v) => v == null
    ? `<span class="undef">未定（${reasonText(ag.counts_undefined_reason || "snapshot_gap")}）</span>`
    : `<span class="ok">${v}</span>`;
  h += `<tr><td>改写总次数</td><td class="num">${cnt(ag.replace_total)}</td>` +
       `<td class="num">涉及词元 ${ag.rewritten_tokens == null ? "未定" : ag.rewritten_tokens}</td></tr>`;
  h += `<tr><td>撤回总次数</td><td class="num">${cnt(ag.retract_total)}</td>` +
       `<td class="num">涉及词元 ${ag.retracted_tokens == null ? "未定" : ag.retracted_tokens}</td></tr>`;
  const rs = iv && iv.reading_speed;
  h += "<tr><td>阅读速度(字/秒)</td><td class='num'>" +
    (rs && rs.defined
      ? `均值 ${rs.mean} ｜ p90 ${rs.p90} ｜ 峰值 ${rs.max}`
      : `<span class="undef">未定（${reasonText(rs && rs.reason || "no_anchor")}）</span>`) +
    "</td><td></td></tr>";
  const unc = iv && iv.uncaptioned;
  let uncTxt;
  if (unc && unc.defined) {
    uncTxt = unc.ranges.length
      ? unc.ranges.map(r => `${fmtS(r.start)}–${fmtS(r.end)}`).join("，") +
        `（共 ${unc.total}s）`
      : "无";
  } else {
    uncTxt = `<span class="undef">未定（${reasonText(unc && unc.reason || "audio_range_incomplete")}）</span>`;
  }
  h += `<tr><td>无字幕区间</td><td class="num" colspan="2">${uncTxt}</td></tr>`;
  const cov = iv && iv.coverage;
  h += "<tr><td>日志覆盖</td><td class='num' colspan='2'>" +
    (cov && cov.defined
      ? `<span class="ok">${fmtS(cov.start)}–${fmtS(cov.end)} / 音频 ${fmtS(cov.audio_duration)}</span>`
      : `<span class="undef">不完整（${cov && (cov.problems || []).join(",") || reasonText(cov && cov.reason || "")}）</span>`) +
    "</td></tr>";
  h += `<tr><td>词元</td><td class="num" colspan="2">共 ${ag.token_count}，计入 ${ag.counted}，` +
       `<span class="undef">未定 ${ag.undefined_tokens}</span></td></tr>`;
  h += "</table>";
  $("metrics").innerHTML = h;
}

function renderFlags() {
  const el = $("flags");
  if (!S.flags || !S.flags.length) {
    el.innerHTML = "<div class='none'>日志完整，未见异常。</div>";
    return;
  }
  el.innerHTML = S.flags.map(f =>
    `<div class="flag ${f.type === "ambiguous_alignment" ? "warn" : ""}">` +
    `<b>${reasonText(f.type)}</b> ${f.detail || ""}</div>`).join("");
}

// ---------------------------------------------------------------- 锚点
function renderAnchors() {
  const tb = document.querySelector("#anchor-table tbody");
  tb.innerHTML = "";
  const residuals = (S.clock && S.clock.residuals) || [];
  anchorRows.forEach((a, k) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><input type="number" step="0.001" value="${a.log_ts}" data-k="${k}" data-f="log_ts"></td>` +
      `<td><input type="number" step="0.001" value="${a.audio_ts}" data-k="${k}" data-f="audio_ts"></td>` +
      `<td class="num">${residuals[k] != null ? residuals[k].toFixed(3) + "s" : "—"}</td>` +
      `<td><button data-del="${k}">删</button></td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll("input").forEach(inp => inp.addEventListener("change", () => {
    anchorRows[+inp.dataset.k][inp.dataset.f] = parseFloat(inp.value);
  }));
  tb.querySelectorAll("button[data-del]").forEach(b =>
    b.addEventListener("click", () => {
      anchorRows.splice(+b.dataset.del, 1);
      renderAnchors();
    }));
  const msg = [];
  if (!S.clock) msg.push("尚无锚点：延迟类指标全部未定。");
  else if (S.residual_exceeded)
    msg.push(`最大残差 ${S.clock.max_residual.toFixed(3)}s 超过阈值 ` +
             `${S.params.anchor_residual_max}s：延迟类指标未定。`);
  else msg.push(`拟合 a=${S.clock.a.toFixed(6)} b=${S.clock.b.toFixed(3)}，` +
                `最大残差 ${S.clock.max_residual.toFixed(3)}s。`);
  $("anchor-msg").textContent = msg.join("");
}

// ---------------------------------------------------------------- 话语
function renderUtterances() {
  const el = $("utterances");
  el.innerHTML = "";
  S.utterances.forEach((u, k) => {
    const div = document.createElement("div");
    div.className = "utt" + (selectedUtt === k ? " sel" : "");
    div.innerHTML = `<span class="idx">#${k}</span>` +
      `<span class="txt" title="${u.text}">${u.text}</span>` +
      (k < S.utterances.length - 1
        ? `<button data-m="${k}" ${locked()}>合并↓</button>` : "");
    div.addEventListener("click", (e) => {
      if (e.target.tagName === "BUTTON") return;
      selectedUtt = selectedUtt === k ? null : k;
      renderUtterances();
      drawBand();
    });
    el.appendChild(div);
  });
  el.querySelectorAll("button[data-m]").forEach(b =>
    b.addEventListener("click", async () => {
      applyState(await post(`/api/session/${SID}/merge`,
                            { utterance: +b.dataset.m }));
    }));
}

const locked = () => S.session.status === "confirmed" ? "disabled" : "";

// ---------------------------------------------------------------- 休会段
function renderRecess() {
  const el = $("recess-list");
  el.innerHTML = "";
  S.recess.forEach((r, k) => {
    const s = document.createElement("span");
    s.textContent = `休会 ${fmtS(r.start)}–${fmtS(r.end)} ✕`;
    s.title = "点击删除该休会段";
    s.addEventListener("click", async () => {
      if (S.session.status !== "open") return;
      const ranges = S.recess.filter((_, i) => i !== k);
      applyState(await put(`/api/session/${SID}/recess`, { ranges }));
    });
    el.appendChild(s);
  });
  $("btn-recess-mode").className = recessMode ? "active" : "";
}

// ---------------------------------------------------------------- 重算记录
function renderRecomputeLog() {
  const el = $("recompute-log");
  const log = (S.recompute_log || []).slice(-10).reverse();
  el.innerHTML = log.map(e => {
    const rng = e.affected_log_range
      ? `，影响日志区间 [${e.affected_log_range[0] == null ? "-∞" : e.affected_log_range[0].toFixed(1)}, ` +
        `${e.affected_log_range[1] == null ? "+∞" : e.affected_log_range[1].toFixed(1)}]`
      : "";
    return `<div>#${e.n} ${e.reason}：${e.scope === "all" ? "全量" : "部分"}重算，` +
      `词元 ${e.tokens_recomputed} 个${e.intervals ? "，含区间指标" : ""}${rng}</div>`;
  }).join("") || "<div class='hint'>暂无</div>";
}

// ---------------------------------------------------------------- 音频
async function loadAudio() {
  try {
    const buf = await (await fetch(`/api/session/${SID}/audio.wav`)).arrayBuffer();
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    audioBuf = await audioCtx.decodeAudioData(buf);
    const ch = audioBuf.getChannelData(0);
    const n = 800;
    peaks = new Float32Array(n);
    const step = Math.floor(ch.length / n);
    for (let i = 0; i < n; i++) {
      let m = 0;
      for (let k = i * step; k < (i + 1) * step && k < ch.length; k += 16)
        m = Math.max(m, Math.abs(ch[k]));
      peaks[i] = m;
    }
    drawWave();
  } catch (e) {
    console.warn("音频加载失败", e);
  }
}

function currentTime() {
  if (!audioBuf) return 0;
  let t = playing ? playStartOff + (audioCtx.currentTime - playStartCtx)
                  : playStartOff;
  return Math.min(Math.max(t, 0), audioBuf.duration);
}

function drawWave() {
  const cv = $("wave");
  const wrap = cv.parentElement;
  const W = wrap.clientWidth || 600;
  cv.width = W;
  cv.height = 90;
  const g = cv.getContext("2d");
  g.fillStyle = "#0b101c";
  g.fillRect(0, 0, W, 90);
  if (!S) return;
  const dur = S.session.audio_duration || 1;
  const X = (t) => t / dur * W;
  // 休会段
  g.fillStyle = "rgba(240,201,106,.14)";
  for (const r of S.recess) g.fillRect(X(r.start), 0, X(r.end) - X(r.start), 90);
  // 缺页/倒序区间
  g.fillStyle = "rgba(176,48,48,.18)";
  const snaps = S.snapshots;
  for (let i = 1; i < snaps.length; i++) {
    if ((snaps[i].gap_before || snaps[i].mono_before) &&
        snaps[i - 1].audio_ts != null && snaps[i].audio_ts != null) {
      g.fillRect(X(snaps[i - 1].audio_ts), 0,
                 Math.max(1, X(snaps[i].audio_ts) - X(snaps[i - 1].audio_ts)), 90);
    }
  }
  // 波形
  if (peaks) {
    g.strokeStyle = "#4f7cc0";
    g.beginPath();
    for (let x = 0; x < W; x++) {
      const p = peaks[Math.floor(x / W * peaks.length)] || 0;
      const h = Math.max(1, p * 80);
      g.moveTo(x + 0.5, 45 - h / 2);
      g.lineTo(x + 0.5, 45 + h / 2);
    }
    g.stroke();
  }
  // 无字幕区间（底部红条）
  const unc = S.interval_metrics && S.interval_metrics.uncaptioned;
  if (unc && unc.defined) {
    g.fillStyle = "rgba(176,48,48,.7)";
    for (const r of unc.ranges)
      g.fillRect(X(r.start), 82, Math.max(1, X(r.end) - X(r.start)), 5);
  }
}

function waveXToTime(x) {
  const W = $("wave").width;
  return x / W * (S.session.audio_duration || 0);
}

function seekTo(t) {
  playStartOff = Math.min(Math.max(t, 0), audioBuf ? audioBuf.duration : 0);
  if (playing) { stopNode(); startNode(); }
  updatePlayheads();
}

function startNode() {
  srcNode = audioCtx.createBufferSource();
  srcNode.buffer = audioBuf;
  srcNode.connect(audioCtx.destination);
  srcNode.start(0, playStartOff);
  playStartCtx = audioCtx.currentTime;
  srcNode.onended = () => { if (playing && currentTime() >= audioBuf.duration - 0.05) stopAudio(); };
}
function stopNode() { try { srcNode && srcNode.stop(); } catch (e) { /* */ } srcNode = null; }
function stopAudio() {
  if (playing) { playStartOff = currentTime(); }
  playing = false;
  stopNode();
  $("btn-play").textContent = "▶ 播放";
}

// ---------------------------------------------------------------- 演变带
function runAt(runs, i) {
  for (const r of runs) if (i >= r[0] && i < r[1]) return r;
  return null;
}
function lastRunEnd(runs) {
  return runs.length ? runs[runs.length - 1][1] : -1;
}
function cellState(tok, i) {
  const run = runAt(tok.runs, i);
  if (!run) {
    if (tok.first_display_idx != null && i > tok.first_display_idx &&
        i < lastRunEnd(tok.runs)) return "retracted";
    return "absent";
  }
  if (run[3] === tok.norm)
    return (tok.stable_idx != null && i >= tok.stable_idx) ? "stable" : "unstable";
  return "draft";
}
const COLORS = {
  absent: "#1c2333", draft: "#d97b2f", unstable: "#d9b82f",
  stable: "#2f9e54", retracted: "#a83232",
};

function drawBand() {
  const cv = $("band");
  if (!S) return;
  const ns = S.snapshots.length, nt = S.tokens.length;
  cv.width = ns * CELL_W;
  cv.height = nt * CELL_H;
  const g = cv.getContext("2d");
  for (let j = 0; j < nt; j++) {
    const tok = S.tokens[j];
    let x = 0;
    // 按游程与状态逐段填色，避免逐格查询
    for (let i = 0; i < ns; i++) {
      g.fillStyle = COLORS[cellState(tok, i)];
      g.fillRect(i * CELL_W, j * CELL_H, CELL_W - 1, CELL_H - 1);
    }
  }
  // 缺页列标出
  g.fillStyle = "rgba(176,48,48,.25)";
  S.snapshots.forEach((s, i) => {
    if (s.gap_before || s.mono_before)
      g.fillRect(i * CELL_W, 0, 2, nt * CELL_H);
  });
  // 选中词元 / 话语高亮
  if (selectedToken != null) {
    g.strokeStyle = "#6db3f2";
    g.lineWidth = 1.5;
    g.strokeRect(0.5, selectedToken * CELL_H + 0.5, ns * CELL_W - 1, CELL_H - 1);
  }
  if (selectedUtt != null) {
    const u = S.utterances[selectedUtt];
    g.strokeStyle = "rgba(109,179,242,.5)";
    g.strokeRect(0.5, u.start * CELL_H + 0.5, ns * CELL_W - 1,
                 (u.end - u.start) * CELL_H - 1);
  }
}

function bandPos(e) {
  const cv = $("band");
  const r = cv.getBoundingClientRect();
  return {
    i: Math.floor((e.clientX - r.left) / CELL_W),
    j: Math.floor((e.clientY - r.top) / CELL_H),
    x: e.clientX - r.left, y: e.clientY - r.top,
  };
}

function snapXAtTime(t) {
  const snaps = S.snapshots;
  if (!snaps.length || snaps[0].audio_ts == null) return null;
  if (t <= snaps[0].audio_ts) return 0;
  for (let i = 1; i < snaps.length; i++) {
    const a = snaps[i - 1].audio_ts, b = snaps[i].audio_ts;
    if (a != null && b != null && t <= b)
      return (i - 1 + (b > a ? (t - a) / (b - a) : 0)) * CELL_W;
  }
  return (snaps.length - 1) * CELL_W;
}

// ---------------------------------------------------------------- 词元详情
function renderDetail() {
  const el = $("token-detail");
  if (selectedToken == null || !S.tokens[selectedToken]) {
    el.innerHTML = "<p class='hint'>在演变带中点击一个词元。</p>";
    return;
  }
  const t = S.tokens[selectedToken];
  const lat = (v, undef) => v != null ? fmtSigned(v)
    : `<span class="undef">未定${undef.length ? "（" + undef.map(reasonText).join("、") + "）" : ""}</span>`;
  const cnt = (v) => v != null ? v
    : `<span class="undef">未定（快照缺页）</span>`;
  let h = `<div class="kv">`;
  h += `<div><b>词元</b>#${t.j} 「${t.text}」 <span class="status-tag ${t.status}">${t.status}</span></div>`;
  h += `<div><b>参照</b>${t.ref_text != null ? `「${t.ref_text}」 @ ${fmtS(t.ref_time)}` : "—"}</div>`;
  h += `<div><b>首显</b>${fmtS(t.first_display_t)}　<b>首显延迟</b>${lat(t.first_latency, t.undefined)}</div>`;
  h += `<div><b>稳定</b>${fmtS(t.stable_t)}　<b>稳定延迟</b>${lat(t.stable_latency, t.undefined)}</div>`;
  h += `<div><b>改写</b>${cnt(t.replace_count)}　<b>撤回</b>${cnt(t.retract_count)}</div>`;
  if (t.flags.length) h += `<div><b>标记</b>${t.flags.join("、")}</div>`;
  h += `</div>`;
  h += `<div class="runs">` + t.runs.map(r => {
    const a = S.snapshots[r[0]], b = S.snapshots[Math.min(r[1] - 1, S.snapshots.length - 1)];
    const cur = r[3] === t.norm ? " class='cur'" : "";
    return `<span${cur}>seq ${a.seq}–${b.seq} 「${r[2]}」</span>`;
  }).join("") + `</div>`;
  // 重绑 + 拆分
  const near = nearbyRefTokens(t);
  h += `<div class="ops">` +
    `<input type="text" id="rebind-filter" placeholder="筛选参考词" size="8">` +
    `<select id="rebind-sel">${near.map(r =>
      `<option value="${r.i}" ${r.i === t.ref_idx ? "selected" : ""}>` +
      `#${r.i} ${r.text}（${fmtS(r.time)}）</option>`).join("")}</select>` +
    `<button id="btn-rebind" ${locked()}>重绑到所选</button>` +
    (t.status === "rebind"
      ? `<button id="btn-unbind" ${locked()}>解除重绑</button>` : "") +
    `<button id="btn-split" ${locked()}>在此词元前拆分话语</button>` +
    `</div>`;
  el.innerHTML = h;
  $("rebind-filter").addEventListener("input", (e) => {
    const q = e.target.value.trim();
    const sel = $("rebind-sel");
    const list = q ? S.ref_tokens.filter(r => r.text.includes(q)).slice(0, 60)
                   : nearbyRefTokens(t);
    sel.innerHTML = list.map(r =>
      `<option value="${r.i}">#${r.i} ${r.text}（${fmtS(r.time)}）</option>`).join("");
  });
  $("btn-rebind").addEventListener("click", async () => {
    const ref = +$("rebind-sel").value;
    applyState(await post(`/api/session/${SID}/rebind`, { token: t.j, ref }));
  });
  const unb = $("btn-unbind");
  if (unb) unb.addEventListener("click", async () => {
    applyState(await post(`/api/session/${SID}/rebind`, { token: t.j, ref: null }));
  });
  $("btn-split").addEventListener("click", async () => {
    applyState(await post(`/api/session/${SID}/split`, { token: t.j }));
  });
}

function nearbyRefTokens(t) {
  const c = t.ref_idx != null ? t.ref_idx : 0;
  return S.ref_tokens.filter(r => Math.abs(r.i - c) <= 12);
}

// ---------------------------------------------------------------- 播放头联动
function updatePlayheads() {
  const t = currentTime();
  const dur = S ? S.session.audio_duration : 1;
  const wp = $("wave-playhead");
  wp.style.display = "block";
  wp.style.left = (t / dur * 100) + "%";
  const bp = $("band-playhead");
  const x = S ? snapXAtTime(t) : null;
  if (x != null) {
    bp.style.display = "block";
    bp.style.left = x + "px";
  } else bp.style.display = "none";
  $("time-label").textContent =
    t.toFixed(2) + " / " + (audioBuf ? audioBuf.duration.toFixed(2) : (dur || 0).toFixed(2));
}

function tick() {
  if (S) updatePlayheads();
  if (S && L) drawCaption();
  requestAnimationFrame(tick);
}

// ---------------------------------------------------------------- 事件绑定
function bind() {
  $("sess-select").addEventListener("change", (e) => loadSession(+e.target.value));
  $("btn-new-demo").addEventListener("click", async () => {
    const r = await post("/api/demo");
    await refreshSessions(r.id);
    loadSession(r.id);
  });
  $("btn-confirm").addEventListener("click", async () => {
    if (!confirm("确认后将锁定日志摘要、对齐与全部人工决定，并生成导出文件。")) return;
    try {
      await post(`/api/session/${SID}/confirm`);
      applyState(await api(`/api/session/${SID}/state`));
    } catch (e) { alert(e.message); }
  });
  $("btn-play").addEventListener("click", async () => {
    if (!audioBuf) return;
    if (audioCtx.state === "suspended") await audioCtx.resume();
    if (playing) { stopAudio(); }
    else {
      if (playStartOff >= audioBuf.duration - 0.05) playStartOff = 0;
      playing = true;
      startNode();
      $("btn-play").textContent = "⏸ 暂停";
    }
  });
  $("btn-recess-mode").addEventListener("click", () => {
    recessMode = !recessMode;
    renderRecess();
  });

  // 波形：点击定位 / 休会拖选
  const wave = $("wave");
  wave.addEventListener("mousedown", (e) => {
    const r = wave.getBoundingClientRect();
    drag = { x0: e.clientX - r.left, x1: e.clientX - r.left, moved: false };
  });
  window.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const r = wave.getBoundingClientRect();
    drag.x1 = Math.min(Math.max(e.clientX - r.left, 0), r.width);
    drag.moved = drag.moved || Math.abs(drag.x1 - drag.x0) > 3;
    if (recessMode && drag.moved) {
      const sel = $("wave-sel");
      sel.style.display = "block";
      sel.style.left = Math.min(drag.x0, drag.x1) + "px";
      sel.style.width = Math.abs(drag.x1 - drag.x0) + "px";
    }
  });
  window.addEventListener("mouseup", async () => {
    if (!drag) return;
    const d = drag;
    drag = null;
    $("wave-sel").style.display = "none";
    if (recessMode && d.moved && S.session.status === "open") {
      const t0 = waveXToTime(Math.min(d.x0, d.x1));
      const t1 = waveXToTime(Math.max(d.x0, d.x1));
      if (t1 - t0 > 0.2) {
        const ranges = S.recess.concat([{ start: +t0.toFixed(3), end: +t1.toFixed(3) }]);
        applyState(await put(`/api/session/${SID}/recess`, { ranges }));
      }
      recessMode = false;
      renderRecess();
    } else if (!recessMode) {
      seekTo(waveXToTime(d.x1));
    }
  });

  // 演变带：点击选词元，悬停提示
  const band = $("band");
  band.addEventListener("click", (e) => {
    const p = bandPos(e);
    if (p.j >= 0 && p.j < S.tokens.length) {
      selectedToken = p.j;
      drawBand();
      renderDetail();
    }
  });
  band.addEventListener("mousemove", (e) => {
    const p = bandPos(e);
    const tip = $("band-tooltip");
    if (p.j >= 0 && p.j < S.tokens.length && p.i >= 0 && p.i < S.snapshots.length) {
      const t = S.tokens[p.j], s = S.snapshots[p.i];
      const run = runAt(t.runs, p.i);
      tip.style.display = "block";
      tip.style.left = (p.x + 14) + "px";
      tip.style.top = (p.y + 10) + "px";
      tip.textContent = `#${p.j}「${t.text}」 seq ${s.seq}` +
        (s.audio_ts != null ? ` @ ${s.audio_ts.toFixed(2)}s` : "") +
        `：${run ? "显示「" + run[2] + "」" : "未显示"}`;
    } else tip.style.display = "none";
  });
  band.addEventListener("mouseleave", () => { $("band-tooltip").style.display = "none"; });

  // 锚点
  $("btn-add-anchor").addEventListener("click", () => {
    anchorRows.push({ log_ts: 0, audio_ts: 0 });
    renderAnchors();
  });
  $("btn-save-anchors").addEventListener("click", async () => {
    const anchors = anchorRows
      .filter(a => isFinite(a.log_ts) && isFinite(a.audio_ts))
      .map(a => ({ log_ts: +a.log_ts, audio_ts: +a.audio_ts }));
    try {
      applyState(await put(`/api/session/${SID}/anchors`, { anchors }));
    } catch (e) { alert(e.message); }
  });

  // 材料上传
  $("btn-toggle-upload").addEventListener("click", () => {
    const f = $("upload-form");
    f.hidden = !f.hidden;
  });
  $("upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("upload-msg");
    msg.textContent = "上传并计算中…";
    try {
      const fd = new FormData(e.target);
      const r = await api("/api/session/import", { method: "POST", body: fd });
      msg.textContent = "导入成功。";
      await refreshSessions(r.id);
      loadSession(r.id);
    } catch (err) {
      msg.textContent = "导入失败：" + err.message;
    }
  });

  window.addEventListener("resize", () => { drawWave(); });
}

// ---------------------------------------------------------------- 启动
(async function boot() {
  bind();
  const list = await refreshSessions();
  let sid = list.length ? list[list.length - 1].id : null;
  if (sid == null) {
    const r = await post("/api/demo");
    await refreshSessions(r.id);
    sid = r.id;
  }
  $("sess-select").value = sid;
  await loadSession(sid);
  requestAnimationFrame(tick);
})();


// ================================================================ 断行与滚屏复核
// 版面设置 / Canvas 字幕窗口回放 / 断行编辑器 / 呈现指标 / 可读性问题

const ISSUE_LABEL = {
  overwide: "超宽", orphan_line: "孤行", lock_split: "锁定单元被拆",
  dwell_short: "驻留不足", snapshot_gap: "快照缺页",
  font_metrics_missing: "字体度量缺失",
};

async function loadLayout() {
  try {
    L = await api("/api/session/" + SID + "/layout");
  } catch (e) {
    L = null;
    console.warn("版面状态加载失败", e);
    return;
  }
  renderLayoutAll();
}

function renderLayoutAll() {
  if (!L) return;
  renderLayoutSettings();
  renderEditor();
  renderLayoutMetrics();
  renderLayoutIssues();
  drawCaption();
}

async function saveLayout(patch) {
  const msg = $("editor-msg");
  try {
    L = await post("/api/session/" + SID + "/layout/revise", patch);
    if (msg) msg.textContent = "已保存为修订 #" + L.revision +
      "（共 " + L.revisions.length + " 条）";
    renderLayoutAll();
  } catch (e) {
    if (msg) msg.textContent = "保存失败：" + e.message;
  }
}

// ---------------------------------------------------------------- 版面设置
function renderLayoutSettings() {
  const el = $("layout-settings");
  const st = L.settings;
  const fonts = L.fonts.map(f => f.family);
  if (!fonts.includes(st.font_family)) fonts.unshift(st.font_family);
  el.innerHTML =
    '<div class="row">' +
    '<label>画幅 <select id="ls-aspect">' +
      Object.keys(L.aspects).map(a =>
        `<option ${a === st.aspect ? "selected" : ""}>${a}</option>`).join("") +
    '</select></label>' +
    '<label>模式 <select id="ls-mode">' +
      `<option value="scroll" ${st.mode === "scroll" ? "selected" : ""}>逐行滚动</option>` +
      `<option value="replace" ${st.mode === "replace" ? "selected" : ""}>整屏替换</option>` +
    '</select></label>' +
    `<label>行数 <input id="ls-lines" type="number" min="1" max="6" value="${st.lines}"></label>` +
    `<label>字号 <input id="ls-fontsize" type="number" min="8" max="120" value="${st.font_size}"> px</label>` +
    `<label>可用行宽 <input id="ls-width" type="number" min="100" max="4000" step="10" value="${st.line_width}"> px</label>` +
    `<label>最短驻留 <input id="ls-dwell" type="number" min="0" max="10" step="0.1" value="${st.min_dwell}"> s</label>` +
    '</div><div class="row">' +
    `<label>字体 <input id="ls-font" list="ls-fonts" value="${st.font_family}" style="width:180px">` +
    '<datalist id="ls-fonts">' +
      fonts.map(f => `<option value="${f}">`).join("") + '</datalist></label>' +
    `<span id="ls-font-state" class="font-state ${L.font.metrics ? "ok" : "missing"}">` +
      (L.font.metrics
        ? "度量可用（" + (L.font.source === "browser" ? "浏览器实测" : "内置") + "）"
        : "字体度量缺失：行宽占用与超宽检查未定") + '</span>' +
    `<button id="ls-measure">用浏览器测量字体</button>` +
    `<button id="ls-save" class="primary" ${locked()}>保存为修订</button>` +
    `<span class="hint">修订 #${L.revision}（共 ${L.revisions.length} 条）</span>` +
    '</div>';
  $("ls-save").addEventListener("click", () => {
    saveLayout({ settings: {
      aspect: $("ls-aspect").value,
      mode: $("ls-mode").value,
      lines: +$("ls-lines").value,
      font_size: +$("ls-fontsize").value,
      line_width: +$("ls-width").value,
      min_dwell: +$("ls-dwell").value,
      font_family: $("ls-font").value.trim(),
    }});
  });
  $("ls-measure").addEventListener("click", measureFont);
}

// Canvas measureText 探针：以 100px 字号测量各类字符平均宽度（em 相对值）
async function measureFont() {
  const fam = ($("ls-font") ? $("ls-font").value.trim() : "") ||
              L.settings.font_family;
  const g = document.createElement("canvas").getContext("2d");
  const PX = 100;
  g.font = PX + "px " + fam;
  const avg = (s) => g.measureText(s).width / PX / s.length;
  const units = {
    cjk: avg("汉字符测量平均宽度样本"),
    latin: avg("abcdefghijklmnopqrstuvwxyz"),
    digit: avg("0123456789"),
    punct_cjk: avg("，。、；：？！"),
    punct_ascii: avg(".,;:!?()[]-"),
    space: (g.measureText("i i").width - g.measureText("ii").width) / PX,
    overrides: {},
  };
  try {
    await put("/api/fonts/" + encodeURIComponent(fam) + "/metrics", { units });
    await loadLayout();
  } catch (e) {
    $("ls-font-state").textContent = "测量保存失败：" + e.message;
  }
}

// ---------------------------------------------------------------- 断行编辑器
function renderEditor() {
  const el = $("line-editor");
  el.innerHTML = "";
  const toks = L.result.tokens, lines = L.result.lines;
  const brks = new Set(L.result.breaks);
  const lockOf = {};
  L.result.locks.forEach((lk, i) => {
    for (let j = lk[0]; j < lk[1]; j++) lockOf[j] = i;
  });
  const splitLocks = new Set();
  for (const it of L.result.issues) {
    if (it.type === "lock_split" && it.lock) {
      for (let j = it.lock[0]; j < it.lock[1]; j++) splitLocks.add(j);
    }
  }
  const lineStart = new Set(lines.map(ln => ln.start));

  const mkGap = (j) => {          // 词元 j 之前的间隙（j-1 与 j 之间）
    const g = document.createElement("span");
    g.className = "gap";
    g.dataset.gap = j;
    if (j > 0 && (brks.has(j) || lineStart.has(j))) {
      const b = document.createElement("span");
      b.className = "brk " + (brks.has(j) ? "manual" : "auto");
      b.dataset.gap = j;
      b.title = brks.has(j) ? "手动换行点：拖动移动，点击间隙取消"
                            : "自动换行点：可拖动调整";
      g.appendChild(b);
    }
    return g;
  };

  for (const ln of lines) {
    const row = document.createElement("div");
    row.className = "lrow" + (ln.orphan ? " orphan-row" : "");
    const no = document.createElement("span");
    no.className = "lno";
    no.textContent = "行" + (ln.idx + 1);
    row.appendChild(no);
    for (let j = ln.start; j < ln.end; j++) {
      if (j > ln.start) row.appendChild(mkGap(j));
      const c = document.createElement("span");
      c.className = "tok";
      c.textContent = toks[j].text;
      c.dataset.j = j;
      if (lockOf[j] != null) {
        const lk = L.result.locks[lockOf[j]];
        c.classList.add("locked");
        if (j === lk[0]) c.classList.add("lock-head");
        if (j === lk[1] - 1) c.classList.add("lock-tail");
        c.title = "锁定单元 #" + (lockOf[j] + 1) + "（不可拆；点击解锁）";
      }
      if (splitLocks.has(j)) {
        c.classList.add("lock-split");
        c.title = "该锁定单元被换行点拆开（可读性未定）";
      }
      row.appendChild(c);
    }
    if (ln.end < toks.length) row.appendChild(mkGap(ln.end));
    const usage = document.createElement("span");
    usage.className = "usage" + (ln.overwide ? " over" : "");
    usage.textContent = ln.usage == null ? "行宽未定"
      : Math.round(ln.usage * 100) + "%";
    if (ln.overwide) usage.title = "超宽：" + ln.width + "px > " +
      L.settings.line_width + "px";
    row.appendChild(usage);
    el.appendChild(row);
  }
  if (!L.locked) bindEditor(el);
}

function bindEditor(el) {
  el.querySelectorAll(".gap").forEach(g => {
    g.addEventListener("mousedown", (e) => {
      if (e.target.classList.contains("brk")) return;   // 拖动优先
      gapClick = { gap: +g.dataset.gap, x: e.clientX, y: e.clientY };
    });
    g.addEventListener("mouseenter", () => {
      if (brkDrag) g.classList.add("drop-target");
    });
    g.addEventListener("mouseleave", () => g.classList.remove("drop-target"));
  });
  el.querySelectorAll(".brk").forEach(b => {
    b.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      brkDrag = { gap: +b.dataset.gap };
      b.classList.add("dragging");
    });
  });
  el.querySelectorAll(".tok").forEach(c => {
    c.addEventListener("mousedown", (e) => {
      e.preventDefault();
      editorSel = { a: +c.dataset.j, b: +c.dataset.j };
    });
    c.addEventListener("mouseenter", () => {
      if (editorSel) {
        editorSel.b = +c.dataset.j;
        highlightSel();
      }
    });
  });
}

function highlightSel() {
  if (!editorSel) return;
  const a = Math.min(editorSel.a, editorSel.b);
  const b = Math.max(editorSel.a, editorSel.b);
  document.querySelectorAll("#line-editor .tok").forEach(c => {
    const j = +c.dataset.j;
    c.classList.toggle("sel", j >= a && j <= b);
  });
}

// 编辑器鼠标释放：换行点拖放 / 间隙点击切换 / 拖选锁定 / 单击解锁
window.addEventListener("mouseup", async (e) => {
  if (brkDrag) {
    const from = brkDrag.gap;
    brkDrag = null;
    document.querySelectorAll(".brk.dragging")
      .forEach(x => x.classList.remove("dragging"));
    const tgt = document.querySelector(".gap.drop-target");
    document.querySelectorAll(".gap.drop-target")
      .forEach(x => x.classList.remove("drop-target"));
    if (tgt) {
      const to = +tgt.dataset.gap;
      if (to !== from && to > 0) {
        const breaks = L.result.breaks.filter(b => b !== from);
        if (!breaks.includes(to)) breaks.push(to);
        await saveLayout({ breaks });
      }
    }
    return;
  }
  if (gapClick) {
    const gc = gapClick;
    gapClick = null;
    if (Math.abs(e.clientX - gc.x) < 4 && Math.abs(e.clientY - gc.y) < 4 &&
        gc.gap > 0) {
      const breaks = L.result.breaks.slice();
      const i = breaks.indexOf(gc.gap);
      if (i >= 0) breaks.splice(i, 1); else breaks.push(gc.gap);
      await saveLayout({ breaks });
    }
    return;
  }
  if (editorSel) {
    const sel = editorSel;
    editorSel = null;
    document.querySelectorAll("#line-editor .tok.sel")
      .forEach(c => c.classList.remove("sel"));
    const a = Math.min(sel.a, sel.b), b = Math.max(sel.a, sel.b);
    if (a !== b) {                    // 拖选多个词元 → 锁成不可拆单元
      const overlap = L.result.locks.some(lk => a < lk[1] && lk[0] <= b);
      if (overlap) {
        $("editor-msg").textContent = "与现有锁定单元重叠，请先点击解锁。";
        return;
      }
      await saveLayout({ locks: L.result.locks.concat([[a, b + 1]]) });
    } else {                          // 单击锁内词元 → 解锁
      const k = L.result.locks.findIndex(lk => lk[0] <= a && a < lk[1]);
      if (k >= 0) {
        await saveLayout({ locks: L.result.locks.filter((_, i) => i !== k) });
      }
    }
  }
});

// ---------------------------------------------------------------- 呈现指标
function renderLayoutMetrics() {
  const el = $("layout-metrics");
  const ag = L.result.aggregates;
  const d = ag.dwell;
  const wu = ag.width_usage;
  const modeTxt = L.settings.mode === "scroll" ? "滚动" : "翻屏";
  let h = "<table>";
  h += `<tr><td>行数 / 呈现</td><td class="num">${ag.lines} 行 / ` +
       `${ag.presentations} 次（${modeTxt}，窗口 ${L.settings.lines} 行）</td></tr>`;
  h += `<tr><td>驻留时长</td><td class="num">` +
    (d.defined
      ? `最短 ${d.min}s ｜ 均值 ${d.mean}s ｜ 最长 ${d.max}s`
      : '<span class="undef">未定</span>') + `　驻留不足 ${ag.dwell_short} 次</td></tr>`;
  h += `<tr><td>滚屏频率</td><td class="num">${ag.scroll_events} 次` +
    (ag.scroll_per_min != null ? `（${ag.scroll_per_min} 次/分）`
                               : '（<span class="undef">频率未定</span>）') +
    `</td></tr>`;
  h += `<tr><td>回读距离</td><td class="num">共 ${ag.re_read_total} 字 ｜ ` +
       `单次最大 ${ag.re_read_max} 字</td></tr>`;
  h += `<tr><td>行宽占用</td><td class="num">` +
    (wu.defined
      ? `峰值 ${Math.round(wu.max * 100)}% ｜ 均值 ${Math.round(wu.mean * 100)}%`
      : '<span class="undef">未定（字体度量缺失）</span>') + `</td></tr>`;
  h += `<tr><td>可读性</td><td class="num">` +
    (L.result.readability === "ok"
      ? '<span class="readability ok">通过</span>'
      : '<span class="readability undefined">未定</span>') +
    `　未定呈现 ${ag.undefined_presentations}/${ag.presentations}</td></tr>`;
  h += "</table>";
  el.innerHTML = h;

  const strip = $("pres-strip");
  strip.innerHTML = "";
  for (const p of L.result.presentations) {
    const d2 = document.createElement("div");
    d2.className = "pres" + (p.readability !== "ok" ? " bad" : "") +
      (p.dwell == null ? " undef" : "");
    d2.style.width = (p.dwell != null
      ? Math.max(8, Math.min(120, p.dwell * 14)) : 10) + "px";
    const b = p.begin == null ? "?" : p.begin.toFixed(2);
    const e2 = p.end == null ? "?" : p.end.toFixed(2);
    d2.title = `#${p.idx} ${b}–${e2}s 驻留 ` +
      (p.dwell == null ? "未定" : p.dwell.toFixed(2) + "s") +
      (p.undefined.length ? "｜未定：" + p.undefined.join("、") : "");
    if (p.begin != null) d2.addEventListener("click", () => seekTo(p.begin));
    strip.appendChild(d2);
  }
}

// ---------------------------------------------------------------- 可读性问题
function renderLayoutIssues() {
  const el = $("layout-issues");
  const iss = L.result.issues;
  if (!iss.length) {
    el.innerHTML = "<div class='none'>未发现可读性问题。</div>";
    return;
  }
  el.innerHTML = "";
  for (const it of iss) {
    const div = document.createElement("div");
    div.className = "issue";
    const t0 = it.start == null ? "—" : it.start.toFixed(2) + "s";
    const t1 = it.end == null ? "" : "–" + it.end.toFixed(2) + "s";
    div.innerHTML = `<span class="t">#${it.id} ${ISSUE_LABEL[it.type] || it.type}` +
      `</span><span>${it.detail}</span><span class="t">${t0}${t1}</span>`;
    if (it.start != null) {
      const b = document.createElement("button");
      b.textContent = "定位";
      b.addEventListener("click", () => seekTo(it.start));
      div.appendChild(b);
    }
    el.appendChild(div);
  }
}

// ---------------------------------------------------------------- 字幕窗口回放
function drawCaption() {
  const cv = $("caption");
  if (!cv || !L || !S) return;
  const W = (cv.parentElement.clientWidth || 600) - 24;
  const H = 300;
  if (cv.width !== W) cv.width = W;
  cv.height = H;
  const g = cv.getContext("2d");
  const st = L.result.settings;
  const ar = (L.aspects && L.aspects[st.aspect]) || [16, 9];
  g.fillStyle = "#05070d";
  g.fillRect(0, 0, W, H);
  // 视频帧（按画幅）
  let fh = H - 16, fw = fh * ar[0] / ar[1];
  if (fw > W - 16) { fw = W - 16; fh = fw * ar[1] / ar[0]; }
  const fx = (W - fw) / 2, fy = (H - fh) / 2;
  g.fillStyle = "#10141f";
  g.fillRect(fx, fy, fw, fh);
  g.strokeStyle = "#2a3450";
  g.strokeRect(fx + 0.5, fy + 0.5, fw - 1, fh - 1);
  const t = currentTime();
  // 字幕窗口：假定字幕区占帧宽 86%
  const scale = fw / (st.line_width / 0.86);
  const winW = st.line_width * scale;
  const fs = st.font_size * scale;
  const lineH = fs * 1.5;
  const winH = st.lines * lineH;
  const wx = fx + (fw - winW) / 2;
  const wy = fy + fh - winH - fh * 0.05;
  // 依快照时钟定位当前呈现
  const pres = L.result.presentations || [];
  let cur = null;
  for (const p of pres) {
    if (p.begin != null && p.begin <= t && (p.end == null || t < p.end)) {
      cur = p;
      break;
    }
  }
  if (!cur) {
    for (let i = pres.length - 1; i >= 0; i--) {
      if (pres[i].begin != null && pres[i].begin <= t) { cur = pres[i]; break; }
    }
  }
  const bad = cur && cur.readability !== "ok";
  g.fillStyle = "rgba(0,0,0,.55)";
  g.fillRect(wx, wy, winW, winH);
  g.strokeStyle = bad ? "#c0392b" : "rgba(255,255,255,.18)";
  g.lineWidth = bad ? 2 : 1;
  g.strokeRect(wx + 0.5, wy + 0.5, winW - 1, winH - 1);
  g.lineWidth = 1;
  if (cur) {
    g.save();
    g.beginPath();
    g.rect(fx, fy, fw, fh);
    g.clip();
    g.font = fs + "px " + st.font_family;
    g.textAlign = "center";
    g.textBaseline = "middle";
    const a = cur.lines[0], z = cur.lines[1];
    const shown = L.result.lines.slice(a, z + 1).slice(-st.lines);
    shown.forEach((ln, r) => {
      const y = wy + (r + 0.5) * lineH;
      let text = ln.text;
      if (ln.idx === z) {          // 末行随播放头逐词出现
        text = "";
        for (let j = ln.start; j < ln.end; j++) {
          const tk = L.result.tokens[j];
          const tt = L.result.token_times[j];
          if (tt != null && tt <= t)
            text += (tk.space_before ? " " : "") + tk.text;
        }
      }
      g.fillStyle = ln.overwide ? "#ff6b5e"
        : (ln.orphan ? "#f0c96a" : "#ffffff");
      g.fillText(text, fx + fw / 2, y);
    });
    g.restore();
  }
  // 信息行
  const info = $("caption-info");
  let html;
  if (!pres.length) {
    html = "无呈现（无词元）。";
  } else if (!cur) {
    html = pres[0] && pres[0].begin != null && t < pres[0].begin
      ? "字幕尚未出现。"
      : '呈现时刻 <span class="undef">未定（缺少可用时钟）</span>。';
  } else {
    const b = cur.begin == null ? "?" : cur.begin.toFixed(2);
    const e2 = cur.end == null ? "?" : cur.end.toFixed(2);
    const dw = cur.dwell == null ? '<span class="undef">未定</span>'
      : cur.dwell.toFixed(2) + "s";
    const us = cur.width_usage == null ? '<span class="undef">未定</span>'
      : Math.round(cur.width_usage * 100) + "%";
    const rb = cur.readability === "ok"
      ? '<span class="ok">正常</span>' : '<span class="undef">未定</span>';
    html = `呈现 #${cur.idx}（${b}–${e2}s）驻留 ${dw} ｜ 行宽占用 ${us} ｜ ` +
           `回读 ${cur.re_read} 字 ｜ 可读性 ${rb}`;
  }
  if (info._last !== html) { info.innerHTML = html; info._last = html; }
}
