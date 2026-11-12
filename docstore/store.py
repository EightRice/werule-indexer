"""The SQLite file behind the document store.

One file holds every network's documents. Each committed write gets a version `v`: integer
microseconds, strictly increasing across the whole file (several indexer processes may write it;
a write transaction takes the file's write lock, so versions never repeat). Every write also
appends to the change log, which the publisher tails.

Tables:
  docs(path, parent, cid, data, v, ct)   one row per document; data is tagged JSON (values.py)
  changes(seq, path, v, deleted)         one row per committed document write
  clock(id, v)                           the last version handed out
"""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    path   TEXT PRIMARY KEY,
    parent TEXT NOT NULL,
    cid    TEXT NOT NULL,
    data   TEXT NOT NULL,
    v      INTEGER NOT NULL,
    ct     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS docs_parent ON docs(parent);
CREATE INDEX IF NOT EXISTS docs_cid ON docs(cid);
CREATE TABLE IF NOT EXISTS changes (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    path    TEXT NOT NULL,
    v       INTEGER NOT NULL,
    deleted INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    v  INTEGER NOT NULL
);
INSERT OR IGNORE INTO clock (id, v) VALUES (1, 0);
"""


def split(path):
    """'a/b/c/d' -> ('a/b/c', 'c'): the parent collection path and the collection id."""
    parent = path.rsplit("/", 1)[0]
    return parent, parent.rsplit("/", 1)[-1]


class Store:
    def __init__(self, path, timeout=60.0):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=timeout, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    def close(self):
        with self.lock:
            self.conn.close()

    # ---------- reads ----------

    def read(self, path):
        """(data_json, v, ct) or None"""
        with self.lock:
            return self.conn.execute("SELECT data, v, ct FROM docs WHERE path = ?", (path,)).fetchone()

    def children(self, parent):
        """[(path, data_json, v, ct)] of the collection at `parent`"""
        with self.lock:
            return self.conn.execute(
                "SELECT path, data, v, ct FROM docs WHERE parent = ? ORDER BY path", (parent,)).fetchall()

    def group(self, cid):
        """[(path, data_json, v, ct)] of every collection named `cid`"""
        with self.lock:
            return self.conn.execute(
                "SELECT path, data, v, ct FROM docs WHERE cid = ? ORDER BY path", (cid,)).fetchall()

    def under(self, prefix):
        """[(path, data_json, v)] of every document whose path starts with prefix + '/'"""
        hi = prefix + "/\U0010ffff"
        with self.lock:
            return self.conn.execute(
                "SELECT path, data, v FROM docs WHERE path > ? AND path < ? ORDER BY path",
                (prefix + "/", hi)).fetchall()

    def head(self):
        """(last change seq, last version)"""
        with self.lock:
            seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0]
            v = self.conn.execute("SELECT v FROM clock WHERE id = 1").fetchone()[0]
            return seq, v

    def changes_since(self, seq, limit=5000):
        """[(seq, path, v, deleted)] after seq, and the lowest seq still kept"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT seq, path, v, deleted FROM changes WHERE seq > ? ORDER BY seq LIMIT ?",
                (seq, limit)).fetchall()
            low = self.conn.execute("SELECT COALESCE(MIN(seq), 0) FROM changes").fetchone()[0]
            return rows, low

    # ---------- writes ----------

    def write(self, fn):
        """Run fn(tx) inside one write transaction; tx.put/tx.remove apply document writes.
        Returns fn's result. Versions are assigned at commit, one per written document."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                last = self.conn.execute("SELECT v FROM clock WHERE id = 1").fetchone()[0]
                tx = _Tx(self.conn, max(int(time.time() * 1_000_000), last + 1))
                out = fn(tx)
                self.conn.execute("UPDATE clock SET v = ? WHERE id = 1", (max(last, tx.v - 1),))
                self.conn.execute("COMMIT")
                return out
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def prune_changes(self, keep=200_000):
        """Drop the oldest change rows, keeping the last `keep`. A tail that falls behind the kept
        range resyncs from a full read."""
        with self.lock:
            self.conn.execute("DELETE FROM changes WHERE seq <= (SELECT MAX(seq) FROM changes) - ?", (keep,))


class _Tx:
    def __init__(self, conn, v):
        self.conn = conn
        self.v = v          # the version the next document write gets
        self.now_us = v     # commit time, for SERVER_TIMESTAMP

    def read(self, path):
        return self.conn.execute("SELECT data, v, ct FROM docs WHERE path = ?", (path,)).fetchone()

    def put(self, path, data_json):
        row = self.conn.execute("SELECT ct FROM docs WHERE path = ?", (path,)).fetchone()
        v = self._next()
        ct = row[0] if row else v
        parent, cid = split(path)
        self.conn.execute(
            "INSERT INTO docs (path, parent, cid, data, v, ct) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET data = excluded.data, v = excluded.v",
            (path, parent, cid, data_json, v, ct))
        self.conn.execute("INSERT INTO changes (path, v, deleted) VALUES (?, ?, 0)", (path, v))
        return v

    def remove(self, path):
        cur = self.conn.execute("DELETE FROM docs WHERE path = ?", (path,))
        if cur.rowcount:
            v = self._next()
            self.conn.execute("INSERT INTO changes (path, v, deleted) VALUES (?, ?, 1)", (path, v))
            return v
        return None

    def _next(self):
        v = self.v
        self.v += 1
        return v
