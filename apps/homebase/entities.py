from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Union
from enum import Enum

class Member:
    def __init__(self, address, delegate, personalBalance, votingWeight) -> None:
        self.address = address
        self.delegate = delegate
        self.personalBalance = personalBalance
        self.constituents = []
        self.votingWeight = votingWeight
        self.proposalsVoted = []
        self.proposalsCreated = []

    def toJson(self):
        return {
            'address': self.address,
            'delegate': self.delegate,
            'personalBalance': str(self.personalBalance),
            'votingWeight': str(self.votingWeight),
            'constituents': self.constituents,
            'proposalsVoted': self.proposalsVoted,
            'proposalsCreated': self.proposalsCreated,
            'balanceSort': balance_sort(self.personalBalance),
            'lastSeen': datetime.now(timezone.utc)
        }


def balance_sort(balance):
    """personalBalance (a uint256 string) as a number, so apps can order members by balance in a query.
    Only for ordering: a double loses precision past 2^53 wei."""
    try:
        return float(int(balance))
    except (TypeError, ValueError):
        return 0.0


class ProposalStatus(Enum):
    pending = 'pending'
    active = 'active'
    passed = 'passed'
    queued = 'queued'
    executable = 'executable'
    executed = 'executed'
    expired = 'expired'
    noQuorum = 'noQuorum'
    rejected = 'rejected'


class StateInContract(Enum):
    Pending = 0
    Active = 1
    Canceled = 2
    Defeated = 3
    Succeeded = 4
    Queued = 5
    Expired = 6
    Executed = 7


class Txaction:
    def toJson(self):
        pass  # Implement as needed


class Token:
    def __init__(self, name: str, symbol: str, decimals: Optional[int]):
        self.name: str = name
        self.symbol: str = symbol
        self.decimals: Optional[int] = decimals
        self.address: Optional[str] = None

    @classmethod
    def fromJson(cls, json_data: Dict[str, Union[str, int]]):
        name = json_data['name']
        symbol = json_data['symbol']
        decimals = json_data.get('decimals')
        address = json_data.get('address')
        token = cls(name=name, symbol=symbol, decimals=decimals)
        token.address = address
        return token

    def toJson(self):
        return {
            'name': self.name,
            'symbol': self.symbol,
            'decimals': self.decimals,
            'address': self.address,
        }


class Org:
    def __init__(self, name: str, govToken: Optional[Token] = None, description: Optional[str] = None, govTokenAddress: Optional[str] = None):
        self.pollsCollection = None
        self.votesCollection = None
        self.name = name
        self.govToken = govToken
        self.description = description
        self.govTokenAddress = govTokenAddress
        self.creationDate: Optional[datetime] = None
        self.memberAddresses: Dict[str, Member] = {}
        self.symbol: Optional[str] = None
        self.decimals: Optional[int] = None
        self.proposalThreshold: Optional[str] = 0
        self.totalSupply: Optional[str] = 0
        self.nonTransferrable: bool = False
        self.treasuryAddress: Optional[str] = None
        self.registryAddress: Optional[str] = None
        self.proposals: List['Proposal'] = []
        self.proposalIDs: Optional[List[str]] = []
        self.treasuryMap: Dict[str, str] = {}
        self.registry: Dict[str, str] = {}
        self.treasury: Dict[Token, str] = {}
        self.address: Optional[str] = None
        self.holders: int = 1
        self.quorum: int = 0
        self.votingDelay: int = 0
        self.votingDuration: int = 0
        self.nativeBalance: str = "0"
        self.executionDelay: int = 0
        self.underlyingToken: Optional[str] = None
        self.economy: Optional[str] = None  # Economy contract address for trustless DAOs

    def toJson(self):
        return {
            'underlyingToken': self.underlyingToken if self.underlyingToken else "None",
            'name': self.name,
            'creationDate': self.creationDate,
            'description': self.description,
            'token': self.govTokenAddress,
            'treasuryAddress': self.treasuryAddress,
            'registryAddress': self.registryAddress,
            'address': self.address,
            'holders': self.holders,
            'symbol': self.symbol,
            'decimals': self.decimals,
            'proposals': self.proposalIDs,
            'proposalThreshold': str(self.proposalThreshold),
            'registry': self.registry if self.registry != None else {},
            'treasury': {token.toJson(): value for token, value in self.treasury.items()},
            'votingDelay': self.votingDelay,
            'totalSupply': self.totalSupply,
            'votingDuration': self.votingDuration,
            'executionDelay': self.executionDelay,
            'quorum': self.quorum,
            'nonTransferrable': self.nonTransferrable,
            'underlying': self.underlyingToken if self.underlyingToken else "None",
            'economy': self.economy if self.economy else None,
        }


class Proposal:
    def __init__(self, org: Org, name: Optional[str] = None):
        self.id: Optional[str] = ""
        self.inAppnumber: int = 0
        self.state: Optional[ProposalStatus] = None
        self.hash: str = ""
        self.totalSupply: str = "0"
        self.org: Org = org
        self.type: Optional[str] = None
        self.name: Optional[str] = name if name else "Title of the proposal (max 80 characters)"
        self.description: Optional[str] = "(no description)"
        self.rawDescription: str = ""  # the on-chain description, as proposed
        self.author: Optional[str] = None
        self.value: float = 0.0
        self.targets: List[str] = []
        self.values: List[str] = []
        self.executionHash = ""
        self.callDatas: List = []
        self.callData: Optional[str] = "0x"
        self.createdAt: Optional[datetime] = datetime.now(timezone.utc)
        self.votingStarts: Optional[datetime] = None
        self.votingEnds: Optional[datetime] = None
        self.executionStarts: Optional[datetime] = None
        self.executionEnds: Optional[datetime] = None
        self.status: str = ""
        self.statusHistory: Dict[str, datetime] = {
            "pending": datetime.now(timezone.utc)}
        self.latestStage = "pending"
        self.turnoutPercent: int = 0
        self.votingStartsBlock: Optional[int] = None
        self.votingEndsBlock: Optional[int] = None
        self.executionStartsBlock: Optional[int] = None
        self.executionEndsBlock: Optional[int] = None
        self.inFavor: str = "0"
        self.against: str = "0"
        self.abstain: str = "0"
        self.votesFor: int = 0
        self.votesAgainst: int = 0
        self.votesAbstain: int = 0
        self.externalResource: Optional[str] = "(no link provided)"
        self.transactions: List[Txaction] = []
        self.votes: List['Vote'] = []

    def toJson(self):
        return {
            'hash': self.hash,
            'type': self.type,
            'title': self.name,
            'description': self.description,
            'rawDescription': self.rawDescription,
            'author': self.author,
            'calldata': self.callData,
            'createdAt': self.createdAt,
            'callDatas': self.callDatas,
            'targets': self.targets,
            'totalSupply': self.totalSupply,
            'votingStartsBlock': self.votingStartsBlock,
            'votingEndsBlock': self.votingEndsBlock,
            'values': self.values,
            'executionHash': self.executionHash,
            'statusHistory': self.statusHistory,
            'turnoutPercent': self.turnoutPercent,
            'inFavor': self.inFavor,
            'against': self.against,
            'abstain': self.abstain,
            'votesFor': self.votesFor,
            'latestStage': self.latestStage,
            'votesAgainst': self.votesAgainst,
            'votesAbstain': self.votesAbstain,
            'externalResource': self.externalResource,
            'transactions': [tx.toJson() for tx in self.transactions],
        }

    def fromJson(self, firestore_data: Dict) -> None:
        """
        Method to populate the Proposal object from Firestore data.

        Args:
            firestore_data (Dict): The Firestore data as a dictionary.
        """
        self.id = firestore_data.get('id', "")
        self.state = firestore_data.get('state')
        self.hash = firestore_data.get('hash', "")
        self.type = firestore_data.get('type')
        self.totalSupply = firestore_data.get('totalSupply', "0")
        self.name = firestore_data.get('title', self.name)
        self.description = firestore_data.get('description', self.description)
        self.author = firestore_data.get('author')
        self.value = firestore_data.get('value', 0.0)
        self.targets = firestore_data.get('targets', [])
        self.values = firestore_data.get('values', [])
        self.callDatas = firestore_data.get('callDatas', [])
        self.callData = firestore_data.get('calldata', "0x")
        self.createdAt = firestore_data.get(
            'createdAt', datetime.now(timezone.utc))
        self.votingStarts = firestore_data.get('votingStarts')
        self.votingEnds = firestore_data.get('votingEnds')
        self.executionStarts = firestore_data.get('executionStarts')
        self.executionEnds = firestore_data.get('executionEnds')
        self.status = firestore_data.get('status', "")
        self.statusHistory = firestore_data.get(
            'statusHistory', {"pending": datetime.now(timezone.utc)})
        self.latestStage = firestore_data.get('latestStage', "pending")
        self.turnoutPercent = firestore_data.get('turnoutPercent', 0)
        self.votingStartsBlock = firestore_data.get('votingStartsBlock')
        self.votingEndsBlock = firestore_data.get('votingEndsBlock')
        self.executionStartsBlock = firestore_data.get('executionStartsBlock')
        self.executionEndsBlock = firestore_data.get('executionEndsBlock')
        self.inFavor = firestore_data.get('inFavor', "0")
        self.executionHash = firestore_data.get('executionHash', "")
        self.against = firestore_data.get('against', "0")
        self.votesFor = firestore_data.get('votesFor', 0)
        self.votesAgainst = firestore_data.get('votesAgainst', 0)
        self.abstain = firestore_data.get('abstain', "0")
        self.votesAbstain = firestore_data.get('votesAbstain', 0)
        self.externalResource = firestore_data.get(
            'externalResource', "(no link provided)")

        # Deserialize transactions if present
        transactions_data = firestore_data.get('transactions', [])
        # Assuming Txaction has a `fromJson` method
        self.transactions = [Txaction.fromJson(tx) for tx in transactions_data]

        # Deserialize votes if present
        votes_data = firestore_data.get('votes', [])
        # Assuming Vote has a `fromJson` method
        self.votes = [Vote.fromJson(vote) for vote in votes_data]


class Vote:
    def __init__(self, votingPower: str, voter: str, proposalID: str, option: int, castAt=None):
        self.voter: str = voter
        self.hash = ""
        self.proposalID: str = proposalID
        self.option: int = option
        self.reason: Optional[str] = None
        self.votingPower: str = votingPower
        self.castAt: datetime = castAt if castAt else datetime.now(
            timezone.utc)

    def toJson(self):
        return {
            'weight': self.votingPower,
            'cast': self.castAt.isoformat(),
            'voter': self.voter,
            'reason': self.reason,
            'option': self.option,
            'hash': self.hash
        }
