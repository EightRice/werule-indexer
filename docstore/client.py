"""A Firestore-shaped client over the SQLite store: the part of the google-cloud-firestore API the
indexer uses, with the same semantics, so the indexer's modules keep their code.

    db = Client("data/werule.sqlite")
    db.collection("idaosLocalhost").document(dao).set({...}, merge=True)
"""
import functools
import os
import random
import string
import threading
from datetime import datetime, timezone

from . import fields as F
from . import values
from .store import Store


class NotFound(Exception):
    pass


class AlreadyExists(Exception):
    pass


def _dt(us):
    return datetime.fromtimestamp(us // 1_000_000, tz=timezone.utc).replace(microsecond=us % 1_000_000)


def _check_path(path, kind):
    parts = path.split("/")
    if any(p == "" for p in parts):
        raise ValueError(f"invalid {kind} path {path!r}")
    if (len(parts) % 2 == 0) != (kind == "document"):
        raise ValueError(f"{path!r} is not a {kind} path")
    return path


class Client:
    def __init__(self, path=None, store=None):
        self.store = store or Store(path or os.getenv("DOCSTORE_PATH", "data/werule.sqlite"))
        # A transaction holds this for its whole run, so two threads' transactions don't interleave
        self.tx_lock = threading.RLock()

    def collection(self, path):
        return CollectionReference(self, _check_path(path, "collection"))

    def document(self, path):
        return DocumentReference(self, _check_path(path, "document"))

    def collection_group(self, cid):
        return Query(self, group=cid)

    def batch(self):
        return WriteBatch(self)

    def transaction(self):
        return Transaction(self)

    def close(self):
        self.store.close()

    # ---------- internals ----------

    def _ref(self, path):
        return DocumentReference(self, path)

    def _snap(self, path, row):
        if row is None:
            return DocumentSnapshot(DocumentReference(self, path), None, None, None)
        data_json, v, ct = row
        return DocumentSnapshot(DocumentReference(self, path), values.loads(data_json, self._ref), v, ct)

    def _commit(self, ops):
        """ops: [(kind, path, data, merge)] applied in order in one store transaction."""
        def run(tx):
            now = _dt(tx.now_us)
            results = []
            for kind, path, data, merge in ops:
                if kind == "delete":
                    tx.remove(path)
                    results.append(WriteResult(_dt(tx.v)))
                    continue
                row = tx.read(path)
                cur = values.loads(row[0]) if row else None
                if kind == "create":
                    if row is not None:
                        raise AlreadyExists(f"document {path} already exists")
                    doc = F.resolve(data, F._MISSING, now)
                elif kind == "set":
                    if merge:
                        doc = cur if cur is not None else {}
                        F.merge_into(doc, data, now)
                    else:
                        doc = F.resolve(data, F._MISSING, now)
                elif kind == "update":
                    if row is None:
                        raise NotFound(f"no document to update: {path}")
                    doc = cur
                    F.update_into(doc, data, now)
                else:
                    raise ValueError(kind)
                v = tx.put(path, values.dumps(doc))
                results.append(WriteResult(_dt(v)))
            return results
        return self.store.write(run)


class WriteResult:
    def __init__(self, update_time):
        self.update_time = update_time


class DocumentSnapshot:
    def __init__(self, reference, data, v, ct):
        self.reference = reference
        self._data = data
        self.update_time = _dt(v) if v is not None else None
        self.create_time = _dt(ct) if ct is not None else None
        self.v = v

    @property
    def id(self):
        return self.reference.id

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        if self._data is None:
            return None
        return values.loads(values.dumps(self._data))  # a copy the caller may change

    def get(self, field_path):
        if self._data is None:
            return None
        return F.get_path(self._data, field_path)


class DocumentReference:
    def __init__(self, client, path):
        self._client = client
        self.path = path

    @property
    def id(self):
        return self.path.rsplit("/", 1)[1]

    @property
    def parent(self):
        return CollectionReference(self._client, self.path.rsplit("/", 1)[0])

    def collection(self, cid):
        return CollectionReference(self._client, _check_path(f"{self.path}/{cid}", "collection"))

    def get(self, field_paths=None, transaction=None):
        return self._client._snap(self.path, self._client.store.read(self.path))

    def set(self, data, merge=False):
        return self._client._commit([("set", self.path, data, merge)])[0]

    def create(self, data):
        return self._client._commit([("create", self.path, data, False)])[0]

    def update(self, data):
        return self._client._commit([("update", self.path, data, False)])[0]

    def delete(self):
        return self._client._commit([("delete", self.path, None, False)])[0]

    def _key(self):
        return tuple(self.path.split("/"))

    def __eq__(self, other):
        return isinstance(other, DocumentReference) and other.path == self.path

    def __hash__(self):
        return hash(self.path)

    def __repr__(self):
        return f"DocumentReference({self.path!r})"


class FieldFilter:
    def __init__(self, field_path, op_string, value):
        self.field_path, self.op_string, self.value = field_path, op_string, value


def _cmp_key(v):
    """Firestore orders values by type first; within a type, naturally."""
    if v is None:
        return (0, 0)
    if isinstance(v, bool):
        return (1, v)
    if isinstance(v, (int, float)):
        return (2, v)
    if isinstance(v, datetime):
        return (3, v)
    if isinstance(v, str):
        return (4, v)
    if isinstance(v, (bytes, bytearray)):
        return (5, bytes(v))
    if isinstance(v, DocumentReference):
        return (6, v._key())
    if isinstance(v, list):
        return (8, [_cmp_key(x) for x in v])
    if isinstance(v, dict):
        return (9, sorted((k, _cmp_key(x)) for k, x in v.items()))
    return (10, repr(v))


class Query:
    def __init__(self, client, parent=None, group=None, filters=(), orders=(), limit_n=None, after=None):
        self._client = client
        self._parent, self._group = parent, group
        self._filters, self._orders = tuple(filters), tuple(orders)
        self._limit, self._after = limit_n, after

    def _with(self, **kw):
        a = dict(client=self._client, parent=self._parent, group=self._group, filters=self._filters,
                 orders=self._orders, limit_n=self._limit, after=self._after)
        a.update(kw)
        return Query(**a)

    def where(self, field_path=None, op_string=None, value=None, filter=None):
        f = filter or FieldFilter(field_path, op_string, value)
        return self._with(filters=self._filters + (f,))

    def order_by(self, field_path, direction="ASCENDING"):
        return self._with(orders=self._orders + ((field_path, direction),))

    def limit(self, n):
        return self._with(limit_n=n)

    def start_after(self, snapshot_or_values):
        return self._with(after=snapshot_or_values)

    def _value(self, snap, field_path):
        if field_path is F.DOCUMENT_ID or field_path == "__name__":
            return snap.reference
        return F.get_path(snap._data, field_path)

    def _match(self, snap, f):
        try:
            got = self._value(snap, f.field_path)
        except KeyError:
            return False  # Firestore never matches a document that lacks the field
        want = f.value
        if f.field_path is F.DOCUMENT_ID and isinstance(want, str):
            want = DocumentReference(self._client, f"{self._parent}/{want}") if self._parent else want
        op = f.op_string
        if op == "==":
            return got == want
        if op == "!=":
            return got != want
        if op == "in":
            return got in want
        if op == "not-in":
            return got not in want
        if op == "array-contains":
            return isinstance(got, list) and want in got
        if op == "array-contains-any":
            return isinstance(got, list) and any(w in got for w in want)
        a, b = _cmp_key(got), _cmp_key(want)
        if a[0] != b[0]:
            return False  # Firestore range filters only match values of the same type
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]

    def _rows(self):
        store = self._client.store
        return store.group(self._group) if self._group is not None else store.children(self._parent)

    def stream(self, transaction=None):
        snaps = [self._client._snap(path, (data, v, ct)) for path, data, v, ct in self._rows()]
        snaps = [s for s in snaps if all(self._match(s, f) for f in self._filters)]
        if self._group is not None:
            snaps.sort(key=lambda s: s.reference._key())
        for field_path, direction in reversed(self._orders):
            def key(s, fp=field_path):
                try:
                    return (0, _cmp_key(self._value(s, fp)))
                except KeyError:
                    return (1, ())
            present = [s for s in snaps if key(s)[0] == 0]  # Firestore drops docs missing the order field
            snaps = sorted(present, key=key, reverse=str(direction).upper().startswith("DESC"))
        if self._after is not None:
            if isinstance(self._after, DocumentSnapshot):
                paths = [s.reference.path for s in snaps]
                i = paths.index(self._after.reference.path) if self._after.reference.path in paths else -1
                snaps = snaps[i + 1:]
        if self._limit is not None:
            snaps = snaps[:self._limit]
        return iter(snaps)

    def get(self, transaction=None):
        return list(self.stream())


class CollectionReference(Query):
    def __init__(self, client, path):
        super().__init__(client, parent=path)
        self.path = path

    def _with(self, **kw):
        a = dict(client=self._client, parent=self._parent, group=None, filters=self._filters,
                 orders=self._orders, limit_n=self._limit, after=self._after)
        a.update(kw)
        return Query(**a)

    @property
    def id(self):
        return self.path.rsplit("/", 1)[-1]

    @property
    def parent(self):
        return DocumentReference(self._client, self.path.rsplit("/", 1)[0]) if "/" in self.path else None

    def document(self, document_id=None):
        if document_id is None:
            document_id = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(20))
        return DocumentReference(self._client, _check_path(f"{self.path}/{document_id}", "document"))

    def add(self, data, document_id=None):
        ref = self.document(document_id)
        res = ref.create(data)
        return res.update_time, ref

    def list_documents(self):
        return [DocumentReference(self._client, p) for p, _, _, _ in self._client.store.children(self.path)]


class WriteBatch:
    def __init__(self, client):
        self._client = client
        self._ops = []

    def set(self, reference, document_data, merge=False):
        self._ops.append(("set", reference.path, document_data, merge))

    def create(self, reference, document_data):
        self._ops.append(("create", reference.path, document_data, False))

    def update(self, reference, field_updates):
        self._ops.append(("update", reference.path, field_updates, False))

    def delete(self, reference):
        self._ops.append(("delete", reference.path, None, False))

    def commit(self):
        ops, self._ops = self._ops, []
        return self._client._commit(ops) if ops else []

    def __len__(self):
        return len(self._ops)


class Transaction(WriteBatch):
    """Reads see committed data; the writes apply together when the transactional function returns.
    The client's transaction lock keeps the read-modify-write isolated from this process's other
    threads; the indexer is a document's only writer, so that is all the isolation it needs."""


def transactional(fn):
    @functools.wraps(fn)
    def run(transaction, *args, **kwargs):
        with transaction._client.tx_lock:
            transaction._ops = []
            out = fn(transaction, *args, **kwargs)
            transaction.commit()
            return out
    return run
