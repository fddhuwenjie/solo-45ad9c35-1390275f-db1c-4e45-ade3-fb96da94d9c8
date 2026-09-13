"""计算管线：对齐、快照追踪、指标计算与受影响区间重算。

指标（均以音频时钟计）：
  首显延迟   词元位置首次出现任何文字的时刻 − 参照词元音频时刻
  稳定延迟   词元保持终稿形态不再变化的时刻 − 参照词元音频时刻
  撤回次数   相邻快照间 非空→空 的转换次数
  改写次数   相邻快照间 非空→非空且不同 的转换次数
  阅读速度   相邻快照间隔内 屏上字数 / 间隔时长（字/秒）
  无字幕区间 日志覆盖范围内无字幕且参考稿有词的音频区间

指标未定（undefined）情形：
  timestamp_nonmonotonic / snapshot_gap / ambiguous_alignment /
  anchor_residual_exceeded / no_anchor / audio_range_incomplete /
  no_reference_match
"""

import hashlib
import json
import wave

from .alignment import align_final_to_ref, map_snapshot_to_final
from .anchors import fit_clock, map_time
from .textnorm import char_count, content_tokens, join_tokens, tokenize

DEFAULT_PARAMS = {
    "anchor_residual_max": 0.75,  # 锚点拟合残差阈值（秒）
    "coverage_tol": 1.0,          # 日志覆盖音频的容差（秒）
    "min_uncaptioned": 0.5,       # 无字幕区间的最短时长（秒）
    "min_speed_interval": 0.05,   # 阅读速度统计的最短间隔（秒）
}

SENT_END = set("。！？；!?")


# ---------------------------------------------------------------- 输入解析

def read_wav_info(path):
    """读取 PCM WAV 的时长等信息；非 PCM 抛异常。"""
    with wave.open(path, "rb") as w:
        if w.getcomptype() != "NONE":
            raise ValueError("仅支持未压缩 PCM WAV（当前：%s）" % w.getcompname())
        frames, rate = w.getnframes(), w.getframerate()
        return {
            "duration": frames / float(rate),
            "sample_rate": rate,
            "channels": w.getnchannels(),
            "sample_width": w.getsampwidth(),
        }


def parse_reference(text, audio_duration):
    """解析参考稿。

    行格式 `start end 文本`（秒）为带时标行；否则为纯文本行。
    全部无时标时，把内容词元均匀分布到整个音频时长。
    返回 (ref_ctoks, ref_times, timed)。
    """
    lines = [ln.strip() for ln in (text or "").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    segments = []
    for ln in lines:
        parts = ln.split(None, 2)
        if len(parts) == 3:
            try:
                a, b = float(parts[0]), float(parts[1])
                if b > a >= 0:
                    segments.append((a, b, parts[2]))
                    continue
            except ValueError:
                pass
        segments.append((None, None, ln))
    timed = any(a is not None for a, _, _ in segments)
    ref_ctoks, ref_times = [], []
    if not timed:
        all_toks = []
        for _, _, t in segments:
            all_toks.extend(content_tokens(tokenize(t)))
        n = len(all_toks)
        for k, tok in enumerate(all_toks):
            ref_ctoks.append(tok)
            ref_times.append(round(audio_duration * (k + 0.5) / max(n, 1), 3))
        return ref_ctoks, ref_times, False
    # 有时标行内均匀分布；无时标行夹在中间时并入前一行的时间范围末尾
    last_end = 0.0
    for a, b, t in segments:
        toks = content_tokens(tokenize(t))
        if a is None:
            a = b = last_end
        n = len(toks)
        for k, tok in enumerate(toks):
            ref_ctoks.append(tok)
            ref_times.append(round(a + (b - a) * (k + 0.5) / max(n, 1), 3))
        last_end = b
    return ref_ctoks, ref_times, True


def parse_snapshot_log(text):
    """解析快照日志（JSONL）：每行 {seq, log_ts, text}。"""
    snaps = []
    for ln, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            snaps.append({"seq": int(obj["seq"]),
                          "log_ts": float(obj["log_ts"]),
                          "text": str(obj.get("text", ""))})
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError("快照日志第 %d 行无法解析：%s" % (ln, e))
    if not snaps:
        raise ValueError("快照日志为空")
    return snaps


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 词元事件

def _series_events(series, final_norm):
    """从单个词元位置的取值序列提取事件。

    series: 按快照顺序的 [None | token dict]。
    返回 first_display / stable（快照下标或 None）、改写/撤回次数、游程。
    """
    n = len(series)
    first_display = next((i for i, v in enumerate(series) if v is not None),
                         None)
    stable = None
    if n and series[-1] is not None and series[-1]["norm"] == final_norm:
        last_bad = -1
        for i in range(n - 1, -1, -1):
            v = series[i]
            if v is None or v["norm"] != final_norm:
                last_bad = i
                break
        stable = last_bad + 1
    replace_count = 0
    retract_count = 0
    runs = []
    run_start = None
    for i in range(n):
        v = series[i]
        p = series[i - 1] if i else None
        if v is not None and p is not None and v["norm"] != p["norm"]:
            replace_count += 1
        if v is None and p is not None:
            retract_count += 1
        if v is not None and run_start is None:
            run_start = i
        if v is None and run_start is not None:
            runs.append([run_start, i, series[run_start]["text"]])
            run_start = None
    if run_start is not None:
        runs.append([run_start, n, series[run_start]["text"]])
    return {"first_display": first_display, "stable": stable,
            "replace_count": replace_count, "retract_count": retract_count,
            "runs": runs}


# ---------------------------------------------------------------- 会话上下文

class Ctx:
    """一次请求内复用的会话计算上下文。"""

    def __init__(self, session, snapshots, anchors, rebinds, utterances,
                 recess, ref_ctoks, ref_times):
        self.session = session
        self.params = dict(DEFAULT_PARAMS)
        self.params.update(json.loads(session.get("params") or "{}"))
        self.audio_duration = session["audio_duration"]
        self.snapshots = snapshots
        self.anchors = anchors
        self.rebinds = rebinds          # {final_np_idx: ref_np_idx}
        self.utterances = utterances    # [{start,end}) 终稿内容词元下标
        self.recess = recess            # [{start,end}] 音频秒
        self.ref_ctoks = ref_ctoks
        self.ref_times = ref_times
        self.ref_norms = [t["norm"] for t in ref_ctoks]

        # 终稿 = 最后一条快照
        final_text = snapshots[-1]["text"] if snapshots else ""
        self.final_full = tokenize(final_text)
        self.final_ctoks = content_tokens(self.final_full)
        self.final_norms = [t["norm"] for t in self.final_ctoks]
        self.np_to_full = [i for i, t in enumerate(self.final_full)
                           if t["kind"] != "punct"]

        # 终稿 ↔ 参考稿
        (self.mapping, self.status, self.ambig_blocks,
         self.missing_ref) = align_final_to_ref(self.ref_norms,
                                                self.final_norms)

        # 快照内容词元与取值矩阵
        self.snap_ctoks = [content_tokens(tokenize(s["text"]))
                           for s in snapshots]
        self.values = [map_snapshot_to_final(self.final_norms, ct)
                       for ct in self.snap_ctoks]

        # 日志完整性
        self.gap_before, self.mono_before, self.log_flags = \
            check_log_integrity(snapshots)

        # 时钟
        self.clock = fit_clock(anchors)
        self.residual_exceeded = bool(
            self.clock and
            self.clock["max_residual"] > self.params["anchor_residual_max"])

        # 覆盖范围
        self.coverage = self._coverage()

    # -- 时钟 --
    def to_audio(self, log_ts):
        return map_time(self.clock, log_ts)

    def _coverage(self):
        if self.clock is None or not self.snapshots:
            return {"defined": False, "reason": "no_anchor"}
        t0 = self.to_audio(self.snapshots[0]["log_ts"])
        t1 = self.to_audio(self.snapshots[-1]["log_ts"])
        tol = self.params["coverage_tol"]
        dur = self.audio_duration
        problems = []
        if t0 is None or t1 is None or t1 < t0:
            problems.append("clock_unusable")
        else:
            if t0 > tol:
                problems.append("log_starts_late")
            if t1 < dur - tol:
                problems.append("log_ends_early")
            if t1 > dur + 2 * tol:
                problems.append("log_exceeds_audio")
        return {"defined": not problems, "start": t0, "end": t1,
                "audio_duration": dur, "problems": problems,
                "reason": "audio_range_incomplete" if problems else None}

    # -- 词元指标 --
    def compute_token(self, j):
        final_norm = self.final_norms[j]
        series = [self.values[i][j] for i in range(len(self.snapshots))]
        ev = _series_events(series, final_norm)
        undefined = []
        flags = []

        # 对齐状态（校审重绑优先）
        if j in self.rebinds:
            status, ref_idx = "rebind", self.rebinds[j]
        else:
            status, ref_idx = self.status[j], self.mapping[j]
        ref_time = (self.ref_times[ref_idx]
                    if ref_idx is not None and ref_idx < len(self.ref_times)
                    else None)

        fd, st = ev["first_display"], ev["stable"]
        t_fd = (self.to_audio(self.snapshots[fd]["log_ts"])
                if fd is not None else None)
        t_st = (self.to_audio(self.snapshots[st]["log_ts"])
                if st is not None else None)

        # --- 未定条件 ---
        if self.clock is None:
            undefined.append("no_anchor")
        elif self.residual_exceeded:
            undefined.append("anchor_residual_exceeded")
        if status == "ambiguous":
            undefined.append("ambiguous_alignment")
        if ref_idx is None and status != "ambiguous":
            undefined.append("no_reference_match")
        for name, idx in (("first_display", fd), ("stable", st)):
            if idx is None:
                continue
            if self.mono_before[idx]:
                undefined.append("timestamp_nonmonotonic")
            if self.gap_before[idx]:
                undefined.append("snapshot_gap")
                flags.append("gap_uncertain:" + name)
        if st is None:
            flags.append("never_stable")
        # 改写/撤回在缺页处只增不减 → 计数为下界
        if any(self.gap_before[i] for i in range(1, len(self.snapshots))
               if (series[i] is None) != (series[i - 1] is None)
               or (series[i] is not None and series[i - 1] is not None
                   and series[i]["norm"] != series[i - 1]["norm"])):
            flags.append("counts_lower_bound")

        latency_undef = [u for u in undefined if u in (
            "no_anchor", "anchor_residual_exceeded",
            "ambiguous_alignment", "no_reference_match",
            "timestamp_nonmonotonic", "snapshot_gap")]

        def _lat(t_ev):
            if latency_undef or t_ev is None or ref_time is None:
                return None
            return round(t_ev - ref_time, 3)

        if ref_time is not None and self._in_recess(ref_time):
            flags.append("recess_excluded")

        return {
            "j": j,
            "text": self.final_ctoks[j]["text"],
            "norm": final_norm,
            "kind": self.final_ctoks[j]["kind"],
            "status": status,
            "ref_idx": ref_idx,
            "ref_text": (self.ref_ctoks[ref_idx]["text"]
                         if ref_idx is not None
                         and ref_idx < len(self.ref_ctoks) else None),
            "ref_time": ref_time,
            "first_display_t": _r(t_fd),
            "stable_t": _r(t_st),
            "first_latency": _lat(t_fd),
            "stable_latency": _lat(t_st),
            "replace_count": ev["replace_count"],
            "retract_count": ev["retract_count"],
            "runs": ev["runs"],
            "first_display_idx": fd,
            "stable_idx": st,
            "undefined": sorted(set(undefined)),
            "flags": flags,
        }

    def _in_recess(self, t):
        return any(r["start"] <= t < r["end"] for r in self.recess)

    # -- 区间指标 --
    def interval_metrics(self):
        return {
            "coverage": self.coverage,
            "reading_speed": self._reading_speed(),
            "uncaptioned": self._uncaptioned(),
        }

    def _reading_speed(self):
        if self.clock is None:
            return {"defined": False, "reason": "no_anchor"}
        if self.residual_exceeded:
            return {"defined": False, "reason": "anchor_residual_exceeded"}
        snaps = self.snapshots
        samples = []
        for i in range(len(snaps) - 1):
            if self.gap_before[i + 1] or self.mono_before[i + 1]:
                continue  # 缺页/倒序的间隔不参与统计
            t0 = self.to_audio(snaps[i]["log_ts"])
            t1 = self.to_audio(snaps[i + 1]["log_ts"])
            if t0 is None or t1 is None:
                continue
            dt = t1 - t0
            if dt < self.params["min_speed_interval"]:
                continue
            mid = (t0 + t1) / 2
            if self._in_recess(mid):
                continue  # 休会段不计
            chars = char_count(snaps[i]["text"])
            samples.append({"t": round(t0, 3), "chars": chars,
                            "dt": round(dt, 3),
                            "cps": round(chars / dt, 2)})
        if not samples:
            return {"defined": False, "reason": "no_valid_intervals"}
        vals = sorted(s["cps"] for s in samples)
        return {
            "defined": True,
            "mean": round(sum(vals) / len(vals), 2),
            "max": vals[-1],
            "p90": vals[min(len(vals) - 1, int(len(vals) * 0.9))],
            "samples": samples,
        }

    def _uncaptioned(self):
        cov = self.coverage
        if not cov["defined"]:
            return {"defined": False,
                    "reason": cov["reason"] or "audio_range_incomplete"}
        snaps = self.snapshots
        times = [self.to_audio(s["log_ts"]) for s in snaps]
        empty = [char_count(s["text"]) == 0 for s in snaps]
        ranges = []
        start = None
        for i in range(len(snaps)):
            if empty[i] and start is None:
                start = times[i]
            if not empty[i] and start is not None:
                ranges.append([start, times[i]])
                start = None
        if start is not None:
            ranges.append([start, cov["end"]])
        # 合并相邻、扣除休会、过滤
        merged = []
        for a, b in sorted(ranges):
            if merged and a <= merged[-1][1] + 1e-6:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for r in self.recess:
            nxt = []
            for a, b in merged:
                if r["end"] <= a or r["start"] >= b:
                    nxt.append([a, b])
                else:
                    if a < r["start"]:
                        nxt.append([a, r["start"]])
                    if r["end"] < b:
                        nxt.append([r["end"], b])
            merged = nxt
        out = []
        for a, b in merged:
            if b - a < self.params["min_uncaptioned"]:
                continue
            # 区间内参考稿有词才算"该有字幕而没有"
            if not any(t is not None and a <= t < b for t in self.ref_times):
                continue
            out.append({"start": round(a, 3), "end": round(b, 3),
                        "duration": round(b - a, 3)})
        return {"defined": True, "ranges": out,
                "total": round(sum(r["duration"] for r in out), 3)}


def _r(t):
    return round(t, 3) if t is not None else None


def check_log_integrity(snapshots):
    """缺页与时标倒序检查（按文件顺序）。"""
    gap_before = [False] * len(snapshots)
    mono_before = [False] * len(snapshots)
    flags = []
    seen = False
    for i in range(1, len(snapshots)):
        prev, cur = snapshots[i - 1], snapshots[i]
        if cur["seq"] != prev["seq"] + 1:
            gap_before[i] = True
            flags.append({"type": "snapshot_gap", "at_index": i,
                          "detail": "seq %d → %d 之间存在缺页"
                                    % (prev["seq"], cur["seq"])})
        if cur["log_ts"] <= prev["log_ts"]:
            seen = True
            flags.append({"type": "timestamp_nonmonotonic", "at_index": i,
                          "detail": "seq %d 时标 %.3f 不晚于前一条 %.3f"
                                    % (cur["seq"], cur["log_ts"],
                                       prev["log_ts"])})
        mono_before[i] = seen
    return gap_before, mono_before, flags


# ---------------------------------------------------------------- 聚合

def _stats(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    med = (vals[n // 2] if n % 2
           else (vals[n // 2 - 1] + vals[n // 2]) / 2)
    return {"n": n, "mean": round(sum(vals) / n, 3),
            "median": round(med, 3),
            "p90": vals[min(n - 1, int(n * 0.9))],
            "max": vals[-1]}


def aggregate(token_metrics):
    """由词元指标聚合整体指标；recess_excluded 词元不计入。"""
    counted = [t for t in token_metrics
               if "recess_excluded" not in t["flags"]]
    first_ok = [t["first_latency"] for t in counted
                if not t["undefined"] and t["first_latency"] is not None]
    stable_ok = [t["stable_latency"] for t in counted
                 if not t["undefined"] and t["stable_latency"] is not None]
    return {
        "token_count": len(token_metrics),
        "counted": len(counted),
        "undefined_tokens": sum(1 for t in token_metrics if t["undefined"]),
        "first_latency": _stats(first_ok),
        "stable_latency": _stats(stable_ok),
        "replace_total": sum(t["replace_count"] for t in counted),
        "retract_total": sum(t["retract_count"] for t in counted),
        "rewritten_tokens": sum(1 for t in counted if t["replace_count"]),
        "retracted_tokens": sum(1 for t in counted if t["retract_count"]),
    }


# ---------------------------------------------------------------- 重算范围

def anchor_affected_log_range(old_anchors, new_anchors):
    """锚点调整后受影响的日志时间范围。

    被改动的锚点，其影响域为相邻未变锚点之间；端点之外延伸到日志边界。
    返回 (lo, hi)（日志秒，可为 ±inf）；无变化返回 None。
    """
    old = {round(a["log_ts"], 6): a["audio_ts"] for a in old_anchors}
    new = {round(a["log_ts"], 6): a["audio_ts"] for a in new_anchors}
    changed = [k for k in set(old) | set(new) if old.get(k) != new.get(k)]
    if not changed:
        return None
    unchanged = sorted(set(old) & set(new))
    lo, hi = float("-inf"), float("inf")
    for c in changed:
        before = [u for u in unchanged if u < c]
        after = [u for u in unchanged if u > c]
        lo = max(lo, before[-1] if before else float("-inf"))
        hi = min(hi, after[0] if after else float("inf"))
    return (lo, hi)


def tokens_in_log_range(ctx, lo, hi):
    """事件落在日志时间范围 (lo, hi) 内的词元下标。"""
    snaps = ctx.snapshots
    out = []
    for j in range(len(ctx.final_ctoks)):
        series = [ctx.values[i][j] for i in range(len(snaps))]
        ev = _series_events(series, ctx.final_norms[j])
        idxs = {ev["first_display"], ev["stable"]}
        for a, b, _t in ev["runs"]:
            idxs.add(a)
            idxs.add(b - 1)
        idxs.discard(None)
        if any(0 <= i < len(snaps)
               and lo < snaps[i]["log_ts"] <= hi for i in idxs):
            out.append(j)
    return out


# ---------------------------------------------------------------- 话语结构

def init_utterances(ctx):
    """按句末标点把终稿内容词元切成话语。"""
    utts = []
    start = 0
    for j in range(len(ctx.final_ctoks)):
        full_idx = ctx.np_to_full[j]
        nxt = (ctx.final_full[full_idx + 1]
               if full_idx + 1 < len(ctx.final_full) else None)
        last = j == len(ctx.final_ctoks) - 1
        if last or (nxt and nxt["kind"] == "punct" and nxt["text"]
                    and nxt["text"][0] in SENT_END):
            utts.append({"start": start, "end": j + 1})
            start = j + 1
    return utts


def token_audio_times(ctx, token_metrics):
    """每个终稿内容词元的音频时刻：有参照用参照时刻，否则线性插值。"""
    n = len(ctx.final_ctoks)
    times = [None] * n
    for j in range(n):
        m = token_metrics.get(j)
        if m and m.get("ref_time") is not None:
            times[j] = m["ref_time"]
    last_t, last_j = None, None
    for j in range(n):
        if times[j] is not None:
            if last_j is not None and last_j < j - 1:
                step = (times[j] - last_t) / (j - last_j)
                for k in range(last_j + 1, j):
                    times[k] = last_t + step * (k - last_j)
            last_t, last_j = times[j], j
    # 两端补全
    for j in range(n):
        if times[j] is not None:
            for k in range(j):
                if times[k] is None:
                    times[k] = max(0.0, times[j] - 0.3 * (j - k))
            break
    for j in range(n - 1, -1, -1):
        if times[j] is not None:
            for k in range(j + 1, n):
                if times[k] is None:
                    times[k] = times[j] + 0.3 * (k - j)
            break
    return times


def utterance_text(ctx, utt, token_metrics):
    """话语的修正文本：应用同音词重绑。"""
    toks = []
    for j in range(utt["start"], utt["end"]):
        full_idx = ctx.np_to_full[j]
        m = token_metrics.get(j, {})
        tok = dict(ctx.final_full[full_idx])
        if m.get("status") == "rebind" and m.get("ref_text"):
            tok["text"] = m["ref_text"]
        toks.append(tok)
        # 带上紧随其后的标点
        k = full_idx + 1
        while k < len(ctx.final_full) and ctx.final_full[k]["kind"] == "punct":
            toks.append(ctx.final_full[k])
            k += 1
    return join_tokens(toks)
