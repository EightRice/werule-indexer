import json

import pytest

import networks
from docstore import Client
from publisher.targets import DirTarget


def write(root, name, data):
    d = root / "networks"
    d.mkdir(exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data))


def test_list_and_load(tmp_path):
    write(tmp_path, "Localhost", {"rpc": "http://127.0.0.1:8545", "chainId": 31337, "fromBlock": 1})
    write(tmp_path, "Sepolia", {"name": "Sepolia", "rpc": "https://rpc.example", "chainId": 11155111})
    t = DirTarget(str(tmp_path))
    found = networks.list_networks(t)
    assert sorted(found) == ["Localhost", "Sepolia"]
    assert found["Localhost"]["name"] == "Localhost"
    assert networks.load("Sepolia", t)["chainId"] == 11155111
    with pytest.raises(FileNotFoundError):
        networks.load("Nope", t)


def test_bad_files(tmp_path):
    t = DirTarget(str(tmp_path))
    write(tmp_path, "A", {"name": "B", "rpc": "x"})
    with pytest.raises(ValueError, match="differs"):
        networks.load("A", t)
    write(tmp_path, "C", {"chainId": 1})
    with pytest.raises(ValueError, match="no rpc"):
        networks.load("C", t)


def test_keyed_rpc_stays_out_of_the_store(tmp_path, monkeypatch):
    write(tmp_path, "Mainnet-X", {"rpc": "https://public.example", "chainId": 1})
    monkeypatch.setenv("RPC_MAINNET_X", "https://keyed.example/SECRET")
    t = DirTarget(str(tmp_path))
    data = networks.load("Mainnet-X", t)
    assert networks.indexer_rpc("Mainnet-X", data) == "https://keyed.example/SECRET"
    db = Client(str(tmp_path / "s.sqlite"))
    networks.sync_to_store(db, "Mainnet-X", data)
    assert "SECRET" not in json.dumps(db.collection("contracts").document("Mainnet-X").get().to_dict())
    db.close()


def test_sync_keeps_checkpoints_and_skips_no_ops(tmp_path):
    db = Client(str(tmp_path / "s.sqlite"))
    ref = db.collection("contracts").document("Localhost")
    data = {"name": "Localhost", "rpc": "r", "jurisdictionFactory": "0x1"}
    assert networks.sync_to_store(db, "Localhost", data)
    ref.set({"lastSyncedBlock": 50, "projectsLastSyncedBlock": 40}, merge=True)
    assert not networks.sync_to_store(db, "Localhost", data)
    data2 = {"name": "Localhost", "rpc": "r", "jurisdictionFactory": "0x2"}
    assert networks.sync_to_store(db, "Localhost", data2)
    d = ref.get().to_dict()
    assert d["jurisdictionFactory"] == "0x2" and d["lastSyncedBlock"] == 50 and d["projectsLastSyncedBlock"] == 40
    db.close()


def test_put(tmp_path, monkeypatch):
    src = tmp_path / "Localhost.json"
    src.write_text(json.dumps({"rpc": "r", "chainId": 31337}))
    out = tmp_path / "out"
    t = DirTarget(str(out))
    networks.put([str(src)], t)
    assert networks.load("Localhost", t)["chainId"] == 31337
