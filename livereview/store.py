"""SQLite 持久化：会话、快照、锚点、校审决定、词元指标缓存、确认记录。"""

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, created REAL, status TEXT DEFAULT 'open',
  audio_path TEXT, ref_path TEXT, log_path TEXT,
  audio_duration REAL,
  log_sha256 TEXT, ref_sha256 TEXT, audio_sha256 TEXT,
  params TEXT, aggregates TEXT, interval_metrics TEXT, flags TEXT,
  recompute_log TEXT DEFAULT '[]',
  confirmed_at REAL, confirm_digest TEXT
);
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INT, seq INT, log_ts REAL, text TEXT
);
CREATE TABLE IF NOT EXISTS anchors(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INT, log_ts REAL, audio_ts REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS edits(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INT, type TEXT, payload TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS token_metrics(
  session_id INT, token_idx INT, data TEXT,
  PRIMARY KEY(session_id, token_idx)
);
CREATE TABLE IF NOT EXISTS utterances(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INT, start_idx INT, end_idx INT
);
CREATE TABLE IF NOT EXISTS recess(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INT, start REAL, end REAL
);
CREATE TABLE IF NOT EXISTS confirmations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INT, digest TEXT, created REAL, exports TEXT
);
CREATE TABLE IF NOT EXISTS layout_revisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INT, rev INT, created REAL, payload TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS font_metrics(
  font_family TEXT PRIMARY KEY,
  units TEXT, source TEXT, updated REAL
);
"""


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- 会话 ---
    def create_session(self, name, audio_path, ref_path, log_path,
                       audio_duration, digests, params):
        cur = self.conn.execute(
            "INSERT INTO sessions(name,created,status,audio_path,ref_path,"
            "log_path,audio_duration,log_sha256,ref_sha256,audio_sha256,"
            "params,recompute_log) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, time.time(), "open", audio_path, ref_path, log_path,
             audio_duration, digests["log"], digests["ref"], digests["audio"],
             json.dumps(params), "[]"))
        self.conn.commit()
        return cur.lastrowid

    def get_session(self, sid):
        r = self.conn.execute("SELECT * from sessions where id=?",
                              (sid,)).fetchone()
        return dict(r) if r else None

    def list_sessions(self):
        return [dict(r) for r in self.conn.execute(
            "select id,name,created,status,confirmed_at from sessions"
            " order by id")]

    def update_session(self, sid, **kw):
        cols = ", ".join("%s=?" % k for k in kw)
        self.conn.execute("update sessions set %s where id=?" % cols,
                          (*kw.values(), sid))
        self.conn.commit()

    # --- 快照 ---
    def add_snapshots(self, sid, snaps):
        self.conn.executemany(
            "insert into snapshots(session_id,seq,log_ts,text) values(?,?,?,?)",
            [(sid, s["seq"], s["log_ts"], s["text"]) for s in snaps])
        self.conn.commit()

    def get_snapshots(self, sid):
        return [dict(r) for r in self.conn.execute(
            "select seq,log_ts,text from snapshots where session_id=?"
            " order by seq", (sid,))]

    # --- 锚点 ---
    def set_anchors(self, sid, anchors, source="manual"):
        self.conn.execute("delete from anchors where session_id=?", (sid,))
        self.conn.executemany(
            "insert into anchors(session_id,log_ts,audio_ts,source)"
            " values(?,?,?,?)",
            [(sid, a["log_ts"], a["audio_ts"], source) for a in anchors])
        self.conn.commit()

    def get_anchors(self, sid):
        return [dict(r) for r in self.conn.execute(
            "select log_ts,audio_ts,source from anchors where session_id=?"
            " order by log_ts", (sid,))]

    # --- 校审决定 ---
    def add_edit(self, sid, etype, payload):
        self.conn.execute(
            "insert into edits(session_id,type,payload,created)"
            " values(?,?,?,?)",
            (sid, etype, json.dumps(payload, ensure_ascii=False),
             time.time()))
        self.conn.commit()

    def get_edits(self, sid, etype=None):
        q = "select type,payload,created from edits where session_id=?"
        args = (sid,)
        if etype:
            q += " and type=?"
            args = (sid, etype)
        return [{"type": r["type"], "payload": json.loads(r["payload"]),
                 "created": r["created"]}
                for r in self.conn.execute(q + " order by id", args)]

    # --- 词元指标缓存 ---
    def set_token_metric(self, sid, j, data):
        self.conn.execute(
            "insert or replace into token_metrics(session_id,token_idx,data)"
            " values(?,?,?)",
            (sid, j, json.dumps(data, ensure_ascii=False)))

    def flush(self):
        self.conn.commit()

    def get_token_metrics(self, sid):
        rows = self.conn.execute(
            "select token_idx,data from token_metrics where session_id=?"
            " order by token_idx", (sid,)).fetchall()
        return {r["token_idx"]: json.loads(r["data"]) for r in rows}

    def clear_token_metrics(self, sid):
        self.conn.execute("delete from token_metrics where session_id=?",
                          (sid,))
        self.conn.commit()

    # --- 话语（utterance） ---
    def set_utterances(self, sid, utts):
        self.conn.execute("delete from utterances where session_id=?", (sid,))
        self.conn.executemany(
            "insert into utterances(session_id,start_idx,end_idx)"
            " values(?,?,?)",
            [(sid, u["start"], u["end"]) for u in utts])
        self.conn.commit()

    def get_utterances(self, sid):
        return [dict(r) for r in self.conn.execute(
            "select start_idx as start,end_idx as end from utterances"
            " where session_id=? order by start_idx", (sid,))]

    # --- 休会段 ---
    def set_recess(self, sid, ranges):
        self.conn.execute("delete from recess where session_id=?", (sid,))
        self.conn.executemany(
            "insert into recess(session_id,start,end) values(?,?,?)",
            [(sid, r["start"], r["end"]) for r in ranges])
        self.conn.commit()

    def get_recess(self, sid):
        return [dict(r) for r in self.conn.execute(
            "select start,end from recess where session_id=? order by start",
            (sid,))]

    # --- 确认 ---
    def add_confirmation(self, sid, digest, exports):
        self.conn.execute(
            "insert into confirmations(session_id,digest,created,exports)"
            " values(?,?,?,?)",
            (sid, digest, time.time(), json.dumps(exports)))
        self.conn.commit()

    def get_confirmation(self, sid):
        r = self.conn.execute(
            "select digest,created,exports from confirmations"
            " where session_id=? order by id desc limit 1", (sid,)).fetchone()
        if not r:
            return None
        return {"digest": r["digest"], "created": r["created"],
                "exports": json.loads(r["exports"])}

    # --- 版面修订 ---
    def add_layout_revision(self, sid, payload, result):
        r = self.conn.execute(
            "select coalesce(max(rev),0) as m from layout_revisions"
            " where session_id=?", (sid,)).fetchone()
        rev = r["m"] + 1
        self.conn.execute(
            "insert into layout_revisions(session_id,rev,created,payload,"
            "result) values(?,?,?,?,?)",
            (sid, rev, time.time(),
             json.dumps(payload, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False) if result else None))
        self.conn.commit()
        return rev

    def get_layout_revision(self, sid, rev=None):
        if rev is None:
            r = self.conn.execute(
                "select rev,created,payload,result from layout_revisions"
                " where session_id=? order by rev desc limit 1",
                (sid,)).fetchone()
        else:
            r = self.conn.execute(
                "select rev,created,payload,result from layout_revisions"
                " where session_id=? and rev=?", (sid, rev)).fetchone()
        if not r:
            return None
        return {"rev": r["rev"], "created": r["created"],
                "payload": json.loads(r["payload"]),
                "result": json.loads(r["result"]) if r["result"] else None}

    def list_layout_revisions(self, sid):
        return [{"rev": r["rev"], "created": r["created"]}
                for r in self.conn.execute(
                    "select rev,created from layout_revisions"
                    " where session_id=? order by rev", (sid,))]

    # --- 字体度量 ---
    def set_font_metrics(self, family, units, source):
        self.conn.execute(
            "insert or replace into font_metrics(font_family,units,source,"
            "updated) values(?,?,?,?)",
            (family, json.dumps(units), source, time.time()))
        self.conn.commit()

    def get_font_metrics(self, family):
        r = self.conn.execute(
            "select units,source,updated from font_metrics"
            " where font_family=?", (family,)).fetchone()
        if not r:
            return None
        return {"units": json.loads(r["units"]), "source": r["source"],
                "updated": r["updated"]}

    def list_font_metrics(self):
        return [{"font_family": r["font_family"], "source": r["source"],
                 "updated": r["updated"]}
                for r in self.conn.execute(
                    "select font_family,source,updated from font_metrics"
                    " order by font_family")]
