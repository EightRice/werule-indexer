import time
from datetime import datetime, timezone

import pytest
from eth_account import Account

from apps.debates import eip712
from apps.debates.engine import ZERO, Debates, Reject, detached, spent, tally
from docstore import Client

C = "idaosLocalhost"
CHAIN = 31337
DAO = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
E18 = 10 ** 18
DID = "0123456789abcdef"
ALICE = Account.create("alice")
BOB = Account.create("bob")
CAROL = Account.create("carol")  # no voting power


# ---------- tally (pure) ----------

def arg(parent=ZERO, con=False, stakes=None, retired=False, author="0xa"):
    return {"parent": parent, "con": con, "stakes": stakes or {}, "retired": retired, "author": author}


def test_floor_and_reserve_example():
    # T has 10; con C has 50 (T floors at 0, C keeps a reserve of 40); D (con of C) 30 leaves T at 0
    w = {"t": 10, "c": 50, "d": 30}
    args = {"C": arg(con=True, stakes={"c": 10000}), "D": arg(parent="C", con=True, stakes={"d": 10000})}
    scores, support, opposition, _ = tally(args, {"t": 10000}, w)
    assert scores["C"] == (50, 20) and scores["D"] == (30, 30)
    assert (support, opposition) == (10, 20)
    # D grows past C's reserve: C falls below T's 10 and T's support is no longer cancelled
    w["d"] = 45
    scores, support, opposition, _ = tally(args, {"t": 10000}, w)
    assert scores["C"] == (50, 5) and (support, opposition) == (10, 5)


def test_refuted_child_never_flips_its_parent():
    w = {"p": 10, "x": 100, "q": 7}
    args = {"P": arg(stakes={"p": 10000}),                         # pro of the thesis, 10
            "X": arg(parent="P", con=True, stakes={"x": 10000}),   # con of P, 100: P floors at 0
            "Q": arg(con=True, stakes={"q": 10000})}               # con of the thesis, 7
    scores, support, opposition, _ = tally(args, {}, w)
    assert scores["P"] == (10, 0)
    assert (support, opposition) == (0, 7)  # P adds nothing, never subtracts


def test_shares_scale_with_current_power():
    args = {"A": arg(stakes={"m": 2500})}
    assert tally(args, {"m": 5000}, {"m": 100})[0]["A"] == (25, 25)
    assert tally(args, {"m": 5000}, {"m": 200})[1] == 100 + 50  # power doubled, same shares


def test_retired_subtree_detaches_and_frees_budget():
    args = {"A": arg(stakes={"m": 4000}, retired=True, author="m"), "B": arg(parent="A", stakes={"m": 3000}, author="m"),
            "C": arg(stakes={"m": 2000}, author="m")}
    assert detached(args) == {"A", "B"}
    assert spent(args, {"m": 1000}, "M") == 3000
    scores, support, _, participants = tally(args, {}, {"m": 100})
    assert set(scores) == {"C"} and support == 20 and participants == 1


# ---------- engine over the store ----------

@pytest.fixture
def world(tmp_path):
    db = Client(str(tmp_path / "s.sqlite"))
    dao = db.collection(C).document(DAO)
    dao.set({"name": "D", "proposalThreshold": str(E18)})
    dao.collection("members").document(ALICE.address).set({"votingWeight": str(100 * E18)})
    dao.collection("members").document(BOB.address).set({"votingWeight": str(50 * E18)})
    dao.collection("members").document(CAROL.address).set({"votingWeight": "0"})
    eng = Debates(db, C, CHAIN, log=lambda *a: None)
    yield db, dao, eng
    db.close()


def open_debate(db, dao, value="open", tx="0x" + "11" * 32, pid="42", title="Title", thesis="Thesis"):
    dao.collection("proposals").document(pid).set({"title": title, "description": thesis, "externalResource": "",
                                                   "type": "debate"})
    when = datetime(2026, 11, 12, tzinfo=timezone.utc)
    dao.collection("activity").document(f"{tx}_9").set({"type": "proposalExecuted", "txHash": tx, "targetId": pid,
                                                        "time": when})
    db.collection("debate_registry").document(f"{C}|{tx}|3").set({
        "network": C, "dao": DAO, "debate": DID, "value": value, "txHash": tx, "blockNumber": 5, "logIndex": 3,
        "seenAt": time.time(), "done": False})


def msg(acct, type_, nonce, **f):
    m = {"debate": DID, "nonce": str(nonce), **f}
    return {"type": type_, "dao": DAO, "message": m, "signature": eip712.sign(type_, m, CHAIN, DAO, acct.key),
            "author": acct.address}


def test_open_post_stake_retire(world):
    db, dao, eng = world
    open_debate(db, dao)
    eng.resolve_registry()
    d = dao.collection("debates").document(DID).get().to_dict()
    assert d["status"] == "open" and d["title"] == "Title" and d["thesis"] == "Thesis" and d["proposalId"] == "42"
    assert any(a.to_dict()["type"] == "debateOpened" for a in dao.collection("activity").stream())

    a1 = msg(ALICE, "Argument", 1, parent=ZERO, con=False, bps=6000, text="Yes, because")
    assert eng.apply(a1) == "ok"
    a1id = eip712.digest("Argument", a1["message"], CHAIN, DAO)
    b1 = msg(BOB, "Argument", 1, parent=a1id, con=True, bps=10000, text="No")
    assert eng.apply(b1) == "ok"
    d = dao.collection("debates").document(DID).get().to_dict()
    # A1: 60 own minus 50 from B1's con = 10
    assert d["support"] == str(10 * E18) and d["opposition"] == "0" and d["participants"] == 2
    assert dao.collection("debates").document(DID).collection("arguments").document(a1id).get().to_dict()["score"] == str(10 * E18)

    # Alice's budget: 60% used, 40% left
    with pytest.raises(Reject, match="100%"):
        eng.apply(msg(ALICE, "Stake", 2, argument=ZERO, bps=5000))
    assert eng.apply(msg(ALICE, "Stake", 3, argument=ZERO, bps=4000)) == "ok"
    assert dao.collection("debates").document(DID).get().to_dict()["support"] == str(50 * E18)

    # Bob retires his con: A1 is back to 60
    b1id = eip712.digest("Argument", b1["message"], CHAIN, DAO)
    with pytest.raises(Reject, match="only the author"):
        eng.apply(msg(ALICE, "Retire", 4, argument=b1id))
    assert eng.apply(msg(BOB, "Retire", 2, argument=b1id)) == "ok"
    assert dao.collection("debates").document(DID).get().to_dict()["support"] == str(100 * E18)
    assert len(list(dao.collection("debates").document(DID).collection("messages").stream())) == 4


def test_rejections(world):
    db, dao, eng = world
    with pytest.raises(Reject, match="not open"):
        eng.apply(msg(ALICE, "Argument", 1, parent=ZERO, con=False, bps=0, text="early"))
    open_debate(db, dao)
    eng.resolve_registry()
    with pytest.raises(Reject, match="proposal threshold"):
        eng.apply(msg(CAROL, "Argument", 1, parent=ZERO, con=False, bps=0, text="no power"))
    ok = msg(ALICE, "Argument", 5, parent=ZERO, con=False, bps=0, text="x")
    assert eng.apply(ok) == "ok"
    assert eng.apply(ok) == "duplicate"
    with pytest.raises(Reject, match="nonce"):
        eng.apply(msg(ALICE, "Argument", 5, parent=ZERO, con=True, bps=0, text="replayed nonce"))
    with pytest.raises(Reject, match="parent"):
        eng.apply(msg(ALICE, "Argument", 6, parent="0x" + "ab" * 32, con=False, bps=0, text="orphan"))
    with pytest.raises(Reject, match="4096"):
        eng.apply(msg(ALICE, "Argument", 7, parent=ZERO, con=False, bps=0, text="x" * 4097))
    bad = msg(ALICE, "Argument", 8, parent=ZERO, con=False, bps=0, text="forged")
    bad["message"]["text"] = "changed after signing"
    with pytest.raises(Reject, match="signer differs"):
        eng.apply(bad)
    other_chain = msg(ALICE, "Stake", 9, argument=ZERO, bps=1)
    other_chain["signature"] = eip712.sign("Stake", other_chain["message"], 1, DAO, ALICE.key)
    with pytest.raises(Reject):
        eng.apply(other_chain)


def test_retire_debate_and_reopen(world):
    db, dao, eng = world
    open_debate(db, dao)
    eng.resolve_registry()
    eng.apply(msg(ALICE, "Argument", 1, parent=ZERO, con=False, bps=1000, text="kept"))
    open_debate(db, dao, value="", tx="0x" + "22" * 32, pid="43")
    eng.resolve_registry()
    d = dao.collection("debates").document(DID).get().to_dict()
    assert d["status"] == "retired" and d["retiredBy"] == "43"
    with pytest.raises(Reject, match="not open"):
        eng.apply(msg(ALICE, "Stake", 2, argument=ZERO, bps=1))
    open_debate(db, dao, tx="0x" + "33" * 32, pid="44")
    eng.resolve_registry()
    d = dao.collection("debates").document(DID).get().to_dict()
    assert d["status"] == "open" and d["arguments"] == 1  # reopened with its arguments


def test_registry_change_without_a_proposal_waits_then_gives_up(world):
    db, dao, eng = world
    db.collection("debate_registry").document(f"{C}|0xff|1").set({
        "network": C, "dao": DAO, "debate": DID, "value": "open", "txHash": "0xff", "blockNumber": 1,
        "logIndex": 1, "seenAt": time.time(), "done": False})
    eng.resolve_registry()
    assert not db.collection("debate_registry").document(f"{C}|0xff|1").get().to_dict()["done"]
    eng.clock = lambda: time.time() + 3600
    eng.resolve_registry()
    e = db.collection("debate_registry").document(f"{C}|0xff|1").get().to_dict()
    assert e["done"] and "no executed proposal" in e["outcome"]
    assert not dao.collection("debates").document(DID).get().exists


def test_queue_pull_moves_the_cursor(world):
    db, dao, eng = world
    open_debate(db, dao)
    eng.resolve_registry()

    class Q:
        def __init__(self, entries):
            self.entries, self.asked = entries, []

        def fetch(self, after):
            self.asked.append(after)
            return [e for e in self.entries if e["seq"] > after]

    good = dict(msg(ALICE, "Argument", 1, parent=ZERO, con=False, bps=100, text="queued"), seq=1)
    bad = dict(msg(CAROL, "Argument", 1, parent=ZERO, con=False, bps=0, text="rejected"), seq=2)
    eng.queue = Q([good, bad])
    assert eng.pull() == 2
    assert eng.pull() == 0 and eng.queue.asked == [0, 2]
    assert dao.collection("debates").document(DID).get().to_dict()["arguments"] == 1
