# apps/trustless/config.py

APP_NAME = "trustless"

# Projects are written under the DAO their Economy belongs to: {dao_collection}/{dao}/projects/{project},
# next to the DAO's proposals and debates.

# The checkpoint lives in the network's contracts/{network} doc, in its own field: the DAO module's
# historical sync advances lastSyncedBlock on its own.
CHECKPOINT_FIELD = "projectsLastSyncedBlock"

# Economy contract event signatures
ECONOMY_EVENT_SIGNATURES = [
    "NewProject(address,string,address,address,string,string,string,address)",
]

# Project contract event signatures (both NativeProject and ERC20Project)
PROJECT_EVENT_SIGNATURES = [
    "SetParties(address,address,string)",
    "SendFunds(address,uint256,uint256)",
    "ImmediateFundsReleased(address,uint256)",
    "ContractSigned(address)",
    "ProjectDisputed(address)",
    "ProjectClosed(address)",
    "ArbitrationDecision(address,uint256,string)",
    "ArbitrationFinalized(address)",
    "DaoOverruled(address,uint256,string)",
    "ContractorPaid(address,uint256)",
    "ContributorWithdrawn(address,uint256)",
    "AuthorPaid(address,uint256)",
    "VetoedByDao(address)",
    "BackerVoteCast(address,uint256,bool)",
    "ArbitrationStakeReclaimed(address,uint256)",
    "OrphanedTokensSwept(address,uint256)",
]

# All event signatures combined
EVENT_SIGNATURES = ECONOMY_EVENT_SIGNATURES + PROJECT_EVENT_SIGNATURES
