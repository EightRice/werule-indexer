// EIP-712 digests and signer recovery for debate messages (DEBATES.md section 2).
// Imports only the noble libraries so the node unit tests can load it directly.

import { secp256k1 } from "@noble/curves/secp256k1";
import { keccak_256 } from "@noble/hashes/sha3";

export interface Field {
  name: string;
  type: string;
}

export type DebateType = "Argument" | "Stake" | "Retire";

// Build the base point table (about 30 ms) at isolate startup, not inside the first
// request's 10 ms CPU budget. Recovery itself then costs about 4 ms.
secp256k1.utils.precompute(8);

export const DOMAIN_NAME = "werule debates";
export const DOMAIN_VERSION = "1";

export const DOMAIN_FIELDS: Field[] = [
  { name: "name", type: "string" },
  { name: "version", type: "string" },
  { name: "chainId", type: "uint256" },
  { name: "verifyingContract", type: "address" },
];

export const DEBATE_TYPES: Record<DebateType, Field[]> = {
  Argument: [
    { name: "debate", type: "string" },
    { name: "parent", type: "bytes32" },
    { name: "con", type: "bool" },
    { name: "bps", type: "uint16" },
    { name: "text", type: "string" },
    { name: "nonce", type: "uint64" },
  ],
  Stake: [
    { name: "debate", type: "string" },
    { name: "argument", type: "bytes32" },
    { name: "bps", type: "uint16" },
    { name: "nonce", type: "uint64" },
  ],
  Retire: [
    { name: "debate", type: "string" },
    { name: "argument", type: "bytes32" },
    { name: "nonce", type: "uint64" },
  ],
};

const encoder = new TextEncoder();

export function bytesToHex(b: Uint8Array): string {
  let s = "0x";
  for (const x of b) s += x.toString(16).padStart(2, "0");
  return s;
}

/** `0x`-prefixed hex (either case) of exactly `len` bytes, or null. */
export function hexToBytes(hex: string, len: number): Uint8Array | null {
  if (typeof hex !== "string" || hex.length !== 2 + len * 2 || !/^0x[0-9a-fA-F]*$/.test(hex)) return null;
  const out = new Uint8Array(len);
  for (let i = 0; i < len; i++) out[i] = parseInt(hex.slice(2 + i * 2, 4 + i * 2), 16);
  return out;
}

export function keccak(data: Uint8Array | string): Uint8Array {
  return keccak_256(typeof data === "string" ? encoder.encode(data) : data);
}

/** `Name(type1 name1,type2 name2,...)`; these structs reference no other structs. */
export function encodeType(primary: string, fields: Field[]): string {
  return `${primary}(${fields.map((f) => `${f.type} ${f.name}`).join(",")})`;
}

export function typeHash(primary: string, fields: Field[]): Uint8Array {
  return keccak(encodeType(primary, fields));
}

function uintWord(v: unknown, bits: number): Uint8Array {
  let n: bigint;
  if (typeof v === "bigint") n = v;
  else if (typeof v === "number" && Number.isSafeInteger(v)) n = BigInt(v);
  else if (typeof v === "string" && /^(0|[1-9][0-9]*)$/.test(v)) n = BigInt(v);
  else throw new Error(`not a uint${bits}`);
  if (n < 0n || n >= 1n << BigInt(bits)) throw new Error(`out of range for uint${bits}`);
  const out = new Uint8Array(32);
  for (let i = 31; i >= 0 && n > 0n; i--) {
    out[i] = Number(n & 0xffn);
    n >>= 8n;
  }
  return out;
}

/** One 32-byte word of `encodeData` for an atomic or dynamic (string) type. */
export function encodeValue(type: string, v: unknown): Uint8Array {
  if (type === "string") {
    if (typeof v !== "string") throw new Error("not a string");
    return keccak(v);
  }
  if (type === "bytes32") {
    const b = typeof v === "string" ? hexToBytes(v, 32) : null;
    if (!b) throw new Error("not a bytes32");
    return b;
  }
  if (type === "bool") {
    if (typeof v !== "boolean") throw new Error("not a bool");
    const out = new Uint8Array(32);
    out[31] = v ? 1 : 0;
    return out;
  }
  if (type === "address") {
    const b = typeof v === "string" ? hexToBytes(v, 20) : null;
    if (!b) throw new Error("not an address");
    const out = new Uint8Array(32);
    out.set(b, 12);
    return out;
  }
  const m = /^uint(\d+)$/.exec(type);
  if (m) {
    const bits = Number(m[1]);
    if (bits < 8 || bits > 256 || bits % 8 !== 0) throw new Error(`unsupported type ${type}`);
    return uintWord(v, bits);
  }
  throw new Error(`unsupported type ${type}`);
}

/** keccak256(typeHash ‖ encodeData(values)). */
export function hashStruct(primary: string, fields: Field[], values: Record<string, unknown>): Uint8Array {
  const buf = new Uint8Array(32 * (fields.length + 1));
  buf.set(typeHash(primary, fields), 0);
  fields.forEach((f, i) => buf.set(encodeValue(f.type, values[f.name]), 32 * (i + 1)));
  return keccak(buf);
}

export function domainSeparator(chainId: bigint | number | string, verifyingContract: string): Uint8Array {
  return hashStruct("EIP712Domain", DOMAIN_FIELDS, {
    name: DOMAIN_NAME,
    version: DOMAIN_VERSION,
    chainId,
    verifyingContract,
  });
}

/** keccak256("\x19\x01" ‖ domainSeparator ‖ structHash). */
export function debateDigest(
  type: DebateType,
  message: Record<string, unknown>,
  chainId: bigint | number | string,
  dao: string,
): Uint8Array {
  const buf = new Uint8Array(66);
  buf[0] = 0x19;
  buf[1] = 0x01;
  buf.set(domainSeparator(chainId, dao), 2);
  buf.set(hashStruct(type, DEBATE_TYPES[type], message), 34);
  return keccak(buf);
}

/**
 * Recovers the lowercase `0x` address that signed `digest`, or null.
 * `signature` is 65 bytes r ‖ s ‖ v as `0x` hex, v in {27, 28} or {0, 1}.
 * High-s signatures are rejected, so each (digest, author) has exactly one signature.
 */
export function recoverAddress(digest: Uint8Array, signature: string): string | null {
  const raw = hexToBytes(signature, 65);
  if (!raw) return null;
  const v = raw[64];
  const recovery = v >= 27 ? v - 27 : v;
  if (recovery !== 0 && recovery !== 1) return null;
  try {
    const sig = secp256k1.Signature.fromCompact(raw.subarray(0, 64)).addRecoveryBit(recovery);
    if (sig.hasHighS()) return null;
    const pub = sig.recoverPublicKey(digest).toRawBytes(false);
    return bytesToHex(keccak(pub.subarray(1)).subarray(12));
  } catch {
    return null;
  }
}
