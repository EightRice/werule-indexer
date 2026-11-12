import json

from publisher.build import Doc, build_list
from publisher.encoding import canonical
from publisher.layout import NET_DAOS, TOP_LISTS, NESTED, PAGE_MAX, classify


def mk(i, k, data=None, pid=None):
    did = pid or str(i)
    entry = {"path": f"c/0x{'a' * 40}/proposals/{did}", "id": did, "v": 100 + i, "data": data or {}}
    return Doc(entry["path"], did, entry["v"], k, entry, canonical(entry))


def page(lb, i):
    return json.loads(lb.objects[lb.index["pages"][i]["h"]])


def test_rank_pages_ascending_and_P():
    docs = [mk(i, 1000 - i) for i in range(120)]  # k descending with i
    lb = build_list(TOP_LISTS["proposals"], docs)
    ix = lb.index
    assert ix["count"] == 120 and ix["hw"] == 219
    assert [p["n"] for p in ix["pages"]] == [50, 50, 20]
    assert ix["order"] == {"key": "createdAt", "dir": "desc"}
    first = page(lb, 0)["docs"]
    assert [d["id"] for d in first[:2]] == ["119", "118"]  # oldest first
    assert ix["pages"][0]["lo"] == [881, "119"] and ix["pages"][-1]["hi"] == [1000, "0"]


def test_ties_by_lowercase_id():
    docs = [mk(0, 5, pid="B"), mk(1, 5, pid="a"), mk(2, 5, pid="C")]
    lb = build_list(TOP_LISTS["activity"], docs)
    assert [d["id"] for d in page(lb, 0)["docs"]] == ["a", "B", "C"]


def test_size_split_and_big_doc_alone():
    big = "x" * (PAGE_MAX // 3)
    docs = [mk(i, i, {"t": big}) for i in range(4)] + [mk(9, 9, {"t": "y" * (PAGE_MAX + 10)})]
    lb = build_list(TOP_LISTS["proposals"], docs)
    assert [p["n"] for p in lb.index["pages"]] == [2, 2, 1]
    for p in lb.index["pages"][:2]:
        assert len(lb.objects[p["h"]]) <= PAGE_MAX


def test_deterministic_and_reuse():
    docs = [mk(i, i) for i in range(60)]
    a = build_list(TOP_LISTS["proposals"], docs)
    b = build_list(TOP_LISTS["proposals"], list(reversed(docs)))
    assert a.index == b.index
    c = build_list(TOP_LISTS["proposals"], docs + [mk(60, 60)])
    assert a.index["pages"][0]["h"] == c.index["pages"][0]["h"]   # only the newest page changes
    assert a.index["pages"][1]["h"] != c.index["pages"][1]["h"]


def member(addr, bal, extra=0):
    entry = {"path": f"c/0x{'a' * 40}/members/{addr}", "id": addr, "v": 1,
             "data": {"address": addr, "personalBalance": str(bal), "pad": "z" * extra}}
    return Doc(entry["path"], addr, 1, 0, entry, canonical(entry))


def addr(i):
    return "0x" + format(i * 2654435761 % (16 ** 40), "040x")


def test_members_prefix_levels():
    spec = TOP_LISTS["members"]
    small = build_list(spec, [member(addr(i), i) for i in range(400)])
    assert [p["p"] for p in small.index["pages"]] == [""] and "lo" not in small.index["pages"][0]
    mid = build_list(spec, [member(addr(i), i) for i in range(401)])
    ps = [p["p"] for p in mid.index["pages"]]
    assert ps == sorted(ps) and all(len(p) == 1 for p in ps)
    for p in mid.index["pages"]:
        assert all(d["id"].lower()[2:3] == p["p"] for d in json.loads(mid.objects[p["h"]])["docs"])
    big = build_list(spec, [member(addr(i), i) for i in range(6401)])
    assert all(len(p["p"]) == 2 for p in big.index["pages"])


def test_members_bucket_split_by_size():
    spec = TOP_LISTS["members"]
    docs = [member(addr(i), 1, extra=2000) for i in range(300)]  # ~600 KB in one bucket
    lb = build_list(spec, docs)
    assert len(lb.index["pages"]) >= 3
    assert all(p["p"] == "" and "lo" in p and "hi" in p for p in lb.index["pages"])


def test_members_top_view():
    spec = TOP_LISTS["members"]
    docs = [member(addr(1), 2 ** 70), member(addr(2), 0), member(addr(3), 5), member(addr(4), 5)]
    docs[0].entry["data"].pop("address")
    lb = build_list(spec, docs)
    top = json.loads(lb.objects[lb.index["views"]["top"]])["items"]
    assert [t["id"] for t in top] == [addr(1)] + sorted([addr(3), addr(4)])
    assert top[0]["address"] == addr(1) and top[0]["personalBalance"] == str(2 ** 70)


def test_queued_and_loc_views():
    docs = [mk(i, i, {"latestStage": "Queued" if i % 2 else "pending"}) for i in range(55)]
    lb = build_list(TOP_LISTS["proposals"], docs)
    q = json.loads(lb.objects[lb.index["views"]["queued"]])
    assert q["coll"] == "proposals" and len(q["docs"]) == 27
    loc = json.loads(lb.objects[lb.index["views"]["loc"]])["loc"]
    assert loc["0"] == 0 and loc["54"] == 1


def test_search_view():
    e = {"path": "c/0xA", "id": "0xA", "v": 1, "data": {"name": "N", "symbol": "S", "economy": "0x" + "1" * 40}}
    d = Doc(e["path"], "0xA", 1, 5, e, canonical(e))
    lb = build_list(NET_DAOS, [d])
    s = json.loads(lb.objects[lb.index["views"]["search"]])
    assert s == {"items": [{"id": "0xA", "name": "N", "symbol": "S", "economy": True, "k": 5}]}


def test_classify():
    C = "idaosX"
    a = "0x" + "Ab" * 20
    assert classify(f"{C}/{a}", C)[0].list_key == "__doc__"
    assert classify(f"{C}/{a}/proposals/1/votes/{a}", C)[0].list_key == "proposals/1/votes"
    p = classify(f"{C}/{a}/projects/{a}/backers/x", C)[0]
    assert p.list_key == f"projects/{a.lower()}/backers" and p.spec is NESTED[("projects", "backers")]
    assert classify(f"{C}/{a}/projects/{a}/activity/t_1", C)[0].list_key.endswith("/activity")
    assert classify(f"{C}/{a}/debates/0123456789abcdef", C)[0].list_key == "debates"
    assert classify(f"{C}/{a}/debates/0123456789abcdef/arguments/0x1", C)[0].list_key == "debates/0123456789abcdef/arguments"
    assert classify(f"{C}/{a}/debates/0123456789abcdef/messages/0x1", C)[0].list_key == "debates/0123456789abcdef/messages"
    assert classify(f"{C}/{a}/elsewhere/1", C)[0] is None
    assert classify(f"idaosY/{a}", C)[0] is None
