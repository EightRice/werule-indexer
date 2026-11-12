"""The local edge stack werule reads with LOCAL=true, in one command:

    python dev.py                  the main local stack (werule's LOCAL=true defaults)
    python dev.py --e2e --store <new file> --out <empty dir> --networks dir:<root>
                                   the e2e ports, for test runs that must not touch the main stack

It runs, until Ctrl-C:
  - the supervisor: an indexer and a publisher per network file (NETWORKS_SOURCE), publishing to --out
  - publisher.serve: --out over HTTP (werule's EDGE_DATA_URL; main 8788, e2e 8798)
  - the Worker under wrangler dev (EDGE_LIVE_URL; main 8787, e2e 8797), which the publishers notify

Main defaults: the store data/werule.sqlite, the files ../local-edge, the network files that
trustless-contracts' deploys write (dir:../trustless-contracts/deployments). The local hardhat chain
(8545) and a deploy that writes deployments/networks/Localhost.json come first.
Logs: LOG_DIR (default logs/), one file per process.
"""
import argparse
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET = "dev-secret"


def kill_tree(p):
    if p.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    else:
        p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python dev.py")
    ap.add_argument("--e2e", action="store_true", help="the e2e ports (8798 data, 8797 live); needs --store and --out")
    ap.add_argument("--store", help="document store file")
    ap.add_argument("--out", help="where the publishers write and publisher.serve serves")
    ap.add_argument("--networks", help="NETWORKS_SOURCE (default dir:../trustless-contracts/deployments)")
    ap.add_argument("--poll", type=float, default=10.0, help="seconds between reads of the network files")
    a = ap.parse_args(argv)

    main_store = os.path.join(HERE, "data", "werule.sqlite")
    main_out = os.path.abspath(os.path.join(HERE, "..", "local-edge"))
    if a.e2e:
        if not a.store or not a.out:
            ap.error("--e2e needs --store and --out (never the main stack's)")
        if os.path.abspath(a.store) == os.path.abspath(main_store) or os.path.abspath(a.out) == main_out:
            ap.error("--e2e must not use the main store or the main local-edge folder")
        data_port, live_port, worker_script = 8798, 8797, "dev:e2e"
    else:
        data_port, live_port, worker_script = 8788, 8787, "dev"
    store = os.path.abspath(a.store or main_store)
    out = os.path.abspath(a.out or main_out)
    networks = a.networks or "dir:" + os.path.abspath(os.path.join(HERE, "..", "trustless-contracts", "deployments"))
    os.makedirs(out, exist_ok=True)
    log_dir = os.path.abspath(os.getenv("LOG_DIR", os.path.join(HERE, "logs")))
    os.makedirs(log_dir, exist_ok=True)

    worker_dir = os.path.join(HERE, "publisher", "worker")
    if not os.path.isdir(os.path.join(worker_dir, "node_modules")):
        sys.exit("run `npm install` in publisher/worker first")
    dev_vars = os.path.join(worker_dir, ".dev.vars")
    if not os.path.exists(dev_vars):
        with open(dev_vars, "w") as f:
            f.write(f"NOTIFY_SECRET={SECRET}\n")

    env = dict(os.environ, DOCSTORE_PATH=store, NETWORKS_SOURCE=networks, LOG_DIR=log_dir, PYTHONUNBUFFERED="1",
               NOTIFY_URL=f"http://127.0.0.1:{live_port}/notify", NOTIFY_SECRET=SECRET)
    suffix = "-e2e" if a.e2e else ""
    procs = []

    def start(name, argv, cwd=HERE, shell=False):
        f = open(os.path.join(log_dir, f"dev-{name}{suffix}.log"), "ab")
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT, shell=shell)
        procs.append((name, p))
        print(f"{name}: pid {p.pid}, log {f.name}")

    # The local Worker checks debate messages against the local networks.json, not production's
    start("worker", f"npm run {worker_script} -- --var DATA_URL:http://127.0.0.1:{data_port}", cwd=worker_dir, shell=True)
    start("serve", [sys.executable, "-u", "-m", "publisher.serve", "--dir", out, "--port", str(data_port)])
    time.sleep(3)  # the Worker takes a moment; a publisher's early notifies are retried anyway
    start("supervisor", [sys.executable, "-u", "supervisor.py", "--target", f"dir:{out}", "--poll", str(a.poll)])
    print(f"\nedge data http://127.0.0.1:{data_port}  live ws://127.0.0.1:{live_port}  store {store}\n"
          f"networks {networks}\nfiles {out}\nCtrl-C stops everything.")

    stopping = []
    signal.signal(signal.SIGTERM, lambda *x: stopping.append(1))
    try:
        while not stopping:
            for name, p in procs:
                if p.poll() is not None:
                    print(f"{name} exited with {p.returncode}; see its log. Stopping.")
                    stopping.append(1)
                    break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for name, p in reversed(procs):
            kill_tree(p)
        print("stopped")


if __name__ == "__main__":
    main()
