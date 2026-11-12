# apps/trustless/indexer.py
#
# Economy projects live in the store, under the DAO each Economy belongs to:
#   {daos_collection}/{dao}/projects/{project}       project document (lowercase address as ID)
#       backers/{backer}                             one per backer
#       activity/{txHash}_{logIndex}                 every indexed transaction on the project
# Economies are found through the DAO documents' `economy` field, which the homebase app writes.

import json
import time
from web3 import Web3

from apps.trustless.config import (
    ECONOMY_EVENT_SIGNATURES,
    PROJECT_EVENT_SIGNATURES,
    EVENT_SIGNATURES,
    CHECKPOINT_FIELD,
)
from apps.trustless.paper import Paper, DAO_LINK_FIELDS
import startup_cache

STAGE_MAP = {
    0: 'open', 1: 'pending', 2: 'ongoing', 3: 'dispute',
    4: 'appealable', 5: 'appeal', 6: 'closed',
}


def initialize(db, web3: Web3, network_config: dict):
    """
    Called once at startup.
    Returns dict with event_signatures mapping hash -> event name.
    """
    print("[trustless] Initializing...")

    # Hash all event signatures
    event_sigs = {}
    for sig_string in EVENT_SIGNATURES:
        event_name = sig_string.split('(')[0]
        sig_hash = web3.keccak(text=sig_string).hex()
        event_sigs[sig_hash] = event_name
        print(f"[trustless]   {event_name}: {sig_hash[:20]}...")

    print(f"[trustless] Initialized with {len(event_sigs)} event signatures")

    return {
        'event_signatures': event_sigs,
    }


def _checksum(address):
    try:
        return Web3.to_checksum_address(address)
    except Exception:
        return None


def _discover(db, daos_collection: str):
    """
    Economies, from the DAO docs' `economy` field: {economy: dao}.
    Projects, from every projects subcollection under this network's DAOs:
    {project: (economy, dao, {kind: linked proposal id})}. A project stays followed after its Economy is
    detached from the DAO, since it keeps running. Also {project: stage} as recorded.
    Both reads are shared with the other apps' startup (startup_cache).
    """
    economies = {}
    for dao_doc in startup_cache.dao_docs(db.collection(daos_collection)):
        economy = (dao_doc.to_dict() or {}).get('economy')
        dao = _checksum(dao_doc.id)
        if economy and dao and _checksum(economy):
            economies[_checksum(economy)] = dao

    projects = {}
    stages = {}
    for project_doc in startup_cache.subcollection_docs(db, daos_collection, 'projects'):
        dao_ref = project_doc.reference.parent.parent
        data = project_doc.to_dict() or {}
        project, dao, economy = _checksum(project_doc.id), _checksum(dao_ref.id), _checksum(data.get('economyAddress', ''))
        if project and dao and economy:
            links = {kind: int(data[field]) for kind, field in DAO_LINK_FIELDS.items() if data.get(field)}
            projects[project] = (economy, dao, links)
            stages[project] = data.get('stage', '')
    return economies, projects, stages


def _papers(db, web3, daos_collection, economies, projects):
    papers = {}
    for economy, dao in economies.items():
        papers[economy] = Paper(address=economy, kind='economy', web3=web3, db=db,
                                daos_collection=daos_collection, dao_address=dao)
    for project, (economy, dao, links) in projects.items():
        paper = Paper(address=project, kind='project', web3=web3, db=db,
                      daos_collection=daos_collection, dao_address=dao, economy_address=economy)
        paper.dao_links = dict(links)
        papers[project] = paper
    return papers


def run_historical_sync(db, web3: Web3, network_config: dict, alert_func):
    """
    Full historical sync with optimizations:
    1. Own checkpoint field in the homebase contracts/{network} doc
    2. Scan all economies for NewProject events
    3. Batch scan ALL project addresses together (1 RPC call per chunk instead of N)
    4. Final state verification from contract reads
    """
    from apps.trustless.abis import projectReadAbi

    daos_collection = network_config['dao_collection_name']
    doc_name = network_config['firestore_doc_name']
    checkpoint_field = network_config.get('checkpoint_field', CHECKPOINT_FIELD)
    print(f"[trustless] Running historical sync for {daos_collection}...")

    economy_sig_hashes = [web3.keccak(text=sig).hex() for sig in ECONOMY_EVENT_SIGNATURES]
    project_sig_hashes = [web3.keccak(text=sig).hex() for sig in PROJECT_EVENT_SIGNATURES]

    # Our checkpoint; the first time, start where the DAO contracts start (fromBlock)
    current_block = web3.eth.block_number
    checkpoint = 0
    from_block_config = 0
    try:
        contracts_doc = db.collection("contracts").document(doc_name).get()
        if contracts_doc.exists:
            config = contracts_doc.to_dict()
            checkpoint = config.get(checkpoint_field, 0) or 0
            from_block_config = config.get('fromBlock', 0) or 0
    except Exception as e:
        print(f"[trustless] Could not read checkpoint: {e}")

    from_block = checkpoint + 1 if checkpoint > 0 else from_block_config

    if from_block >= current_block:
        print(f"[trustless] Already synced to block {checkpoint}")
        return

    print(f"[trustless] Syncing from block {from_block} to {current_block}")

    economies, projects, stages = _discover(db, daos_collection)
    if not economies and not projects:
        print(f"[trustless] No economies found in {daos_collection}")
    else:
        print(f"[trustless] Found {len(economies)} economies and {len(projects)} projects")

    papers = _papers(db, web3, daos_collection, economies, projects)
    economy_addresses = list(economies.keys())
    project_addresses = list(projects.keys())

    # Scan blocks - all economies + all projects in batched queries
    chunk_size = 500
    processed_from = from_block
    new_projects = 0
    events_processed = 0

    failures = 0
    touched = set()
    while processed_from <= current_block and (economy_addresses or project_addresses):
        to_block = min(processed_from + chunk_size - 1, current_block)

        try:
            if economy_addresses:
                for log in web3.eth.get_logs({'fromBlock': processed_from, 'toBlock': to_block, 'address': economy_addresses}):
                    topic0 = log['topics'][0].hex() if log['topics'] else None
                    if topic0 not in economy_sig_hashes:
                        continue
                    economy = Web3.to_checksum_address(log['address'])
                    result = papers[economy].handle_event(log, func='NewProject')
                    if result:
                        new_addr = Web3.to_checksum_address(result)
                        if new_addr not in papers:
                            project_addresses.append(new_addr)
                            papers[new_addr] = Paper(address=new_addr, kind='project', web3=web3, db=db,
                                                     daos_collection=daos_collection, dao_address=economies[economy],
                                                     economy_address=economy)
                            new_projects += 1

            # Query ALL project addresses in ONE call (batched)
            if project_addresses:
                for log in web3.eth.get_logs({'fromBlock': processed_from, 'toBlock': to_block, 'address': project_addresses}):
                    topic0 = log['topics'][0].hex() if log['topics'] else None
                    if topic0 not in project_sig_hashes:
                        continue
                    address = Web3.to_checksum_address(log['address'])
                    if address in papers:
                        event_name = PROJECT_EVENT_SIGNATURES[project_sig_hashes.index(topic0)].split('(')[0]
                        papers[address].handle_event(log, func=event_name)
                        touched.add(address)
                        events_processed += 1

        except Exception as e:
            # Never skip a chunk: retry it, and abort (leaving the checkpoint untouched) if it keeps failing.
            # Handlers are idempotent, so re-running a partly processed chunk is safe.
            failures += 1
            print(f"[trustless] Error at blocks {processed_from}-{to_block} (attempt {failures}): {e}")
            if failures >= 5:
                raise
            time.sleep(2 * failures)
            continue

        failures = 0
        processed_from = to_block + 1
        time.sleep(0.02)

    print(f"[trustless] Scanned: {new_projects} new projects, {events_processed} events processed")
    if new_projects:
        startup_cache.invalidate(db, ('group', daos_collection, 'projects'))

    # Final state verification: each project's stage on chain against the one recorded. Known projects
    # compare with the stage read at discovery; only a project whose events ran above, or a new one,
    # is read again, since its handlers may have changed it.
    print(f"[trustless] Verifying final state...")
    read_abi = json.loads(projectReadAbi)
    verified = 0
    for address in project_addresses:
        try:
            contract_stage = STAGE_MAP.get(web3.eth.contract(address=address, abi=read_abi).functions.stage().call(), 'unknown')
            ref = papers[address]._get_project_ref()
            if address in stages and address not in touched:
                fs_stage = stages[address]
            else:
                doc = ref.get()
                if not doc.exists:
                    continue
                fs_stage = doc.to_dict().get('stage', '')
            if fs_stage != contract_stage:
                ref.update({'stage': contract_stage})
                print(f"[trustless]   Fixed {address[:10]}...: {fs_stage} -> {contract_stage}")
            verified += 1
        except Exception:
            pass
        time.sleep(0.01)

    print(f"[trustless] Verified {verified} projects")

    try:
        db.collection("contracts").document(doc_name).set({checkpoint_field: current_block}, merge=True)
        print(f"[trustless] Updated checkpoint to block {current_block}")
    except Exception as e:
        print(f"[trustless] Could not update checkpoint: {e}")

    print(f"[trustless] Historical sync complete")


def setup_papers(db, web3: Web3, network_config: dict):
    """
    Creates Paper objects for every known economy and project.
    Returns dict with addresses and papers.
    """
    daos_collection = network_config['dao_collection_name']
    print(f"[trustless] Setting up papers for {daos_collection}...")

    economies, projects, _ = _discover(db, daos_collection)
    papers = _papers(db, web3, daos_collection, economies, projects)
    addresses = list(papers.keys())

    print(f"[trustless] Setup complete: {len(economies)} economies, {len(projects)} projects")

    return {
        'addresses': addresses,
        'papers': papers,
        'economy_addresses': list(economies.keys()),
        'daos_collection': daos_collection,
    }


def check_appeals(papers: dict, web3: Web3):
    """Clears appeal and veto links whose DAO proposal ended without executing (see Paper.check_links)."""
    pending = [p for p in papers.values() if isinstance(p, Paper) and p.kind == 'project' and p.dao_links]
    if not pending:
        return
    now = web3.eth.get_block('latest')['timestamp']
    for paper in pending:
        try:
            paper.check_links(now)
        except Exception as e:
            print(f"[trustless] Could not check the DAO proposals on {paper.address}: {e}")
