import 'package:flutter_test/flutter_test.dart';
import 'package:edge_reader/src/codec.dart';
import 'package:edge_reader/src/doc_store.dart';

DocEntry e(String path, int v, {bool deleted = false, Map<String, dynamic>? data}) =>
    DocEntry(path: path, id: path.split('/').last, v: v, deleted: deleted, data: deleted ? null : (data ?? {'v': v}));

void main() {
  group('decodeValue', () {
    test('tagged values', () {
      final d = decodeData({
        'big': {r'$int': '12345678901234567890'},
        'nan': {r'$float': 'NaN'},
        'inf': {r'$float': '-Infinity'},
        'at': {r'$ts': '2026-10-13T12:00:00.123456Z'},
        'raw': {r'$bytes': '0xDEADBEEF'},
        'ref': {r'$ref': 'a/b'},
        'geo': {r'$geo': [1, 2.5]},
        'nested': {
          'list': [
            {r'$ts': '2026-01-01T00:00:00.000000Z'},
            3,
          ],
          'two': {r'$int': '1', 'x': 2}, // two keys: a plain map
        },
        'n': 5,
        'f': 1.5,
      });
      expect(d['big'], BigInt.parse('12345678901234567890'));
      expect((d['nan'] as double).isNaN, isTrue);
      expect(d['inf'], double.negativeInfinity);
      expect(d['at'], DateTime.utc(2026, 10, 13, 12, 0, 0, 123, 456));
      expect((d['at'] as DateTime).isUtc, isTrue);
      expect(d['raw'], '0xdeadbeef');
      expect(d['ref'], 'a/b');
      expect(d['geo'], [1.0, 2.5]);
      expect((d['nested'] as Map)['list'], [DateTime.utc(2026), 3]);
      expect((d['nested'] as Map)['two'], {r'$int': '1', 'x': 2});
      expect(d['n'], 5);
      expect(d['f'], 1.5);
    });

    test('sort values: timestamps, ISO strings (naive is UTC), missing is 0', () {
      final at = DateTime.utc(2026, 10, 13, 12);
      expect(sortValue({'t': at}, 't'), at.microsecondsSinceEpoch);
      expect(sortValue({'t': '2026-10-13T12:00:00+00:00'}, 't'), at.microsecondsSinceEpoch);
      expect(sortValue({'t': '2026-10-13T12:00:00'}, 't'), at.microsecondsSinceEpoch);
      expect(sortValue({'t': '2026-10-13T14:00:00.5+02:00'}, 't'), at.microsecondsSinceEpoch + 500000);
      expect(sortValue({'t': 'garbage'}, 't'), 0);
      expect(sortValue({}, 't'), 0);
      expect(sortValue({'t': 7}, ''), 0);
    });
  });

  group('DocStore merge', () {
    test('highest v wins regardless of arrival order', () {
      final a = DocStore()..merge(e('c/x', 1))..merge(e('c/x', 3))..merge(e('c/x', 2));
      final b = DocStore()..merge(e('c/x', 3))..merge(e('c/x', 1))..merge(e('c/x', 2));
      expect(a.live('c/x')!.v, 3);
      expect(b.live('c/x')!.v, 3);
    });

    test('a tie keeps the existing entry', () {
      final s = DocStore()..merge(e('c/x', 5, data: {'a': 1}));
      expect(s.merge(e('c/x', 5, data: {'a': 2})), isFalse);
      expect(s.live('c/x')!.data, {'a': 1});
    });

    test('a tombstone hides older data, a newer re-creation wins over it', () {
      final s = DocStore()
        ..merge(e('c/x', 5, deleted: true))
        ..merge(e('c/x', 4)); // an old file arriving late
      expect(s.live('c/x'), isNull);
      expect(s.entry('c/x')!.deleted, isTrue);
      s.merge(e('c/x', 6));
      expect(s.live('c/x')!.v, 6);
    });

    test('keys are case-insensitive paths', () {
      final s = DocStore()..merge(e('idaosN/0xABC/proposals/1', 1));
      expect(s.live('idaosn/0xabc/proposals/1'), isNotNull);
    });

    test('stale markers are never stored', () {
      final s = DocStore();
      expect(s.merge(const DocEntry(path: 'c/x', id: 'x', v: 9, stale: true)), isFalse);
      expect(s.entry('c/x'), isNull);
    });
  });
}
