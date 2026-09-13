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
  selectedToken = null;
  selectedUtt = null;
  anchorRows = S.anchors.map(a => ({ log_ts: a.log_ts, audio_ts: a.audio_ts }));
  stopAudio();
  renderAll();
  loadAudio();
}

function applyState(st) {
  S = st;
  anchorRows = S.anchors.map(a => ({ log_ts: a.log_ts, audio_ts: a.audio_ts }));
  renderAll();
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
                              ["svg", "延迟曲线 SVG"], ["json", "复算 JSON"]]) {
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
