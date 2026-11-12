import threading
from datetime import datetime, timezone

import pytest

import docstore as firestore
from docstore import (AlreadyExists, Client, FieldFilter, FieldPath, NotFound, Store)


@pytest.fixture
def db(tmp_path):
    c = Client(str(tmp_path / "s.sqlite"))
    yield c
    c.close()


C = "idaosLocalhost"
DAO = "0x5FbDB2315678afecb367f032d93F642f64180aa3"


def test_set_get_and_types_round_trip(db):
    t = datetime(2026, 11, 12, 9, 30, 0, 123456, tzinfo=timezone.utc)
    ref = db.collection(C).document(DAO)
    ref.set({"name": "A", "big": 2 ** 70, "small": 7, "f": 1.5, "t": t, "b": b"\x01\xff",
             "nested": {"x": [1, "a", None, True]}})
    snap = ref.get()
    assert snap.exists and snap.id == DAO
    d = snap.to_dict()
    assert d["big"] == 2 ** 70 and d["small"] == 7 and d["f"] == 1.5 and d["b"] == b"\x01\xff"
    assert d["t"] == t and d["t"].tzinfo is not None
    assert d["nested"] == {"x": [1, "a", None, True]}
    assert snap.get("nested.x") == [1, "a", None, True]
    d["name"] = "changed"
    assert ref.get().to_dict()["name"] == "A"  # to_dict is a copy


def test_naive_datetime_is_utc(db):
    ref = db.document(f"{C}/d")
    ref.set({"t": datetime(2026, 1, 2, 3, 4, 5)})
    assert ref.get().to_dict()["t"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_missing_document(db):
    snap = db.collection(C).document("nope").get()
    assert not snap.exists and snap.to_dict() is None


def test_set_replaces_and_merge_merges_maps(db):
    ref = db.collection(C).document(DAO)
    ref.set({"a": 1, "m": {"x": 1, "y": 2}})
    ref.set({"m": {"y": 3, "z": 4}}, merge=True)
    assert ref.get().to_dict() == {"a": 1, "m": {"x": 1, "y": 3, "z": 4}}
    ref.set({"b": 2})
    assert ref.get().to_dict() == {"b": 2}


def test_merge_keys_are_field_names_not_paths(db):
    ref = db.document(f"{C}/d")
    ref.set({"jurisdiction.economy": "0x1"}, merge=True)
    assert ref.get().to_dict() == {"jurisdiction.economy": "0x1"}


def test_update_paths_and_backticks(db):
    ref = db.collection(C).document(DAO)
    ref.set({"registry": {"a": "1"}, "optionVotes": {}})
    ref.update({
        FieldPath("registry", "jurisdiction.autonet.substrate").to_api_repr(): "0xS",
        "optionVotes.3": "100",
        "votesCast": 1,
    })
    d = ref.get().to_dict()
    assert d["registry"] == {"a": "1", "jurisdiction.autonet.substrate": "0xS"}
    assert d["optionVotes"] == {"3": "100"} and d["votesCast"] == 1


def test_field_path_repr():
    assert FieldPath("registry", "description").to_api_repr() == "registry.description"
    assert FieldPath("registry", "a.b").to_api_repr() == "registry.`a.b`"
    assert FieldPath("x", "we`ird").to_api_repr() == "x.`we\\`ird`"
    from docstore.fields import parse
    assert parse("registry.`a.b`") == ("registry", "a.b")
    assert parse("x.`we\\`ird`") == ("x", "we`ird")


def test_update_missing_raises_not_found(db):
    with pytest.raises(NotFound):
        db.collection(C).document("nope").update({"a": 1})


def test_create_raises_already_exists(db):
    ref = db.collection(C).document("p")
    ref.create({"a": 1})
    with pytest.raises(AlreadyExists):
        ref.create({"a": 2})
    assert ref.get().to_dict() == {"a": 1}


def test_transforms(db):
    ref = db.collection(C).document(DAO)
    ref.set({"c": ["a"], "n": 1, "economy": "0xE"})
    ref.update({"c": firestore.ArrayUnion(["a", "b"]), "n": firestore.Increment(2),
                "economy": firestore.DELETE_FIELD, "at": firestore.SERVER_TIMESTAMP})
    d = ref.get().to_dict()
    assert d["c"] == ["a", "b"] and d["n"] == 3 and "economy" not in d
    assert isinstance(d["at"], datetime)
    ref.update({"c": firestore.ArrayRemove(["a"])})
    assert ref.get().to_dict()["c"] == ["b"]
    ref.set({"fresh": firestore.ArrayUnion(["x"]), "k": firestore.Increment(5)}, merge=True)
    d = ref.get().to_dict()
    assert d["fresh"] == ["x"] and d["k"] == 5


def test_delete(db):
    ref = db.collection(C).document(DAO)
    ref.set({"a": 1})
    ref.delete()
    assert not ref.get().exists
    ref.delete()  # deleting a missing doc is fine


def test_subcollections_and_parents(db):
    dao = db.collection(C).document(DAO)
    v = dao.collection("proposals").document("42").collection("votes").document("0xV")
    v.set({"w": 1})
    assert v.path == f"{C}/{DAO}/proposals/42/votes/0xV"
    assert v.parent.id == "votes" and v.parent.parent.id == "42"
    assert v.parent.parent.parent.parent.id == DAO and v.parent.parent.parent.parent.parent.id == C
    assert db.collection(C).parent is None


def test_stream_where_order_limit(db):
    col = db.collection(C)
    for i, tok in enumerate(["0xT2", "0xT1", "0xT3"]):
        col.document(f"0xD{i}").set({"token": tok, "n": i})
    assert [s.id for s in col.stream()] == ["0xD0", "0xD1", "0xD2"]
    assert [s.id for s in col.where("token", "==", "0xT1").limit(1).stream()] == ["0xD1"]
    assert [s.id for s in col.where(filter=FieldFilter("n", ">=", 1)).stream()] == ["0xD1", "0xD2"]
    assert [s.id for s in col.order_by("n", direction="DESCENDING").stream()] == ["0xD2", "0xD1", "0xD0"]
    assert [s.id for s in col.where("missing", "==", 1).stream()] == []
    first = list(col.limit(1).stream())[0]
    assert [s.id for s in col.start_after(first).stream()] == ["0xD1", "0xD2"]
    assert len(col.list_documents()) == 3


def test_collection_group_bounded_like_startup_cache(db):
    db.collection(C).document("0xA").collection("projects").document("p1").set({"a": 1})
    db.collection(C).document("0xB").collection("projects").document("p2").set({"a": 2})
    db.collection("idaosOther").document("0xA").collection("projects").document("p3").set({"a": 3})
    db.collection(C).document("0xA").collection("debates").document("d1").set({"a": 4})
    group = db.collection_group("projects")
    low = db.document(f"{C}/\u0000/projects/\u0000")
    high = db.document(f"{C}/￿/projects/￿")
    docs = list(group.where(filter=FieldFilter(FieldPath.document_id(), ">=", low))
                .where(filter=FieldFilter(FieldPath.document_id(), "<=", high)).stream())
    assert [d.id for d in docs] == ["p1", "p2"]
    assert {d.id for d in group.stream()} == {"p1", "p2", "p3"}
    assert docs[0].reference.parent.parent.parent.id == C


def test_batch_is_atomic(db):
    a, b = db.collection(C).document("a"), db.collection(C).document("b")
    batch = db.batch()
    batch.set(a, {"x": 1})
    batch.update(b, {"y": 1})  # b does not exist: the whole batch fails
    with pytest.raises(NotFound):
        batch.commit()
    assert not a.get().exists
    batch = db.batch()
    batch.set(a, {"x": 1})
    batch.set(b, {"y": 2})
    assert len(batch) == 2
    batch.commit()
    assert a.get().to_dict() == {"x": 1} and b.get().to_dict() == {"y": 2}


def test_transactional_read_modify_write(db):
    ref = db.collection(C).document("p")
    ref.set({"votes": 0})

    @firestore.transactional
    def bump(transaction, r):
        snap = r.get(transaction=transaction)
        transaction.update(r, {"votes": snap.to_dict()["votes"] + 1})
        return "ok"

    threads = [threading.Thread(target=lambda: bump(db.transaction(), ref)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ref.get().to_dict()["votes"] == 20


def test_transaction_failure_writes_nothing(db):
    ref = db.collection(C).document("p")

    @firestore.transactional
    def broken(transaction):
        transaction.set(ref, {"a": 1})
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        broken(db.transaction())
    assert not ref.get().exists


def test_versions_and_change_log(db):
    ref = db.collection(C).document(DAO)
    r1 = ref.set({"a": 1})
    r2 = ref.update({"a": 2})
    ref.delete()
    rows, low = db.store.changes_since(0)
    assert [(p, d) for _, p, _, d in rows] == [(ref.path, 0), (ref.path, 0), (ref.path, 1)]
    vs = [v for _, _, v, _ in rows]
    assert vs == sorted(vs) and len(set(vs)) == 3
    assert r2.update_time > r1.update_time
    seq, last_v = db.store.head()
    assert seq == rows[-1][0] and last_v == vs[-1]


def test_snapshot_times(db):
    ref = db.collection(C).document("x")
    ref.set({"a": 1})
    first = ref.get()
    ref.update({"a": 2})
    second = ref.get()
    assert second.create_time == first.create_time and second.update_time > first.update_time


def test_two_connections_share_one_clock(tmp_path):
    path = str(tmp_path / "s.sqlite")
    a, b = Client(path), Client(path)
    seen = []
    for i in range(50):
        c = a if i % 2 else b
        seen.append(c.collection(C).document(f"d{i}").set({"i": i}).update_time)
    assert seen == sorted(seen) and len(set(seen)) == len(seen)
    assert len(list(b.collection(C).stream())) == 50
    a.close()
    b.close()


def test_prune_keeps_the_tail(tmp_path):
    s = Store(str(tmp_path / "s.sqlite"))
    c = Client(store=s)
    for i in range(10):
        c.collection(C).document(f"d{i}").set({"i": i})
    s.prune_changes(keep=3)
    rows, low = s.changes_since(0)
    assert len(rows) == 3 and low == rows[0][0]
    c.close()


def test_under_lists_a_subtree(db):
    db.collection(C).document("0xA").set({"a": 1})
    db.collection(C).document("0xA").collection("members").document("m").set({"m": 1})
    db.collection("idaosOther").document("0xA").set({"a": 1})
    assert [p for p, _, _ in db.store.under(C)] == [f"{C}/0xA", f"{C}/0xA/members/m"]
