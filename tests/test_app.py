"""HTTP 层测试：静态资源、材料导入、缺页反例、校审/锁定/导出回归。

运行：PYTHONPATH=/tmp/pylibs/local/lib/python3.11/dist-packages \
      python3 tests/test_app.py
"""

import io
import json
import os
import shutil
import struct
import sys
import tempfile
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="lrapp_")
os.environ["LIVEREVIEW_DATA"] = _TMP
os.environ["LIVEREVIEW_DB"] = os.path.join(_TMP, "test.db")

import app as appmod  # noqa: E402
import make_demo  # noqa: E402
from livereview import engine  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, extra))


def make_wav(path, seconds=10.0, sr=16000):
    n = int(seconds * sr)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<%dh" % n, *([0] * n)))


def make_gap_materials(dirpath, gap=True):
    """构造最小材料：10 字参考稿，快照在 seq 4→7 处缺页且文本跨缺口变化。"""
    os.makedirs(dirpath, exist_ok=True)
    audio = os.path.join(dirpath, "a.wav")
    make_wav(audio, 10.0)
    ref = os.path.join(dirpath, "r.txt")
    with open(ref, "w", encoding="utf-8") as f:
        f.write("0 5 甲乙丙丁戊己庚辛壬癸\n")
    texts = ["", "甲", "甲乙", "甲乙丙", "甲乙丙丁戊", "甲乙丙丁戊己",
             "甲乙丙丁戊己庚", "甲乙丙丁戊己庚辛", "甲乙丙丁戊己庚辛壬",
             "甲乙丙丁戊己庚辛壬癸", "甲乙丙丁戊己庚辛壬癸"]
    times = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 9.8]
    seqs = list(range(1, 12))
    if gap:
        seqs[4] = 7  # 4 → 7 跳号，恰在"丁戊"出现时
        seqs = [s + (3 if i > 4 else 0) for i, s in enumerate(seqs)]
    log = os.path.join(dirpath, "l.jsonl")
    with open(log, "w", encoding="utf-8") as f:
        for sq, ts, tx in zip(seqs, times, texts):
            f.write(json.dumps({"seq": sq, "log_ts": ts, "text": tx},
                               ensure_ascii=False) + "\n")
    anchors = os.path.join(dirpath, "an.json")
    with open(anchors, "w", encoding="utf-8") as f:
        json.dump([{"log_ts": 0.5, "audio_ts": 0.5},
                   {"log_ts": 9.8, "audio_ts": 9.8}], f)
    return {"audio": audio, "reference": ref, "log": log, "anchors": anchors}


def main():
    client = appmod.app.test_client()

    # ---------- 1. 静态资源 ----------
    print("[1] 静态资源")
    r = client.get("/")
    check("首页 200", r.status_code == 200)
    check("首页引用 app.js", b"/static/app.js" in r.data)
    check("首页引用 style.css", b"/static/style.css" in r.data)
    r = client.get("/static/app.js")
    check("app.js 200", r.status_code == 200)
    check("app.js 使用 Web Audio", b"AudioContext" in r.data)
    check("app.js 联动演变带 Canvas", b"drawBand" in r.data)
    r = client.get("/static/style.css")
    check("style.css 200", r.status_code == 200)

    # ---------- 2. 材料导入 ----------
    print("[2] 材料导入")
    demo_dir = os.path.join(_TMP, "demo")
    paths = make_demo.build(demo_dir)
    with open(paths["audio"], "rb") as fa, \
            open(paths["reference"], "rb") as fr, \
            open(paths["log"], "rb") as fl, \
            open(paths["anchors"], "rb") as fn:
        r = client.post("/api/session/import", data={
            "name": "上传测试",
            "audio": (io.BytesIO(fa.read()), "lecture.wav"),
            "reference": (io.BytesIO(fr.read()), "ref.txt"),
            "log": (io.BytesIO(fl.read()), "snap.jsonl"),
            "anchors": (io.BytesIO(fn.read()), "anchors.json"),
        }, content_type="multipart/form-data")
    check("导入成功", r.status_code == 200, r.get_data(as_text=True)[:200])
    up_sid = r.get_json()["id"]
    st = client.get("/api/session/%d/state" % up_sid).get_json()
    check("导入会话有词元", st["token_count"] > 100)
    check("导入会话延迟已定义",
          st["aggregates"]["first_latency"] is not None)
    r = client.get("/api/session/%d/audio.wav" % up_sid)
    check("音频可回放", r.status_code == 200 and
          r.data[:4] == b"RIFF")

    # 缺文件 → 400
    r = client.post("/api/session/import", data={"name": "x"},
                    content_type="multipart/form-data")
    check("缺少文件返回 400", r.status_code == 400)
    # 坏日志 → 400
    with open(paths["audio"], "rb") as fa, \
            open(paths["reference"], "rb") as fr:
        r = client.post("/api/session/import", data={
            "name": "坏日志",
            "audio": (io.BytesIO(fa.read()), "a.wav"),
            "reference": (io.BytesIO(fr.read()), "r.txt"),
            "log": (io.BytesIO(b"{bad json\n"), "l.jsonl"),
        }, content_type="multipart/form-data")
    check("坏快照日志返回 400", r.status_code == 400,
          r.get_data(as_text=True)[:120])
    # 非 WAV 扩展名 → 400
    r = client.post("/api/session/import", data={
        "name": "错格式",
        "audio": (io.BytesIO(b"x"), "a.mp3"),
        "reference": (io.BytesIO("0 1 甲".encode("utf-8")), "r.txt"),
        "log": (io.BytesIO(b'{"seq":1,"log_ts":0,"text":""}\n'), "l.jsonl"),
    }, content_type="multipart/form-data")
    check("非 WAV 返回 400", r.status_code == 400)

    # ---------- 3. 缺页反例（计数必须为未定） ----------
    print("[3] 缺页反例")
    from livereview.store import Store
    db = Store(os.path.join(_TMP, "gap.db"))
    mat = make_gap_materials(os.path.join(_TMP, "gap"), gap=True)
    sid = engine.import_session(db, "gap", mat["audio"], mat["reference"],
                                mat["log"], mat["anchors"])
    st = engine.state(db, sid)
    ding = st["tokens"][3]   # 丁：在缺口之后首次出现
    jia = st["tokens"][0]    # 甲：缺口之前已稳定
    check("跨缺口出现词元改写数未定", ding["replace_count"] is None)
    check("跨缺口出现词元撤回数未定", ding["retract_count"] is None)
    check("该词元标记缺页", "snapshot_gap" in ding["undefined"])
    check("缺口前词元计数仍为数值",
          jia["replace_count"] == 0 and jia["retract_count"] == 0)
    check("缺口前词元首显延迟",
          abs(jia["first_latency"] - 0.75) < 1e-6,
          str(jia["first_latency"]))
    ag = st["aggregates"]
    check("聚合改写总数未定", ag["replace_total"] is None)
    check("聚合撤回总数未定", ag["retract_total"] is None)
    check("派生聚合未定", ag["rewritten_tokens"] is None
          and ag["retracted_tokens"] is None)
    check("未定原因=缺页", ag["counts_undefined_reason"] == "snapshot_gap")

    # 对照组：同样文本、连续编号 → 计数确定
    mat2 = make_gap_materials(os.path.join(_TMP, "nogap"), gap=False)
    sid2 = engine.import_session(db, "nogap", mat2["audio"],
                                 mat2["reference"], mat2["log"],
                                 mat2["anchors"])
    st2 = engine.state(db, sid2)
    ag2 = st2["aggregates"]
    check("对照组计数有定义", ag2["counts_defined"] is True)
    check("对照组改写总数为 0", ag2["replace_total"] == 0)
    check("对照组丁首显延迟",
          abs(st2["tokens"][3]["first_latency"] - 0.75) < 1e-6,
          str(st2["tokens"][3]["first_latency"]))

    # ---------- 4. HTTP 回归：校审 → 锁定 → 导出 ----------
    print("[4] 校审/锁定/导出回归")
    r = client.post("/api/demo")
    check("演示会话 200", r.status_code == 200)
    sid = r.get_json()["id"]
    st = client.get("/api/session/%d/state" % sid).get_json()
    # 锚点微调 → 部分重算
    anchors = st["anchors"]
    anchors.append({"log_ts": anchors[-1]["log_ts"] + 5,
                    "audio_ts": anchors[-1]["audio_ts"] + 5})
    r = client.put("/api/session/%d/anchors" % sid,
                   json={"anchors": anchors})
    check("锚点保存 200", r.status_code == 200)
    check("锚点改动触发部分重算",
          r.get_json()["recompute_log"][-1]["scope"] == "partial")
    # 重绑
    tgt = next(t for t in st["tokens"]
               if t["status"] == "sub" and t["text"] == "得")
    r = client.post("/api/session/%d/rebind" % sid,
                    json={"token": tgt["j"], "ref": tgt["ref_idx"]})
    check("重绑 200", r.status_code == 200
          and r.get_json()["tokens"][tgt["j"]]["status"] == "rebind")
    # 拆分 / 合并
    n_utt = len(st["utterances"])
    r = client.post("/api/session/%d/split" % sid, json={"token": 5})
    check("拆分 200", r.status_code == 200
          and len(r.get_json()["utterances"]) == n_utt + 1)
    r = client.post("/api/session/%d/merge" % sid, json={"utterance": 0})
    check("合并 200", r.status_code == 200
          and len(r.get_json()["utterances"]) == n_utt)
    # 休会
    r = client.put("/api/session/%d/recess" % sid,
                   json={"ranges": [{"start": 24.0, "end": 26.0}]})
    check("休会 200", r.status_code == 200
          and len(r.get_json()["recess"]) == 1)
    # 确认锁定
    r = client.post("/api/session/%d/confirm" % sid)
    check("确认 200", r.status_code == 200, r.get_data(as_text=True)[:200])
    digest = r.get_json()["digest"]
    check("摘要 64 位", len(digest) == 64)
    # 锁定后禁止修改
    r = client.post("/api/session/%d/rebind" % sid,
                    json={"token": 0, "ref": 0})
    check("锁定后修改返回 409", r.status_code == 409)
    # 四类导出
    for kind, head in (("vtt", b"WEBVTT"), ("csv", None),
                       ("svg", b"<svg"), ("json", b"{")):
        r = client.get("/api/session/%d/export/%s" % (sid, kind))
        ok = r.status_code == 200 and (head is None or head in r.data[:200])
        check("导出 %s" % kind, ok, str(r.status_code))
    r = client.get("/api/session/%d/export/csv" % sid)
    check("CSV 计数列含未定词元的空值", b"snapshot_gap" in r.data)

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
