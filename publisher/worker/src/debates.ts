// Debates intake: request validation and rate-limit rules (DEBATES.md sections 2 and 6).
// Self-contained (no imports) so the node unit tests can load it directly.

export const MAX_DEBATE_BODY_BYTES = 8 * 1024;
export const MAX_TEXT_BYTES = 4096;
export const MAX_BPS = 10000;
export const RATE_PER_MINUTE = 10;
export const RATE_PER_DAY = 300;
export const MINUTE_MS = 60 * 1000;
export const DAY_MS = 24 * 60 * 60 * 1000;
export const QUEUE_RETENTION_MS = 7 * DAY_MS;
export const QUEUE_PAGE = 500;

const UINT64_MAX = (1n << 64n) - 1n;
const NETWORK_RE = /^[A-Za-z0-9-]{1,64}$/;
const DEBATE_RE = /^[0-9a-f]{16}$/;
const BYTES32_RE = /^0x[0-9a-fA-F]{64}$/;
const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;
const SIGNATURE_RE = /^0x[0-9a-fA-F]{130}$/;
const NONCE_RE = /^(0|[1-9][0-9]{0,19})$/;

export type DebateType = "Argument" | "Stake" | "Retire";

const FIELDS: Record<DebateType, string[]> = {
  Argument: ["debate", "parent", "con", "bps", "text", "nonce"],
  Stake: ["debate", "argument", "bps", "nonce"],
  Retire: ["debate", "argument", "nonce"],
};

/** The signed fields, normalized: hex lowercased, nonce as a decimal string, keys in type order. */
export type DebateMessage = Record<string, string | number | boolean>;

export interface DebateSubmission {
  network: string;
  /** Lowercase governor address (the domain's verifyingContract). */
  dao: string;
  type: DebateType;
  message: DebateMessage;
  /** Lowercase 65-byte hex. */
  signature: string;
}

export class DebateError extends Error {
  status: number;
  constructor(message: string, status = 400) {
    super(message);
    this.status = status;
  }
}

const encoder = new TextEncoder();

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function exactKeys(obj: Record<string, unknown>, keys: string[], where: string): void {
  for (const k of Object.keys(obj)) {
    if (!keys.includes(k)) throw new DebateError(`${where}: unexpected field ${k.slice(0, 40)}`);
  }
  for (const k of keys) {
    if (!(k in obj)) throw new DebateError(`${where}: missing field ${k}`);
  }
}

/** True when the string has no unpaired UTF-16 surrogate (so its UTF-8 encoding is exact). */
export function isWellFormed(s: string): boolean {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const d = s.charCodeAt(i + 1);
      if (!(d >= 0xdc00 && d <= 0xdfff)) return false;
      i++;
    } else if (c >= 0xdc00 && c <= 0xdfff) {
      return false;
    }
  }
  return true;
}

function parseNonce(v: unknown): string {
  let s: string;
  if (typeof v === "number") {
    if (!Number.isSafeInteger(v) || v < 0) throw new DebateError("message.nonce must be a uint64");
    s = String(v);
  } else if (typeof v === "string" && NONCE_RE.test(v)) {
    s = v;
  } else {
    throw new DebateError("message.nonce must be a uint64 (decimal string or number)");
  }
  if (BigInt(s) > UINT64_MAX) throw new DebateError("message.nonce must be a uint64");
  return s;
}

function parseMessage(type: DebateType, raw: unknown): DebateMessage {
  if (!isPlainObject(raw)) throw new DebateError("message must be an object");
  exactKeys(raw, FIELDS[type], "message");
  const out: DebateMessage = {};
  for (const k of FIELDS[type]) {
    const v = raw[k];
    switch (k) {
      case "debate":
        if (typeof v !== "string" || !DEBATE_RE.test(v)) throw new DebateError("message.debate must be 16 lowercase hex");
        out[k] = v;
        break;
      case "parent":
      case "argument":
        if (typeof v !== "string" || !BYTES32_RE.test(v)) throw new DebateError(`message.${k} must be 0x + 64 hex`);
        out[k] = v.toLowerCase();
        break;
      case "con":
        if (typeof v !== "boolean") throw new DebateError("message.con must be a boolean");
        out[k] = v;
        break;
      case "bps":
        if (typeof v !== "number" || !Number.isInteger(v) || v < 0 || v > MAX_BPS) {
          throw new DebateError("message.bps must be an integer 0..10000");
        }
        out[k] = v;
        break;
      case "text": {
        if (typeof v !== "string" || !isWellFormed(v)) throw new DebateError("message.text must be a UTF-8 string");
        const n = encoder.encode(v).byteLength;
        if (n < 1 || n > MAX_TEXT_BYTES) throw new DebateError("message.text must be 1..4096 UTF-8 bytes");
        out[k] = v;
        break;
      }
      case "nonce":
        out[k] = parseNonce(v);
        break;
    }
  }
  return out;
}

/** Parses and validates the POST /debates body text (already known to be at most 8 KB of UTF-8). */
export function parseDebateBody(text: string): DebateSubmission {
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    throw new DebateError("body is not JSON");
  }
  if (!isPlainObject(body)) throw new DebateError("body must be an object");
  exactKeys(body, ["network", "dao", "type", "message", "signature"], "body");
  const { network, dao, type, message, signature } = body;
  if (typeof network !== "string" || !NETWORK_RE.test(network)) throw new DebateError("bad network");
  if (typeof dao !== "string" || !ADDRESS_RE.test(dao)) throw new DebateError("dao must be 0x + 40 hex");
  if (type !== "Argument" && type !== "Stake" && type !== "Retire") {
    throw new DebateError("type must be Argument, Stake or Retire");
  }
  if (typeof signature !== "string" || !SIGNATURE_RE.test(signature)) {
    throw new DebateError("signature must be 0x + 130 hex");
  }
  return {
    network,
    dao: dao.toLowerCase(),
    type,
    message: parseMessage(type, message),
    signature: signature.toLowerCase(),
  };
}

/** Network name, as the queue route accepts it. */
export function isValidNetwork(v: unknown): v is string {
  return typeof v === "string" && NETWORK_RE.test(v);
}

/**
 * networks.json (publisher CONTRACT.md 4.1) to a map of network id to chainId.
 * Networks without a usable positive integer `data.chainId` are left out.
 */
export function parseNetworks(json: unknown): Map<string, bigint> {
  const out = new Map<string, bigint>();
  if (!isPlainObject(json) || !Array.isArray(json.networks)) throw new Error("networks.json: no networks array");
  for (const n of json.networks) {
    if (!isPlainObject(n) || typeof n.id !== "string" || !isPlainObject(n.data)) continue;
    const c = n.data.chainId;
    let id: bigint | null = null;
    if (typeof c === "number" && Number.isSafeInteger(c) && c > 0) id = BigInt(c);
    else if (typeof c === "string" && /^[1-9][0-9]{0,30}$/.test(c)) id = BigInt(c);
    if (id !== null) out.set(n.id, id);
  }
  return out;
}

/**
 * Given the receive times (ms) of an author's queued entries on one network, the reason
 * a new one at `now` is refused, or null when it is within 10 per minute and 300 per day.
 */
export function rateLimitError(times: number[], now: number): string | null {
  let minute = 0;
  let day = 0;
  for (const t of times) {
    if (t > now - DAY_MS) day++;
    if (t > now - MINUTE_MS) minute++;
  }
  if (minute >= RATE_PER_MINUTE) return `rate limit: ${RATE_PER_MINUTE} messages a minute`;
  if (day >= RATE_PER_DAY) return `rate limit: ${RATE_PER_DAY} messages a day`;
  return null;
}

/** `after` query value: absent means 0, otherwise a non-negative safe integer. */
export function parseAfter(raw: string | null): number | undefined {
  if (raw === null || raw === "") return 0;
  if (!/^\d{1,16}$/.test(raw)) return undefined;
  const n = Number(raw);
  return Number.isSafeInteger(n) ? n : undefined;
}
