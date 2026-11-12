// One Durable Object per network: the debates intake queue (DEBATES.md section 6).
// SQLite storage. Reading a queue that never received a message never writes storage.

import { DurableObject } from "cloudflare:workers";
import { QUEUE_PAGE, QUEUE_RETENTION_MS, DAY_MS, rateLimitError } from "./debates";
import type { DebateType } from "./debates";

export interface QueueInput {
  dao: string;
  type: DebateType;
  /** JSON text of the normalized signed fields. */
  message: string;
  signature: string;
  author: string;
  digest: string;
}

export type SubmitResult =
  | { ok: true; seq: number }
  | { ok: false; status: 429; error: string };

export interface QueueEntry {
  seq: number;
  dao: string;
  type: DebateType;
  message: Record<string, unknown>;
  signature: string;
  author: string;
  digest: string;
  receivedAt: number;
}

export class DebateQueue extends DurableObject<Record<string, unknown>> {
  private schemaReady = false;

  private get sql(): SqlStorage {
    return this.ctx.storage.sql;
  }

  private hasSchema(): boolean {
    if (this.schemaReady) return true;
    this.schemaReady =
      this.sql.exec("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'entries'").toArray().length > 0;
    return this.schemaReady;
  }

  private ensureSchema(): void {
    if (this.schemaReady) return;
    // `meta.last_seq` keeps seq increasing even after pruning empties the table.
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS meta (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_seq INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS entries (
        seq INTEGER PRIMARY KEY,
        dao TEXT NOT NULL,
        type TEXT NOT NULL,
        message TEXT NOT NULL,
        signature TEXT NOT NULL,
        author TEXT NOT NULL,
        digest TEXT NOT NULL,
        received_at INTEGER NOT NULL,
        UNIQUE (digest, author)
      );
      CREATE INDEX IF NOT EXISTS entries_author ON entries (author, received_at);
      CREATE INDEX IF NOT EXISTS entries_received ON entries (received_at);
    `);
    this.schemaReady = true;
  }

  /**
   * Stores one verified message. The same (digest, author) again is answered with its
   * existing seq (a retried POST), without counting against the rate limits.
   * Everything here is synchronous, so checks and insert are atomic.
   */
  async submit(e: QueueInput): Promise<SubmitResult> {
    this.ensureSchema();
    const now = Date.now();
    this.sql.exec("DELETE FROM entries WHERE received_at < ?", now - QUEUE_RETENTION_MS);

    const existing = this.sql
      .exec<{ seq: number }>("SELECT seq FROM entries WHERE digest = ? AND author = ?", e.digest, e.author)
      .toArray();
    if (existing.length > 0) return { ok: true, seq: existing[0].seq };

    const times = this.sql
      .exec<{ t: number }>(
        "SELECT received_at AS t FROM entries WHERE author = ? AND received_at > ?",
        e.author,
        now - DAY_MS,
      )
      .toArray()
      .map((r) => r.t);
    const limited = rateLimitError(times, now);
    if (limited) return { ok: false, status: 429, error: limited };

    const last = this.sql.exec<{ s: number }>("SELECT last_seq AS s FROM meta WHERE id = 1").toArray();
    const seq = (last.length ? last[0].s : 0) + 1;
    this.sql.exec(
      "INSERT INTO meta (id, last_seq) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET last_seq = excluded.last_seq",
      seq,
    );
    this.sql.exec(
      `INSERT INTO entries (seq, dao, type, message, signature, author, digest, received_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      seq, e.dao, e.type, e.message, e.signature, e.author, e.digest, now,
    );
    return { ok: true, seq };
  }

  /** Up to 500 entries with seq > after, in seq order. Read-only. */
  async list(after: number): Promise<QueueEntry[]> {
    if (!this.hasSchema()) return [];
    const rows = this.sql
      .exec<Record<string, SqlStorageValue>>(
        `SELECT seq, dao, type, message, signature, author, digest, received_at
         FROM entries WHERE seq > ? AND received_at >= ? ORDER BY seq LIMIT ?`,
        after,
        Date.now() - QUEUE_RETENTION_MS,
        QUEUE_PAGE,
      )
      .toArray();
    return rows.map((r) => ({
      seq: r.seq as number,
      dao: r.dao as string,
      type: r.type as DebateType,
      message: JSON.parse(r.message as string),
      signature: r.signature as string,
      author: r.author as string,
      digest: r.digest as string,
      receivedAt: r.received_at as number,
    }));
  }
}
