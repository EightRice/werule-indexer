# activity.py
#
# A jurisdiction's activity feed: one document per indexed event, written by every app that
# indexes something for the DAO (DAOs and projects), under the homebase DAO document:
#
#   {daos_collection}/{dao}/activity/{txHash}_{logIndex}
#       type         what happened: proposalCreated, vote, delegate, registryEdit, a project
#                    function (sendFunds, arbitrate, ...), agentRegistered, debateArgument, ...
#       summary      one line for people, e.g. "Voted For on Raise the quorum"
#       actor        who did it (lowercase address): the event's actor, else the tx sender
#       time         block time
#       blockNumber
#       txHash       0x-prefixed, for the block explorer
#       targetKind   proposal | project | debate | agent | '' (what the entry links to)
#       targetId     the proposal id, project or debate address, or agent address
#       targetLabel  how to show the target (proposal title, project name, debate title)
#       proposalType for proposalCreated: the proposal's type (appeal and veto get their own look)
#
# The document ID matches the per-project activity's, so an event is one entry wherever it shows,
# and replays overwrite instead of duplicating.

from datetime import datetime, timezone

from web3 import Web3

_block_times = {}


def _tx_hash(log):
    raw = log['transactionHash']
    return raw.hex() if hasattr(raw, 'hex') else str(raw)


def doc_id(log):
    return f"{_tx_hash(log)}_{log.get('logIndex', 0)}"


def block_time(web3, block_number):
    if block_number not in _block_times:
        if len(_block_times) > 2000:
            _block_times.clear()
        _block_times[block_number] = datetime.fromtimestamp(web3.eth.get_block(block_number)['timestamp'], tz=timezone.utc)
    return _block_times[block_number]


def tx_sender(web3, log):
    try:
        return web3.eth.get_transaction(log['transactionHash'])['from'].lower()
    except Exception:
        return ""


def short(address):
    return f"{address[:6]}...{address[-4:]}" if address and len(address) > 12 else (address or "")


def format_units(amount, decimals):
    """A token amount in whole units, without trailing zeros"""
    amount, decimals = int(amount), int(decimals or 0)
    whole, frac = divmod(amount, 10 ** decimals) if decimals else (amount, 0)
    frac_str = str(frac).rjust(decimals, '0').rstrip('0') if decimals else ''
    return f"{whole:,}" + (f".{frac_str[:6]}" if frac_str else '')


def record_activity(dao_ref, web3, log, type_, summary, actor=None, target_kind='', target_id='',
                    target_label='', extra=None):
    """Writes one feed entry under [dao_ref] (the homebase DAO document). Never raises."""
    try:
        h = _tx_hash(log)
        data = {
            'type': type_,
            'summary': summary,
            'actor': (actor or tx_sender(web3, log) or '').lower(),
            'time': block_time(web3, log['blockNumber']),
            'blockNumber': int(log['blockNumber']),
            'txHash': h if h.startswith('0x') else '0x' + h,
            'targetKind': target_kind,
            'targetId': str(target_id or ''),
            'targetLabel': target_label or '',
        }
        if extra:
            data.update(extra)
        dao_ref.collection('activity').document(doc_id(log)).set(data)
    except Exception as e:
        print(f"[activity] Could not record {type_} for {getattr(dao_ref, 'id', dao_ref)}: {e}")


def checksum(address):
    try:
        return Web3.to_checksum_address(address)
    except Exception:
        return address
