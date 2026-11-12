// Debate message signing for the tests (node --experimental-strip-types).
import { secp256k1 } from "@noble/curves/secp256k1";
import { bytesToHex, debateDigest, keccak } from "../src/eip712.ts";

// Hardhat account #0 (a public test key).
export const KEY0 = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
export const ADDR0 = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266";

export function addressOf(privHex) {
  const pub = secp256k1.getPublicKey(privHex, false);
  return bytesToHex(keccak(pub.subarray(1)).subarray(12));
}

/** eth_signTypedData_v4-style 65-byte signature (v = 27/28) over the debate digest. */
export function signDebate(privHex, type, message, chainId, dao) {
  const digest = debateDigest(type, message, chainId, dao);
  const sig = secp256k1.sign(digest, privHex);
  return { digest: bytesToHex(digest), signature: "0x" + sig.toCompactHex() + (27 + sig.recovery).toString(16) };
}

export function randomKey() {
  return bytesToHex(secp256k1.utils.randomPrivateKey()).slice(2);
}
