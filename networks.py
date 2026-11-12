"""Networks are data, not code: one JSON file per network, written by the deploy that put the
factories on that chain (trustless-contracts), read by the indexer at startup and by the supervisor
to decide what to run.

    NETWORKS_SOURCE=dir:<root>            files <root>/networks/<Name>.json (default dir:., the repo)
    NETWORKS_SOURCE=r2:<bucket>           objects networks/<Name>.json (the data bucket: the files are public)

    python networks.py put <file>...      copies network files into NETWORKS_SOURCE, after checking them
    python networks.py list               the networks NETWORKS_SOURCE has

A network file is the network's contracts document: the same fields the apps read (rpc, chainId,
jurisdictionFactory, moduleCatalog, moduleProcessor, nativeProject, erc20Project, debatesFactory,
wrapper..., fromBlock), plus "name" (defaults to the file name). "rpc" is the public RPC URL the apps
use: the file is published (networks.json). The indexer connects through RPC_<NAME> (name upper-cased,
'-' as '_') when that is set, so a keyed RPC URL stays in the indexer's environment.

The indexer copies the file into the store as contracts/<Name>, where its modules read it and the
publisher lists it in networks.json; the indexer's checkpoint fields in that document are kept.
"""
import json
import os
import re

NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
PREFIX = "networks/"
CHECKPOINT_SUFFIX = "lastsyncedblock"


def _target():
    from publisher.targets import make_target
    spec = os.getenv("NETWORKS_SOURCE", "dir:.")
    return make_target(spec)


def _parse(path, raw):
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a network file is a JSON object")
    stem = path.rsplit("/", 1)[-1][:-len(".json")]
    name = data.get("name") or stem
    if name != stem:
        raise ValueError(f"{path}: name {name!r} differs from the file name")
    if not NAME_RE.match(name):
        raise ValueError(f"{path}: network name {name!r} does not match [A-Za-z0-9-]{{1,64}}")
    if not data.get("rpc") and not os.getenv(_rpc_var(name)):
        raise ValueError(f"{path}: no rpc (set it in the file or in {_rpc_var(name)})")
    data["name"] = name
    return name, data


def _rpc_var(name):
    return "RPC_" + name.upper().replace("-", "_")


def indexer_rpc(name, data):
    """The RPC URL the indexer uses: RPC_<NAME> when set, else the file's public one."""
    return os.getenv(_rpc_var(name)) or data["rpc"]


def list_networks(target=None):
    """{name: network data} of every network file"""
    target = target or _target()
    out = {}
    for path in sorted(target.list(PREFIX)):
        if not path.endswith(".json") or "/" in path[len(PREFIX):]:
            continue
        name, data = _parse(path, target.get(path))
        out[name] = data
    return out


def load(name, target=None):
    target = target or _target()
    path = f"{PREFIX}{name}.json"
    raw = target.get(path)
    if raw is None:
        raise FileNotFoundError(f"no network file {path} in {os.getenv('NETWORKS_SOURCE', 'dir:.')}")
    return _parse(path, raw)[1]


def collections(name):
    """The store names the apps use for a network: its contracts doc and its DAO collection."""
    return {"firestore_doc_name": name, "dao_collection_name": f"idaos{name}"}


def sync_to_store(db, name, data):
    """contracts/<name> := the network file, keeping the indexer's checkpoint fields. Writes only
    when something changed, so a restart doesn't touch networks.json."""
    ref = db.collection("contracts").document(name)
    cur = ref.get().to_dict() or {}
    kept = {k: v for k, v in cur.items() if k.lower().endswith(CHECKPOINT_SUFFIX)}
    new = dict(data)
    new.update(kept)
    if new != cur:
        ref.set(new)
        return True
    return False


def put(paths, target=None):
    target = target or _target()
    for p in paths:
        with open(p, "rb") as f:
            raw = f.read()
        stem = os.path.basename(p)
        name, _ = _parse(PREFIX + stem, raw)
        target.put(f"{PREFIX}{name}.json", raw, "no-store")
        print(f"{name}: {p} -> {os.getenv('NETWORKS_SOURCE', 'dir:.')} {PREFIX}{name}.json")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "put":
        put(sys.argv[2:])
    elif sys.argv[1:] == ["list"]:
        for n, d in list_networks().items():
            print(f"{n}: chainId {d.get('chainId')}, fromBlock {d.get('fromBlock', 0)}, rpc {d.get('rpc')}")
    else:
        sys.exit("usage: python networks.py put <file>... | list")
