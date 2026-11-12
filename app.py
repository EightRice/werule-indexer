# indexer/app.py

# --- Imports (Standard Libraries First) ---
import sys
import os
import re
import time
import argparse
import traceback
import yaml
import importlib
from datetime import datetime, timezone

# --- Load .env file BEFORE any other imports that might need it ---
from dotenv import load_dotenv
load_dotenv()

# --- Imports (Heavy/Custom Libraries After .env is loaded) ---
from web3 import Web3
from web3.exceptions import Web3RPCError

# --- Safe Import for Discord Alerter ---
try:
    from apps.generic.services import send_indexer_alert
    CAN_SEND_ALERTS = True
except ImportError:
    print("WARNING: 'generic/services.py' not found. Discord alerts will be disabled.")
    def send_indexer_alert(msg, network="N/A", app="N/A"): pass
    CAN_SEND_ALERTS = False

# --- Argument Parsing ---
parser = argparse.ArgumentParser(description="werule indexer: DAOs and their projects.")
parser.add_argument('network', help="Network name: its file networks/<name>.json in NETWORKS_SOURCE (e.g. Localhost)")
parser.add_argument('app', nargs='*', default=None, choices=['homebase', 'trustless', 'agents', 'debates', 'all'], help="App(s) to index. One or more, or omit for 'all'.")

parser.add_argument('--instance-id', type=str,
                    help="Instance name; an 'indexer-e2e' prefix guards the main local stack")

parser.add_argument('--skip-historical', action='store_true',
                    help="Skip historical sync and go straight to active listening mode")

parser.add_argument('--no-alerts', action='store_true',
                    help="Disable Discord alerts (useful for local testing)")

args = parser.parse_args()

def alert(message: str):
    """Helper function to safely send alerts with network/app context."""
    if CAN_SEND_ALERTS and not args.no_alerts:
        send_indexer_alert(message, network=args.network, app=args.app)

# --- Load Config to Get Enabled Apps ---
config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
try:
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    enabled_apps = config.get('enabled_apps', [])
    print(f"Loaded config.yaml: enabled_apps = {enabled_apps}")
except Exception as e:
    error_msg = f"Could not load config.yaml: {e}"
    print(f"FATAL: {error_msg}")
    alert(error_msg)
    sys.exit(1)

# --- Filter Enabled Apps Based on Command-Line Argument(s) ---
# `app` is a list: ['all'] (default) runs every enabled app; otherwise run the
# named subset, in the order given, restricted to apps enabled in config.yaml.
requested = args.app or ['all']   # omitted (None) or empty -> all
if 'all' in requested:
    apps_to_run = enabled_apps
else:
    apps_to_run = [a for a in requested if a in enabled_apps]
    missing = [a for a in requested if a not in enabled_apps]
    if missing:
        error_msg = (f"App(s) {missing} not enabled in config.yaml. "
                     f"Enabled apps: {enabled_apps}")
        print(f"FATAL: {error_msg}")
        alert(error_msg)
        sys.exit(1)

print(f"--- Indexer starting for Network: {args.network.upper()}, App(s): {', '.join(apps_to_run).upper()} ---")

# --- Network: its file, copied into the store ---
import networks
import docstore

try:
    network_file = networks.load(args.network)
except Exception as e:
    error_msg = f"Could not load network '{args.network}': {e}"
    print(f"FATAL: {error_msg}")
    alert(error_msg)
    sys.exit(1)
rpc = networks.indexer_rpc(args.network, network_file)

DEFAULT_DOCSTORE = os.path.join("data", "werule.sqlite")
docstore_path = os.getenv("DOCSTORE_PATH") or DEFAULT_DOCSTORE
# An e2e instance must never touch the owner's local stack (hardhat on 8545, the default store)
if ((args.instance_id or "").startswith("indexer-e2e") and os.getenv("E2E_ALLOW_MAIN_STACK") != "1"
        and os.path.abspath(docstore_path) == os.path.abspath(DEFAULT_DOCSTORE)):
    print("FATAL: e2e indexer pointed at the main local store; set DOCSTORE_PATH")
    sys.exit(2)
db = docstore.Client(docstore_path)
print(f"Document store: {os.path.abspath(docstore_path)}")
if networks.sync_to_store(db, args.network, network_file):
    print(f"Network file {args.network} copied into contracts/{args.network}")

# --- Dynamic App Loading ---
app_modules = {}  # Store loaded app modules
app_dbs = {}  # The store client each app writes through (one store for all)
app_configs = {}  # Store app network configs

for app_name in apps_to_run:
    try:
        print(f"\n--- Loading app: {app_name} ---")
        app_config_module = importlib.import_module(f'apps.{app_name}.config')
        app_indexer_module = importlib.import_module(f'apps.{app_name}.indexer')
        app_modules[app_name] = {
            'config': app_config_module,
            'indexer': app_indexer_module
        }
        network_config = networks.collections(args.network)
        if getattr(app_config_module, 'CHECKPOINT_FIELD', None):
            network_config['checkpoint_field'] = app_config_module.CHECKPOINT_FIELD
        app_configs[app_name] = network_config
        app_dbs[app_name] = db
        print(f"Network config for {app_name}: {network_config}")
    except Exception as e:
        error_msg = f"Could not load app '{app_name}': {e}"
        print(f"FATAL: {error_msg}")
        traceback.print_exc()
        alert(error_msg)
        sys.exit(1)

# The main local chain is off limits to e2e instances too
if ((args.instance_id or "").startswith("indexer-e2e") and rpc.rstrip("/").endswith(":8545")
        and os.getenv("E2E_ALLOW_MAIN_STACK") != "1"):
    print(f"FATAL: e2e indexer would read the main local chain ({rpc})")
    sys.exit(2)

web3 = Web3(Web3.HTTPProvider(rpc))
if not web3.is_connected():
    error_msg = f"Node connection to {rpc} failed!"
    print(f"FATAL: {error_msg}")
    alert(error_msg)
    sys.exit(1)
print(f"Node connected successfully to {rpc}")

# --- Initialize Apps and Setup Event Listening ---
listening_to_addresses = []
event_signatures = {}
papers = {}
app_data = {}  # Store app-specific data (addresses, papers, collections, etc.)

# --- Dynamic App Initialization ---
for app_name in apps_to_run:
    try:
        print(f"\n=== Initializing app: {app_name} ===")
        db = app_dbs[app_name]
        network_config = app_configs[app_name]
        indexer_module = app_modules[app_name]['indexer']

        # Call app's initialize() function
        init_result = indexer_module.initialize(db, web3, network_config)
        print(f"Initialization result for {app_name}: {list(init_result.keys())}")

        # Add event signatures from this app
        app_event_sigs = init_result.get('event_signatures', {})
        event_signatures.update(app_event_sigs)
        print(f"Added {len(app_event_sigs)} event signatures from {app_name}")

        # Store app-specific data for later use
        app_data[app_name] = init_result

    except Exception as e:
        error_msg = f"Could not initialize app '{app_name}': {e}"
        print(f"FATAL: {error_msg}")
        traceback.print_exc()
        alert(error_msg)
        sys.exit(1)

# --- Sync checkpoints ---
# Each app keeps its own checkpoint in the store's contracts/<network> doc, in the field its network
# config names (lastSyncedBlock by default, projectsLastSyncedBlock for projects), so every app's
# checkpoint stays meaningful. On resume we take the lowest, so no app misses events.
def _checkpoint_refs():
    refs = []
    for name in apps_to_run:
        doc_name = app_configs.get(name, {}).get('firestore_doc_name')
        field = app_configs.get(name, {}).get('checkpoint_field', 'lastSyncedBlock')
        if doc_name and app_dbs.get(name) is not None:
            refs.append((name, app_dbs[name].collection("contracts").document(doc_name), field))
    return refs

def read_checkpoint():
    blocks = []
    for name, ref, field in _checkpoint_refs():
        try:
            doc = ref.get()
            block = doc.to_dict().get(field) if doc.exists else None
        except Exception as e:
            print(f"Warning: Could not read lastSyncedBlock for {name}: {e}")
            continue
        if block:
            blocks.append(block)
    return min(blocks) if blocks else None

def write_checkpoint(block):
    # Both apps keep their fields in the same doc: one write per doc
    by_doc = {}
    for name, ref, field in _checkpoint_refs():
        entry = by_doc.setdefault((id(ref._client), ref.path), [ref, [], {}])
        entry[1].append(name)
        entry[2][field] = block
    for ref, names, fields in by_doc.values():
        try:
            ref.set(fields, merge=True)
        except Exception as e:
            print(f"Warning: Could not update lastSyncedBlock for {', '.join(names)}: {e}")

# --- Historical Sync for All Apps ---
# Only run if: checkpoint exists AND --skip-historical is NOT set
#
# The main loop resumes from the lowest checkpoint as it was BEFORE these syncs, so it replays the
# whole downtime gap. That is deliberate: the per-app syncs don't cover everything the live loop does
# (linking appeal and veto proposals to their projects, the jurisdictions' Substrates, SupermajoritySet
# entries), and every handler is idempotent (proposals and DAOs are created once, votes are skipped
# once recorded, balances are read from chain), so the replay adds what is missing and changes nothing
# else.
resume_checkpoint = read_checkpoint()
# A store that has never indexed this network starts where the network's contracts start (the network
# file's fromBlock), so a fresh store, or a new one built for a network that already has DAOs, misses
# nothing. Without fromBlock it starts near the chain's head.
if resume_checkpoint is None and int(network_file.get('fromBlock') or 0) > 0:
    resume_checkpoint = int(network_file['fromBlock']) - 1
    print(f"No checkpoint: indexing from the network's fromBlock {resume_checkpoint + 1}")
if args.skip_historical:
    print("\n=== SKIPPING HISTORICAL SYNC (--skip-historical flag) ===")
else:
    # Check if checkpoint exists
    checkpoint_exists = resume_checkpoint is not None

    if not checkpoint_exists:
        print("\n=== SKIPPING HISTORICAL SYNC (no checkpoint found) ===")
    else:
        for app_name in apps_to_run:
            try:
                print(f"\n=== Running historical sync for app: {app_name} ===")
                db = app_dbs[app_name]
                network_config = app_configs[app_name]
                indexer_module = app_modules[app_name]['indexer']

                # Call app's run_historical_sync() function
                indexer_module.run_historical_sync(db, web3, network_config, alert)

            except Exception as e:
                error_msg = f"Error during {app_name} historical sync: {e}"
                print(f"ERROR: {error_msg}")
                traceback.print_exc()
                alert(error_msg)

# --- Setup Paper Objects for All Apps ---
for app_name in apps_to_run:
    try:
        print(f"\n=== Setting up papers for app: {app_name} ===")
        db = app_dbs[app_name]
        network_config = app_configs[app_name]
        indexer_module = app_modules[app_name]['indexer']

        # Call app's setup_papers() function
        papers_result = indexer_module.setup_papers(db, web3, network_config)

        # Extract addresses and papers
        app_addresses = papers_result.get('addresses', [])
        app_papers = papers_result.get('papers', {})

        listening_to_addresses.extend(app_addresses)
        papers.update(app_papers)

        # Store additional app-specific data (like collections)
        app_data[app_name].update({
            'addresses': app_addresses,
            'papers': app_papers,
            'daos_collection': papers_result.get('daos_collection'),  # For homebase
        })

        print(f"Added {len(app_addresses)} addresses and {len(app_papers)} papers from {app_name}")

    except Exception as e:
        error_msg = f"Could not setup papers for app '{app_name}': {e}"
        print(f"FATAL: {error_msg}")
        traceback.print_exc()
        alert(error_msg)
        sys.exit(1)

import startup_cache

# --- Agents: follow every jurisdiction's Substrate ---
# Each jurisdiction with the autonet module records its Substrate in the registry; the DAO module keeps
# those maps, so read them once here, and the main loop adds new ones live.
def add_substrate_paper(address, dao):
    dao_ref = app_data['homebase']['daos_collection'].document(dao)
    if address in papers:
        if getattr(papers[address], 'dao_ref', None) is None:
            papers[address].dao_ref = dao_ref
        return
    from apps.agents.paper import Paper as AgentsPaper
    papers[address] = AgentsPaper(address, web3, app_dbs['agents'], dao_ref=dao_ref)
    if address not in listening_to_addresses:
        listening_to_addresses.append(address)
    print(f"[agents] Following Substrate {address} of DAO {dao}")

if 'agents' in apps_to_run and 'homebase' in apps_to_run:
    try:
        for substrate, dao in app_modules['agents']['indexer'].jurisdiction_substrates(app_data['homebase']['daos_collection']):
            add_substrate_paper(substrate, dao)
    except Exception as e:
        print(f"Warning: could not read jurisdiction Substrates: {e}")

# Startup reads were shared through startup_cache; the main loop reads live
startup_cache.clear()

# --- Debates: a thread of their own (registry changes, the Worker's queued messages, tallies) ---
if 'debates' in apps_to_run and 'homebase' in apps_to_run:
    app_modules['debates']['indexer'].start(db, args.network, app_configs['debates'], network_file, alert)

# --- Finalize Listener Setup ---
listening_to_addresses = list(set([addr for addr in listening_to_addresses if addr]))
print("\n--- Initializing with Monitored Event Signatures ---")
for hash_val, name in event_signatures.items():
    print(f"- {name}: {hash_val}")
print("----------------------------------------------------")
print(f"Listening for {len(event_signatures)} events on {len(listening_to_addresses)} contracts.")

# --- Main Indexing Loop ---
heartbeat = 0
# --- START OF CHANGE: Stateful Indexing Logic ---
# Some RPCs (like Base Sepolia public nodes) limit the number of addresses per query,
# so addresses are queried in batches and the results merged in chain order.
MAX_ADDRESSES_PER_QUERY = 5

def log_position(log_entry):
    return (log_entry['blockNumber'], log_entry['transactionIndex'], log_entry['logIndex'])

DAO_OVERRULE_SELECTOR = Web3.keccak(text="daoOverrule(uint256,string)")[:4]
DAO_VETO_SELECTOR = Web3.keccak(text="daoVeto()")[:4]
PROPOSAL_CREATED_TYPES = ["uint256", "address", "address[]", "uint256[]", "string[]", "bytes[]", "uint256", "uint256", "string"]

# Registers DAO proposals on the projects they veto or overrule, so the project holds signing and
# finalizing until the proposal resolves. Anyone may register; this is the automatic path. The key
# only pays gas: INDEXER_RELAYER_KEY, or on a local hardhat chain (31337) its public account #9.
HARDHAT_ACCOUNT_9 = "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6"
relayer_key = os.getenv("INDEXER_RELAYER_KEY") or (HARDHAT_ACCOUNT_9 if int(network_file.get("chainId") or 0) == 31337 else None)
if not relayer_key:
    print("No INDEXER_RELAYER_KEY: DAO proposals won't be registered on projects automatically")

def register_on_project(project, targets, values, calldatas, description):
    from apps.trustless.abis import register_dao_proposal_abi
    account = web3.eth.account.from_key(relayer_key)
    contract = web3.eth.contract(address=project, abi=register_dao_proposal_abi)
    fn = contract.functions.registerDaoProposal(targets, values, calldatas, Web3.keccak(text=description))
    try:
        fn.call({"from": account.address})
    except Exception as e:
        # Already registered by someone, closed, no longer live: nothing to do
        print(f"[trustless] Not registering on {project}: {e}")
        return
    tx = fn.build_transaction({"from": account.address, "nonce": web3.eth.get_transaction_count(account.address, "pending")})
    signed = account.sign_transaction(tx)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    print(f"[trustless] Registered DAO proposal on {project}: status {receipt.status}, tx {tx_hash.hex()}")

def notify_appeal_proposals(log_entry, papers):
    """A DAO proposal that calls daoVeto or daoOverrule on a trustless project: the project links it
    (an overrule is its appeal, a veto its veto), and either is registered on the project so it holds."""
    try:
        from eth_abi import decode
        proposal_id, _, targets, values, _, calldatas, _, _, description = decode(PROPOSAL_CREATED_TYPES, bytes(log_entry["data"]))
        projects = []
        for target, calldata in zip(targets, calldatas):
            address = Web3.to_checksum_address(target)
            paper = papers.get(address)
            if getattr(paper, "kind", None) != "project":
                continue
            if calldata[:4] == DAO_OVERRULE_SELECTOR:
                paper.handle_dao_proposal(log_entry, proposal_id, 'appeal')
            elif calldata[:4] == DAO_VETO_SELECTOR:
                paper.handle_dao_proposal(log_entry, proposal_id, 'veto')
            if calldata[:4] in (DAO_OVERRULE_SELECTOR, DAO_VETO_SELECTOR) and address not in projects:
                projects.append(address)
        if relayer_key:
            checksummed = [Web3.to_checksum_address(t) for t in targets]
            for project in projects:
                try:
                    register_on_project(project, checksummed, list(values), list(calldatas), description)
                except Exception as e:
                    print(f"[trustless] Could not register proposal {proposal_id} on {project}: {e}")
                    alert(f"Could not register DAO proposal {proposal_id} on project {project}: {e}")
    except Exception as e:
        print(f"[trustless] Could not inspect proposal for appeals: {e}")

def fetch_logs(addresses, from_block, to_block):
    if len(addresses) <= MAX_ADDRESSES_PER_QUERY:
        return list(web3.eth.get_logs({"fromBlock": from_block, "toBlock": to_block, "address": list(addresses)}))
    found = []
    num_batches = (len(addresses) + MAX_ADDRESSES_PER_QUERY - 1) // MAX_ADDRESSES_PER_QUERY
    for i in range(0, len(addresses), MAX_ADDRESSES_PER_QUERY):
        found.extend(web3.eth.get_logs({"fromBlock": from_block, "toBlock": to_block, "address": addresses[i:i + MAX_ADDRESSES_PER_QUERY]}))
        # Small delay between batches to avoid rate limiting (50ms per batch)
        if num_batches > 10 and i + MAX_ADDRESSES_PER_QUERY < len(addresses):
            time.sleep(0.05)
    found.sort(key=log_position)
    return found

last_processed_block = -1  # Sentinel for uninitialized state
BLOCK_HEADROOM = 3  # Query logs 3 blocks behind the head of the chain for safety
APPEAL_CHECK_SECONDS = 30  # between checks of open appeal proposals
last_appeal_check = 0
MAX_BLOCK_RANGE = 500 # Process a maximum of 500 blocks at a time to not overload the RPC
CHECKPOINT_INTERVAL = 60  # seconds between checkpoint writes
last_checkpoint_write = 0

while True:
    heartbeat += 1
    try:
        if not listening_to_addresses:
            print("No contracts to listen to. Waiting...")
            time.sleep(15)
            continue

        latest_on_chain = web3.eth.block_number

        # Initialize our block tracker on the first run.
        if last_processed_block == -1:
            if args.skip_historical:
                # Skip historical = start from current block (minus small buffer)
                last_processed_block = max(0, latest_on_chain - 15)
                print(f"Skipping historical sync. Starting from block {last_processed_block}.")
            else:
                checkpoint_block = resume_checkpoint

                # Use checkpoint if available, otherwise start from latest - 15
                if checkpoint_block is not None and checkpoint_block >= 0:
                    last_processed_block = checkpoint_block
                    print(f"Resuming from checkpoint. Starting from block {last_processed_block}.")
                else:
                    last_processed_block = max(0, latest_on_chain - 15)
                    print(f"No checkpoint found. Starting from block {last_processed_block}.")
            continue

        # Determine the range of blocks to scan in this iteration
        from_block = last_processed_block + 1
        to_block = max(0, latest_on_chain - BLOCK_HEADROOM)

        # If we are already caught up, just wait.
        if from_block > to_block:
            time.sleep(5)
            continue

        # If the indexer has been offline, process in manageable chunks.
        if (to_block - from_block) > MAX_BLOCK_RANGE:
            to_block = from_block + MAX_BLOCK_RANGE - 1
            print(f"[{args.network.upper()}] Large gap detected. Processing chunk: {from_block} -> {to_block}")

        logs = fetch_logs(listening_to_addresses, from_block, to_block)

        if logs:
            print(f"[{args.network.upper()}] Found {len(logs)} logs between blocks {from_block} and {to_block}")

        # Contracts discovered mid-batch (new DAOs, tokens, economies, projects) may emit events
        # later in this same range, so their logs are fetched and merged into the queue as found.
        pending_logs = list(logs)
        handled_logs = set()
        while pending_logs:
            log_entry = pending_logs.pop(0)
            log_key = (log_entry["transactionHash"], log_entry["logIndex"])
            if log_key in handled_logs:
                continue
            handled_logs.add(log_key)
            known_before = set(listening_to_addresses)
            tx_hash = log_entry["transactionHash"].hex()
            # The processed_transactions check was removed from here as it was causing events within the same tx to be skipped.
            # The block-by-block processing already prevents reprocessing old transactions.
            contract_address = Web3.to_checksum_address(log_entry["address"])
            if not log_entry["topics"]: continue
            event_signature_from_log = log_entry["topics"][0].hex()
            event_name = event_signatures.get(event_signature_from_log)
            if not event_name: continue
            print(f"-> Event: {event_name}, Contract: {contract_address}, Tx: {tx_hash}")
            if contract_address not in papers:
                print(f"WARNING: Paper object not found for {contract_address}. Skipping event.")
                continue

            new_contract_info = papers[contract_address].handle_event(log_entry, func=event_name)
            if event_name == 'ProposalCreated' and 'trustless' in apps_to_run:
                notify_appeal_proposals(log_entry, papers)
            
            # Handle new contract events dynamically per app
            if isinstance(new_contract_info, list) and len(new_contract_info) in (2, 3) and all(isinstance(x, str) for x in new_contract_info):
                # Homebase: New DAO created (returns [dao_address, token_address, registry_address])
                if 'homebase' in apps_to_run:
                    dao_address_new, token_address_new = new_contract_info[:2]
                    registry_address_new = Web3.to_checksum_address(new_contract_info[2]) if len(new_contract_info) == 3 and new_contract_info[2] else None
                    print(f"Adding new Homebase DAO {dao_address_new} and Token {token_address_new} to listener.")
                    if dao_address_new not in listening_to_addresses: listening_to_addresses.append(dao_address_new)
                    if token_address_new not in listening_to_addresses: listening_to_addresses.append(token_address_new)

                    # Get homebase-specific data
                    homebase_db = app_dbs['homebase']
                    homebase_data = app_data['homebase']
                    daos_collection = homebase_data.get('daos_collection')

                    # Import homebase Paper class
                    from apps.homebase.paper import Paper as HomebasePaper

                    if token_address_new not in papers:
                        p_new_token = HomebasePaper(address=token_address_new, kind="token", daos_collection=daos_collection, db=homebase_db, dao=dao_address_new, web3=web3)
                        papers.update({token_address_new: p_new_token})
                    else:
                        p_new_token = papers[token_address_new]
                    if dao_address_new not in papers:
                        papers.update({dao_address_new: HomebasePaper(token=p_new_token, address=dao_address_new, kind="dao", daos_collection=daos_collection, db=homebase_db, dao=dao_address_new, web3=web3)})
                    if registry_address_new and registry_address_new not in papers:
                        listening_to_addresses.append(registry_address_new)
                        papers.update({registry_address_new: HomebasePaper(address=registry_address_new, kind="registry", daos_collection=daos_collection, db=homebase_db, dao=dao_address_new, web3=web3)})
                    print(f"Now listening to {len(listening_to_addresses)} addresses.")
            elif isinstance(new_contract_info, list) and all(isinstance(item, tuple) for item in new_contract_info):
                # Homebase: SuiteConfigured returns list of tuples: [('economy', addr, dao_addr), ('registry', addr, dao_addr)];
                # a registry's jurisdiction.economy entry returns [('economy', addr, dao_addr)]
                for item in new_contract_info:
                    if item[0] == 'economy' and 'trustless' in apps_to_run:
                        economy_address_new = Web3.to_checksum_address(item[1])
                        print(f"Adding new Trustless Economy {economy_address_new} to listener.")
                        if economy_address_new not in listening_to_addresses: listening_to_addresses.append(economy_address_new)

                        if economy_address_new not in papers:
                            from apps.trustless.paper import Paper as TrustlessPaper
                            papers.update({economy_address_new: TrustlessPaper(
                                address=economy_address_new,
                                kind="economy",
                                web3=web3,
                                db=app_dbs['trustless'],
                                daos_collection=app_data['trustless'].get('daos_collection'),
                                dao_address=item[2],
                            )})
                        print(f"Now listening to {len(listening_to_addresses)} addresses.")
                    elif item[0] == 'substrate' and 'agents' in apps_to_run and 'homebase' in apps_to_run:
                        add_substrate_paper(item[1], item[2])
                    elif item[0] == 'registry' and 'homebase' in apps_to_run:
                        # Registry for Economy DAO description updates
                        registry_address_new = Web3.to_checksum_address(item[1])
                        dao_address_for_registry = Web3.to_checksum_address(item[2])
                        print(f"Adding new Registry {registry_address_new} (DAO: {dao_address_for_registry[:10]}...) to listener.")
                        if registry_address_new not in listening_to_addresses: listening_to_addresses.append(registry_address_new)

                        if registry_address_new not in papers:
                            homebase_db = app_dbs['homebase']
                            homebase_data = app_data['homebase']
                            daos_collection = homebase_data.get('daos_collection')
                            from apps.homebase.paper import Paper as HomebasePaper
                            papers.update({registry_address_new: HomebasePaper(
                                address=registry_address_new,
                                kind="registry",
                                daos_collection=daos_collection,
                                db=homebase_db,
                                dao=dao_address_for_registry,
                                web3=web3,
                            )})
                        print(f"Now listening to {len(listening_to_addresses)} addresses.")
            elif isinstance(new_contract_info, str) and new_contract_info.startswith("0x"):
                # A single address: a new project, returned by the economy that emitted NewProject
                # Determine which app based on the emitting paper's kind

                emitting_paper = papers.get(contract_address)
                emitting_kind = getattr(emitting_paper, 'kind', None) if emitting_paper else None

                # Trustless: New project created (economy emits NewProject)
                if 'trustless' in apps_to_run and emitting_kind == 'economy':
                    new_project_address = Web3.to_checksum_address(new_contract_info)
                    # contract_address IS the economy that emitted the NewProject event
                    economy_address = contract_address
                    print(f"Adding new Trustless Project {new_project_address} (economy: {economy_address[:10]}...) to listener.")
                    if new_project_address not in listening_to_addresses: listening_to_addresses.append(new_project_address)
                    if new_project_address not in papers:
                        from apps.trustless.paper import Paper as TrustlessPaper

                        # The project's documents go under the economy's DAO
                        papers.update({new_project_address: TrustlessPaper(
                            address=new_project_address,
                            kind="project",
                            web3=web3,
                            db=app_dbs['trustless'],
                            daos_collection=app_data['trustless'].get('daos_collection'),
                            dao_address=emitting_paper.dao_address,
                            economy_address=economy_address,
                        )})
                    print(f"Now listening to {len(listening_to_addresses)} addresses.")

            discovered = [a for a in listening_to_addresses if a not in known_before]
            if discovered:
                # Include the whole discovering transaction: a project's initial SendFunds is emitted
                # before the economy's NewProject in the same tx.
                tx_start = (log_entry['blockNumber'], log_entry['transactionIndex'], -1)
                later_logs = [l for l in fetch_logs(discovered, log_entry['blockNumber'], to_block)
                              if log_position(l) > tx_start and (l["transactionHash"], l["logIndex"]) not in handled_logs]
                if later_logs:
                    print(f"Fetched {len(later_logs)} later logs for {len(discovered)} newly discovered contract(s).")
                    pending_logs = sorted(pending_logs + later_logs, key=log_position)

        # IMPORTANT: Update our checkpoint after a successful run
        last_processed_block = to_block

        # Governors emit nothing when a proposal is defeated or lapses, so appeal and veto proposals are polled
        if 'trustless' in apps_to_run and time.time() - last_appeal_check >= APPEAL_CHECK_SECONDS:
            last_appeal_check = time.time()
            app_modules['trustless']['indexer'].check_appeals(papers, web3)

        # At most once a minute: a restart resumes from the checkpoint and the handlers are idempotent,
        # so a checkpoint up to a minute old only means re-reading that minute of logs
        should_update_checkpoint = time.time() - last_checkpoint_write >= CHECKPOINT_INTERVAL

        if apps_to_run and should_update_checkpoint:
            write_checkpoint(last_processed_block)
            last_checkpoint_write = time.time()

    except Web3RPCError as e:
        # This is a more robust way to catch recoverable errors without crashing
        error_message = str(e).lower()
        is_recoverable_error = any(phrase in error_message for phrase in ["block range", "unavailable", "block limit", "server error", "rate limit", "too many requests"])
        if is_recoverable_error:
            # Parse retry time from rate limit messages like "retry in 10m0s" or "retry in 30s"
            wait_time = 10  # Default wait time
            retry_match = re.search(r'retry in (\d+)m?(\d*)s?', str(e))
            if retry_match:
                minutes = int(retry_match.group(1)) if retry_match.group(1) else 0
                seconds = int(retry_match.group(2)) if retry_match.group(2) else 0
                # If the first capture is minutes (has 'm' after), parse accordingly
                if 'm' in str(e)[retry_match.start():retry_match.end()]:
                    wait_time = minutes * 60 + seconds
                else:
                    # Just seconds (e.g., "retry in 10s")
                    wait_time = minutes  # The first number is actually seconds
                # Cap at 10 minutes max, minimum 10 seconds
                wait_time = max(10, min(wait_time, 600))
            print(f"WARN: Recoverable RPC error encountered: {e}. Waiting {wait_time}s before retry.")
            time.sleep(wait_time)
        else:
            # For other, more severe RPC errors, trigger the full reconnect
            raise e
    except Exception as e:
        error_msg = f"MAIN LOOP ERROR: {e}"
        print(error_msg)
        traceback.print_exc()
        alert(error_msg)
        try:
            web3 = Web3(Web3.HTTPProvider(rpc))
            if web3.is_connected(): print("Node reconnected successfully.")
            else: print("Node reconnection failed!")
        except Exception as recon_e:
            print(f"Error during reconnection: {recon_e}")
            
    if heartbeat % 50 == 0:
        print(f"[{args.network.upper()}] Heartbeat: {heartbeat}. Listening to {len(listening_to_addresses)} addresses on app(s): {args.app}. Current Block: {last_processed_block}")
    
    # Adjust sleep time based on whether we are catching up or fully synced
    if (latest_on_chain - last_processed_block) > MAX_BLOCK_RANGE:
        time.sleep(0.5) # Sleep less if we are catching up
    else:
        time.sleep(5)   # Normal 5-second sleep if we are near the head
# indexer/app.py