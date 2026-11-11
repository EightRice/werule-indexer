# apps/homebase/paper.py

from apps.homebase.abis import wrapperAbi, daoAbiGlobal, tokenAbiGlobal, wrapper_token_abi, timelock_min_delay_abi, wrapper_w_abi, trustless_wrapper_abi, registryAbi
from activity import record_activity, format_units, short
from datetime import datetime, timezone, timedelta
from apps.homebase.entities import ProposalStatus, Proposal, StateInContract, Txaction, Token, Member, Org, Vote, balance_sort
import re
from web3 import Web3
from google.cloud import firestore
from google.cloud.firestore_v1.field_path import FieldPath
from google.api_core.exceptions import NotFound, AlreadyExists
import codecs
from apps.generic.converting import decode_function_parameters
from apps.homebase.eventSignatures import quorum_function_abi, voting_period_function_abi,proposal_threshold_function_abi, voting_delay_function_abi, mint_function_abi, burn_function_abi


ATN_EPOCH_SELECTOR = Web3.keccak(text="anchor(string,bytes32,bytes32,bytes32,string,bytes32,bytes32,uint256)")[:4]
ATN_EPOCH_TYPES = ["string", "bytes32", "bytes32", "bytes32", "string", "bytes32", "bytes32", "uint256"]
ATN_EPOCH_FIELDS = ["epochId", "epochRoot", "prevEpochRoot", "prevAnchorHash", "agentMintCid", "payloadHash", "agentMintRoot", "cutoff"]
# An AnchorElection's proposal for one epoch: a single-choice vote among the anchors its drawn agents register
ANCHOR_ELECTION_SELECTOR = Web3.keccak(text="anchorWinner(uint256,uint256)")[:4]
# Constitutional proposals (trustless-contracts docs/governance.md): the DAO's threshold, and the one
# each protected proposal got when it was proposed (0 for ordinary proposals). Absent on older governors.
SUPERMAJORITY_ABI = [
    {"type": "function", "name": "supermajorityBps", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "proposalSupermajority", "stateMutability": "view",
     "inputs": [{"name": "proposalId", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}]},
]
OPTION_SOURCE_ABI = [{"type": "function", "name": "optionSourceOf", "stateMutability": "view",
                      "inputs": [{"name": "proposalId", "type": "uint256"}], "outputs": [{"name": "", "type": "address"}]}]


def _decode_atn_epoch(calldatas):
    """The Substrate.anchor arguments if the proposal is exactly one anchor call, else None."""
    if len(calldatas) != 1:
        return None
    data = bytes(calldatas[0]) if not isinstance(calldatas[0], str) else bytes.fromhex(calldatas[0].removeprefix("0x"))
    if data[:4] != ATN_EPOCH_SELECTOR:
        return None
    try:
        from eth_abi import decode
        values = decode(ATN_EPOCH_TYPES, data[4:])
    except Exception:
        return None
    return {k: ("0x" + v.hex() if isinstance(v, bytes) else v) for k, v in zip(ATN_EPOCH_FIELDS, values)}


def _decode_anchor_election(calldatas):
    """(epochNumber, attempt) if the proposal is exactly one AnchorElection.anchorWinner call, else None."""
    if len(calldatas) != 1:
        return None
    data = bytes(calldatas[0]) if not isinstance(calldatas[0], str) else bytes.fromhex(calldatas[0].removeprefix("0x"))
    if data[:4] != ANCHOR_ELECTION_SELECTOR:
        return None
    try:
        from eth_abi import decode
        return decode(["uint256", "uint256"], data[4:])
    except Exception:
        return None


# A project appeal: the DAO overrules (or replaces a silent) arbiter of one of its Economy's projects
DAO_OVERRULE_SELECTOR = Web3.keccak(text="daoOverrule(uint256,string)")[:4]


def _decode_appeal(targets, calldatas):
    """{project, percent, ruling} if the proposal is exactly one daoOverrule call, else None."""
    if len(calldatas) != 1:
        return None
    data = bytes(calldatas[0]) if not isinstance(calldatas[0], str) else bytes.fromhex(calldatas[0].removeprefix("0x"))
    if data[:4] != DAO_OVERRULE_SELECTOR:
        return None
    try:
        from eth_abi import decode
        percent, ruling = decode(["uint256", "string"], data[4:])
    except Exception:
        return None
    return {"project": Web3.to_checksum_address(targets[0]), "percent": int(percent), "ruling": ruling}


# A project veto: the DAO closes one of its Economy's projects, refunding every backer
DAO_VETO_SELECTOR = Web3.keccak(text="daoVeto()")[:4]


def _decode_veto(targets, calldatas):
    """The vetoed project's address if the proposal is exactly one daoVeto call, else None."""
    if len(calldatas) != 1:
        return None
    data = bytes(calldatas[0]) if not isinstance(calldatas[0], str) else bytes.fromhex(calldatas[0].removeprefix("0x"))
    if data[:4] != DAO_VETO_SELECTOR:
        return None
    return Web3.to_checksum_address(targets[0])

class Paper:
    ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
    def __init__(self, address, kind, web3, daos_collection, db, dao=None, token=None):
        self.address = address
        self.kind = kind
        self.contract = None
        self.dao = dao
        self.token_paper: Paper = token
        self.web3: Web3 = web3
        self.daos_collection = daos_collection
        self.db = db
        self.abi_string = None

        if kind == "wrapper":
            self.abi_string = wrapperAbi
        elif kind == "jurisdiction":
            # JurisdictionFactory: every DAO, any token kind, emits NewDaoCreated
            self.abi_string = wrapperAbi
        elif kind == "wrapper_t":
            # Transferable non-wrapped token factory (emits NewDaoCreated)
            self.abi_string = wrapperAbi
        elif kind == "wrapper_w":
            # Wrapped ERC20 token factory (emits DaoWrappedDeploymentInfo)
            # wrapper_w_abi contains both NewDaoCreated and DaoWrappedDeploymentInfo for compatibility
            self.abi_string = wrapper_w_abi
        elif kind == "wrapper_trustless":
            # TrustlessFactory emits NewDaoCreated and SuiteConfigured events
            self.abi_string = trustless_wrapper_abi
        elif kind == "registry":
            # Registry contract for Economy DAOs - emits RegistryUpdated events
            self.abi_string = registryAbi
        elif kind == "token":
            self.abi_string = tokenAbiGlobal
        else: # dao
            self.abi_string = daoAbiGlobal
        
        if self.abi_string:
            self.abi = re.sub(r'\n+', ' ', self.abi_string).strip()
        else:
            self.abi = None


    def get_contract(self):
        if self.contract is None and self.address and self.abi:
            try:
                self.contract = self.web3.eth.contract(
                    address=Web3.to_checksum_address(self.address), abi=self.abi)
            except Exception as e:
                print(f"Error creating contract object for {self.address} with kind {self.kind}: {e}")
                return None
        return self.contract

    # ---- Activity feed (see activity.py) ----

    def _activity(self, log, type_, summary, dao=None, **kw):
        record_activity(self.daos_collection.document(dao or self.dao), self.web3, log, type_, summary, **kw)

    # Shared by every paper in the process: proposal titles seen (for activity entries), and member
    # documents known to exist (so updates merge without reading first). Both only grow; a title or
    # a member doc doesn't go away.
    _titles = {}
    _known_members = set()

    def _remember_title(self, proposal_id, title):
        if title:
            self._titles[(self.daos_collection.id, self.dao, proposal_id)] = title

    def _proposal_title(self, proposal_id):
        key = (self.daos_collection.id, self.dao, proposal_id)
        if key in self._titles:
            return self._titles[key]
        try:
            snap = self.daos_collection.document(self.dao).collection('proposals').document(proposal_id).get()
            title = (snap.to_dict() or {}).get('title') if snap.exists else None
        except Exception:
            title = None
        if title:
            self._titles[key] = title
        return title or f"proposal {proposal_id[:8]}"

    def _member_key(self, address, dao=None):
        return (self.daos_collection.id, dao or self.dao, address)

    def _remember_member(self, address, dao=None):
        self._known_members.add(self._member_key(address, dao))

    def _member_exists(self, address):
        """Whether the member doc exists, read at most once per member per process"""
        if self._member_key(address) in self._known_members:
            return True
        if self._member_ref(address).get().exists:
            self._remember_member(address)
            return True
        return False

    def _supermajority(self, governor, proposal_id=None):
        """The governor's threshold, or a proposal's (0 if unprotected); None if the governor predates them"""
        try:
            c = self.get_specific_contract(governor, SUPERMAJORITY_ABI)
            fn = c.functions.supermajorityBps() if proposal_id is None else c.functions.proposalSupermajority(proposal_id)
            return int(fn.call())
        except Exception:
            return None

    def refresh_supermajority(self, dao=None, known=None):
        """Records the DAO's supermajority threshold on its document, unless it already holds it (known)"""
        dao = dao or self.dao
        bps = self._supermajority(dao)
        if bps is None or bps == known:
            return
        try:
            self.daos_collection.document(dao).update({'supermajorityBps': bps})
        except NotFound:
            pass
        except Exception as e:
            print(f"Could not record the supermajority of DAO {dao}: {e}")

    def supermajority_set(self, log):
        """SupermajoritySet(oldBps, newBps): the DAO changed its threshold (or set it at creation)"""
        from eth_abi import decode
        old, new = decode(['uint256', 'uint256'], bytes(log['data']))
        try:
            self.daos_collection.document(self.dao).update({'supermajorityBps': int(new)})
        except NotFound:
            pass  # set at creation, before NewDaoCreated: add_dao reads it
        if old:
            self._activity(log, 'supermajoritySet',
                           f"Changed the supermajority from {old / 100:g}% to {new / 100:g}%")
        return None

    def _token_units(self):
        """(decimals, symbol) of the DAO's token, read once"""
        if not hasattr(self, '_units'):
            data = self.daos_collection.document(self.dao).get().to_dict() or {}
            self._units = (int(data.get('decimals') or 18), data.get('symbol') or '')
        return self._units

    def _block_time(self, log):
        """When the event happened on-chain (not when it was indexed, which differs on replays and catch-up)."""
        try:
            return datetime.fromtimestamp(self.web3.eth.get_block(log['blockNumber'])['timestamp'], tz=timezone.utc)
        except Exception as e:
            print(f"Could not read block time for log at block {log.get('blockNumber')}: {e}")
            return datetime.now(timezone.utc)

    def get_specific_contract(self, address, abi):
        """Helper to get a contract instance with a specific address and ABI."""
        try:
            final_abi = abi
            if isinstance(abi, str):
                final_abi = re.sub(r'\n+', ' ', abi).strip()
            
            return self.web3.eth.contract(
                address=Web3.to_checksum_address(address), abi=final_abi
            )
        except Exception as e:
            print(f"Error creating specific contract {address}: {e}")
            return None

    def add_dao(self, log):
        contract_instance = self.get_contract()
        if not contract_instance:
            print(f"Could not get contract instance for {self.address} in add_dao")
            return None
        try:
            decoded_event = contract_instance.events.NewDaoCreated().process_log(log)
        except Exception as e:
            print(f"Error processing NewDaoCreated log with ABI for {self.address}: {e}")
            return None

        args = decoded_event['args']
        name = args['name']
        safe_name = name.encode('ascii', 'replace').decode()
        print(f"New DAO (original wrapper): {safe_name} from event")
        
        org = Org(name=name)
        org.creationDate = self._block_time(log)
        org.govTokenAddress = args['token']
        org.address = args['dao']
        org.symbol = args['symbol']
        org.registryAddress = args['registry']
        org.description = args['description']
        members = args['initialMembers']
        amounts = args['initialAmounts']
        org.holders = len(members) if members else 0

        token_contract = self.get_specific_contract(org.govTokenAddress, tokenAbiGlobal)
        if token_contract:
            try:
                org.decimals = token_contract.functions.decimals().call()
            except Exception as e:
                print(f"Error fetching decimals for token {org.govTokenAddress}: {e}")
                org.decimals = 18

            # Fetch transferability status from token contract
            try:
                is_transferable = token_contract.functions.isTransferable().call()
                # Note: nonTransferrable is the OPPOSITE of isTransferable
                org.nonTransferrable = not is_transferable
                print(f"Token {org.govTokenAddress} isTransferable: {is_transferable}, nonTransferrable: {org.nonTransferrable}")
            except Exception as e:
                print(f"Error fetching isTransferable for token {org.govTokenAddress}: {e}")
                org.nonTransferrable = False  # Default to transferable if we can't read it
        else:
            org.decimals = 18
            org.nonTransferrable = False

        # Wrapped-token DAOs from the JurisdictionFactory also emit NewDaoCreated
        wrapped_contract = self.get_specific_contract(org.govTokenAddress, wrapper_token_abi)
        if wrapped_contract:
            try:
                org.underlyingToken = str(wrapped_contract.functions.underlying().call())
                print(f"Token {org.govTokenAddress} wraps {org.underlyingToken}")
            except Exception:
                pass  # not a WrappedRepToken
        
        # --- START OF FIX ---
        # The previous logic incorrectly tried to read settings from the 'initialAmounts' array.
        # The correct method is to query the newly created DAO contract for its actual settings.
        dao_contract = self.get_specific_contract(org.address, daoAbiGlobal)
        if dao_contract:
            try:
                # Fetch settings directly from the DAO contract.
                # As you noted, the contract returns these values in seconds.
                # We convert them to minutes for storage in Firestore.
                voting_delay_seconds = dao_contract.functions.votingDelay().call()
                org.votingDelay = voting_delay_seconds // 60

                voting_period_seconds = dao_contract.functions.votingPeriod().call()
                org.votingDuration = voting_period_seconds // 60

                org.proposalThreshold = str(dao_contract.functions.proposalThreshold().call())
                
                # The OZ Governor contract has a quorumNumerator function we can use.
                org.quorum = dao_contract.functions.quorumNumerator().call()

                print(f"Successfully fetched on-chain settings for DAO {name}.")

            except Exception as e:
                print(f"ERROR: Could not fetch on-chain settings for new DAO {org.address}: {e}. Defaulting to 0.")
                org.votingDelay = 0
                org.votingDuration = 0
                org.proposalThreshold = "0"
                org.quorum = 0

        else:
            print(f"WARNING: Could not create contract instance for new DAO {org.address}. Settings will be defaulted to 0.")
            org.votingDelay = 0
            org.votingDuration = 0
            org.proposalThreshold = "0"
            org.quorum = 0
        
        # executionDelay is correct as it's a dedicated field in the event.
        org.executionDelay = args['executionDelay']
        # --- END OF FIX ---

        # In Homebase EVM DAOs the Registry contract IS the treasury (it holds
        # funds, with spend caps; the OZ timelock is only the execution-delay
        # mechanism). The app reads registryAddress for all treasury UI; keep
        # treasuryAddress consistent with it.
        org.treasuryAddress = org.registryAddress

        # One read: a replay finds the DAO indexed already (its later events are in its documents, so
        # rewriting them would undo those), and SuiteConfigured may have recorded the economy first
        doc_ref = self.daos_collection.document(org.address)
        existing_doc = doc_ref.get()
        existing_data = (existing_doc.to_dict() or {}) if existing_doc.exists else {}
        if existing_data.get('token'):
            print(f"DAO {org.address} already indexed; not rewriting it")
            for pending in ('_pending_economy_addresses', '_pending_descriptions'):
                getattr(self, pending, {}).pop(org.address, None)
            return [org.address, org.govTokenAddress, org.registryAddress]

        supply = 0
        batch = self.db.batch()
        for i in range(len(members)):
            member_address_checksum = Web3.to_checksum_address(members[i])
            member_balance = amounts[i]
            supply += member_balance
            m = Member(address=member_address_checksum, personalBalance=str(member_balance), delegate="", votingWeight="0")
            member_doc_ref = self.daos_collection.document(org.address).collection('members').document(m.address)
            batch.set(member_doc_ref, m.toJson())
            self._remember_member(m.address, dao=org.address)

        org.totalSupply = str(supply)

        keys = args['keys']
        values = args['values']
        if keys and values and len(keys) == len(values):
            org.registry = {keys[i]: values[i] for i in range(len(keys)) if keys[i] and values[i]}
        else:
            org.registry = {}
        
        # Check if there's a pending economy address from SuiteConfigured event
        # (SuiteConfigured may arrive before NewDaoCreated in the same transaction)
        if hasattr(self, '_pending_economy_addresses') and org.address in self._pending_economy_addresses:
            org.economy = self._pending_economy_addresses[org.address]
            del self._pending_economy_addresses[org.address]
            print(f"Applied pending economy address {org.economy} to DAO {org.address}")
        elif existing_data.get('economy'):
            # SuiteConfigured recorded it on the document first
            org.economy = existing_data['economy']
            print(f"Preserved existing economy address {org.economy} for DAO {org.address}")

        # Check if there's a pending description from SuiteConfigured event
        # (For Economy DAOs, description is fetched from Registry during SuiteConfigured)
        if hasattr(self, '_pending_descriptions') and org.address in self._pending_descriptions:
            org.description = self._pending_descriptions[org.address]
            del self._pending_descriptions[org.address]
            print(f"Applied pending description to DAO {org.address}: {org.description[:50]}...")

        dao_json = org.toJson()
        if org.economy:
            dao_json['economy'] = str(org.economy)
        # The supermajority threshold, in the same write
        bps = self._supermajority(org.address)
        if bps is not None:
            dao_json['supermajorityBps'] = bps

        # The DAO and its initial members in one commit
        batch.set(doc_ref, dao_json)
        try:
            batch.commit()
            print(f"Successfully added DAO {org.name} / {org.address} to Firestore.")
        except Exception as e:
            print(f"Error committing batch for DAO {org.name}: {e}")

        self._activity(log, 'daoCreated', f"Created {org.name}", dao=org.address)
        return [org.address, org.govTokenAddress, org.registryAddress]


    def add_dao_wrapped(self, log):
        contract_instance = self.get_contract()
        if not contract_instance:
            print(f"Could not get contract instance for {self.address} in add_dao_wrapped")
            return None
        try:
            decoded_event = contract_instance.events.DaoWrappedDeploymentInfo().process_log(log)
        except Exception as e:
            print(f"Error processing DaoWrappedDeploymentInfo log with ABI for {self.address}: {e}")
            return None

        args = decoded_event['args']
        dao_name = args['daoName']
        print(f"New DAO (wrapped wrapper): {dao_name} from event")
        # A replay finds it indexed already, with its later events in it: don't rewrite it
        dao_address = args['daoAddress']
        existing = self.daos_collection.document(dao_address).get()
        if existing.exists and (existing.to_dict() or {}).get('token'):
            print(f"DAO {dao_address} already indexed; not rewriting it")
            return [dao_address, args['wrappedTokenAddress'], args['registryAddress']]
        org = Org(name=dao_name)
        org.creationDate = self._block_time(log)
        org.govTokenAddress = args['wrappedTokenAddress']
        org.address = args['daoAddress']
        org.symbol = args['wrappedTokenSymbol']
        org.registryAddress = args['registryAddress']
        org.description = args['description']
        org.quorum = args['quorumFraction']

        # Set underlyingToken from event (new StandardFactoryWrapped includes it)
        if 'underlyingTokenAddress' in args:
            org.underlyingToken = str(args['underlyingTokenAddress'])
            print(f"Underlying token from event: {org.underlyingToken}")

        # Get governance settings from event (new StandardFactoryWrapped includes these)
        if 'executionDelay' in args:
            org.executionDelay = args['executionDelay']
        if 'votingDelay' in args:
            # Event provides values in seconds, convert to minutes for DB
            org.votingDelay = args['votingDelay'] // 60
        if 'votingPeriod' in args:
            # Event provides values in seconds, convert to minutes for DB
            org.votingDuration = args['votingPeriod'] // 60
        if 'proposalThreshold' in args:
            org.proposalThreshold = str(args['proposalThreshold'])

        org.holders = 0

        wrapped_token_contract = self.get_specific_contract(org.govTokenAddress, wrapper_token_abi)

        if wrapped_token_contract:
            try:
                org.decimals = wrapped_token_contract.functions.decimals().call()
                org.totalSupply = str(wrapped_token_contract.functions.totalSupply().call())
                # Also fetch from contract as backup (sets both fields for web app)
                underlying_from_contract = str(wrapped_token_contract.functions.underlying().call())
                if not org.underlyingToken:
                    org.underlyingToken = underlying_from_contract
                print(f"Wrapped token info: decimals={org.decimals}, totalSupply={org.totalSupply}, underlying={org.underlyingToken}")
            except Exception as e:
                print(f"Error fetching info for wrapped token {org.govTokenAddress}: {e}")
                if not hasattr(org, 'decimals') or org.decimals is None:
                    org.decimals = 18
                if not hasattr(org, 'totalSupply') or org.totalSupply is None:
                    org.totalSupply = "0"

            # Fetch transferability status from wrapped token contract
            try:
                is_transferable = wrapped_token_contract.functions.isTransferable().call()
                # Note: nonTransferrable is the OPPOSITE of isTransferable
                org.nonTransferrable = not is_transferable
                print(f"Wrapped token {org.govTokenAddress} isTransferable: {is_transferable}, nonTransferrable: {org.nonTransferrable}")
            except Exception as e:
                print(f"Error fetching isTransferable for wrapped token {org.govTokenAddress}: {e}")
                org.nonTransferrable = False  # Default to transferable if we can't read it
        else:
            print(f"Could not create contract instance for wrapped token {org.govTokenAddress}")
            if not hasattr(org, 'decimals') or org.decimals is None:
                org.decimals = 18
            if not hasattr(org, 'totalSupply') or org.totalSupply is None:
                org.totalSupply = "0"
            org.nonTransferrable = False

        # Fallback: If governance settings weren't in the event, query contracts
        if not hasattr(org, 'proposalThreshold') or org.proposalThreshold is None:
            dao_contract = self.get_specific_contract(org.address, daoAbiGlobal)
            if dao_contract:
                try:
                    org.proposalThreshold = str(dao_contract.functions.proposalThreshold().call())
                    org.votingDelay = dao_contract.functions.votingDelay().call() // 60
                    org.votingDuration = dao_contract.functions.votingPeriod().call() // 60

                    timelock_address = dao_contract.functions.timelock().call()
                    timelock_contract = self.get_specific_contract(timelock_address, [timelock_min_delay_abi])
                    if timelock_contract:
                        org.executionDelay = timelock_contract.functions.getMinDelay().call()
                    print(f"Fetched governance settings from contracts as fallback")
                except Exception as e:
                    print(f"Error fetching DAO/Timelock settings for {org.address}: {e}")
                    org.proposalThreshold = "0"
                    org.votingDelay = 0
                    org.votingDuration = 0
                    org.executionDelay = 0
            else:
                print(f"Could not create DAO contract instance, using defaults")
                org.proposalThreshold = "0"
                org.votingDelay = 0
                org.votingDuration = 0
                org.executionDelay = 0

        # The Registry contract is the treasury (see add_dao note).
        org.treasuryAddress = org.registryAddress

        # Ensure all required fields have defaults
        if not hasattr(org, 'proposalThreshold') or org.proposalThreshold is None:
            org.proposalThreshold = "0"
        if not hasattr(org, 'votingDelay') or org.votingDelay is None:
            org.votingDelay = 0
        if not hasattr(org, 'votingDuration') or org.votingDuration is None:
            org.votingDuration = 0
        if not hasattr(org, 'executionDelay') or org.executionDelay is None:
            org.executionDelay = 0

        org.registry = {}

        dao_json = org.toJson()
        # The supermajority threshold (governors that have one), in the same write
        bps = self._supermajority(org.address)
        if bps is not None:
            dao_json['supermajorityBps'] = bps
        self.daos_collection.document(org.address).set(dao_json)
        print(f"Successfully added DAO (wrapped) {org.name} / {org.address} to Firestore.")
        self._activity(log, 'daoCreated', f"Created {org.name}", dao=org.address)
        return [org.address, org.govTokenAddress, org.registryAddress]


    def configure_economy(self, log):
        """
        Handle SuiteConfigured event from TrustlessFactory.
        Updates the DAO document with the economy contract address; the trustless app indexes
        the Economy's projects under that DAO.

        Event: SuiteConfigured(deployer, economy, registry, timelock, repToken, dao)

        Note: SuiteConfigured may arrive before NewDaoCreated in the same transaction
        since events are processed in log order. We store pending economy addresses
        to be applied when the DAO is created.
        """
        contract_instance = self.get_contract()
        if not contract_instance:
            print(f"Could not get contract instance for {self.address} in configure_economy")
            return None

        try:
            decoded_event = contract_instance.events.SuiteConfigured().process_log(log)
        except Exception as e:
            print(f"Error processing SuiteConfigured log for {self.address}: {e}")
            return None

        args = decoded_event['args']
        dao_address = Web3.to_checksum_address(args['dao'])
        economy_address = Web3.to_checksum_address(args['economy'])
        registry_address = Web3.to_checksum_address(args['registry'])

        print(f"SuiteConfigured: DAO {dao_address} linked to Economy {economy_address}")

        # Query the registry contract for the description (set during deployment)
        # This is more reliable than trying to catch the RegistryUpdated event from the same tx
        description = None
        try:
            registry_contract = self.get_specific_contract(registry_address, registryAbi)
            if registry_contract:
                description = registry_contract.functions.getRegistryValue("description").call()
                if description:
                    print(f"Fetched description from Registry {registry_address}: {description[:50]}...")
                else:
                    print(f"No description found in Registry {registry_address}")
        except Exception as e:
            print(f"Error fetching description from Registry {registry_address}: {e}")

        # Update the DAO document with economy address (Homebase Firestore)
        try:
            dao_doc_ref = self.daos_collection.document(dao_address)
            dao_doc = dao_doc_ref.get()

            if dao_doc.exists:
                update_data = {'economy': economy_address}
                if description:
                    update_data['description'] = description
                    update_data['registry.description'] = description
                dao_doc_ref.update(update_data)
                print(f"Updated DAO {dao_address} with economy address {economy_address}")
            else:
                # DAO doesn't exist yet - store pending economy address and description
                # This happens when SuiteConfigured arrives before NewDaoCreated
                if not hasattr(self, '_pending_economy_addresses'):
                    self._pending_economy_addresses = {}
                if not hasattr(self, '_pending_descriptions'):
                    self._pending_descriptions = {}
                self._pending_economy_addresses[dao_address] = economy_address
                if description:
                    self._pending_descriptions[dao_address] = description
                print(f"Stored pending economy address for DAO {dao_address}: {economy_address}")
        except Exception as e:
            print(f"Error updating DAO {dao_address} with economy address: {e}")

        # Return economy and registry addresses with marker so main loop can add them to listener
        # Registry paper is needed to capture RegistryUpdated events for description updates
        return [('economy', economy_address, dao_address), ('registry', registry_address, dao_address)]


    def delegate(self, log):
        if not self.dao:
            print(f"DAO address not set for token {self.address}, cannot process delegate event.")
            return None
            
        # Handle both raw logs (from real-time) and processed events (from get_logs)
        if 'args' in log:
            # Already processed event from get_logs()
            data = log
        else:
            # Raw log from real-time listener, needs processing
            contract_instance = self.get_contract()
            if not contract_instance: return None
            try:
                data = contract_instance.events.DelegateChanged().process_log(log)
            except Exception as e:
                print(f"Error processing DelegateChanged for {self.address} in DAO {self.dao}: {e}")
                return None

        args = data['args']
        delegator = Web3.to_checksum_address(args['delegator'])
        from_delegate = Web3.to_checksum_address(args['fromDelegate'])
        to_delegate = Web3.to_checksum_address(args['toDelegate'])
        
        batch = self.db.batch()
        delegator_member_ref = self.daos_collection.document(self.dao).collection('members').document(delegator)
        
        if not self._member_exists(delegator):
            print(f"Delegator {delegator} not found as member in DAO {self.dao}. Creating.")
            try:
                token_contract_instance = self.get_contract()
                balance = token_contract_instance.functions.balanceOf(delegator).call()
                new_member = Member(address=delegator, personalBalance=str(balance), delegate=to_delegate, votingWeight="0")
                batch.set(delegator_member_ref, new_member.toJson())
            except Exception as e:
                print(f"Error creating new member {delegator} for delegation: {e}")
                new_member = Member(address=delegator, personalBalance="0", delegate=to_delegate, votingWeight="0")
                batch.set(delegator_member_ref, new_member.toJson())

        else:
            batch.set(delegator_member_ref, {"delegate": to_delegate}, merge=True)

        if to_delegate != self.ZERO_ADDRESS and to_delegate != delegator:
            to_delegate_member_ref = self.daos_collection.document(self.dao).collection('members').document(to_delegate)
            if not self._member_exists(to_delegate):
                print(f"Delegatee {to_delegate} not found as member in DAO {self.dao}. Creating.")
                try:
                    token_contract_instance = self.get_contract()
                    balance = token_contract_instance.functions.balanceOf(to_delegate).call()
                    new_delegatee_member = Member(address=to_delegate, personalBalance=str(balance), delegate="", votingWeight="0")
                    batch.set(to_delegate_member_ref, new_delegatee_member.toJson())
                except Exception as e:
                    print(f"Error creating new delegatee member {to_delegate}: {e}")
                    new_delegatee_member = Member(address=to_delegate, personalBalance="0", delegate="", votingWeight="0")
                    batch.set(to_delegate_member_ref, new_delegatee_member.toJson())

            batch.set(to_delegate_member_ref, {
                "constituents": firestore.ArrayUnion([delegator])
            }, merge=True)

        if from_delegate != self.ZERO_ADDRESS and from_delegate != delegator and from_delegate != to_delegate:
            from_delegate_member_ref = self.daos_collection.document(self.dao).collection('members').document(from_delegate)
            batch.set(from_delegate_member_ref, {
                "constituents": firestore.ArrayRemove([delegator])
            }, merge=True)
        
        try:
            batch.commit()
            self._remember_member(delegator)
            if to_delegate != self.ZERO_ADDRESS:
                self._remember_member(to_delegate)
        except Exception as e:
            print(f"Error committing batch for delegation in DAO {self.dao}: {e}")
        summary = "Delegated to themselves" if to_delegate == delegator else (
            "Removed their delegation" if to_delegate == self.ZERO_ADDRESS else f"Delegated to {short(to_delegate)}")
        self._activity(log, 'delegate', summary, actor=delegator)
        return None

    def propose(self, log):
        if not self.dao:
            print(f"DAO address not set for contract {self.address}, cannot process propose event.")
            return None

        # Handle both raw logs (from real-time) and processed events (from get_logs)
        if 'args' in log:
            # Already processed event from get_logs()
            event = log
        else:
            # Raw log from real-time listener, needs processing
            contract_instance = self.get_contract()
            if not contract_instance:
                return None
            try:
                event = contract_instance.events.ProposalCreated().process_log(log)
            except Exception as e:
                print(f"Error processing ProposalCreated for {self.address} in DAO {self.dao}: {e}")
                return None

        proposal_id_raw = event["args"]["proposalId"]
        proposal_id = str(proposal_id_raw)

        print(f"Processing new proposal {proposal_id} for DAO {self.dao}")

        proposer = Web3.to_checksum_address(event["args"]["proposer"])
        targets = [Web3.to_checksum_address(t) for t in event["args"]["targets"]]
        values = [str(v) for v in event["args"]["values"]]
        calldatas_raw = event["args"]["calldatas"]
        calldatas = [cd.hex() if isinstance(cd, bytes) else str(cd) for cd in calldatas_raw]

        vote_start_block = event["args"]["voteStart"]
        vote_end_block = event["args"]["voteEnd"]
        description_full = event["args"]["description"]
        
        parts = description_full.split("0|||0")
        if len(parts) >= 4:
            name = parts[0] if parts[0] else "(No Title Provided)"
            type_ = parts[1] if parts[1] else "unknown"
            desc = parts[2] if parts[2] else description_full
            link = parts[3] if parts[3] else "(No Link Provided)"
        elif len(parts) == 1 and description_full:
            name = description_full[:80]
            type_ = "custom"
            desc = description_full
            link = "(No Link Provided)"
        else:
            name = "(No Title Provided)"
            type_ = "unknown"
            desc = description_full if description_full else "(No Description Provided)"
            link = "(No Link Provided)"

        # A daemon's epoch anchor: one call to Substrate.anchor, described in plain text
        anchor = _decode_atn_epoch(calldatas_raw)
        if anchor is not None:
            type_ = "atn epoch"
            name = f"ATN epoch {anchor['epochId']}"
            desc = (f"Anchors Autonet epoch {anchor['epochId']} on the Substrate.\n"
                    f"Epoch root: {anchor['epochRoot']}\n"
                    f"Previous epoch root: {anchor['prevEpochRoot']}\n"
                    f"Previous anchor: {anchor['prevAnchorHash']}\n"
                    f"Agent mints: {anchor['agentMintCid'] or '(none)'} (root {anchor['agentMintRoot']})\n"
                    f"Payload hash: {anchor['payloadHash']}")

        # An epoch close: the governor counts it as a single-choice election when the target is an
        # approved option source, and votes are option indexes (tallied in optionVotes)
        option_source = None
        election = _decode_anchor_election(calldatas_raw)
        if election is not None:
            try:
                source = self.get_specific_contract(self.address, OPTION_SOURCE_ABI).functions.optionSourceOf(proposal_id_raw).call()
                if int(source, 16) != 0:
                    option_source = Web3.to_checksum_address(source)
            except Exception as e:
                print(f"Could not read the option source of proposal {proposal_id}: {e}")
            type_ = "atn epoch"
            name = f"ATN epoch {election[0]} close"
            desc = (f"Elects the anchor of Autonet epoch {election[0]} (attempt {election[1]}) among the options "
                    f"registered by drawn agents. The winning option is anchored on the Substrate when this executes.")

        # werule names these "appeal"; one made elsewhere gets the type, and a title if it has none
        appeal = _decode_appeal(targets, calldatas_raw)
        if appeal is not None:
            type_ = "appeal"
            if len(parts) < 4:
                name = f"Appeal: project {appeal['project']}"
                desc = (f"Overrules the arbitration of project {appeal['project']}: {appeal['percent']}% of the "
                        f"funds to the contractor.\n\n{appeal['ruling']}")

        # Likewise vetoes, typed "veto"
        veto_project = _decode_veto(targets, calldatas_raw)
        if veto_project is not None:
            type_ = "veto"
            if len(parts) < 4:
                name = f"Veto: project {veto_project}"
                desc = (f"Closes project {veto_project}; every backer gets their contribution back.\n\n"
                        f"{description_full}")

        p = Proposal(name=name, org=self.dao)
        p.author = proposer
        p.id = proposal_id
        p.type = type_
        # The exact description, so queue and execute can rebuild the proposal id
        p.rawDescription = description_full
        p.targets = targets
        p.values = values
        p.description = desc
        p.callDatas = calldatas
        p.createdAt = self._block_time(log)
        p.votingStartsBlock = str(vote_start_block)
        p.votingEndsBlock = str(vote_end_block)
        p.externalResource = link
        
        p.totalSupply = "0"
        if self.token_paper and self.token_paper.address:
            token_contract_for_dao = self.get_specific_contract(self.token_paper.address, tokenAbiGlobal)
            if token_contract_for_dao:
                try:
                    p.totalSupply = str(token_contract_for_dao.functions.getPastTotalSupply(vote_start_block).call())
                except Exception as e:
                    print(f"Warning: Could not fetch past total supply for proposal {proposal_id} from token {self.token_paper.address} at block {vote_start_block}. Error: {e}. Attempting to use current total supply.")
                    try:
                        p.totalSupply = str(token_contract_for_dao.functions.totalSupply().call())
                    except Exception as e2:
                        print(f"Error fetching current total supply for proposal {proposal_id}. Error: {e2}. Defaulting to '0'.")
        else:
            print(f"Warning: Token paper or token address not set for DAO {self.dao}. Proposal {proposal_id} will have totalSupply of '0'.")

        proposal_doc_ref = self.daos_collection.document(self.dao).collection('proposals').document(proposal_id)
        doc = p.toJson()
        if election is not None:
            doc.update({'epochNumber': int(election[0]), 'attempt': int(election[1])})
        if option_source:
            doc.update({'optionSource': option_source, 'optionVotes': {}, 'votesCast': 0})
        # A constitutional proposal: the share of For+Against it needs, fixed when it was proposed
        supermajority = self._supermajority(self.address, proposal_id_raw)
        if supermajority:
            doc['supermajorityBps'] = supermajority
        if appeal is not None:
            doc.update({'appealProject': appeal['project'].lower(), 'appealPercent': appeal['percent']})
        if veto_project is not None:
            doc.update({'vetoProject': veto_project.lower()})
        try:
            # create(), not set(): on a replay the proposal already holds its votes and later stages
            try:
                proposal_doc_ref.create(doc)
            except AlreadyExists:
                print(f"Proposal {proposal_id} already indexed; keeping it")
                self._remember_title(proposal_id, doc.get('title'))
                return None
            self._remember_title(proposal_id, doc.get('title'))

            member_doc_ref = self.daos_collection.document(self.dao).collection('members').document(proposer)
            if self._member_exists(proposer):
                 member_doc_ref.update({"proposalsCreated": firestore.ArrayUnion([proposal_id])})
            else:
                print(f"Proposer {proposer} not found. Creating member entry.")
                balance = "0"
                if self.token_paper and self.token_paper.address:
                    token_contract_for_dao = self.get_specific_contract(self.token_paper.address, tokenAbiGlobal)
                    if token_contract_for_dao:
                        try:
                            balance = str(token_contract_for_dao.functions.balanceOf(proposer).call())
                        except:
                            pass
                new_member = Member(address=proposer, personalBalance=balance, delegate="", votingWeight="0")
                new_member.proposalsCreated = [proposal_id]
                member_doc_ref.set(new_member.toJson())
                self._remember_member(proposer)
            
            print(f"Successfully saved proposal {proposal_id} to DAO {self.dao}")
            self._activity(log, 'proposalCreated', p.name, actor=proposer, target_kind='proposal',
                           target_id=proposal_id, target_label=p.name, extra={'proposalType': type_})

        except Exception as e:
            print(f"Error saving proposal {proposal_id} or updating member {proposer} in DAO {self.dao}: {e}")


    def vote(self, log):
        if not self.dao:
            print(f"DAO address not set for contract {self.address}, cannot process vote event.")
            return None
        # Handle both raw logs (from real-time) and processed events (from get_logs)
        if 'args' in log:
            # Already processed event from get_logs()
            event = log
        else:
            # Raw log from real-time listener, needs processing
            contract_instance = self.get_contract()
            if not contract_instance: return None
            try:
                event = contract_instance.events.VoteCast().process_log(log)
            except Exception as e:
                print(f"Error processing VoteCast for {self.address} in DAO {self.dao}: {e}")
                return None

        proposal_id = str(event["args"]["proposalId"])
        tx_hash_bytes = event['transactionHash']
        tx_hash_hex = tx_hash_bytes.hex()


        voter = Web3.to_checksum_address(event["args"]["voter"])
        support = event["args"]["support"]
        weight = event["args"]["weight"]
        reason = event["args"]["reason"]
        
        vote_obj = Vote(proposalID=proposal_id, votingPower=str(weight), option=support, voter=voter)
        vote_obj.reason = reason
        vote_obj.hash = tx_hash_hex
        
        proposal_doc_ref = self.daos_collection.document(self.dao).collection('proposals').document(proposal_id)
        vote_doc_ref = proposal_doc_ref.collection("votes").document(voter)
        member_doc_ref = self.daos_collection.document(self.dao).collection('members').document(voter)
        member_known = self._member_key(voter) in self._known_members

        def new_member_json():
            balance = "0"
            if self.token_paper and self.token_paper.address:
                token_contract_for_dao = self.get_specific_contract(self.token_paper.address, tokenAbiGlobal)
                if token_contract_for_dao:
                    try:
                        balance = str(token_contract_for_dao.functions.balanceOf(voter).call())
                    except: pass
            new_member = Member(address=voter, personalBalance=balance, delegate="", votingWeight="0")
            new_member.proposalsVoted = [proposal_id]
            return new_member.toJson()

        # One transaction: the vote doc (a replay skips), the proposal (read once, for the tally, the
        # title and whether it is an election) and the member, then all three writes
        @firestore.transactional
        def update_proposal_votes(transaction, proposal_ref, weight_val, support_val):
            if vote_doc_ref.get(transaction=transaction).exists:
                return 'replay', None
            proposal_snapshot = proposal_ref.get(transaction=transaction)
            member_exists = member_known or member_doc_ref.get(transaction=transaction).exists
            transaction.set(vote_doc_ref, vote_obj.toJson())
            if member_exists:
                transaction.update(member_doc_ref, {"proposalsVoted": firestore.ArrayUnion([proposal_id])})
            else:
                print(f"Voter {voter} not found. Creating member entry for vote.")
                transaction.set(member_doc_ref, new_member_json())
            if not proposal_snapshot.exists:
                print(f"Proposal {proposal_id} not found during vote update transaction.")
                return 'ok', {}

            prop_data = proposal_snapshot.to_dict()
            
            current_in_favor = int(prop_data.get('inFavor', "0"))
            current_against = int(prop_data.get('against', "0"))
            
            current_votes_for = prop_data.get('votesFor', 0)
            current_votes_against = prop_data.get('votesAgainst', 0)

            if prop_data.get('optionSource'):
                tallies = prop_data.get('optionVotes') or {}
                key = str(support_val)
                transaction.update(proposal_ref, {
                    f'optionVotes.{key}': str(int(tallies.get(key, "0")) + weight_val),
                    'votesCast': prop_data.get('votesCast', 0) + 1
                })
            elif support_val == 1:
                new_in_favor = current_in_favor + weight_val
                transaction.update(proposal_ref, {
                    'inFavor': str(new_in_favor),
                    'votesFor': current_votes_for + 1
                })
            elif support_val == 0:
                new_against = current_against + weight_val
                transaction.update(proposal_ref, {
                    'against': str(new_against),
                    'votesAgainst': current_votes_against + 1
                })
            elif support_val == 2:
                # Abstain counts toward the quorum, as For does
                transaction.update(proposal_ref, {
                    'abstain': str(int(prop_data.get('abstain', "0")) + weight_val),
                    'votesAbstain': prop_data.get('votesAbstain', 0) + 1
                })
            return 'ok', prop_data

        try:
            outcome, prop_data = update_proposal_votes(self.db.transaction(), proposal_doc_ref, int(weight), support)
            if outcome == 'replay':
                print(f"Vote by {voter} on proposal {proposal_id} already indexed; skipping replay.")
                return None
            self._remember_member(voter)
            print(f"Vote by {voter} on proposal {proposal_id} processed.")
            self._remember_title(proposal_id, prop_data.get('title'))
            title = self._proposal_title(proposal_id)
            # Elections tally option indexes, 0 being reject (none of these); other proposals 0 against,
            # 1 for, 2 abstain
            if prop_data.get('optionSource'):
                choice = "reject (none of these)" if int(support) == 0 else f"option {support}"
            else:
                choice = {0: 'Against', 1: 'For', 2: 'Abstain'}.get(support, str(support))
            self._activity(log, 'vote', f"Voted {choice} on {title}", actor=voter, target_kind='proposal',
                           target_id=proposal_id, target_label=title)
        except Exception as e:
            print(f"Error during vote processing for proposal {proposal_id} by {voter}: {e}")


    def queue(self, log):
        if not self.dao:
            print(f"DAO address not set for contract {self.address}, cannot process queue event.")
            return None
        # Handle both raw logs (from real-time) and processed events (from get_logs)
        if 'args' in log:
            # Already processed event from get_logs()
            event = log
        else:
            # Raw log from real-time listener, needs processing
            contract_instance = self.get_contract()
            if not contract_instance: return None
            try:
                event = contract_instance.events.ProposalQueued().process_log(log)
            except Exception as e:
                print(f"Error processing ProposalQueued for {self.address} in DAO {self.dao}: {e}")
                return None

        proposal_id = str(event['args']['proposalId'])
        
        proposal_doc_ref = self.daos_collection.document(self.dao).collection('proposals').document(proposal_id)
        
        try:
            proposal_doc_ref.update({
                "statusHistory.queued": self._block_time(log),
                "latestStage": "Queued",
                
            })
            print(f"Proposal {proposal_id} queued in DAO {self.dao}.")
            title = self._proposal_title(proposal_id)
            self._activity(log, 'proposalQueued', f"Queued {title}", target_kind='proposal',
                           target_id=proposal_id, target_label=title)
        except Exception as e:
            print(f"Error updating proposal {proposal_id} on queue event in DAO {self.dao}: {e}")


    def bytes_to_int(self, byte_array):
        return int.from_bytes(byte_array, byteorder='big')

    def decode_params(self, data_bytes_hex):
        if not isinstance(data_bytes_hex, str):
            print(f"decode_params expects a hex string, got {data_bytes_hex}")
            return None, None
        
        if data_bytes_hex.startswith("0x"):
            data_bytes_hex = data_bytes_hex[2:]

        try:
            data_bytes = bytes.fromhex(data_bytes_hex)
        except ValueError as e:
            print(f"Error converting hex to bytes in decode_params: {data_bytes_hex}, error: {e}")
            return None, None

        data_without_selector = data_bytes[4:]
        if len(data_without_selector) < 64:
            print("Not enough data for two offsets in decode_params")
            return None,None

        param1_offset_bytes = data_without_selector[:32]
        param2_offset_bytes = data_without_selector[32:64]
        param1_offset = self.bytes_to_int(param1_offset_bytes)
        param2_offset = self.bytes_to_int(param2_offset_bytes)
        
        if param1_offset + 32 > len(data_without_selector):
             print("Param1 offset out of bounds")
             return None,None
        param1_length_bytes = data_without_selector[param1_offset : param1_offset + 32]
        param1_length = self.bytes_to_int(param1_length_bytes)
        if param1_offset + 32 + param1_length > len(data_without_selector):
            print("Param1 length out of bounds")
            return None, None
        param1_data_bytes = data_without_selector[param1_offset + 32 : param1_offset + 32 + param1_length]
        param1_data = param1_data_bytes.decode('utf-8', errors='replace')

        if param2_offset + 32 > len(data_without_selector):
            print("Param2 offset out of bounds")
            return None,None
        param2_length_bytes = data_without_selector[param2_offset : param2_offset + 32]
        param2_length = self.bytes_to_int(param2_length_bytes)
        if param2_offset + 32 + param2_length > len(data_without_selector):
            print("Param2 length out of bounds")
            return None, None
        param2_data_bytes = data_without_selector[param2_offset + 32 : param2_offset + 32 + param2_length]
        param2_data = param2_data_bytes.decode('utf-8', errors='replace')
        
        return param1_data, param2_data

     
    def execute(self, log):
        if not self.dao:
            print(f"DAO address not set for contract {self.address}, cannot process execute event.")
            return None
        # Handle both raw logs (from real-time) and processed events (from get_logs)
        if 'args' in log:
            # Already processed event from get_logs()
            event = log
        else:
            # Raw log from real-time listener, needs processing
            contract_instance = self.get_contract()
            if not contract_instance: return None
            try:
                event = contract_instance.events.ProposalExecuted().process_log(log)
            except Exception as e:
                print(f"Error processing ProposalExecuted for {self.address} in DAO {self.dao}: {e}")
                return None

        proposal_id = str(event['args']['proposalId'])
        print(f"Executing proposal id: {proposal_id} in DAO {self.dao}")
        
        proposal_doc_ref = self.daos_collection.document(self.dao).collection('proposals').document(proposal_id)
        proposal_snapshot = proposal_doc_ref.get()

        if not proposal_snapshot.exists:
            print(f"Proposal {proposal_id} not found in DB for DAO {self.dao} during execution.")
            return

        prop_data_from_db = proposal_snapshot.to_dict()
        self._remember_title(proposal_id, prop_data_from_db.get('title'))
        
        updates_for_proposal = {
            "statusHistory.executed": self._block_time(log),
            "latestStage": "Executed",
            "executionHash": event['transactionHash'].hex()
        }

        proposal_type = prop_data_from_db.get('type', "").lower()
        proposal_calldatas = prop_data_from_db.get('callDatas', [])
        proposal_targets_db = prop_data_from_db.get('targets', [])

        dao_doc_ref = self.daos_collection.document(self.dao)
        dao_updates = {}

        try:
            if "voting period" in proposal_type and proposal_calldatas:
                calldata_bytes = bytes.fromhex(proposal_calldatas[0])
                decoded = decode_function_parameters(voting_period_function_abi, calldata_bytes)
                if decoded and len(decoded) > 0:
                    new_voting_period_seconds = int(decoded[0])
                    # Convert seconds to minutes
                    dao_updates["votingDuration"] = new_voting_period_seconds // 60
                    print(f"DAO {self.dao} voting period updated to {dao_updates['votingDuration']} minutes")

            if "threshold" in proposal_type and proposal_calldatas:
                calldata_bytes = bytes.fromhex(proposal_calldatas[0])
                decoded = decode_function_parameters(proposal_threshold_function_abi, calldata_bytes)
                if decoded and len(decoded) > 0:
                    new_raw_threshold = int(decoded[0])
                    dao_snapshot = dao_doc_ref.get()
                    if dao_snapshot.exists:
                        current_dao_data = dao_snapshot.to_dict()
                        decimals = int(current_dao_data.get('decimals', 18))
                        new_proposal_threshold_adjusted = str(new_raw_threshold // (10**decimals))
                        dao_updates["proposalThreshold"] = str(new_proposal_threshold_adjusted)
                        print(f"DAO {self.dao} proposal threshold updated to {new_proposal_threshold_adjusted} (adjusted from raw {new_raw_threshold})")
                    else:
                        print(f"Could not fetch DAO data to adjust proposal threshold for DAO {self.dao}")
                        dao_updates["proposalThreshold"] = str(new_raw_threshold)

            if "delay" in proposal_type and proposal_calldatas:
                calldata_bytes = bytes.fromhex(proposal_calldatas[0])
                decoded = decode_function_parameters(voting_delay_function_abi, calldata_bytes)
                if decoded and len(decoded) > 0:
                    new_voting_delay_seconds = int(decoded[0])
                    # Convert seconds to minutes
                    dao_updates["votingDelay"] = new_voting_delay_seconds // 60
                    print(f"DAO {self.dao} voting delay updated to {dao_updates['votingDelay']} minutes")
            
            if "timelock delay" in proposal_type and proposal_calldatas:
                pass


            if "quorum" in proposal_type and proposal_calldatas:
                calldata_bytes = bytes.fromhex(proposal_calldatas[0])
                decoded = decode_function_parameters(quorum_function_abi, calldata_bytes)
                if decoded and len(decoded) > 0:
                    dao_updates["quorum"] = int(decoded[0])
                    print(f"DAO {self.dao} quorum updated to {int(decoded[0])}")

            if proposal_type == "registry" and proposal_calldatas:
                key, value = self.decode_params(proposal_calldatas[0])
                if key is not None and value is not None:
                    # Just this key; keys contain dots, so quote them as one map field
                    dao_updates[FieldPath('registry', key).to_api_repr()] = value
                    print(f"DAO {self.dao} registry updated: {key} -> {value}")
                    # Also update the description field if key is "description"
                    if key == "description":
                        dao_updates["description"] = value
                        print(f"DAO {self.dao} description updated: {value[:50]}...")


            if "mint" in proposal_type.lower() or "burn" in proposal_type.lower() and proposal_calldatas and proposal_targets_db:
                print(f"Processing mint/burn for proposal {proposal_id} in DAO {self.dao}")
                token_address_target = Web3.to_checksum_address(proposal_targets_db[0])
                target_token_contract = self.get_specific_contract(token_address_target, tokenAbiGlobal)

                if target_token_contract:
                    try:
                        calldata_bytes = bytes.fromhex(proposal_calldatas[0])
                        params = decode_function_parameters(function_abi=mint_function_abi, data_bytes=calldata_bytes)
                        if params and len(params) > 0:
                            member_address_affected = Web3.to_checksum_address(params[0])
                            
                            new_balance = target_token_contract.functions.balanceOf(member_address_affected).call()
                            member_doc_ref = self.daos_collection.document(self.dao).collection('members').document(member_address_affected)
                            if self._member_exists(member_address_affected):
                                member_doc_ref.update({"personalBalance": str(new_balance),
                                                       "balanceSort": balance_sort(new_balance)})
                            else:
                                print(f"Member {member_address_affected} not found for mint/burn. Creating.")
                                new_member = Member(address=member_address_affected, personalBalance=str(new_balance), delegate="", votingWeight="0")
                                member_doc_ref.set(new_member.toJson())
                            print(f"Member {member_address_affected} balance updated to {new_balance} after mint/burn.")

                            new_total_supply = target_token_contract.functions.totalSupply().call()
                            dao_updates["totalSupply"] = str(new_total_supply)
                            print(f"DAO {self.dao} total supply updated to {new_total_supply} after mint/burn.")
                    except Exception as e:
                        print(f"Error decoding/processing mint/burn params for proposal {proposal_id}: {e}")
                else:
                    print(f"Could not get contract for target token {token_address_target} in mint/burn.")

            

            if dao_updates:
                dao_doc_ref.update(dao_updates)
            proposal_doc_ref.update(updates_for_proposal)
            print(f"Proposal {proposal_id} execution processed for DAO {self.dao}.")
            title = self._proposal_title(proposal_id)
            self._activity(log, 'proposalExecuted', f"Executed {title}", target_kind='proposal',
                           target_id=proposal_id, target_label=title)

        except Exception as e:
            import traceback
            print(f"Error during proposal execution processing for prop {proposal_id}, DAO {self.dao}: {e}")
            print(traceback.format_exc())



    def registry_updated(self, log):
        """
        Handle RegistryUpdated event from Registry contracts.
        Updates the DAO's description field when key is 'description'.
        
        Event: RegistryUpdated(string key, string value)
        """
        if not self.dao:
            print(f"Registry paper at {self.address} has no DAO address set, cannot process RegistryUpdated")
            return None
            
        contract_instance = self.get_contract()
        if not contract_instance:
            print(f"Could not get contract instance for registry {self.address}")
            return None

        try:
            decoded_event = log if 'args' in log else contract_instance.events.RegistryUpdated().process_log(log)
        except Exception as e:
            print(f"Error processing RegistryUpdated log for {self.address}: {e}")
            return None

        args = decoded_event['args']
        key = args['key']
        value = args['value']
        
        print(f"RegistryUpdated: key='{key}' for DAO {self.dao}")
        shown = value if len(value) <= 60 else value[:57] + '...'
        self._activity(log, 'registryEdit', f"Set {key} to {shown}" if value else f"Cleared {key}")

        # If key is 'description', update the DAO's description field
        if key == 'description':
            try:
                dao_doc_ref = self.daos_collection.document(self.dao)
                dao_doc_ref.update({
                    'description': value,
                    FieldPath('registry', 'description').to_api_repr(): value  # Also update in registry map
                })
                print(f"Updated description for DAO {self.dao}: {value[:50]}...")
            except NotFound:
                print(f"DAO {self.dao} not indexed yet; description comes with NewDaoCreated")
            except Exception as e:
                print(f"Error updating description for DAO {self.dao}: {e}")
        else:
            # For other keys, just update the registry map
            try:
                dao_doc_ref = self.daos_collection.document(self.dao)
                # Keys contain dots (e.g. jurisdiction.parity.0x...); quote them so they stay flat map keys
                dao_doc_ref.update({
                    FieldPath('registry', key).to_api_repr(): value
                })
                print(f"Updated registry key '{key}' for DAO {self.dao}")
            except NotFound:
                # Factory batch edits fire before NewDaoCreated in the same tx; that event carries the keys
                print(f"DAO {self.dao} not indexed yet; registry key '{key}' comes with NewDaoCreated")
            except Exception as e:
                print(f"Error updating registry key '{key}' for DAO {self.dao}: {e}")

            # The economy module, attached at origination or by proposal, or detached ("")
            if key == 'jurisdiction.economy':
                return self.economy_attached(log, value) if value else self.economy_detached()
            # The autonet module: the main loop follows the jurisdiction's Substrate
            if key == 'jurisdiction.autonet.substrate' and Web3.is_address(value):
                return [('substrate', Web3.to_checksum_address(value), self.dao)]

        return None

    def economy_attached(self, log, value):
        """
        jurisdiction.economy set: link the Economy to the DAO doc. Returns [('economy', addr, dao)]
        so the main loop listens to it and indexes its projects under this DAO.
        """
        try:
            economy_address = Web3.to_checksum_address(value)
        except Exception as e:
            print(f"Invalid jurisdiction.economy value '{value}' for DAO {self.dao}: {e}")
            return None
        print(f"jurisdiction.economy: DAO {self.dao} linked to Economy {economy_address}")

        try:
            self.daos_collection.document(self.dao).update({'economy': economy_address})
            print(f"Updated DAO {self.dao} with economy address {economy_address}")
        except NotFound:
            print(f"DAO {self.dao} not indexed yet; cannot link economy {economy_address}")
        except Exception as e:
            print(f"Error updating DAO {self.dao} with economy address: {e}")

        return [('economy', economy_address, self.dao)]

    def economy_detached(self):
        """jurisdiction.economy cleared: unlink the DAO doc. Its projects keep running and stay
        indexed under the DAO."""
        try:
            self.daos_collection.document(self.dao).update({'economy': firestore.DELETE_FIELD})
            print(f"DAO {self.dao} economy detached")
        except NotFound:
            pass
        except Exception as e:
            print(f"Error detaching economy from DAO {self.dao}: {e}")
        return None


    def _member_ref(self, address):
        return self.daos_collection.document(self.dao).collection('members').document(address)

    def _upsert_member(self, address, fields):
        """Merge fields into a member doc, creating a full member record if it doesn't exist yet.
        A member already seen by this process is merged without reading it first."""
        ref = self._member_ref(address)
        if "personalBalance" in fields:
            fields = {**fields, "balanceSort": balance_sort(fields["personalBalance"])}
        if self._member_exists(address):
            ref.set(fields, merge=True)
        else:
            member = Member(address=address, personalBalance=fields.get("personalBalance", "0"),
                            delegate="", votingWeight=fields.get("votingWeight", "0"))
            ref.set({**member.toJson(), **fields})
            self._remember_member(address)

    def transfer(self, log):
        """Token Transfer (incl. mints from REP claims, coin offering, wrapping): refresh both balances."""
        contract = self.get_contract()
        if not contract or not self.dao:
            return None
        try:
            args = (log if 'args' in log else contract.events.Transfer().process_log(log))['args']
        except Exception as e:
            print(f"Error processing Transfer for {self.address} in DAO {self.dao}: {e}")
            return None
        # Balances are read from the token, so replays converge on the true state
        for party in (args['from'], args['to']):
            if party == self.ZERO_ADDRESS:
                continue
            party = Web3.to_checksum_address(party)
            balance = str(contract.functions.balanceOf(party).call())
            self._upsert_member(party, {"personalBalance": balance})
        if args['from'] == self.ZERO_ADDRESS or args['to'] == self.ZERO_ADDRESS:
            self.daos_collection.document(self.dao).set(
                {"totalSupply": str(contract.functions.totalSupply().call())}, merge=True)
        decimals, symbol = self._token_units()
        amount = f"{format_units(args['value'], decimals)} {symbol}".strip()
        if args['from'] == self.ZERO_ADDRESS:
            self._activity(log, 'mint', f"Minted {amount} to {short(args['to'])}")
        elif args['to'] == self.ZERO_ADDRESS:
            self._activity(log, 'burn', f"Burned {amount} from {short(args['from'])}", actor=args['from'])
        else:
            self._activity(log, 'transfer', f"Sent {amount} to {short(args['to'])}", actor=args['from'])
        return None

    def delegate_votes_changed(self, log):
        contract = self.get_contract()
        if not contract or not self.dao:
            return None
        try:
            args = (log if 'args' in log else contract.events.DelegateVotesChanged().process_log(log))['args']
        except Exception as e:
            print(f"Error processing DelegateVotesChanged for {self.address} in DAO {self.dao}: {e}")
            return None
        delegate = Web3.to_checksum_address(args['delegate'])
        self._upsert_member(delegate, {"votingWeight": str(contract.functions.getVotes(delegate).call())})
        return None

    def cancel(self, log):
        contract = self.get_contract()
        if not contract or not self.dao:
            return None
        try:
            proposal_id = str((log if 'args' in log else contract.events.ProposalCanceled().process_log(log))['args']['proposalId'])
        except Exception as e:
            print(f"Error processing ProposalCanceled for {self.address} in DAO {self.dao}: {e}")
            return None
        try:
            self.daos_collection.document(self.dao).collection('proposals').document(proposal_id).update({
                "statusHistory.canceled": self._block_time(log),
                "latestStage": "Canceled",
            })
            title = self._proposal_title(proposal_id)
            self._activity(log, 'proposalCanceled', f"Canceled {title}", target_kind='proposal',
                           target_id=proposal_id, target_label=title)
        except Exception as e:
            print(f"Error updating proposal {proposal_id} on cancel in DAO {self.dao}: {e}")
        return None

    # ---- Debates (DebatesFactory and each Debate) ----

    def debate_created(self, log):
        """DebatesFactory.DebateCreated: record the debate under the DAO whose token it is for, and
        return it so the main loop follows its arguments."""
        topics = log['topics']
        debate = Web3.to_checksum_address('0x' + bytes(topics[1])[-20:].hex())
        token = Web3.to_checksum_address('0x' + bytes(topics[2])[-20:].hex())
        creator = Web3.to_checksum_address('0x' + bytes(topics[3])[-20:].hex())
        from eth_abi import decode
        title, _ = decode(['string', 'uint256'], bytes(log['data']))
        dao = None
        for candidate in (token, token.lower()):
            for doc in self.daos_collection.where('token', '==', candidate).limit(1).stream():
                dao = doc.id
            if dao:
                break
        if dao is None:
            print(f"DebateCreated for token {token}, which no indexed DAO has; skipping")
            return None
        self.daos_collection.document(dao).collection('debates').document(debate.lower()).set({
            'address': debate, 'title': title, 'creator': creator, 'token': token,
            'createdAt': self._block_time(log),
        })
        self._activity(log, 'debateCreated', f"Opened the debate {title}", dao=dao, actor=creator,
                       target_kind='debate', target_id=debate, target_label=title)
        return [('debate', debate, dao)]

    def _debate_title(self):
        if not hasattr(self, '_title'):
            snap = self.daos_collection.document(self.dao).collection('debates').document(self.address.lower()).get()
            self._title = (snap.to_dict() or {}).get('title', '') if snap.exists else ''
        return self._title

    def debate_event(self, log, func):
        from eth_abi import decode
        topics, data = log['topics'], bytes(log['data'])
        title = self._debate_title()
        target = dict(target_kind='debate', target_id=self.address, target_label=title)
        if func == 'ArgumentAdded':
            author = Web3.to_checksum_address('0x' + bytes(topics[3])[-20:].hex())
            arg_type, _, content = decode(['uint8', 'uint256', 'string'], data)
            shown = content if len(content) <= 80 else content[:77] + '...'
            self._activity(log, 'debateArgument', f"Argued {'con' if arg_type == 1 else 'pro'}: {shown}", actor=author, **target)
        elif func == 'WeightAdded':
            argument = int.from_bytes(bytes(topics[1]), 'big')
            contributor = Web3.to_checksum_address('0x' + bytes(topics[2])[-20:].hex())
            self._activity(log, 'debateWeight', f"Backed argument #{argument}", actor=contributor, **target)
        elif func == 'DebateClosed':
            self._activity(log, 'debateClosed', "Closed the debate", **target)
        return None

    def handle_event(self, log, func=None):
        # Debug logging
        print(f"[DEBUG handle_event] kind={self.kind}, func={func}, address={self.address}")

        if self.kind in ("wrapper", "jurisdiction"):
            if func == "NewDaoCreated":
                return self.add_dao(log)
        elif self.kind == "wrapper_t":
            if func == "NewDaoCreated":
                # Transferable non-wrapped tokens
                return self.add_dao(log)
        elif self.kind == "wrapper_trustless":
            if func == "NewDaoCreated":
                return self.add_dao(log)
            elif func == "SuiteConfigured":
                return self.configure_economy(log)
        elif self.kind == "wrapper_w":
            print(f"[DEBUG wrapper_w] Event detected: {func}")
            if func == "NewDaoCreated":
                # For backward compatibility with old wrapper_w that used NewDaoCreated
                return self.add_dao(log)
            elif func == "DaoWrappedDeploymentInfo":
                print(f"[DEBUG wrapper_w] Calling add_dao_wrapped()")
                # New StandardFactoryWrapped uses DaoWrappedDeploymentInfo for wrapped ERC20 tokens
                return self.add_dao_wrapped(log)
        elif self.kind == "token":
            if func == "DelegateChanged":
                self.delegate(log)
            elif func == "Transfer":
                self.transfer(log)
            elif func == "DelegateVotesChanged":
                self.delegate_votes_changed(log)
        elif self.kind == "dao":
            if func == "ProposalCreated":
                self.propose(log)
            elif func == "VoteCast":
                self.vote(log)
            elif func == "ProposalQueued":
                self.queue(log)
            elif func == "ProposalExecuted":
                self.execute(log)
            elif func == "ProposalCanceled":
                self.cancel(log)
            elif func == "SupermajoritySet":
                self.supermajority_set(log)
        elif self.kind == "registry":
            if func == "RegistryUpdated":
                return self.registry_updated(log)
        elif self.kind == "debates_factory":
            if func == "DebateCreated":
                return self.debate_created(log)
        elif self.kind == "debate":
            if func in ("ArgumentAdded", "WeightAdded", "DebateClosed"):
                return self.debate_event(log, func)
        return None
# apps/homebase/paper.py