"""Acceptance checks against a scratch document store (never the main data/werule.sqlite).

Run from the repo root:
  python -m publisher.tests.store_e2e --store <new .sqlite file> --dir <empty dir> [--stub-port 8796]
Seeds idaosLocalhost, runs "python -m publisher" as a subprocess with a local notify stub, and checks
CONTRACT.md section 12 (publisher items).
"""
import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = "e2e-secret"
N, C = "Localhost", "idaosLocalhost"
DAO1 = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
DAO2 = "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512"
VOTERS = ["0x70997970C51812dc3A010C7d01b50e0d17dc79C8", "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
          "0x90F79bf6EB2c4f870365E785982E1f101E93b906"]
PROJECT = "0x9fe46736679d2d9a65f0992f2272de9f3c7fa6e0"
BACKERS = ["0x15d34aaf54267db7d7c367839aaf71a00a2c6a65", "0x9965507d1a55bcc2695c58ba16fb37d819b0a4dc"]


class Stub:
    """Notify endpoint: checks the signature, that the pointer and head exist, records messages."""

    def __init__(self, port, root):
        self.root = root
        self.up = True
        self.msgs = []
        self.errors = []
        self.last = {}
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if not stub.up:
                    self.send_response(503)
                    self.end_headers()
                    return
                ts = self.headers["X-Edge-Timestamp"]
                want = "v1=" + hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(want, self.headers.get("X-Edge-Signature", "")) or \
                        abs(time.time() - int(ts)) > 300 or len(body) > 512 * 1024:
                    stub.errors.append("bad signature/timestamp/size")
                    self.send_response(401)
                    self.end_headers()
                    return
                msgs = json.loads(body)["messages"]
                res = []
                for m in msgs:
                    stub.check(m)
                    last = stub.last.get(m["channel"], 0)
                    st = "ok" if m["seq"] > last else "duplicate" if m["seq"] == last else "stale"
                    if st == "ok":
                        stub.last[m["channel"]] = m["seq"]
                        stub.msgs.append(m)
                    res.append({"channel": m["channel"], "status": st, "lastSeq": stub.last.get(m["channel"], 0)})
                out = json.dumps({"results": res}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def check(self, m):
        ch = m["channel"]
        ptr = os.path.join(self.root, N, "ptr.json") if ch.startswith("net:") else \
            os.path.join(self.root, N, "d", ch.split(":")[2], "ptr.json")
        if m["head"] is not None and not os.path.exists(os.path.join(self.root, N, "o", m["head"] + ".json")):
            self.errors.append(f"{ch} seq {m['seq']}: head object missing at notify time")
        try:
            p = json.load(open(ptr))
            if p["seq"] < m["seq"]:
                self.errors.append(f"{ch}: pointer seq {p['seq']} behind message {m['seq']}")
        except FileNotFoundError:
            self.errors.append(f"{ch}: pointer missing at notify time")

    def take(self):
        m, self.msgs = self.msgs, []
        return m


def ts(days=0, hours=0):
    return datetime(2026, 10, 13, 12, tzinfo=timezone.utc) + timedelta(days=days, hours=hours)


def seed(db):
    import docstore as gf
    db.collection("contracts").document("Localhost").set({
        "wrapper": "0x60E813F889c4c29eB4FB8614fa37fFA7587e305B", "wrapper_trustless": "0xd3aDda8626C18ec4E85985cc6B489d96D836C07f",
        "rpc": "http://127.0.0.1:8546", "lastSyncedBlock": 10, "projectsLastSyncedBlock": 10})
    for i, dao in enumerate([DAO1, DAO2]):
        d = db.collection(C).document(dao)
        d.set({"underlyingToken": "None", "name": f"E2E DAO {i}", "creationDate": ts(i), "description": "seeded",
               "token": "0x" + "1" * 40, "treasuryAddress": "0x" + "2" * 40, "registryAddress": "0x" + "3" * 40,
               "address": dao, "holders": 3, "symbol": f"E{i}", "decimals": 18, "proposals": ["1", "2"],
               "proposalThreshold": "0", "registry": {"description": "x"}, "treasury": {}, "votingDelay": 1,
               "totalSupply": "3000000000000000000", "votingDuration": 5, "executionDelay": 1, "quorum": 4,
               "nonTransferrable": True, "underlying": "None", "economy": "0x" + "4" * 40 if i else None,
               "supermajorityBps": 6667})
        for j, pid in enumerate(["1", "2" + "0" * 70]):
            d.collection("proposals").document(pid).set({
                "hash": "0x" + "ab" * 32, "type": "transfer", "title": f"P{j}", "description": "d",
                "author": VOTERS[0], "createdAt": ts(i, j), "latestStage": "Queued" if j else "pending",
                "statusHistory": {"pending": ts(i, j)}, "callDatas": [b"\xde\xad\xbe\xef", "0x"],
                "inFavor": "0", "against": "0", "votesFor": 0, "votesAgainst": 0, "votesCast": 0,
                "optionVotes": {}})
        for k, v in enumerate(VOTERS):
            d.collection("members").document(v).set({
                "address": v, "delegate": v, "personalBalance": str((k + 1) * 10 ** 18),
                "votingWeight": str((k + 1) * 10 ** 18), "constituents": [], "proposalsVoted": [],
                "proposalsCreated": ["1"] if k == 0 else [], "balanceSort": float((k + 1) * 10 ** 18),
                "lastSeen": gf.SERVER_TIMESTAMP})
        d.collection("activity").document("0x" + "aa" * 32 + "_0").set({
            "type": "proposal_created", "summary": "P0", "actor": VOTERS[0], "time": ts(i),
            "blockNumber": 5, "txHash": "0x" + "aa" * 32, "targetKind": "proposal", "targetId": "1", "targetLabel": "P0"})
    pr = db.collection(C).document(DAO2).collection("projects").document(PROJECT)
    pr.set({"id": PROJECT, "name": "Proj", "description": "", "repo": None, "termsHash": "", "author": VOTERS[0].lower(),
            "contractor": "0x" + "0" * 40, "arbiter": "0x" + "0" * 40, "tokenAddress": "0x" + "0" * 40, "stage": "open",
            "daoAddress": DAO2, "economyAddress": "0x" + "4" * 40, "createdAt": ts(2), "amount": "0"})
    for b in BACKERS:
        pr.collection("backers").document(b).set({"address": b, "contribution": "100", "immediateBps": 0,
                                                  "vote": "none", "lastUpdated": ts(2)})
    pr.collection("activity").document("0x" + "bb" * 32 + "_1").set({
        "sender": BACKERS[0], "functionName": "sendFunds", "contractAddress": PROJECT, "projectId": PROJECT,
        "params": {"amount": "100"}, "time": ts(2), "blockNumber": 6})


def vote(db, dao, pid, voter):
    """Shaped like homebase/paper.py update_proposal_votes: one transaction."""
    import docstore as gf
    d = db.collection(C).document(dao)
    t = db.transaction()

    @gf.transactional
    def run(tx):
        p = d.collection("proposals").document(pid)
        tx.set(p.collection("votes").document(voter), {"voter": voter, "weight": "1000000000000000000", "option": 1,
                                                       "cast": datetime.now(timezone.utc).isoformat(), "reason": "", "hash": "0x" + "cd" * 32})
        tx.update(p, {"votesFor": gf.Increment(1), "votesCast": gf.Increment(1), "inFavor": "1000000000000000000"})
        tx.update(d.collection("members").document(voter), {"proposalsVoted": gf.ArrayUnion([pid])})
    run(t)


class Pub:
    def __init__(self, root, env, log_path):
        self.log_path = log_path
        self.f = open(log_path, "w")
        self.p = subprocess.Popen([sys.executable, "-m", "publisher", "--network", N, "--networks-index",
                                   "--target", f"dir:{root}"], env=env, stdout=self.f, stderr=subprocess.STDOUT)

    def wait_line(self, needle, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            txt = open(self.log_path).read()
            if needle in txt:
                return txt
            if self.p.poll() is not None:
                raise RuntimeError("publisher exited:\n" + txt)
            time.sleep(0.2)
        raise TimeoutError(needle + "\n" + open(self.log_path).read())

    def stop(self):
        self.p.terminate()
        self.p.wait(10)
        self.f.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True, help="a new store file for this run")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--stub-port", type=int, default=8796)
    ap.add_argument("--skip-outage", action="store_true")
    a = ap.parse_args()
    store = os.path.abspath(a.store)
    if store == os.path.abspath(os.path.join("data", "werule.sqlite")) or os.path.exists(store):
        sys.exit("--store must be a new file, not the main local store")
    import docstore
    db = docstore.Client(store)
    root = os.path.abspath(a.dir)
    os.makedirs(root, exist_ok=True)
    assert not os.listdir(root), "--dir must be empty"
    seed(db)
    stub = Stub(a.stub_port, root)
    env = dict(os.environ, DOCSTORE_PATH=store, NOTIFY_URL=f"http://127.0.0.1:{a.stub_port}/notify", NOTIFY_SECRET=SECRET,
               PUBLISHER_LOG_LEVEL="INFO", PYTHONUNBUFFERED="1")
    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond)))
        print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""), flush=True)

    logs = os.path.join(root, "..", "pub-logs")
    os.makedirs(logs, exist_ok=True)

    # 1. first start publishes everything
    p = Pub(root, env, os.path.join(logs, "run1.log"))
    p.wait_line("startup:")
    time.sleep(2)
    first = stub.take()
    chans = {m["channel"] for m in first}
    check("first start notifies net + both DAO channels",
          chans == {f"net:{N}", f"dao:{N}:{DAO1.lower()}", f"dao:{N}:{DAO2.lower()}"}, chans)
    nets = json.load(open(os.path.join(root, "networks.json")))
    check("networks.json drops checkpoint fields",
          "lastSyncedBlock" not in nets["networks"][0]["data"] and "projectsLastSyncedBlock" not in nets["networks"][0]["data"])
    ptr2 = json.load(open(os.path.join(root, N, "d", DAO2.lower(), "ptr.json")))
    head2 = json.load(open(os.path.join(root, N, "o", ptr2["head"] + ".json")))
    check("DAO head has project nested lists",
          set(head2["nested"]) == {f"projects/{PROJECT}/backers", f"projects/{PROJECT}/activity"}, head2["nested"].keys())
    check("pointer seq == last message seq of its flush",
          ptr2["seq"] == max(m["seq"] for m in first if m["channel"] == f"dao:{N}:{DAO2.lower()}"))
    p.stop()

    # 2. restart twice, no changes: zero PUTs, zero notifies on the second start
    for run in (2, 3):
        snap = {os.path.join(dp, f): os.path.getmtime(os.path.join(dp, f)) for dp, _, fs in os.walk(root) for f in fs}
        p = Pub(root, env, os.path.join(logs, f"run{run}.log"))
        txt = p.wait_line("startup:")
        time.sleep(2)
        after = {os.path.join(dp, f): os.path.getmtime(os.path.join(dp, f)) for dp, _, fs in os.walk(root) for f in fs}
        msgs = stub.take()
        check(f"restart {run - 1}: zero PUTs, zero notifies",
              snap == after and not msgs and "0 changed, 0 PUTs" in txt, f"{len(msgs)} msgs; {txt[-300:]}")
        if run == 3:
            break
        p.stop()

    # 3. one vote: one changes message with vote, proposal and member, after its objects exist
    vote(db, DAO1, "1", VOTERS[1])
    time.sleep(5)
    msgs = stub.take()
    dao1 = [m for m in msgs if m["channel"] == f"dao:{N}:{DAO1.lower()}"]
    paths = {e["path"] for m in dao1 for e in m.get("changes", [])}
    want = {f"{C}/{DAO1}/proposals/1/votes/{VOTERS[1]}", f"{C}/{DAO1}/proposals/1", f"{C}/{DAO1}/members/{VOTERS[1]}"}
    check("one vote: one changes message on the DAO channel", len(dao1) == 1 and dao1[0]["kind"] == "changes", msgs)
    check("vote message has vote, proposal, member", paths == want, paths)
    check("no net message for a vote", not [m for m in msgs if m["channel"].startswith("net:")])
    ptr1 = json.load(open(os.path.join(root, N, "d", DAO1.lower(), "ptr.json")))
    check("pointer seq equals message seq", dao1 and ptr1["seq"] == dao1[-1]["seq"])

    # 4. backer deleted, DAO doc field deleted: delete entry and net push
    import docstore as gf
    db.collection(C).document(DAO2).collection("projects").document(PROJECT).collection("backers").document(BACKERS[1]).delete()
    db.collection(C).document(DAO2).update({"economy": gf.DELETE_FIELD})
    time.sleep(5)
    msgs = stub.take()
    d2 = [e for m in msgs if m["channel"] == f"dao:{N}:{DAO2.lower()}" for e in m["changes"]]
    check("backer delete pushed as a delete entry",
          any(e.get("deleted") and e["id"] == BACKERS[1] for e in d2), d2)
    net = [e for m in msgs if m["channel"] == f"net:{N}" for e in m["changes"]]
    check("DAO doc field delete pushed on net channel", len(net) == 1 and "economy" not in net[0]["data"], net)

    # 5. worker outage: files keep updating; afterwards one resync per affected channel
    if not a.skip_outage:
        stub.up = False
        before = json.load(open(os.path.join(root, N, "d", DAO1.lower(), "ptr.json")))
        vote(db, DAO1, "1", VOTERS[2])
        db.collection(C).document(DAO1).update({"quorum": 5})
        time.sleep(5)
        mid = json.load(open(os.path.join(root, N, "d", DAO1.lower(), "ptr.json")))
        check("worker down: pointer still advances", mid["head"] != before["head"] and mid["seq"] > before["seq"])
        db.collection(C).document(DAO1).collection("activity").document("0x" + "ee" * 32 + "_0").set(
            {"type": "x", "summary": "y", "actor": VOTERS[0], "time": ts(5), "blockNumber": 9,
             "txHash": "0x" + "ee" * 32, "targetKind": "proposal", "targetId": "1", "targetLabel": "P0"})
        time.sleep(66)
        stub.up = True
        time.sleep(70)  # the sender's backoff is at most 60 s
        msgs = stub.take()
        kinds = sorted((m["channel"], m["kind"]) for m in msgs)
        check("after outage: exactly one resync per affected channel",
              kinds == sorted([(f"dao:{N}:{DAO1.lower()}", "resync"), (f"net:{N}", "resync")]), kinds)
        last = json.load(open(os.path.join(root, N, "d", DAO1.lower(), "ptr.json")))
        check("resync carries the pointer's seq and head",
              any(m["seq"] == last["seq"] and m["head"] == last["head"] for m in msgs))
    p.stop()

    check("stub saw no ordering or signature errors", not stub.errors, stub.errors)
    stub.srv.shutdown()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
