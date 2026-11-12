"""Field paths and write transforms, with Firestore's semantics."""
import re

_SIMPLE = re.compile(r"^[_a-zA-Z][_a-zA-Z0-9]*$")


class FieldPath:
    """FieldPath('registry', 'jurisdiction.economy'): a path whose parts may contain dots."""

    def __init__(self, *parts):
        if not parts or any(not isinstance(p, str) or p == "" for p in parts):
            raise ValueError(f"invalid field path parts {parts!r}")
        self.parts = tuple(parts)

    @classmethod
    def document_id(cls):
        return DOCUMENT_ID

    def to_api_repr(self):
        return ".".join(p if _SIMPLE.match(p) else "`" + p.replace("\\", "\\\\").replace("`", "\\`") + "`"
                        for p in self.parts)

    def __eq__(self, other):
        return isinstance(other, FieldPath) and other.parts == self.parts

    def __hash__(self):
        return hash(self.parts)

    def __repr__(self):
        return f"FieldPath{self.parts!r}"


class _DocumentId:
    def __repr__(self):
        return "__name__"


DOCUMENT_ID = _DocumentId()


def parse(path):
    """'a.b.`c.d`' -> ('a', 'b', 'c.d'); a FieldPath or a tuple passes through."""
    if isinstance(path, FieldPath):
        return path.parts
    if isinstance(path, tuple):
        return path
    parts, cur, i, quoted, started = [], [], 0, False, False
    while i < len(path):
        c = path[i]
        if quoted:
            if c == "\\" and i + 1 < len(path):
                cur.append(path[i + 1])
                i += 2
                continue
            if c == "`":
                quoted = False
            else:
                cur.append(c)
        elif c == "`":
            quoted, started = True, True
        elif c == ".":
            if not cur and not started:
                raise ValueError(f"empty segment in field path {path!r}")
            parts.append("".join(cur))
            cur, started = [], False
        else:
            cur.append(c)
        i += 1
    if quoted:
        raise ValueError(f"unterminated backtick in field path {path!r}")
    if not cur and not started:
        raise ValueError(f"empty segment in field path {path!r}")
    parts.append("".join(cur))
    return tuple(parts)


# ---------- transforms ----------

class _Sentinel:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


SERVER_TIMESTAMP = _Sentinel("SERVER_TIMESTAMP")
DELETE_FIELD = _Sentinel("DELETE_FIELD")


class ArrayUnion:
    def __init__(self, values):
        self.values = list(values)


class ArrayRemove:
    def __init__(self, values):
        self.values = list(values)


class Increment:
    def __init__(self, value):
        self.value = value


def _is_transform(v):
    return v is SERVER_TIMESTAMP or isinstance(v, (ArrayUnion, ArrayRemove, Increment))


def resolve(value, old, now):
    """The stored value of `value` written over `old` (old is _MISSING when absent)."""
    if value is SERVER_TIMESTAMP:
        return now
    if isinstance(value, Increment):
        base = old if isinstance(old, (int, float)) and not isinstance(old, bool) else 0
        return base + value.value
    if isinstance(value, ArrayUnion):
        out = list(old) if isinstance(old, list) else []
        for x in value.values:
            if x not in out:
                out.append(x)
        return out
    if isinstance(value, ArrayRemove):
        return [x for x in old if x not in value.values] if isinstance(old, list) else []
    if isinstance(value, dict):
        return {k: resolve(v, _MISSING, now) for k, v in value.items() if v is not DELETE_FIELD}
    if isinstance(value, (list, tuple)):
        return [resolve(v, _MISSING, now) for v in value]
    return value


class _Missing:
    pass


_MISSING = _Missing()


def merge_into(doc, data, now):
    """set(data, merge=True): keys are field names; maps merge recursively, other values replace."""
    for k, v in data.items():
        if v is DELETE_FIELD:
            doc.pop(k, None)
        elif isinstance(v, dict) and not _is_transform(v) and isinstance(doc.get(k), dict):
            merge_into(doc[k], v, now)
        elif isinstance(v, dict) and not _is_transform(v):
            doc[k] = {}
            merge_into(doc[k], v, now)
        else:
            doc[k] = resolve(v, doc.get(k, _MISSING), now)


def update_into(doc, data, now):
    """update(data): keys are field paths; the value at each path replaces what was there."""
    for key, v in data.items():
        parts = parse(key)
        node = doc
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                if v is DELETE_FIELD:
                    node = None
                    break
                nxt = node[p] = {}
            node = nxt
        if node is None:
            continue
        last = parts[-1]
        if v is DELETE_FIELD:
            node.pop(last, None)
        else:
            node[last] = resolve(v, node.get(last, _MISSING), now)


def get_path(doc, path):
    node = doc
    for p in parse(path):
        if not isinstance(node, dict) or p not in node:
            raise KeyError(path)
        node = node[p]
    return node
