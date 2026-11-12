// Unit tests for the debates intake: EIP-712, recovery, body validation, rate limits.
import { test } from "node:test";
import assert from "node:assert/strict";
import { secp256k1 } from "@noble/curves/secp256k1";
import {
  DEBATE_TYPES, bytesToHex, debateDigest, domainSeparator, encodeType, encodeValue, hashStruct, recoverAddress, typeHash,
} from "../src/eip712.ts";
import {
  DebateError, MAX_TEXT_BYTES, parseAfter, parseDebateBody, parseNetworks, rateLimitError, MINUTE_MS, DAY_MS,
} from "../src/debates.ts";
import { ADDR0, KEY0, addressOf, randomKey, signDebate } from "./debate-helpers.mjs";

// Vector computed with ethers 6.15.0 (TypedDataEncoder, Wallet.signTypedData), hardhat account #0.
const CHAIN = 11155111;
const DAO = "0x5FbDB2315678afecb367f032d93F642f64180aa3";
const V = {
  domainSeparator: "0xb3a47b072667b11383caccfb5827542729eb012f457db016f501175366c95c6e",
  Argument: {
    message: { debate: "0123456789abcdef", parent: "0x" + "00".repeat(32), con: true, bps: 2500, text: "Héllo, wörld \u{1F30D}\nline two", nonce: "18446744073709551615" },
    encodeType: "Argument(string debate,bytes32 parent,bool con,uint16 bps,string text,uint64 nonce)",
    typeHash: "0x64b90b3584dd3f1c9de1533b0a78bc28ca77772e133009207e4d19d7dbd92d26",
    structHash: "0x550a95cb4cc2147a28a39390a0650efd778341c01c554e1b1ed6d588c9b43446",
    digest: "0x5a36483654a471e0e40871a83688da8d6b1d4d5c2996a836205f784c868c23bc",
    sig: "0x8cf921dc21b0f851e080b250a0aefb0482eba0ad94cd78d28d5f4c9390530cc12a003594e670efe0f0f6f2e53639ab1bf2146bc8f861307e900cb9d78cf3f4e21c",
  },
  Stake: {
    message: { debate: "fedcba9876543210", argument: "0x" + "ab".repeat(32), bps: 10000, nonce: "7" },
    encodeType: "Stake(string debate,bytes32 argument,uint16 bps,uint64 nonce)",
    typeHash: "0x2f881bd6f411b572b123a70882d3875418a6948a2bb40fc2114cdc53fea3bd4d",
    structHash: "0x62d4bd0e126334e5b18570a806985bc80633ae052eb57de8e07482c144843a4e",
    digest: "0xacc3c80995baf53a844d45ac4309ab0d6beaaa2f7b40290a45aa06dfc0239444",
    sig: "0xffdfe776865589d3530dac213919014259800e40f6e1ec013b90f83054c337961fbdfae47f6b4c58d5bf050c37a7fa7acf538f824e66560c6b69d886548711611b",
  },
  Retire: {
    message: { debate: "fedcba9876543210", argument: "0x" + "cd".repeat(32), nonce: "0" },
    encodeType: "Retire(string debate,bytes32 argument,uint64 nonce)",
    typeHash: "0x3a8100207ab1402ec2e204bfbfcd9b98d7facb611c160409dd5ddca7e0487f72",
    structHash: "0x1db6f85a901bd6489d405cf1ee028be62a6d856845722049e240052d22a57fc3",
    digest: "0x0764669a3de7aae9fc907a302a4c109b18ba7572310aa3b83cccc7ff34e7c9f9",
    sig: "0xc622b7c048253a7769ec071ca0352485e28a4b67674a63758d485fe66df04c3f44d87f48b7a8d40fcb839272c2e4e12b56b2851f694fb4907ccc3cb20d1a58a71b",
  },
};
const TYPES = ["Argument", "Stake", "Retire"];
const digestHex = (t, m = V[t].message, c = CHAIN, d = DAO) => bytesToHex(debateDigest(t, m, c, d));

test("EIP-712 encoding matches the ethers vector", () => {
  assert.equal(addressOf(KEY0), ADDR0);
  assert.equal(bytesToHex(domainSeparator(CHAIN, DAO)), V.domainSeparator);
  assert.equal(bytesToHex(domainSeparator(BigInt(CHAIN), DAO.toLowerCase())), V.domainSeparator);
  for (const t of TYPES) {
    assert.equal(encodeType(t, DEBATE_TYPES[t]), V[t].encodeType, t);
    assert.equal(bytesToHex(typeHash(t, DEBATE_TYPES[t])), V[t].typeHash, t);
    assert.equal(bytesToHex(hashStruct(t, DEBATE_TYPES[t], V[t].message)), V[t].structHash, t);
    assert.equal(digestHex(t), V[t].digest, t);
  }
});

test("EIP-712 value encoding edge cases", () => {
  assert.equal(bytesToHex(encodeValue("uint64", "18446744073709551615")), "0x" + "00".repeat(24) + "ff".repeat(8));
  assert.equal(bytesToHex(encodeValue("uint64", 7)), "0x" + "00".repeat(31) + "07");
  assert.equal(bytesToHex(encodeValue("bool", false)), "0x" + "00".repeat(32));
  assert.throws(() => encodeValue("uint64", "18446744073709551616"));
  assert.throws(() => encodeValue("uint16", 65536));
  assert.throws(() => encodeValue("uint16", -1));
  assert.throws(() => encodeValue("bytes32", "0x00"));
  assert.throws(() => encodeValue("bool", 1));
  // The nonce as a number or a string hashes the same.
  assert.equal(digestHex("Stake", { ...V.Stake.message, nonce: 7 }), V.Stake.digest);
});

test("recovery: the ethers signatures recover to the signer", () => {
  for (const t of TYPES) {
    assert.equal(recoverAddress(debateDigest(t, V[t].message, CHAIN, DAO), V[t].sig), ADDR0, t);
  }
  // v as 0/1 is accepted too; uppercase hex too.
  const s = V.Stake.sig;
  const v01 = s.slice(0, -2) + (parseInt(s.slice(-2), 16) - 27).toString(16).padStart(2, "0");
  const d = debateDigest("Stake", V.Stake.message, CHAIN, DAO);
  assert.equal(recoverAddress(d, v01), ADDR0);
  assert.equal(recoverAddress(d, "0x" + s.slice(2).toUpperCase()), ADDR0);
});

test("recovery: our signer round-trips with random keys", () => {
  for (let i = 0; i < 5; i++) {
    const k = randomKey();
    const { digest, signature } = signDebate(k, "Argument", V.Argument.message, 31337, DAO);
    assert.equal(recoverAddress(Uint8Array.from(Buffer.from(digest.slice(2), "hex")), signature), addressOf(k));
  }
  // And matches the ethers signature byte for byte (RFC 6979 deterministic nonces).
  assert.equal(signDebate(KEY0, "Retire", V.Retire.message, CHAIN, DAO).signature, V.Retire.sig);
});

test("recovery: wrong signature, wrong chainId, wrong verifyingContract do not give the signer", () => {
  const t = "Argument";
  const d = debateDigest(t, V[t].message, CHAIN, DAO);
  const sig = V[t].sig;
  // A flipped byte in s: recovers someone else, or nothing.
  const flipped = sig.slice(0, 80) + (sig[80] === "0" ? "1" : "0") + sig.slice(81);
  assert.notEqual(recoverAddress(d, flipped), ADDR0);
  // Other recovery bit: another key.
  const otherV = sig.slice(0, -2) + (sig.endsWith("1b") ? "1c" : "1b");
  assert.notEqual(recoverAddress(d, otherV), ADDR0);
  // Same signature, other domain.
  assert.notEqual(recoverAddress(debateDigest(t, V[t].message, 1, DAO), sig), ADDR0);
  assert.notEqual(recoverAddress(debateDigest(t, V[t].message, CHAIN, "0x" + "11".repeat(20)), sig), ADDR0);
  // Other message (one more nonce).
  assert.notEqual(recoverAddress(debateDigest(t, { ...V[t].message, nonce: "1" }, CHAIN, DAO), sig), ADDR0);
  // Malformed: bad v, wrong length, r = 0, s >= n, high s.
  assert.equal(recoverAddress(d, sig.slice(0, -2) + "1d"), null);
  assert.equal(recoverAddress(d, sig.slice(0, -2)), null);
  assert.equal(recoverAddress(d, "0x" + "00".repeat(32) + sig.slice(66)), null);
  assert.equal(recoverAddress(d, sig.slice(0, 66) + "ff".repeat(32) + sig.slice(-2)), null);
  const n = secp256k1.CURVE.n;
  const s = BigInt("0x" + sig.slice(66, 130));
  const high = (n - s).toString(16).padStart(64, "0");
  const flipV = sig.endsWith("1b") ? "1c" : "1b";
  assert.equal(recoverAddress(d, sig.slice(0, 66) + high + flipV), null, "high-s twin rejected");
});

const valid = {
  Argument: { network: "Sepolia", dao: DAO, type: "Argument", message: V.Argument.message, signature: V.Argument.sig },
  Stake: { network: "Sepolia", dao: DAO, type: "Stake", message: V.Stake.message, signature: V.Stake.sig },
  Retire: { network: "Sepolia", dao: DAO, type: "Retire", message: V.Retire.message, signature: V.Retire.sig },
};

test("body validation accepts the three types and normalizes", () => {
  for (const t of TYPES) {
    const s = parseDebateBody(JSON.stringify(valid[t]));
    assert.equal(s.type, t);
    assert.equal(s.dao, DAO.toLowerCase());
    assert.deepEqual(Object.keys(s.message), DEBATE_TYPES[t].map((f) => f.name));
    assert.equal(bytesToHex(debateDigest(t, s.message, CHAIN, s.dao)), V[t].digest, t);
  }
  const up = parseDebateBody(JSON.stringify({ ...valid.Stake, message: { ...V.Stake.message, argument: "0x" + "AB".repeat(32), nonce: 7 }, signature: V.Stake.sig.toUpperCase().replace("0X", "0x") }));
  assert.equal(up.message.argument, "0x" + "ab".repeat(32));
  assert.equal(up.message.nonce, "7");
  assert.equal(up.signature, V.Stake.sig);
  // Text of exactly 4096 bytes (multi-byte characters count as bytes) and bps 0 / 10000.
  const ok4096 = "é".repeat(2048);
  parseDebateBody(JSON.stringify({ ...valid.Argument, message: { ...V.Argument.message, text: ok4096, bps: 0 } }));
  parseDebateBody(JSON.stringify({ ...valid.Argument, message: { ...V.Argument.message, text: "x", bps: 10000 } }));
});

test("body validation rejects malformed bodies", () => {
  const bad = (body, re) => {
    const text = typeof body === "string" ? body : JSON.stringify(body);
    assert.throws(() => parseDebateBody(text), (e) => e instanceof DebateError && e.status === 400 && (!re || re.test(e.message)), text.slice(0, 120));
  };
  const A = valid.Argument;
  const M = V.Argument.message;
  bad("{not json", /JSON/);
  bad("[]");
  bad("null");
  bad({ ...A, extra: 1 }, /unexpected/);
  bad({ ...A, network: undefined }, /missing/);
  bad({ ...A, network: "bad_name" });
  bad({ ...A, network: "x".repeat(65) });
  bad({ ...A, dao: "0x1234" });
  bad({ ...A, dao: DAO.slice(2) });
  bad({ ...A, type: "Vote" });
  bad({ ...A, type: "argument" });
  bad({ ...A, signature: A.signature.slice(0, -2) });
  bad({ ...A, signature: A.signature.slice(2) });
  bad({ ...A, message: "x" });
  bad({ ...A, message: { ...M, extra: 1 } }, /unexpected/);
  bad({ ...A, message: { ...M, argument: M.parent } }, /unexpected/);
  bad({ ...valid.Stake, message: { ...V.Stake.message, con: true } }, /unexpected/);
  bad({ ...valid.Retire, message: { ...V.Retire.message, bps: 1 } }, /unexpected/);
  bad({ ...A, message: { ...M, debate: "0123456789ABCDEF" } }, /debate/);
  bad({ ...A, message: { ...M, debate: "0123456789abcde" } }, /debate/);
  bad({ ...A, message: { ...M, debate: "0x0123456789abcdef" } }, /debate/);
  bad({ ...A, message: { ...M, parent: "0x" + "00".repeat(31) } }, /parent/);
  bad({ ...A, message: { ...M, parent: "00".repeat(32) } }, /parent/);
  bad({ ...A, message: { ...M, con: "false" } }, /con/);
  bad({ ...A, message: { ...M, con: 0 } }, /con/);
  for (const bps of [-1, 10001, 1.5, "100", null]) bad({ ...A, message: { ...M, bps } }, /bps/);
  bad({ ...A, message: { ...M, text: "" } }, /text/);
  bad({ ...A, message: { ...M, text: "x".repeat(MAX_TEXT_BYTES + 1) } }, /text/);
  bad({ ...A, message: { ...M, text: "é".repeat(2048) + "x" } }, /text/);
  bad('{"network":"Sepolia","dao":"' + DAO + '","type":"Argument","signature":"' + A.signature + '","message":{"debate":"0123456789abcdef","parent":"' + M.parent + '","con":true,"bps":1,"text":"lone \\ud800 surrogate","nonce":"1"}}', /text/);
  bad({ ...A, message: { ...M, text: 5 } }, /text/);
  for (const nonce of [-1, 1.5, "01", "-1", "1e3", " 1", "18446744073709551616", 2 ** 53, "", null]) {
    bad({ ...A, message: { ...M, nonce } }, /nonce/);
  }
});

test("networks.json parsing", () => {
  const m = parseNetworks({
    format: 1,
    networks: [
      { id: "Sepolia", data: { chainId: 11155111, rpc: "x" } },
      { id: "Str", data: { chainId: "31337" } },
      { id: "NoChain", data: { rpc: "x" } },
      { id: "Zero", data: { chainId: 0 } },
      { id: "Bad", data: { chainId: "0x1" } },
      "junk",
    ],
  });
  assert.deepEqual([...m], [["Sepolia", 11155111n], ["Str", 31337n]]);
  assert.throws(() => parseNetworks({ format: 1 }));
});

test("rate limits: 10 a minute, 300 a day, per author", () => {
  const now = 10 * DAY_MS;
  const within = (n, spacing, from = now) => Array.from({ length: n }, (_, i) => from - 1 - i * spacing);
  assert.equal(rateLimitError([], now), null);
  assert.equal(rateLimitError(within(9, 1000), now), null);
  assert.match(rateLimitError(within(10, 1000), now), /minute/);
  // Ten, but the oldest is exactly a minute old: it no longer counts.
  assert.equal(rateLimitError([...within(9, 1000), now - MINUTE_MS], now), null);
  // 299 spread over the day: fine; 300: refused; older ones ignored.
  const spread = (n) => Array.from({ length: n }, (_, i) => now - MINUTE_MS - 1 - i * 60_000);
  assert.equal(rateLimitError(spread(299), now), null);
  assert.match(rateLimitError(spread(300), now), /day/);
  assert.equal(rateLimitError([...spread(299), now - DAY_MS, now - 2 * DAY_MS], now), null);
});

test("after parsing", () => {
  assert.equal(parseAfter(null), 0);
  assert.equal(parseAfter(""), 0);
  assert.equal(parseAfter("42"), 42);
  assert.equal(parseAfter("-1"), undefined);
  assert.equal(parseAfter("1.5"), undefined);
  assert.equal(parseAfter("9007199254740992"), undefined);
});

test("CPU: parse + digest + recovery of a 4096-byte Argument stays under 10 ms (guard at 8)", () => {
  const msg = { ...V.Argument.message, text: "é".repeat(2048) };
  const { signature } = signDebate(KEY0, "Argument", msg, CHAIN, DAO);
  const body = JSON.stringify({ network: "Sepolia", dao: DAO, type: "Argument", message: msg, signature });
  const run = () => {
    const s = parseDebateBody(body);
    assert.equal(recoverAddress(debateDigest(s.type, s.message, BigInt(CHAIN), s.dao), s.signature), ADDR0);
  };
  let t0 = process.cpuUsage();
  run();
  let d = process.cpuUsage(t0);
  console.log(`# first call after import (base table built at module load): ${((d.user + d.system) / 1000).toFixed(2)} ms CPU`);
  for (let i = 0; i < 20; i++) run();
  const N = 100;
  t0 = process.cpuUsage();
  for (let i = 0; i < N; i++) run();
  d = process.cpuUsage(t0);
  const ms = (d.user + d.system) / 1000 / N;
  console.log(`# debates POST verification: ${ms.toFixed(2)} ms CPU per request`);
  assert.ok(ms < 8, `${ms} ms`);
});
