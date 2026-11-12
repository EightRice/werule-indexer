# apps/debates/indexer.py
#
# The debates module's place in app.py: no chain events and no contracts to follow; a thread that
# resolves debates.* registry changes, applies the Worker's queued messages and keeps tallies current.

import json
import os
import threading
import time
import traceback
import urllib.request

from apps.debates.config import POLL_SECONDS
from apps.debates.engine import Debates


def initialize(db, web3, network_config):
    return {"event_signatures": {}}


def setup_papers(db, web3, network_config):
    return {"addresses": [], "papers": {}}


def run_historical_sync(db, web3, network_config, alert_func):
    pass


class WorkerQueue:
    """GET {live}/debates/queue, signed like the publisher's notify (X-Edge-Timestamp, X-Edge-Signature)"""

    def __init__(self, base_url, secret, network, timeout=20):
        self.base, self.secret, self.network, self.timeout = base_url.rstrip("/"), secret, network, timeout

    def fetch(self, after):
        from publisher.notify import sign
        ts = int(time.time())
        req = urllib.request.Request(
            f"{self.base}/debates/queue?network={self.network}&after={int(after)}",
            headers={"X-Edge-Timestamp": str(ts), "X-Edge-Signature": sign(self.secret, ts, b""),
                     "User-Agent": "werule-indexer/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))


def live_base():
    """The Worker's base URL: DEBATES_QUEUE_URL, else NOTIFY_URL without /notify"""
    url = os.getenv("DEBATES_QUEUE_URL") or os.getenv("NOTIFY_URL") or ""
    return url[:-len("/notify")] if url.endswith("/notify") else url


def start(db, network_name, network_config, network_file, alert=None):
    """Runs the module in a daemon thread; returns the engine"""
    secret = os.getenv("NOTIFY_SECRET")
    base = live_base()
    queue = WorkerQueue(base, secret, network_name) if base and secret else None
    if queue is None:
        print("[debates] no Worker (NOTIFY_URL and NOTIFY_SECRET): debates open and retire, no messages")
    engine = Debates(db, network_config["dao_collection_name"], int(network_file.get("chainId") or 0), queue=queue)

    def loop():
        while True:
            try:
                engine.tick()
            except Exception as e:
                print(f"[debates] tick failed: {e}")
                traceback.print_exc()
                if alert:
                    alert(f"debates tick failed: {e}")
            time.sleep(POLL_SECONDS)

    threading.Thread(target=loop, name="debates", daemon=True).start()
    print(f"[debates] running for {network_name}" + (f", queue {base}/debates/queue" if queue else ""))
    return engine
