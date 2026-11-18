# apps/trustless/entities.py
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, List


# Stage enum matching the Flutter app
class Stage:
    OPEN = "open"
    PENDING = "pending"
    ONGOING = "ongoing"
    REVIEW = "review"  # the contractor declared the work done; the dispute window, then the time to contest
    DISPUTE = "dispute"
    APPEALABLE = "appealable"
    CLOSED = "closed"


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass
class Project:
    """Project entity for Firestore"""
    address: str
    name: str
    description: str = ""
    repo: str = ""
    termsHash: str = ""
    author: str = ""  # Set from transaction sender in NewProject
    contractor: str = ZERO_ADDRESS
    arbiter: str = ZERO_ADDRESS
    tokenAddress: str = ZERO_ADDRESS
    stage: str = Stage.OPEN
    daoAddress: str = ""  # the homebase DAO the Economy belongs to
    economyAddress: str = ""
    createdAt: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    amount: str = "0"  # Wei as string
    # The jurisdiction's settings as fixed when the parties were named (TermsFixed), and the clock
    terms: Optional[dict] = None
    coolingOffPeriodEnds: Optional[int] = None
    deadline: Optional[int] = None      # the last moment the contractor can declare the work done
    reviewEnds: Optional[int] = None    # the end of the dispute window
    totalDisputed: str = "0"            # locked funds the backers disputed
    # Additional fields for tracking
    immediateReleased: str = "0"
    disputeResolution: Optional[int] = None  # Percent 0-100
    rulingHash: str = ""
    appealProposalId: Optional[str] = None
    vetoProposalId: Optional[str] = None

    def to_firestore(self) -> dict:
        """Convert to Firestore document format"""
        return {
            "id": self.address,
            "name": self.name,
            "description": self.description,
            "repo": self.repo,
            "termsHash": self.termsHash,
            "author": self.author.lower() if self.author else "",
            "contractor": self.contractor.lower() if self.contractor else ZERO_ADDRESS,
            "arbiter": self.arbiter.lower() if self.arbiter else ZERO_ADDRESS,
            "tokenAddress": self.tokenAddress.lower() if self.tokenAddress else ZERO_ADDRESS,
            "stage": self.stage,
            "daoAddress": self.daoAddress,
            "economyAddress": self.economyAddress.lower() if self.economyAddress else "",
            "createdAt": self.createdAt,
            "amount": self.amount,
            "terms": self.terms,
            "coolingOffPeriodEnds": self.coolingOffPeriodEnds,
            "deadline": self.deadline,
            "reviewEnds": self.reviewEnds,
            "totalDisputed": self.totalDisputed,
            "immediateReleased": self.immediateReleased,
            "disputeResolution": self.disputeResolution,
            "rulingHash": self.rulingHash,
            "appealProposalId": self.appealProposalId,
            "vetoProposalId": self.vetoProposalId,
        }

    @classmethod
    def from_firestore(cls, doc_dict: dict) -> "Project":
        """Create from Firestore document"""
        return cls(
            address=doc_dict.get("id", ""),
            name=doc_dict.get("name", ""),
            description=doc_dict.get("description", ""),
            repo=doc_dict.get("repo", ""),
            termsHash=doc_dict.get("termsHash", ""),
            author=doc_dict.get("author", ""),
            contractor=doc_dict.get("contractor", ZERO_ADDRESS),
            arbiter=doc_dict.get("arbiter", ZERO_ADDRESS),
            tokenAddress=doc_dict.get("tokenAddress", ZERO_ADDRESS),
            stage=doc_dict.get("stage", Stage.OPEN),
            daoAddress=doc_dict.get("daoAddress", ""),
            economyAddress=doc_dict.get("economyAddress", ""),
            createdAt=doc_dict.get("createdAt", datetime.now(timezone.utc)),
            amount=doc_dict.get("amount", "0"),
            terms=doc_dict.get("terms"),
            coolingOffPeriodEnds=doc_dict.get("coolingOffPeriodEnds"),
            deadline=doc_dict.get("deadline"),
            reviewEnds=doc_dict.get("reviewEnds"),
            totalDisputed=doc_dict.get("totalDisputed", "0"),
            immediateReleased=doc_dict.get("immediateReleased", "0"),
            disputeResolution=doc_dict.get("disputeResolution"),
            rulingHash=doc_dict.get("rulingHash", ""),
            appealProposalId=doc_dict.get("appealProposalId"),
            vetoProposalId=doc_dict.get("vetoProposalId"),
        )


@dataclass
class Backer:
    """Backer entity for project backers subcollection"""
    address: str
    contribution: str = "0"  # Wei as string
    disputed: bool = False  # disputed their own locked share during the dispute window
    lastUpdated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    immediateBps: int = 0  # Basis points 0-2000 (max 20%)

    def to_firestore(self) -> dict:
        return {
            "contribution": self.contribution,
            "disputed": self.disputed,
            "lastUpdated": self.lastUpdated,
            "immediateBps": self.immediateBps,
        }

    @classmethod
    def from_firestore(cls, address: str, doc_dict: dict) -> "Backer":
        return cls(
            address=address,
            contribution=doc_dict.get("contribution", "0"),
            disputed=doc_dict.get("disputed", False),
            lastUpdated=doc_dict.get("lastUpdated", datetime.now(timezone.utc)),
            immediateBps=doc_dict.get("immediateBps", 0),
        )


@dataclass
class Transaction:
    """Transaction record entity"""
    txHash: str
    sender: str
    functionName: str
    contractAddress: str
    projectId: Optional[str] = None  # Project address if applicable
    params: dict = field(default_factory=dict)
    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    blockNumber: int = 0

    def to_firestore(self) -> dict:
        return {
            "sender": self.sender.lower() if self.sender else "",
            "functionName": self.functionName,
            "contractAddress": self.contractAddress.lower() if self.contractAddress else "",
            "projectId": self.projectId.lower() if self.projectId else None,
            "params": self.params,
            "time": self.time,
            "blockNumber": self.blockNumber,
        }
