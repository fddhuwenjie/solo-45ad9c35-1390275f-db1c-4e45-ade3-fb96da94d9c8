"""导出：修正 WebVTT、逐词 CSV、延迟曲线 SVG、复算 JSON。"""

import csv
import io
import json


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
