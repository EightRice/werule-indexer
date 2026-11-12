# Edge reads: integration contract

Status: draft v2 (2026-11-01, reviewed against werule `67246a6` and indexer `feat/edge-reads`). Branches: `feat/edge-reads` (indexer repo: publisher), `feature/edge-reads` (werule).

Changelog: v2.1 (2026-11-01, publisher implementation): `order.key` of (id) asc lists is `"__name__"`; `lo`/`hi` carry the lowercase id; every ListIndex has `views` (`{}` when none); notify results are one per message in request order; notify retries are re-signed with a fresh timestamp and re-packed from the queues; a scope that empties while running gets `head: null` plus one `resync` (as at startup), and a pointer already at `head: null` is left alone on restart. The werule checkout for this work is `C:\code\werule-edge`. Owner decisions on section 13: (1) accept with a billing notification, (2) confirmed, (3) remove Firebase entirely once every read is covered, (4) accept the gap, no backfill.

Changelog: v2.2 (2026-11-01, publisher review): a `stale` result whose `lastSeq` is a seq this publisher process already posted on that channel (and above the message's seq) is treated as `duplicate` up to `lastSeq`, since it only means an earlier attempt was applied and the retry was packed differently; a resync after a genuine `stale` uses a seq above both `lastSeq` and the publisher's own last seq; a Firestore listener that stops for good makes the publisher exit so its supervisor restarts it.

Changelog: 2026-11-01 worker: section 6 defines `duplicate` for a retried multi-message POST, requires strictly increasing seqs per channel within one POST, `seq >= 1`, `head` always present, no `changes` on `resync`, and status codes for malformed bodies (400) and more than 20 messages or a message over 256 KB (413); 7.2 buffer gains an internal `size` column; 12 notes local miniflare's own per-object file.

Changelog: 2026-11-01 client: 9.2 gains a release grace period (a scope stays connected 30 s after its last user) and one shared list state per list key; 9.4: a failed first connect reads the pointer at once for the initial head (the first load never waits 10 s on a broken socket), and a connect attempt that has not opened after 10 s counts as a failure; 9.6: R2 is dropped (no caller), R4 is the DAO list's count from the latest frame, `getDaoCount` stays for one-off use; werule treats an `order.key` of `""`, `"id"` or `"__name__"` as k = 0. `APP_CHECK_SETUP.md` and `FIRESTORE_AUDIT.md` exist only in the werule-projects checkout, which is off limits for this work, so their header line is still to add. Client review: a socket with two unanswered pings (about 60 s without any frame) is treated as closed; the reconnect backoff resets only after a socket has stayed up 60 s (a server that drops every socket after `hello` gets a growing backoff, not one connect per second); data host GETs time out after 20 s; a head taken from a pointer drops older frame counts, so counts fall back to that head's ListIndex counts.

Three components, built independently against this document:

| Component | Owner | Lives in |
|---|---|---|
| Publisher (Python) | publisher engineer | `C:\code\DAO\indexer\publisher\` |
| Worker + Durable Object | worker engineer | new dir, e.g. `C:\code\DAO\indexer\publisher\worker\` |
| werule data layer (Dart) | client engineer | `C:\code\werule-projects\lib\src\data\` (new) |

Firestore (`homebase-6907d`) stays the source of truth. The indexer is not changed. Firestore document shapes are not changed (homebase-app still reads them). werule stops reading Firestore entirely.

Key words MUST / SHOULD / MAY as in RFC 2119.

---

## 1. What werule reads today

All reads are in `lib/src/services/firestore_service.dart`, `members_service.dart`, `paged_query.dart`, `providers/dao_provider.dart` (verified by grep of `lib/` for `collection(`, `.doc(`, `snapshots(`, `.get(`, `count()`, `FirebaseFirestore`; nothing else touches Firestore). werule performs **no Firestore writes**, and Firebase is used for nothing but Firestore (`cloud_firestore`, `firebase_core`, `firebase_app_check` in `pubspec.yaml`), so after migration all three packages, `firebase_options.dart`, `config/app_check.dart` and the emulator branches in `main.dart` can go. `{C}` = `idaos{Network.name}`, e.g. `idaosEtherlink-Shadownet`. `{dao}` = DAO doc id (checksummed address).

| # | Method (caller) | Firestore read | Filter / order / limit | Live |
|---|---|---|---|---|
| R1 | `getNetworks` (NetworkProvider) | `contracts` (all docs) | none | no |
| R2 | `getDaos` (unused) | `{C}` all | none | no |
| R3 | `watchNewestDaos` (DaoProvider) | `{C}` | `orderBy creationDate desc, limit 5` | yes |
| R4 | `getDaoCount` (DaoProvider) | `{C}` count() | none | no |
| R5 | `getDaosPaginated` (DaoProvider) | `{C}` | doc-id order, `limit 21`, `startAfterDocument` | no |
| R6 | `searchDaos` (DaoProvider) | `{C}/{q}` and `{C}/{q.lower}` get, else `{C}` `limit 500` cached 5 min, substring on name/address in memory | | no |
| R7 | `getDao` (dao, proposal, project, debate screens) | `{C}/{id}` for id in {as given, checksummed, lowercase}; cached 1 min | | no |
| R8 | `waitForDao` (DaoCreatorProvider) | `{C}/{checksummed}` snapshots until exists, 3 min timeout | | yes |
| R9 | `proposalsQuery` + PagedQuery (ProposalsTab, page 25) | `{C}/{dao}/proposals` | `createdAt desc`, first page live, older pages `startAfterDocument` | first page |
| R10 | `getOpenProposals` (DaoDetailScreen) | `{C}/{dao}/proposals` | `createdAt >= now - (votingDelay+votingDuration min + 1 day)` UNION `latestStage == 'Queued'` | no |
| R11 | `getProposalsByIds` (MemberProvider) | `{C}/{dao}/proposals` | `documentId in [30-chunk]`, sorted createdAt desc client side | no |
| R12 | `getProposal` (unused) / `getProposalStream` (ProposalDetailScreen) | `{C}/{dao}/proposals/{pid}` | | stream |
| R13 | `votesQuery` + PagedQuery (VotesModal, page 20, `live: false`) | `{C}/{dao}/proposals/{pid}/votes` | `cast desc` | no |
| R14 | `hasUserVoted` (ProposalDetailProvider) | `.../votes` | `voter == checksummed, limit 1` | no |
| R15 | `getMemberActivity` (MemberProvider) | `{C}/{dao}/members/{checksummed}` | | no |
| R16 | `daoActivityQuery` + PagedQuery (DaoActivityFeed) | `{C}/{dao}/activity` | `time desc` | first page |
| R17 | `hasProjects` (DaoDetailScreen) | `{C}/{dao}/projects` | `limit 1` | no |
| R18 | `projectsQuery` + PagedQuery (ProjectsTab) | `{C}/{dao}/projects` | `createdAt desc` | first page |
| R19 | `getProjectStream` (ProjectDetailProvider) | `{C}/{dao}/projects/{lower}` | | stream |
| R20 | `getProjectBackersStream` (ProjectDetailProvider) | `{C}/{dao}/projects/{lower}/backers` | all, sorted by `contribution` (BigInt string) desc client side | stream |
| R21 | `projectActivityQuery` + PagedQuery (ProjectDetailProvider) | `{C}/{dao}/projects/{lower}/activity` | `time desc` | first page |
| R22 | `MembersService._membersFromFirestore` (DaoMembersWidget, explorer fallback) | `{C}/{dao}/members` | `balanceSort > 0, balanceSort desc, limit 50`; legacy DAOs: all | no |

Model parsers (`lib/src/models`) read Firestore types: `Timestamp` (Org.creationDate, Proposal.createdAt, Proposal.statusHistory values, DaoActivity.time, Project.createdAt, ProjectActivity.time), `Blob` (Proposal.callDatas items), everything else plain JSON-like. Vote.cast is an ISO string already.

## 2. Firestore data the publisher mirrors

Per network `{N}` (= `contracts` doc id = werule `Network.name`, e.g. `Etherlink-Shadownet`), collection `{C}` = `idaos{N}`:

| Firestore path | Written by | Notes |
|---|---|---|
| `contracts/{N}` | deploy scripts, indexer checkpoints | checkpoint fields `lastSyncedBlock` (homebase, autonet) and `projectsLastSyncedBlock` (trustless) are rewritten every poll loop (`app.py write_checkpoint`) |
| `{C}/{dao}` | homebase paper | id checksummed (`Web3.to_checksum_address`); fields per `entities.Org.toJson` plus later updates: `name`, `symbol`, `decimals`, `creationDate` (Timestamp), `token`, `registryAddress`, `treasuryAddress`, `address`, `proposals` (array of pid strings, grows about 80 B per proposal), `registry` map, `treasury` map, `votingDelay`, `votingDuration`, `quorum`, `supermajorityBps`, `economy` (string, removed with `DELETE_FIELD` on detach) |
| `{C}/{dao}/proposals/{pid}` | homebase paper | pid = uint256 decimal string (up to 78 chars); `createdAt` (Timestamp), `latestStage` (`pending`, `Queued`, `Executed`, `Canceled`), `statusHistory` (map of Timestamps), `callDatas` (strings or bytes), `inFavor`/`against` uint256 strings, `votesFor`/`votesAgainst`/`votesCast` ints, `optionVotes` map of strings |
| `{C}/{dao}/proposals/{pid}/votes/{voter}` | homebase paper | id = checksummed voter; `voter`, `weight` (string), `option` (int), `cast` (Python `isoformat()` string, e.g. `2026-10-13T12:00:00+00:00`, fraction optional), `reason`, `hash`. Written in one transaction with the member doc's `proposalsVoted` ArrayUnion |
| `{C}/{dao}/members/{addr}` | homebase paper | id checksummed; `address`, `personalBalance` uint256 string, `votingWeight`, `delegate`, `constituents`, `balanceSort` double (may be missing on legacy docs), `proposalsVoted`, `proposalsCreated` (arrays of pids, about 80 B each, unbounded), `lastSeen` (Timestamp, rewritten on every full set) |
| `{C}/{dao}/activity/{txHash}_{logIndex}` | activity.py (homebase, trustless) | `type`, `summary`, `actor`, `time` (Timestamp), `blockNumber`, `txHash`, `targetKind`, `targetId`, `targetLabel`, extras |
| `{C}/{dao}/projects/{lower}` | trustless paper | id lowercase, DAO segment checksummed (`trustless/paper.py` checksums `dao_address`); `createdAt` (Timestamp); fields per `trustless/entities.py Project.to_firestore`; link fields MAY be set to `null` |
| `{C}/{dao}/projects/{lower}/backers/{addr}` | trustless paper | id lowercase; `address`, `contribution` (uint256 string), `immediateBps`, `vote`, `lastUpdated`; after close, `settled`/`withdrawn` merged in; deleted on withdrawal before signing |
| `{C}/{dao}/projects/{lower}/activity/{txHash}_{logIndex}` | trustless paper | `sender`, `functionName`, `contractAddress`, `projectId`, `params` map, `time` (Timestamp), `blockNumber` |
| `{C}/{dao}/debates/{d}` | homebase paper | NOT published in v1 (werule reads debates from chain) |

Deletes that happen today: backer docs; whole DAOs via maintenance scripts. Field deletes (`economy`) are just updates.

## 3. Encoding (shared by files and pushes)

### 3.1 Document values

Firestore values map to JSON as follows. A JSON object with exactly one key that is one of the tags below is a tagged value; the publisher MUST fail loudly if a real Firestore map would be ambiguous (never seen today).

| Firestore | JSON | Dart decoder yields |
|---|---|---|
| null, bool, string | same | same |
| integer, abs <= 2^53-1 | number | `int` |
| integer, larger | `{"$int":"12345678901234567890"}` | `BigInt` |
| double (finite) | number | `double` |
| double NaN/Inf | `{"$float":"NaN"}` / `"Infinity"` / `"-Infinity"` | `double` |
| Timestamp | `{"$ts":"2026-10-13T12:00:00.123456Z"}` (UTC, exactly 6 fraction digits) | `DateTime` (UTC) |
| bytes | `{"$bytes":"0xdeadbeef"}` (lowercase hex) | `String` (the hex, which is what Proposal.callDatas wants) |
| reference | `{"$ref":"full/doc/path"}` | `String` |
| geopoint | `{"$geo":[lat,lng]}` | `List<double>` |
| array, map | recursive | `List`, `Map<String,dynamic>` |

### 3.2 Document entry

Every document, in a file or a push, is a **doc entry**:

```json
{"path":"idaosEtherlink-Shadownet/0xAbC.../proposals/42","id":"42","v":1761998400123456,"data":{...}}
```

- `path`: exact Firestore path, original case. Client store key = `path.toLowerCase()`.
- `id`: last path segment, original case.
- `v`: the document's Firestore `update_time` in integer microseconds since epoch. Deterministic, survives publisher restarts, monotonic per document.
- `data`: the document fields encoded per 3.1. Shapes are exactly Firestore's.

A delete in a push is `{"path":...,"id":...,"v":<read_time µs of the snapshot that observed the delete>,"deleted":true}`. For deletes found at startup (5.4) `v` is the `read_time` of that listener's first snapshot. This is safe: a snapshot whose `read_time` is R and which shows the doc absent means any re-creation commits after R, so a re-created doc always has `v` greater than the tombstone.

**Merge rule (client):** for each store key keep the entry with the highest `v` (on a tie keep the existing one); a delete is kept as a tombstone with its `v` for the life of the session. Merging is therefore commutative: order of arrival does not matter, and an old file never overwrites a newer push. `v` values are below 2^53 (about 1.8e15), so they are exact as JS numbers. This rule covers document *data*; list *membership* is covered by the seq rule in 9.2.

### 3.3 Canonical JSON and hashes

All immutable objects are serialized canonically: UTF-8, keys sorted, no insignificant whitespace (`json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False)`), no `NaN`. Object name = first 32 hex chars of SHA-256 of those bytes. Example: `{"coll":"proposals","docs":[]}` hashes to `ceffff57e213d53c5e9eaad9cff91d3a`. Identical data MUST produce identical bytes across publisher restarts (no timestamps of build time, no seq, inside immutable objects).

### 3.4 Path segments

In file paths and channel names, a segment that is an address (`0x` + 40 hex) is lowercased; other segments are used verbatim (network names match `[A-Za-z0-9-]{1,64}`). The DAO scope key is the lowercase DAO address, so docs under any case variant of a DAO doc id belong to the same scope (werule's `getDao` probes three variants because legacy docs may differ). If two Firestore docs map to the same lowercased path (client store key), the publisher publishes only the one whose DAO segment is checksummed (else the one whose path sorts first) and logs a warning once per path.

## 4. File layout (bucket / local dir)

Base URL: `https://data.autonet.computer/` (prod), `http://127.0.0.1:8788/` (dev). `{N}` network, `{d}` lowercase DAO address.

| Path | Kind | Cache-Control | Content |
|---|---|---|---|
| `networks.json` | mutable | `public, max-age=60` | all networks (4.1) |
| `{N}/ptr.json` | mutable pointer | `public, max-age=5` | network scope pointer (4.2) |
| `{N}/d/{d}/ptr.json` | mutable pointer | `public, max-age=5` | DAO scope pointer (4.2) |
| `{N}/o/{hash}.json` | immutable object | `public, max-age=31536000, immutable` | heads, pages, list indexes, views |

`Content-Type: application/json; charset=utf-8` for all. Objects are stored uncompressed (Cloudflare compresses on the way out). 404s MUST NOT be cached anywhere (a garbage collected object can be re-uploaded under the same name, 5.5); R2 sends no `Cache-Control` on 404 and the free plan has no status-code TTL, so this holds by default. The `dir:` target writes each file to a temp name in the same directory and renames it into place, so the dev server never serves a partial file (R2 PUTs are atomic).

Why this shape: the only mutable files are a handful of tiny pointers (about 100 bytes) plus `networks.json`. Everything a client actually downloads in bulk is content addressed and immutable, so it caches forever at the edge and in the browser, and adding or changing one item rewrites only the objects on the path from that item to the head (one page, maybe one nested index or view, one head, one pointer). A 5 s pointer TTL bounds staleness only for clients without a websocket; websocket clients get the current head hash from the Durable Object (section 7), so they never depend on the TTL. Worst-case pointer staleness for a polling client is about 10 s (5 s at the edge plus 5 s in the browser). Mutable files are few and tiny, so even a full miss storm on them is bounded by (edge locations x pointers / 5 s), independent of reader count.

### 4.1 `networks.json`

```json
{"format":1,"networks":[{"id":"Etherlink-Shadownet","data":{...contracts doc fields...}}]}
```

All docs of `contracts`, sorted by id. The publisher MUST drop checkpoint fields (any field named `lastSyncedBlock` or ending in `LastSyncedBlock`, case-insensitive) so the file only changes when deployments change. Only one publisher per bucket writes it (`--networks-index` flag). werule feeds `data` to `Network.fromFirestore(data, id)` unchanged.

### 4.2 Pointer

```json
{"format":1,"scope":"dao:Etherlink-Shadownet:0xabc...","seq":1761998400123,"head":"<hash>"}
```

`head` is `null` when the scope was deleted (DAO doc and all its subcollections gone). A missing pointer (404) means the scope does not exist. `seq` is the seq of the last notify message of the flush that wrote this pointer (5.2), so pointer seqs and websocket seqs are one sequence per channel (net scope pointer = channel `net:{N}`, DAO scope pointer = channel `dao:{N}:{d}`).

### 4.3 Head object (immutable)

Network scope:

```json
{"format":1,"scope":"net:Etherlink-Shadownet","lists":{"daos":<ListIndex>}}
```

DAO scope:

```json
{"format":1,"scope":"dao:Etherlink-Shadownet:0xabc...",
 "doc":<DocEntry or null>,
 "lists":{"proposals":<ListIndex>,"members":<ListIndex>,"activity":<ListIndex>,"projects":<ListIndex>},
 "nested":{"proposals/42/votes":{"count":3,"ix":"<hash>"},
           "projects/0xdef.../backers":{"count":2,"ix":"<hash>"},
           "projects/0xdef.../activity":{"count":9,"ix":"<hash>"}}}
```

- All four top-level lists are always present (count 0, empty pages when empty).
- `nested` keys are collection paths relative to the DAO doc using lowercase address segments; only non-empty nested collections appear. `ix` names a ListIndex object.
- If the serialized head exceeds 256 KB, the publisher replaces `"nested":{...}` with `"nestedRef":"<hash>"`, an object `{"nested":{...}}`. Clients MUST support both.

### 4.4 ListIndex

```json
{"coll":"proposals","count":120,"hw":1761998400123456,
 "order":{"key":"createdAt","dir":"desc"},
 "part":"rank",
 "pages":[{"h":"<hash>","n":50,"lo":[1700000000000000,"1"],"hi":[1750000000000000,"77"]}, ...],
 "views":{"queued":"<hash>","loc":"<hash>"}}
```

- `hw`: max `v` over the list's docs (0 if empty).
- `order`: display order werule wants. **Pages are always stored ascending** by `(k, lowercase id)`, oldest first; the last page is the newest and the only one that is normally partial.
- `k` (sort value) = the `order.key` field: Timestamps as µs integers, ISO strings (`cast`) parsed to µs with `datetime.fromisoformat` (a naive value is taken as UTC; with or without fraction and offset), numbers as is, missing or unparsable = 0 (sorts oldest). Note: Firestore `orderBy` hid docs missing the field; they now appear at the old end.
- `part: "rank"`: greedy from the oldest doc, start a new page when the page has P docs or adding the next doc would push the page over 256 KB serialized (a single larger doc gets a page of its own). `lo`/`hi` are the first and last `[k,id]` of the page, with `id` lowercased (the same id the page order uses), so compare lowercase ids against them.
- `views` is always present: `{}` for lists without views.
- `part: "prefix"` (members only): see 4.6; `pages` entries carry `"p":"<hex prefix>"` instead of `lo`/`hi`.

Lists and their parameters:

| List | Firestore collection | order.key / dir | part | P | views |
|---|---|---|---|---|---|
| net `daos` | `{C}` | `creationDate` desc | rank | 50 | `search` |
| `proposals` | `.../proposals` | `createdAt` desc | rank | 50 | `queued`, `loc` |
| `members` | `.../members` | (id) asc | prefix | 400 per bucket | `top` |
| `activity` | `.../activity` | `time` desc | rank | 100 | |
| `projects` | `.../projects` | `createdAt` desc | rank | 50 | `loc` |
| nested `votes` | `.../proposals/{pid}/votes` | `cast` desc | rank | 200 | |
| nested `backers` | `.../projects/{p}/backers` | (id) asc, client sorts by contribution | rank | 200 | |
| nested `activity` | `.../projects/{p}/activity` | `time` desc | rank | 100 | |

For `(id) asc` lists `k` is 0 for every doc, so order is by lowercase id, and the ListIndex says `"order":{"key":"__name__","dir":"asc"}`.

### 4.5 Page object

```json
{"coll":"proposals","docs":[<DocEntry>, ...]}
```

Docs ascending by `(k, lowercase id)`. Deleted docs never appear in pages.

### 4.6 Members partitioning (large DAOs)

Members re-rank on every transfer, so they are not paged by balance. Buckets by the first `L` hex chars after `0x` of the lowercase address, `L` = 0 if count <= 400, 1 if <= 6400, else 2. `pages` lists only non-empty buckets, ascending by `p` (`""` when L = 0); each page holds that bucket's docs ascending by id. A bucket over 256 KB is split rank-style within the bucket (entries share `p` and carry `lo`/`hi`). To find one member: compute its prefix, fetch the page(s) with that `p`. `L` changes rewrite all member pages once (rare).

### 4.7 Views (immutable objects referenced from a ListIndex)

| View | Content | Serves |
|---|---|---|
| `daos.search` | `{"items":[{"id","name","symbol","economy":bool,"k"}]}` every DAO, ascending by `(k,id)`; `economy` = `data.economy` non-empty | R6 name search, type filter |
| `proposals.queued` | `{"coll":"proposals","docs":[DocEntry...]}` docs with `latestStage == "Queued"` | R10 |
| `proposals.loc` / `projects.loc` | `{"loc":{"<lowercase id>":<index into pages>}}` | by-id lookups R11, R12, R19 |
| `members.top` | `{"items":[{"id","address","personalBalance"}]}` top 100 by exact `int(personalBalance)` desc, only > 0, ties by id; `address` = `data.address` or the id. A projection, not DocEntries: member docs can be tens of KB (`proposalsVoted`), R22 needs only these two fields. Uses the uint256 string, not `balanceSort`, so legacy docs without `balanceSort` rank correctly | R22 |

`loc` only changes when membership or page boundaries change, not when a proposal's votes change. Every view object MUST stay under 1 MB serialized; if `proposals.queued` would exceed it, it holds `{"coll":"proposals","ids":[...]}` instead and the client resolves ids through `loc` (clients MUST support both).

### 4.8 Size expectations

DAO doc 2 to 5 KB plus about 80 B per proposal (the `proposals` id array: a DAO with 1,000 proposals has an ~85 KB doc, which also rides on every net-channel push of that DAO), proposal 1 to 5 KB (callDatas can be larger), vote ~250 B, member ~400 B plus about 80 B per proposal voted or created, activity ~400 B. A page is at most 256 KB. A DAO with 5,000 members is 13 bucket pages of ~150 KB; a proposal with 5,000 votes is 25 pages of ~50 KB, of which the modal loads the newest one or two. The head for a DAO with 1,000 proposals is roughly 100 KB before the `nestedRef` cutoff.

## 5. Publisher

### 5.1 Inputs and listeners

CLI: `python -m publisher --network shadownet [--networks-index] --target dir:C:\path | r2:<bucket>` (run from `C:\code\DAO\indexer` so `apps.homebase.config` imports; the target value is split on the first `:` only, so Windows drive letters survive). The network key maps through `apps/homebase/config.NETWORKS` to `firestore_doc_name` (`{N}`) and `dao_collection_name` (`{C}`). Credentials: `homebase.json` (same as indexer; also used against the emulator, since an anonymous client does not share emulator data). New dependency: `boto3` (S3 API).

Env:

| Var | Required | Meaning |
|---|---|---|
| `FIRESTORE_EMULATOR_HOST` | dev only | e.g. `127.0.0.1:8080` (main local stack) or `127.0.0.1:8081` (e2e stack); set before importing `firebase_admin` |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | with `r2:` | endpoint `https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com`, region `auto`; the token needs Object Read & Write on that bucket only |
| `NOTIFY_URL` | no | e.g. `https://live.autonet.computer/notify`; unset = files only, no pushes (clients poll) |
| `NOTIFY_SECRET` | with `NOTIFY_URL` | HMAC key, at least 32 random bytes in prod |
| `PUBLISHER_LOG_LEVEL` | no | default `INFO` |

Listeners (firebase-admin `on_snapshot`):

1. `collection("contracts")` (only with `--networks-index`). Checkpoint writes fire this listener every poll loop (one billed read each, a few tens of thousands a day, acceptable); after dropping checkpoint fields (4.1) the result is identical and nothing is written.
2. `collection(C)` (DAO docs).
3. Collection groups `proposals`, `votes`, `members`, `activity`, `projects`, `backers`, each restricted to this network with the same document-path bounds `startup_cache._network_group` already uses: `where(FieldPath.document_id() >= db.document(f"{C}/\u0000/{g}/\u0000"))` and `<= db.document(f"{C}/\uffff/{g}/\uffff")` (paths compare segment by segment, so the bounds also enclose the deeper `votes`, `backers` and project `activity` docs). A string bound like `C + "/"` is not valid for a collection-group `__name__` filter. The single `activity` group covers both DAO and project activity. These need `__name__` COLLECTION_GROUP ascending index overrides. They live in `C:\code\werule-projects\firestore.indexes.json` `fieldOverrides`, which today has only `projects` and `debates`; add `proposals`, `votes`, `members`, `activity`, `backers` with the same three entries each (COLLECTION asc, COLLECTION desc, COLLECTION_GROUP asc; an override replaces the automatic indexes, so the COLLECTION entries must stay, or werule's and the indexer's ordinary queries break) and deploy with `firebase deploy --only firestore:indexes --project homebase-6907d`. If a bounded listener fails, fall back to the unbounded group and keep only docs whose path starts with `{C}/` (costs reads of other networks' docs on every restart). The emulator needs no indexes.

Classification by path shape under `{C}`: `{dao}` doc; `{dao}/proposals/{pid}`; `{dao}/proposals/{pid}/votes/{v}`; `{dao}/members/{m}`; `{dao}/activity/{a}` (3 segments after `{C}`); `{dao}/projects/{p}`; `{dao}/projects/{p}/backers/{b}`; `{dao}/projects/{p}/activity/{a}` (5 segments after `{C}`). Anything else is ignored (log once per collection name).

The mirror is `path -> (v, encoded data)`. A snapshot change whose `update_time` equals the mirrored `v` is a no-op (listener reconnects replay unchanged docs). Changes mark the affected scope(s) dirty: a DAO doc change dirties both `net:{N}` and `dao:{N}:{d}`; everything else dirties only its DAO scope.

### 5.2 Debounce and flush

Per scope: flush when 1000 ms pass without a new change for that scope, or 3000 ms after the first pending change, whichever comes first. Pending changes to the same path coalesce (last wins). Flushes of one scope are serialized; different scopes flush concurrently (cap 8).

Flush steps, in this order:

1. Recompute only the dirty lists, nested indexes, views and the head. Reuse unchanged page bytes.
2. PUT every object whose hash is not in the known-objects set (populated at startup by listing `{N}/o/`, then maintained). Parallel, retried with backoff.
3. Allocate seqs for this flush's k notify messages (5.3; k >= 1): `s1 = max(last_seq + 1, now_ms)`, then `s1+1 ... sk`. PUT the pointer with `seq = sk` and the new head hash. `last_seq = sk`.
4. Enqueue the k messages on that channel's notify queue and return; the flush does not wait for the Worker.

Never notify before the objects and pointer exist.

Notify queue (one per channel, independent of flushing, so a Worker outage never stalls file updates): send in seq order, never seq n+1 before n is accepted, retry with backoff (1 s doubling to 60 s). If the queued messages for a channel exceed 1 MB, or the oldest has waited over 60 s, replace the whole queue with one `resync` carrying the newest `seq` and `head` (the client re-reads from files; nothing is lost). Messages for several channels MAY share one POST (section 6 limits).

### 5.3 Notify content per flush

Channels per flush: a `dao:{N}:{d}` flush notifies channel `dao:{N}:{d}` with every changed doc in the scope (including the DAO doc itself); a `net:{N}` flush notifies `net:{N}` with the changed DAO docs only.

`changes` = the coalesced doc entries of this flush (sets and deletes) for that channel, split into messages of at most 256 KB serialized each, each message its own `seq`, all carrying the flush's final `head` and `counts` (the `count` of each top-level list of the scope after the flush, e.g. `{"daos":123}` or `{"proposals":40,"members":12,"activity":300,"projects":2}`). A single doc entry over 200 KB is replaced by `{"path","id","v","stale":true}`; the client re-reads it from the head in the message. If a flush's changes for one channel total over 1 MB (bulk backfill, a new DAO with thousands of members, restart after long downtime), send one `resync` message instead (section 6).

### 5.4 Startup (no notify spam)

1. Attach all listeners; wait until each has delivered its first snapshot. No flushing while warming.
2. List `{N}/o/` into the known-objects set.
3. For each scope that exists in Firestore or has a pointer in the target: build its head. Read the existing pointer. If `head` is identical: write nothing, notify nothing. Otherwise write objects and pointer, then notify the doc-level diff:
   - sets = docs in the new state with `v` greater than the old ListIndex `hw` (or old head `doc.v`), per list, nested lists included (a nested list absent from the old head: all its docs);
   - deletes = for every list (top-level and nested) of the old head, fetch the old pages whose hashes are absent from the new index (a list or nested list missing from the new head: all its old pages) and emit a delete for each path in them that is no longer present. Counting is not enough (an add and a delete in the same list cancel out). Unchanged pages keep their hash, so only changed pages are fetched; the old head's objects are still reachable from the live pointer, so GC has not removed them.
   - Over 1 MB per channel, or old head or any needed old page unreadable: `resync`.
   - `seq` continues from `max(old ptr seq + 1, now_ms)`.
4. Scopes with a pointer but no Firestore docs: write pointer with `head: null`, send `resync` with `head: null` (nothing if the pointer already says `head: null`). The same happens at runtime when a scope's last doc is deleted.
5. Then switch to normal debounced operation.

Because heads are deterministic, a restart with no Firestore changes during downtime writes nothing and sends nothing; it costs one full Firestore read (accepted).

### 5.5 Garbage collection

Hourly, after the first full build: list `{N}/o/` and delete objects that have been unreachable from every current head (including nested indexes, views, `nestedRef`) for at least 2 hours. Upload time is NOT the criterion: a page uploaded weeks ago may be superseded a minute before GC runs, and clients holding the previous head still need it. The publisher keeps `unreachable_since` per object in memory, set when an object stops being reachable and cleared if it becomes reachable again; objects found at startup that are unreachable get `unreachable_since = startup time`. A deleted object is removed from the known-objects set, so identical content is re-uploaded if needed. Clients handle a 404 on an object by refreshing the head (9.5).

### 5.6 Rules

- Exactly one publisher per network per target. Two would fight over `seq`.
- Write volume is driven by on-chain events, not by readers: about 3 to 8 PUTs per flush per scope (a vote rewrites a votes page, the nested index, the proposal's page, the proposals index, a member page, the head and the pointer), at most one flush per scope per second.

## 6. Notify API (publisher to Worker)

`POST /notify`, `Content-Type: application/json`.

Headers:

- `X-Edge-Timestamp`: unix seconds.
- `X-Edge-Signature`: `v1=` + lowercase hex HMAC-SHA256 with key `NOTIFY_SECRET` over the bytes `timestamp + "." + body`.

Test vector: secret `test-secret`, timestamp `1730000000`, body `{"messages":[]}` gives `v1=8cc09b05dd38fe0709b20698d82926fdf629b028c26ed77bd47929dff3df2a15`.

The Worker MUST reject (401) a bad signature (constant-time compare), and a timestamp more than 300 s from its clock. It accepts `NOTIFY_SECRET` or `NOTIFY_SECRET_PREV` (rotation). Body at most 512 KB (413 otherwise), at most 20 messages (413 otherwise). The cap exists because the Workers free plan allows 10 ms CPU per request: verifying and parsing much more than that risks a CPU-limit error, which the publisher would see as a failed POST and retry forever. The publisher therefore packs at most about 2 full-size messages per POST.

Signature check order: length check, then HMAC over the raw bytes, then `JSON.parse` (never parse unauthenticated bodies).

Body schema:

```json
{
  "messages": [
    {
      "channel": "dao:Etherlink-Shadownet:0xabc...",
      "seq": 1761998400123,
      "kind": "changes",
      "head": "<hash> | null",
      "counts": {"proposals": 40, "members": 12, "activity": 300, "projects": 2},
      "changes": [ <DocEntry or delete entry or stale entry> ]
    }
  ]
}
```

| Field | Type | Rules |
|---|---|---|
| `channel` | string | `^net:[A-Za-z0-9-]{1,64}$` or `^dao:[A-Za-z0-9-]{1,64}:0x[0-9a-f]{40}$` |
| `seq` | integer | strictly increasing per channel, safe JS integer |
| `kind` | `"changes"` \| `"resync"` | `changes`: broadcast and buffer. `resync`: broadcast, clear buffer, set continuity point (7.2). (v1 had a `head` kind; it is removed: heads are deterministic from the doc set, so a head never changes without doc entries to send.) |
| `head` | string or null | head hash after this message |
| `counts` | object | top-level list counts after this message (5.3); present on both kinds, omitted when `head` is null |
| `changes` | array | present only for `changes`; serialized message at most 256 KB |

Unknown fields MUST be passed through unchanged by the Worker (forward compatibility). `seq` MUST be >= 1. `head` MUST be present (a string of at most 128 chars, or null). A `resync` MUST NOT carry `changes`. Within one POST, the messages of one channel MUST have strictly increasing seqs (they are applied in body order; different channels are applied in parallel). After authentication, a body that is not valid JSON or breaks a rule in this section is 400, and a message over 256 KB serialized is 413; the publisher treats these as bugs (log loudly), since resending the same POST cannot succeed.

Response 200: `{"results":[{"channel":"...","status":"ok"|"duplicate"|"stale","lastSeq":N}]}`, exactly one result per message, in request order (the publisher treats a different length as a failed POST). Results are in body order. `duplicate` = seq equals the stored last seq, or seq is lower than the stored last seq and the same POST carries a message on that channel whose seq equals the stored last seq (a retried POST whose earlier attempt was applied, possibly only in part); treat as ok. `stale` = any other seq lower than the stored last seq. That only happens with two publishers on one channel or a rolled-back pointer, so the publisher logs an error and replaces that channel's queue with a `resync` at a fresh seq `max(lastSeq + 1, own last_seq + 1, now_ms)` (rewriting the pointer with that seq first). Exception: if `lastSeq` is a seq this publisher process already posted on that channel, an earlier attempt was applied and the retry was packed differently; the publisher treats the result as `duplicate` and drops its queued messages up to `lastSeq`. Any other response or a network error: retry. Each retry is re-signed with a fresh `X-Edge-Timestamp` (an old one would fall outside the 300 s window during a long outage) and re-packed from the current queues, so it may also carry newer messages or a collapsed `resync`; per channel, messages always go in seq order and an already-sent seq is answered `duplicate`.

## 7. Worker and Durable Object

Host `live.autonet.computer` (Workers custom domain). Free plan, one DO class `Channel`, SQLite backend (`new_sqlite_classes` migration), WebSocket Hibernation API. DO instance = `idFromName(channel)`.

`wrangler.toml` essentials:

```toml
name = "autonet-live"
main = "src/index.ts"
compatibility_date = "2026-10-01"
routes = [{ pattern = "live.autonet.computer", custom_domain = true }]
[[durable_objects.bindings]]
name = "CHANNEL"
class_name = "Channel"
[[migrations]]
tag = "v1"
new_sqlite_classes = ["Channel"]
```

Secrets via `wrangler secret put NOTIFY_SECRET` (and `NOTIFY_SECRET_PREV`); locally in `.dev.vars`.

### 7.1 Endpoints

- `GET /ws?channel=<channel>[&since=<seq>]`: validate channel regex (400 otherwise) and `since` as a non-negative safe integer, require `Upgrade: websocket` (426 otherwise), forward to the DO, which calls `ctx.acceptWebSocket`. No auth (data is public). Connecting to a channel that never received a notify MUST NOT write DO storage (only reads), so scripted connects to random channel names cost requests but leave nothing behind.
- `POST /notify`: section 6. Fan out each message to its DO.
- `GET /health`: `ok`.
- `POST /debates` and `GET /debates/queue`: the debates intake, specified in [../DEBATES.md](../DEBATES.md) section 6. A second SQLite DO class, `DebateQueue` (binding `DEBATES`, migration `v2`), one instance per network; the network list and chainId come from `networks.json` under the `DATA_URL` var (cached 60 s). The queue read uses this section's HMAC over the empty body. Queued entries carry the signed fields normalized (hex lowercase, `nonce` as a decimal string) and `receivedAt` in epoch milliseconds; a rate-limited POST is 429, and reposting the same message by the same author returns its existing `seq`.

### 7.2 DO state (SQLite)

- `meta`: `last_seq`, `head`, `counts` (JSON text), `continuous_since`.
- `buffer(seq INTEGER PRIMARY KEY, body TEXT, at INTEGER, size INTEGER)` (`body` = the client frame, `size` = its UTF-8 bytes): `changes` messages, retained 15 minutes and at most 500 rows or 4 MB, whichever is smaller. When rows with seq <= X are pruned, `continuous_since = max(continuous_since, X)`.
- First message ever received: `continuous_since = seq - 1`. `resync`: delete buffer, `continuous_since = seq`.

Replay of `since = S` is possible iff `continuous_since <= S <= last_seq`. Pruning runs when a message is stored (no alarms needed; stale-but-continuous rows are still valid for replay).

### 7.3 Server to client frames (JSON text)

| Frame | When |
|---|---|
| `{"t":"hello","channel":C,"seq":last_seq,"head":head,"replay":bool}` | first frame after connect. With `since` and replay possible: `replay:true`, then every buffered message with seq > since in order, then live. Otherwise `replay:false`: client (re)loads from `head`. `head` null and `seq` 0 if this DO has never received anything (client falls back to the pointer file) |
| `{"t":"changes","channel","seq","head","counts","changes":[...]}` | each `changes` message, fields copied unchanged from the notify message |
| `{"t":"resync","channel","seq","head","counts"}` | publisher resync |

`hello` also carries `counts` as last stored (omitted when unknown). The DO stores `counts` in `meta` with `head`.

Client to server: text `ping` answered `pong` via `setWebSocketAutoResponse` (does not wake the DO); client sends it every 30 s. Any other client frame closes the socket with 1008. Max 1 MB per frame (we stay under 256 KB).

### 7.4 Abuse limits

Free plan quotas fail closed (no bill): 100k Worker requests/day, and separately 100k DO requests/day plus the DO duration allowance (hibernation keeps idle sockets from accruing duration; `setWebSocketAutoResponse` pings do not wake the DO). Every WS connect costs one Worker request and one DO request; every notify POST costs one Worker request plus one DO request per message. Budget: about 2 connects per visit (net + one DAO), so roughly 40k visits/day before the quota trips. When it trips (or someone burns it by scripting connects), `/ws` fails and every client drops to pointer polling (9.4), which is served from the edge cache, so reads keep working and nothing is billed. The rate-limiting rule (section 8, item 8) also covers `/ws`.

## 8. Edge configuration (Cloudflare, zone autonet.computer)

1. R2 bucket (e.g. `autonet-data`), connected as custom domain `data.autonet.computer`. Disable the `r2.dev` public URL so the cached domain is the only way in. R2 CORS policy: `AllowedOrigins: ["*"]`, `AllowedMethods: ["GET","HEAD"]`, `AllowedHeaders: ["*"]`, `MaxAgeSeconds: 86400`.
2. **Response Header Transform Rule** for `http.host eq "data.autonet.computer"`: set static `Access-Control-Allow-Origin: *`. Required, not optional: R2 adds CORS headers only when the request carries `Origin`, and the edge cache ignores `Vary: Origin`, so an object first fetched without `Origin` (curl, a crawler) would be cached without the header and fail CORS for every browser at that edge location. The client sends only simple GETs (no custom headers), so there are no preflights.
3. **URL Rewrite Transform Rule** for `http.host eq "data.autonet.computer"`: rewrite query to empty. Together with the Cache Rule below, a query string can neither split the cache key nor reach R2.
4. Cache Rule for `http.host eq "data.autonet.computer"`: eligible for cache (required: `.json` is not in Cloudflare's default cached extensions); edge TTL "use cache-control header if present" (origin `max-age=5` is honored; the plan minimum applies only to override values); browser TTL "respect origin"; cache key query string: ignore all. Do not cache 404s (section 4).
5. The publisher sets `Cache-Control` and `Content-Type` as object metadata on every PUT (table in section 4).
6. Worker custom domain `live.autonet.computer`; secrets `NOTIFY_SECRET` (and `NOTIFY_SECRET_PREV` during rotation).
7. **WAF custom rule** (free plan allows 5), action Block: `http.host eq "data.autonet.computer" and (not http.request.method in {"GET" "HEAD"} or not (http.request.uri.path eq "/networks.json" or ends_with(http.request.uri.path, "/ptr.json") or (http.request.uri.path contains "/o/" and ends_with(http.request.uri.path, ".json"))))`. Blocked requests never reach R2. (Regex `matches` is not available on the free plan, hence the string operators.)
8. **Rate limiting rule** (the one free rule), per IP, period 10 s, block 10 s, threshold 150. The free plan limits rate-limiting expressions to the URI path field, so the expression is path-only and also counts `.json` paths on other hosts in the zone (harmless): `ends_with(http.request.uri.path, ".json") or http.request.uri.path eq "/ws"` (if the dashboard refuses `ends_with` for this rule type, use `http.request.uri.path contains ".json"`). A full first load of a DAO page is about 6 to 15 requests. It counts cached hits too.

**What can still reach R2 past the cache:** only GETs for names that are not cached at that edge location: never-seen pointer paths (`{N}/d/<random address>/ptr.json`) and random 32-hex object names. Each is one R2 Class B read and a 404. Per IP this is capped at 15/s by rule 8; a distributed attacker is not capped. R2 Class B costs $0.36 per million beyond the free 10M/month, and R2 has no hard spend cap, so this is a residual (slow, attacker-paid-in-IPs) bill risk, not a Firestore drain. Mitigations in place: set an R2 billing notification. Firestore is never reachable from clients through this path.

## 9. werule client

### 9.1 Configuration

`--dart-define=EDGE_DATA_URL=https://data.autonet.computer` and `EDGE_LIVE_URL=wss://live.autonet.computer` (these are the defaults when the defines are absent); local: `http://127.0.0.1:8788` and `ws://127.0.0.1:8787`; e2e stack: `http://127.0.0.1:8798` and `ws://127.0.0.1:8797` (11). `LOCAL=true` without the defines uses the local values. The `FIRESTORE_EMULATOR` define and the emulator branches in `main.dart` are removed. Requests MUST be simple GETs without custom headers and without cache-busting query strings (they are stripped anyway).

### 9.2 Scope sync (one per open scope, reference counted)

1. Open `EDGE_LIVE_URL/ws?channel=...` (add `since=<seq>` when resuming). Apply every `changes` frame to the doc store as it arrives (merge rule 3.2), even while loading.
2. On `hello`: if `replay:true`, continue. Else set `head` = hello.head, or if null fetch the pointer (`ptr.json`) and use its `head` and `seq`. Fetch the head object (immutable).
3. Lists load lazily from the head (8 lists, nested via `ix`). Fetch pages newest first (the end of `pages`).
4. Remember per scope the current `(seq, head, counts)`: updated by every `changes`/`resync` frame, by `hello`, and by a polled pointer. **Never move backwards:** a head from `hello` or a pointer whose `seq` is lower than the current `seq` is ignored (an edge-cached pointer can be up to ~10 s older than the last push). Lazy loads (step 3) always use the current head.
5. Reference counting: every screen that reads a scope holds a reference; the last release closes the socket after a 30 s grace period, so moving between screens of one DAO does not reconnect. A list key has one shared state per scope (pages fetched, base head), whoever reads it.

**List contents** = docs from the pages fetched for the list's *base head*, plus pushed docs whose path is directly in the list's collection and whose push `seq` is greater than the base head's seq, minus store tombstones; data per doc from the store (max `v`); sorted per `order`. When a list is re-based on a newer head (9.3), pushed-only members with push `seq` <= the new base seq are dropped (the new pages are authoritative up to that seq), and those with a higher seq are kept. This is what keeps a refresh from an older or equal head from losing newer pushes. The visible window of a paged list is everything with `[k,id]` >= the `lo` of the oldest fetched page (pushed items newer than that window are included, matching PagedQuery's live first page; a pushed doc whose `[k,id]` falls below the window is only shown if its page is loaded).

`counts` from the latest frame is the source for list counts (R4, R17); the client never derives counts by adding and subtracting.

### 9.3 Recovering missed pushes

- Socket closed: reconnect with `since=<last seq>` (backoff 1, 2, 4 ... 60 s with jitter). `replay:true` means nothing was lost.
- `replay:false` or `resync`: take the new head (if its seq is not lower than the current one), re-fetch the indexes of the lists currently loaded, fetch only pages whose hashes are new, and re-base those lists on the new head (9.2): membership comes from the new pages, which drops docs deleted during the gap; the doc store keeps max-`v` data. A `resync` with `head: null` means the scope was deleted: clear it.
- `stale` entry: re-read that doc via the message's `head` (find its page via `loc` or by scanning the relevant list index).
- Browser tab resumes from background: treat as a reconnect.

### 9.4 Polling fallback

A failed connect before the first head is known reads the pointer immediately, so the first load does not wait on a broken socket. A connect attempt that has not opened within 10 s is abandoned and counts as a failure. If the socket cannot connect within 10 s or fails 3 times in a row, poll `ptr.json` every 15 s (plus up to 5 s jitter) while still retrying the socket in the background. When the pointer's `head` changes, do the `replay:false` procedure above. Stop polling once a socket is up again (connect with `since` = pointer seq).

### 9.5 Object 404

An immutable object can be garbage collected after its head is superseded for 2 hours. On 404: get the current head (fresh `hello` via reconnect, or the pointer) and retry once through the new indexes.

### 9.6 Read site mapping

| # | New behaviour |
|---|---|
| R1 | `GET networks.json`; `Network.fromFirestore(e.data, e.id)` for each |
| R2 | net scope `daos` list, all pages (keep only if a caller appears) |
| R3 | net channel `changes` frames through the 9.2 list rule: an entry already listed updates in place; an entry not listed goes in at its `creationDate` position only if that is inside the visible window (so a new DAO lands on top, and an update to an old, unloaded DAO is not inserted on top as `_watchNewDaos` would now do, since every DAO doc update is pushed, not only the newest 5); deletes remove |
| R4 | `counts.daos` from the latest net frame, else `head.lists.daos.count` |
| R5 | `daos` pages newest first; "load more" fetches the next older page. Order changes from doc-id order to `creationDate` desc (see 10) |
| R6 | address (`0x`, length >= 10): if it is a full address fetch `{N}/d/{lower}/ptr.json` then head `doc` (404 = none); otherwise substring over `search` view ids. Name: substring over `search` view name and id (all DAOs, no 500 cap). Resolve hits to docs by locating each hit's `[k,id]` in the `daos` ListIndex page ranges (`lo`/`hi`) and fetching those pages (usually one or two), not per-DAO pointers; show at most 50 hits |
| R7 | DAO scope head `doc` (path uses lowercase address, so any case works). Keep a short in-memory cache keyed by lowercase address |
| R8 | subscribe net channel, wait for a `changes` entry for `{C}/{addr}` (case-insensitive) or poll `{N}/d/{lower}/ptr.json` every 5 s until 200 with non-null head; same 3 min timeout |
| R9 | `proposals` list, page size of the UI is independent of file pages; PagedQuery API kept by a new `EdgeList<T>` (`items`, `ready`, `loadingMore`, `hasMore`, `error`, `loadMore`, `live`) |
| R10 | newest `proposals` pages until a page's `lo.k` < window start, filter `createdAt >= since`; union `queued` view docs |
| R11 | `loc` view, group ids by page index, fetch those pages, sort createdAt desc |
| R12 | `loc` + page for first value, then the store entry for that path (live via DAO channel). Id not in `loc` (proposal not indexed yet): emit `null`, then the doc when a push for that path arrives, as the Firestore stream does today. `getProposal` (unused) is dropped |
| R13 | `nested["proposals/{pid}/votes"].ix` then pages newest first; `live:false` may still merge pushes |
| R14 | member doc (R15) `proposalsVoted` contains `pid`. The indexer adds it in the same transaction that writes the vote doc (`homebase/paper.py update_proposal_votes`). Live after the user votes via the DAO channel. Caveat: votes indexed before that transaction existed may lack the member entry (see 10.2) |
| R15 | `members` index: compute prefix of lowercase address, fetch that bucket page, take the doc; absent = `MemberActivity.empty()` |
| R16 | `activity` list |
| R17 | `counts.projects > 0` (latest DAO frame, else `head.lists.projects.count`) |
| R18 | `projects` list |
| R19 | `projects.loc` + page, then store (live); not in `loc`: `null` until pushed, as R12 |
| R20 | `nested["projects/{p}/backers"].ix`, all pages, sort by `BigInt(contribution)` desc as today, live via store |
| R21 | `nested["projects/{p}/activity"].ix` |
| R22 | `members.top` view items, first 50, mapped to the Blockscout shape `{'address':{'hash':address},'value':personalBalance}` as today; the legacy "read all members" branch is gone |

Models: replace `fromFirestore(DocumentSnapshot)` with `fromMap(String id, Map<String,dynamic> data)` over the decoded values of 3.1 (`DateTime` where code checks `is Timestamp`, i.e. `Org.creationDate`, `Proposal.createdAt`/`statusHistory`, `DaoActivity.time`, `Project.createdAt`, `ProjectActivity.time`; `$bytes` already decodes to the `0x` lowercase hex that `bytesToHex(include0x: true)` produced for `Blob`). Field names and defaults do not change. `Org.fromFirestore(json, docId)` and `Network.fromFirestore(data, docId)` already take maps and keep working. `PaginatedDaosResult.lastDocument` (a `DocumentSnapshot`) is replaced by the `EdgeList` cursor.

## 10. Gaps and decided changes

1. **DAO list order (R5):** today doc-id order (effectively arbitrary). Files cannot serve doc-id order cache-friendly while staying append-stable, and newest first is better UX: list is now `creationDate` desc. DAOs missing `creationDate` go last.
2. **hasUserVoted (R14):** there is no per-voter file for votes; served from the member doc's `proposalsVoted` instead. Same truth for every vote indexed by the current transactional code. Votes indexed by older code may have a vote doc without the member entry (false "not voted", which only re-enables the vote button; the chain rejects a double vote). If that matters, the indexer's balanceSort-style backfill can add missing `proposalsVoted` entries once.
3. **Docs missing the sort field** (R9, R16, R18, R21) were excluded by Firestore `orderBy`; now shown at the old end. Harmless; indexer always writes them.
4. **Members fallback (R22):** exact uint256 ordering instead of `balanceSort` double; legacy DAOs no longer need the read-everything path.
5. **Search (R6):** whole-network substring search instead of the first 500 docs. The `search` view is about 80 bytes per DAO; past ~5,000 DAOs it SHOULD be split by `k` range (future).
6. **Debates** are not published in v1; werule does not read them from Firestore. Adding them later is one more DAO list.
7. **`contracts` checkpoint fields** are not published. werule does not use them.
8. **Collection-group `__name__` index overrides** for `proposals`, `votes`, `members`, `activity`, `backers` must be added to `werule-projects/firestore.indexes.json` and deployed to `homebase-6907d` for the bounded listeners (5.1); fallback exists.
9. **Write-side cost:** R2 Class A operations (PUT, LIST) scale with indexed events (3 to 8 PUTs per flush) plus hourly GC listings (one per 1,000 objects). Free tier is 1M/month (about 4,000 to 10,000 flushes/day); an attacker would have to pay gas to drive it. Storage stays small (free 10 GB) because GC removes superseded objects after 2 h. DeleteObject is free. Set an R2 billing notification.
10. Firestore client reads are NOT closed by this work; that happens after homebase-app migrates.
11. **Residual read-side bill risk** (8, last paragraph): distributed cache-miss probing costs R2 Class B reads. Accepted for now with a billing notification; see open questions.
12. **DAO doc pushes are large for big DAOs:** every DAO doc update (new proposal, treasury change) pushes the whole DAO doc, `proposals` array included, to every net-channel subscriber. Fine at current scale; if it hurts, the net channel could carry a projection, but that would need a second doc shape and is out of scope for v1.
13. **Firebase leaves werule:** with no Firestore reads or writes left, `cloud_firestore`, `firebase_core` and `firebase_app_check` are removed (section 1).

## 11. Local development

1. Firestore emulator on 8080 (existing local stack), indexer as today.
2. `python -m publisher --network localhost --networks-index --target dir:C:\code\DAO\local-edge` with `FIRESTORE_EMULATOR_HOST=127.0.0.1:8080`, `NOTIFY_URL=http://127.0.0.1:8787/notify`, `NOTIFY_SECRET=dev-secret`.
3. `python -m publisher.serve --dir C:\code\DAO\local-edge --port 8788`: static server sending `Access-Control-Allow-Origin: *` and the section 4 `Cache-Control` by path pattern (`/o/` immutable, pointers and `networks.json` `no-store` locally).
4. `wrangler dev --port 8787` with `NOTIFY_SECRET=dev-secret` in `.dev.vars` (Durable Objects with SQLite work locally).
5. werule with `--dart-define=EDGE_DATA_URL=http://127.0.0.1:8788 --dart-define=EDGE_LIVE_URL=ws://127.0.0.1:8787`.

Ports: 8788 (static files) and 8787 (wrangler dev; its inspector defaults to 9229) for the main local stack. The indexer e2e stack (Hardhat 8546, emulator 8081) runs alongside it and MUST NOT touch 8545/8080, so its edge parts use their own ports and directory: publisher with `FIRESTORE_EMULATOR_HOST=127.0.0.1:8081 --target dir:C:\code\DAO\local-edge-e2e`, `NOTIFY_URL=http://127.0.0.1:8797/notify`; `publisher.serve --port 8798`; `wrangler dev --port 8797 --inspector-port 9239 --persist-to .wrangler/e2e`. The e2e network key must map to a `{N}` that exists in that emulator (`localhost` -> `Localhost`).

`publisher.serve` details: serves only GET/HEAD from `--dir`, ignores query strings, returns 404 with no caching for missing files, sets `Content-Type: application/json; charset=utf-8`, `Access-Control-Allow-Origin: *`, and `Cache-Control` per path (`/o/` immutable as in section 4; `ptr.json` and `networks.json` `no-store`). Binds 127.0.0.1 only.

## 12. Acceptance checks (each engineer can run alone)

- Publisher: restart twice with no Firestore changes: zero PUTs, zero notifies on the second start. One vote: one `changes` message on the DAO channel containing the vote, proposal and member entries, sent after its objects exist.
- Publisher: with the Worker stopped, file updates continue; after it restarts, each affected channel gets one `resync` (not a backlog). Pointer `seq` equals the last message `seq` of its flush. A superseded page survives GC for 2 h after being superseded even if it was uploaded long before.
- Worker: bad signature 401; seq replay returns `duplicate`; connect with `since` inside the buffer replays exactly the missed messages; `since` older than `continuous_since` gives `replay:false`; connecting to a never-notified channel leaves no DO storage (locally, miniflare itself creates a file per touched object holding only its `__miniflare_do_name` table; the check is that no Worker table exists); a 512 KB body with 2 messages stays under the free-plan CPU limit (check `wrangler tail` for exceeded-CPU errors).
- Edge: `curl https://data.autonet.computer/networks.json?x=1` and `?x=2` both return `cf-cache-status: HIT` after the first; a response fetched first without `Origin` still carries `Access-Control-Allow-Origin: *`; `POST` and odd paths are blocked by the WAF rule.
- Client: with the socket blocked, changes appear within ~20 s via polling; with it open, within ~2 s; a deleted backer disappears after a forced `resync`; a pointer older than the last push does not roll the UI back.

## 13. Open questions (owner decision)

1. **Residual R2 read bill under distributed miss probing** (8, last paragraph). Options: (a) accept with a billing notification (current text); (b) a paid zone plan: more rate-limiting fields and rules, and on higher tiers counting only responses that reached the origin (cache misses); check current plan matrix; (c) put a Worker in front of misses, which fails closed at 100k/day (no bill, but reads go down under attack).
2. **DAO list order** becomes `creationDate` desc instead of doc-id order (10.1). Confirm.
3. **Remove Firebase from werule entirely**, App Check included (10.13). Confirm nothing outside `lib/` (hosting config, analytics) depends on it.
4. **R14 legacy gap** (10.2): accept, or run a one-time `proposalsVoted` backfill.
