import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from eth_abi import encode
from hexbytes import HexBytes

import signals

FACTORY = "0xAF47F40E996a81b6F818BD6fA354CF60e082b05A"
SENDER = "0x81926df0Cf2692764E4CbC02ef5e642897a930e1"
TX = "0x" + "ab" * 32
WHEN = datetime(2026, 11, 26, 9, 5, tzinfo=timezone.utc)


def make(sender=SENDER, modules=(), own=(), silence_own=False):
    return signals.build_jurisdiction_signal(
        network="Ethereum", explorer="https://etherscan.io/", factory=FACTORY, name="Guild", symbol="GLD",
        governor="0x" + "11" * 20, tx_hash=TX, log_index=7, block=123, when=WHEN, sender=sender,
        members=3, modules=list(modules), own=signals.own_addresses(",".join(own)), silence_own=silence_own)


def test_signal_shape():
    s = make()
    assert s["source"] == "chain:Ethereum"
    assert s["key"] == f"{TX}:7"
    assert s["sender"] == SENDER
    assert s["summary"] == "New jurisdiction deployed: Guild"
    assert s["link"] == f"https://etherscan.io/tx/{TX}"
    assert "2026-11-26 09:05 UTC" in s["snippet"] and "block 123" in s["snippet"]
    assert "No modules (core only)." in s["snippet"]
    assert "is not one of the operator's known addresses" in s["snippet"]
    assert "expected" not in s


def test_own_address_and_modules():
    s = make(modules=["economy", "autonet"], own=[" " + SENDER.lower() + " ", "0x" + "22" * 20])
    assert "Modules: economy, autonet." in s["snippet"]
    assert "is one of the operator's own addresses" in s["snippet"]
    assert "expected" not in s  # the operator hears about their own deployments unless they ask not to
    quiet = make(own=[SENDER], silence_own=True)
    assert quiet["expected"] is True
    assert "expected" not in make(sender="0x" + "44" * 20, own=[SENDER], silence_own=True)


def test_deployer_text_is_cut_and_flattened():
    s = signals.build_jurisdiction_signal(
        network="Ethereum", explorer="", factory=FACTORY, name="A\nB " + "x" * 500, symbol="S" * 90,
        governor="0x0", tx_hash=TX, log_index=0, block=1, when=WHEN, sender=SENDER, members=0, modules=[], own=set())
    assert "\n" not in s["summary"] and len(s["summary"]) <= len("New jurisdiction deployed: ") + signals.MAX_NAME
    assert s["summary"].endswith("...") and s["link"] == ""


def test_attached_modules():
    def entry(address, topic, name):
        return {"address": address, "topics": [HexBytes(topic), HexBytes("0x" + "00" * 32)],
                "data": HexBytes(encode(["string", "address", "uint256"], [name, FACTORY, 1]))}
    receipt = {"logs": [
        entry(FACTORY, signals.MODULE_ATTACHED, "economy"),
        entry("0x" + "33" * 20, signals.MODULE_ATTACHED, "from another contract"),
        entry(FACTORY, "0x" + "01" * 32, "another event"),
        entry(FACTORY.lower(), signals.MODULE_ATTACHED, "autonet"),
        {"address": FACTORY, "topics": []},
    ]}
    assert signals.attached_modules(receipt, FACTORY) == ["economy", "autonet"]


def test_outbox_keeps_what_was_not_delivered(tmp_path):
    path = str(tmp_path / "outbox.json")
    answers = []
    box = signals.Outbox(path, lambda s: answers.pop(0))
    a, b = make(), dict(make(), key="other:1")
    box.add(a)
    box.add(a)  # the same key is kept once
    box.add(b)
    assert len(box.pending) == 2

    def refused(_):
        raise ConnectionRefusedError("down")
    box.deliver = refused
    assert box.flush() == 0
    box.deliver = lambda s: 503
    assert box.flush() == 0
    # A new process finds them in the file, and delivers in order
    again = signals.Outbox(path, None)
    assert [s["key"] for s in again.pending] == [a["key"], b["key"]]
    seen = []
    again.deliver = lambda s: seen.append(s["key"]) or 200
    assert again.flush() == 2
    assert seen == [a["key"], b["key"]]
    assert json.load(open(path)) == []


def test_outbox_refused_body_leaves_and_refused_token_waits(tmp_path):
    box = signals.Outbox(str(tmp_path / "outbox.json"), None)
    for status in (200, 400, 401, 200):
        box.add(dict(make(), key=f"k{len(box.pending)}:{status}"))
    calls = []
    box.deliver = lambda s: calls.append(s["key"]) or int(s["key"].split(":")[1])
    assert box.flush() == 2
    assert [s["key"] for s in box.pending] == ["k2:401", "k3:200"]
    # Nothing is sent while the token is refused
    assert box.flush() == 0 and len(calls) == 3
    box.hold_until = 0
    box.deliver = lambda s: 200
    assert box.flush() == 2 and box.pending == []


def test_post_sends_the_bearer_and_the_body():
    got = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got["auth"] = self.headers["Authorization"]
            got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(401 if got["auth"] != "Bearer good" else 202)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/signal"
        assert signals.post(url, "good", make()) == 202
        assert got["body"]["key"] == f"{TX}:7"
        assert signals.post(url, "bad", make()) == 401
    finally:
        server.shutdown()


def test_configure_is_off_without_url_or_token(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGNAL_URL", raising=False)
    assert signals.configure("Ethereum", {}, str(tmp_path)) is False
    monkeypatch.setenv("SIGNAL_URL", "http://127.0.0.1:1/signal")
    monkeypatch.delenv("SIGNAL_TOKEN", raising=False)
    monkeypatch.setenv("SIGNAL_TOKEN_FILE", str(tmp_path / "missing"))
    assert signals.configure("Ethereum", {}, str(tmp_path)) is False
    assert signals._outbox is None
    # Off: a new jurisdiction does nothing and needs no chain
    signals.jurisdiction_created(None, {}, FACTORY, "Guild", "GLD", "0x0", WHEN, 1)


def test_jurisdiction_created_reads_the_receipt(tmp_path, monkeypatch):
    class Eth:
        def get_transaction_receipt(self, tx_hash):
            assert tx_hash == TX
            return {"from": SENDER, "logs": [{
                "address": FACTORY, "topics": [HexBytes(signals.MODULE_ATTACHED)],
                "data": HexBytes(encode(["string", "address", "uint256"], ["economy", FACTORY, 1]))}]}

    class W3:
        eth = Eth()

    box = signals.Outbox(str(tmp_path / "outbox.json"), None)
    monkeypatch.setattr(signals, "_outbox", box)
    monkeypatch.setattr(signals, "_network", {"name": "Polygon", "explorer": "https://polygonscan.com",
                                              "own": signals.own_addresses(SENDER)})
    log = {"transactionHash": HexBytes(TX), "logIndex": 4, "blockNumber": 99}
    signals.jurisdiction_created(W3(), log, FACTORY, "Guild", "GLD", "0x" + "11" * 20, WHEN, 2)
    (s,) = box.pending
    assert s["source"] == "chain:Polygon" and s["key"] == f"{TX}:4" and s["sender"] == SENDER
    assert "Modules: economy." in s["snippet"] and "own addresses" in s["snippet"]
    assert s["link"] == f"https://polygonscan.com/tx/{TX}"
    # A chain read that fails is reported, not raised
    signals.jurisdiction_created(None, log, FACTORY, "Guild", "GLD", "0x0", WHEN, 2)
    assert len(box.pending) == 1


def test_skip_own_sends_nothing_for_the_operators_own_deploys(tmp_path, monkeypatch):
    class Eth:
        def __init__(self, sender):
            self.sender = sender

        def get_transaction_receipt(self, tx_hash):
            return {"from": self.sender, "logs": []}

    class W3:
        def __init__(self, sender):
            self.eth = Eth(sender)

    box = signals.Outbox(str(tmp_path / "outbox.json"), None)
    monkeypatch.setattr(signals, "_outbox", box)
    monkeypatch.setattr(signals, "_network", {"name": "Sepolia", "explorer": "", "skip_own": True,
                                              "own": signals.own_addresses(SENDER.upper().replace("0X", "0x"))})
    log = {"transactionHash": HexBytes(TX), "logIndex": 4, "blockNumber": 99}
    signals.jurisdiction_created(W3(SENDER), log, FACTORY, "Guild", "GLD", "0x0", WHEN, 2)
    assert box.pending == []
    # Anyone else's deploy still goes out
    signals.jurisdiction_created(W3("0x" + "44" * 20), log, FACTORY, "Guild", "GLD", "0x0", WHEN, 2)
    assert len(box.pending) == 1


def test_configure_reads_skip_own(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNAL_URL", "http://127.0.0.1:1/signal")
    monkeypatch.setenv("SIGNAL_TOKEN", "t")
    monkeypatch.setenv("SIGNAL_SKIP_OWN", "1")
    monkeypatch.setattr(signals.Outbox, "start", lambda self: None)
    assert signals.configure("Sepolia", {}, str(tmp_path)) is True
    assert signals._network["skip_own"] is True and signals._network["silence_own"] is False
