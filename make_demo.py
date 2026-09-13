"""生成演示数据：PCM WAV、带时标参考稿、快照日志、初始锚点。

模拟一场约 47 秒的讲座字幕：
  - 终稿与参考稿存在同音词差异（的/得、姓/性）与一处多解区域；
  - 快照流包含草稿期错字后改、一次撤回重现、一次缺页、
    一段约 2.5 秒的字幕丢失（无字幕区间）；
  - 日志时钟相对音频有 500 秒偏移与轻微漂移，锚点带小误差。
"""

import json
import math
import os
import random
import struct
import wave

SR = 16000

SEGMENTS = [
    (0.5, 4.0, "各位老师同学，大家下午好。"),
    (4.5, 9.0, "今天我们复盘实时字幕的延迟与改写。"),
    (9.5, 14.0, "现场讲座中，字幕会反复修改同一句话。"),
    (14.5, 19.0, "依赖字幕的观众，可能晚几秒才看到内容。"),
    (19.5, 24.0, "中途闪过的错误姓名，也会被日志完整记录。"),
    (26.0, 31.0, "我们把每次快照与参考稿逐词对齐。"),
    (31.5, 36.0, "首显延迟、稳定延迟与撤回次数都能复算。"),
    (36.5, 41.0, "校审员可以校正锚点，圈出休会时段。"),
    (41.5, 46.0, "确认后导出修正字幕与复算数据。"),
]

# 终稿相对参考稿的差异（同音词错误 + 多解 + 多字）
FINAL_REPLACE = {
    "依赖字幕的观众": "依赖字幕得观众",
    "错误姓名": "错误性名",
    "校正锚点": "调整时钟基准",
    "复算数据": "复算数据哦",
}

# 草稿期暂时错字（先在屏幕上出现、随后改对）：{终稿子串: 暂时错写}
DRAFT_ERRORS = {
    "字幕": "字慕",
    "延迟": "延尺",
    "时钟": "时种",
    "观众": "观种",
    "撤回": "撤会",
    "快照": "快找",
}

FEED_DROP = (27.0, 29.5)     # 字幕丢失区间（快照文本为空）
SEQ_GAP = (33.0, 33.7)       # 缺页：该区间内快照丢失且 seq 跳号
RETRACT_SPAN = "错误"         # 该词先出现、消失、再出现（撤回）


def _final_text():
    parts = []
    for _, _, t in SEGMENTS:
        for a, b in FINAL_REPLACE.items():
            t = t.replace(a, b)
        parts.append(t)
    return "".join(parts)


def _char_speech_times(final):
    """每个终稿字符的"被说出"时刻：按段落时长均匀分布。"""
    times = []
    # 终稿与参考稿字符位置近似一致，按段落顺序分配
    idx = 0
    pos = 0
    for (a, b, ref) in SEGMENTS:
        seg_final = ref
        for x, y in FINAL_REPLACE.items():
            seg_final = seg_final.replace(x, y)
        n = len(seg_final)
        for k in range(n):
            times.append(a + (b - a) * (k + 0.5) / n)
        idx += n
        pos += n
    return times[:len(final)]


def build(outdir):
    os.makedirs(outdir, exist_ok=True)
    rng = random.Random(20260913)

    final = _final_text()
    speak_t = _char_speech_times(final)
    n = len(final)

    # 每个字符的首显时刻 = 说出时刻 + 抖动延迟
    lag = [1.5 + 0.6 * math.sin(k / 4.0) + rng.uniform(-0.2, 0.4)
           for k in range(n)]
    first_show = [speak_t[k] + max(0.6, lag[k]) for k in range(n)]

    # 草稿错字：fix_before[k] 之前显示错写
    wrong = [None] * n
    fix_at = [0.0] * n
    for correct, bad in DRAFT_ERRORS.items():
        start = 0
        while True:
            p = final.find(correct, start)
            if p < 0:
                break
            for k in range(p, p + len(correct)):
                wrong[k] = bad[k - p]
                fix_at[k] = first_show[k] + rng.uniform(0.8, 2.2)
            start = p + 1

    # 撤回演示：RETRACT_SPAN 首次出现后消失 0.8 秒再出现
    retract_at = final.find(RETRACT_SPAN)
    retract_range = range(retract_at, retract_at + len(RETRACT_SPAN)) \
        if retract_at >= 0 else range(0)
    retract_begin = (first_show[retract_at] + 0.5 if retract_at >= 0 else -1)
    retract_end = retract_begin + 0.8

    # 快照时刻
    snaps = []
    t = 0.2
    seq = 1
    while t <= 47.5:
        in_gap = SEQ_GAP[0] <= t < SEQ_GAP[1]
        if not in_gap:
            if FEED_DROP[0] <= t < FEED_DROP[1]:
                text = ""
            else:
                chars = []
                for k in range(n):
                    if t < first_show[k]:
                        break  # 字幕按顺序推进
                    if retract_at >= 0 and k in retract_range \
                            and retract_begin <= t < retract_end:
                        continue  # 撤回中
                    if wrong[k] is not None and t < fix_at[k]:
                        chars.append(wrong[k])
                    else:
                        chars.append(final[k])
                text = "".join(chars)
            log_ts = 500.0 + t * 1.0002  # 日志时钟：偏移 + 漂移
            snaps.append({"seq": seq, "log_ts": round(log_ts, 3),
                          "text": text})
        seq += 1
        t += 0.35

    # 缺页处 seq 跳号（模拟丢页）
    gap_seq = None
    for s in snaps:
        if s["log_ts"] > 500.0 + SEQ_GAP[1] * 1.0002 and gap_seq is None:
            gap_seq = s["seq"]
    if gap_seq is not None:
        for s in snaps:
            if s["seq"] >= gap_seq:
                s["seq"] += 3

    # 音频：每段语音为调幅噪声+谐波，段间近静音
    dur = 47.5
    total = int(dur * SR)
    samples = [0] * total
    for (a, b, _t) in SEGMENTS:
        ia, ib = int(a * SR), int(b * SR)
        f0 = 140 + 40 * math.sin(a)
        for i in range(ia, min(ib, total)):
            tt = i / SR
            env = 0.5 + 0.5 * math.sin(2 * math.pi * 3.1 * tt)
            v = (0.22 * env * math.sin(2 * math.pi * f0 * tt)
                 + 0.12 * env * math.sin(2 * math.pi * 2.7 * f0 * tt)
                 + 0.06 * rng.uniform(-1, 1))
            samples[i] = int(max(-1, min(1, v)) * 30000)
    for i in range(total):  # 底噪
        samples[i] = max(-32768, min(32767,
                                     samples[i] + int(rng.uniform(-1, 1) * 300)))

    audio_path = os.path.join(outdir, "lecture.wav")
    with wave.open(audio_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(struct.pack("<%dh" % total, *samples))

    ref_path = os.path.join(outdir, "reference.txt")
    with open(ref_path, "w", encoding="utf-8") as f:
        for a, b, t in SEGMENTS:
            f.write("%.2f %.2f %s\n" % (a, b, t))

    log_path = os.path.join(outdir, "snapshots.jsonl")
    with open(log_path, "w", encoding="utf-8") as f:
        for s in snaps:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    # 初始锚点：3 对，带 ±0.15s 误差（残差在阈值内）
    anchors = []
    for at in (2.0, 24.0, 44.0):
        log_ts = 500.0 + at * 1.0002
        anchors.append({"log_ts": round(log_ts, 3),
                        "audio_ts": round(at + rng.uniform(-0.15, 0.15), 3)})
    anchor_path = os.path.join(outdir, "anchors.json")
    with open(anchor_path, "w", encoding="utf-8") as f:
        json.dump(anchors, f, indent=1)

    return {"audio": audio_path, "reference": ref_path, "log": log_path,
            "anchors": anchor_path}


if __name__ == "__main__":
    p = build(os.path.join(os.path.dirname(__file__), "data", "demo"))
    print(json.dumps(p, indent=1))
