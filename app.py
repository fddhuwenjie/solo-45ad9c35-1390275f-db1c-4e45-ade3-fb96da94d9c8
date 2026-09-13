"""Flask 入口：实时字幕延迟与改写复盘。

运行：PYTHONPATH=/tmp/pylibs/local/lib/python3.11/dist-packages python3 app.py
"""

import os
import time

from flask import Flask, abort, jsonify, render_template, request, send_file

from livereview import engine
from livereview.store import Store

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("LIVEREVIEW_DATA", os.path.join(BASE, "data"))
DB_PATH = os.environ.get("LIVEREVIEW_DB", os.path.join(DATA, "review.db"))
EXPORT_ROOT = os.path.join(DATA, "exports")
UPLOAD_ROOT = os.path.join(DATA, "uploads")

app = Flask(__name__)
store = Store(DB_PATH)


def _err(fn):
    try:
        return jsonify(fn())
    except KeyError as e:
        abort(404, str(e))
    except RuntimeError as e:
        abort(409, str(e))
    except ValueError as e:
        abort(400, str(e))


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
def handle_err(e):
    return jsonify({"error": e.description}), e.code


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/sessions")
def sessions():
    return jsonify(store.list_sessions())


@app.post("/api/demo")
def demo():
    """生成演示数据并导入为一个会话。"""
    import make_demo
    paths = make_demo.build(os.path.join(DATA, "demo"))
    sid = engine.import_session(
        store, "演示讲座", paths["audio"], paths["reference"],
        paths["log"], paths["anchors"])
    return jsonify({"id": sid})


@app.post("/api/session/import")
def import_materials():
    """接收用户材料：PCM WAV、参考稿、带时标快照日志（锚点可选）。"""
    name = (request.form.get("name") or "").strip() or "未命名会话"
    audio = request.files.get("audio")
    ref = request.files.get("reference")
    log = request.files.get("log")
    anchors = request.files.get("anchors")
    if not (audio and audio.filename):
        abort(400, "缺少 PCM WAV 音频文件")
    if not (ref and ref.filename):
        abort(400, "缺少参考稿")
    if not (log and log.filename):
        abort(400, "缺少快照日志")
    if not audio.filename.lower().endswith(".wav"):
        abort(400, "音频必须是 WAV 文件")

    updir = os.path.join(UPLOAD_ROOT, "%d" % (time.time() * 1000))
    os.makedirs(updir, exist_ok=True)
    apath = os.path.join(updir, "audio.wav")
    rpath = os.path.join(updir, "reference.txt")
    lpath = os.path.join(updir, "snapshots.jsonl")
    audio.save(apath)
    ref.save(rpath)
    log.save(lpath)
    anpath = None
    if anchors and anchors.filename:
        anpath = os.path.join(updir, "anchors.json")
        anchors.save(anpath)

    def _import():
        try:
            return {"id": engine.import_session(
                store, name, apath, rpath, lpath, anpath)}
        except ValueError:
            # 材料不可用时清理已保存文件，避免留下坏会话
            import shutil
            shutil.rmtree(updir, ignore_errors=True)
            raise
    return _err(_import)


@app.get("/api/session/<int:sid>/state")
def get_state(sid):
    return _err(lambda: engine.state(store, sid))


@app.get("/api/session/<int:sid>/audio.wav")
def audio(sid):
    sess = store.get_session(sid)
    if not sess:
        abort(404)
    return send_file(sess["audio_path"], mimetype="audio/wav")


@app.put("/api/session/<int:sid>/anchors")
def set_anchors(sid):
    anchors = request.get_json(force=True).get("anchors", [])
    for a in anchors:
        a["log_ts"] = float(a["log_ts"])
        a["audio_ts"] = float(a["audio_ts"])
    return _err(lambda: engine.op_set_anchors(store, sid, anchors))


@app.post("/api/session/<int:sid>/rebind")
def rebind(sid):
    p = request.get_json(force=True)
    return _err(lambda: engine.op_rebind(
        store, sid, int(p["token"]),
        None if p.get("ref") is None else int(p["ref"])))


@app.post("/api/session/<int:sid>/split")
def split(sid):
    p = request.get_json(force=True)
    return _err(lambda: engine.op_split(store, sid, int(p["token"])))


@app.post("/api/session/<int:sid>/merge")
def merge(sid):
    p = request.get_json(force=True)
    return _err(lambda: engine.op_merge(store, sid, int(p["utterance"])))


@app.put("/api/session/<int:sid>/recess")
def recess(sid):
    ranges = request.get_json(force=True).get("ranges", [])
    for r in ranges:
        r["start"] = float(r["start"])
        r["end"] = float(r["end"])
    return _err(lambda: engine.op_set_recess(store, sid, ranges))


@app.post("/api/session/<int:sid>/confirm")
def confirm(sid):
    return _err(lambda: engine.confirm(store, sid,
                                       os.path.join(EXPORT_ROOT, str(sid))))


@app.get("/api/session/<int:sid>/layout")
def get_layout(sid):
    """当前版面状态与全量复核结果（每次读取现算）。"""
    return _err(lambda: engine.get_layout(store, sid))


@app.post("/api/session/<int:sid>/layout/revise")
def layout_revise(sid):
    """校审员的一次版面调整：另存一条 SQLite 修订。"""
    return _err(lambda: engine.op_layout_revise(
        store, sid, request.get_json(force=True)))


@app.get("/api/fonts")
def fonts():
    return jsonify(engine.list_fonts(store))


@app.put("/api/fonts/<family>/metrics")
def font_metrics(family):
    """登记浏览器实测的字体度量（Canvas measureText 探针）。"""
    p = request.get_json(force=True)
    units = p.get("units") if isinstance(p, dict) and "units" in p else p
    return _err(lambda: engine.op_font_metrics(store, family, units))


@app.get("/api/session/<int:sid>/export/<kind>")
def export(sid, kind):
    sess = store.get_session(sid)
    if not sess:
        abort(404)
    conf = store.get_confirmation(sid)
    if not conf:
        abort(409, "会话尚未确认，确认后才能导出")
    path = conf["exports"].get(kind)
    if not path or not os.path.exists(path):
        abort(404)
    mime = {"vtt": "text/vtt", "csv": "text/csv",
            "svg": "image/svg+xml", "json": "application/json",
            "breaks_vtt": "text/vtt", "issues_csv": "text/csv",
            "window_svg": "image/svg+xml",
            "layout_json": "application/json"}.get(kind)
    if mime is None:
        abort(404)
    return send_file(path, mimetype=mime, as_attachment=True,
                     download_name=os.path.basename(path))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
