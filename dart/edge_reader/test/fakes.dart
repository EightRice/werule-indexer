// Fakes for the edge data layer tests: an in-memory data host and scripted websockets.

import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:edge_reader/src/edge_client.dart';
import 'package:edge_reader/src/transport.dart';

const net = 'N';
const coll = 'idaosN';
final daoLower = '0x${'a' * 40}';
final daoChecksummed = '0x${'A' * 40}';
final daoPath = '$coll/$daoChecksummed';

/// A data host: path -> JSON (absent = 404)
class FakeFetcher implements EdgeFetcher {
  final Map<String, Object?> files = {};
  final List<String> requests = [];
  Object? failWith; // thrown on every request while set

  @override
  Future<Object?> getJson(String path) async {
    requests.add(path);
    if (failWith != null) throw failWith!;
    final f = files[path];
    // a round trip through JSON, like the network
    return f == null ? null : jsonDecode(jsonEncode(f));
  }

  String obj(String hash, Map<String, dynamic> o) {
    files['$net/o/$hash.json'] = o;
    return hash;
  }

  void pointer(String path, int seq, String? head) => files[path] = {'format': 1, 'seq': seq, 'head': head};

  int count(String path) => requests.where((r) => r == path).length;
}

class FakeSocket implements EdgeSocket {
  final Uri uri;
  final _ctrl = StreamController<String>();
  final List<String> sent = [];
  bool closed = false;

  FakeSocket(this.uri);

  @override
  Stream<String> get messages => _ctrl.stream;

  @override
  void send(String text) => sent.add(text);

  @override
  Future<void> close() async {
    closed = true;
    if (!_ctrl.isClosed) await _ctrl.close();
  }

  void frame(Map<String, dynamic> m) => _ctrl.add(jsonEncode(m));

  void add(String text) => _ctrl.add(text);

  /// The server closes the socket
  void drop() {
    if (!_ctrl.isClosed) _ctrl.close();
  }

  String? get since => uri.queryParameters['since'];
}

class FakeConnector {
  final List<FakeSocket> sockets = [];
  final List<Uri> uris = [];
  int failNext = 0;
  bool refuseAll = false;
  void Function(FakeSocket socket)? onConnect; // e.g. send the hello

  Future<EdgeSocket> call(Uri uri) async {
    uris.add(uri);
    if (refuseAll || failNext > 0) {
      if (failNext > 0) failNext--;
      throw Exception('refused');
    }
    final s = FakeSocket(uri);
    sockets.add(s);
    final hook = onConnect;
    if (hook != null) scheduleMicrotask(() => hook(s));
    return s;
  }

  FakeSocket get last => sockets.last;
}

EdgeClient makeClient(FakeFetcher f, FakeConnector c) =>
    EdgeClient(fetcher: f, connector: c.call, liveUrl: 'ws://live.test', random: Random(1));

/// `{"$ts": ...}` for microseconds since epoch
Map<String, String> ts(int micros) =>
    {r'$ts': DateTime.fromMicrosecondsSinceEpoch(micros, isUtc: true).toIso8601String()};

Map<String, dynamic> doc(String path, int v, Map<String, dynamic> data) =>
    {'path': path, 'id': path.split('/').last, 'v': v, 'data': data};

Map<String, dynamic> proposal(String pid, int createdMicros, {int v = 1, String title = ''}) =>
    doc('$daoPath/proposals/$pid', v, {'createdAt': ts(createdMicros), 'title': title.isEmpty ? 'P$pid' : title});

/// A rank page entry for a ListIndex
Map<String, dynamic> pageRef(String hash, List<Map<String, dynamic>> docs, String key) {
  num k(Map<String, dynamic> d) {
    final v = (d['data'] as Map)[key];
    if (v is Map && v[r'$ts'] != null) return DateTime.parse(v[r'$ts'] as String).microsecondsSinceEpoch;
    return 0;
  }

  return {
    'h': hash,
    'n': docs.length,
    'lo': [k(docs.first), (docs.first['id'] as String).toLowerCase()],
    'hi': [k(docs.last), (docs.last['id'] as String).toLowerCase()],
  };
}

/// Publishes pages of proposals (ascending lists of docs) and returns the ListIndex
Map<String, dynamic> proposalsIndex(FakeFetcher f, String tag, List<List<Map<String, dynamic>>> pages,
    {Map<String, dynamic> views = const {}}) {
  final refs = <Map<String, dynamic>>[];
  var i = 0;
  var count = 0;
  for (final p in pages) {
    final h = f.obj('${tag}p$i', {'coll': 'proposals', 'docs': p});
    refs.add(pageRef(h, p, 'createdAt'));
    count += p.length;
    i++;
  }
  return {
    'coll': 'proposals',
    'count': count,
    'hw': 0,
    'order': {'key': 'createdAt', 'dir': 'desc'},
    'part': 'rank',
    'pages': refs,
    'views': views,
  };
}

Map<String, dynamic> emptyList(String coll, String key, String dir) =>
    {'coll': coll, 'count': 0, 'hw': 0, 'order': {'key': key, 'dir': dir}, 'part': 'rank', 'pages': []};

/// A DAO head object
String daoHead(FakeFetcher f, String hash,
    {Map<String, dynamic>? proposals, Map<String, dynamic>? members, Map<String, dynamic>? nested, Map<String, dynamic>? daoDoc}) {
  return f.obj(hash, {
    'format': 1,
    'scope': 'dao:$net:$daoLower',
    'doc': daoDoc ?? doc(daoPath, 1, {'name': 'Dao', 'creationDate': ts(1000)}),
    'lists': {
      'proposals': proposals ?? emptyList('proposals', 'createdAt', 'desc'),
      'members': members ?? {'coll': 'members', 'count': 0, 'hw': 0, 'order': {'key': '', 'dir': 'asc'}, 'part': 'prefix', 'pages': []},
      'activity': emptyList('activity', 'time', 'desc'),
      'projects': emptyList('projects', 'createdAt', 'desc'),
    },
    'nested': nested ?? {},
  });
}

String get daoPtr => '$net/d/$daoLower/ptr.json';

Map<String, dynamic> hello(int seq, String? head, {bool replay = false}) =>
    {'t': 'hello', 'channel': 'dao:$net:$daoLower', 'seq': seq, 'head': head, 'replay': replay};

Map<String, dynamic> changes(int seq, String head, List<Map<String, dynamic>> entries, {Map<String, int>? counts}) => {
      't': 'changes',
      'channel': 'dao:$net:$daoLower',
      'seq': seq,
      'head': head,
      'counts': counts ?? {'proposals': 0, 'members': 0, 'activity': 0, 'projects': 0},
      'changes': entries,
    };

Map<String, dynamic> deletion(String path, int v) => {'path': path, 'id': path.split('/').last, 'v': v, 'deleted': true};
