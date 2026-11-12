# apps/debates/engine.py
#
# Debates (DEBATES.md): opened and retired by proposal through the registry, argued in signed messages
# that the Worker queues and this engine validates, tallied with every score floored at zero.
#
# Store, published (under the network's DAO collection C):
#   {C}/{dao}/debates/{id}                       the debate: topic, status, support, opposition, thesis stakes
#   {C}/{dao}/debates/{id}/arguments/{argId}     an argument: text, parent, side, stakes, direct, score
#   {C}/{dao}/debates/{id}/messages/{digest}     every accepted message, to replay and check
# Store, internal (not under C, never published):
#   debate_registry/{C}|{txHash}|{logIndex}      a debates.* registry change, until resolved to its proposal
#   debate_nonces/{C}|{dao}|{author}             the author's last accepted nonce in the DAO
#   debate_quota/{C}|{dao}|{day}                 accepted messages that day
#   checkpoints/debates-{C}                      the Worker queue cursor

import re
import time
from datetime import datetime, timezone

from web3 import Web3

from apps.debates import eip712
from apps.debates.config import BPS, DAILY_QUOTA, MAX_OPEN, MAX_TEXT_BYTES

ZERO = "0x" + "00" * 32
ID_RE = re.compile(r"^[0-9a-f]{16}$")
B32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
RESOLVE_GIVE_UP = 600  # seconds a registry change waits for its executed proposal to be indexed


class Reject(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _int(v):
    try:
        return int(str(v or 0))
    except ValueError:
        return 0


# ---------- tally (pure) ----------

def detached(args):
    """Ids of arguments that do not count: retired, or below a retired one"""
    out = set()
    for aid in args:
        cur, seen = aid, set()
        while cur and cur != ZERO and cur in args and cur not in seen:
            seen.add(cur)
            if args[cur].get("retired"):
                out.add(aid)
                break
            cur = args[cur].get("parent")
    return out


def tally(args, thesis_stakes, weights):
    """args: {id: {parent, con, stakes: {author: bps}, retired}}; weights: {author_lower: int}.
    Returns ({id: (direct, score)}, support, opposition, participants)."""
    gone = detached(args)
    live = {a: d for a, d in args.items() if a not in gone}

    def weight_of(stakes):
        return sum(weights.get(a.lower(), 0) * int(b) // BPS for a, b in (stakes or {}).items())

    children = {}
    for a, d in live.items():
        children.setdefault(d.get("parent") or ZERO, []).append(a)
    direct = {a: weight_of(d.get("stakes")) for a, d in live.items()}
    score = {}
    # post-order without recursion: children before parents
    order, stack = [], list(children.get(ZERO, []))
    while stack:
        a = stack.pop()
        order.append(a)
        stack.extend(children.get(a, []))
    for a in reversed(order):
        s = direct[a]
        for c in children.get(a, []):
            s += -score[c] if live[c].get("con") else score[c]
        score[a] = max(0, s)
    roots = children.get(ZERO, [])
    support = weight_of(thesis_stakes) + sum(score[c] for c in roots if not live[c].get("con"))
    opposition = sum(score[c] for c in roots if live[c].get("con"))
    participants = set(a.lower() for a in (thesis_stakes or {}))
    for d in live.values():
        participants.add(str(d.get("author", "")).lower())
        participants.update(a.lower() for a in (d.get("stakes") or {}))
    participants.discard("")
    return {a: (direct[a], score.get(a, 0)) for a in live}, support, opposition, len(participants)


def spent(args, thesis_stakes, author):
    """The author's bps on the thesis and on arguments that still count"""
    gone = detached(args)
    a = author.lower()
    total = sum(int(b) for k, b in (thesis_stakes or {}).items() if k.lower() == a)
    for aid, d in args.items():
        if aid not in gone:
            total += sum(int(b) for k, b in (d.get("stakes") or {}).items() if k.lower() == a)
    return total


# ---------- field checks ----------

def check_fields(type_, m):
    if type_ not in eip712.TYPES:
        raise Reject(f"unknown type {type_!r}")
    if not isinstance(m, dict):
        raise Reject("message is not an object")
    if not isinstance(m.get("debate"), str) or not ID_RE.match(m["debate"]):
        raise Reject("debate is not a 16-hex id")
    try:
        nonce = int(str(m.get("nonce")))
    except ValueError:
        raise Reject("bad nonce")
    if not 0 <= nonce < 2 ** 64:
        raise Reject("bad nonce")
    if type_ in ("Argument", "Stake"):
        try:
            bps = int(str(m.get("bps")))
        except ValueError:
            raise Reject("bad bps")
        if not 0 <= bps <= BPS:
            raise Reject("bps out of range")
    if type_ == "Argument":
        if not isinstance(m.get("parent"), str) or not B32_RE.match(m["parent"]):
            raise Reject("bad parent")
        if not isinstance(m.get("con"), bool):
            raise Reject("con is not a boolean")
        text = m.get("text")
        if not isinstance(text, str) or not 1 <= len(text.encode("utf-8")) <= MAX_TEXT_BYTES:
            raise Reject("text must be 1 to 4096 bytes")
    else:
        if not isinstance(m.get("argument"), str) or not B32_RE.match(m["argument"]):
            raise Reject("bad argument")


class Debates:
    def __init__(self, db, C, chain_id, queue=None, clock=time.time, log=print):
        self.db, self.C, self.chain_id, self.queue = db, C, int(chain_id), queue
        self.clock, self.log = clock, log
        self.cursor_ref = db.collection("checkpoints").document(f"debates-{C}")
        self.last_full_tally = 0.0

    # ---------- lookups ----------

    def dao_ref(self, address):
        if not isinstance(address, str) or not ADDR_RE.match(address):
            return None
        coll = self.db.collection(self.C)
        for cand in (Web3.to_checksum_address(address), address, address.lower()):
            ref = coll.document(cand)
            if ref.get().exists:
                return ref
        return None

    def _weight(self, dao_ref, author):
        for cand in (Web3.to_checksum_address(author), author.lower()):
            snap = dao_ref.collection("members").document(cand).get()
            if snap.exists:
                return _int(snap.to_dict().get("votingWeight"))
        return 0

    def _args(self, debate_ref):
        return {s.id: s.to_dict() for s in debate_ref.collection("arguments").stream()}

    # ---------- opening and retiring (registry) ----------

    def resolve_registry(self):
        """Turn debates.* registry changes into opened or retired debates once their executed proposal is
        indexed (it is logged after the registry change in the same transaction)."""
        pending = [s for s in self.db.collection("debate_registry").where("network", "==", self.C).stream()
                   if not s.to_dict().get("done")]
        pending.sort(key=lambda s: (s.to_dict().get("blockNumber", 0), s.to_dict().get("logIndex", 0)))
        for snap in pending:
            e = snap.to_dict()
            dao_ref = self.dao_ref(e.get("dao", ""))
            if dao_ref is None:
                self._done(snap, "unknown DAO")
                continue
            executed = [a.to_dict() for a in dao_ref.collection("activity").where("txHash", "==", e["txHash"]).stream()]
            executed = [a for a in executed if a.get("type") == "proposalExecuted"]
            proposal = None
            if executed:
                p = dao_ref.collection("proposals").document(str(executed[0].get("targetId"))).get()
                proposal = p.to_dict() if p.exists else None
            if proposal is None:
                if self.clock() - e.get("seenAt", self.clock()) > RESOLVE_GIVE_UP:
                    self._done(snap, "no executed proposal in the transaction")
                continue
            pid = str(executed[0].get("targetId"))
            when = executed[0].get("time") or _now()
            if e.get("value") == "open":
                why = self._open(dao_ref, e, pid, proposal, when)
            else:
                why = self._retire(dao_ref, e, pid, when)
            self._done(snap, why)

    def _done(self, snap, why):
        snap.reference.set({"done": True, "outcome": why}, merge=True)
        if why != "ok":
            self.log(f"[debates] registry change {snap.id}: {why}")

    def _open(self, dao_ref, e, pid, proposal, when):
        did = e["debate"]
        ref = dao_ref.collection("debates").document(did)
        cur = ref.get().to_dict() or {}
        if cur.get("status") == "open":
            return "ok"
        live = [s for s in dao_ref.collection("debates").where("status", "==", "open").stream()]
        if len(live) >= MAX_OPEN:
            return f"over {MAX_OPEN} open debates"
        doc = {"id": did, "title": proposal.get("title", ""), "thesis": proposal.get("description", ""),
               "link": proposal.get("externalResource") or "", "proposalId": pid, "status": "open",
               "openedAt": when, "retiredAt": None}
        if not cur:
            doc.update({"support": "0", "opposition": "0", "arguments": 0, "participants": 0, "stakes": {},
                        "lastMessageAt": None})
        ref.set(doc, merge=True)
        self._activity(dao_ref, e, "debateOpened", f"Opened the debate {doc['title']}", did, doc["title"], when)
        self.tally_debate(dao_ref, did)
        return "ok"

    def _retire(self, dao_ref, e, pid, when):
        did = e["debate"]
        ref = dao_ref.collection("debates").document(did)
        cur = ref.get().to_dict()
        if not cur or cur.get("status") != "open":
            return "ok"
        ref.set({"status": "retired", "retiredAt": when, "retiredBy": pid}, merge=True)
        self._activity(dao_ref, e, "debateRetired", f"Retired the debate {cur.get('title', '')}", did,
                       cur.get("title", ""), when)
        return "ok"

    def _activity(self, dao_ref, e, type_, summary, did, title, when):
        dao_ref.collection("activity").document(f"{e['txHash']}_{e['logIndex']}-debate").set({
            "type": type_, "summary": summary, "actor": "", "time": when, "blockNumber": e.get("blockNumber", 0),
            "txHash": e["txHash"], "targetKind": "debate", "targetId": did, "targetLabel": title})

    # ---------- messages ----------

    def apply(self, entry):
        """One queued message; returns "ok", "duplicate" or raises Reject"""
        type_, m, sig = entry.get("type"), entry.get("message"), entry.get("signature")
        check_fields(type_, m)
        dao_ref = self.dao_ref(entry.get("dao"))
        if dao_ref is None:
            raise Reject("unknown DAO")
        try:
            author = eip712.signer(type_, m, self.chain_id, dao_ref.id, sig)
            digest = eip712.digest(type_, m, self.chain_id, dao_ref.id)
        except Exception as ex:
            raise Reject(f"bad signature: {ex}")
        if entry.get("author") and entry["author"].lower() != author.lower():
            raise Reject("signer differs from the queued author")
        debate_ref = dao_ref.collection("debates").document(m["debate"])
        msg_ref = debate_ref.collection("messages").document(digest)
        prior = msg_ref.get()
        if prior.exists:
            if prior.to_dict().get("author", "").lower() != author.lower():
                raise Reject("argument id taken")  # the same signed fields from another author
            return "duplicate"
        debate = debate_ref.get().to_dict()
        if not debate or debate.get("status") != "open":
            raise Reject("debate is not open")
        dao = dao_ref.get().to_dict() or {}
        if self._weight(dao_ref, author) < max(1, _int(dao.get("proposalThreshold"))):
            raise Reject("voting power below the proposal threshold")
        nonce = int(str(m["nonce"]))
        nonce_ref = self.db.collection("debate_nonces").document(f"{self.C}|{dao_ref.id.lower()}|{author.lower()}")
        last = nonce_ref.get()
        if last.exists and nonce <= int(last.to_dict()["nonce"]):
            raise Reject("nonce not above the author's last")
        now = _now()
        quota_ref = self.db.collection("debate_quota").document(f"{self.C}|{dao_ref.id.lower()}|{now:%Y-%m-%d}")
        used = _int((quota_ref.get().to_dict() or {}).get("n"))
        if used >= DAILY_QUOTA:
            raise Reject("the DAO's daily message quota is used up")

        args = self._args(debate_ref)
        gone = detached(args)
        thesis = dict(debate.get("stakes") or {})
        have = spent(args, thesis, author)
        writes = []
        if type_ == "Argument":
            parent = m["parent"].lower()
            if parent != ZERO and (parent not in args or parent in gone):
                raise Reject("parent is missing or retired")
            bps = int(str(m["bps"]))
            if have + bps > BPS:
                raise Reject("over 100% of the author's voting power in this debate")
            writes.append((debate_ref.collection("arguments").document(digest), {
                "id": digest, "parent": parent, "con": m["con"], "author": author, "text": m["text"],
                "createdAt": now, "retired": False, "direct": "0", "score": "0",
                "stakes": {author: bps} if bps else {}}, False))
        elif type_ == "Stake":
            target = m["argument"].lower()
            bps = int(str(m["bps"]))
            if target == ZERO:
                old = sum(int(b) for k, b in thesis.items() if k.lower() == author.lower())
                thesis = {k: b for k, b in thesis.items() if k.lower() != author.lower()}
                if bps:
                    thesis[author] = bps
                if have - old + bps > BPS:
                    raise Reject("over 100% of the author's voting power in this debate")
                writes.append((debate_ref, {"stakes": thesis}, True))
            else:
                if target not in args or target in gone:
                    raise Reject("argument is missing or retired")
                stakes = {k: b for k, b in (args[target].get("stakes") or {}).items() if k.lower() != author.lower()}
                old = sum(int(b) for k, b in (args[target].get("stakes") or {}).items() if k.lower() == author.lower())
                if have - old + bps > BPS:
                    raise Reject("over 100% of the author's voting power in this debate")
                if bps:
                    stakes[author] = bps
                writes.append((debate_ref.collection("arguments").document(target), {"stakes": stakes}, "stakes"))
        else:  # Retire
            target = m["argument"].lower()
            if target not in args:
                raise Reject("argument is missing")
            if args[target].get("author", "").lower() != author.lower():
                raise Reject("only the author retires an argument")
            if args[target].get("retired"):
                raise Reject("already retired")
            writes.append((debate_ref.collection("arguments").document(target), {"retired": True}, True))

        batch = self.db.batch()
        for ref, data, mode in writes:
            if mode == "stakes":
                batch.update(ref, {"stakes": data["stakes"]})
            elif mode:
                batch.set(ref, data, merge=True)
            else:
                batch.create(ref, data)
        batch.create(msg_ref, {"type": type_, "author": author, "message": m, "signature": sig, "nonce": nonce,
                               "acceptedAt": now})
        batch.set(nonce_ref, {"nonce": nonce})
        batch.set(quota_ref, {"n": used + 1})
        batch.update(debate_ref, {"lastMessageAt": now})
        batch.commit()
        self.tally_debate(dao_ref, m["debate"])
        return "ok"

    # ---------- tally ----------

    def tally_debate(self, dao_ref, did):
        debate_ref = dao_ref.collection("debates").document(did)
        debate = debate_ref.get().to_dict()
        if not debate:
            return
        args = self._args(debate_ref)
        authors = set(a.lower() for a in (debate.get("stakes") or {}))
        for d in args.values():
            authors.update(a.lower() for a in (d.get("stakes") or {}))
        weights = {a: self._weight(dao_ref, a) for a in authors}
        scores, support, opposition, participants = tally(args, debate.get("stakes"), weights)
        batch, n = self.db.batch(), 0
        for aid, d in args.items():
            direct, score = scores.get(aid, (0, 0))
            if d.get("direct") != str(direct) or d.get("score") != str(score):
                batch.update(debate_ref.collection("arguments").document(aid), {"direct": str(direct), "score": str(score)})
                n += 1
        head = {"support": str(support), "opposition": str(opposition), "arguments": len(scores),
                "participants": participants}
        if any(debate.get(k) != v for k, v in head.items()):
            batch.update(debate_ref, head)
            n += 1
        if n:
            batch.commit()

    def tally_all(self):
        for dao in self.db.collection(self.C).stream():
            for d in dao.reference.collection("debates").where("status", "==", "open").stream():
                self.tally_debate(dao.reference, d.id)

    # ---------- the loop ----------

    def pull(self):
        """Apply the Worker's queued messages after the cursor; returns how many were read"""
        if self.queue is None:
            return 0
        after = _int((self.cursor_ref.get().to_dict() or {}).get("seq"))
        entries = self.queue.fetch(after)
        for e in sorted(entries, key=lambda x: int(x["seq"])):
            try:
                outcome = self.apply(e)
            except Reject as r:
                outcome = f"rejected: {r}"
            except Exception as ex:
                outcome = f"error: {ex}"
            self.log(f"[debates] queue {e.get('seq')} {e.get('type')} from {e.get('author')}: {outcome}")
            self.cursor_ref.set({"seq": int(e["seq"])}, merge=True)
        return len(entries)

    def tick(self):
        self.resolve_registry()
        self.pull()
        if self.clock() - self.last_full_tally >= 60:
            self.last_full_tally = self.clock()
            self.tally_all()
