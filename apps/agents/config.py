# apps/agents/config.py
#
# A jurisdiction's Autonet agents, as they appear in its DAO's activity feed. Each jurisdiction with the
# autonet module records its Substrate in the registry (jurisdiction.autonet.substrate); the main loop
# follows every such Substrate and records its agent events under the DAO:
#   {dao_collection}/{dao}/activity/{txHash}_{logIndex}   type agentRegistered | agentBound | agentRevoked | agentEndpoint
#
# The agents' parent links are kept in the store, outside the DAO collection (never published), so a
# revocation can name how many descendants it took with it:
#   substrates/{chainId}:{substrate}/agents/{agent}   {parent}

APP_NAME = "agents"

EVENT_SIGNATURES = [
    "AgentRegistered(address,bytes,uint256)",
    "OwnerBound(address,address,address,address,uint256)",
    "AgentRevoked(address,address)",
    "EndpointUpdated(address,string)",
]
