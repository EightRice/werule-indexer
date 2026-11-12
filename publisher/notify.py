"""Notify queues and the signed POST to the Worker (CONTRACT.md sections 5.2 and 6)."""
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass

from .encoding import canonical

log = logging.getLogger("publisher.notify")

BODY_MAX = 512 * 1024
MSG_MAX = 256 * 1024
MAX_MESSAGES = 20
QUEUE_MAX_BYTES = 1024 * 1024
QUEUE_MAX_AGE = 60.0


def sign(secret, timestamp, body):
    mac = hmac.new(secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + body, hashlib.sha256)
    return "v1=" + mac.hexdigest()


def body_bytes(raw_messages):
    return b'{"messages":[' + b",".join(raw_messages) + b"]}"


@dataclass
class Msg:
    channel: str
    seq: int
    obj: dict
    raw: bytes
    at: float


def make_msg(obj, at):
    return Msg(obj["channel"], obj["seq"], obj, canonical(obj), at)


def resync_obj(channel, seq, head, counts):
    o = {"channel": channel, "seq": seq, "kind": "resync", "head": head}
    if head is not None and counts is not None:
        o["counts"] = counts
    return o


def http_post(url, body, headers, timeout=30):
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Notifier:
    """One queue per channel; sends in seq order, collapses a stuck queue into one resync."""

    def __init__(self, url, secret, post=http_post, clock=time.time, on_stale=None):
        self.url, self.secret, self.post, self.clock = url, secret, post, clock
        self.on_stale = on_stale
        self.queues = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.backoff = 1.0
        self.next_try = 0.0
        self.sent = []  # (channel, seq, kind) accepted, for tests and logs
        self.posted = {}  # channel -> highest seq this process has posted
        self._stop = False

    def enqueue(self, msgs):
        with self.lock:
            for m in msgs:
                self.queues.setdefault(m.channel, deque()).append(m)
            # bound memory while a POST is backing off: collapse here too, not only before a send
            self._collapse(self.clock())
        self.wake.set()

    def replace(self, channel, msg):
        with self.lock:
            self.queues[channel] = deque([msg])
        self.wake.set()

    def pending(self):
        with self.lock:
            return sum(len(q) for q in self.queues.values())

    def _collapse(self, now):
        for ch, q in self.queues.items():
            if not q or (len(q) == 1 and q[0].obj["kind"] == "resync"):
                continue
            total = sum(len(m.raw) for m in q)
            if total > QUEUE_MAX_BYTES or now - q[0].at > QUEUE_MAX_AGE:
                last = q[-1].obj
                r = make_msg(resync_obj(ch, last["seq"], last["head"], last.get("counts")), now)
                log.warning("notify queue for %s collapsed into one resync (seq %s)", ch, r.seq)
                self.queues[ch] = deque([r])

    def _batch(self):
        chans = sorted((q[0].at, ch) for ch, q in self.queues.items() if q)
        picked, size = [], len(body_bytes([]))
        for _, ch in chans:
            for m in self.queues[ch]:
                add = len(m.raw) + (1 if picked else 0)
                if len(picked) >= MAX_MESSAGES or (picked and size + add > BODY_MAX):
                    break
                picked.append(m)
                size += add
            if len(picked) >= MAX_MESSAGES:
                break
        return picked

    def step(self):
        """One POST attempt. Returns True if something was accepted."""
        now = self.clock()
        with self.lock:
            self._collapse(now)
            batch = self._batch()
        if not batch:
            return False
        body = body_bytes([m.raw for m in batch])
        ts = int(self.clock())
        # A named User-Agent: Cloudflare refuses Python's default one (error 1010) before the Worker sees it
        headers = {"Content-Type": "application/json", "User-Agent": "werule-publisher/1.0",
                   "X-Edge-Timestamp": str(ts), "X-Edge-Signature": sign(self.secret, ts, body)}
        status, resp = None, b""
        try:
            status, resp = self.post(self.url, body, headers)
            results = json.loads(resp)["results"] if status == 200 else None
            if not isinstance(results, list) or not all(
                    isinstance(r, dict) and r.get("status") in ("ok", "duplicate", "stale") for r in results):
                results = None
        except Exception as e:
            log.warning("notify POST failed: %s", e)
            results = None
        with self.lock:
            for m in batch:
                self.posted[m.channel] = max(self.posted.get(m.channel, 0), m.seq)
        if not results or len(results) != len(batch):
            if status in (400, 413):
                log.error("notify POST rejected with %s (a publisher bug): %s", status, resp[:300])
            log.warning("notify POST not accepted (status %s); retrying in %.0fs",
                        status, self.backoff)
            self.next_try = self.clock() + self.backoff
            self.backoff = min(self.backoff * 2, 60.0)
            return False
        self.backoff = 1.0
        stale = {}
        with self.lock:
            for m, r in zip(batch, results):
                q = self.queues.get(m.channel)
                st = r.get("status")
                last = r.get("lastSeq")
                if st == "stale" and isinstance(last, int) and m.seq < last <= self.posted.get(m.channel, 0):
                    # The Worker holds a seq this process posted earlier: an earlier attempt was
                    # applied and this retry was packed differently. Same meaning as duplicate.
                    st, done = "duplicate", last
                else:
                    done = m.seq
                if st in ("ok", "duplicate"):
                    while q and q[0].seq <= done:
                        q.popleft()
                    self.sent.append((m.channel, m.seq, m.obj["kind"]))
                    stale.pop(m.channel, None)
                elif st == "stale":
                    stale[m.channel] = last if isinstance(last, int) else 0
        for ch, last in stale.items():
            log.error("notify: channel %s is stale at the Worker (lastSeq %s); a second publisher?", ch, last)
            if self.on_stale:
                self.on_stale(ch, last)
        return True

    def run(self):
        while not self._stop:
            wait = self.next_try - self.clock()
            if wait > 0:
                self.wake.wait(min(wait, 1.0))
                self.wake.clear()
                continue
            if self.pending() == 0:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            try:
                self.step()
            except Exception:
                log.exception("notify step failed; retrying in %.0fs", self.backoff)
                self.next_try = self.clock() + self.backoff
                self.backoff = min(self.backoff * 2, 60.0)

    def start(self):
        t = threading.Thread(target=self.run, name="notify", daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop = True
        self.wake.set()
