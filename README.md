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

A network is a JSON file, `networks/<Name>.json`, written by the deploy that put the factories on that chain (see trustless-contracts). Its fields are the network's addresses and settings: `rpc`, `chainId`, `jurisdictionFactory`, `moduleCatalog`, `moduleProcessor`, `nativeProject`, `erc20Project`, `debatesFactory`, `fromBlock`, and so on. Adding a network is adding its file; the supervisor picks it up within a minute.

`NETWORKS_SOURCE` says where the files are: `dir:<root>` (files under `<root>/networks/`, default `dir:.`) or `r2:<bucket>` (objects under `networks/`; use a private bucket). `RPC_<NAME>` overrides a network's `rpc`, so a keyed RPC URL stays out of the file.

## Running

```
pip install -r requirements.txt
python supervisor.py --target r2:<bucket>          # every network, published to R2
python supervisor.py --target dir:../edge-out      # locally, to a directory
python app.py Localhost                            # one network's indexer only
python -m publisher --network Localhost --networks-index --target dir:../edge-out
```

Environment: `DOCSTORE_PATH` (default `data/werule.sqlite`), `NETWORKS_SOURCE`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (for `r2:` targets), `NOTIFY_URL` and `NOTIFY_SECRET` (the Worker), `INDEXER_RELAYER_KEY` (pays gas to register DAO proposals on the projects they veto or overrule), `LOG_DIR`.

## Tests

```
python -m pytest docstore publisher/tests tests
python -m publisher.tests.store_e2e --store <new file> --dir <empty dir>
```

The indexer's end-to-end harness lives in trustless-contracts (`scripts/indexer-e2e/run.sh <this repo>`): it deploys the contracts to a scratch hardhat node, drives every event type, and checks the documents in a scratch store.
