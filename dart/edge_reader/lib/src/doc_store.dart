// lib/src/doc_store.dart

import 'codec.dart';

/// Every document seen this session, keyed by lowercase path. For each key the entry with the
/// highest `v` wins (a tie keeps the existing one), and deletes stay as tombstones, so the order
/// in which files and pushes arrive does not matter (CONTRACT.md 3.2).
class DocStore {
  final Map<String, DocEntry> _docs = {};

  /// The winning entry for [key] (possibly a tombstone), or null if never seen
  DocEntry? entry(String key) => _docs[key.toLowerCase()];

  /// The live entry for [key]: null if never seen or deleted
  DocEntry? live(String key) {
    final e = _docs[key.toLowerCase()];
    return e != null && e.exists ? e : null;
  }

  /// Merges [e]; true when it replaced what was stored. Stale markers are never stored.
  bool merge(DocEntry e) {
    if (e.stale) return false;
    final key = e.key;
    final old = _docs[key];
    if (old != null && e.v <= old.v) return false;
    _docs[key] = e;
    return true;
  }

  int get length => _docs.length;
}
