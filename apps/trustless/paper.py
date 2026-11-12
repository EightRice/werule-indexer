# apps/trustless/paper.py

import json
import traceback
from datetime import datetime, timezone
from web3 import Web3

from apps.trustless.abis import economyAbi, projectAbi, projectReadAbi
from apps.trustless.entities import Project, Transaction, Stage, ZERO_ADDRESS
from activity import record_activity
from docstore import AlreadyExists

# How a project event reads in the DAO's activity feed, before the project's name
FEED_PHRASES = {
    'createProject': 'Created the project', 'setParties': 'Named the parties of',
    'sendFunds': 'Funded', 'sendFundsWithImmediate': 'Funded', 'immediateFundsReleased': 'Released funds at signing on',
    'signContract': 'Signed', 'voteToRelease': 'Voted to release', 'voteToDispute': 'Voted to dispute',
    'projectDisputed': 'Disputed', 'projectClosed': 'Closed', 'arbitrate': 'Ruled on',
    'finalizeArbitration': 'Finalized the ruling on', 'daoOverruled': 'Overruled the arbiter of',
    'withdrawAsContractor': 'Was paid for', 'withdrawAsContributor': 'Withdrew their backing from',
    'authorPaid': 'Paid the author of', 'vetoedByDao': 'Vetoed', 'reclaimArbitrationStake': 'Reclaimed the stake on',
    'sweepOrphanedTokens': 'Swept stray tokens from',
}

# Governor ProposalState values (OpenZeppelin)
PENDING, ACTIVE, CANCELED, DEFEATED, SUCCEEDED, QUEUED, EXPIRED, EXECUTED = range(8)
# The DAO proposals a project doc links while they may still pass: kind -> field
DAO_LINK_FIELDS = {'appeal': 'appealProposalId', 'veto': 'vetoProposalId'}
# DaoProposalHold.GRACE: a passed proposal holds this long after its deadline (or eta, once queued)
HOLD_GRACE = 86400
GOVERNOR_STATE_ABI = json.loads('''[
  {"inputs":[{"name":"proposalId","type":"uint256"}],"name":"state","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"proposalId","type":"uint256"}],"name":"proposalDeadline","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"proposalId","type":"uint256"}],"name":"proposalEta","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}
]''')


class Paper:
    """
    Paper object handles events for a specific contract address.

    Kinds:
    - 'economy': Handles NewProject events from Economy contract
    - 'project': Handles all project lifecycle events from NativeProject/ERC20Project

    Writes to the homebase Firestore, under the DAO the Economy belongs to:
    {daos_collection}/{dao}/projects/{project}, with backers/ and activity/ under each project.
    """

    def __init__(self, address: str, kind: str, web3: Web3, db,
                 daos_collection: str, dao_address: str, economy_address: str = None):
        self.address = Web3.to_checksum_address(address)
        self.kind = kind  # 'economy' or 'project'
        self.web3 = web3
        self.db = db
        self.daos_collection = daos_collection
        self.dao_address = Web3.to_checksum_address(dao_address)  # homebase DAO doc IDs are checksummed
        self.economy_address = economy_address  # For projects, which economy they belong to
        self.contract = None
        # DAO proposals that overrule (appeal) or veto this project, while they may still pass
        # (see check_links): kind -> proposal id
        self.dao_links = {}
        self.dao_governor = None

        # Set ABI based on kind
        if kind == "economy":
            self.abi_string = economyAbi
        else:  # project
            self.abi_string = projectAbi

        if self.abi_string:
            self.abi = json.loads(self.abi_string)
        else:
            self.abi = None

    def get_contract(self):
        """Get or create contract instance"""
        if self.contract is None and self.address and self.abi:
            try:
                self.contract = self.web3.eth.contract(
                    address=self.address, abi=self.abi
                )
            except Exception as e:
                print(f"[trustless] Error creating contract for {self.address}: {e}")
                return None
        return self.contract

    def handle_event(self, log, func=None):
        """
        Route events to appropriate handlers.
        Returns new contract address(es) to listen to, or None.
        """
        if self.kind == 'economy':
            if func == 'NewProject':
                return self.handle_new_project(log)
        elif self.kind == 'project':
            handlers = {
                'SetParties': self.handle_set_parties,
                'SendFunds': self.handle_send_funds,
                'ImmediateFundsReleased': self.handle_immediate_funds_released,
                'ContractSigned': self.handle_contract_signed,
                'ProjectDisputed': self.handle_project_disputed,
                'ProjectClosed': self.handle_project_closed,
                'ArbitrationDecision': self.handle_arbitration_decision,
                'ArbitrationFinalized': self.handle_arbitration_finalized,
                'DaoOverruled': self.handle_dao_overruled,
                'ContractorPaid': self.handle_contractor_paid,
                'ContributorWithdrawn': self.handle_contributor_withdrawn,
                'AuthorPaid': self.handle_author_paid,
                'VetoedByDao': self.handle_vetoed_by_dao,
                'BackerVoteCast': self.handle_backer_vote_cast,
                'ArbitrationStakeReclaimed': self.handle_arbitration_stake_reclaimed,
                'OrphanedTokensSwept': self.handle_orphaned_tokens_swept,
            }
            if func in handlers:
                try:
                    handlers[func](log)
                except Exception as e:
                    print(f"[trustless] Error handling {func} for {self.address}: {e}")
        return None

    def _projects_collection(self):
        return (self.db.collection(self.daos_collection)
                .document(self.dao_address)
                .collection("projects"))

    def _get_project_ref(self, project_address: str = None):
        """Get Firestore reference to a project document (this one by default)"""
        return self._projects_collection().document((project_address or self.address).lower())

    def _get_backers_collection(self):
        """Get Firestore reference to backers subcollection"""
        return self._get_project_ref().collection("backers")

    def _record_transaction(self, log, function_name: str, params: dict = None, project_id: str = None):
        """Record a transaction in Firestore"""
        tx_hash = log['transactionHash'].hex()
        log_index = log.get('logIndex', 0)
        block = self.web3.eth.get_block(log['blockNumber'])

        project = project_id or self.address
        tx = Transaction(
            txHash=tx_hash,
            sender=self._get_tx_sender(log),
            functionName=function_name,
            contractAddress=log['address'],
            projectId=project,
            params=params or {},
            time=datetime.fromtimestamp(block['timestamp'], tz=timezone.utc),
            blockNumber=log['blockNumber'],
        )

        # Use tx_hash + log_index as document ID to handle multiple events per transaction
        doc_id = f"{tx_hash}_{log_index}"
        self._get_project_ref(project).collection("activity").document(doc_id).set(tx.to_firestore())
        print(f"[trustless] Recorded transaction: {function_name} - {tx_hash[:10]}...")

        # Also the DAO's activity feed. Appeals and vetoes are proposals there, recorded by the DAO indexer.
        if function_name not in ('appeal', 'veto'):
            name = (params or {}).get('name') if function_name == 'createProject' else self._project_name(project)
            record_activity(self.db.collection(self.daos_collection).document(self.dao_address), self.web3, log,
                            function_name, f"{FEED_PHRASES.get(function_name, function_name)} {name}".strip(),
                            actor=tx.sender, target_kind='project', target_id=project.lower(), target_label=name)

    def _project_name(self, project):
        if not hasattr(self, '_names'):
            self._names = {}
        if project not in self._names:
            snap = self._get_project_ref(project).get()
            self._names[project] = (snap.to_dict() or {}).get('name', '') if snap.exists else ''
        return self._names[project]

    def _get_tx_sender(self, log) -> str:
        """Get the sender address from a transaction"""
        try:
            tx = self.web3.eth.get_transaction(log['transactionHash'])
            return tx['from'].lower()
        except Exception as e:
            print(f"[trustless] Error getting tx sender: {e}")
            return ""

    def _read_contribution(self, who):
        """(total, immediate, locked) for a backer, read from the project contract"""
        read_contract = self.web3.eth.contract(address=self.address, abi=json.loads(projectReadAbi))
        total, immediate, locked = read_contract.functions.contributions(Web3.to_checksum_address(who)).call()
        return total, immediate, locked

    def _update_amount_from_onchain(self, extra=None):
        """Read actual balance from on-chain and update Firestore amount field, in one update with any
        other fields of the event (extra). An unchanged amount alone isn't rewritten."""
        try:
            read_abi = json.loads(projectReadAbi)
            read_contract = self.web3.eth.contract(address=self.address, abi=read_abi)

            # ERC20 projects expose token(); native projects don't
            try:
                token_address = read_contract.functions.token().call()
            except Exception:
                token_address = None

            if token_address and token_address != ZERO_ADDRESS:
                # ERC20 project - get token balance of project contract
                erc20_abi = json.loads('[{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]')
                token_contract = self.web3.eth.contract(address=token_address, abi=erc20_abi)
                on_chain_amount = str(token_contract.functions.balanceOf(self.address).call())
            else:
                # Native project - get ETH balance of project contract
                on_chain_amount = str(self.web3.eth.get_balance(self.address))

            # Update project amount from on-chain data
            if extra or getattr(self, '_last_amount', None) != on_chain_amount:
                self._get_project_ref().update({**(extra or {}), 'amount': on_chain_amount})
                self._last_amount = on_chain_amount
                print(f'[trustless] Updated project amount to {on_chain_amount} from on-chain balance')
            return on_chain_amount
        except Exception as e:
            print(f'[trustless] Warning: Could not read/update balance: {e}')
            return None

    # ============ Economy Event Handlers ============

    def handle_new_project(self, log):
        """Handle NewProject event - creates project document and returns address to listen to"""
        contract = self.get_contract()
        if not contract:
            return None

        try:
            decoded = contract.events.NewProject().process_log(log)
            args = decoded['args']

            project_address = args['contractAddress']
            project_name = args['projectName']

            print(f"[trustless] NewProject: {project_name} at {project_address}")

            # Get block timestamp
            block = self.web3.eth.get_block(log['blockNumber'])
            created_at = datetime.fromtimestamp(block['timestamp'], tz=timezone.utc)

            # Get transaction sender (author)
            author = self._get_tx_sender(log) or ""

            # Get addresses with fallback to empty string if None
            contractor = args.get('contractor') or ""
            arbiter = args.get('arbiter') or ""
            token_address = args.get('token') or ""

            # Create project entity
            project = Project(
                address=project_address,
                name=project_name,
                description=args.get('description') or "",
                repo=args.get('repo') or "",
                termsHash=args.get('termsHash') or "",
                author=author,
                contractor=contractor,
                arbiter=arbiter,
                tokenAddress=token_address,
                # Parties named at creation: the contract starts in pending (cooling-off running)
                stage=Stage.PENDING if contractor not in ("", ZERO_ADDRESS) and arbiter not in ("", ZERO_ADDRESS) else Stage.OPEN,
                daoAddress=self.dao_address,
                economyAddress=self.address,
                createdAt=created_at,
            )

            # Write to Firestore
            project_ref = self._get_project_ref(project_address)
            # On a replay the doc already reflects later events; don't reset it
            try:
                project_ref.create(project.to_firestore())
            except AlreadyExists:
                pass

            # Record transaction
            self._record_transaction(log, "createProject", {
                "name": project_name,
                "contractor": contractor,
                "arbiter": arbiter,
                "tokenAddress": token_address,
            }, project_id=project_address)

            print(f"[trustless] Created project document: {project_address}")

            # Return the new project address so it gets added to listeners
            return project_address

        except Exception as e:
            print(f"[trustless] Error processing NewProject: {e}")
            traceback.print_exc()
            return None

    # ============ Project Event Handlers ============

    def handle_set_parties(self, log):
        """Handle SetParties - updates contractor, arbiter, termsHash, stage"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.SetParties().process_log(log)
        args = decoded['args']

        contractor = args['_contractor'].lower()
        arbiter = args['_arbiter'].lower()

        updates = {
            "contractor": contractor,
            "arbiter": arbiter,
            "termsHash": args['_termsHash'],
            "stage": Stage.PENDING,
        }

        self._get_project_ref().update(updates)
        self._record_transaction(log, "setParties", {
            "contractor": args['_contractor'],
            "arbiter": args['_arbiter'],
            "termsHash": args['_termsHash'],
        })

        print(f"[trustless] SetParties: contractor={args['_contractor'][:10]}...")

    def handle_send_funds(self, log):
        """Handle SendFunds - update project amount, upsert backer"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.SendFunds().process_log(log)
        args = decoded['args']

        who = args['who'].lower()
        how_much = str(args['howMuch'])
        immediate_bps = args['immediateBps']

        # Amounts come from contract state rather than running totals, so replaying an event
        # (restart overlap, retried batch) can't double count.
        self._update_amount_from_onchain()
        total, immediate, _locked = self._read_contribution(who)
        backer_ref = self._get_backers_collection().document(who)
        if total > 0:
            backer_ref.set({
                'address': who,
                'contribution': str(total),
                'immediateBps': (immediate * 10000) // total,
                'lastUpdated': datetime.now(timezone.utc),
            }, merge=True)

        self._record_transaction(log, "sendFunds", {
            "amount": how_much,
            "immediateBps": immediate_bps,
        })

        print(f"[trustless] SendFunds: {who[:10]}... sent {how_much} wei")

    def handle_immediate_funds_released(self, log):
        """Handle ImmediateFundsReleased - funds sent to contractor, update balance"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.ImmediateFundsReleased().process_log(log)
        args = decoded['args']

        amount = str(args['amount'])

        # With the amount from the on-chain balance (funds left the contract), in one update
        self._update_amount_from_onchain({'immediateReleased': amount})

        self._record_transaction(log, "immediateFundsReleased", {
            "contractor": args['contractor'],
            "amount": amount,
        })

        print(f"[trustless] ImmediateFundsReleased: {amount} wei")

    def handle_contract_signed(self, log):
        """Handle ContractSigned - stage becomes ongoing"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.ContractSigned().process_log(log)
        args = decoded['args']

        self._get_project_ref().update({
            'stage': Stage.ONGOING,
        })

        self._record_transaction(log, "signContract", {
            "contractor": args['contractor'],
        })

        print(f"[trustless] ContractSigned: stage -> ongoing")

    def handle_project_disputed(self, log):
        """Handle ProjectDisputed - stage becomes dispute"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.ProjectDisputed().process_log(log)
        args = decoded['args']

        self._get_project_ref().update({
            'stage': Stage.DISPUTE,
        })

        self._record_transaction(log, "projectDisputed", {
            "by": args['by'],
        })

        print(f"[trustless] ProjectDisputed: stage -> dispute")

    def handle_project_closed(self, log):
        """Handle ProjectClosed - stage becomes closed"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.ProjectClosed().process_log(log)
        args = decoded['args']

        self._update_amount_from_onchain({'stage': Stage.CLOSED})

        self._record_transaction(log, "projectClosed", {
            "by": args['by'],
        })

        print(f"[trustless] ProjectClosed: stage -> closed")

    def handle_arbitration_decision(self, log):
        """Handle ArbitrationDecision - stage becomes appealable"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.ArbitrationDecision().process_log(log)
        args = decoded['args']

        self._get_project_ref().update({
            'stage': Stage.APPEALABLE,
            'disputeResolution': args['percent'],
            'rulingHash': args['rulingHash'],
        })

        self._record_transaction(log, "arbitrate", {
            "arbiter": args['arbiter'],
            "percent": args['percent'],
            "rulingHash": args['rulingHash'],
        })

        print(f"[trustless] ArbitrationDecision: {args['percent']}% -> appealable")

    def handle_dao_proposal(self, log, proposal_id: int, kind: str):
        """A DAO proposal that calls daoOverrule (kind 'appeal') or daoVeto (kind 'veto') on this
        project was created: the project doc links it while it may still pass (see check_links)."""
        read = self.web3.eth.contract(address=self.address, abi=json.loads(projectReadAbi))
        governor = read.functions.daoGovernor().call()
        if Web3.to_checksum_address(governor) != Web3.to_checksum_address(log['address']):
            print(f"[trustless] Ignoring {kind} proposal from a different governor for {self.address}")
            return
        self._get_project_ref().update({DAO_LINK_FIELDS[kind]: str(proposal_id)})
        self.dao_links[kind] = int(proposal_id)
        self.dao_governor = Web3.to_checksum_address(governor)
        self._record_transaction(log, kind, {
            "proposer": self._get_tx_sender(log),
            "proposalId": str(proposal_id),
        })
        print(f"[trustless] {kind.capitalize()} underway: proposal {proposal_id}")

    def handle_arbitration_finalized(self, log):
        """Handle ArbitrationFinalized - stage becomes closed"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.ArbitrationFinalized().process_log(log)
        args = decoded['args']

        self._get_project_ref().update({
            'stage': Stage.CLOSED,
        })

        self._record_transaction(log, "finalizeArbitration", {
            "finalizer": args['finalizer'],
        })

        print(f"[trustless] ArbitrationFinalized: stage -> closed")

    def handle_dao_overruled(self, log):
        """Handle DaoOverruled - DAO timelock overrides arbiter decision"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.DaoOverruled().process_log(log)
        args = decoded['args']

        self._get_project_ref().update({
            'stage': Stage.CLOSED,
            'disputeResolution': args['percent'],
            'rulingHash': args['rulingHash'],
        })

        self._record_transaction(log, "daoOverruled", {
            "timelock": args['timelock'],
            "percent": args['percent'],
            "rulingHash": args['rulingHash'],
        })

        print(f"[trustless] DaoOverruled: {args['percent']}%")

    def handle_contractor_paid(self, log):
        """Handle ContractorPaid - funds sent to contractor, update balance"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.ContractorPaid().process_log(log)
        args = decoded['args']

        # Update project amount from on-chain balance (funds left the contract)
        self._update_amount_from_onchain()

        self._record_transaction(log, "withdrawAsContractor", {
            "contractor": args['contractor'],
            "amount": str(args['amount']),
        })

        print(f"[trustless] ContractorPaid: {args['amount']} wei")

    def handle_contributor_withdrawn(self, log):
        """Handle ContributorWithdrawn - delete backer and update project amount from on-chain"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.ContributorWithdrawn().process_log(log)
        args = decoded['args']

        contributor = args['contributor'].lower()
        withdrawn_amount = str(args['amount'])

        # Update project amount from on-chain balance (funds left the contract)
        self._update_amount_from_onchain()

        # Before signing a withdrawal undoes the backing, so the record goes. After close the
        # contract keeps the contribution (it still counts as spending), so mark it settled.
        backer_ref = self._get_backers_collection().document(contributor)
        if self._read_contribution(contributor)[0] == 0:
            backer_ref.delete()
        else:
            backer_ref.set({'settled': True, 'withdrawn': withdrawn_amount,
                            'lastUpdated': datetime.now(timezone.utc)}, merge=True)

        self._record_transaction(log, "withdrawAsContributor", {
            "contributor": contributor,
            "amount": withdrawn_amount,
        })

        print(f"[trustless] ContributorWithdrawn: {contributor[:10]}... withdrew {withdrawn_amount} wei")

    def handle_author_paid(self, log):
        """Handle AuthorPaid"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.AuthorPaid().process_log(log)
        args = decoded['args']

        self._record_transaction(log, "authorPaid", {
            "author": args['author'],
            "amount": str(args['amount']),
        })

        print(f"[trustless] AuthorPaid: {args['amount']} wei")

    def handle_vetoed_by_dao(self, log):
        """Handle VetoedByDao - project is closed with 0% to contractor"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.VetoedByDao().process_log(log)
        args = decoded['args']

        self._get_project_ref().update({
            'stage': Stage.CLOSED,
            'disputeResolution': 0,
        })

        self._record_transaction(log, "vetoedByDao", {
            "timelock": args['timelock'],
        })

        print(f"[trustless] VetoedByDao: stage -> closed (0% to contractor)")

    def handle_arbitration_stake_reclaimed(self, log):
        """Handle ArbitrationStakeReclaimed - contractor took back their half of the arbitration fee"""
        decoded = self.get_contract().events.ArbitrationStakeReclaimed().process_log(log)
        args = decoded['args']
        self._update_amount_from_onchain()
        self._record_transaction(log, "reclaimArbitrationStake", {
            "contractor": args['contractor'],
            "amount": str(args['amount']),
        })
        print(f"[trustless] ArbitrationStakeReclaimed: {args['amount']} wei")

    def handle_orphaned_tokens_swept(self, log):
        """Handle OrphanedTokensSwept - stray tokens sent to the DAO treasury"""
        decoded = self.get_contract().events.OrphanedTokensSwept().process_log(log)
        args = decoded['args']
        self._update_amount_from_onchain()
        self._record_transaction(log, "sweepOrphanedTokens", {
            "token": args['token'],
            "amount": str(args['amount']),
        })

    def handle_backer_vote_cast(self, log):
        """Handle BackerVoteCast - record backer's vote for/against fund release"""
        contract = self.get_contract()
        if not contract:
            return

        decoded = contract.events.BackerVoteCast().process_log(log)
        args = decoded['args']

        voter = args['voter'].lower()
        for_dispute = args['forDispute']
        voting_power = str(args['votingPower'])

        # Update backer's vote in Firestore
        # vote field expects: "none", "release", or "dispute"
        vote_value = "dispute" if for_dispute else "release"
        backer_ref = self._get_backers_collection().document(voter)
        backer_ref.set({
            'vote': vote_value,
            'votingPower': voting_power,
        }, merge=True)

        self._record_transaction(log, "voteToDispute" if for_dispute else "voteToRelease", {
            "voter": voter,
            "forDispute": for_dispute,
            "votingPower": voting_power,
        })

        vote_type = "dispute" if for_dispute else "release"
        print(f"[trustless] BackerVoteCast: {voter[:10]}... voted for {vote_type} with {voting_power} power")

    # ============ DAO proposal links ============

    def check_links(self, now: int):
        """Clears each linked DAO proposal (appealProposalId, vetoProposalId) once it can no longer
        pass: canceled, defeated, expired, or past the hold grace without being queued or executed
        (the rule DaoProposalHold.isLive applies on-chain), so the app offers Appeal, Escalate or a
        new veto again. An executed overrule or veto closes the project, so its link stays."""
        for kind, field in list(DAO_LINK_FIELDS.items()):
            pid = self.dao_links.get(kind)
            if pid is None:
                continue
            if self.dao_governor is None:  # links loaded from Firestore at startup
                read = self.web3.eth.contract(address=self.address, abi=json.loads(projectReadAbi))
                self.dao_governor = Web3.to_checksum_address(read.functions.daoGovernor().call())
            governor = self.web3.eth.contract(address=self.dao_governor, abi=GOVERNOR_STATE_ABI)
            state = governor.functions.state(pid).call()
            if state == EXECUTED:
                del self.dao_links[kind]
                continue
            if state in (PENDING, ACTIVE):
                continue
            if state == SUCCEEDED and now <= governor.functions.proposalDeadline(pid).call() + HOLD_GRACE:
                continue
            if state == QUEUED and now <= governor.functions.proposalEta(pid).call() + HOLD_GRACE:
                continue
            ref = self._get_project_ref()
            snap = ref.get()
            # A later proposal of the same kind may have replaced it already
            if snap.exists and str((snap.to_dict() or {}).get(field)) == str(pid):
                ref.update({field: None})
                print(f"[trustless] {kind} proposal {pid} on {self.address} ended without executing (state {state}); link cleared")
            del self.dao_links[kind]
