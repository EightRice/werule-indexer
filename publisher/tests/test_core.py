import json

from publisher.core import GC_GRACE
from publisher.notify import Notifier
from publisher.tests.helpers import *  # noqa: F401,F403


def started(pub):
    pub.startup()
    return pub


def test_first_start_publishes_and_notifies():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    ptr = t.json(f"{N}/d/{D}/ptr.json")
    head = t.json(f"{N}/o/{ptr['head']}.json")
    assert head["scope"] == f"dao:{N}:{D}" and head["doc"]["id"] == DAO
    assert set(head["lists"]) == {"proposals", "members", "activity", "projects", "debates"}
    assert head["lists"]["activity"]["count"] == 0 and head["nested"] == {}
    dao_msgs = [m for m in nt.msgs if m["channel"] == f"dao:{N}:{D}"]
    assert len(dao_msgs) == 1 and ptr["seq"] == dao_msgs[-1]["seq"]
    assert {e["path"] for e in dao_msgs[0]["changes"]} == {
        f"{C}/{DAO}", f"{C}/{DAO}/proposals/0", f"{C}/{DAO}/members/{VOTER}"}
    net = [m for m in nt.msgs if m["channel"] == f"net:{N}"]
    assert len(net) == 1 and net[0]["counts"] == {"daos": 1}


def test_restart_no_changes_zero_puts_zero_notifies():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    for _ in range(2):
        pub2, _, nt2 = make_pub(target=t)
        seed_basic(pub2)
        before = len(t.log)
        assert pub2.startup() == 0
        assert len(t.log) == before and nt2.msgs == []


def test_one_vote_one_message():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    nt.msgs.clear()
    pub.mono.t += 10
    pub.apply(f"{C}/{DAO}/proposals/0/votes/{VOTER}", 2_000_001, vote_doc(VOTER, 10 ** 18), 2)
    p = proposal_doc(0)
    p.update(votesFor=1, votesCast=1, inFavor=str(10 ** 18))
    pub.apply(f"{C}/{DAO}/proposals/0", 2_000_002, p, 2)
    pub.apply(f"{C}/{DAO}/members/{VOTER}", 2_000_003, member_doc(VOTER, 10 ** 18, ["0"]), 2)
    assert pub.due(pub.mono.t + 0.5) == []
    assert pub.due(pub.mono.t + 1.0) == [D]
    assert pub.flush(D)
    assert len(nt.msgs) == 1
    m = nt.msgs[0]
    assert m["channel"] == f"dao:{N}:{D}" and m["kind"] == "changes"
    assert {e["id"] for e in m["changes"]} == {VOTER, "0"} and len(m["changes"]) == 3
    ptr = t.json(f"{N}/d/{D}/ptr.json")
    assert ptr["seq"] == m["seq"] and ptr["head"] == m["head"]
    head = t.json(f"{N}/o/{m['head']}.json")
    assert head["nested"]["proposals/0/votes"]["count"] == 1
    assert m["counts"] == {"proposals": 1, "members": 1, "activity": 0, "projects": 0, "debates": 0}
    puts = t.puts()
    assert puts[-1] == f"{N}/d/{D}/ptr.json"   # pointer after objects
    assert not pub.due(pub.mono.t + 100)       # net scope untouched by a vote


def test_debounce_max_wait():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    base = pub.mono.t
    for i in range(6):
        pub.mono.t = base + i * 0.5
        pub.apply(f"{C}/{DAO}/activity/0x{i:064x}_0", 3_000_000 + i, {"type": "x", "time": ts("2026-10-15T00:00:00")}, 3)
    assert pub.due(base + 2.9) == []
    assert pub.due(base + 3.0) == [D]


def test_noop_replay_and_coalesce():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    nt.msgs.clear()
    pub.apply(f"{C}/{DAO}/proposals/0", 1_000_002, proposal_doc(0), 9)   # same update_time: no-op
    assert not pub.scopes[D].pending
    pub.apply(f"{C}/{DAO}/proposals/0", 5_000_000, proposal_doc(0, "active"), 9)
    pub.apply(f"{C}/{DAO}/proposals/0", 5_000_001, proposal_doc(0, "Queued"), 9)
    pub.flush(D)
    (m,) = nt.msgs
    assert len(m["changes"]) == 1 and m["changes"][0]["v"] == 5_000_001


def test_runtime_delete_and_dao_doc_change_hits_both_channels():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    nt.msgs.clear()
    pub.apply(f"{C}/{DAO}/members/{VOTER}", None, None, 7_000_000)
    pub.apply(f"{C}/{DAO}", 7_000_001, dao_doc(2), 7)
    pub.flush(D)
    pub.flush("net")
    dao = [m for m in nt.msgs if m["channel"].startswith("dao:")][0]
    dels = [e for e in dao["changes"] if e.get("deleted")]
    assert dels == [{"path": f"{C}/{DAO}/members/{VOTER}", "id": VOTER, "v": 7_000_000, "deleted": True}]
    net = [m for m in nt.msgs if m["channel"].startswith("net:")][0]
    assert [e["path"] for e in net["changes"]] == [f"{C}/{DAO}"]


def test_restart_diff_sets_and_deletes():
    pub, t, nt = make_pub()
    seed_basic(pub)
    pub.apply(f"{C}/{DAO}/proposals/0/votes/{VOTER}", 1_000_010, vote_doc(VOTER, 1), 1)
    started(pub)
    # downtime: the vote and the member are deleted, a proposal is added
    pub2, _, nt2 = make_pub(target=t)
    pub2.apply(f"{C}/{DAO}", 1_000_001, dao_doc(), 1)
    pub2.apply(f"{C}/{DAO}/proposals/0", 1_000_002, proposal_doc(0), 1)
    pub2.apply(f"{C}/{DAO}/proposals/1", 2_000_000, proposal_doc(1, created="2026-10-20T00:00:00"), 1)
    pub2.startup()
    (m,) = [m for m in nt2.msgs if m["channel"].startswith("dao:")]
    sets = {e["path"] for e in m["changes"] if not e.get("deleted")}
    dels = {e["path"]: e["v"] for e in m["changes"] if e.get("deleted")}
    assert sets == {f"{C}/{DAO}/proposals/1"}
    assert dels == {f"{C}/{DAO}/members/{VOTER}": pub2.first_read["members"],
                    f"{C}/{DAO}/proposals/0/votes/{VOTER}": pub2.first_read["votes"]}
    old_seq = json.loads(t.files[f"{N}/d/{D}/ptr.json"])["seq"]
    assert m["seq"] == old_seq
    assert not [x for x in nt2.msgs if x["channel"].startswith("net:")]  # DAO doc unchanged


def test_restart_scope_gone_resync_null_once():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    pub2, _, nt2 = make_pub(target=t)
    pub2.startup()
    (m,) = [m for m in nt2.msgs if m["channel"].startswith("dao:")]
    assert m["kind"] == "resync" and m["head"] is None and "counts" not in m
    assert t.json(f"{N}/d/{D}/ptr.json")["head"] is None
    pub3, _, nt3 = make_pub(target=t)
    before = len(t.log)
    pub3.startup()
    assert nt3.msgs == [] and len(t.log) == before


def test_restart_unreadable_old_page_resyncs():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    head = t.json(f"{N}/o/{t.json(f'{N}/d/{D}/ptr.json')['head']}.json")
    for p in head["lists"]["members"]["pages"]:
        del t.files[f"{N}/o/{p['h']}.json"]
    pub2, _, nt2 = make_pub(target=t)
    pub2.apply(f"{C}/{DAO}", 1_000_001, dao_doc(), 1)
    pub2.apply(f"{C}/{DAO}/proposals/0", 1_000_002, proposal_doc(0), 1)
    pub2.startup()
    (m,) = [m for m in nt2.msgs if m["channel"].startswith("dao:")]
    assert m["kind"] == "resync" and m["counts"]["members"] == 0


def test_case_variants_publish_checksummed_only():
    pub, t, nt = make_pub()
    seed_basic(pub)
    pub.apply(f"{C}/{D}", 1_000_100, dao_doc(), 1)  # lowercase duplicate of the DAO doc
    started(pub)
    head = t.json(f"{N}/o/{t.json(f'{N}/d/{D}/ptr.json')['head']}.json")
    assert head["doc"]["path"] == f"{C}/{DAO}"
    net = t.json(f"{N}/o/{t.json(f'{N}/ptr.json')['head']}.json")
    assert net["lists"]["daos"]["count"] == 1


def test_gc_keeps_superseded_for_two_hours():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    old_head = t.json(f"{N}/d/{D}/ptr.json")["head"]
    old_page = t.json(f"{N}/o/{old_head}.json")["lists"]["proposals"]["pages"][0]["h"]
    pub.clock.t += 30 * 86400          # the page was uploaded a month ago
    assert pub.gc() == 0
    pub.apply(f"{C}/{DAO}/proposals/0", 9_000_000, proposal_doc(0, "Executed"), 9)
    pub.flush(D)                        # supersedes old_page and old_head now
    assert f"{N}/o/{old_page}.json" in t.files
    pub.clock.t += GC_GRACE - 60
    assert pub.gc() == 0 and f"{N}/o/{old_page}.json" in t.files
    pub.clock.t += 120
    assert pub.gc() >= 2
    assert f"{N}/o/{old_page}.json" not in t.files and f"{N}/o/{old_head}.json" not in t.files
    cur = t.json(f"{N}/d/{D}/ptr.json")["head"]
    assert f"{N}/o/{cur}.json" in t.files


def test_gc_startup_orphans_use_startup_time():
    pub, t, nt = make_pub()
    seed_basic(pub)
    t.files[f"{N}/o/{'f' * 32}.json"] = b"{}"
    started(pub)
    pub.clock.t += GC_GRACE - 1
    assert pub.gc() == 0
    pub.clock.t += 2
    assert pub.gc() == 1 and f"{N}/o/{'f' * 32}.json" not in t.files


def test_gc_reupload_after_delete():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    head0 = t.json(f"{N}/d/{D}/ptr.json")["head"]
    pub.apply(f"{C}/{DAO}/proposals/0", 9_000_000, proposal_doc(0, "Executed"), 9)
    pub.flush(D)
    pub.clock.t += GC_GRACE + 1
    pub.gc()
    assert f"{N}/o/{head0}.json" not in t.files
    pub.apply(f"{C}/{DAO}/proposals/0", 1_000_002, proposal_doc(0), 10)  # back to the original content
    pub.flush(D)
    assert t.json(f"{N}/d/{D}/ptr.json")["head"] == head0
    assert f"{N}/o/{head0}.json" in t.files


def test_networks_json_drops_checkpoints():
    pub, t, nt = make_pub(networks_index=True)
    pub.apply_contract("Localhost", {"wrapper": "0x1", "lastSyncedBlock": 5, "projectsLastSyncedBlock": 7})
    started(pub)
    assert t.json("networks.json") == {"format": 1, "networks": [{"id": "Localhost", "data": {"wrapper": "0x1"}}]}
    n = len(t.log)
    pub.apply_contract("Localhost", {"wrapper": "0x1", "lastSyncedBlock": 6, "projectsLastSyncedBlock": 8})
    pub.flush_networks()
    assert len(t.log) == n


class FakeWorker:
    def __init__(self):
        self.up = False
        self.last = {}
        self.posts = []

    def __call__(self, url, body, headers):
        if not self.up:
            raise ConnectionError("down")
        msgs = json.loads(body)["messages"]
        self.posts.append(msgs)
        res = []
        for m in msgs:
            last = self.last.get(m["channel"], 0)
            st = "ok" if m["seq"] > last else "duplicate" if m["seq"] == last else "stale"
            if st == "ok":
                self.last[m["channel"]] = m["seq"]
            res.append({"channel": m["channel"], "status": st, "lastSeq": self.last.get(m["channel"], 0)})
        return 200, json.dumps({"results": res}).encode()


def test_worker_outage_collapses_to_one_resync_per_channel():
    t = FakeTarget()
    clock = Clock()
    w = FakeWorker()
    nf = Notifier("http://x/notify", "s", post=w, clock=clock)
    from publisher.core import Publisher
    pub = Publisher(N, C, t, notifier=nf, clock=clock, mono=Clock(0.0))
    pub.first_read = {"daos": 1}
    seed_basic(pub)
    pub.startup()
    for i in range(5):
        clock.t += 2
        pub.apply(f"{C}/{DAO}/proposals/0", 2_000_000 + i, proposal_doc(0, f"s{i}"), 2)
        pub.flush(D)
        assert nf.step() is False                      # worker down: files still updated
        assert t.json(f"{N}/d/{D}/ptr.json")["seq"] == nf.queues[f"dao:{N}:{D}"][-1].seq
    clock.t += 61
    w.up = True
    while nf.pending():
        nf.step()
    kinds = [(m["channel"], m["kind"]) for post in w.posts for m in post]
    assert sorted(kinds) == sorted([(f"dao:{N}:{D}", "resync"), (f"net:{N}", "resync")])
    assert w.last[f"dao:{N}:{D}"] == t.json(f"{N}/d/{D}/ptr.json")["seq"]


def test_stale_reply_rewrites_pointer_and_resyncs():
    t = FakeTarget()
    clock = Clock()
    w = FakeWorker()
    w.up = True
    nf = Notifier("http://x/notify", "s", post=w, clock=clock)
    from publisher.core import Publisher
    pub = Publisher(N, C, t, notifier=nf, clock=clock, mono=Clock(0.0))
    pub.first_read = {"daos": 1}
    seed_basic(pub)
    w.last[f"dao:{N}:{D}"] = int(clock.t * 1000) + 10 ** 6   # someone else is ahead
    pub.startup()
    nf.step()
    q = nf.queues[f"dao:{N}:{D}"]
    assert len(q) == 1 and q[0].obj["kind"] == "resync"
    assert q[0].seq == w.last[f"dao:{N}:{D}"] + 1 == t.json(f"{N}/d/{D}/ptr.json")["seq"]
    nf.step()
    assert not nf.queues[f"dao:{N}:{D}"]


def test_large_flush_becomes_resync():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    nt.msgs.clear()
    for i in range(3000):
        a = "0x" + format(i + 1, "040x")
        pub.apply(f"{C}/{DAO}/members/{a}", 4_000_000 + i, member_doc(a, i + 1), 4)
    pub.flush(D)
    (m,) = nt.msgs
    assert m["kind"] == "resync" and m["counts"]["members"] == 3001


def test_message_split_under_256k():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    nt.msgs.clear()
    for i in range(6):
        p = proposal_doc(i + 1)
        p["description"] = "x" * 100_000
        pub.apply(f"{C}/{DAO}/proposals/{i + 1}", 4_000_000 + i, p, 4)
    pub.flush(D)
    assert len(nt.msgs) == 3
    seqs = [m["seq"] for m in nt.msgs]
    assert seqs == list(range(seqs[0], seqs[0] + 3))
    assert all(m["head"] == nt.msgs[-1]["head"] for m in nt.msgs)
    from publisher.encoding import canonical
    assert all(len(canonical(m)) <= 256 * 1024 for m in nt.msgs)
    assert t.json(f"{N}/d/{D}/ptr.json")["seq"] == seqs[-1]


def test_nested_ref_when_head_too_big_and_restart_diff_reads_it():
    pub, t, nt = make_pub()
    seed_basic(pub)
    for i in range(3500):
        pub.apply(f"{C}/{DAO}/proposals/0/votes/0x{i:040x}", 1_100_000 + i, vote_doc(f"0x{i:040x}", 1), 1)
        pub.apply(f"{C}/{DAO}/projects/0x{i:040x}/backers/0x{1:040x}", 1_200_000 + i, {"contribution": "5"}, 1)
    started(pub)
    head = t.json(f"{N}/o/{t.json(f'{N}/d/{D}/ptr.json')['head']}.json")
    assert "nested" not in head and "nestedRef" in head
    nested = t.json(f"{N}/o/{head['nestedRef']}.json")["nested"]
    assert nested["proposals/0/votes"]["count"] == 3500 and len(nested) == 3501
    pub2, _, nt2 = make_pub(target=t)
    seed_basic(pub2)
    for i in range(3500):
        pub2.apply(f"{C}/{DAO}/proposals/0/votes/0x{i:040x}", 1_100_000 + i, vote_doc(f"0x{i:040x}", 1), 1)
        if i:
            pub2.apply(f"{C}/{DAO}/projects/0x{i:040x}/backers/0x{1:040x}", 1_200_000 + i, {"contribution": "5"}, 1)
    pub2.startup()
    (m,) = [m for m in nt2.msgs if m["channel"].startswith("dao:")]
    assert [e["path"] for e in m["changes"]] == [f"{C}/{DAO}/projects/0x{0:040x}/backers/0x{1:040x}"]
    assert m["changes"][0]["deleted"]


def test_big_doc_is_stale_entry():
    pub, t, nt = make_pub()
    seed_basic(pub)
    started(pub)
    nt.msgs.clear()
    p = proposal_doc(0)
    p["callDatas"] = [b"\x00" * 120_000]
    pub.apply(f"{C}/{DAO}/proposals/0", 8_000_000, p, 8)
    pub.flush(D)
    (m,) = nt.msgs
    assert m["changes"] == [{"path": f"{C}/{DAO}/proposals/0", "id": "0", "v": 8_000_000, "stale": True}]
