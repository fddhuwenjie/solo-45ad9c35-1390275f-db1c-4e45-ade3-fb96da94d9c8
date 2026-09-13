"""断行与滚屏复核：字幕窗口布局、呈现重建与可读性检查。

模型
  布局对象为终稿内容词元（应用同音词重绑后的展示文本，尾随标点并入前一
  词元）。换行点与锁定单元以终稿内容词元下标记录，每次调整另存 SQLite
  修订。词元 j 的出现时刻取其首显时刻（快照时钟 → 音频时钟）；首显未定
  的词元（无锚点 / 锚点残差超限 / 时标倒序 / 快照缺页）时刻按 None 处理。

  字幕窗口有 N 行。逐行滚动（scroll）：行 k 的首词元出现时该行进入窗口
  底部，行 k+N 出现时行 k 滚出；呈现 P_k = 行 k 进入到行 k+1 进入之间的
  窗口状态。整屏替换（replace）：每 N 行一屏，呈现 = 一屏从首词出现到被
  下一屏整体替换。

指标（每次呈现）
  驻留时长   本呈现开始 → 下一呈现开始（末个呈现终于日志覆盖末端）
  行宽占用   窗口内各行宽度 / 可用行宽 的最大值（字体度量缺失时未定）
  回读距离   逐行滚动：滚动后仍在屏上但位置上移的字符数；
             整屏替换：换屏时被整屏撤下的字符数
  滚屏频率   滚屏/翻屏事件数 ÷ 呈现总时长（次/分）

可读性未定（界面定位到对应时段）
  overwide 超宽 / orphan_line 孤行 / lock_split 锁定单元被拆 /
  dwell_short 驻留不足 / snapshot_gap 快照缺页 /
  font_metrics_missing 字体度量缺失
"""

# ---------------------------------------------------------------- 常量

ASPECTS = {"16:9": (16, 9), "4:3": (4, 3), "9:16": (9, 16), "1:1": (1, 1)}
MODES = ("scroll", "replace")

DEFAULT_SETTINGS = {
    "aspect": "16:9",             # 画幅
    "font_family": "Noto Sans CJK SC",
    "font_size": 42,              # 字号（px）
    "lines": 2,                   # 窗口行数
    "line_width": 960,            # 可用行宽（px）
    "min_dwell": 1.0,             # 最短驻留时间（秒）
    "mode": "scroll",             # scroll 逐行滚动 / replace 整屏替换
    "orphan_chars": 2,            # 孤行阈值（内容字符数不超过即孤行）
}

# 内置字体度量（em 相对宽度）；浏览器实测值经 API 存入 SQLite 后优先
BUILTIN_METRICS = {
    "Noto Sans CJK SC": {"cjk": 1.00, "latin": 0.55, "digit": 0.58,
                         "punct_cjk": 1.00, "punct_ascii": 0.34,
                         "space": 0.30, "overrides": {}},
    "Noto Serif CJK SC": {"cjk": 1.00, "latin": 0.52, "digit": 0.55,
                          "punct_cjk": 1.00, "punct_ascii": 0.32,
                          "space": 0.28, "overrides": {}},
    "DejaVu Sans Mono": {"cjk": 1.00, "latin": 0.60, "digit": 0.60,
                         "punct_cjk": 1.00, "punct_ascii": 0.60,
                         "space": 0.60, "overrides": {}},
}
# 度量缺失时的估算值（仅用于排版，宽度类指标保持未定）
FALLBACK_UNITS = BUILTIN_METRICS["Noto Sans CJK SC"]

# 出现时标不可信的词元未定原因
TIMING_UNDEF = ("no_anchor", "anchor_residual_exceeded",
                "timestamp_nonmonotonic", "snapshot_gap")

ISSUE_TYPES = ("overwide", "orphan_line", "lock_split", "dwell_short",
               "snapshot_gap", "font_metrics_missing")

_CJK_PUNCT = set("，。、；：？！""''（）《》〈〉【】…—·～")


def default_payload():
    return {"settings": dict(DEFAULT_SETTINGS), "breaks": [], "locks": []}


# ---------------------------------------------------------------- 字体度量

def char_class(ch):
    if ch.isspace():
        return "space"
    o = ord(ch)
    if (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF) \
            or (0xF900 <= o <= 0xFAFF):
        return "cjk"
    if ch in _CJK_PUNCT:
        return "punct_cjk"
    if "0" <= ch <= "9":
        return "digit"
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
        return "latin"
    return "punct_ascii"


def text_width_em(text, units):
    ov = units.get("overrides") or {}
    return sum(ov.get(ch, units[char_class(ch)]) for ch in text)


def validate_units(units):
    """校验浏览器实测或内置的字体度量，返回规范化副本。"""
    if not isinstance(units, dict):
        raise ValueError("字体度量必须是对象")
    out = {}
    for k in ("cjk", "latin", "digit", "punct_cjk", "punct_ascii", "space"):
        try:
            v = float(units[k])
        except (KeyError, TypeError, ValueError):
            raise ValueError("字体度量缺少有效数值：%s" % k)
        if not (0.05 <= v <= 3.0):
            raise ValueError("字体度量超出合理范围：%s=%s" % (k, v))
        out[k] = round(v, 4)
    ov = units.get("overrides") or {}
    if not isinstance(ov, dict):
        raise ValueError("overrides 必须是对象")
    out["overrides"] = {str(c)[0]: round(float(w), 4)
                        for c, w in ov.items() if str(c)}
    return out


# ---------------------------------------------------------------- 输入校验

def validate_payload(ctx, payload):
    """校验并规范化一次版面修订；非法输入抛 ValueError。"""
    if not isinstance(payload, dict):
        raise ValueError("修订内容必须是对象")
    s = dict(DEFAULT_SETTINGS)
    s.update(payload.get("settings") or {})
    try:
        s["font_size"] = int(s["font_size"])
        s["lines"] = int(s["lines"])
        s["line_width"] = int(s["line_width"])
        s["min_dwell"] = float(s["min_dwell"])
        s["orphan_chars"] = int(s["orphan_chars"])
    except (TypeError, ValueError):
        raise ValueError("版面设置含非数值项")
    if s["aspect"] not in ASPECTS:
        raise ValueError("未知画幅：%s" % s["aspect"])
    if s["mode"] not in MODES:
        raise ValueError("未知滚屏模式：%s" % s["mode"])
    if not (8 <= s["font_size"] <= 120):
        raise ValueError("字号需在 8–120 px 之间")
    if not (1 <= s["lines"] <= 6):
        raise ValueError("行数需在 1–6 之间")
    if not (100 <= s["line_width"] <= 4000):
        raise ValueError("可用行宽需在 100–4000 px 之间")
    if not (0.0 <= s["min_dwell"] <= 10.0):
        raise ValueError("最短驻留需在 0–10 s 之间")
    if not (0 <= s["orphan_chars"] <= 10):
        raise ValueError("孤行阈值需在 0–10 之间")
    s["font_family"] = str(s["font_family"]).strip()[:60] \
        or DEFAULT_SETTINGS["font_family"]

    n = len(ctx.final_ctoks)
    try:
        breaks = sorted(set(int(b) for b in (payload.get("breaks") or [])))
    except (TypeError, ValueError):
        raise ValueError("换行点必须是整数列表")
    for b in breaks:
        if not 0 < b < n:
            raise ValueError("换行点越界：%s（词元数 %d）" % (b, n))
    locks = []
    for span in payload.get("locks") or []:
        try:
            a, b = int(span[0]), int(span[1])
        except (TypeError, ValueError, IndexError):
            raise ValueError("锁定区间非法：%r" % (span,))
        if not (0 <= a < b <= n) or b - a < 2:
            raise ValueError("锁定区间非法：[%s,%s)" % (a, b))
        locks.append((a, b))
    locks.sort()
    for (a1, b1), (a2, b2) in zip(locks, locks[1:]):
        if a2 < b1:
            raise ValueError("锁定区间重叠：[%d,%d) 与 [%d,%d)" % (a1, b1, a2, b2))
    return {"settings": s, "breaks": breaks,
            "locks": [list(x) for x in locks]}


# ---------------------------------------------------------------- 展示词元

def display_tokens(ctx, token_metrics):
    """每个终稿内容词元的展示文本（应用重绑、尾随标点并入前一词元）。"""
    toks = []
    for j in range(len(ctx.final_ctoks)):
        full_idx = ctx.np_to_full[j]
        m = token_metrics.get(j) or {}
        tok = ctx.final_full[full_idx]
        text = tok["text"]
        if m.get("status") == "rebind" and m.get("ref_text"):
            text = m["ref_text"]
        k = full_idx + 1
        while k < len(ctx.final_full) and ctx.final_full[k]["kind"] == "punct":
            text += ctx.final_full[k]["text"]
            k += 1
        toks.append({"j": j, "text": text, "kind": tok["kind"]})
    for i, t in enumerate(toks):
        # 与 textnorm.join_tokens 一致：两个拉丁词之间、且中间无标点时补空格
        prev = toks[i - 1] if i else None
        t["space_before"] = bool(
            prev and t["kind"] == "word" and prev["kind"] == "word"
            and ctx.np_to_full[t["j"]] == ctx.np_to_full[prev["j"]] + 1)
    return toks


def join_display(toks):
    out = []
    for t in toks:
        if t["space_before"]:
            out.append(" ")
        out.append(t["text"])
    return "".join(out)


def content_chars(text):
    return sum(1 for ch in text
               if char_class(ch) in ("cjk", "latin", "digit"))


# ---------------------------------------------------------------- 断行

def break_lines(dtoks, locks, breaks, settings, metrics_ok, issues):
    """把展示词元排成行：锁定单元为原子，手动换行点强制断开，其余贪心填充。

    手动换行点落在锁定单元内部时，该单元被拆开并记录 lock_split 问题。
    """
    n = len(dtoks)
    # 原子单元：锁定区间 + 单词元
    units = []
    covered = [False] * n
    for lid, (s, e) in enumerate(sorted(locks)):
        units.append([s, e, lid])
        for j in range(s, e):
            covered[j] = True
    for j in range(n):
        if not covered[j]:
            units.append([j, j + 1, None])
    units.sort(key=lambda u: u[0])

    bset = set(breaks)
    atomic = []
    for s, e, lid in units:
        cuts = [s] + sorted(b for b in bset if s < b < e) + [e]
        if lid is not None and len(cuts) > 2:
            issues.append({"type": "lock_split", "lock": [s, e],
                           "detail": "锁定单元 [%d,%d) 被换行点拆开" % (s, e)})
        for a, b in zip(cuts, cuts[1:]):
            atomic.append((a, b))

    line_width = settings["line_width"]
    lines = []
    cur, cur_w = [], 0.0
    for a, b in atomic:
        uw = sum(dtoks[j]["_w"] for j in range(a, b))
        if cur and (a in bset or cur_w + uw > line_width + 0.5):
            lines.append(cur)
            cur, cur_w = [], 0.0
        cur.append((a, b))
        cur_w += uw
    if cur:
        lines.append(cur)

    out = []
    for idx, us in enumerate(lines):
        s, e = us[0][0], us[-1][1]
        toks = dtoks[s:e]
        text = join_display(toks)
        w = sum(t["_w"] for t in toks)
        n_ch = content_chars(text)
        out.append({
            "idx": idx, "start": s, "end": e, "text": text,
            "width": round(w, 1) if metrics_ok else None,
            "usage": round(w / line_width, 3) if metrics_ok else None,
            "n_chars": n_ch,
            "overwide": (w > line_width + 0.5) if metrics_ok else None,
            "orphan": n_ch <= settings["orphan_chars"],
        })
    return out


# ---------------------------------------------------------------- 时刻与缺口

def token_times(ctx, token_metrics):
    """每个终稿内容词元的出现时刻（首显时刻）；时标不可信时为 None。"""
    out = []
    for j in range(len(ctx.final_ctoks)):
        m = token_metrics.get(j) or {}
        t = m.get("first_display_t")
        if t is not None and any(u in TIMING_UNDEF
                                 for u in m.get("undefined", [])):
            t = None
        out.append(t)
    return out


def gap_intervals(ctx):
    """快照缺页在音频时钟下的区间列表。"""
    out = []
    snaps = ctx.snapshots
    for i in range(1, len(snaps)):
        if ctx.gap_before[i]:
            a = ctx.to_audio(snaps[i - 1]["log_ts"])
            b = ctx.to_audio(snaps[i]["log_ts"])
            if a is not None and b is not None and b > a:
                out.append((a, b))
    return out


# ---------------------------------------------------------------- 呈现重建

def build_presentations(lines, times, tok_undef, settings, coverage_end,
                        gaps):
    """由行序列与词元时刻重建各次呈现，并计算驻留/回读/行宽占用。"""
    n_lines = settings["lines"]
    mode = settings["mode"]
    m = len(lines)
    line_begin = [times[l["start"]] for l in lines]
    line_chars = [l["n_chars"] for l in lines]
    pres = []
    if mode == "scroll":
        for k in range(m):
            a = max(0, k - n_lines + 1)
            scrolled = k >= n_lines
            pres.append({
                "idx": k, "kind": "scroll", "lines": [a, k],
                "begin": line_begin[k],
                "scroll": scrolled,
                # 滚动后仍在屏上但位置上移的字符（视线需重新跟踪）
                "re_read": (sum(line_chars[a:k]) if scrolled else 0),
            })
    else:
        q = 0
        for s in range(0, m, n_lines):
            e = min(s + n_lines, m) - 1
            pres.append({
                "idx": q, "kind": "replace", "lines": [s, e],
                "begin": line_begin[s],
                "scroll": q > 0,
                # 换屏时被整屏撤下的字符（阅读位置完全丢失）
                "re_read": (sum(line_chars[s - n_lines:s]) if q else 0),
            })
            q += 1
    for i, p in enumerate(pres):
        nxt = pres[i + 1]["begin"] if i + 1 < len(pres) else coverage_end
        p["end"] = nxt
        b, e = p["begin"], p["end"]
        p["dwell"] = round(e - b, 3) \
            if (b is not None and e is not None) else None
        # 时标未定原因：窗口内词元的未定标记 ∪ 区间覆盖的缺页
        undef = set()
        a, z = p["lines"]
        for k in range(a, z + 1):
            for j in range(lines[k]["start"], lines[k]["end"]):
                undef |= tok_undef[j]
        if b is None or e is None:
            undef.add("token_time_unknown")
        for g0, g1 in gaps:
            if b is not None and e is not None and b < g1 and g0 < e:
                undef.add("snapshot_gap")
        p["undefined"] = sorted(undef)
    return pres


# ---------------------------------------------------------------- 汇总计算

def compute(ctx, token_metrics, payload, units):
    """计算整份版面复核结果。units 为 None 表示字体度量缺失。"""
    settings = payload["settings"]
    dtoks = display_tokens(ctx, token_metrics)
    eff = units or FALLBACK_UNITS
    fs = settings["font_size"]
    for t in dtoks:
        w = text_width_em(t["text"], eff) + \
            (eff["space"] if t["space_before"] else 0.0)
        t["_w"] = w * fs
        t["width"] = round(w * fs, 2) if units else None

    issues = []
    lines = break_lines(dtoks, payload["locks"], payload["breaks"], settings,
                        units is not None, issues)
    times = token_times(ctx, token_metrics)
    tok_undef = [set(u for u in (token_metrics.get(j) or {}).get(
        "undefined", []) if u in TIMING_UNDEF)
        for j in range(len(ctx.final_ctoks))]
    gaps = gap_intervals(ctx)
    pres = build_presentations(lines, times, tok_undef, settings,
                               ctx.coverage.get("end"), gaps)

    # 行的首秀呈现（滚动：行进窗口时；替换：所在屏）
    nl = settings["lines"]
    debut = [(k if settings["mode"] == "scroll" else k // nl)
             for k in range(len(lines))]
    tok_line = {}
    for ln in lines:
        for j in range(ln["start"], ln["end"]):
            tok_line[j] = ln["idx"]

    # 行宽占用（每次呈现取窗口内各行最大值）
    for p in pres:
        a, z = p["lines"]
        us = [lines[k]["usage"] for k in range(a, z + 1)]
        p["width_usage"] = max(us) if (units and all(u is not None
                                                     for u in us)) else None
        if units is None:
            p["undefined"] = sorted(set(p["undefined"])
                                    | {"font_metrics_missing"})

    # ---- 问题 ----
    def line_span(k):
        p = pres[debut[k]] if debut and debut[k] < len(pres) else None
        return (p["idx"] if p else None,
                p["begin"] if p else None, p["end"] if p else None)

    for ln in lines:
        pi, a, b = line_span(ln["idx"])
        if ln["overwide"]:
            issues.append({"type": "overwide", "line": ln["idx"],
                           "presentation": pi, "start": a, "end": b,
                           "detail": "行 %d 宽 %.0fpx 超过可用行宽 %dpx"
                                     % (ln["idx"], ln["width"],
                                        settings["line_width"])})
        if ln["orphan"]:
            issues.append({"type": "orphan_line", "line": ln["idx"],
                           "presentation": pi, "start": a, "end": b,
                           "detail": "行 %d 仅 %d 个内容字符（孤行）"
                                     % (ln["idx"], ln["n_chars"])})
    for it in issues:
        if it["type"] == "lock_split":
            s, e = it["lock"]
            k = tok_line.get(s)
            pi, a, b = line_span(k) if k is not None else (None, None, None)
            it.update({"line": k, "presentation": pi, "start": a, "end": b})
    for p in pres:
        if p["dwell"] is not None and p["dwell"] < settings["min_dwell"]:
            issues.append({"type": "dwell_short",
                           "presentation": p["idx"],
                           "start": p["begin"], "end": p["end"],
                           "detail": "呈现 %d 驻留 %.2fs 低于最短 %.2fs"
                                     % (p["idx"], p["dwell"],
                                        settings["min_dwell"])})
        if "snapshot_gap" in p["undefined"]:
            issues.append({"type": "snapshot_gap",
                           "presentation": p["idx"],
                           "start": p["begin"], "end": p["end"],
                           "detail": "呈现 %d 的时段覆盖快照缺页，驻留未定"
                                     % p["idx"]})
    if units is None:
        issues.append({"type": "font_metrics_missing",
                       "presentation": None, "start": None, "end": None,
                       "detail": "字体「%s」无度量数据，行宽占用与超宽检查未定"
                                 % settings["font_family"]})

    # 排序并编号；标记受影响呈现的可读性
    issues.sort(key=lambda it: (it["start"] is None, it["start"] or 0.0,
                                ISSUE_TYPES.index(it["type"])))
    for i, it in enumerate(issues):
        it["id"] = i + 1
    hit = set(it["presentation"] for it in issues
              if it["presentation"] is not None)
    for p in pres:
        p["readability"] = "undefined" \
            if (p["idx"] in hit or p["undefined"]) else "ok"

    # ---- 聚合 ----
    dwells = [p["dwell"] for p in pres if p["dwell"] is not None]
    events = sum(1 for p in pres if p["scroll"])
    span = None
    if pres and pres[0]["begin"] is not None and pres[-1]["end"] is not None:
        span = pres[-1]["end"] - pres[0]["begin"]
    usages = [l["usage"] for l in lines if l["usage"] is not None]
    by_type = {t: sum(1 for it in issues if it["type"] == t)
               for t in ISSUE_TYPES}
    aggregates = {
        "lines": len(lines), "presentations": len(pres),
        "dwell": ({"defined": True, "min": round(min(dwells), 3),
                   "mean": round(sum(dwells) / len(dwells), 3),
                   "max": round(max(dwells), 3)}
                  if dwells else {"defined": False}),
        "dwell_short": by_type["dwell_short"],
        "scroll_events": events,
        "scroll_per_min": (round(events / span * 60.0, 2)
                           if span and span > 0 else None),
        "re_read_total": sum(p["re_read"] for p in pres),
        "re_read_max": max((p["re_read"] for p in pres), default=0),
        "width_usage": ({"defined": True, "max": round(max(usages), 3),
                         "mean": round(sum(usages) / len(usages), 3)}
                        if usages else {"defined": False}),
        "issues": by_type,
        "undefined_presentations": sum(1 for p in pres
                                       if p["readability"] != "ok"),
    }
    readability = "undefined" if (
        issues or aggregates["undefined_presentations"]) else "ok"

    for t in dtoks:
        t.pop("_w", None)
    return {
        "settings": settings, "breaks": payload["breaks"],
        "locks": payload["locks"],
        "tokens": dtoks, "token_times": times,
        "lines": lines, "presentations": pres,
        "issues": issues, "aggregates": aggregates,
        "readability": readability,
        "metrics_missing": units is None,
        "font_family": settings["font_family"],
    }
