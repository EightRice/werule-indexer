# startup_cache.py
#
# Startup reads several apps need: the network's DAO documents, and the projects and debates
# under them. Each is read once per startup and shared, instead of once per app and phase.
# A phase that changes what was read (new DAOs, an Economy attached, new projects or debates)
# invalidates it, so the next reader sees the change. app.py clears everything once startup is
# done; the main loop never reads from here.

_cache = {}


def _key(db, name):
    return (id(db), name)


def dao_docs(daos_collection):
    """The network's DAO documents (a list of snapshots), read once"""
    key = _key(daos_collection._client, ('daos', daos_collection.id))
    if key not in _cache:
        _cache[key] = list(daos_collection.stream())
    return _cache[key]


def subcollection_docs(db, daos_collection_id, name):
    """Every {daos_collection_id}/{dao}/{name}/{doc}, read once. The collection group query is limited
    to this network's DAOs by a document path range, so other networks' documents aren't read."""
    key = _key(db, ('group', daos_collection_id, name))
    if key not in _cache:
        _cache[key] = _network_group(db, daos_collection_id, name)
    return _cache[key]


def _network_group(db, daos_collection_id, name):
    from docstore import FieldPath, FieldFilter
    group = db.collection_group(name)
    try:
        # Paths order segment by segment, so this network's documents lie between these bounds
        low = db.document(f"{daos_collection_id}/\u0000/{name}/\u0000")
        high = db.document(f"{daos_collection_id}/\uffff/{name}/\uffff")
        docs = list(group.where(filter=FieldFilter(FieldPath.document_id(), '>=', low))
                    .where(filter=FieldFilter(FieldPath.document_id(), '<=', high)).stream())
    except Exception as e:
        print(f"[startup] Path-limited {name} query failed ({e}); reading the whole group")
        docs = list(group.stream())
    # Keep the check: the bounds are an optimisation, the parent is what decides
    return [d for d in docs if d.reference.parent.parent is not None
            and d.reference.parent.parent.parent.id == daos_collection_id]


def invalidate(db, *what):
    """Drop cached reads: ('daos', collection_id) or ('group', collection_id, name)"""
    for w in what:
        _cache.pop(_key(db, w), None)


def clear():
    _cache.clear()
