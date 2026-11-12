# apps/agents/paper.py

from web3 import Web3

from activity import record_activity, short


def _as_bytes(value) -> bytes:
    """Normalise a web3 log field (HexBytes / hex str) to raw bytes."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        s = value[2:] if value.startswith("0x") else value
        return bytes.fromhex(s)
    raise TypeError(f"cannot coerce {type(value).__name__} to bytes")


class Paper:
    """A jurisdiction's Substrate: its agent events go to the DAO's activity feed. Events, keyed on the
    indexed `agent` address (topics[1]): AgentRegistered, OwnerBound (owner, parent), AgentRevoked (the
    agent and, implicitly, every descendant), EndpointUpdated ('' clears it)."""

    kind = "substrate"

    def __init__(self, address: str, web3: Web3, db, dao_ref=None):
        self.address = Web3.to_checksum_address(address)
        self.web3 = web3
        self.db = db
        self.dao_ref = dao_ref
        self.chain_id = int(web3.eth.chain_id)
        self.links = db.collection("substrates").document(f"{self.chain_id}:{self.address}").collection("agents")

    def _activity(self, log, type_, summary, agent):
        if self.dao_ref is not None:
            record_activity(self.dao_ref, self.web3, log, type_, summary,
                            target_kind='agent', target_id=agent, target_label=short(agent))

    def handle_event(self, log, func=None):
        """Dispatch a matched log. Returns None: a Substrate creates no contracts to follow."""
        try:
            if func == "AgentRegistered":
                agent = self._agent(log)
                self._activity(log, 'agentRegistered', f"Agent {short(agent)} registered", agent)
            elif func == "OwnerBound":
                agent, owner, parent = self._agent(log), self._agent(log, 2), self._agent(log, 3)
                self.links.document(agent).set({"parent": parent}, merge=True)
                self._activity(log, 'agentBound', f"Agent {short(agent)} bound to owner {short(owner)}", agent)
            elif func == "AgentRevoked":
                root = self._agent(log)
                n = self._descendants(root)
                more = f" and {n} descendant(s)" if n else ""
                self._activity(log, 'agentRevoked', f"Agent {short(root)}{more} revoked", root)
            elif func == "EndpointUpdated":
                agent = self._agent(log)
                (ws_endpoint,) = self.web3.codec.decode(["string"], _as_bytes(log["data"]))
                self._activity(log, 'agentEndpoint',
                               f"Agent {short(agent)} {'set its endpoint' if ws_endpoint else 'cleared its endpoint'}", agent)
        except Exception as e:
            print(f"[agents] error handling {func}: {e}")
        return None

    def _agent(self, log, i=1) -> str:
        return Web3.to_checksum_address("0x" + _as_bytes(log["topics"][i])[-20:].hex())

    def _descendants(self, root) -> int:
        todo, seen = [root], set()
        while todo:
            agent = todo.pop()
            if agent in seen:
                continue
            seen.add(agent)
            todo += [c.id for c in self.links.where("parent", "==", agent).stream()]
        return len(seen) - 1
