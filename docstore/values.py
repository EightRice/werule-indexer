"""Document values as stored: Firestore-shaped Python values <-> tagged JSON.

The tags are the publisher's (publisher/CONTRACT.md section 3.1), so a stored document is already
in the shape the publisher encodes, and every value the indexer wrote comes back with its type:
datetimes stay timezone-aware UTC datetimes, ints beyond 2^53 stay ints, bytes stay bytes.
"""
import json
import math
from datetime import datetime, timezone

TAGS = ("$int", "$float", "$ts", "$bytes", "$ref", "$geo")
MAX_SAFE = 2 ** 53 - 1


def _ts(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".%06dZ" % dt.microsecond


def encode(v):
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v if abs(v) <= MAX_SAFE else {"$int": str(v)}
    if isinstance(v, float):
        if math.isnan(v):
            return {"$float": "NaN"}
        if math.isinf(v):
            return {"$float": "Infinity" if v > 0 else "-Infinity"}
        return v
    if isinstance(v, datetime):
        return {"$ts": _ts(v)}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$bytes": "0x" + bytes(v).hex()}
    if isinstance(v, (list, tuple)):
        return [encode(x) for x in v]
    if isinstance(v, dict):
        if len(v) == 1 and next(iter(v)) in TAGS:
            raise ValueError(f"map {v!r} looks like a tagged value")
        return {str(k): encode(x) for k, x in v.items()}
    path = getattr(v, "path", None)
    if path is not None and hasattr(v, "parent") and hasattr(v, "id"):
        return {"$ref": path}
    raise TypeError(f"unsupported document value {type(v).__name__}")


def decode(v, ref=None):
    """ref: callable path -> DocumentReference, for $ref values (a plain path string without it)"""
    if isinstance(v, list):
        return [decode(x, ref) for x in v]
    if isinstance(v, dict):
        if len(v) == 1:
            tag, x = next(iter(v.items()))
            if tag == "$int":
                return int(x)
            if tag == "$float":
                return float(x.replace("Infinity", "inf"))
            if tag == "$ts":
                return datetime.strptime(x, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            if tag == "$bytes":
                return bytes.fromhex(x[2:])
            if tag == "$ref":
                return ref(x) if ref else x
            if tag == "$geo":
                return tuple(x)
        return {k: decode(x, ref) for k, x in v.items()}
    return v


def dumps(data):
    return json.dumps(encode(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def loads(text, ref=None):
    return decode(json.loads(text), ref)
