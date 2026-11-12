# werule indexer

Indexes the chains werule runs on and publishes what it finds as static, content-addressed JSON files on Cloudflare R2, with a small Worker that pushes changes to open apps. werule and the Autonet app read only those files: there is no database to query and no Firestore.

```
chain events -> indexer (app.py) -> document store (SQLite) -> publisher -> R2 files + Worker pushes -> apps
```

- **Indexer** (`app.py <network>`): follows a network's factories, DAOs, projects, debates and jurisdictions' Substrates, and writes documents (DAOs, proposals, votes, members, projects, backers, activity) to the document store.
- **Document store** (`docstore/`): Firestore's data model and the part of its client API the indexer uses, on one SQLite file. Every write is versioned and appended to a change log.
- **Publisher** (`publisher/`): tails the change log and publishes each network's documents to R2 (or a local directory) as described in [publisher/CONTRACT.md](publisher/CONTRACT.md), and notifies the Worker (`publisher/worker`).
- **Supervisor** (`supervisor.py`): runs one indexer and one publisher per network file, and restarts them when they exit or their file changes.

## Networks

A network is a JSON file, `networks/<Name>.json`, written by the deploy that put the factories on that chain (trustless-contracts' `scripts/deploy-jurisdiction-suite.js` publishes it to R2 when R2 keys are available, the way it used to write Firestore). Its fields are the network's addresses and settings: `rpc`, `chainId`, `jurisdictionFactory`, `moduleCatalog`, `moduleProcessor`, `nativeProject`, `erc20Project`, `debatesFactory`, `fromBlock`, and so on. Adding a network is adding its file; the supervisor picks it up within a minute.

`NETWORKS_SOURCE` says where the files are: `dir:<root>` (files under `<root>/networks/`, default `dir:.`) or `r2:<bucket>` (objects under `networks/`, in the data bucket itself: a network file holds only public facts, and R2 keys decide who can write it). `RPC_<NAME>` overrides a network's `rpc`, so a keyed RPC URL stays out of the file.

## Running

```
pip install -r requirements.txt
python supervisor.py --target r2:<bucket>          # every network, published to R2
python supervisor.py --target dir:../edge-out      # locally, to a directory
python app.py Localhost                            # one network's indexer only
python -m publisher --network Localhost --networks-index --target dir:../edge-out
```

Environment: `DOCSTORE_PATH` (default `data/werule.sqlite`), `NETWORKS_SOURCE`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (for `r2:` targets), `NOTIFY_URL` and `NOTIFY_SECRET` (the Worker), `INDEXER_RELAYER_KEY` (pays gas to register DAO proposals on the projects they veto or overrule), `LOG_DIR`.

## Local development

werule with `--dart-define=LOCAL=true` reads `http://127.0.0.1:8788` (files) and `ws://127.0.0.1:8787` (live pushes). The local stack:

1. `npx hardhat node` in trustless-contracts (chain 8545), then `npx hardhat run scripts/indexer-e2e/deploy.js --network localhost`, which writes `deployments/networks/Localhost.json`.
2. `npm install` once in `publisher/worker`, then `python dev.py` here: the supervisor (indexer and publisher for every network file in trustless-contracts' `deployments`), the file server on 8788 and the Worker on 8787. Ctrl-C stops them all.
3. `flutter run -d web-server --web-port 5005 --dart-define=LOCAL=true` in werule.

`python dev.py --e2e --store <new file> --out <empty dir> --networks dir:<root>` runs the same stack on the e2e ports (8798, 8797) for tests that must not touch the main one.

## Tests

```
python -m pytest docstore publisher/tests tests
python -m publisher.tests.store_e2e --store <new file> --dir <empty dir>
```

The indexer's end-to-end harness lives in trustless-contracts (`scripts/indexer-e2e/run.sh <this repo>`): it deploys the contracts to a scratch hardhat node, drives every event type, and checks the documents in a scratch store.
