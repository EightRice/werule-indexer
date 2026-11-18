# apps/trustless/abis.py
# ABIs for decoding events and reading contract state

# Economy contract ABI - NewProject event + read functions
economyAbi = '''[
    {
        "anonymous": false,
        "inputs": [
            {"indexed": true, "internalType": "address", "name": "contractAddress", "type": "address"},
            {"indexed": false, "internalType": "string", "name": "projectName", "type": "string"},
            {"indexed": false, "internalType": "address", "name": "contractor", "type": "address"},
            {"indexed": false, "internalType": "address", "name": "arbiter", "type": "address"},
            {"indexed": false, "internalType": "string", "name": "termsHash", "type": "string"},
            {"indexed": false, "internalType": "string", "name": "repo", "type": "string"},
            {"indexed": false, "internalType": "string", "name": "description", "type": "string"},
            {"indexed": false, "internalType": "address", "name": "token", "type": "address"}
        ],
        "name": "NewProject",
        "type": "event"
    },
    {
        "inputs": [],
        "name": "name",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    }
]'''

# Project contract ABI for reading state (used in state sync)
projectReadAbi = '''[
    {
        "inputs": [],
        "name": "daoGovernor",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "stage",
        "outputs": [
            {
                "internalType": "uint8",
                "name": "",
                "type": "uint8"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "token",
        "outputs": [
            {
                "internalType": "address",
                "name": "",
                "type": "address"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "projectValue",
        "outputs": [
            {
                "internalType": "uint256",
                "name": "",
                "type": "uint256"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {"inputs": [], "name": "coolingOffPeriodEnds", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "deadline", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "reviewEnds", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalDisputed", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "terms", "outputs": [{"name": "platformFeeBps", "type": "uint16"}, {"name": "authorFeeBps", "type": "uint16"}, {"name": "arbitrationFeeBps", "type": "uint16"}, {"name": "disputeWindow", "type": "uint40"}, {"name": "contestWindow", "type": "uint40"}, {"name": "arbitrationTimeout", "type": "uint40"}, {"name": "appealPeriod", "type": "uint40"}, {"name": "duration", "type": "uint40"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "", "type": "address"}], "name": "disputing", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "", "type": "address"}], "name": "depositOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [],
        "name": "disputeResolution",
        "outputs": [
            {
                "internalType": "uint256",
                "name": "",
                "type": "uint256"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {
                "name": "",
                "type": "address"
            }
        ],
        "name": "contributions",
        "outputs": [
            {
                "name": "total",
                "type": "uint256"
            },
            {
                "name": "immediate",
                "type": "uint256"
            },
            {
                "name": "locked",
                "type": "uint256"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {
                "name": "",
                "type": "address"
            }
        ],
        "name": "contributorSettled",
        "outputs": [
            {
                "name": "",
                "type": "bool"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    }
]'''

# NativeProject / ERC20Project ABI - all project lifecycle events
projectAbi = '''[
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": false,
                "internalType": "address",
                "name": "_contractor",
                "type": "address"
            },
            {
                "indexed": false,
                "internalType": "address",
                "name": "_arbiter",
                "type": "address"
            },
            {
                "indexed": false,
                "internalType": "string",
                "name": "_termsHash",
                "type": "string"
            }
        ],
        "name": "SetParties",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": false,
                "internalType": "address",
                "name": "who",
                "type": "address"
            },
            {
                "indexed": false,
                "internalType": "uint256",
                "name": "howMuch",
                "type": "uint256"
            },
            {
                "indexed": false,
                "internalType": "uint256",
                "name": "immediateBps",
                "type": "uint256"
            }
        ],
        "name": "SendFunds",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": false,
                "internalType": "address",
                "name": "contractor",
                "type": "address"
            },
            {
                "indexed": false,
                "internalType": "uint256",
                "name": "amount",
                "type": "uint256"
            }
        ],
        "name": "ImmediateFundsReleased",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": false,
                "internalType": "address",
                "name": "contractor",
                "type": "address"
            }
        ],
        "name": "ContractSigned",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": false,
                "internalType": "address",
                "name": "by",
                "type": "address"
            }
        ],
        "name": "ProjectDisputed",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": false,
                "internalType": "address",
                "name": "by",
                "type": "address"
            }
        ],
        "name": "ProjectClosed",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": false,
                "internalType": "address",
                "name": "arbiter",
                "type": "address"
            },
            {
                "indexed": false,
                "internalType": "uint256",
                "name": "percent",
                "type": "uint256"
            },
            {
                "indexed": false,
                "internalType": "string",
                "name": "rulingHash",
                "type": "string"
            }
        ],
        "name": "ArbitrationDecision",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": true,
                "internalType": "address",
                "name": "appealer",
                "type": "address"
            },
            {
                "indexed": true,
                "internalType": "uint256",
                "name": "proposalId",
                "type": "uint256"
            }
        ],
        "name": "ArbitrationAppealed",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": true,
                "internalType": "address",
                "name": "finalizer",
                "type": "address"
            }
        ],
        "name": "ArbitrationFinalized",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": true,
                "internalType": "address",
                "name": "timelock",
                "type": "address"
            },
            {
                "indexed": false,
                "internalType": "uint256",
                "name": "percent",
                "type": "uint256"
            },
            {
                "indexed": false,
                "internalType": "string",
                "name": "rulingHash",
                "type": "string"
            }
        ],
        "name": "DaoOverruled",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": false,
                "internalType": "address",
                "name": "contractor",
                "type": "address"
            },
            {
                "indexed": false,
                "internalType": "uint256",
                "name": "amount",
                "type": "uint256"
            }
        ],
        "name": "ContractorPaid",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": false,
                "internalType": "address",
                "name": "contributor",
                "type": "address"
            },
            {
                "indexed": false,
                "internalType": "uint256",
                "name": "amount",
                "type": "uint256"
            }
        ],
        "name": "ContributorWithdrawn",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": false,
                "internalType": "address",
                "name": "author",
                "type": "address"
            },
            {
                "indexed": false,
                "internalType": "uint256",
                "name": "amount",
                "type": "uint256"
            }
        ],
        "name": "AuthorPaid",
        "type": "event"
    },
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": true,
                "internalType": "address",
                "name": "timelock",
                "type": "address"
            }
        ],
        "name": "VetoedByDao",
        "type": "event"
    },
    {"anonymous": false, "name": "TermsFixed", "type": "event", "inputs": [{"indexed": false, "name": "platformFeeBps", "type": "uint256"}, {"indexed": false, "name": "authorFeeBps", "type": "uint256"}, {"indexed": false, "name": "arbitrationFeeBps", "type": "uint256"}, {"indexed": false, "name": "disputeWindow", "type": "uint256"}, {"indexed": false, "name": "contestWindow", "type": "uint256"}, {"indexed": false, "name": "arbitrationTimeout", "type": "uint256"}, {"indexed": false, "name": "appealPeriod", "type": "uint256"}, {"indexed": false, "name": "duration", "type": "uint256"}, {"indexed": false, "name": "coolingOffPeriodEnds", "type": "uint256"}]},
    {"anonymous": false, "name": "WorkDeclared", "type": "event", "inputs": [{"indexed": true, "name": "contractor", "type": "address"}, {"indexed": false, "name": "reviewEnds", "type": "uint256"}]},
    {"anonymous": false, "name": "ShareDisputed", "type": "event", "inputs": [{"indexed": true, "name": "backer", "type": "address"}, {"indexed": false, "name": "amount", "type": "uint256"}, {"indexed": false, "name": "deposit", "type": "uint256"}]},
    {"anonymous": false, "name": "UndisputedReleased", "type": "event", "inputs": [{"indexed": false, "name": "amount", "type": "uint256"}]},
    {"anonymous": false, "name": "DisputeContested", "type": "event", "inputs": [{"indexed": true, "name": "contractor", "type": "address"}, {"indexed": false, "name": "deposit", "type": "uint256"}]},
    {"anonymous": false, "name": "DisputeUncontested", "type": "event", "inputs": [{"indexed": true, "name": "contractor", "type": "address"}, {"indexed": false, "name": "disputed", "type": "uint256"}]},
    {"anonymous": false, "name": "DurationExpired", "type": "event", "inputs": [{"indexed": true, "name": "contractor", "type": "address"}]},
    {"anonymous": false, "name": "PaymentOwed", "type": "event", "inputs": [{"indexed": true, "name": "payee", "type": "address"}, {"indexed": false, "name": "amount", "type": "uint256"}]},
    {"anonymous": false, "name": "OwedClaimed", "type": "event", "inputs": [{"indexed": true, "name": "payee", "type": "address"}, {"indexed": false, "name": "amount", "type": "uint256"}]},
    {
        "anonymous": false,
        "inputs": [
            {
                "indexed": true,
                "name": "token",
                "type": "address"
            },
            {
                "indexed": false,
                "name": "amount",
                "type": "uint256"
            }
        ],
        "name": "OrphanedTokensSwept",
        "type": "event"
    }
]'''



# NativeProject / ERC20Project: register a live DAO proposal that vetoes or overrules the project
register_dao_proposal_abi = [{
    "type": "function", "name": "registerDaoProposal", "stateMutability": "nonpayable", "outputs": [],
    "inputs": [
        {"name": "targets", "type": "address[]"},
        {"name": "values", "type": "uint256[]"},
        {"name": "calldatas", "type": "bytes[]"},
        {"name": "descriptionHash", "type": "bytes32"},
    ],
}]
