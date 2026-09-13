"""断行与滚屏复核回归测试：页面操作闭环、窄行/画幅/双行整屏边界、
负驻留修复、锁定单元、字体度量缺失、修订累积与导出入口。

运行：PYTHONPATH=/tmp/pylibs/local/lib/python3.11/dist-packages \
      python3 tests/test_layout.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP = tempfile.mkdtemp(prefix="lrlay_")
os.environ["LIVEREVIEW_DATA"] = _TMP
os.environ["LIVEREVIEW_DB"] = os.path.join(_TMP, "test.db")

import app as appmod  # noqa: E402
import make_demo  # noqa: E402
from livereview import engine  # noqa: E402
from livereview.store import Store  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, extra))


def new_session(client, name):
    r = client.post("/api/demo")
    assert r.status_code == 200
    sid = r.get_json()["id"]
    return sid


def num_phrase(layout):
    """演示文本中数字短语「3.」「5」「秒」的首词元下标。"""
    toks = layout["result"]["tokens"]
    return next(t["j"] for t in toks if t["text"] == "3.")


def main():
    client = appmod.app.test_client()

    # ---------- 1. 页面操作闭环（HTTP） ----------
    print("[1] 页面操作闭环")
    sid = new_session(client, "闭环")
    r = client.get("/api/session/%d/layout" % sid)
    check("版面状态 200", r.status_code == 200)
    lay = r.get_json()
    check("默认修订 #1", lay["revision"] == 1, str(lay["revision"]))
    check("默认双行滚动", lay["settings"]["lines"] == 2
          and lay["settings"]["mode"] == "scroll")
    check("呈现非空", lay["result"]["aggregates"]["presentations"] > 0)
    check("内置字体度量可用", lay["font"]["metrics"] is True)

    # 保存版面设置（画幅/字体/字号/行数/行宽/最短驻留/模式）
    r = client.post("/api/session/%d/layout/revise" % sid, json={
        "settings": {"aspect": "4:3", "font_size": 36, "lines": 3,
                     "line_width": 640, "min_dwell": 1.5,
                     "mode": "replace", "font_family": "Noto Serif CJK SC"}})
    check("设置修订 200", r.status_code == 200, r.get_data(as_text=True)[:200])
    lay = r.get_json()
    check("设置已生效", lay["settings"]["aspect"] == "4:3"
          and lay["settings"]["lines"] == 3
          and lay["settings"]["mode"] == "replace")
    check("修订递增", lay["revision"] == 2, str(lay["revision"]))

    # 断点与锁定闭环
    j0 = num_phrase(lay)
    r = client.post("/api/session/%d/layout/revise" % sid, json={
        "breaks": [5, 11], "locks": [[j0, j0 + 3]]})
    lay = r.get_json()
    check("断点已保存", lay["result"]["breaks"] == [5, 11])
    check("锁定已保存", lay["result"]["locks"] == [[j0, j0 + 3]])
    check("修订再递增", lay["revision"] == 3)
    r = client.get("/api/session/%d/layout" % sid)
    check("刷新后断点仍在", r.get_json()["result"]["breaks"] == [5, 11])

    # 字体度量登记闭环（浏览器 Canvas 实测 → 服务端存储）
    r = client.post("/api/session/%d/layout/revise" % sid, json={
        "settings": {"font_family": "My Custom Font"}})
    lay = r.get_json()
    check("未知字体度量缺失", lay["font"]["metrics"] is False)
    check("度量缺失→行宽占用未定",
          lay["result"]["aggregates"]["width_usage"]["defined"] is False)
    check("度量缺失→问题列表标记",
          any(i["type"] == "font_metrics_missing"
              for i in lay["result"]["issues"]))
    check("度量缺失→可读性未定", lay["result"]["readability"] == "undefined")
    units = {"cjk": 1.0, "latin": 0.5, "digit": 0.5, "punct_cjk": 1.0,
             "punct_ascii": 0.3, "space": 0.25, "overrides": {}}
    r = client.put("/api/fonts/My%%20Custom%%20Font/metrics".replace("%%", "%"),
                   json={"units": units})
    check("字体度量登记 200", r.status_code == 200,
          r.get_data(as_text=True)[:200])
    lay = client.get("/api/session/%d/layout" % sid).get_json()
    check("实测后度量可用", lay["font"]["metrics"] is True
          and lay["font"]["source"] == "browser")
    check("实测后行宽占用恢复",
          lay["result"]["aggregates"]["width_usage"]["defined"] is True)
    r = client.get("/api/fonts")
    check("字体列表含实测字体",
          any(f["family"] == "My Custom Font" for f in r.get_json()))

    # 确认锁定 → 四种新导出
    r = client.post("/api/session/%d/confirm" % sid)
    check("确认 200", r.status_code == 200, r.get_data(as_text=True)[:200])
    for kind, head in (("breaks_vtt", b"WEBVTT"), ("issues_csv", None),
                       ("window_svg", b"<svg"), ("layout_json", b"{")):
        r = client.get("/api/session/%d/export/%s" % (sid, kind))
        ok = r.status_code == 200 and (head is None or head in r.data[:200])
        check("导出 %s" % kind, ok, str(r.status_code))
    r = client.post("/api/session/%d/layout/revise" % sid,
                    json={"settings": {"lines": 2}})
    check("锁定后版面调整返回 409", r.status_code == 409)

    # ---------- 2. 断行 WebVTT 与问题 CSV 内容 ----------
    print("[2] 导出内容")
    r = client.get("/api/session/%d/export/breaks_vtt" % sid)
    vtt = r.get_data(as_text=True)
    check("断行 VTT 含换行 cue", "\n李雷与韩梅梅" in vtt
          or "统计了\n" in vtt or "\n3.5" in vtt
          or any("\n" in blk.split("\n", 2)[-1]
                 for blk in vtt.split("\n\n") if "-->" in blk),
          vtt[-200:])
    check("断行 VTT 数字短语未被拆开", "3.5秒" in vtt)
    r = client.get("/api/session/%d/export/issues_csv" % sid)
    csv = r.get_data(as_text=True)
    check("问题 CSV 表头", csv.startswith("﻿issue_id,type") or
          "issue_id,type" in csv.splitlines()[0])
    check("问题 CSV 含缺页行", "snapshot_gap" in csv)
    r = client.get("/api/session/%d/export/layout_json" % sid)
    lj = json.loads(r.get_data(as_text=True))
    check("版面 JSON 含样式与人工决定",
          "settings" in lj and "decisions" in lj
          and lj["decisions"]["locks"] == [[j0, j0 + 3]])
    check("版面 JSON 含呈现与问题",
          "presentations" in lj and "issues" in lj)

    # ---------- 3. 9:16 画幅 + 窄行 + 双行整屏替换边界 ----------
    print("[3] 9:16 / 120px / 100px / 双行整屏替换边界")
    sid = new_session(client, "边界")
    for w in (120, 100):
        r = client.post("/api/session/%d/layout/revise" % sid, json={
            "settings": {"aspect": "9:16", "line_width": w,
                         "mode": "replace", "lines": 2}})
        check("9:16 %dpx 配置 200" % w, r.status_code == 200,
              r.get_data(as_text=True)[:160])
        lay = r.get_json()
        st = lay["settings"]
        check("画幅保存为 9:16", st["aspect"] == "9:16")
        res = lay["result"]
        pres = res["presentations"]
        lines = res["lines"]
        n = st["lines"]
        check("整屏呈现数 = ceil(行数/2)",
              len(pres) == (len(lines) + n - 1) // n,
              "%d vs %d" % (len(pres), len(lines)))
        check("每屏行数 ≤ 窗口行数",
              all(p["lines"][1] - p["lines"][0] + 1 <= n for p in pres))
        check("屏内行号连续",
              all(p["lines"][1] - p["lines"][0] ==
                  (pres[i + 1]["lines"][0] - p["lines"][0] - 1
                   if i + 1 < len(pres) else
                   len(lines) - 1 - p["lines"][0])
                  for i, p in enumerate(pres)))
        dwells = [p["dwell"] for p in pres if p["dwell"] is not None]
        check("驻留全部非负", all(d >= 0 for d in dwells),
              str([d for d in dwells if d < 0][:5]))
        check("结束时刻不早于开始时刻",
              all(p["end"] is None or p["begin"] is None
                  or p["end"] >= p["begin"] for p in pres))
        begins = [p["begin"] for p in pres if p["begin"] is not None]
        check("开始时刻单调不减",
              all(a <= b + 1e-9 for a, b in zip(begins, begins[1:])))
        defined = [p for p in pres
                   if p["begin"] is not None and p["end"] is not None]
        check("相邻呈现首尾相接",
              all(abs(a["end"] - b["begin"]) < 1e-6
                  for a, b in zip(defined, defined[1:])))
        check("末屏终于日志覆盖末端",
              pres[-1]["end"] is not None and pres[-1]["end"] > 50.0,
              str(pres[-1]["end"]))
        check("翻屏事件数 = 屏数 - 1",
              res["aggregates"]["scroll_events"] == len(pres) - 1,
              str(res["aggregates"]["scroll_events"]))
        check("滚屏频率有定义",
              res["aggregates"]["scroll_per_min"] is not None
              and res["aggregates"]["scroll_per_min"] > 0)
        # 整屏替换：回读距离 = 被撤下的上一屏字符数
        check("翻屏回读距离为正",
              all(p["re_read"] > 0 for p in pres[1:]),
              str([p["re_read"] for p in pres[1:6]]))

    # ---------- 4. 负驻留修复（引擎级，含 9:16 窄行整屏） ----------
    print("[4] 负驻留修复")
    store = Store(os.path.join(_TMP, "neg.db"))
    paths = make_demo.build(os.path.join(_TMP, "demo2"))
    esid = engine.import_session(store, "neg", paths["audio"],
                                 paths["reference"], paths["log"],
                                 paths["anchors"])
    # 词元首显时刻存在抖动：行首时刻序列并非单调（缺陷存在的证据）
    lay = engine.op_layout_revise(store, esid, {
        "settings": {"line_width": 100, "mode": "replace", "aspect": "9:16"}})
    times = lay["result"]["token_times"]
    raw = [times[l["start"]] for l in lay["result"]["lines"]]
    raw = [t for t in raw if t is not None]
    check("行首原始时刻存在逆序（缺陷前提）",
          any(b < a for a, b in zip(raw, raw[1:])))
    for w, mode, aspect in ((100, "replace", "9:16"), (120, "replace", "9:16"),
                            (100, "scroll", "16:9"), (120, "scroll", "4:3"),
                            (100, "replace", "16:9")):
        lay = engine.op_layout_revise(store, esid, {
            "settings": {"line_width": w, "mode": mode, "aspect": aspect,
                         "lines": 2}})
        pres = lay["result"]["presentations"]
        dwells = [p["dwell"] for p in pres if p["dwell"] is not None]
        check("%s/%s/%dpx 无负驻留" % (aspect, mode, w),
              all(d >= 0 for d in dwells),
              str([d for d in dwells if d < 0][:5]))
        check("%s/%s/%dpx 结束不早于开始" % (aspect, mode, w),
              all(p["end"] is None or p["begin"] is None
                  or p["end"] >= p["begin"] for p in pres))

    # ---------- 5. 锁定单元与断点 ----------
    print("[5] 锁定单元与断点")
    lay = engine.get_layout(store, esid)
    j0 = next(t["j"] for t in lay["result"]["tokens"] if t["text"] == "3.")
    lay = engine.op_layout_revise(store, esid, {
        "settings": {"line_width": 300, "mode": "scroll"},
        "locks": [[j0, j0 + 3]], "breaks": []})
    res = lay["result"]
    holder = [l for l in res["lines"]
              if l["start"] <= j0 and j0 + 2 < l["end"]]
    check("锁定单元完整处于同一行", len(holder) == 1,
          json.dumps([l["text"] for l in res["lines"]], ensure_ascii=False))
    check("无锁定被拆问题",
          not any(i["type"] == "lock_split" for i in res["issues"]))
    lay = engine.op_layout_revise(store, esid, {"breaks": [j0 + 1]})
    res = lay["result"]
    check("锁内断点触发 lock_split",
          any(i["type"] == "lock_split" for i in res["issues"]))
    check("lock_split 问题带定位时段",
          all(i["start"] is not None for i in res["issues"]
              if i["type"] == "lock_split"))
    check("锁定被拆时可读性未定", res["readability"] == "undefined")
    lay = engine.op_layout_revise(store, esid, {"breaks": []})
    check("撤掉断点后锁定恢复",
          not any(i["type"] == "lock_split"
                  for i in lay["result"]["issues"]))

    # ---------- 6. 超宽 / 孤行 / 驻留不足 ----------
    print("[6] 超宽 / 孤行 / 驻留不足")
    lay = engine.op_layout_revise(store, esid, {
        "settings": {"line_width": 100, "orphan_chars": 2}, "locks": []})
    res = lay["result"]
    check("100px 行宽产生孤行",
          any(i["type"] == "orphan_line" for i in res["issues"]),
          json.dumps(res["aggregates"]["issues"]))
    lay = engine.op_layout_revise(store, esid, {
        "locks": [[j0, j0 + 3]]})   # 锁 3 词元（>100px）→ 必超宽
    res = lay["result"]
    check("锁定单元超行宽触发超宽",
          any(i["type"] == "overwide" for i in res["issues"]),
          json.dumps(res["aggregates"]["issues"]))
    ow = next(i for i in res["issues"] if i["type"] == "overwide")
    check("超宽问题带行号与时段",
          ow["line"] is not None and ow["start"] is not None)
    lay = engine.op_layout_revise(store, esid, {
        "settings": {"line_width": 960, "min_dwell": 8.0}, "locks": []})
    res = lay["result"]
    check("高最短驻留触发驻留不足",
          any(i["type"] == "dwell_short" for i in res["issues"]))
    ds = next(i for i in res["issues"] if i["type"] == "dwell_short")
    check("驻留不足问题可定位", ds["start"] is not None
          and ds["presentation"] is not None)
    lay = engine.op_layout_revise(store, esid, {
        "settings": {"min_dwell": 1.0}})
    check("恢复阈值后驻留不足消失",
          not any(i["type"] == "dwell_short"
                  for i in lay["result"]["issues"]))

    # ---------- 7. 修订累积（SQLite） ----------
    print("[7] 修订累积")
    revs = store.list_layout_revisions(esid)
    check("每次调整另存一条修订", len(revs) >= 10, str(len(revs)))
    check("修订号连续递增",
          [r["rev"] for r in revs] == list(range(1, len(revs) + 1)))
    latest = store.get_layout_revision(esid)
    check("最新修订可读回", latest and latest["rev"] == revs[-1]["rev"])
    check("修订带计算摘要", latest["result"]
          and "aggregates" in latest["result"])

    # ---------- 8. 缺页与非法输入 ----------
    print("[8] 缺页与非法输入")
    lay = engine.op_layout_revise(store, esid, {
        "settings": {"line_width": 480, "mode": "scroll"}})
    res = lay["result"]
    check("缺页覆盖呈现被标记",
          any(i["type"] == "snapshot_gap" for i in res["issues"]))
    gap_p = next(p for p in res["presentations"]
                 if "snapshot_gap" in p["undefined"])
    check("缺页呈现可读性未定", gap_p["readability"] == "undefined")
    sid8 = new_session(client, "非法")
    r = client.post("/api/session/%d/layout/revise" % sid8,
                    json={"settings": {"line_width": 10}})
    check("行宽越界 400", r.status_code == 400)
    r = client.post("/api/session/%d/layout/revise" % sid8,
                    json={"breaks": [10 ** 6]})
    check("断点越界 400", r.status_code == 400)
    r = client.post("/api/session/%d/layout/revise" % sid8,
                    json={"locks": [[2, 5], [4, 8]]})
    check("锁定重叠 400", r.status_code == 400)
    r = client.post("/api/session/%d/layout/revise" % sid8,
                    json={"settings": {"mode": "weird"}})
    check("未知模式 400", r.status_code == 400)
    r = client.put("/api/fonts/X/metrics", json={"units": {"cjk": 99}})
    check("字体度量非法 400", r.status_code == 400)

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
