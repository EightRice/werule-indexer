# apps/homebase/config.py
# Homebase app configuration

APP_NAME = "homebase"

# Networks come from their files (networks.py); this module only lists the events.

# Event signatures that this app listens for
EVENT_SIGNATURES = [
    "NewDaoCreated(address,address,address[],uint256[],string,string,string,uint256,address,string[],string[])",
    "DaoWrappedDeploymentInfo(address,address,address,address,string,string,string,uint8,uint256,uint48,uint32,uint256)",
    "SuiteConfigured(address,address,address,address,address,address)",  # TrustlessFactory: deployer, economy, registry, timelock, repToken, dao
    "DelegateChanged(address,address,address)",
    "DelegateVotesChanged(address,uint256,uint256)",
    "Transfer(address,address,uint256)",
    "ProposalCanceled(uint256)",
    "ProposalCreated(uint256,address,address[],uint256[],string[],bytes[],uint256,uint256,string)",
    "ProposalQueued(uint256,uint256)",
    "ProposalExecuted(uint256)",
    "VoteCast(address,uint256,uint8,uint256,string)",
    "SupermajoritySet(uint256,uint256)",  # Governor: the threshold for constitutional proposals
    "RegistryUpdated(string,string)",  # Registry: key, value - for Economy DAO descriptions
    # Debates (contracts/debates): the factory's new debates, then each debate's arguments
    "DebateCreated(address,address,address,string,uint256)",
    "ArgumentAdded(uint256,uint256,uint8,address,uint256,string)",
    "WeightAdded(uint256,address,uint256)",
    "DebateClosed()",
]

DEBATE_EVENTS = ["DebateCreated", "ArgumentAdded", "WeightAdded", "DebateClosed"]
