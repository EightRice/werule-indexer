# Debates

A debate is a living sentiment on a topic the DAO has opened. It is non-executive: nothing in it reaches the timelock. The topic is on-chain (the proposal that opened it); the arguments and stakes are signed messages, kept off-chain, that anyone can verify against the chain. Participants pay no gas.

## 1. Lifecycle (on-chain)

- **Open:** an ordinary proposal whose single action is `Registry.editRegistry("debates.<id>", "open")` on the DAO's registry. Its description is `{title}0|||0debate0|||0{thesis}0|||0{link}`. `<id>` is the first 16 hex characters (lowercase, no `0x`) of `keccak256(utf8(title + "\n" + thesis))`, so reopening the same topic revives the same debate.
- **Retire:** an ordinary proposal `Registry.editRegistry("debates.<id>", "")`, description type `retire_debate`. A retired debate is read-only: its arguments and tally stay published, frozen; new messages are rejected. Reopening it (same title and thesis, value `open`) makes it live again.
- The indexer opens a debate when the registry entry becomes `open` in a transaction that also executed a proposal of that DAO; the title and thesis come from that proposal's description. An entry set any other way (no executed proposal in the transaction) opens nothing.
- At most **20 open debates per DAO**; an opening beyond that is ignored by the indexer (logged).

## 2. Messages (off-chain, EIP-712)

Domain: `{ name: "werule debates", version: "1", chainId: <the network's chainId>, verifyingContract: <the DAO's governor address> }`.

```
Argument(string debate, bytes32 parent, bool con, uint16 bps, string text, uint64 nonce)
Stake(string debate, bytes32 argument, uint16 bps, uint64 nonce)
Retire(string debate, bytes32 argument, uint64 nonce)
```

- `debate`: the 16-hex id. `parent` / `argument`: an argument id, or `0x00…00` for the thesis.
- An argument's id is its message digest: the EIP-712 hash (`keccak256("\x19\x01" ‖ domainSeparator ‖ structHash)`) of its `Argument` message.
- `Argument`: a reply to `parent`, pro (`con = false`) or con. `bps` (0 to 10000) is the share of the author's voting power staked on it at once; 0 is allowed. `text`: 1 to 4096 bytes of UTF-8.
- `Stake`: sets the author's share on `argument` (or the thesis) to `bps`; 0 withdraws it.
- `Retire`: the author withdraws their own argument (only the author's). A retired argument and everything below it stop counting; all stakes on them are released.
- `nonce`: per author and DAO, strictly increasing across all message types. Messages of one author are applied in nonce order; a nonce at or below the author's last accepted one is a replay and is rejected.

## 3. Validity (the indexer applies these; anyone can re-check)

A message is accepted when all hold, evaluated when it is applied:
1. The signature recovers to `author` for the domain above (the DAO's governor and the network's chainId).
2. The debate is open.
3. The author's current voting power in the DAO (`getVotes`, which the indexer mirrors as the member's `votingWeight`) is at least `max(1, proposalThreshold)`.
4. The nonce is above the author's last accepted nonce for that DAO.
5. `Argument`: the parent exists in this debate and is not retired; text within limits. `Stake` / `Retire`: the argument exists in this debate; `Retire` only by its author, and not already retired.
6. Budget: after the message, the sum of the author's `bps` over the debate's arguments that are not retired (and the thesis) is at most 10000.
7. Quotas: at most 2000 accepted messages per DAO per day.

Rejected messages are not published. The raw accepted messages are (section 5), so the whole state can be replayed and checked.

## 4. Tally

- An author's weight on an argument = `bps / 10000 × votingWeight` (current voting power). Recomputed on every accepted message and at least once a minute, so the debate follows the DAO as power moves.
- `direct(a)` = the sum of the weights on `a`.
- `score(a) = max(0, direct(a) + Σ score(pro children) − Σ score(con children))`, over children that are not retired. A refuted argument stops counting but never pushes its parent the other way; a con's surplus is a reserve against the cons beneath it.
- At the root: **support** = thesis direct weight + Σ score(pro children); **opposition** = Σ score(con children). Shown as two numbers (and a ratio), never floored together.

## 5. Published documents (under the DAO, like proposals)

- `{C}/{dao}/debates/{id}`: `id, title, thesis, link, proposalId, status ("open" | "retired"), openedAt, retiredAt, support, opposition, arguments (count), participants (count), lastMessageAt`.
- `{C}/{dao}/debates/{id}/arguments/{argId}`: `id, parent, con, author, text, createdAt, retired, direct, score, stakes ({author: bps})`.
- `{C}/{dao}/debates/{id}/messages/{digest}`: `type, author, message (the signed fields), signature, nonce, acceptedAt`: everything needed to replay.

Lists (publisher layout): `debates` top-level (order `openedAt` desc), nested `arguments` (order `createdAt` asc) and `messages` (order `acceptedAt` asc).

## 6. Intake: the Worker

`POST https://live.autonet.computer/debates` with JSON `{network, dao, type: "Argument"|"Stake"|"Retire", message: {…fields}, signature}`:
- Rejects at once (4xx with a reason): bad JSON, body over 8 KB, unknown network (from `networks.json`), malformed fields or text over 4096 bytes, a signature that does not recover, more than 10 messages a minute or 300 a day from one author on one network.
- Otherwise stores it in the intake queue (one Durable Object per network, SQLite, kept 7 days) and answers `202 {"seq": n, "author": "0x…", "digest": "0x…"}`. Acceptance into the queue is not acceptance into the debate: the indexer applies section 3.
- `GET /debates/queue?network=<N>&after=<seq>` (signed like `/notify`: `X-Edge-Timestamp`, `X-Edge-Signature` over the empty body): up to 500 queued entries after `seq`, `[{seq, dao, type, message, signature, author, digest, receivedAt}]`. Only the indexer calls it.

## 7. Indexer

The `debates` module, per network: opens and retires debates from registry updates (section 1), polls the Worker queue every 2 seconds (cursor in the store, `checkpoints`), applies section 3 in queue order (each author's messages in nonce order), writes section 5's documents, and recomputes open debates' tallies at least once a minute.

## 8. werule

- **Open a debate:** a proposal type with title and thesis (and link); it builds the registry action and the description.
- **Retire:** from the debate page, a proposal.
- **Debate page:** the thesis, support against opposition, the argument tree with scores, the participant's remaining share. Reply pro or con (with an optional share), set or withdraw a share on any argument, retire one's own argument. Each is signed in the wallet (`eth_signTypedData_v4`) and sent to the Worker; it shows as pending until it appears in the published files. The text field counts down from 4096 bytes. Posting requires voting power at least `max(1, proposalThreshold)`.
- The on-chain `Debate` and `DebatesFactory` contracts are gone, and with them the old debate screens and the indexer's on-chain debate events.
