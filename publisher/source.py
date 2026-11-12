"""The document store feeding the publisher (CONTRACT.md section 5.1).

Startup reads every document of the network once (the DAO collection and everything under it,
plus contracts/* for networks.json), then a thread tails the store's change log and hands each
changed document to the publisher in its current state. Several changes to one document between
two polls arrive as one; the publisher ignores a document whose version it already has.

If the indexer pruned the change log past the point this feed had read, changes may have been
missed: the feed reports itself dead, the publisher exits, and the supervisor restarts it with a
full read (which sends only the real differences).
"""
import logging
import threading
import time

from docstore import Store
from docstore.values import loads

log = logging.getLogger("publisher.source")

GROUPS = ("proposals", "votes", "members", "activity", "projects", "backers")


class StoreFeed:
    def __init__(self, store_path, C, publisher, networks_index, poll=0.2):
        self.store = Store(store_path)
        self.C, self.pub, self.networks_index, self.poll = C, publisher, networks_index, poll
        self.seq = 0
        self.error = None
        self._stop = threading.Event()
        self.thread = None

    def _apply_contract(self, path, data_json):
        self.pub.apply_contract(path.split("/", 1)[1], loads(data_json) if data_json is not None else None)

    def load(self):
        """The full read: everything the publisher mirrors, as of one point of the change log."""
        with self.store.lock:
            self.store.conn.execute("BEGIN")
            try:
                seq, v0 = self.store.head()
                docs = self.store.under(self.C)
                contracts = self.store.children("contracts") if self.networks_index else []
            finally:
                self.store.conn.execute("COMMIT")
        self.seq = seq
        if self.networks_index:
            for path, data, _, _ in contracts:
                self._apply_contract(path, data)
        for path, data, v in docs:
            self.pub.apply(path, v, loads(data), v0)
        for g in ("daos",) + GROUPS:
            self.pub.first_read[g] = v0
        log.info("full read: %d documents under %s, change log at %d", len(docs), self.C, seq)

    def step(self):
        """Apply the changes after self.seq; returns how many change rows were read."""
        rows, low = self.store.changes_since(self.seq)
        if rows and low > self.seq + 1 and self.seq > 0:
            raise RuntimeError(f"change log pruned past {self.seq} (oldest kept {low})")
        latest = {}
        for seq, path, v, deleted in rows:
            latest[path] = (v, deleted)
            self.seq = seq
        for path, (v, deleted) in latest.items():
            if path.startswith("contracts/") and path.count("/") == 1:
                if self.networks_index:
                    row = self.store.read(path)
                    self._apply_contract(path, row[0] if row else None)
            elif path.startswith(self.C + "/"):
                row = self.store.read(path)
                if row is None:
                    self.pub.apply(path, None, None, v)
                else:
                    self.pub.apply(path, row[1], loads(row[0]), row[1])
        return len(rows)

    def _run(self):
        try:
            while not self._stop.is_set():
                if self.step() == 0:
                    self._stop.wait(self.poll)
        except Exception as e:
            self.error = e
            log.exception("change feed stopped")

    def start(self):
        self.thread = threading.Thread(target=self._run, name="feed", daemon=True)
        self.thread.start()
        return self

    def dead(self):
        return self.error is not None or (self.thread is not None and not self.thread.is_alive()
                                          and not self._stop.is_set())

    def stop(self):
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.store.close()
