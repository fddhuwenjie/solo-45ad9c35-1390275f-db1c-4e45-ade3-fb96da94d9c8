"""引擎层：会话生命周期、校审操作、受影响区间重算、确认导出。"""

import json
import os

from . import exports, layout, pipeline
from .pipeline import Ctx, DEFAULT_PARAMS
from .textnorm import join_tokens


# ---------------------------------------------------------------- 导入

def import_session(store, name, audio_path, ref_path, log_path,
                   anchor_path=None):
    """读取 PCM WAV、参考稿、快照日志，建会话并做首次全量计算。"""
    info = pipeline.read_wav_info(audio_path)  # 非 PCM 在此抛错
    with open(ref_path, encoding="utf-8") as f:
        ref_text = f.read()
    with open(log_path, encoding="utf-8") as f:
        log_text = f.read()
    snaps = pipeline.parse_snapshot_log(log_text)
    ref_ctoks, ref_times, _ = pipeline.parse_reference(ref_text,
                                                       info["duration"])
    digests = {"audio": pipeline.sha256_file(audio_path),
               "ref": pipeline.sha256_file(ref_path),
               "log": pipeline.sha256_file(log_path)}
    sid = store.create_session(name, audio_path, ref_path, log_path,
                               info["duration"], digests, DEFAULT_PARAMS)
    store.add_snapshots(sid, snaps)

    anchors = []
    if anchor_path and os.path.exists(anchor_path):
        with open(anchor_path, encoding="utf-8") as f:
            anchors = json.load(f)
    if anchors:
        store.set_anchors(sid, anchors, source="import")
        store.add_edit(sid, "anchor_import", {"anchors": anchors})

    ctx = build_ctx(store, sid)
    # 初始话语划分：句末标点处断开
    utts = _initial_utterances(ctx)
    store.set_utterances(sid, utts)
    ctx.utterances = utts
    # 版面复核的初始修订（默认设置、无手动换行/锁定）
    store.add_layout_revision(sid, layout.default_payload(), None)
    _full_recompute(store, sid, ctx, reason="import")
    return sid


def _initial_utterances(ctx):
    utts = []
    start = 0
    n = len(ctx.final_ctoks)
    for j in range(n):
        full_idx = ctx.np_to_full[j]
        nxt = (ctx.final_full[full_idx + 1]
               if full_idx + 1 < len(ctx.final_full) else None)
        if j == n - 1 or (nxt and nxt["kind"] == "punct"
                          and nxt["text"] in pipeline.SENT_END):
            utts.append({"start": start, "end": j + 1})
            start = j + 1
    if not utts and n:
        utts = [{"start": 0, "end": n}]
    return utts


# ---------------------------------------------------------------- 上下文

def build_ctx(store, sid):
    session = store.get_session(sid)
    if not session:
        raise KeyError("会话不存在：%s" % sid)
    snaps = store.get_snapshots(sid)
    anchors = store.get_anchors(sid)
    with open(session["ref_path"], encoding="utf-8") as f:
        ref_ctoks, ref_times, _ = pipeline.parse_reference(
            f.read(), session["audio_duration"])
    rebinds = {}
    for e in store.get_edits(sid, "rebind"):
        p = e["payload"]
        if p.get("ref") is None:
            rebinds.pop(p["token"], None)
        else:
            rebinds[p["token"]] = p["ref"]
    return Ctx(session, snaps, anchors, rebinds,
               store.get_utterances(sid), store.get_recess(sid),
               ref_ctoks, ref_times)


# ---------------------------------------------------------------- 重算

def _full_recompute(store, sid, ctx, reason):
    store.clear_token_metrics(sid)
    metrics = [ctx.compute_token(j) for j in range(len(ctx.final_ctoks))]
    for m in metrics:
        store.set_token_metric(sid, m["j"], m)
    intervals = ctx.interval_metrics()
    aggs = pipeline.aggregate(metrics)
    _persist_results(store, sid, ctx, aggs, intervals)
    _append_recompute_log(store, sid, {
        "reason": reason, "scope": "all",
        "tokens_recomputed": len(metrics), "intervals": True})
    store.flush()
    return aggs, intervals


def _scoped_recompute(store, sid, ctx, token_js, intervals, reason,
                      affected_range=None):
    """只重算受影响的词元与区间指标，其余沿用缓存。"""
    cached = store.get_token_metrics(sid)
    for j in token_js:
        if 0 <= j < len(ctx.final_ctoks):
            cached[j] = ctx.compute_token(j)
            store.set_token_metric(sid, j, cached[j])
    metrics = [cached[j] for j in sorted(cached)]
    if intervals:
        iv = ctx.interval_metrics()
    else:
        sess = store.get_session(sid)
        iv = json.loads(sess.get("interval_metrics") or "null")
        if iv is None:
            iv = ctx.interval_metrics()
    aggs = pipeline.aggregate(metrics)
    _persist_results(store, sid, ctx, aggs, iv)
    _append_recompute_log(store, sid, {
        "reason": reason, "scope": "partial",
        "affected_log_range": affected_range,
        "tokens_recomputed": len(token_js), "intervals": intervals})
    store.flush()
    return aggs, iv


def _persist_results(store, sid, ctx, aggs, intervals):
    flags = list(ctx.log_flags)
    if ctx.residual_exceeded:
        flags.append({"type": "anchor_residual_exceeded",
                      "detail": "锚点最大残差 %.3fs 超过阈值 %.3fs"
                                % (ctx.clock["max_residual"],
                                   ctx.params["anchor_residual_max"])})
    if ctx.clock is None:
        flags.append({"type": "no_anchor", "detail": "未设置时钟锚点"})
    if not ctx.coverage["defined"]:
        problems = ctx.coverage.get("problems") or [
            ctx.coverage.get("reason") or "unknown"]
        flags.append({"type": "audio_range_incomplete",
                      "detail": "日志覆盖范围不完整：%s" % ",".join(problems)})
    if ctx.ambig_blocks:
        flags.append({"type": "ambiguous_alignment",
                      "detail": "%d 处终稿↔参考稿对齐多解"
                                % len(ctx.ambig_blocks)})
    store.update_session(sid, aggregates=json.dumps(aggs),
                         interval_metrics=json.dumps(intervals),
                         flags=json.dumps(flags, ensure_ascii=False))


def _append_recompute_log(store, sid, entry):
    sess = store.get_session(sid)
    log = json.loads(sess.get("recompute_log") or "[]")
    entry["n"] = len(log) + 1
    log.append(entry)
    store.update_session(sid, recompute_log=json.dumps(log))


def _require_open(store, sid):
    sess = store.get_session(sid)
    if not sess:
        raise KeyError("会话不存在")
    if sess["status"] != "open":
        raise RuntimeError("会话已确认锁定，不能再修改")


# ---------------------------------------------------------------- 校审操作

def op_set_anchors(store, sid, anchors):
    """校正日志时钟锚点：只重算受影响日志区间内的词元。"""
    _require_open(store, sid)
    old = store.get_anchors(sid)
    rng = pipeline.anchor_affected_log_range(old, anchors)
    store.set_anchors(sid, anchors, source="manual")
    store.add_edit(sid, "anchor_set", {"anchors": anchors})
    ctx = build_ctx(store, sid)
    if rng is None:
        return state(store, sid)
    lo, hi = rng
    affected = []
    cached = store.get_token_metrics(sid)
    snaps = ctx.snapshots
    for j in range(len(ctx.final_ctoks)):
        m = cached.get(j)
        idxs = set()
        if m:
            idxs.add(m.get("first_display_idx"))
            idxs.add(m.get("stable_idx"))
        for run in (m or {}).get("runs", []):
            idxs.add(run[0])
            idxs.add(run[1] - 1)
        for i in idxs:
            if i is None or not (0 <= i < len(snaps)):
                continue
            lt = snaps[i]["log_ts"]
            if lo <= lt <= hi:
                affected.append(j)
                break
    if not cached:  # 首次（导入时无锚点）→ 全量
        _full_recompute(store, sid, ctx, reason="anchor_set(initial)")
    else:
        _scoped_recompute(store, sid, ctx, sorted(set(affected)),
                          intervals=True, reason="anchor_set",
                          affected_range=[None if lo == float("-inf") else lo,
                                          None if hi == float("inf") else hi])
    return state(store, sid)


def op_rebind(store, sid, token, ref):
    """重绑同音词：只重算该词元。"""
    _require_open(store, sid)
    store.add_edit(sid, "rebind", {"token": token, "ref": ref})
    ctx = build_ctx(store, sid)
    _scoped_recompute(store, sid, ctx, [token], intervals=False,
                      reason="rebind")
    return state(store, sid)


def op_split(store, sid, token):
    """在词元 token 前拆分为两句。"""
    _require_open(store, sid)
    utts = store.get_utterances(sid)
    for k, u in enumerate(utts):
        if u["start"] < token < u["end"]:
            utts[k:k + 1] = [{"start": u["start"], "end": token},
                             {"start": token, "end": u["end"]}]
            break
    else:
        raise ValueError("该位置无法拆分")
    store.set_utterances(sid, utts)
    store.add_edit(sid, "split", {"token": token})
    ctx = build_ctx(store, sid)
    _append_recompute_log(store, sid, {"reason": "split", "scope": "utterance",
                                       "tokens_recomputed": 0,
                                       "intervals": False})
    return state(store, sid)


def op_merge(store, sid, utterance_index):
    """合并第 utterance_index 句与下一句。"""
    _require_open(store, sid)
    utts = store.get_utterances(sid)
    if not (0 <= utterance_index < len(utts) - 1):
        raise ValueError("没有可合并的下一句")
    a, b = utts[utterance_index], utts[utterance_index + 1]
    utts[utterance_index:utterance_index + 2] = [
        {"start": a["start"], "end": b["end"]}]
    store.set_utterances(sid, utts)
    store.add_edit(sid, "merge", {"utterance": utterance_index})
    ctx = build_ctx(store, sid)
    _append_recompute_log(store, sid, {"reason": "merge", "scope": "utterance",
                                       "tokens_recomputed": 0,
                                       "intervals": False})
    return state(store, sid)


def op_set_recess(store, sid, ranges):
    """圈出休会段：只重算区间指标（阅读速度、无字幕区间）。"""
    _require_open(store, sid)
    store.set_recess(sid, ranges)
    store.add_edit(sid, "recess", {"ranges": ranges})
    ctx = build_ctx(store, sid)
    _scoped_recompute(store, sid, ctx, [], intervals=True, reason="recess")
    return state(store, sid)


# ---------------------------------------------------------------- 断行与滚屏复核

def _font_units(store, family):
    """字体度量：浏览器实测（SQLite）优先，其次内置表；都没有 → None。"""
    m = store.get_font_metrics(family)
    if m:
        return m["units"], m["source"] or "measured"
    if family in layout.BUILTIN_METRICS:
        return layout.BUILTIN_METRICS[family], "builtin"
    return None, None


def list_fonts(store):
    """已知字体及其度量可用性（内置 + 浏览器实测）。"""
    out = [{"family": f, "metrics": True, "source": "builtin"}
           for f in layout.BUILTIN_METRICS]
    for m in store.list_font_metrics():
        out.append({"family": m["font_family"], "metrics": True,
                    "source": m["source"]})
    return out


def get_layout(store, sid):
    """当前版面状态 + 全量复核计算（每次读取现算，保证与词元指标一致）。"""
    sess = store.get_session(sid)
    if not sess:
        raise KeyError("会话不存在：%s" % sid)
    ctx = build_ctx(store, sid)
    rev = store.get_layout_revision(sid)
    if rev is None:
        payload, revno, created = layout.default_payload(), 0, None
    else:
        payload = layout.validate_payload(ctx, rev["payload"])
        revno, created = rev["rev"], rev["created"]
    units, unit_src = _font_units(store, payload["settings"]["font_family"])
    result = layout.compute(ctx, store.get_token_metrics(sid), payload, units)
    return {
        "revision": revno, "created": created,
        "revisions": store.list_layout_revisions(sid),
        "settings": result["settings"],
        "breaks": result["breaks"], "locks": result["locks"],
        "font": {"family": payload["settings"]["font_family"],
                 "metrics": units is not None, "source": unit_src},
        "fonts": list_fonts(store),
        "aspects": layout.ASPECTS,
        "modes": list(layout.MODES),
        "result": result,
        "locked": sess["status"] != "open",
    }


def op_layout_revise(store, sid, payload):
    """校审员的一次版面调整：另存一条 SQLite 修订并返回最新复核结果。"""
    _require_open(store, sid)
    ctx = build_ctx(store, sid)
    cur = store.get_layout_revision(sid)
    base = cur["payload"] if cur else layout.default_payload()
    merged = {
        "settings": dict(base["settings"],
                         **(payload.get("settings") or {})),
        "breaks": payload["breaks"] if "breaks" in payload else base["breaks"],
        "locks": payload["locks"] if "locks" in payload else base["locks"],
    }
    clean = layout.validate_payload(ctx, merged)
    units, _ = _font_units(store, clean["settings"]["font_family"])
    result = layout.compute(ctx, store.get_token_metrics(sid), clean, units)
    summary = {"aggregates": result["aggregates"],
               "readability": result["readability"],
               "issues": len(result["issues"])}
    rev = store.add_layout_revision(sid, clean, summary)
    store.add_edit(sid, "layout_revise",
                   {"rev": rev, "settings": clean["settings"],
                    "breaks": len(clean["breaks"]),
                    "locks": len(clean["locks"])})
    _append_recompute_log(store, sid, {
        "reason": "layout_revise(r%d)" % rev, "scope": "layout",
        "tokens_recomputed": 0, "intervals": False})
    return get_layout(store, sid)


def op_font_metrics(store, family, units, source="browser"):
    """登记浏览器实测的字体度量（Canvas measureText 探针）。"""
    family = (family or "").strip()[:60]
    if not family:
        raise ValueError("字体名不能为空")
    clean = layout.validate_units(units)
    store.set_font_metrics(family, clean, source)
    return {"family": family, "metrics": True, "source": source}


# ---------------------------------------------------------------- 状态

def state(store, sid):
    sess = store.get_session(sid)
    if not sess:
        raise KeyError("会话不存在")
    ctx = build_ctx(store, sid)
    cached = store.get_token_metrics(sid)
    tokens = [cached.get(j) or ctx.compute_token(j)
              for j in range(len(ctx.final_ctoks))]
    snaps_meta = []
    for i, s in enumerate(ctx.snapshots):
        snaps_meta.append({
            "i": i, "seq": s["seq"], "log_ts": s["log_ts"],
            "audio_ts": _r(ctx.to_audio(s["log_ts"])),
            "chars": sum(1 for c in s["text"] if not c.isspace()),
            "gap_before": ctx.gap_before[i],
            "mono_before": ctx.mono_before[i],
        })
    ref_tokens = [{"i": k, "text": t["text"], "time": ctx.ref_times[k]}
                  for k, t in enumerate(ctx.ref_ctoks)]
    # 话语带起止词文本，便于前端展示
    utts = []
    for k, u in enumerate(ctx.utterances):
        toks = [ctx.final_ctoks[j]["text"]
                for j in range(u["start"], u["end"])]
        utts.append({"index": k, "start": u["start"], "end": u["end"],
                     "text": "".join(toks)})
    return {
        "session": {"id": sid, "name": sess["name"],
                    "status": sess["status"],
                    "audio_duration": sess["audio_duration"],
                    "log_sha256": sess["log_sha256"],
                    "confirmed_at": sess["confirmed_at"],
                    "confirm_digest": sess["confirm_digest"]},
        "params": ctx.params,
        "tokens": tokens,
        "token_count": len(tokens),
        "final_text": join_tokens(ctx.final_full),
        "snapshots": snaps_meta,
        "anchors": ctx.anchors,
        "clock": ctx.clock,
        "residual_exceeded": ctx.residual_exceeded,
        "utterances": utts,
        "recess": ctx.recess,
        "ref_tokens": ref_tokens,
        "ambig_blocks": ctx.ambig_blocks,
        "aggregates": json.loads(sess.get("aggregates") or "null"),
        "interval_metrics": json.loads(sess.get("interval_metrics") or "null"),
        "flags": json.loads(sess.get("flags") or "[]"),
        "recompute_log": json.loads(sess.get("recompute_log") or "[]"),
        "confirmation": store.get_confirmation(sid),
    }


def _r(t):
    return round(t, 3) if t is not None else None


# ---------------------------------------------------------------- 确认与导出

def confirm(store, sid, export_dir):
    """锁定日志摘要、对齐与人工决定，生成四类导出文件。"""
    _require_open(store, sid)
    sess = store.get_session(sid)
    ctx = build_ctx(store, sid)
    cached = store.get_token_metrics(sid)
    tokens = [cached[j] for j in sorted(cached)]
    aggs = json.loads(sess.get("aggregates") or "null")
    intervals = json.loads(sess.get("interval_metrics") or "null")
    recompute_log = json.loads(sess.get("recompute_log") or "[]")
    digests = {"audio_sha256": sess["audio_sha256"],
               "ref_sha256": sess["ref_sha256"],
               "log_sha256": sess["log_sha256"]}
    # 定稿版面：样式、断行、锁定与滚屏模式随会话一并锁定
    lay = get_layout(store, sid)
    lay_result = lay["result"]
    # 锁定摘要：日志 + 对齐 + 人工决定 + 参数
    lock_payload = json.dumps({
        "digests": digests,
        "alignment": {"mapping": ctx.mapping, "status": ctx.status,
                      "ambiguous_blocks": ctx.ambig_blocks},
        "decisions": {"anchors": ctx.anchors, "rebinds": ctx.rebinds,
                      "utterances": ctx.utterances, "recess": ctx.recess,
                      "layout": {"revision": lay["revision"],
                                 "settings": lay["settings"],
                                 "breaks": lay["breaks"],
                                 "locks": lay["locks"]}},
        "params": ctx.params,
    }, ensure_ascii=False, sort_keys=True)
    import hashlib
    digest = hashlib.sha256(lock_payload.encode("utf-8")).hexdigest()

    os.makedirs(export_dir, exist_ok=True)
    token_metrics_list = tokens
    tm_map = _token_metric_map(ctx, cached)
    files = {
        "vtt": exports.export_webvtt(ctx, tm_map),
        "csv": exports.export_csv(token_metrics_list),
        "svg": exports.export_svg(ctx, token_metrics_list, intervals),
        "json": exports.export_json(sess, ctx, token_metrics_list, aggs,
                                    intervals, recompute_log, digests),
        "breaks_vtt": exports.export_layout_vtt(ctx, tm_map, lay_result),
        "issues_csv": exports.export_issues_csv(lay_result),
        "window_svg": exports.export_window_svg(lay_result),
        "layout_json": exports.export_layout_json(sess, lay, digests),
    }
    paths = {}
    names = {"vtt": "corrected.vtt", "csv": "words.csv",
             "svg": "latency.svg", "json": "recompute.json",
             "breaks_vtt": "broken.vtt", "issues_csv": "issues.csv",
             "window_svg": "window.svg", "layout_json": "layout.json"}
    for kind, content in files.items():
        p = os.path.join(export_dir, names[kind])
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        paths[kind] = p
    store.add_confirmation(sid, digest, paths)
    store.update_session(sid, status="confirmed",
                         confirmed_at=__import__("time").time(),
                         confirm_digest=digest)
    return {"digest": digest, "exports": paths}


def _token_metric_map(ctx, cached):
    """exports.export_webvtt 需要的 token_metrics: {j: {ref_time,status,...}}"""
    out = {}
    for j, m in cached.items():
        out[j] = {"ref_time": m.get("ref_time"), "status": m.get("status"),
                  "ref_text": m.get("ref_text")}
    return out
