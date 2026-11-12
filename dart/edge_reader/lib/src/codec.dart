// lib/src/codec.dart
//
// Decoding of the edge files' document values (CONTRACT.md 3.1) and doc entries (3.2).

/// Decodes one encoded value: tagged single-key maps become Dart values, everything else is
/// decoded recursively.
Object? decodeValue(Object? v) {
  if (v is Map) {
    if (v.length == 1) {
      final tag = v.keys.first;
      final x = v.values.first;
      switch (tag) {
        case r'$int':
          return BigInt.parse(x as String);
        case r'$float':
          switch (x) {
            case 'NaN':
              return double.nan;
            case 'Infinity':
              return double.infinity;
            case '-Infinity':
              return double.negativeInfinity;
          }
          return double.parse(x as String);
        case r'$ts':
          return DateTime.parse(x as String).toUtc();
        case r'$bytes':
          return (x as String).toLowerCase();
        case r'$ref':
          return x as String;
        case r'$geo':
          return [for (final n in x as List) (n as num).toDouble()];
      }
    }
    return <String, dynamic>{for (final e in v.entries) e.key.toString(): decodeValue(e.value)};
  }
  if (v is List) return [for (final e in v) decodeValue(e)];
  return v;
}

Map<String, dynamic> decodeData(Object? data) =>
    data is Map ? decodeValue(data) as Map<String, dynamic> : <String, dynamic>{};

/// A decoded Timestamp as local time (what Firestore's `Timestamp.toDate()` returned), else null
DateTime? asLocalDate(Object? v) => v is DateTime ? v.toLocal() : null;

final _offset = RegExp(r'(Z|z|[+-]\d{2}(:?\d{2})?)$');

/// Microseconds since epoch of an ISO 8601 string; a value without an offset is UTC
int? isoMicros(String s) {
  var t = s.trim();
  if (t.isEmpty) return null;
  if (t.contains('T') || t.contains(' ')) {
    if (!_offset.hasMatch(t)) t = '${t}Z';
  }
  return DateTime.tryParse(t)?.microsecondsSinceEpoch;
}

/// One document as the publisher sends it, in a file or a push
class DocEntry {
  final String path;
  final String id;
  final int v;
  final Map<String, dynamic>? data; // decoded; null for deletes and stale markers
  final bool deleted;
  final bool stale;

  const DocEntry({required this.path, required this.id, required this.v, this.data, this.deleted = false, this.stale = false});

  /// Store key: the path in lowercase
  String get key => path.toLowerCase();

  bool get exists => !deleted && !stale && data != null;

  factory DocEntry.fromJson(Map<String, dynamic> json) {
    final path = json['path'] as String;
    final deleted = json['deleted'] == true;
    final stale = json['stale'] == true;
    return DocEntry(
      path: path,
      id: (json['id'] as String?) ?? path.split('/').last,
      v: (json['v'] as num).toInt(),
      data: deleted || stale ? null : decodeData(json['data']),
      deleted: deleted,
      stale: stale,
    );
  }

  @override
  String toString() => 'DocEntry($path, v=$v${deleted ? ', deleted' : ''}${stale ? ', stale' : ''})';
}

/// The sort value `k` of a doc for a list ordered by [key] (CONTRACT.md 4.4)
num sortValue(Map<String, dynamic>? data, String? key) {
  if (key == null || key.isEmpty || key == 'id' || key == '__name__' || data == null) return 0;
  final v = data[key];
  if (v is DateTime) return v.microsecondsSinceEpoch;
  if (v is String) return isoMicros(v) ?? 0;
  if (v is int) return v;
  if (v is double) return v.isFinite ? v : 0;
  if (v is BigInt) return v.toDouble();
  return 0;
}

/// Compares two `[k, id]` positions ascending
int comparePos(num ka, String ida, num kb, String idb) {
  final c = ka.compareTo(kb);
  return c != 0 ? c : ida.toLowerCase().compareTo(idb.toLowerCase());
}
