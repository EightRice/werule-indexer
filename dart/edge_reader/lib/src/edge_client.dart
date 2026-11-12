// lib/src/edge_client.dart
//
// The read side of the werule indexer's published files: immutable JSON objects and small pointers from the data host, live
// pushes from the websocket host (CONTRACT.md sections 4, 7 and 9).

import 'dart:async';
import 'dart:collection';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';

import 'codec.dart';
import 'doc_store.dart';
import 'transport.dart';

/// Timings of the scope sync; tests shorten them
class EdgeTiming {
  final Duration connectTimeout;
  final int failuresBeforePolling;
  final Duration pollInterval;
  final Duration pollJitter;
  final Duration minBackoff;
  final Duration maxBackoff;
  final Duration ping;
  final Duration releaseGrace;

  const EdgeTiming({
    this.connectTimeout = const Duration(seconds: 10),
    this.failuresBeforePolling = 3,
    this.pollInterval = const Duration(seconds: 15),
    this.pollJitter = const Duration(seconds: 5),
    this.minBackoff = const Duration(seconds: 1),
    this.maxBackoff = const Duration(seconds: 60),
    this.ping = const Duration(seconds: 30),
    this.releaseGrace = const Duration(seconds: 30),
  });
}

/// An immutable object that is no longer on the data host (garbage collected after its head was
/// superseded); the reader refreshes the head and retries once (CONTRACT.md 9.5)
class ObjectGoneException implements Exception {
  final String hash;
  ObjectGoneException(this.hash);
  @override
  String toString() => 'ObjectGoneException($hash)';
}

/// A head object with the seq it was current at
class HeadRef {
  final int seq;
  final String hash;
  final Map<String, dynamic> obj;
  const HeadRef(this.seq, this.hash, this.obj);
}

/// What changed in a scope
class ScopeEvent {
  final Set<String> keys; // store keys whose data or list membership may have changed
  final bool rebased; // lists were re-based on a new head (membership may have changed anywhere)
  final bool counts;
  const ScopeEvent({this.keys = const {}, this.rebased = false, this.counts = false});
}

/// Display order of the lists, used when a nested list is absent from the head
const Map<String, Map<String, String>> _defaultOrder = {
  'votes': {'key': 'cast', 'dir': 'desc'},
  'backers': {'key': '', 'dir': 'asc'},
  'activity': {'key': 'time', 'dir': 'desc'},
  'proposals': {'key': 'createdAt', 'dir': 'desc'},
  'projects': {'key': 'createdAt', 'dir': 'desc'},
  'members': {'key': '', 'dir': 'asc'},
  'daos': {'key': 'creationDate', 'dir': 'desc'},
};

Map<String, dynamic> emptyIndex(String listKey) {
  final coll = listKey.split('/').last;
  return {'coll': coll, 'count': 0, 'hw': 0, 'order': _defaultOrder[coll] ?? const {'key': '', 'dir': 'asc'}, 'pages': const []};
}

class EdgeClient {
  final EdgeFetcher fetcher;
  final EdgeSocketConnector connector;
  final String liveUrl;
  final EdgeTiming timing;
  final Random random;
  final DocStore store = DocStore();

  final Map<String, EdgeScope> _scopes = {};
  final LinkedHashMap<String, Future<Map<String, dynamic>>> _objects = LinkedHashMap();
  static const _maxObjects = 200; // pages are also in the store; evicted ones come back from the HTTP cache

  EdgeClient({required this.fetcher, required this.connector, required String liveUrl, this.timing = const EdgeTiming(), Random? random})
      : liveUrl = liveUrl.endsWith('/') ? liveUrl.substring(0, liveUrl.length - 1) : liveUrl,
        random = random ?? Random();

  /// A client for a data host (https://data.autonet.computer, or a local publisher's directory served
  /// over HTTP) and a live host (wss://live.autonet.computer, or a local Worker)
  factory EdgeClient.connect({required String dataUrl, required String liveUrl}) =>
      EdgeClient(fetcher: HttpEdgeFetcher(dataUrl), connector: connectWebSocket, liveUrl: liveUrl);

  /// A client that reads the files only, without live pushes: a scope reads its pointer at once, and
  /// lists that stay open refresh by polling. For one-shot readers (readNetworks, readDaos).
  factory EdgeClient.filesOnly({required String dataUrl}) =>
      EdgeClient(fetcher: HttpEdgeFetcher(dataUrl), connector: noLiveHost, liveUrl: 'ws://no-live-host');

  /// The connector of a files-only client: there is no live host to connect to
  static Future<EdgeSocket> noLiveHost(Uri _) => Future.error(UnsupportedError('no live host'));

  /// A mutable file (networks.json, a pointer): null on 404
  Future<Object?> file(String path) => fetcher.getJson(path);

  /// An immutable object, fetched once and kept (bounded)
  Future<Map<String, dynamic>> object(String network, String hash) {
    final key = '$network/$hash';
    final cached = _objects.remove(key);
    if (cached != null) {
      _objects[key] = cached;
      return cached;
    }
    final f = fetcher.getJson('$network/o/$hash.json').then((j) {
      if (j == null) throw ObjectGoneException(hash);
      return j as Map<String, dynamic>;
    });
    _objects[key] = f;
    f.then((_) {}, onError: (_) {
      if (identical(_objects[key], f)) _objects.remove(key);
    });
    while (_objects.length > _maxObjects) {
      _objects.remove(_objects.keys.first);
    }
    return f;
  }

  /// The scope of a network (`dao` null) or of one DAO, reference counted: every acquire needs a
  /// matching [release]. The scope stays connected for [EdgeTiming.releaseGrace] after the last
  /// release, so moving between screens of one DAO does not reconnect.
  EdgeScope acquire(String network, [String? dao]) {
    final d = dao?.toLowerCase();
    final id = d == null ? 'net:$network' : 'dao:$network:$d';
    final scope = _scopes.putIfAbsent(id, () => EdgeScope._(this, network, d));
    scope._retain();
    return scope;
  }

  void release(EdgeScope scope) => scope._release();

  Future<T> withScope<T>(String network, String? dao, Future<T> Function(EdgeScope scope) body) async {
    final scope = acquire(network, dao);
    try {
      return await body(scope);
    } finally {
      release(scope);
    }
  }

  void _drop(EdgeScope scope) {
    if (identical(_scopes[scope.id], scope)) _scopes.remove(scope.id);
  }

  /// The app came back from the background: every open scope reconnects with `since`
  void resumeAll() {
    for (final s in _scopes.values.toList()) {
      s.reconnectNow();
    }
  }

  Iterable<EdgeScope> get openScopes => _scopes.values;

  /// One document of a list, live: the current value (null when absent or deleted), then every
  /// change. The doc is found through the list's `loc` view; one that is not indexed yet emits
  /// null and then appears when a push for it arrives (CONTRACT.md 9.6, R12 and R19).
  Stream<DocEntry?> watchDoc(String network, String dao, String listName, String id) {
    late StreamController<DocEntry?> controller;
    EdgeScope? scope;
    void Function(ScopeEvent)? listener;
    var lastV = -2;
    var cancelled = false;

    void emit() {
      final s = scope;
      if (cancelled || s == null) return;
      final e = store.live(s.childKey(listName, id));
      final v = e?.v ?? -1;
      if (v == lastV) return;
      lastV = v;
      controller.add(e);
    }

    Future<void> locate() async {
      final s = scope!;
      try {
        await s.withRetry(() => s.locateInLoc(listName, id));
        emit();
      } catch (e) {
        if (!cancelled) controller.addError(e);
      }
    }

    controller = StreamController<DocEntry?>(onListen: () {
      final s = scope = acquire(network, dao);
      listener = (ev) {
        if (ev.rebased) {
          locate();
        } else if (ev.keys.contains(s.childKey(listName, id))) {
          emit();
        }
      };
      s.addListener(listener!);
      locate();
    }, onCancel: () {
      cancelled = true;
      final s = scope;
      if (s != null) {
        if (listener != null) s.removeListener(listener!);
        release(s);
      }
    });
    return controller.stream;
  }

  void dispose() {
    for (final s in _scopes.values.toList()) {
      s._close();
    }
    _scopes.clear();
  }
}

/// One sync scope: the network (its DAO list) or one DAO with all its lists (CONTRACT.md 9.2)
class EdgeScope {
  final EdgeClient client;
  final String network;
  final String? dao; // lowercase

  EdgeScope._(this.client, this.network, this.dao);

  String get id => dao == null ? 'net:$network' : 'dao:$network:$dao';
  String get channel => id;
  String get pointerPath => dao == null ? '$network/ptr.json' : '$network/d/$dao/ptr.json';

  /// Store key of the scope root: the DAO collection (net scope) or the DAO doc (DAO scope)
  String get rootKey => dao == null ? 'idaos$network'.toLowerCase() : 'idaos$network/$dao'.toLowerCase();

  /// Store key of the collection a list holds
  String collectionKey(String listKey) => dao == null ? rootKey : '$rootKey/${listKey.toLowerCase()}';

  String childKey(String listKey, String id) => '${collectionKey(listKey)}/${id.toLowerCase()}';

  bool isChildOf(String listKey, String storeKey) {
    final prefix = '${collectionKey(listKey)}/';
    return storeKey.startsWith(prefix) && !storeKey.substring(prefix.length).contains('/');
  }

  /// Store keys of a list's collection pushed after [afterSeq]
  Iterable<String> pushedChildren(String listKey, int afterSeq) =>
      [for (final e in pushedAt.entries) if (e.value > afterSeq && isChildOf(listKey, e.key)) e.key];

  // --- current state; seq never moves backwards ---
  int _seq = 0;
  String? _headHash;
  Map<String, int>? _counts;
  bool? _exists;
  HeadRef? _lastHead;
  final Completer<void> _known = Completer<void>();

  /// Push seq per store key (latest), for list membership
  final Map<String, int> pushedAt = {};

  int get seq => _seq;
  String? get headHash => _headHash;
  bool? get exists => _exists;

  final Map<String, ScopeList> _lists = {};
  final List<void Function(ScopeEvent)> _listeners = [];

  void addListener(void Function(ScopeEvent) l) => _listeners.add(l);
  void removeListener(void Function(ScopeEvent) l) => _listeners.remove(l);

  void _emit(ScopeEvent e) {
    for (final l in List.of(_listeners)) {
      l(e);
    }
  }

  // --- lifecycle ---
  int _refs = 0;
  bool _started = false;
  bool _closed = false;
  Timer? _releaseTimer;

  bool get closed => _closed;

  void _retain() {
    _refs++;
    _releaseTimer?.cancel();
    _releaseTimer = null;
    if (!_started) {
      _started = true;
      _connect();
    }
  }

  void _release() {
    if (_refs <= 0) return;
    _refs--;
    if (_refs == 0) {
      _releaseTimer = Timer(client.timing.releaseGrace, _close);
    }
  }

  void _close() {
    if (_closed) return;
    _closed = true;
    _releaseTimer?.cancel();
    _reconnectTimer?.cancel();
    _connectTimer?.cancel();
    _pingTimer?.cancel();
    _stableTimer?.cancel();
    _stopPolling();
    _connectGen++;
    _dropSocket();
    client._drop(this);
  }

  // --- socket ---
  EdgeSocket? _socket;
  StreamSubscription<String>? _sub;
  bool _connecting = false;
  int _connectGen = 0; // bumped to orphan a connect in flight
  bool _socketUp = false; // hello received on the current socket
  int _failures = 0; // connect attempts in a row that ended without a hello
  int _attempt = 0; // reconnect backoff exponent
  int _quietPings = 0; // pings sent since the last frame from the server
  Timer? _stableTimer; // resets the backoff once a socket has stayed up for a while
  Timer? _reconnectTimer;
  Timer? _connectTimer;
  Timer? _pingTimer;
  Timer? _pollTimer;

  bool get socketUp => _socketUp;
  bool get polling => _pollTimer != null;

  Uri _wsUri() => Uri.parse('${client.liveUrl}/ws').replace(queryParameters: {
        'channel': channel,
        if (_seq > 0) 'since': '$_seq',
      });

  Future<void> _connect() async {
    if (_closed || _socket != null || _connecting) return;
    _connecting = true;
    final gen = ++_connectGen;
    _connectTimer?.cancel();
    _connectTimer = Timer(client.timing.connectTimeout, () {
      if (!_socketUp) _startPolling();
    });
    EdgeSocket s;
    final pending = client.connector(_wsUri());
    try {
      s = await pending.timeout(client.timing.connectTimeout);
    } catch (_) {
      // A socket that connects after the timeout is closed at once
      pending.then((late) => late.close(), onError: (_) {});
      if (gen != _connectGen) return;
      _connecting = false;
      _onSocketLost(null);
      return;
    }
    if (gen != _connectGen || _closed) {
      s.close();
      return;
    }
    _connecting = false;
    _socket = s;
    _socketUp = false;
    _quietPings = 0;
    _sub = s.messages.listen(_onFrame,
        onError: (Object _) => _onSocketLost(s), onDone: () => _onSocketLost(s), cancelOnError: true);
    _pingTimer?.cancel();
    _pingTimer = Timer.periodic(client.timing.ping, (_) {
      if (!identical(s, _socket)) return;
      // Two pings without a pong: the connection is dead even if the socket never reported it
      if (_quietPings >= 2) {
        _onSocketLost(s);
        return;
      }
      _quietPings++;
      try {
        s.send('ping');
      } catch (_) {}
    });
  }

  void _dropSocket() {
    final s = _socket;
    _socket = null;
    _socketUp = false;
    _sub?.cancel();
    _sub = null;
    _pingTimer?.cancel();
    _stableTimer?.cancel();
    if (s != null) {
      s.close().catchError((_) {});
    }
  }

  void _onSocketLost(EdgeSocket? s) {
    if (s != null && !identical(s, _socket)) return;
    final hadHello = _socketUp;
    _dropSocket();
    if (_closed) return;
    if (!hadHello) _failures++;
    if (_failures >= client.timing.failuresBeforePolling) _startPolling();
    // The first load does not wait on a socket that fails: the pointer has the head too
    if (!_known.isCompleted) _pollOnce();
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    _reconnectTimer?.cancel();
    final t = client.timing;
    final ms = min(t.maxBackoff.inMilliseconds, t.minBackoff.inMilliseconds * (1 << min(_attempt, 20)));
    _attempt++;
    final jittered = (ms * (0.75 + client.random.nextDouble() * 0.5)).round();
    _reconnectTimer = Timer(Duration(milliseconds: jittered), _connect);
  }

  /// Reconnect at once with `since` (tab resumed from background)
  void reconnectNow() {
    if (_closed) return;
    _reconnectTimer?.cancel();
    _attempt = 0;
    _dropSocket();
    _connectGen++;
    _connecting = false;
    if (polling) _pollOnce();
    _connect();
  }

  void _onFrame(String text) {
    _quietPings = 0;
    if (text == 'pong') return;
    try {
      final msg = jsonDecode(text);
      if (msg is! Map<String, dynamic>) return;
      switch (msg['t']) {
        case 'hello':
          _onHello(msg);
          break;
        case 'changes':
          applyChanges(msg);
          break;
        case 'resync':
          _adopt(_int(msg['seq']), msg['head'] as String?, _countsOf(msg['counts']), rebase: true);
          break;
      }
    } catch (e) {
      debugPrint('[edge] bad frame on $channel: $e');
    }
  }

  static int _int(Object? v) => v is num ? v.toInt() : 0;

  static Map<String, int>? _countsOf(Object? v) =>
      v is Map ? {for (final e in v.entries) if (e.value is num) e.key.toString(): (e.value as num).toInt()} : null;

  void _onHello(Map<String, dynamic> msg) {
    _socketUp = true;
    _failures = 0;
    // The backoff resets only once the socket has stayed up: a server that sends hello and then
    // drops every socket must not get a reconnect per second from every client
    _stableTimer?.cancel();
    _stableTimer = Timer(client.timing.maxBackoff, () => _attempt = 0);
    _connectTimer?.cancel();
    _stopPolling();
    final s = _int(msg['seq']);
    final head = msg['head'] as String?;
    final counts = _countsOf(msg['counts']);
    if (msg['replay'] == true && _known.isCompleted) return; // nothing lost; buffered frames follow
    if (head == null && s == 0) {
      _pollOnce(); // this channel's DO has never received anything
      return;
    }
    _adopt(s, head, counts, rebase: true);
  }

  /// Applies a `changes` message (from the socket, or a test)
  void applyChanges(Map<String, dynamic> msg) {
    final s = _int(msg['seq']);
    final head = msg['head'] as String?;
    final counts = _countsOf(msg['counts']);
    final keys = <String>{};
    final stale = <DocEntry>[];
    for (final raw in (msg['changes'] as List?) ?? const []) {
      final e = DocEntry.fromJson(Map<String, dynamic>.from(raw as Map));
      if (e.stale) {
        stale.add(e);
      } else {
        client.store.merge(e);
      }
      keys.add(e.key);
      final prev = pushedAt[e.key];
      if (prev == null || s > prev) pushedAt[e.key] = s;
    }
    var countsChanged = false;
    if (s > _seq) {
      _seq = s;
      _headHash = head;
      _exists = head != null;
      if (counts != null) {
        countsChanged = !mapEquals(counts, _counts);
        _counts = counts;
      }
      if (!_known.isCompleted) _known.complete();
    }
    for (final e in stale) {
      _rereadStale(e, head);
    }
    _emit(ScopeEvent(keys: keys, counts: countsChanged));
  }

  /// Takes a head from a hello, a resync or a pointer; ignored when older than the current seq.
  /// With [rebase], loaded lists are re-based on it (CONTRACT.md 9.3).
  void _adopt(int s, String? head, Map<String, int>? counts, {required bool rebase}) {
    if (s < _seq) return;
    final changed = head != _headHash || _exists == null || (head == null && _exists != false);
    _seq = s;
    if (counts != null) {
      _counts = counts;
    } else if (changed) {
      // A pointer carries no counts: the new head's own counts apply, not an older frame's
      _counts = null;
    }
    if (head == null) {
      _headHash = null;
      _exists = false;
      _counts = null;
      if (!_known.isCompleted) _known.complete();
      if (changed) {
        for (final l in _lists.values) {
          l._clear();
        }
        _emit(const ScopeEvent(rebased: true, counts: true));
      }
      return;
    }
    _headHash = head;
    _exists = true;
    if (!_known.isCompleted) _known.complete();
    if (rebase && changed) {
      _rebaseAll();
    } else if (counts != null) {
      _emit(const ScopeEvent(counts: true));
    }
  }

  Future<void> _rebaseAll() async {
    for (final l in _lists.values.toList()) {
      try {
        await l.rebase();
      } catch (e) {
        debugPrint('[edge] rebase ${l.key}: $e');
      }
    }
    if (!_closed) _emit(const ScopeEvent(rebased: true, counts: true));
  }

  // --- polling fallback (CONTRACT.md 9.4) ---
  Future<void>? _pollInFlight;

  void _startPolling() {
    if (_pollTimer != null || _closed) return;
    _pollOnce();
    _schedulePoll();
  }

  void _schedulePoll() {
    final t = client.timing;
    final jitter = Duration(milliseconds: (client.random.nextDouble() * t.pollJitter.inMilliseconds).round());
    _pollTimer = Timer(t.pollInterval + jitter, () {
      if (_closed) return;
      _pollOnce();
      _schedulePoll();
    });
  }

  void _stopPolling() {
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  Future<void> _pollOnce() => _pollInFlight ??= _readPointer().whenComplete(() => _pollInFlight = null);

  Future<void> _readPointer() async {
    Object? ptr;
    try {
      ptr = await client.file(pointerPath);
    } catch (e) {
      debugPrint('[edge] pointer $pointerPath: $e');
      return;
    }
    if (_closed) return;
    if (ptr == null) {
      // No pointer: the scope does not exist (yet)
      if (!_known.isCompleted) {
        _exists = false;
        _known.complete();
        _emit(const ScopeEvent(rebased: true));
      }
      return;
    }
    final p = ptr as Map<String, dynamic>;
    _adopt(_int(p['seq']), p['head'] as String?, null, rebase: true);
  }

  /// Re-reads the pointer (after an object 404)
  Future<void> refreshFromPointer() => _pollOnce();

  /// Runs [body]; on an object 404 refreshes the head once and runs it again
  Future<T> withRetry<T>(Future<T> Function() body) async {
    try {
      return await body();
    } on ObjectGoneException {
      await refreshFromPointer();
      return await body();
    }
  }

  // --- reading through the current head ---

  /// Whether the scope's head (or its absence) is known yet
  bool get known => _known.isCompleted;

  /// The current head, null when the scope does not exist or was deleted. Waits for the first head.
  Future<HeadRef?> currentHead() async {
    await _known.future;
    for (var i = 0; i < 5; i++) {
      final h = _headHash;
      final s = _seq;
      if (h == null) return null;
      final last = _lastHead;
      if (last != null && last.hash == h) return HeadRef(s, h, last.obj);
      final obj = await client.object(network, h);
      if (_headHash == h) {
        final ref = HeadRef(s, h, obj);
        _lastHead = ref;
        final d = obj['doc'];
        if (d is Map) _mergeEntries([d]);
        return ref;
      }
    }
    throw StateError('head of $id keeps moving');
  }

  /// Count of a top-level list: the latest frame's counts, else the head's
  int? count(String list) {
    final c = _counts?[list];
    if (c != null) return c;
    if (_exists == false) return 0;
    final h = _lastHead;
    if (h != null && h.hash == _headHash) return (((h.obj['lists'] as Map?)?[list] as Map?)?['count'] as num?)?.toInt();
    return null;
  }

  Future<int> countAsync(String list) async {
    final h = await currentHead();
    if (h == null) return 0;
    return count(list) ?? 0;
  }

  /// The scope root doc (the DAO doc of a DAO scope), max-v merged with pushes
  Future<DocEntry?> rootDoc() async {
    final h = await currentHead();
    final pushed = pushedAt[rootKey];
    if (h == null) return pushed != null ? client.store.live(rootKey) : null;
    if (h.obj['doc'] == null && (pushed == null || pushed <= h.seq)) return null;
    return client.store.live(rootKey);
  }

  Future<Map<String, dynamic>> nestedOf(HeadRef h) async {
    final n = h.obj['nested'];
    if (n is Map<String, dynamic>) return n;
    final ref = h.obj['nestedRef'];
    if (ref is String) return ((await client.object(network, ref))['nested'] as Map?)?.cast<String, dynamic>() ?? {};
    return {};
  }

  /// The ListIndex of [listKey] under head [h] (an empty index when absent)
  Future<Map<String, dynamic>> indexFor(HeadRef h, String listKey) async {
    if (!listKey.contains('/')) {
      final ix = (h.obj['lists'] as Map?)?[listKey];
      return ix is Map<String, dynamic> ? ix : emptyIndex(listKey);
    }
    final e = (await nestedOf(h))[listKey.toLowerCase()];
    if (e is! Map || e['ix'] is! String) return emptyIndex(listKey);
    return client.object(network, e['ix'] as String);
  }

  /// Fetches a page object and merges its docs; returns their store keys in page order
  Future<List<String>> fetchPage(String hash) async {
    final obj = await client.object(network, hash);
    return _mergeEntries((obj['docs'] as List?) ?? const []);
  }

  List<String> _mergeEntries(List docs) {
    final keys = <String>[];
    final changed = <String>{};
    for (final raw in docs) {
      final e = DocEntry.fromJson(Map<String, dynamic>.from(raw as Map));
      if (client.store.merge(e)) changed.add(e.key);
      keys.add(e.key);
    }
    if (changed.isNotEmpty) _emit(ScopeEvent(keys: changed));
    return keys;
  }

  /// Finds one doc of a list with a `loc` view (proposals, projects) and merges its page
  Future<void> locateInLoc(String listName, String id) async {
    final h = await currentHead();
    if (h == null) return;
    final ix = await indexFor(h, listName);
    final locHash = (ix['views'] as Map?)?['loc'];
    if (locHash is! String) return;
    final loc = ((await client.object(network, locHash))['loc'] as Map?) ?? const {};
    final i = loc[id.toLowerCase()];
    final pages = (ix['pages'] as List?) ?? const [];
    if (i is num && i.toInt() < pages.length) await fetchPage((pages[i.toInt()] as Map)['h'] as String);
  }

  /// A doc pushed as `stale` (too large for a push) is re-read through the message's head
  Future<void> _rereadStale(DocEntry e, String? head) async {
    if (head == null) return;
    try {
      final key = e.key;
      if (dao == null) {
        // A DAO doc: its own scope's head carries it
        final d = key.split('/').last;
        final ptr = await client.file('$network/d/$d/ptr.json') as Map<String, dynamic>?;
        final h = ptr?['head'];
        if (h is String) {
          final doc = (await client.object(network, h))['doc'];
          if (doc is Map) _mergeEntries([doc]);
        }
        return;
      }
      final obj = await client.object(network, head);
      final ref = HeadRef(_seq, head, obj);
      if (key == rootKey) {
        final doc = obj['doc'];
        if (doc is Map) _mergeEntries([doc]);
        return;
      }
      final rel = key.substring(rootKey.length + 1).split('/');
      final listKey = rel.sublist(0, rel.length - 1).join('/');
      final id = rel.last;
      final ix = await indexFor(ref, listKey);
      final locHash = (ix['views'] as Map?)?['loc'];
      final pages = (ix['pages'] as List?) ?? const [];
      if (locHash is String) {
        final loc = ((await client.object(network, locHash))['loc'] as Map?) ?? const {};
        final i = loc[id];
        if (i is num && i.toInt() < pages.length) await fetchPage((pages[i.toInt()] as Map)['h'] as String);
        return;
      }
      if (listKey == 'members') {
        for (final p in pages) {
          final prefix = (p as Map)['p'] as String? ?? '';
          if (id.startsWith('0x') && id.substring(2).startsWith(prefix)) await fetchPage(p['h'] as String);
        }
        return;
      }
      // No locator: scan pages newest first
      for (final p in pages.reversed.take(20)) {
        final keys = await fetchPage((p as Map)['h'] as String);
        if (keys.contains(key)) return;
      }
    } catch (err) {
      debugPrint('[edge] stale re-read ${e.path}: $err');
    }
  }

  /// The shared list state for [listKey]; every call needs a matching [unlist]
  ScopeList list(String listKey, {bool loadAll = false}) {
    final l = _lists.putIfAbsent(listKey.toLowerCase(), () => ScopeList._(this, listKey.toLowerCase()));
    l._users++;
    if (loadAll) l.loadAll = true;
    return l;
  }

  void unlist(ScopeList l) {
    l._users--;
    if (l._users <= 0) _lists.remove(l.key);
  }
}

/// One list of a scope: the pages fetched for its base head plus the pushes newer than that head
/// (CONTRACT.md 9.2, "List contents")
class ScopeList {
  final EdgeScope scope;
  final String key;
  bool loadAll = false;
  int _users = 0;

  ScopeList._(this.scope, this.key);

  Map<String, dynamic>? _index;
  String? baseHash;
  int baseSeq = -1;
  final Map<String, List<String>> _pageKeys = {}; // page hash -> store keys
  int _loadedFromEnd = 0; // newest pages fetched
  Future<void> _queue = Future.value();

  String get collectionKey => scope.collectionKey(key);
  bool get loaded => _index != null;
  List get _pages => (_index?['pages'] as List?) ?? const [];
  bool get hasMore => loaded && _loadedFromEnd < _pages.length;
  int get pageCount => _pages.length;
  int get loadedPages => _loadedFromEnd;
  Map<String, dynamic> get order => ((_index?['order'] as Map?) ?? const {}).cast<String, dynamic>();
  int? get indexCount => (_index?['count'] as num?)?.toInt();

  bool isChild(String storeKey) => scope.isChildOf(key, storeKey);

  Future<T> _serial<T>(Future<T> Function() body) {
    final r = _queue.then((_) => body());
    _queue = r.then((_) {}, onError: (_) {});
    return r;
  }

  void _clear() {
    _index = emptyIndex(key);
    baseHash = null;
    baseSeq = scope.seq;
    _pageKeys.clear();
    _loadedFromEnd = 0;
  }

  /// Oldest `[k, id]` of the fetched window; null when every page is fetched
  (num, String)? get windowLo {
    if (!loaded || loadAll || _loadedFromEnd >= _pages.length || _loadedFromEnd == 0) return null;
    final lo = (_pages[_pages.length - _loadedFromEnd] as Map)['lo'];
    if (lo is List && lo.length == 2) return ((lo[0] as num), lo[1].toString());
    return null;
  }

  Future<Map<String, List<String>>> _fetchNewest(Map<String, dynamic> ix, bool Function(int fetched, int docs, Map page) more) async {
    final pages = (ix['pages'] as List?) ?? const [];
    final out = <String, List<String>>{};
    if (loadAll) {
      final all = await Future.wait([for (final p in pages) scope.fetchPage((p as Map)['h'] as String)]);
      for (var i = 0; i < pages.length; i++) {
        out[(pages[i] as Map)['h'] as String] = all[i];
      }
      return out;
    }
    var docs = 0;
    for (var i = pages.length - 1; i >= 0; i--) {
      final p = pages[i] as Map;
      if (out.isNotEmpty && !more(out.length, docs, p)) break;
      final keys = await scope.fetchPage(p['h'] as String);
      out[p['h'] as String] = keys;
      docs += keys.length;
    }
    return out;
  }

  /// First load: the newest pages until [minDocs] docs (all pages with [loadAll])
  Future<void> load({int minDocs = 1}) => _serial(() async {
        if (loaded) return;
        await scope.withRetry(() async {
          final h = await scope.currentHead();
          if (h == null) {
            _clear();
            return;
          }
          final ix = await scope.indexFor(h, key);
          final fetched = await _fetchNewest(ix, (n, docs, _) => docs < minDocs);
          _apply(h, ix, fetched);
        });
      });

  void _apply(HeadRef h, Map<String, dynamic> ix, Map<String, List<String>> fetched) {
    _index = ix;
    baseHash = h.hash;
    baseSeq = h.seq;
    _pageKeys
      ..clear()
      ..addAll(fetched);
    _loadedFromEnd = fetched.length;
  }

  /// The next older pages of the current base, until [minDocs] more docs
  Future<void> loadMore({int minDocs = 1}) => _serial(() async {
        if (!hasMore) return;
        Future<void> once() async {
          var docs = 0;
          while (_loadedFromEnd < _pages.length && (docs == 0 || docs < minDocs)) {
            final p = _pages[_pages.length - 1 - _loadedFromEnd] as Map;
            final keys = await scope.fetchPage(p['h'] as String);
            _pageKeys[p['h'] as String] = keys;
            _loadedFromEnd++;
            docs += keys.length;
            if (keys.isEmpty) break;
          }
        }

        try {
          await once();
        } on ObjectGoneException {
          await scope.refreshFromPointer();
          await _rebaseNow();
          await once();
        }
      });

  /// Re-bases on the scope's current head: the new index, the same window, only new pages fetched
  Future<void> rebase() => _serial(_rebaseNow);

  Future<void> _rebaseNow() async {
    if (!loaded) return;
    await scope.withRetry(() async {
      final h = await scope.currentHead();
      if (h == null) {
        _clear();
        return;
      }
      if (h.hash == baseHash) return;
      final ix = await scope.indexFor(h, key);
      final everything = loadAll || _loadedFromEnd >= _pages.length;
      final lo = windowLo;
      final fetched = everything
          ? await _fetchAllOf(ix)
          // keep fetching while the next page still overlaps the old window
          : await _fetchNewest(ix, (n, docs, page) => lo == null ? n < _loadedFromEnd : _hiAtLeast(page, lo));
      _apply(h, ix, fetched);
    });
  }

  static bool _hiAtLeast(Map page, (num, String) lo) {
    final hi = page['hi'];
    if (hi is! List || hi.length != 2) return false;
    return comparePos(hi[0] as num, hi[1].toString(), lo.$1, lo.$2) >= 0;
  }

  Future<Map<String, List<String>>> _fetchAllOf(Map<String, dynamic> ix) async {
    final pages = (ix['pages'] as List?) ?? const [];
    final all = await Future.wait([for (final p in pages) scope.fetchPage((p as Map)['h'] as String)]);
    return {for (var i = 0; i < pages.length; i++) (pages[i] as Map)['h'] as String: all[i]};
  }

  /// The list's docs in display order
  List<DocEntry> docs() {
    if (!loaded) return const [];
    final store = scope.client.store;
    final pageSet = <String>{for (final ks in _pageKeys.values) ...ks};
    final keys = <String>{...pageSet};
    scope.pushedAt.forEach((k, s) {
      if (s > baseSeq && isChild(k)) keys.add(k);
    });
    final lo = windowLo;
    final orderKey = order['key'] as String?;
    final rows = <(num, DocEntry)>[];
    for (final k in keys) {
      final e = store.live(k);
      if (e == null) continue;
      final kv = sortValue(e.data, orderKey);
      if (lo != null && !pageSet.contains(k) && comparePos(kv, e.id, lo.$1, lo.$2) < 0) continue;
      rows.add((kv, e));
    }
    rows.sort((a, b) => comparePos(a.$1, a.$2.id, b.$1, b.$2.id));
    final out = [for (final r in rows) r.$2];
    return order['dir'] == 'desc' ? out.reversed.toList() : out;
  }
}
