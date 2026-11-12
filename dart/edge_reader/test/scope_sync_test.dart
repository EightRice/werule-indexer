import 'dart:async';

import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:edge_reader/src/codec.dart';
import 'package:edge_reader/src/edge_client.dart';
import 'package:edge_reader/src/edge_list.dart';

import 'fakes.dart';

const t = 1000000; // one second in microseconds

/// pages [[1,2],[3,4],[5]] at head H1, seq 100, with a loc view
void publishH1(FakeFetcher f) {
  final p = [for (var i = 1; i <= 5; i++) proposal('$i', i * 100 * t)];
  final loc = f.obj('loc1', {
    'loc': {'1': 0, '2': 0, '3': 1, '4': 1, '5': 2}
  });
  daoHead(f, 'H1', proposals: proposalsIndex(f, 'a', [[p[0], p[1]], [p[2], p[3]], [p[4]]], views: {'loc': loc}));
  f.pointer(daoPtr, 100, 'H1');
}

EdgeList<String> titles(EdgeClient client, {int pageSize = 1}) =>
    EdgeList<String>(client, net, daoLower, 'proposals', (id, d) => '$id:${d['title']}', pageSize: pageSize);

List<String> ids(EdgeList<String> l) => [for (final s in l.items) s.split(':').first];

void main() {
  test('first load from hello, newest page first, older pages on loadMore', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      expect(c.last.uri.queryParameters, {'channel': 'dao:$net:$daoLower'});
      expect(list.ready, isFalse);
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      expect(list.ready, isTrue);
      expect(ids(list), ['5']);
      expect(list.hasMore, isTrue);
      expect(list.totalCount, 5);
      list.loadMore();
      async.flushMicrotasks();
      expect(ids(list), ['5', '4', '3']);
      list.loadMore();
      async.flushMicrotasks();
      expect(ids(list), ['5', '4', '3', '2', '1']);
      expect(list.hasMore, isFalse);
      expect(f.count(daoPtr), 0, reason: 'the hello carried the head');
      list.dispose();
      client.dispose();
    });
  });

  test('pushes: new doc on top, update in place, delete, older than the window stays out', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      expect(ids(list), ['5']);

      c.last.frame(changes(101, 'H2', [proposal('6', 600 * t)], counts: {'proposals': 6}));
      async.flushMicrotasks();
      expect(ids(list), ['6', '5']);
      expect(list.totalCount, 6, reason: 'counts come from the latest frame');

      c.last.frame(changes(102, 'H3', [proposal('5', 500 * t, v: 2, title: 'renamed')]));
      async.flushMicrotasks();
      expect(list.items, ['6:P6', '5:renamed']);

      // an old file arriving late does not overwrite the newer push
      final scope = client.acquire(net, daoLower);
      scope.applyChanges(changes(90, 'H1', [proposal('5', 500 * t, v: 1, title: 'old')]));
      async.flushMicrotasks();
      expect(list.items, ['6:P6', '5:renamed']);

      c.last.frame(changes(103, 'H4', [deletion('$daoPath/proposals/6', 10)]));
      async.flushMicrotasks();
      expect(ids(list), ['5']);

      // below the loaded window ([500, '5']): shown only once its page is loaded
      c.last.frame(changes(104, 'H5', [proposal('0', 50 * t)]));
      async.flushMicrotasks();
      expect(ids(list), ['5']);

      client.release(scope);
      list.dispose();
      client.dispose();
    });
  });

  test('seq never moves backwards: an older pointer does not roll the list back', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      c.last.frame(changes(101, 'H2', [proposal('6', 600 * t)]));
      async.flushMicrotasks();

      final scope = client.acquire(net, daoLower);
      expect(scope.seq, 101);
      // the edge-cached pointer still says seq 100, H1
      scope.refreshFromPointer();
      async.flushMicrotasks();
      expect(scope.seq, 101);
      expect(scope.headHash, 'H2');
      expect(ids(list), ['6', '5']);

      // an older hello (another socket) is ignored too
      c.last.frame(hello(99, 'H1'));
      async.flushMicrotasks();
      expect(scope.headHash, 'H2');
      expect(ids(list), ['6', '5']);
      client.release(scope);
      list.dispose();
      client.dispose();
    });
  });

  test('socket closed: reconnect with since; replay:true refetches nothing', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      c.last.frame(changes(101, 'H2', [proposal('6', 600 * t)]));
      async.flushMicrotasks();
      final before = f.requests.length;

      c.last.drop();
      async.flushMicrotasks();
      async.elapse(const Duration(seconds: 2));
      expect(c.sockets.length, 2);
      expect(c.last.since, '101');
      c.last.frame(hello(102, 'H3', replay: true));
      c.last.frame(changes(102, 'H3', [proposal('7', 700 * t)]));
      async.flushMicrotasks();
      expect(ids(list), ['7', '6', '5']);
      expect(f.requests.length, before, reason: 'nothing was lost, nothing is re-read');
      list.dispose();
      client.dispose();
    });
  });

  test('replay:false re-bases loaded lists: a doc deleted during the gap disappears, later pushes show', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      // H3 (seq 110): 5 and 6 were deleted while disconnected, 8 was added
      final p = [for (var i = 1; i <= 4; i++) proposal('$i', i * 100 * t)];
      daoHead(f, 'H3', proposals: proposalsIndex(f, 'c', [[p[0], p[1]], [p[2], p[3]], [proposal('8', 800 * t)]]));
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      c.last.frame(changes(101, 'H2', [proposal('6', 600 * t)]));
      async.flushMicrotasks();
      expect(ids(list), ['6', '5']);

      c.last.drop();
      async.flushMicrotasks();
      async.elapse(const Duration(seconds: 2));
      c.last.frame(hello(110, 'H3'));
      async.flushMicrotasks();
      expect(ids(list), ['8'], reason: 'membership comes from the new pages');

      c.last.frame(changes(111, 'H4', [proposal('9', 900 * t)]));
      async.flushMicrotasks();
      expect(ids(list), ['9', '8']);
      list.dispose();
      client.dispose();
    });
  });

  test('resync with a null head clears the scope', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      expect(ids(list), ['5']);
      c.last.frame({'t': 'resync', 'channel': 'dao:$net:$daoLower', 'seq': 120, 'head': null});
      async.flushMicrotasks();
      expect(list.items, isEmpty);
      expect(client.acquire(net, daoLower).exists, isFalse);
      list.dispose();
      client.dispose();
    });
  });

  test('socket refused: first load from the pointer, polling after 3 failures, socket resumes with since', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector()..refuseAll = true;
      publishH1(f);
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      expect(list.ready, isTrue, reason: 'the pointer gave the head after the first failure');
      expect(ids(list), ['5']);
      final scope = client.acquire(net, daoLower);
      expect(scope.polling, isFalse);
      async.elapse(const Duration(seconds: 4)); // retries after about 1 s and 2 s
      expect(c.uris.length, greaterThanOrEqualTo(3));
      expect(scope.polling, isTrue);

      // a new head reaches the pointer
      final p = [for (var i = 1; i <= 5; i++) proposal('$i', i * 100 * t)];
      daoHead(f, 'H5', proposals: proposalsIndex(f, 'e', [[p[0], p[1]], [p[2], p[3]], [p[4], proposal('6', 600 * t)]]));
      f.pointer(daoPtr, 120, 'H5');
      async.elapse(const Duration(seconds: 21));
      expect(ids(list), ['6', '5']);

      c.refuseAll = false;
      async.elapse(const Duration(seconds: 70));
      expect(c.sockets, isNotEmpty);
      expect(c.last.since, '120');
      c.last.frame(hello(120, 'H5', replay: true));
      async.flushMicrotasks();
      expect(scope.polling, isFalse);
      expect(scope.socketUp, isTrue);
      client.release(scope);
      list.dispose();
      client.dispose();
    });
  });

  test('no hello within 10 s starts polling', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final scope = client.acquire(net, daoLower);
      async.flushMicrotasks();
      expect(scope.polling, isFalse);
      async.elapse(const Duration(seconds: 11));
      expect(scope.polling, isTrue);
      expect(scope.headHash, 'H1');
      client.release(scope);
      client.dispose();
    });
  });

  test('hello from a channel that never received anything falls back to the pointer', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      c.last.frame(hello(0, null));
      async.flushMicrotasks();
      expect(f.count(daoPtr), 1);
      expect(ids(list), ['5']);
      list.dispose();
      client.dispose();
    });
  });

  test('object 404: the head is refreshed from the pointer and the read retried once', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      f.files.remove('$net/o/ap2.json'); // H1's newest page was garbage collected
      final p = [for (var i = 1; i <= 5; i++) proposal('$i', i * 100 * t)];
      daoHead(f, 'H6', proposals: proposalsIndex(f, 'g', [[p[0], p[1]], [p[2], p[3]], [p[4], proposal('6', 600 * t)]]));
      f.pointer(daoPtr, 130, 'H6');
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      expect(list.error, isNull);
      expect(ids(list), ['6', '5']);
      list.dispose();
      client.dispose();
    });
  });

  test('watchDoc: through loc, live updates, delete; a doc not indexed yet is null until pushed', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final seen = <DocEntry?>[];
      final missing = <DocEntry?>[];
      final sub = client.watchDoc(net, daoLower, 'proposals', '3').listen(seen.add);
      final sub2 = client.watchDoc(net, daoChecksummed, 'proposals', '42').listen(missing.add);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      expect(seen.single!.data!['title'], 'P3');
      expect(missing, [null]);

      c.last.frame(changes(101, 'H2', [proposal('3', 300 * t, v: 5, title: 'edited'), proposal('42', 999 * t)]));
      async.flushMicrotasks();
      expect(seen.last!.data!['title'], 'edited');
      expect(missing.last!.id, '42');

      c.last.frame(changes(102, 'H3', [deletion('$daoPath/proposals/3', 9)]));
      async.flushMicrotasks();
      expect(seen.last, isNull);
      sub.cancel();
      sub2.cancel();
      client.dispose();
    });
  });

  test('a stale entry is re-read through the message head', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final p = [for (var i = 1; i <= 5; i++) proposal('$i', i * 100 * t)];
      final big = proposal('5', 500 * t, v: 7, title: 'huge');
      final loc = f.obj('loc7', {
        'loc': {'1': 0, '2': 0, '3': 1, '4': 1, '5': 2}
      });
      daoHead(f, 'H7', proposals: proposalsIndex(f, 's', [[p[0], p[1]], [p[2], p[3]], [big]], views: {'loc': loc}));
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      c.last.frame(changes(101, 'H7', [
        {'path': '$daoPath/proposals/5', 'id': '5', 'v': 7, 'stale': true}
      ]));
      async.flushMicrotasks();
      expect(list.items, ['5:huge']);
      list.dispose();
      client.dispose();
    });
  });

  test('scopes are reference counted and close after the grace period', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final a = client.acquire(net, daoLower);
      final b = client.acquire(net, daoChecksummed);
      expect(identical(a, b), isTrue, reason: 'one scope per lowercase DAO address');
      async.flushMicrotasks();
      expect(c.sockets.length, 1);
      client.release(a);
      client.release(b);
      async.elapse(const Duration(seconds: 10));
      final again = client.acquire(net, daoLower);
      expect(identical(again, a), isTrue, reason: 'reacquired within the grace period');
      client.release(again);
      async.elapse(const Duration(seconds: 31));
      expect(a.closed, isTrue);
      expect(c.last.closed, isTrue);
      expect(client.openScopes, isEmpty);
      client.dispose();
    });
  });

  test('ping every 30 s, pong ignored', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final s = client.acquire(net, daoLower);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.elapse(const Duration(seconds: 61));
      expect(c.last.sent, ['ping', 'ping']);
      client.release(s);
      client.dispose();
    });
  });

  test('a dead connection (no pong to two pings) is dropped and reconnected with since', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final s = client.acquire(net, daoLower);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      async.elapse(const Duration(seconds: 61));
      expect(c.sockets.length, 1);
      async.elapse(const Duration(seconds: 30)); // third tick: two pings unanswered
      expect(c.sockets.first.closed, isTrue);
      async.elapse(const Duration(seconds: 2));
      expect(c.sockets.length, 2);
      expect(c.last.since, '100');
      client.release(s);
      client.dispose();
    });
  });

  test('pongs keep the connection', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final s = client.acquire(net, daoLower);
      async.flushMicrotasks();
      final sock = c.last;
      sock.frame(hello(100, 'H1'));
      for (var i = 0; i < 6; i++) {
        async.elapse(const Duration(seconds: 30));
        sock.add('pong');
        async.flushMicrotasks();
      }
      expect(c.sockets.length, 1);
      expect(sock.closed, isFalse);
      client.release(s);
      client.dispose();
    });
  });

  test('a server that drops every socket after hello gets a growing backoff, not a reconnect per second', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector()
        ..onConnect = (s) {
          s.frame(hello(100, 'H1'));
          scheduleMicrotask(s.drop);
        };
      publishH1(f);
      final client = makeClient(f, c);
      final s = client.acquire(net, daoLower);
      async.elapse(const Duration(seconds: 60));
      expect(c.sockets.length, lessThanOrEqualTo(7));
      client.release(s);
      client.dispose();
    });
  });

  test('counts follow a head taken from the pointer, not an older frame', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      c.last.frame(changes(101, 'H2', [proposal('6', 600 * t)],
          counts: {'proposals': 6, 'members': 0, 'activity': 0, 'projects': 0}));
      async.flushMicrotasks();
      expect(list.totalCount, 6);
      // the socket goes away for good; polling finds a head with 7 proposals
      c.refuseAll = true;
      c.last.drop();
      final p = [for (var i = 1; i <= 7; i++) proposal('$i', i * 100 * t)];
      daoHead(f, 'H7', proposals: proposalsIndex(f, 'h', [[p[0], p[1]], [p[2], p[3]], [p[4], p[5], p[6]]]));
      f.pointer(daoPtr, 140, 'H7');
      async.elapse(const Duration(seconds: 40));
      final scope = client.acquire(net, daoLower);
      expect(scope.polling, isTrue);
      expect(ids(list).first, '7');
      expect(list.totalCount, 7);
      client.release(scope);
      list.dispose();
      client.dispose();
    });
  });

  test('a backer deleted while pushes were lost disappears after a forced resync', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      final proj = '0x${'d' * 40}';
      Map<String, dynamic> backer(String a) =>
          doc('$daoPath/projects/$proj/backers/$a', 1, {'address': a, 'contribution': '1'});
      final b1 = backer('0x${'1' * 40}');
      final b2 = backer('0x${'2' * 40}');
      Map<String, dynamic> ix(String h, List<Map<String, dynamic>> docs) {
        f.obj('${h}page', {'coll': 'backers', 'docs': docs});
        return {
          'coll': 'backers',
          'count': docs.length,
          'hw': 1,
          'order': {'key': '__name__', 'dir': 'asc'},
          'part': 'rank',
          'pages': [
            {'h': '${h}page', 'n': docs.length, 'lo': [0, docs.first['id']], 'hi': [0, docs.last['id']]}
          ],
          'views': {},
        };
      }

      f.obj('bix1', ix('b1', [b1, b2]));
      f.obj('bix2', ix('b2', [b1]));
      daoHead(f, 'HB1', nested: {'projects/$proj/backers': {'count': 2, 'ix': 'bix1'}});
      daoHead(f, 'HB2', nested: {'projects/$proj/backers': {'count': 1, 'ix': 'bix2'}});
      final client = makeClient(f, c);
      final list = EdgeList<String>(client, net, daoLower, 'projects/$proj/backers', (id, d) => id, loadAll: true);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'HB1'));
      async.flushMicrotasks();
      expect(list.items.length, 2);
      c.last.frame({'t': 'resync', 'channel': 'dao:$net:$daoLower', 'seq': 150, 'head': 'HB2', 'counts': {}});
      async.flushMicrotasks();
      expect(list.items, ['0x${'1' * 40}']);
      list.dispose();
      client.dispose();
    });
  });

  test('a malformed frame is ignored and the socket keeps working', () {
    fakeAsync((async) {
      final f = FakeFetcher();
      final c = FakeConnector();
      publishH1(f);
      final client = makeClient(f, c);
      final list = titles(client);
      async.flushMicrotasks();
      c.last.frame(hello(100, 'H1'));
      async.flushMicrotasks();
      c.last.frame({'t': 'changes', 'seq': 101, 'head': 5, 'changes': 'x'});
      c.last.frame(changes(102, 'H2', [proposal('6', 600 * t)]));
      async.flushMicrotasks();
      expect(ids(list), ['6', '5']);
      list.dispose();
      client.dispose();
    });
  });
}
