# apps/debates/eip712.py
#
# The signed debate messages (DEBATES.md section 2): typed data, digest and signer.

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

DOMAIN_NAME = "werule debates"
DOMAIN_VERSION = "1"

DOMAIN_TYPE = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

TYPES = {
    "Argument": [
        {"name": "debate", "type": "string"},
        {"name": "parent", "type": "bytes32"},
        {"name": "con", "type": "bool"},
        {"name": "bps", "type": "uint16"},
        {"name": "text", "type": "string"},
        {"name": "nonce", "type": "uint64"},
    ],
    "Stake": [
        {"name": "debate", "type": "string"},
        {"name": "argument", "type": "bytes32"},
        {"name": "bps", "type": "uint16"},
        {"name": "nonce", "type": "uint64"},
    ],
    "Retire": [
        {"name": "debate", "type": "string"},
        {"name": "argument", "type": "bytes32"},
        {"name": "nonce", "type": "uint64"},
    ],
}


def _value(kind, v):
    if kind == "bytes32":
        return bytes.fromhex(v[2:])
    if kind in ("uint16", "uint64", "uint256"):
        return int(v)
    return v


def typed_data(type_, message, chain_id, dao):
    fields = TYPES[type_]
    return {
        "types": {"EIP712Domain": DOMAIN_TYPE, type_: fields},
        "primaryType": type_,
        "domain": {"name": DOMAIN_NAME, "version": DOMAIN_VERSION, "chainId": int(chain_id),
                   "verifyingContract": dao},
        "message": {f["name"]: _value(f["type"], message[f["name"]]) for f in fields},
    }


def digest(type_, message, chain_id, dao):
    """The EIP-712 hash, 0x-hex: an Argument's id"""
    s = encode_typed_data(full_message=typed_data(type_, message, chain_id, dao))
    return "0x" + keccak(b"\x19" + s.version + s.header + s.body).hex()


def signer(type_, message, chain_id, dao, signature):
    """The address that signed it (checksummed); raises on a malformed signature"""
    s = encode_typed_data(full_message=typed_data(type_, message, chain_id, dao))
    return Account.recover_message(s, signature=signature)


def sign(type_, message, chain_id, dao, private_key):
    """For tests and tools: the 0x-hex signature"""
    s = encode_typed_data(full_message=typed_data(type_, message, chain_id, dao))
    return "0x" + Account.sign_message(s, private_key=private_key).signature.hex().removeprefix("0x")
