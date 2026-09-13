"""导出：修正 WebVTT、逐词 CSV、延迟曲线 SVG、复算 JSON。"""

import csv
import io
import json

from .layout import ASPECTS, join_display


def _vtt_ts(t):
    t = max(0.0, t or 0.0)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return "%02d:%02d:%06.3f" % (h, m, s)


def export_webvtt(ctx, token_metrics):
    """修正版 WebVTT：按话语切 cue，时间为音频时刻，文本含重绑修正。"""
    times = _utterance_times(ctx, token_metrics)
    lines = ["WEBVTT", ""]
    for k, utt in enumerate(ctx.utterances, 1):
        start, end = times[k - 1]
        text = _utt_text(ctx, utt, token_metrics)
        lines.append(str(k))
        lines.append("%s --> %s" % (_vtt_ts(start), _vtt_ts(end)))
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def _utterance_times(ctx, token_metrics):
    from .pipeline import token_audio_times
    tok_t = token_audio_times(ctx, token_metrics)
    out = []
    prev_end = 0.0
    for utt in ctx.utterances:
        s = tok_t[utt["start"]] if utt["start"] < len(tok_t) else prev_end
        e = tok_t[utt["end"] - 1] if utt["end"] - 1 < len(tok_t) else s
        start = max(prev_end, (s or 0.0) - 0.3)
        end = max(start + 0.4, (e or 0.0) + 0.6)
        out.append((round(start, 3), round(end, 3)))
        prev_end = end
    return out


def _utt_text(ctx, utt, token_metrics):
    from .pipeline import utterance_text
    return utterance_text(ctx, utt, token_metrics)


def export_csv(token_metrics_list):
    """逐词 CSV（utf-8-sig，便于表格软件直接打开）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["token_idx", "text", "ref_text", "status",
                "ref_time_s", "first_display_s", "stable_s",
                "first_latency_s", "stable_latency_s",
                "replace_count", "retract_count",
                "undefined_reasons", "flags"])
    for m in token_metrics_list:
        w.writerow([
            m["j"], m["text"], m.get("ref_text"), m["status"],
            _n(m.get("ref_time")), _n(m.get("first_display_t")),
            _n(m.get("stable_t")), _n(m.get("first_latency")),
            _n(m.get("stable_latency")),
            m["replace_count"], m["retract_count"],
            "|".join(m["undefined"]), "|".join(m["flags"]),
        ])
    return "﻿" + buf.getvalue()


def _n(v):
    return "" if v is None else v


def export_svg(ctx, token_metrics_list, interval_metrics):
    """延迟曲线 SVG：x=音频时间，y=延迟秒数；休会灰底、缺页红底。"""
    W, H = 1000, 340
    ml, mr, mt, mb = 56, 16, 28, 40
    dur = max(ctx.audio_duration, 0.001)
    lats = [m["first_latency"] for m in token_metrics_list
            if m.get("first_latency") is not None]
    lats += [m["stable_latency"] for m in token_metrics_list
             if m.get("stable_latency") is not None]
    ymax = max([1.0] + [abs(v) for v in lats])
    ymax = min(ymax, 30.0)

    def X(t):
        return ml + (W - ml - mr) * (t / dur)

    def Y(v):
        v = max(-ymax, min(ymax, v))
        return mt + (H - mt - mb) * (1 - (v + ymax) / (2 * ymax))

    p = []
    p.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'viewBox="0 0 %d %d" font-family="sans-serif" font-size="11">'
             % (W, H, W, H))
    p.append('<rect width="%d" height="%d" fill="#fff"/>' % (W, H))
    p.append('<text x="%d" y="18" font-size="13" font-weight="bold">'
             '字幕延迟曲线（秒，相对参考稿词元时刻）</text>' % ml)

    # 休会段
    for r in ctx.recess:
        p.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" '
                 'fill="#000" opacity="0.08"/>'
                 % (X(r["start"]), mt, X(r["end"]) - X(r["start"]),
                    H - mt - mb))
    # 缺页/倒序区间（按快照时间映射）
    snaps = ctx.snapshots
    for i in range(1, len(snaps)):
        if ctx.gap_before[i] or ctx.mono_before[i]:
            t0 = ctx.to_audio(snaps[i - 1]["log_ts"])
            t1 = ctx.to_audio(snaps[i]["log_ts"])
            if t0 is not None and t1 is not None and t1 > t0:
                p.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" '
                         'fill="#c00" opacity="0.10"/>'
                         % (X(t0), mt, X(t1) - X(t0), H - mt - mb))
    # 无字幕区间
    unc = (interval_metrics or {}).get("uncaptioned") or {}
    if unc.get("defined"):
        for r in unc["ranges"]:
            p.append('<rect x="%.1f" y="%d" width="%.1f" height="6" '
                     'fill="#c00" opacity="0.5"/>'
                     % (X(r["start"]), H - mb - 8,
                        max(1.0, X(r["end"]) - X(r["start"]))))

    # 坐标轴
    p.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#888"/>'
             % (ml, Y(0), W - mr, Y(0)))
    step = max(1, int(dur / 10))
    t = 0.0
    while t <= dur + 1e-6:
        p.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#ccc"/>'
                 % (X(t), mt, X(t), H - mb))
        p.append('<text x="%.1f" y="%d" text-anchor="middle" fill="#666">'
                 '%ds</text>' % (X(t), H - mb + 14, int(t)))
        t += step
    for v in (-ymax, -ymax / 2, 0, ymax / 2, ymax):
        p.append('<text x="%d" y="%.1f" text-anchor="end" fill="#666">'
                 '%+.1f</text>' % (ml - 6, Y(v) + 4, v))

    # 两条折线：首显延迟（蓝）、稳定延迟（橙）；未定处断开
    for key, color, label_y in (("first_latency", "#1f6feb", 0),
                                ("stable_latency", "#d97706", 14)):
        pts = []
        for m in token_metrics_list:
            t, v = m.get("ref_time"), m.get(key)
            if t is None or v is None:
                if len(pts) > 1:
                    p.append('<polyline points="%s" fill="none" stroke="%s" '
                             'stroke-width="1.5"/>'
                             % (" ".join(pts), color))
                pts = []
                continue
            pts.append("%.1f,%.1f" % (X(t), Y(v)))
        if len(pts) > 1:
            p.append('<polyline points="%s" fill="none" stroke="%s" '
                     'stroke-width="1.5"/>' % (" ".join(pts), color))
        for m in token_metrics_list:
            t, v = m.get("ref_time"), m.get(key)
            if t is not None and v is not None:
                p.append('<circle cx="%.1f" cy="%.1f" r="2" fill="%s"/>'
                         % (X(t), Y(v), color))
    p.append('<rect x="%d" y="6" width="10" height="10" fill="#1f6feb"/>'
             '<text x="%d" y="15" fill="#333">首显延迟</text>'
             % (W - 200, W - 186))
    p.append('<rect x="%d" y="6" width="10" height="10" fill="#d97706"/>'
             '<text x="%d" y="15" fill="#333">稳定延迟</text>'
             % (W - 120, W - 106))
    p.append("</svg>")
    return "\n".join(p)


def export_json(session, ctx, token_metrics_list, aggregates,
                interval_metrics, recompute_log, digests):
    """复算 JSON：参数、摘要、锚点、人工决定、指标——足以独立复算。"""
    return json.dumps({
        "session": {"id": session["id"], "name": session["name"],
                    "status": session["status"]},
        "digests": digests,
        "params": ctx.params,
        "audio_duration": ctx.audio_duration,
        "anchors": ctx.anchors,
        "clock": ctx.clock,
        "edits": {
            "rebinds": ctx.rebinds,
            "utterances": ctx.utterances,
            "recess": ctx.recess,
        },
        "alignment": {
            "mapping": ctx.mapping,
            "status": ctx.status,
            "ambiguous_blocks": ctx.ambig_blocks,
            "missing_ref": ctx.missing_ref,
        },
        "log_flags": ctx.log_flags,
        "coverage": ctx.coverage,
        "aggregates": aggregates,
        "interval_metrics": interval_metrics,
        "token_metrics": token_metrics_list,
        "recompute_log": recompute_log,
    }, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 版面复核导出

def export_layout_vtt(ctx, token_metrics, layout_result):
    """带换行的 WebVTT：按话语切 cue，cue 内按版面行断行。"""
    times = _utterance_times(ctx, token_metrics)
    line_of = {}
    for ln in layout_result["lines"]:
        for j in range(ln["start"], ln["end"]):
            line_of[j] = ln["idx"]
    toks = layout_result["tokens"]
    out = ["WEBVTT", ""]
    for k, utt in enumerate(ctx.utterances, 1):
        start, end = times[k - 1]
        parts, cur, cur_line = [], [], None
        for j in range(utt["start"], utt["end"]):
            li = line_of.get(j)
            if cur_line is not None and li != cur_line:
                parts.append(join_display(cur))
                cur = []
            cur.append(toks[j])
            cur_line = li
        if cur:
            parts.append(join_display(cur))
        out.append(str(k))
        out.append("%s --> %s" % (_vtt_ts(start), _vtt_ts(end)))
        out.append("\n".join(parts))
        out.append("")
    return "\n".join(out)


def export_issues_csv(layout_result):
    """可读性问题 CSV（utf-8-sig）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["issue_id", "type", "line", "presentation",
                "start_s", "end_s", "detail"])
    for it in layout_result["issues"]:
        w.writerow([it["id"], it["type"],
                    _n(it.get("line")), _n(it.get("presentation")),
                    _n(it.get("start")), _n(it.get("end")), it["detail"]])
    return "﻿" + buf.getvalue()


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def export_window_svg(layout_result):
    """窗口预览 SVG：各次呈现的字幕窗口缩略图，问题呈现红框标出。"""
    from .layout import ASPECTS
    st = layout_result["settings"]
    ar_w, ar_h = ASPECTS[st["aspect"]]
    pres = layout_result["presentations"]
    lines = layout_result["lines"]
    issue_pres = {it["presentation"] for it in layout_result["issues"]
                  if it["presentation"] is not None}
    # 抽样：呈现过多时均匀抽取，避免文件过大
    MAXF = 24
    if len(pres) > MAXF:
        step = len(pres) / MAXF
        show = [pres[int(i * step)] for i in range(MAXF)]
    else:
        show = list(pres)
    fw = 280.0
    fh = fw * ar_h / ar_w
    if fh > 300:
        fh, fw = 300.0, 300.0 * ar_w / ar_h
    cols = 4
    rows = (len(show) + cols - 1) // cols
    cap_h = 34
    W = int(cols * (fw + 14) + 14)
    H = int(rows * (fh + cap_h + 14) + 46)
    p = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
         'viewBox="0 0 %d %d" font-family="sans-serif" font-size="11">'
         % (W, H, W, H),
         '<rect width="%d" height="%d" fill="#fff"/>' % (W, H),
         '<text x="14" y="20" font-size="13" font-weight="bold">字幕窗口预览'
         '（画幅 %s，%d 行，行宽 %dpx，%s）</text>'
         % (st["aspect"], st["lines"], st["line_width"],
            "逐行滚动" if st["mode"] == "scroll" else "整屏替换"),
         '<text x="14" y="36" fill="#666">红框 = 存在可读性问题（可读性未定）；'
         '每图下方为驻留时长与行宽占用</text>']
    # 视频帧宽度假定：字幕区占帧宽 86%
    frame_w_px = st["line_width"] / 0.86
    scale = fw / frame_w_px
    win_w = st["line_width"] * scale
    fs = st["font_size"] * scale
    line_h = fs * 1.5
    win_h = st["lines"] * line_h
    for i, pr in enumerate(show):
        cx = 14 + (i % cols) * (fw + 14)
        cy = 46 + (i // cols) * (fh + cap_h + 14)
        bad = pr["idx"] in issue_pres or pr["readability"] != "ok"
        p.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                 'fill="#10141f" stroke="%s" stroke-width="2"/>'
                 % (cx, cy, fw, fh, "#c0392b" if bad else "#2f9e54"))
        wx = cx + (fw - win_w) / 2
        wy = cy + fh - win_h - fh * 0.05
        p.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                 'fill="#000" opacity="0.55"/>' % (wx, wy, win_w, win_h))
        a, z = pr["lines"]
        shown = lines[a:z + 1][-st["lines"]:]
        for r, ln in enumerate(shown):
            ty = wy + (r + 0.5) * line_h + fs * 0.35
            color = "#fff"
            if ln["overwide"]:
                color = "#ff6b5e"
            elif ln["orphan"]:
                color = "#f0c96a"
            p.append('<text x="%.1f" y="%.1f" font-size="%.1f" '
                     'text-anchor="middle" fill="%s">%s</text>'
                     % (cx + fw / 2, ty, fs, color, _esc(ln["text"])))
        dwell = ("%.2fs" % pr["dwell"]) if pr["dwell"] is not None else "未定"
        usage = ("%.0f%%" % (pr["width_usage"] * 100)) \
            if pr["width_usage"] is not None else "未定"
        p.append('<text x="%.1f" y="%.1f" fill="#333">#%d 驻留 %s｜行宽 %s｜'
                 '回读 %d 字</text>'
                 % (cx, cy + fh + 14, pr["idx"], dwell, usage, pr["re_read"]))
        if pr["scroll"]:
            p.append('<text x="%.1f" y="%.1f" fill="#888">%s</text>'
                     % (cx, cy + fh + 27,
                        "滚动入行" if pr["kind"] == "scroll" else "整屏替换"))
    p.append("</svg>")
    return "\n".join(p)


def export_layout_json(session, lay_state, digests):
    """版面复算 JSON：样式、断行、人工决定与全部复核指标。"""
    r = lay_state["result"]
    return json.dumps({
        "session": {"id": session["id"], "name": session["name"],
                    "status": session["status"]},
        "digests": digests,
        "layout_revision": lay_state["revision"],
        "settings": r["settings"],
        "decisions": {"breaks": r["breaks"], "locks": r["locks"]},
        "font": lay_state["font"],
        "tokens": r["tokens"],
        "token_times": r["token_times"],
        "lines": r["lines"],
        "presentations": r["presentations"],
        "issues": r["issues"],
        "aggregates": r["aggregates"],
        "readability": r["readability"],
        "aspects": sorted(ASPECTS),
    }, ensure_ascii=False, indent=1)
