"""Path classification, list parameters and file names (CONTRACT.md sections 3.4, 4, 5.1)."""
import re
from dataclasses import dataclass

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
NET_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
PAGE_MAX = 256 * 1024
VIEW_MAX = 1024 * 1024
HEAD_MAX = 256 * 1024
ID_ORDER = "__name__"  # order.key of (id) asc lists


@dataclass(frozen=True)
class ListSpec:
    coll: str          # last collection name, used as "coll" in pages and indexes
    key: str           # order.key (ID_ORDER for (id) asc)
    dir: str
    part: str          # "rank" or "prefix"
    P: int
    views: tuple = ()


NET_DAOS = ListSpec("daos", "creationDate", "desc", "rank", 50, ("search",))
TOP_LISTS = {
    "proposals": ListSpec("proposals", "createdAt", "desc", "rank", 50, ("queued", "loc")),
    "members": ListSpec("members", ID_ORDER, "asc", "prefix", 400, ("top",)),
    "activity": ListSpec("activity", "time", "desc", "rank", 100),
    "projects": ListSpec("projects", "createdAt", "desc", "rank", 50, ("loc",)),
    "debates": ListSpec("debates", "openedAt", "desc", "rank", 50, ("loc",)),
}
NESTED = {
    ("proposals", "votes"): ListSpec("votes", "cast", "desc", "rank", 200),
    ("projects", "backers"): ListSpec("backers", ID_ORDER, "asc", "rank", 200),
    ("projects", "activity"): ListSpec("activity", "time", "desc", "rank", 100),
    ("debates", "arguments"): ListSpec("arguments", "createdAt", "asc", "rank", 200),
    ("debates", "messages"): ListSpec("messages", "acceptedAt", "asc", "rank", 200),
}
DOC = "__doc__"  # list key of the DAO doc inside its own scope


def seg(s):
    """Path segment as used in file paths, channels and list keys."""
    return s.lower() if ADDR_RE.match(s) else s


@dataclass(frozen=True)
class Place:
    dao: str        # lowercase DAO address (scope key)
    list_key: str   # DOC, a top-level list name, or a nested key like "proposals/42/votes"
    spec: ListSpec  # spec of the list the doc belongs to (NET_DAOS for the DAO doc)
    dao_seg: str    # DAO segment as stored (original case)


def classify(path, C):
    """Place of a Firestore path under collection C, or None (with a reason string)."""
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != C:
        return None, "other collection"
    rest = parts[1:]
    dao = rest[0]
    if not ADDR_RE.match(dao):
        return None, "dao id is not an address"
    d = dao.lower()
    n = len(rest)
    if n == 1:
        return Place(d, DOC, NET_DAOS, dao), None
    if n == 3 and rest[1] in TOP_LISTS:
        return Place(d, rest[1], TOP_LISTS[rest[1]], dao), None
    if n == 5 and (rest[1], rest[3]) in NESTED:
        return Place(d, f"{rest[1]}/{seg(rest[2])}/{rest[3]}", NESTED[(rest[1], rest[3])], dao), None
    return None, rest[1] if n >= 2 else "?"


def net_channel(N):
    return f"net:{N}"


def dao_channel(N, d):
    return f"dao:{N}:{d}"


def net_ptr_path(N):
    return f"{N}/ptr.json"


def dao_ptr_path(N, d):
    return f"{N}/d/{d}/ptr.json"


def obj_path(N, h):
    return f"{N}/o/{h}.json"


CC_IMMUTABLE = "public, max-age=31536000, immutable"
CC_POINTER = "public, max-age=5"
CC_NETWORKS = "public, max-age=60"
