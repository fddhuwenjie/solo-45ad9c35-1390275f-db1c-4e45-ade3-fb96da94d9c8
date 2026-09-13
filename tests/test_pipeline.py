"""管线冒烟测试：导入演示数据 → 计算 → 校审操作 → 确认导出。

运行：PYTHONPATH=/tmp/pylibs/local/lib/python3.11/dist-packages \
      python3 tests/test_pipeline.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import make_demo
from livereview import engine, pipeline
from livereview.store import Store

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, extra))


def fresh_store():
    d = tempfile.mkdtemp(prefix="lrt_")
    return Store(os.path.join(d, "t.db")), d


def import_demo(store, tmp):
    paths = make_demo.build(os.path.join(tmp, "demo"))
    return engine.import_session(store, "t", paths["audio"],
                                 paths["reference"], paths["log"],
                                 paths["anchors"])


def main():
    # ---------- 1. 演示数据导入与指标 ----------
    print("[1] 导入演示数据并全量计算")
    store, tmp = fresh_store()
    sid = import_demo(store, tmp)
    st = engine.state(store, sid)
    ag = st["aggregates"]
    check("会话已建", st["session"]["status"] == "open")
    check("词元数合理", 100 < st["token_count"] < 200,
          str(st["token_count"]))
    check("首显延迟已定义且为正",
          ag["first_latency"] and 0.3 < ag["first_latency"]["mean"] < 5,
          json.dumps(ag["first_latency"]))
    check("稳定延迟 >= 首显延迟",
          ag["stable_latency"]["mean"] >= ag["first_latency"]["mean"])
    check("存在改写词元",
          sum(1 for t in st["tokens"]
              if t["replace_count"] and t["replace_count"] >= 1) >= 5)
    check("存在撤回词元",
          any(t["retract_count"] and t["retract_count"] >= 1
              for t in st["tokens"]))
    # 缺页新语义：跨缺口文本变化的词元计数未定，聚合总数同步未定
    none_counts = [t for t in st["tokens"] if t["replace_count"] is None]
    check("缺页影响词元计数未定", len(none_counts) >= 1,
          str(len(none_counts)))
    check("缺页影响词元撤回数同样未定",
          all(t["retract_count"] is None for t in none_counts))
    check("聚合改写总数未定", ag["replace_total"] is None)
    check("聚合撤回总数未定", ag["retract_total"] is None)
    check("派生聚合(改写词元数)未定", ag["rewritten_tokens"] is None)
    check("计数未定原因标记为缺页",
          ag["counts_defined"] is False
          and ag["counts_undefined_reason"] == "snapshot_gap")
    iv = st["interval_metrics"]
    check("阅读速度已定义", iv["reading_speed"]["defined"],
          json.dumps(iv["reading_speed"]))
    check("检测到无字幕区间",
          iv["uncaptioned"]["defined"] and
          any(2.0 <= r["duration"] <= 4.0 for r in
              iv["uncaptioned"]["ranges"]),
          json.dumps(iv["uncaptioned"]))
    check("覆盖范围完整", iv["coverage"]["defined"],
          json.dumps(iv["coverage"]))
    kinds = {f["type"] for f in st["flags"]}
    check("标记了缺页", "snapshot_gap" in kinds, str(kinds))
    check("标记了对齐多解", "ambiguous_alignment" in kinds, str(kinds))
    # 缺页后的词元首显延迟应未定
    gap_undef = [t for t in st["tokens"]
                 if "snapshot_gap" in t["undefined"]]
    check("缺页影响词元首显未定", len(gap_undef) >= 1, str(len(gap_undef)))
    amb = [t for t in st["tokens"] if "ambiguous_alignment" in t["undefined"]]
    check("多解词元延迟未定", len(amb) >= 1, str(len(amb)))
    hom = [t for t in st["tokens"] if t["status"] == "sub"]
    check("存在同音替换词元(的/得)", any(t["text"] == "得" for t in hom))

    # ---------- 2. 校审操作与区间重算 ----------
    print("[2] 校审操作")
    log0 = len(st["recompute_log"])
    # 重绑同音词：得 → 参考稿的 的
    tgt = next(t for t in st["tokens"]
               if t["status"] == "sub" and t["text"] == "得")
    st2 = engine.op_rebind(store, sid, tgt["j"], tgt["ref_idx"])
    tk = st2["tokens"][tgt["j"]]
    check("重绑后状态为 rebind", tk["status"] == "rebind")
    check("重绑后文本取参考稿", tk["ref_text"] == "的", tk["ref_text"])
    entry = st2["recompute_log"][-1]
    check("重绑只重算 1 个词元",
          entry["tokens_recomputed"] == 1 and entry["scope"] == "partial",
          json.dumps(entry))
    # 拆分与合并
    n_utt = len(st2["utterances"])
    st3 = engine.op_split(store, sid, 5)
    check("拆分后话语数 +1", len(st3["utterances"]) == n_utt + 1)
    st4 = engine.op_merge(store, sid, 0)
    check("合并后话语数还原", len(st4["utterances"]) == n_utt)
    # 休会段：圈出无字幕区间后，该区间不再计入
    unc_before = st4["interval_metrics"]["uncaptioned"]["ranges"]
    r = unc_before[0]
    st5 = engine.op_set_recess(store, sid,
                               [{"start": r["start"], "end": r["end"]}])
    unc_after = st5["interval_metrics"]["uncaptioned"]["ranges"]
    check("休会段从无间区间中扣除",
          all(not (u["start"] == r["start"] and u["end"] == r["end"])
              for u in unc_after),
          json.dumps(unc_after))
    check("休会只重算区间指标",
          st5["recompute_log"][-1]["tokens_recomputed"] == 0)
    st5 = engine.op_set_recess(store, sid, [])  # 还原

    # 锚点改动 → 只重算受影响区间
    anchors = [dict(a) for a in st5["anchors"]]
    anchors[1]["audio_ts"] += 0.3  # 中间锚点挪动
    st6 = engine.op_set_anchors(store, sid, anchors)
    entry = st6["recompute_log"][-1]
    check("锚点改动为部分重算", entry["scope"] == "partial",
          json.dumps(entry))
    check("受影响词元少于全部",
          0 < entry["tokens_recomputed"] < st6["token_count"],
          "%d/%d" % (entry["tokens_recomputed"], st6["token_count"]))

    # ---------- 3. 确认与导出 ----------
    print("[3] 确认导出")
    res = engine.confirm(store, sid, os.path.join(tmp, "exp"))
    check("摘要 64 位十六进制", len(res["digest"]) == 64)
    for k in ("vtt", "csv", "svg", "json"):
        check("导出 %s" % k, os.path.exists(res["exports"][k]))
    with open(res["exports"]["vtt"], encoding="utf-8") as f:
        vtt = f.read()
    check("VTT 头部正确", vtt.startswith("WEBVTT"))
    check("VTT 含重绑修正(的)", "依赖字幕的观众" in vtt)
    with open(res["exports"]["csv"], encoding="utf-8-sig") as f:
        rows = f.read().splitlines()
    check("CSV 行数 = 词元数+1", len(rows) == st6["token_count"] + 1,
          str(len(rows)))
    with open(res["exports"]["json"], encoding="utf-8") as f:
        rj = json.load(f)
    check("复算 JSON 含锚点/决定/重算日志",
          "anchors" in rj and "edits" in rj and "recompute_log" in rj)
    with open(res["exports"]["svg"], encoding="utf-8") as f:
        check("SVG 合法开端", f.read(100).startswith("<svg") or "<svg" in f.read(0))
    try:
        engine.op_rebind(store, sid, 0, 0)
        check("锁定后禁止修改", False)
    except RuntimeError:
        check("锁定后禁止修改", True)

    # ---------- 4. 未定条件 ----------
    print("[4] 未定条件")
    # 4a. 时标倒序
    store2, tmp2 = fresh_store()
    paths = make_demo.build(os.path.join(tmp2, "demo"))
    lines = open(paths["log"], encoding="utf-8").read().splitlines()
    objs = [json.loads(x) for x in lines]
    objs[60]["log_ts"] = objs[10]["log_ts"]  # 制造倒序
    with open(paths["log"], "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    sid2 = engine.import_session(store2, "t2", paths["audio"],
                                 paths["reference"], paths["log"],
                                 paths["anchors"])
    st = engine.state(store2, sid2)
    check("倒序被标记", any(f["type"] == "timestamp_nonmonotonic"
                            for f in st["flags"]))
    mono_undef = [t for t in st["tokens"]
                  if "timestamp_nonmonotonic" in t["undefined"]]
    check("倒序后事件延迟未定", len(mono_undef) > 0, str(len(mono_undef)))

    # 4b. 锚点残差过大
    store3, tmp3 = fresh_store()
    paths = make_demo.build(os.path.join(tmp3, "demo"))
    bad = json.load(open(paths["anchors"], encoding="utf-8"))
    bad[1]["audio_ts"] += 3.0
    json.dump(bad, open(paths["anchors"], "w"))
    sid3 = engine.import_session(store3, "t3", paths["audio"],
                                 paths["reference"], paths["log"],
                                 paths["anchors"])
    st = engine.state(store3, sid3)
    check("残差超限被标记", st["residual_exceeded"])
    check("残差超限时延迟未定",
          all("anchor_residual_exceeded" in t["undefined"]
              for t in st["tokens"]))
    check("残差超限时阅读速度未定",
          not st["interval_metrics"]["reading_speed"]["defined"])

    # 4c. 音频范围不完整（日志提前结束）
    store4, tmp4 = fresh_store()
    paths = make_demo.build(os.path.join(tmp4, "demo"))
    objs = [json.loads(x) for x in
            open(paths["log"], encoding="utf-8").read().splitlines()]
    objs = [o for o in objs if o["log_ts"] < 500.0 + 30 * 1.0002]
    with open(paths["log"], "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    sid4 = engine.import_session(store4, "t4", paths["audio"],
                                 paths["reference"], paths["log"],
                                 paths["anchors"])
    st = engine.state(store4, sid4)
    check("覆盖不完整被标记",
          not st["interval_metrics"]["coverage"]["defined"])
    check("覆盖不完整时无字幕区间未定",
          not st["interval_metrics"]["uncaptioned"]["defined"])

    # 4d. 无锚点（用连续编号的日志，隔离缺页因素）
    store5, tmp5 = fresh_store()
    paths = make_demo.build(os.path.join(tmp5, "demo"))
    objs = [json.loads(x) for x in
            open(paths["log"], encoding="utf-8").read().splitlines()]
    for k, o in enumerate(objs, 1):
        o["seq"] = k  # 重排为连续序号 → 无缺页
    with open(paths["log"], "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    sid5 = engine.import_session(store5, "t5", paths["audio"],
                                 paths["reference"], paths["log"], None)
    st = engine.state(store5, sid5)
    check("无锚点时延迟未定",
          all("no_anchor" in t["undefined"] for t in st["tokens"]))
    check("无锚点时改写计数仍可用",
          st["aggregates"]["counts_defined"] is True
          and st["aggregates"]["replace_total"] > 0)

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
