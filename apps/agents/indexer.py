# apps/agents/indexer.py
#
# The Substrates aren't known from the network file: each comes from a DAO's registry. Startup reads
# them from the indexed DAOs (jurisdiction_substrates); the main loop adds a Substrate when a DAO's
# registry gains the entry (the DAO module returns ('substrate', address, dao)). A restart replays the
# blocks since the checkpoint, which covers agent events missed while stopped.

from web3 import Web3

from apps.agents.config import EVENT_SIGNATURES


def initialize(db, web3: Web3, network_config: dict):
    event_sigs = {}
    for sig in EVENT_SIGNATURES:
        # The main loop matches topics[0].hex(), which has no 0x prefix
        event_sigs[web3.keccak(text=sig).hex().removeprefix("0x")] = sig.split("(")[0]
    return {"event_signatures": event_sigs}


def setup_papers(db, web3: Web3, network_config: dict):
    return {"addresses": [], "papers": {}}


def run_historical_sync(db, web3: Web3, network_config: dict, alert_func):
    pass


def jurisdiction_substrates(daos_collection) -> list:
    """Every indexed jurisdiction's Substrate with its DAO, (substrate, dao doc id), from its
    jurisdiction.autonet.substrate registry entry (the DAO module keeps each DAO's registry map)."""
    import startup_cache
    out = []
    for doc in startup_cache.dao_docs(daos_collection):
        value = ((doc.to_dict() or {}).get("registry") or {}).get("jurisdiction.autonet.substrate", "")
        if Web3.is_address(value):
            out.append((Web3.to_checksum_address(value), doc.id))
    return out
