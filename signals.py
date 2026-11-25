"""Signals: one HTTP POST for each new jurisdiction, to an endpoint that tells the operator about it.

    SIGNAL_URL             where to POST (unset: signals are off, the default)
    SIGNAL_TOKEN_FILE      a file holding the bearer token (or SIGNAL_TOKEN, the token itself)
    SIGNAL_OWN_ADDRESSES   the operator's own addresses, comma-separated: a jurisdiction they deploy says so

The body is JSON: source "chain:<network>", key "<transaction hash>:<log index>" (the same event always
gives the same key, so the receiver can drop a repeat: a rebuilt store sends every jurisdiction again),
sender (who sent the transaction), summary (one line), snippet (plain words), link (the explorer), and
"expected": true when the sender is one of the operator's own addresses. The receiver answers 200 for a
signal it took, a repeat included.

Sending never holds the indexer up and never fails it. A signal is written to an outbox file next to
the store and sent from a thread; one that could not be delivered stays in the file and is tried again
every minute, so the receiver being down loses nothing. A body the receiver refuses (400) is dropped
and logged; a refused token (401, 403) keeps everything and waits an hour before the next try.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import timezone

from eth_abi import decode
from web3 import Web3

MODULE_ATTACHED = Web3.keccak(text="ModuleAttached(address,string,address,uint256)")
REFUSED = {400}            # the receiver will never take this body: sending it again cannot help
UNAUTHORIZED = {401, 403}  # the token is wrong: nothing is sent for an hour, and nothing is dropped
MAX_PENDING = 1000
RETRY_SECONDS = 60
UNAUTHORIZED_WAIT_SECONDS = 3600
MAX_NAME = 200             # name and symbol are the deployer's text

_outbox = None
_network = None


def _token():
    path = os.getenv("SIGNAL_TOKEN_FILE")
    if path:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return f.read().strip()
    return (os.getenv("SIGNAL_TOKEN") or "").strip()


def own_addresses(raw):
    return {a.strip().lower() for a in (raw or "").split(",") if a.strip()}


def _cut(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def attached_modules(receipt, factory):
    """Names of the modules the factory attached in this transaction, in order."""
    names = []
    for entry in receipt.get("logs") or []:
        topics = entry.get("topics") or []
        if not topics or bytes(topics[0]) != bytes(MODULE_ATTACHED):
            continue
        if str(entry.get("address", "")).lower() != str(factory).lower():
            continue
        try:
            names.append(decode(["string", "address", "uint256"], bytes(entry["data"]))[0])
        except Exception as e:
            print(f"Signals: could not read a ModuleAttached log: {e}")
    return names


def build_jurisdiction_signal(network, explorer, factory, name, symbol, governor, tx_hash, log_index,
                              block, when, sender, members, modules, own):
    is_own = str(sender).lower() in own
    name, symbol = _cut(name, MAX_NAME), _cut(symbol, 32)
    time_text = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    snippet = " ".join([
        f'Jurisdiction "{name}" ({symbol}) was created on {network} at {time_text}, in block {block}.',
        f"Governor {governor}, factory {factory}.",
        f"Initial members: {members}.",
        f"Modules: {', '.join(modules)}." if modules else "No modules (core only).",
        f"The deployer {sender} is one of the operator's own addresses." if is_own
        else f"The deployer {sender} is not one of the operator's known addresses.",
    ])
    signal = {
        "source": f"chain:{network}",
        "key": f"{tx_hash}:{log_index}",
        "sender": str(sender),
        "summary": f"New jurisdiction deployed: {name}",
        "snippet": snippet,
        "link": f"{explorer.rstrip('/')}/tx/{tx_hash}" if explorer else "",
    }
    if is_own:
        signal["expected"] = True
    return signal


def post(url, token, signal, timeout=10):
    """The response's status code; a connection that fails raises."""
    req = urllib.request.Request(
        url, data=json.dumps(signal).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                 "User-Agent": "werule-indexer/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class Outbox:
    """Signals not delivered yet, kept in a JSON file. deliver(signal) returns a status code or raises."""

    def __init__(self, path, deliver):
        self.path = path
        self.deliver = deliver
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.pending = []
        self.hold_until = 0
        try:
            with open(path, encoding="utf-8") as f:
                self.pending = json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Signals: could not read the outbox {path}: {e}")

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.pending, f)
        os.replace(tmp, self.path)

    def add(self, signal):
        with self.lock:
            if any(s.get("key") == signal["key"] for s in self.pending):
                return
            self.pending.append(signal)
            if len(self.pending) > MAX_PENDING:
                dropped = self.pending[:-MAX_PENDING]
                self.pending = self.pending[-MAX_PENDING:]
                print(f"Signals: outbox over {MAX_PENDING}, dropped the {len(dropped)} oldest")
            self._save()
        self.wake.set()

    def flush(self):
        """Sends what is pending, oldest first; stops at the first one that must wait. Returns how many left the outbox."""
        done = 0
        if time.monotonic() < self.hold_until:
            return done
        while True:
            with self.lock:
                if not self.pending:
                    return done
                signal = self.pending[0]
            try:
                status = self.deliver(signal)
            except Exception as e:
                print(f"Signals: {signal['key']} not delivered ({e}); it stays in the outbox")
                return done
            if 200 <= status < 300:
                print(f"Signals: delivered {signal['key']} ({signal['summary']})")
            elif status in UNAUTHORIZED:
                self.hold_until = time.monotonic() + UNAUTHORIZED_WAIT_SECONDS
                print(f"Signals: the receiver refused the token (HTTP {status}); {len(self.pending)} waiting, "
                      f"next try in {UNAUTHORIZED_WAIT_SECONDS // 60} minutes or at the next start")
                return done
            elif status in REFUSED:
                print(f"Signals: the receiver refused {signal['key']} with HTTP {status}; dropped: {json.dumps(signal)}")
            else:
                print(f"Signals: {signal['key']} got HTTP {status}; it stays in the outbox")
                return done
            with self.lock:
                self.pending = [s for s in self.pending if s.get("key") != signal["key"]]
                self._save()
            done += 1

    def run(self):
        while True:
            self.wake.wait(RETRY_SECONDS)
            self.wake.clear()
            try:
                self.flush()
            except Exception as e:
                print(f"Signals: flush failed: {e}")

    def start(self):
        threading.Thread(target=self.run, name="signals", daemon=True).start()
        self.wake.set()


def configure(network, network_file, data_dir):
    """Turns signals on for this indexer when SIGNAL_URL and a token are set."""
    global _outbox, _network
    url = os.getenv("SIGNAL_URL")
    if not url:
        print("Signals: off (SIGNAL_URL is not set)")
        return False
    try:
        if not _token():
            raise ValueError("the token is empty")
    except Exception as e:
        print(f"Signals: off, no token (SIGNAL_TOKEN_FILE or SIGNAL_TOKEN): {e}")
        return False
    _network = {
        "name": network,
        "explorer": network_file.get("blockExplorer") or "",
        "own": own_addresses(os.getenv("SIGNAL_OWN_ADDRESSES")),
    }
    _outbox = Outbox(os.path.join(data_dir, f"signals-outbox-{network}.json"),
                     lambda signal: post(url, _token(), signal))
    _outbox.start()
    print(f"Signals: on, new jurisdictions on {network} are posted to {url} "
          f"({len(_outbox.pending)} waiting in the outbox)")
    return True


def jurisdiction_created(web3, log, factory, name, symbol, governor, when, members):
    """Called once a new jurisdiction is stored. Never raises."""
    if _outbox is None:
        return
    try:
        tx_hash = Web3.to_hex(log["transactionHash"])
        receipt = web3.eth.get_transaction_receipt(tx_hash)
        _outbox.add(build_jurisdiction_signal(
            network=_network["name"], explorer=_network["explorer"], factory=factory, name=name,
            symbol=symbol, governor=governor, tx_hash=tx_hash, log_index=log["logIndex"],
            block=log["blockNumber"], when=when, sender=receipt["from"], members=members,
            modules=attached_modules(receipt, factory), own=_network["own"]))
    except Exception as e:
        print(f"Signals: could not build the signal for {name}: {e}")
