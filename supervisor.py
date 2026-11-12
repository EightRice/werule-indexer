"""Runs an indexer and a publisher for every network file, and keeps them running.

    python supervisor.py --target r2:<bucket> | dir:<path>

Every POLL seconds it reads the network files (NETWORKS_SOURCE, see networks.py):
  - a new file starts that network's indexer (app.py <name>) and publisher (python -m publisher);
  - a changed file restarts both, so new addresses or a new RPC take effect;
  - a removed file stops both (the network's documents stay in the store and the published files);
  - a process that exits is restarted, after a pause that grows while it keeps failing.
Exactly one publisher writes networks.json: the one of the first network by name.

Children share this process's environment (DOCSTORE_PATH, NOTIFY_URL, NOTIFY_SECRET, R2_*); their
output goes to LOG_DIR/<network>-indexer.log and LOG_DIR/<network>-publisher.log.
"""
import argparse
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time

import networks

log = logging.getLogger("supervisor")
HERE = os.path.dirname(os.path.abspath(__file__))
BACKOFF_MAX = 300


class Child:
    def __init__(self, name, argv, log_path):
        self.name, self.argv, self.log_path = name, argv, log_path
        self.proc = None
        self.failures = 0
        self.next_start = 0.0
        self.started_at = 0.0

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        f = open(self.log_path, "ab")
        self.proc = subprocess.Popen(self.argv, cwd=HERE, stdout=f, stderr=subprocess.STDOUT,
                                     env=dict(os.environ, PYTHONUNBUFFERED="1"))
        f.close()
        self.started_at = time.monotonic()
        log.info("started %s (pid %d)", self.name, self.proc.pid)

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
            log.info("stopped %s", self.name)
        self.proc = None

    def tend(self, now):
        """Restart after an exit, backing off while it keeps failing quickly."""
        if self.running():
            if now - self.started_at > 600:
                self.failures = 0
            return
        if self.proc is not None:
            code = self.proc.returncode
            self.proc = None
            self.failures += 1
            delay = min(BACKOFF_MAX, 5 * 2 ** min(self.failures - 1, 6))
            self.next_start = now + delay
            log.warning("%s exited with %s; restarting in %ds (see %s)", self.name, code, delay, self.log_path)
        if now >= self.next_start:
            self.start()


class Network:
    def __init__(self, name, digest, target, log_dir, index):
        self.name, self.digest, self.index = name, digest, index
        self.indexer = Child(f"{name} indexer", [sys.executable, "-u", "app.py", name],
                             os.path.join(log_dir, f"{name}-indexer.log"))
        pub = [sys.executable, "-u", "-m", "publisher", "--network", name, "--target", target]
        if index:
            pub.append("--networks-index")
        self.publisher = Child(f"{name} publisher", pub, os.path.join(log_dir, f"{name}-publisher.log"))

    def tend(self, now, store):
        self.indexer.tend(now)
        # The publisher reads the store the indexer creates
        if os.path.exists(store):
            self.publisher.tend(now)

    def stop(self):
        self.publisher.stop()
        self.indexer.stop()


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python supervisor.py")
    ap.add_argument("--target", required=True, help="where the publishers write: dir:<path> or r2:<bucket>")
    ap.add_argument("--poll", type=float, default=60.0, help="seconds between reads of the network files")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_dir = os.path.abspath(os.getenv("LOG_DIR", os.path.join(HERE, "logs")))
    os.makedirs(log_dir, exist_ok=True)
    store = os.path.abspath(os.getenv("DOCSTORE_PATH") or os.path.join(HERE, "data", "werule.sqlite"))
    os.environ["DOCSTORE_PATH"] = store

    running = {}
    stopping = []
    signal.signal(signal.SIGTERM, lambda *a: stopping.append(1))
    last_read = -args.poll
    try:
        while not stopping:
            now = time.monotonic()
            if now - last_read >= args.poll:
                last_read = now
                try:
                    found = networks.list_networks()
                except Exception as e:
                    log.error("cannot read the network files: %s", e)
                    found = None
                if found is not None:
                    first = min(found) if found else None
                    for name in list(running):
                        net = running[name]
                        if name not in found:
                            log.info("network %s removed: stopping it", name)
                            net.stop()
                            del running[name]
                        elif net.digest != digest(found[name]) or net.index != (name == first):
                            log.info("network %s changed: restarting it", name)
                            net.stop()
                            del running[name]
                    for name in sorted(found):
                        if name not in running:
                            log.info("network %s: starting", name)
                            running[name] = Network(name, digest(found[name]), args.target, log_dir, name == first)
            for net in running.values():
                net.tend(now, store)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for net in running.values():
            net.stop()


if __name__ == "__main__":
    main()
